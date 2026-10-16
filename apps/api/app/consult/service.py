"""Orquestração da consulta com IA: falas → Smart Apps (regras na hora + IA em ciclos) → nota → prontuário."""

from __future__ import annotations

import time
from datetime import datetime

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.config import get_settings
from app.consult import ai, knowledge, stt
from app.consult.engine import PATIENT_SIDE, Engine, infer_speaker
from app.consult.export import document_reference, note_as_text
from app.consult.schemas import (
    ConsultCreate,
    ConsultNote,
    Insights,
    Segment,
    SegmentIn,
    SurgicalReport,
    fala_ref,
)
from app.db.models import Consultation, User
from app.fhir_client.client import FhirSource
from app.llm.client import LLMError
from app.llm.pseudonymize import Pseudonymizer
from app.patients.service import load_record
from app.timeline.context import PatientContext, build_context
from app.timeline.events import ClinicalEvent, build_timeline
from app.timeline.record import PatientRecord, parse_dt

ACTIVE_VISIT = ("planned", "arrived", "triaged", "in-progress")
_CHART_TTL_S = 30.0
_chart_cache: dict[str, tuple[float, PatientRecord, list[ClinicalEvent], PatientContext]] = {}


def _chart(fhir: FhirSource, patient_id: str) -> tuple[PatientRecord, list[ClinicalEvent], PatientContext]:
    """Prontuário lido do FHIR; cache curto porque a análise roda a cada fala."""
    hit = _chart_cache.get(patient_id)
    if hit and time.monotonic() - hit[0] < _CHART_TTL_S:
        return hit[1], hit[2], hit[3]
    record = load_record(fhir, patient_id)
    events = build_timeline(record)
    ctx = build_context(record)
    _chart_cache[patient_id] = (time.monotonic(), record, events, ctx)
    return record, events, ctx


def clear_chart_cache() -> None:
    _chart_cache.clear()


def _segments(c: Consultation) -> list[Segment]:
    return [Segment.model_validate(s) for s in c.segments_json or []]


def _engine(c: Consultation, record, events, ctx, segments: list[Segment]) -> Engine:
    return Engine(record, events, ctx, segments, c.specialty, c.kind, clock.now(), c.extra_json or {})


def _valid_refs(record: PatientRecord, events: list[ClinicalEvent], ctx: PatientContext, segments) -> set[str]:
    refs = {e.source_ref for e in events} | ctx.refs() | {fala_ref(s.n) for s in segments}
    for rtype, items in record.resources.items():
        refs |= {f"{rtype}/{r['id']}" for r in items}
    return refs


def _pseudo(record: PatientRecord) -> Pseudonymizer:
    return Pseudonymizer(**record.identifiers_for_pseudonymization())


def reidentify(data, record: PatientRecord):
    """Troca os tokens da pseudonimização pelos nomes reais (só para exibir/exportar, nunca para o LLM)."""
    family = [((rp.get("name") or [{}])[0]).get("text") for rp in record.related_persons]
    family = [f for f in family if f]
    relative = family[0] if len(family) == 1 else "familiar"
    if isinstance(data, str):
        return data.replace("[PACIENTE]", record.name).replace("[FAMILIAR]", relative)
    if isinstance(data, list):
        return [reidentify(v, record) for v in data]
    if isinstance(data, dict):
        return {k: reidentify(v, record) for k, v in data.items()}
    return data


def _get(db: Session, cid: int) -> Consultation:
    c = db.get(Consultation, cid)
    if c is None:
        raise HTTPException(404, "Consulta não encontrada")
    return c


def _insights(c: Consultation, record, events, ctx, segments: list[Segment]) -> Insights:
    """Regras na hora + a última análise da IA re-mesclada (a IA atualiza em ciclos, as regras a cada fala)."""
    base = _engine(c, record, events, ctx, segments).run()
    meta = c.ai_meta_json or {}
    if meta.get("saida") and meta.get("modo") == "real" and c.kind != "cirurgia":
        valid = _valid_refs(record, events, ctx, segments)
        return ai.merge_live(base, ai.ConsultLiveOutput.model_validate(meta["saida"]), valid, segments)
    return base


def out(c: Consultation, user_names: dict[int, str] | None = None) -> dict:
    meta = dict(c.ai_meta_json or {})
    meta.pop("saida", None)
    return {
        "id": c.id,
        "patient_id": c.patient_id,
        "encounter_id": c.encounter_id,
        "tipo": c.kind,
        "especialidade": c.specialty,
        "especialidade_rotulo": knowledge.SPECIALTIES.get(c.specialty, c.specialty),
        "idioma_paciente": c.patient_language,
        "status": c.status,
        "segmentos": c.segments_json or [],
        "insights": c.insights_json,
        "ia": meta or None,
        "nota": c.note_json,
        "nota_meta": c.note_meta_json,
        "fhir_ref": c.fhir_ref,
        "roteiro_demo": (c.extra_json or {}).get("roteiro"),
        "criado_por": (user_names or {}).get(c.created_by or 0),
        "created_at": c.created_at.isoformat(),
        "finished_at": c.finished_at.isoformat() if c.finished_at else None,
        "sent_at": c.sent_at.isoformat() if c.sent_at else None,
    }


# ---------- criar / listar ----------
def create(db: Session, fhir: FhirSource, user: User, patient_id: str, body: ConsultCreate) -> dict:
    if body.especialidade not in knowledge.SPECIALTIES:
        raise HTTPException(422, "Especialidade desconhecida")
    record, events, ctx = _chart(fhir, patient_id)
    visits = [
        e
        for e in record.encounters.values()
        if e.get("class", {}).get("code") == "AMB" and e.get("status") in ACTIVE_VISIT
    ]
    encounter_id = visits[0]["id"] if visits else (record.hospitalization or {}).get("id")
    script = next(
        (
            s
            for s in knowledge.demo_scripts().values()
            if s.get("patient_id") == patient_id and s.get("tipo") == body.tipo
        ),
        None,
    )
    extra = {"roteiro": script["id"], "opme_autorizado": script.get("opme_autorizado", [])} if script else {}
    c = Consultation(
        patient_id=patient_id,
        encounter_id=encounter_id,
        kind=body.tipo,
        specialty=body.especialidade,
        patient_language=body.idioma_paciente,
        status="em_andamento",
        segments_json=[],
        extra_json=extra,
        created_by=user.id,
        created_at=clock.now(),
    )
    db.add(c)
    db.flush()
    c.insights_json = _insights(c, record, events, ctx, []).model_dump(mode="json")
    log_action(
        db,
        user,
        "consulta.iniciada",
        "consultation",
        c.id,
        {"patient_id": patient_id, "tipo": c.kind, "especialidade": c.specialty, "idioma": c.patient_language},
    )
    return out(c)


def list_for_patient(db: Session, patient_id: str) -> list[dict]:
    rows = db.scalars(
        select(Consultation).where(Consultation.patient_id == patient_id).order_by(Consultation.id.desc())
    ).all()
    return [
        {
            "id": c.id,
            "tipo": c.kind,
            "especialidade": knowledge.SPECIALTIES.get(c.specialty, c.specialty),
            "status": c.status,
            "falas": len(c.segments_json or []),
            "created_at": c.created_at.isoformat(),
            "sent_at": c.sent_at.isoformat() if c.sent_at else None,
        }
        for c in rows
    ]


def get(db: Session, user: User, cid: int) -> dict:
    c = _get(db, cid)
    log_action(db, user, "consulta.visualizada", "consultation", c.id, {"patient_id": c.patient_id})
    return out(c)


# ---------- falas (tempo real) ----------
def append_segments(db: Session, fhir: FhirSource, user: User, cid: int, items: list[SegmentIn]) -> dict:
    c = _get(db, cid)
    if c.status != "em_andamento":
        raise HTTPException(409, "Consulta finalizada: não aceita novas falas.")
    record, events, ctx = _chart(fhir, c.patient_id)
    segments = _segments(c)
    elapsed = max(0.0, (clock.now() - c.created_at).total_seconds())
    new: list[Segment] = []
    for item in items:
        text = item.texto.strip()
        if not text:
            continue
        speaker, auto = item.falante, False
        if speaker == "desconhecido":
            speaker, auto = infer_speaker(text), True
        language = item.idioma or (c.patient_language if speaker in PATIENT_SIDE else "pt-BR")
        seg = Segment(
            n=len(segments) + 1,
            t=item.t if item.t is not None else elapsed,
            falante=speaker,
            falante_auto=auto,
            texto=text,
            idioma=language,
            origem=item.origem,
        )
        segments.append(seg)
        new.append(seg)
    c.segments_json = [s.model_dump() for s in segments]
    ins = _insights(c, record, events, ctx, segments)
    c.insights_json = ins.model_dump(mode="json")
    db.commit()
    return {"novos": [s.model_dump() for s in new], "insights": c.insights_json, "total": len(segments)}


def transcribe_audio(
    db: Session, fhir: FhirSource, user: User, cid: int, audio: bytes, content_type: str, speaker: str
) -> dict:
    c = _get(db, cid)
    language = c.patient_language if speaker in PATIENT_SIDE else "pt-BR"
    try:
        text = stt.transcribe(audio, content_type, language)
    except stt.STTUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    log_action(db, user, "consulta.audio", "consultation", c.id, {"bytes": len(audio), "caracteres": len(text)})
    if not text:
        return {"novos": [], "insights": c.insights_json, "total": len(c.segments_json or [])}
    return append_segments(
        db,
        fhir,
        user,
        cid,
        [SegmentIn(falante=speaker, texto=text, origem="servidor")],  # type: ignore[arg-type]
    )


# ---------- IA (ciclos) ----------
def analyze(db: Session, fhir: FhirSource, user: User, cid: int, force: bool = False) -> dict:
    c = _get(db, cid)
    record, events, ctx = _chart(fhir, c.patient_id)
    segments = _segments(c)
    meta = c.ai_meta_json or {}
    if not force and meta.get("falas") == len(segments) and c.insights_json:
        return out(c)
    engine = _engine(c, record, events, ctx, segments)
    base = engine.run()
    valid = _valid_refs(record, events, ctx, segments)
    pseudo = _pseudo(record)
    started = time.monotonic()
    analyzed = len(segments)
    try:
        if c.kind == "cirurgia":
            merged, result = ai.run_surgical(engine, base, pseudo, valid)
            saida = None
        else:
            _, result = ai.run_live(engine, base, pseudo, valid)
            saida = result.output.model_dump(mode="json")
    except LLMError as exc:
        log_action(db, user, "consulta.erro_llm", "consultation", c.id, {"erro": str(exc)})
        raise HTTPException(502, str(exc)) from exc
    # falas novas podem ter chegado durante a chamada da IA: mescla sobre o estado atual, sem sobrescrevê-las
    db.refresh(c)
    segments = _segments(c)
    if saida is not None:
        fixes = {f.n: f.falante for f in result.output.falantes}  # a IA só corrige falas atribuídas automaticamente
        if any(s.falante_auto and s.n in fixes and fixes[s.n] != s.falante for s in segments):
            for s in segments:
                if s.falante_auto and s.n in fixes:
                    s.falante = fixes[s.n]
            c.segments_json = [s.model_dump() for s in segments]
        valid = _valid_refs(record, events, ctx, segments)
        base = _engine(c, record, events, ctx, segments).run()
        merged = ai.merge_live(base, result.output, valid, segments)
        if result.mode == "mock":
            merged.modo = "regras"
    c.ai_meta_json = {
        "modo": result.mode,
        "modelo": result.model,
        "prompt_version": result.prompt_version,
        "falas": analyzed,
        "em": clock.now().isoformat(),
        "segundos": round(time.monotonic() - started, 1),
        "descartados": merged.descartados,
        "saida": saida,
    }
    c.insights_json = merged.model_dump(mode="json")
    log_action(
        db,
        user,
        "consulta.analise_ia",
        "consultation",
        c.id,
        {
            "patient_id": c.patient_id,
            "falas": analyzed,
            "modo": result.mode,
            "itens_descartados": len(merged.descartados),
            "pseudonimizacoes": pseudo.replacements,
        },
    )
    return out(c)


def translate(db: Session, fhir: FhirSource, user: User, cid: int, ns: list[int] | None) -> dict:
    c = _get(db, cid)
    if c.patient_language == "pt-BR":
        return {"traducoes": {}}
    record, _, _ = _chart(fhir, c.patient_id)
    segments = _segments(c)
    targets = {
        s.n: ("pt-BR" if s.falante in PATIENT_SIDE else c.patient_language)
        for s in segments
        if (ns is None or s.n in ns) and not s.traducao
    }
    try:
        done = ai.run_translation(segments, targets, _pseudo(record))
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    for s in segments:
        if s.n in done:
            s.traducao = done[s.n]
    c.segments_json = [s.model_dump() for s in segments]
    log_action(db, user, "consulta.traducao", "consultation", c.id, {"falas": len(done), "idioma": c.patient_language})
    return {"traducoes": {str(k): v for k, v in done.items()}}


# ---------- nota, revisão e envio ----------
def finish(db: Session, fhir: FhirSource, user: User, cid: int) -> dict:
    c = _get(db, cid)
    record, events, ctx = _chart(fhir, c.patient_id)
    segments = _segments(c)
    if not segments:
        raise HTTPException(422, "A consulta não tem falas transcritas.")
    engine = _engine(c, record, events, ctx, segments)
    valid = _valid_refs(record, events, ctx, segments)
    pseudo = _pseudo(record)
    try:
        if c.kind == "cirurgia":
            merged, result = ai.run_surgical(engine, engine.run(), pseudo, valid)
            c.insights_json = merged.model_dump(mode="json")
            note: dict = {"relatorio_cirurgico": merged.relatorio_cirurgico.model_dump(mode="json")}
            dropped = merged.descartados
        else:
            ins = Insights.model_validate(c.insights_json) if c.insights_json else engine.run()
            if ins.falas_analisadas != len(segments):
                ins = _insights(c, record, events, ctx, segments)
            note_model, result, dropped = ai.run_note(engine, ins, pseudo, valid, c.patient_language)
            note = note_model.model_dump(mode="json")
    except LLMError as exc:
        log_action(db, user, "consulta.erro_llm", "consultation", c.id, {"erro": str(exc)})
        raise HTTPException(502, str(exc)) from exc
    c.note_json = note
    c.note_meta_json = {
        "modo": result.mode,
        "modelo": result.model,
        "prompt_version": result.prompt_version,
        "gerada_em": clock.now().isoformat(),
        "descartados": dropped,
        "revisada_por": None,
        "revisada_em": None,
    }
    c.status = "finalizada"
    c.finished_at = clock.now()
    log_action(
        db,
        user,
        "consulta.finalizada",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "falas": len(segments), "modo": result.mode, "itens_descartados": len(dropped)},
    )
    return out(c)


def update_note(db: Session, user: User, cid: int, note: dict) -> dict:
    c = _get(db, cid)
    if c.note_json is None:
        raise HTTPException(409, "Finalize a consulta antes de revisar a nota.")
    if c.status == "enviada":
        raise HTTPException(409, "Nota já enviada ao prontuário.")
    try:
        if c.kind == "cirurgia":
            SurgicalReport.model_validate(note.get("relatorio_cirurgico"))
        else:
            ConsultNote.model_validate(note)
    except ValidationError as exc:
        raise HTTPException(422, f"Nota em formato inválido: {exc.error_count()} erro(s)") from exc
    c.note_json = note
    c.note_meta_json = (c.note_meta_json or {}) | {"revisada_por": user.name, "revisada_em": clock.now().isoformat()}
    log_action(db, user, "consulta.nota_revisada", "consultation", c.id, {"patient_id": c.patient_id})
    return out(c)


def _document(db: Session, fhir: FhirSource, user: User, c: Consultation) -> tuple[str, dict]:
    if c.note_json is None:
        raise HTTPException(409, "Finalize e revise a nota antes de exportar ou enviar.")
    record = load_record(fhir, c.patient_id)
    now = clock.now()
    role = {"medico": "Médico(a)", "enfermagem": "Enfermagem"}.get(user.role, user.role)
    author = f"{user.name} ({role})"
    note = reidentify(c.note_json, record)  # o prontuário recebe os nomes, não os tokens enviados à IA
    text = note_as_text(c.kind, record.name, author, _local(now), note, c.patient_language)
    doc = document_reference(
        kind=c.kind,
        patient_id=c.patient_id,
        patient_name=record.name,
        encounter_id=c.encounter_id,
        author=author,
        when=now,
        text=text,
        consultation_id=c.id,
    )
    return text, doc


def export(db: Session, fhir: FhirSource, user: User, cid: int) -> dict:
    """Texto para colar no prontuário + recurso FHIR, sem enviar nada."""
    c = _get(db, cid)
    text, doc = _document(db, fhir, user, c)
    log_action(db, user, "consulta.exportada", "consultation", c.id, {"patient_id": c.patient_id})
    return {"texto": text, "recurso": doc}


def send_to_ehr(db: Session, fhir: FhirSource, user: User, cid: int) -> dict:
    c = _get(db, cid)
    text, doc = _document(db, fhir, user, c)
    if not get_settings().ehr_writeback:
        log_action(db, user, "consulta.exportada", "consultation", c.id, {"patient_id": c.patient_id})
        return {
            "enviado": False,
            "motivo": "Envio automático desligado (EHR_WRITEBACK=false): copie o texto ou baixe o FHIR.",
            "texto": text,
            "recurso": doc,
            "consulta": out(c),
        }
    if c.status == "enviada" and c.fhir_ref:
        raise HTTPException(409, f"Nota já enviada ({c.fhir_ref}).")
    created = fhir.create(doc)
    c.fhir_ref = f"DocumentReference/{created['id']}"
    c.status = "enviada"
    c.sent_at = clock.now()
    clear_chart_cache()
    log_action(db, user, "consulta.enviada", "consultation", c.id, {"patient_id": c.patient_id, "fhir_ref": c.fhir_ref})
    return {"enviado": True, "fhir_ref": c.fhir_ref, "texto": text, "recurso": created, "consulta": out(c)}


def _local(dt: datetime) -> datetime:
    from zoneinfo import ZoneInfo

    return dt.astimezone(ZoneInfo(get_settings().timezone))


# ---------- agenda do dia ----------
def appointments(db: Session, fhir: FhirSource) -> list[dict]:
    today = _local(clock.now()).date()
    patients = {p["id"]: p for p in fhir.list_patients()}
    rows = []
    for e in fhir.ambulatory_encounters():
        start = parse_dt(e.get("period", {}).get("start"))
        if start is None or _local(start).date() != today:
            continue
        pid = (e.get("subject") or {}).get("reference", "").split("/")[-1]
        p = patients.get(pid)
        if p is None:
            continue
        name = (p.get("name") or [{}])[0].get("text", pid)
        latest = db.scalar(
            select(Consultation)
            .where(Consultation.patient_id == pid, Consultation.kind == "consulta")
            .order_by(Consultation.id.desc())
        )
        rows.append(
            {
                "encounter_id": e["id"],
                "patient_id": pid,
                "nome": name,
                "nascimento": p.get("birthDate"),
                "horario": start.isoformat(),
                "status_fhir": e.get("status"),
                "servico": (e.get("serviceType") or {}).get("text", ""),
                "profissional": ((e.get("participant") or [{}])[0].get("individual") or {}).get("display", ""),
                "motivo": ((e.get("reasonCode") or [{}])[0]).get("text", ""),
                "consulta_id": latest.id if latest and latest.created_at.date() >= today else None,
                "consulta_status": latest.status if latest and latest.created_at.date() >= today else None,
                "cenario": any(t.get("code", "").startswith("cenario") for t in p.get("meta", {}).get("tag", [])),
            }
        )
    rows.sort(key=lambda r: r["horario"])
    return rows

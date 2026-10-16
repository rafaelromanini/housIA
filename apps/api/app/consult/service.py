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
from app.consult import ai, antiglosa, gs1, hipoteses, knowledge, opme, stt
from app.consult import orders as order_drafts
from app.consult.engine import PATIENT_SIDE, Engine, billing_params, infer_speaker
from app.consult.export import (
    claim_resource,
    device_resources,
    document_reference,
    note_as_text,
    procedure_resource,
    transaction_bundle,
)
from app.consult.export import urn as export_urn
from app.consult.schemas import (
    ConsultCreate,
    ConsultNote,
    Insights,
    Segment,
    SegmentIn,
    SurgicalReport,
    fala_ref,
)
from app.consult.surgical import ORDER_SYSTEM
from app.db.models import Consultation, User
from app.fhir_client.client import FhirSource
from app.llm.client import LLMError
from app.llm.pseudonymize import Pseudonymizer
from app.patients.service import load_record
from app.timeline.context import PatientContext, build_context
from app.timeline.events import ClinicalEvent, build_timeline
from app.timeline.record import PatientRecord, parse_dt

ACTIVE_VISIT = ("planned", "arrived", "triaged", "in-progress")
MIN_JUSTIFICATION = order_drafts.MIN_JUSTIFICATION  # justificativa mínima: envio com pendência crítica e pedido crítico
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
    engine = _engine(c, record, events, ctx, segments)
    base = engine.run()
    meta = c.ai_meta_json or {}
    if meta.get("saida") and meta.get("modo") == "real" and c.kind != "cirurgia":
        valid = _valid_refs(record, events, ctx, segments)
        merged = ai.merge_live(base, ai.ConsultLiveOutput.model_validate(meta["saida"]), valid, segments, engine.r.sex)
        ai.reconcile_ai_allergies(engine, merged)
        return hipoteses.apply(merged, c.extra_json)
    return hipoteses.apply(base, c.extra_json)


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
        "opme_guia": (c.extra_json or {}).get("opme_guia"),
        "faturamento": (c.extra_json or {}).get("faturamento"),
        "justificativa_envio": (c.extra_json or {}).get("justificativa_envio"),
        "pedidos_fhir": (c.extra_json or {}).get("pedidos_fhir"),
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
    scripts = [
        s for s in knowledge.demo_scripts().values() if s.get("patient_id") == patient_id and s.get("tipo") == body.tipo
    ]
    script = next((s for s in scripts if s.get("especialidade") == body.especialidade), scripts[0] if scripts else None)
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
    if c.kind == "cirurgia" and ins.relatorio_cirurgico and "prontidao_inicial" not in (c.extra_json or {}):
        # primeira leitura com ditado: base para medir quantas pendências foram resolvidas até o envio
        c.extra_json = (c.extra_json or {}) | {
            "prontidao_inicial": antiglosa.snapshot(ins.relatorio_cirurgico.prontidao)  # type: ignore[arg-type]
        }
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


CHECKLIST_LABEL = {"sign_in": "sign-in", "time_out": "time-out", "sign_out": "sign-out"}


def mark_checklist(db: Session, fhir: FhirSource, user: User, cid: int, step: str, done: bool) -> dict:
    """Cirurgia Segura (OMS) marcada por botão: quem marcou e quando ficam na verificação."""
    c = _get(db, cid)
    if c.kind != "cirurgia":
        raise HTTPException(409, "A lista de verificação de cirurgia segura vale só para o modo cirúrgico.")
    if c.status != "em_andamento":
        raise HTTPException(409, "Relatório finalizado: não aceita novas marcações.")
    record, events, ctx = _chart(fhir, c.patient_id)
    extra = dict(c.extra_json or {})
    checklist = dict(extra.get("checklist") or {})
    checklist[step] = {"feito": done, "por": user.name, "em": clock.now().isoformat()}
    c.extra_json = extra | {"checklist": checklist}
    segments = _segments(c)
    c.insights_json = _insights(c, record, events, ctx, segments).model_dump(mode="json")
    log_action(
        db,
        user,
        "consulta.cirurgia_segura",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "etapa": CHECKLIST_LABEL[step], "feito": done},
    )
    return {"insights": c.insights_json, "total": len(segments)}


def set_main_complaint(db: Session, fhir: FhirSource, user: User, cid: int, key: str | None) -> dict:
    """O profissional fixa (ou solta, com `key` vazio) a queixa principal da anamnese. Vale para o resto da consulta."""
    c = _get(db, cid)
    if c.kind == "cirurgia":
        raise HTTPException(409, "O modo cirúrgico não tem queixa principal.")
    if c.status != "em_andamento":
        raise HTTPException(409, "Consulta finalizada: a queixa principal não pode mais ser alterada.")
    record, events, ctx = _chart(fhir, c.patient_id)
    segments = _segments(c)
    extra = {k: v for k, v in (c.extra_json or {}).items() if k != "qp_principal"}
    if key:
        extra["qp_principal"] = key
    ins = Engine(record, events, ctx, segments, c.specialty, c.kind, clock.now(), extra).run()
    if key and not (ins.anamnese and any(q.chave == key for q in ins.anamnese.queixas_detalhadas)):
        raise HTTPException(422, "Essa queixa não aparece na conversa.")
    c.extra_json = extra
    c.insights_json = _insights(c, record, events, ctx, segments).model_dump(mode="json")
    log_action(
        db,
        user,
        "consulta.queixa_principal",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "queixa": key or None},
    )
    return {"insights": c.insights_json, "total": len(segments)}


def set_hypothesis_decision(
    db: Session, fhir: FhirSource, user: User, cid: int, code: str, decision: str, reason: str
) -> dict:
    """O profissional confirma (ou deixa provisória), descarta ou limpa a decisão de uma hipótese da análise."""
    c = _get(db, cid)
    if c.kind == "cirurgia":
        raise HTTPException(409, "O modo cirúrgico não tem hipóteses diagnósticas.")
    if c.status != "em_andamento":
        raise HTTPException(409, "Consulta finalizada: as hipóteses não podem mais ser alteradas.")
    if decision not in hipoteses.DECISIONS:
        raise HTTPException(422, "Decisão inválida.")
    record, events, ctx = _chart(fhir, c.patient_id)
    segments = _segments(c)
    if not any(
        hipoteses.key(h.cid) == hipoteses.key(code) for h in _insights(c, record, events, ctx, segments).hipoteses
    ):
        raise HTTPException(422, "Essa hipótese não aparece na análise.")
    c.extra_json = hipoteses.decide(c.extra_json or {}, code, decision, reason, user.name, clock.now().isoformat())
    c.insights_json = _insights(c, record, events, ctx, segments).model_dump(mode="json")
    log_action(
        db,
        user,
        "consulta.hipotese",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "cid": hipoteses.key(code), "decisao": decision},
    )
    return {"insights": c.insights_json, "total": len(segments)}


# ---------- OPME: guia, consumo (etiqueta GS1) e faturamento ----------
def _open_surgery(c: Consultation, what: str) -> None:
    if c.kind != "cirurgia":
        raise HTTPException(409, f"{what} vale só para o modo cirúrgico.")
    if c.status != "em_andamento":
        raise HTTPException(409, "Relatório finalizado: edite o OPME na revisão da nota.")


def _refresh(c: Consultation, fhir: FhirSource) -> dict:
    record, events, ctx = _chart(fhir, c.patient_id)
    segments = _segments(c)
    c.insights_json = _insights(c, record, events, ctx, segments).model_dump(mode="json")
    return {"insights": c.insights_json, "total": len(segments)}


def set_opme_guia(db: Session, fhir: FhirSource, user: User, cid: int, body) -> dict:
    """Guia autorizada: entrada manual, ClaimResponse FHIR ou XML TISS. Substitui a guia anterior."""
    c = _get(db, cid)
    _open_surgery(c, "A guia de OPME")
    try:
        if body.itens is not None:
            guia = opme.Guia(
                opme.normalize_items([i.model_dump(exclude_none=True) for i in body.itens]), origem="manual"
            )
        elif body.claim_response is not None:
            guia = opme.from_claim_response(body.claim_response)
        else:
            guia = opme.from_tiss_xml(body.tiss_xml or "")
    except opme.GuiaError as exc:
        raise HTTPException(422, str(exc)) from exc
    number = (body.numero or guia.numero or "").strip() or None
    c.extra_json = (c.extra_json or {}) | {
        "opme_autorizado": guia.itens,
        "opme_guia": {
            "origem": guia.origem,
            "numero": number,
            "senha": guia.senha,
            "itens": len(guia.itens),
            "por": user.name,
            "em": clock.now().isoformat(),
        },
    }
    result = _refresh(c, fhir)
    log_action(
        db,
        user,
        "consulta.opme_guia",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "origem": guia.origem, "itens": len(guia.itens), "numero_guia": number},
    )
    return result | {"guia": c.extra_json["opme_guia"], "autorizado": guia.itens}


def add_opme_consumo(db: Session, fhir: FhirSource, user: User, cid: int, body) -> dict:
    """Consumo de OPME: etiqueta GS1 lida pela câmera (ou lote/série digitados) → GTIN, lote, validade e série."""
    c = _get(db, cid)
    _open_surgery(c, "O consumo de OPME")
    extra = dict(c.extra_json or {})
    consumo = list(extra.get("opme_consumo") or [])
    today = opme.local_today()
    authorized = extra.get("opme_autorizado", [])
    if body.etiqueta:
        try:
            item, label = opme.scan(
                body.etiqueta, today, quantity=body.quantidade, code=body.codigo, authorized=authorized
            )
        except gs1.Gs1Error as exc:
            raise HTTPException(422, f"Etiqueta ilegível: {exc}") from exc
        warnings = label.avisos
    elif body.codigo and (body.lote or body.serie):
        entry = opme.entries_for(authorized, []).get(body.codigo)
        if entry is None:
            raise HTTPException(422, "Código de OPME desconhecido: use o catálogo ou o código da guia.")
        item = {
            "codigo": entry.code,
            "descricao": entry.description,
            "gtin": None,
            "lote": body.lote,
            "validade": body.validade.isoformat() if body.validade else None,
            "serie": body.serie,
            "quantidade": body.quantidade,
            "registro_anvisa": entry.anvisa or None,
            "origem": "manual",
        }
        warnings = []
    else:
        raise HTTPException(422, "Informe a etiqueta lida ou o código com lote ou série.")
    if item.get("serie") and item["quantidade"] != 1:
        raise HTTPException(422, "Item com número de série é unitário: use quantidade 1.")
    twin = next(
        (
            x
            for x in consumo
            if item.get("serie") and x.get("serie") == item["serie"] and x.get("gtin") == item.get("gtin")
        ),
        None,
    )
    if twin:
        raise HTTPException(409, f"Esta peça (série {item['serie']}) já foi registrada neste procedimento.")
    seq = int(extra.get("opme_consumo_seq", 0)) + 1
    item |= {"id": f"e{seq}", "lido_em": clock.now().isoformat(), "por": user.name}
    extra |= {"opme_consumo": [*consumo, item], "opme_consumo_seq": seq}
    c.extra_json = extra
    result = _refresh(c, fhir)
    vencido = opme.expired(item.get("validade"), today)
    log_action(
        db,
        user,
        "consulta.opme_consumo",
        "consultation",
        c.id,
        {
            "patient_id": c.patient_id,
            "codigo": item["codigo"],
            "lote": item.get("lote"),
            "serie": item.get("serie"),
            "validade": item.get("validade"),
            "vencido": vencido,
            "origem": item["origem"],
        },
    )
    return result | {
        "leitura": item | {"vencido": vencido, "avisos": warnings},
        "consumo": c.extra_json["opme_consumo"],
    }


def remove_opme_consumo(db: Session, fhir: FhirSource, user: User, cid: int, item_id: str) -> dict:
    c = _get(db, cid)
    _open_surgery(c, "O consumo de OPME")
    extra = dict(c.extra_json or {})
    consumo = list(extra.get("opme_consumo") or [])
    kept = [x for x in consumo if x["id"] != item_id]
    if len(kept) == len(consumo):
        raise HTTPException(404, "Leitura de etiqueta não encontrada")
    c.extra_json = extra | {"opme_consumo": kept}
    result = _refresh(c, fhir)
    log_action(db, user, "consulta.opme_consumo_removido", "consultation", c.id, {"leitura": item_id})
    return result | {"consumo": kept}


def set_faturamento(db: Session, fhir: FhirSource, user: User, cid: int, convenio: str | None, numero_guia: str | None):
    """Convênio e número da guia usados nas regras anti-glosa (também com o relatório já finalizado)."""
    c = _get(db, cid)
    if c.kind != "cirurgia":
        raise HTTPException(409, "O faturamento vale só para o modo cirúrgico.")
    if c.status == "enviada":
        raise HTTPException(409, "Relatório já enviado.")
    if convenio is not None and convenio not in antiglosa.convenios():
        raise HTTPException(422, "Convênio desconhecido")
    current = dict((c.extra_json or {}).get("faturamento") or {})
    if convenio is not None:
        current["convenio"] = convenio
    if numero_guia is not None:
        current["numero_guia"] = numero_guia.strip() or None
    c.extra_json = (c.extra_json or {}) | {"faturamento": current}
    if c.status == "em_andamento":
        result = _refresh(c, fhir)
    else:
        result = {"consulta": _rescore_note(c)}
    log_action(db, user, "consulta.faturamento", "consultation", c.id, {"convenio": current.get("convenio")})
    return result | {"faturamento": current}


def _rescore_note(c: Consultation) -> dict:
    """Reavalia a prontidão do relatório já revisado (convênio, guia ou campos mudaram)."""
    note = dict(c.note_json or {})
    report = SurgicalReport.model_validate(note["relatorio_cirurgico"])
    report.prontidao = antiglosa.evaluate(report, segments=_segments(c), **billing_params(c.extra_json or {}))
    c.note_json = note | {"relatorio_cirurgico": report.model_dump(mode="json")}
    return out(c)


def _audit_readiness(
    db: Session, user: User, c: Consultation, moment: str, before: dict | None, after: dict, **extra
) -> None:
    """Métrica de glosas evitadas: pendências antes e depois de cada marco (finish e envio)."""
    log_action(
        db,
        user,
        "cirurgia.prontidao",
        "consultation",
        c.id,
        {
            "patient_id": c.patient_id,
            "momento": moment,
            "convenio": billing_params(c.extra_json or {})["convenio"],
            "antes": before,
            "depois": after,
            "resolvidas": antiglosa.resolved(before, after),
        }
        | extra,
        commit=False,
    )


# ---------- pedidos e prescrições em rascunho ----------
EDITABLE_ORDER_FIELDS = ("dose", "via", "frequencia", "duracao")


def decide_order(db: Session, fhir: FhirSource, user: User, cid: int, draft_id: str, body) -> dict:
    """Assinatura item a item de um rascunho (assinar, recusar ou voltar a pendente), com a posologia corrigida se
    preciso. Item crítico (alergia, dose por TFG, interação grave) só é assinado com justificativa."""
    if body.estado == "assinado" and user.role != "medico":
        raise HTTPException(403, "Só o médico assina receitas e pedidos; a enfermagem pode recusar ou revisar.")
    c = _get(db, cid)
    if c.kind == "cirurgia":
        raise HTTPException(409, "Os pedidos do modo cirúrgico seguem o relatório: retire-os na revisão.")
    if c.status == "enviada":
        raise HTTPException(409, "Consulta já enviada ao prontuário: os pedidos não podem mais ser alterados.")
    record, events, ctx = _chart(fhir, c.patient_id)
    segments = _segments(c)
    draft = next((d for d in _insights(c, record, events, ctx, segments).pedidos if d.id == draft_id), None)
    if draft is None:
        raise HTTPException(404, "Pedido em rascunho não encontrado nesta consulta.")
    extra = dict(c.extra_json or {})
    decisions = dict(extra.get("pedidos_decisoes") or {})
    edits = dict((decisions.get(draft_id) or {}).get("edicao") or {})
    edits |= {f: getattr(body, f).strip() for f in EDITABLE_ORDER_FIELDS if getattr(body, f) is not None}
    text = (body.justificativa or "").strip()
    if body.estado == "assinado":
        critical = [k for k in draft.checagens if k.severidade == "critico"]
        if critical and len(text) < MIN_JUSTIFICATION:
            raise HTTPException(
                409,
                {
                    "codigo": "pedido_critico",
                    "mensagem": f"Este item tem {len(critical)} alerta(s) crítico(s). Assine só com uma justificativa "
                    f"de pelo menos {MIN_JUSTIFICATION} caracteres ou recuse o item.",
                    "checagens": [k.model_dump() for k in critical],
                },
            )
        dose = edits.get("dose", draft.dose)
        if draft.tipo == "medicamento" and draft.acao != "suspender" and not dose:
            raise HTTPException(422, "Informe a dose do medicamento (edite o rascunho) antes de assinar.")
    if body.estado == "pendente":
        decisions[draft_id] = {"estado": "pendente", "edicao": edits} if edits else {}
        decisions = {k: v for k, v in decisions.items() if v}
    else:
        decisions[draft_id] = {
            "estado": body.estado,
            "justificativa": text,
            "por": user.name,
            "em": clock.now().isoformat(),
            "edicao": edits,
        }
    c.extra_json = extra | {"pedidos_decisoes": decisions}
    result = _refresh(c, fhir)
    action = {"assinado": "assinado", "recusado": "recusado", "pendente": "revertido"}[body.estado]
    log_action(
        db,
        user,
        f"consulta.pedido_{action}",
        "consultation",
        c.id,
        {
            "patient_id": c.patient_id,
            "pedido": draft.id,
            "tipo": draft.tipo,
            "item": draft.item,
            "acao": draft.acao,
            "gravidade": draft.gravidade,
            "alertas_criticos": [k.texto for k in draft.checagens if k.severidade == "critico"],
            "justificativa": text,
            "editado": bool(edits),
            "refs": draft.refs,
        },
    )
    return result | {"pedido": next((d for d in _draft_list(c) if d["id"] == draft_id), None)}


def _draft_list(c: Consultation) -> list[dict]:
    return (c.insights_json or {}).get("pedidos") or []


def _signed_orders(c: Consultation) -> list[order_drafts.OrderDraft]:
    """Rascunhos assinados da consulta (aprovados item a item pelo profissional)."""
    return [] if c.kind == "cirurgia" else order_drafts.signed(_draft_list(c))


def _order_resources(c: Consultation, author: str, when: datetime) -> list[dict]:
    return [
        order_drafts.fhir_resource(
            d,
            patient_id=c.patient_id,
            encounter_id=c.encounter_id,
            author=author,
            when=when,
            consultation_id=c.id,
        )
        for d in _signed_orders(c)
    ]


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
        engine = _engine(c, record, events, ctx, segments)
        base = engine.run()
        merged = ai.merge_live(base, result.output, valid, segments, engine.r.sex)
        ai.reconcile_ai_allergies(engine, merged)
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
    c.insights_json = hipoteses.apply(merged, c.extra_json).model_dump(mode="json")
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
            snap = antiglosa.snapshot(merged.relatorio_cirurgico.prontidao)  # type: ignore[arg-type]
            _audit_readiness(db, user, c, "finish", (c.extra_json or {}).get("prontidao_inicial"), snap)
            c.extra_json = (c.extra_json or {}) | {"prontidao_finish": snap}
        else:
            ins = Insights.model_validate(c.insights_json) if c.insights_json else engine.run()
            if ins.falas_analisadas != len(segments):
                ins = _insights(c, record, events, ctx, segments)
            note_model, result, dropped = ai.run_note(engine, ins, pseudo, valid, c.patient_language)
            note = hipoteses.apply_to_note(note_model.model_dump(mode="json"), hipoteses.apply(ins, c.extra_json))
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
    changes: list[dict] = []
    try:
        if c.kind == "cirurgia":
            edited = SurgicalReport.model_validate(note.get("relatorio_cirurgico"))
            report = _kept_orders(c, edited.model_dump(mode="json"))
            report, changes = _review_opme(c, report)
            note = note | {"relatorio_cirurgico": report}
        else:
            ConsultNote.model_validate(note)
            # as propostas de Condition vêm das decisões do profissional na consulta, não da edição do texto
            note = note | {"condicoes_propostas": (c.note_json or {}).get("condicoes_propostas", [])}
    except ValidationError as exc:
        raise HTTPException(422, f"Nota em formato inválido: {exc.error_count()} erro(s)") from exc
    c.note_json = note
    if c.kind == "cirurgia":
        _rescore_note(c)
    c.note_meta_json = (c.note_meta_json or {}) | {"revisada_por": user.name, "revisada_em": clock.now().isoformat()}
    payload = {"patient_id": c.patient_id} | ({"opme": changes} if changes else {})
    log_action(db, user, "consulta.nota_revisada", "consultation", c.id, payload)
    return out(c)


def _review_opme(c: Consultation, report: dict) -> tuple[dict, list[dict]]:
    """OPME editável na revisão: só a quantidade citada e a justificativa mudam; guia e etiquetas vêm do servidor."""
    previous = SurgicalReport.model_validate((c.note_json or {}).get("relatorio_cirurgico")).opme
    lines, changes = opme.review_lines(previous, report.get("opme", []))
    convenio = billing_params(c.extra_json or {})["convenio"]
    for change in changes:  # a justificativa esconde a pendência e a trava de envio: o audit guarda texto e regra
        if "tipos_suprimidos" in change:
            change["regras"] = antiglosa.rules_for_kinds(convenio, change.pop("tipos_suprimidos"))
    kept = [p for p in report.get("pendencias", []) if p.get("categoria") != "opme"]
    kept += [h.model_dump(mode="json") for h in opme.review_highlights(lines)]
    order = {"critico": 0, "atencao": 1, "info": 2}
    kept.sort(key=lambda h: order[h["severidade"]])
    return report | {"opme": [line.model_dump(mode="json") for line in lines], "pendencias": kept}, changes


def _order_id(order: dict) -> str | None:
    ids = (order.get("recurso") or {}).get("identifier") or [{}]
    return ids[0].get("value")


def _kept_orders(c: Consultation, report: dict) -> dict:
    """Na revisão, o cirurgião só retira pedidos propostos pelo HousIA: não cria nem altera o que vai ao FHIR."""
    proposed = {_order_id(p): p for p in ((c.note_json or {}).get("relatorio_cirurgico") or {}).get("pedidos", [])}
    kept = [proposed[_order_id(p)] for p in report.get("pedidos", []) if _order_id(p) in proposed]
    return report | {"pedidos": list({_order_id(p): p for p in kept}.values())}


def _document(db: Session, fhir: FhirSource, user: User, c: Consultation) -> tuple[str, dict]:
    if c.note_json is None:
        raise HTTPException(409, "Finalize e revise a nota antes de exportar ou enviar.")
    record = load_record(fhir, c.patient_id)
    now = clock.now()
    role = {"medico": "Médico(a)", "enfermagem": "Enfermagem"}.get(user.role, user.role)
    author = f"{user.name} ({role})"
    note = reidentify(c.note_json, record)  # o prontuário recebe os nomes, não os tokens enviados à IA
    text = note_as_text(c.kind, record.name, author, _local(now), note, c.patient_language, _signed_orders(c))
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


def _orders(c: Consultation, author: str = "", when: datetime | None = None) -> list[dict]:
    """Pedidos a enviar. Cirurgia: os propostos no relatório revisado (o cirurgião pode retirar). Consulta e visita: os
    rascunhos de receita e de exame que o profissional assinou item a item (FHIR draft/proposal)."""
    if c.kind != "cirurgia":
        return _order_resources(c, author, when or clock.now())
    if not c.note_json:
        return []
    return [p["recurso"] for p in (c.note_json.get("relatorio_cirurgico") or {}).get("pedidos", [])]


def _report(c: Consultation) -> dict | None:
    return (c.note_json or {}).get("relatorio_cirurgico") if c.kind == "cirurgia" else None


def _bundle(c: Consultation, record: PatientRecord, doc: dict, orders: list[dict]) -> dict | None:
    """Bundle de transação (relatório, pedidos propostos, implantes, Procedure e Claim em rascunho) para exportar."""
    report = _report(c)
    if report is None:
        if not orders:
            return None
        entries = [(export_urn(c.id, "doc"), doc)]
        entries += [(export_urn(c.id, "pedido", i), o) for i, o in enumerate(orders)]
        return transaction_bundle(entries)
    now = clock.now()
    entries = [(export_urn(c.id, "doc"), doc)]
    entries += [(export_urn(c.id, "pedido", i), o) for i, o in enumerate(orders)]
    devices = device_resources(report, c.patient_id)
    refs = {leitura: export_urn(c.id, "device", i) for i, (leitura, _) in enumerate(devices)}
    entries += [(refs[leitura], r) for leitura, r in devices]
    if devices:
        entries.append(
            (
                export_urn(c.id, "procedure"),
                procedure_resource(
                    report=report,
                    patient_id=c.patient_id,
                    encounter_id=c.encounter_id,
                    when=now,
                    consultation_id=c.id,
                    device_refs=list(refs.values()),
                    document_ref=export_urn(c.id, "doc"),
                ),
            )
        )
    entries.append((export_urn(c.id, "claim"), _claim(c, record, report, refs, now)))
    return transaction_bundle(entries)


def _claim(c: Consultation, record: PatientRecord, report: dict, refs: dict[str, str], when) -> dict:
    return claim_resource(
        report=report,
        patient_id=c.patient_id,
        patient_name=record.name,
        when=when,
        consultation_id=c.id,
        numero_guia=billing_params(c.extra_json or {})["numero_guia"],
        device_refs=refs,
    )


def export(db: Session, fhir: FhirSource, user: User, cid: int) -> dict:
    """Texto para colar no prontuário + recurso FHIR (e os pedidos propostos), sem enviar nada."""
    c = _get(db, cid)
    text, doc = _document(db, fhir, user, c)
    log_action(db, user, "consulta.exportada", "consultation", c.id, {"patient_id": c.patient_id})
    orders = _orders(c, _author(doc), clock.now())
    result = {"texto": text, "recurso": doc, "pedidos": orders}
    if c.kind == "cirurgia" or orders:
        result["bundle"] = _bundle(c, load_record(fhir, c.patient_id), doc, orders)
    return result


def _author(doc: dict) -> str:
    return (doc.get("author") or [{}])[0].get("display") or ""


def _gate_billing(db: Session, user: User, c: Consultation, justification: str | None) -> dict | None:
    """Trava suave do envio da cirurgia: pendência crítica de faturamento exige justificativa (auditada)."""
    if c.kind != "cirurgia":
        return None
    if c.note_json is None:
        raise HTTPException(409, "Finalize e revise a nota antes de exportar ou enviar.")
    note = dict(c.note_json)
    report = SurgicalReport.model_validate(note["relatorio_cirurgico"])
    ready = antiglosa.evaluate(report, segments=_segments(c), **billing_params(c.extra_json or {}))
    critical = [p for p in ready.pendencias if p.severidade == "critico"]
    text = (justification or "").strip()
    if critical and len(text) < MIN_JUSTIFICATION:
        raise HTTPException(
            409,
            {
                "codigo": "prontidao_critica",
                "mensagem": f"Há {len(critical)} pendência(s) crítica(s) de faturamento. Resolva-as ou envie com "
                f"uma justificativa de pelo menos {MIN_JUSTIFICATION} caracteres.",
                "pendencias": [p.model_dump() for p in critical],
            },
        )
    report.prontidao = ready
    c.note_json = note | {"relatorio_cirurgico": report.model_dump(mode="json")}
    snap = antiglosa.snapshot(ready)
    extra = {"justificativa": text, "regras_justificadas": [p.regra for p in critical]} if critical else {}
    if justified := [{"codigo": o.codigo, "justificativa": o.justificativa} for o in report.opme if o.justificativa]:
        extra["opme_justificadas"] = justified  # contornos da revisão que escondem pendências: ficam no audit do envio
    _audit_readiness(db, user, c, "envio", (c.extra_json or {}).get("prontidao_finish"), snap, **extra)
    if critical:
        c.extra_json = (c.extra_json or {}) | {
            "justificativa_envio": {"texto": text, "por": user.name, "em": clock.now().isoformat(), **extra}
        }
    return snap


def send_to_ehr(db: Session, fhir: FhirSource, user: User, cid: int, justification: str | None = None) -> dict:
    c = _get(db, cid)
    if c.status == "enviada" and c.fhir_ref and get_settings().ehr_writeback:
        raise HTTPException(409, f"Nota já enviada ({c.fhir_ref}).")
    _gate_billing(db, user, c, justification)
    record = load_record(fhir, c.patient_id)
    text, doc = _document(db, fhir, user, c)
    if not get_settings().ehr_writeback:
        log_action(db, user, "consulta.exportada", "consultation", c.id, {"patient_id": c.patient_id})
        orders = _orders(c, _author(doc), clock.now())
        return {
            "enviado": False,
            "motivo": "Envio automático desligado (EHR_WRITEBACK=false): copie o texto ou baixe o FHIR.",
            "texto": text,
            "recurso": doc,
            "pedidos": orders,
            "bundle": _bundle(c, record, doc, orders),
            "consulta": out(c),
        }
    created = fhir.create(doc)
    c.fhir_ref = f"DocumentReference/{created['id']}"
    # pedidos propostos aprovados com o relatório: vão como proposta (intent=proposal), ligados ao documento
    author = _author(doc)
    orders = []
    for i, order in enumerate(_orders(c, author, clock.now())):
        if c.kind == "cirurgia":
            resource = order | {
                "status": "active",
                "authoredOn": clock.now().isoformat(),
                "requester": {"display": author},
                "supportingInfo": [{"reference": c.fhir_ref}],
                "identifier": [{"system": ORDER_SYSTEM, "value": f"{c.id}-{i + 1}"}],
            }
        else:  # receitas e pedidos de exame assinados: seguem como rascunho (draft/proposal), ligados à nota
            link = "supportingInformation" if order["resourceType"] == "MedicationRequest" else "supportingInfo"
            resource = order | {link: [{"reference": c.fhir_ref}]}
        orders.append(f"{order['resourceType']}/{fhir.create(resource)['id']}")
    traceability = _send_surgery_records(c, record, fhir)
    c.extra_json = (c.extra_json or {}) | ({"pedidos_fhir": orders} if orders else {}) | traceability
    c.status = "enviada"
    c.sent_at = clock.now()
    clear_chart_cache()
    log_action(
        db,
        user,
        "consulta.enviada",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "fhir_ref": c.fhir_ref, "pedidos": orders} | traceability,
    )
    return {
        "enviado": True,
        "fhir_ref": c.fhir_ref,
        "pedidos": orders,
        "texto": text,
        "recurso": created,
        "consulta": out(c),
    } | traceability


def _send_surgery_records(c: Consultation, record: PatientRecord, fhir: FhirSource) -> dict:
    """Implantes (Device), Procedure com usedReference e Claim em rascunho, criados depois do documento aprovado."""
    report = _report(c)
    if report is None:
        return {}
    now = clock.now()
    refs: dict[str, str] = {}
    for leitura, resource in device_resources(report, c.patient_id):
        refs[leitura] = f"Device/{fhir.create(resource)['id']}"
    out_: dict = {}
    if refs:
        procedure = procedure_resource(
            report=report,
            patient_id=c.patient_id,
            encounter_id=c.encounter_id,
            when=now,
            consultation_id=c.id,
            device_refs=list(refs.values()),
            document_ref=c.fhir_ref or "",
        )
        out_["dispositivos"] = list(refs.values())
        out_["procedimento"] = f"Procedure/{fhir.create(procedure)['id']}"
    out_["claim"] = f"Claim/{fhir.create(_claim(c, record, report, refs, now))['id']}"
    return out_


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

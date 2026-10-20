"""Orquestração da consulta com IA: falas → Smart Apps (regras na hora + IA em ciclos) → nota → prontuário."""

from __future__ import annotations

import copy
import re
import time
from datetime import datetime

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app import clock
from app.alerts.service import upsert_alert
from app.audit.service import log_action
from app.config import get_settings
from app.consult import (
    acoes,
    ai,
    antiglosa,
    apps,
    consentimento,
    consultorio,
    fidelidade,
    gs1,
    hipoteses,
    idiomas,
    knowledge,
    notas,
    note_templates,
    opme,
    pdf,
    privacidade,
    relevancia,
    stt,
    summary,
)
from app.consult import ask as copilot
from app.consult import orders as order_drafts
from app.consult.edicao import edit_percent
from app.consult.edicao import section_diff as edit_sections
from app.consult.engine import PATIENT_SIDE, Engine, billing_params, infer_speaker
from app.consult.export import (
    KIND_TITLE,
    claim_resource,
    device_resources,
    document_reference,
    note_as_text,
    procedure_resource,
    selo_profissional,
    transaction_bundle,
)
from app.consult.export import urn as export_urn
from app.consult.schemas import (
    ConsultCreate,
    ConsultNote,
    FinishIn,
    Insights,
    Segment,
    SegmentEdit,
    SegmentIn,
    SurgicalReport,
    fala_ref,
)
from app.consult.surgical import ORDER_SYSTEM
from app.consult.text import NEG_WORDS, norm
from app.consult.text import clauses as text_clauses
from app.db.models import Alert, Consultation, MedrecRun, User
from app.fhir_client.client import FhirSource
from app.llm.client import LLMError
from app.llm.pseudonymize import Pseudonymizer
from app.patients.service import load_record
from app.reference import icd_entry, icd_problem, norm_icd
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
    return relevancia.ensure([Segment.model_validate(s) for s in c.segments_json or []])


def _medrec_runs(c: Consultation) -> list[str]:
    """Datas das reconciliações medicamentosas do paciente (o lembrete pós-alta some se houve uma na internação)."""
    db = object_session(c)
    if db is None:
        return []
    return [r.isoformat() for r in db.scalars(select(MedrecRun.created_at).where(MedrecRun.patient_id == c.patient_id))]


def _engine(c: Consultation, record, events, ctx, segments: list[Segment]) -> Engine:
    extra = dict(c.extra_json or {})
    if c.kind != "cirurgia":
        extra["medrec_runs"] = _medrec_runs(c)  # só para ler: não é gravado em extra_json
    if not consentimento.flags(c.extra_json).emocoes:
        extra["sem_emocoes"] = True  # emocoes=false no consentimento: o engine não calcula emoções
    return Engine(record, events, ctx, segments, c.specialty, c.kind, clock.now(), extra)


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
        base = merged
    return _decided(c, base, engine)


def _smart_apps(c: Consultation, ins: Insights, engine: Engine) -> list:
    """Smart Apps do registro (app/consult/apps) sobre o que o engine e a IA já produziram; fontes conferidas aqui."""
    chart = engine.chart
    valid = _valid_refs(chart.r, chart.events, chart.ctx, engine.all_segments) | {f"Patient/{chart.r.id}"}
    ctx = apps.AppContext(
        ins=ins,
        chart=chart,
        kind=c.kind,
        specialty=c.specialty,
        segments=engine.segments,
        extra=engine.extra,
    )
    return apps.run_all(ctx, valid)


def _decided(c: Consultation, ins: Insights, engine: Engine) -> Insights:
    """Decisão do profissional sobre cada insight reaplicada a cada análise; críticos vão ao módulo de alertas."""
    result = _consent_filter(c, hipoteses.apply(ins, c.extra_json))
    summary.mark_new(result, c.insights_json)
    result.regras_ms = (c.insights_json or {}).get("regras_ms")
    if c.kind == "cirurgia":
        return result
    acoes.apply(result, c.insight_actions_json, c.insights_json)
    _sync_alerts(c, result)
    result.apps = _smart_apps(c, result, engine)
    return result


def _consent_filter(c: Consultation, ins: Insights) -> Insights:
    """Sem consentimento para emoções, nada delas sobra: nem o que a IA acrescentou na mescla."""
    if not consentimento.flags(c.extra_json).emocoes:
        ins.emocoes, ins.linha_emocional = [], []
    return ins


def _fhir_refs(refs: list[str]) -> list[str]:
    return [r for r in refs if not r.startswith("Fala/")]


def _sync_alerts(c: Consultation, ins: Insights) -> None:
    """Cada alerta crítico da consulta vira Alert(source='consulta'), sem duplicar a cada fala (#3)."""
    db = object_session(c)
    if db is None or c.id is None or c.status != "em_andamento":
        return
    data = c.insight_actions_json or acoes.empty()
    mapping = dict(data.get("alertas") or {})
    seen: set[str] = set()
    changed = False
    for h in ins.alertas:
        if h.severidade != "critico" or h.item_key in seen:
            continue
        seen.add(h.item_key)
        refs = _fhir_refs(h.refs)
        falas = sorted({int(r.split("/")[1]) for r in h.refs if r.startswith("Fala/")})
        detail = f"Consulta #{c.id}" + (f", fala {falas[0]}" if falas else "")
        alert = db.get(Alert, mapping.get(h.item_key)) if h.item_key in mapping else None
        if alert is not None and alert.status in ("aberto", "reconhecido"):
            alert.title, alert.detail, alert.source_refs, alert.severity = h.texto[:300], detail, refs, h.severidade
        else:
            alert = upsert_alert(
                db,
                patient_id=c.patient_id,
                source="consulta",
                severity=h.severidade,
                title=h.texto,
                detail=detail,
                refs=[],
                claimed=set(mapping.values()),
            )
            alert.source_refs = refs  # as fontes não entram no casamento: o título identifica o alerta da consulta
            mapping[h.item_key] = alert.id
            changed = True
    if changed:
        c.insight_actions_json = {**data, "alertas": mapping}


def _nota(c: Consultation) -> dict | None:
    """A nota como a tela a lê: nota antiga (sem modelo) é convertida na leitura, sem mexer no que está gravado."""
    return note_templates.upgrade(c.note_json, c.kind, c.specialty) if c.kind != "cirurgia" else c.note_json


def _public_meta(c: Consultation) -> dict | None:
    """Metadados da nota sem o rascunho da IA (cópia da nota inteira): ele sai pelo GET /note/diff, por seção."""
    if c.note_meta_json is None:
        return None
    return {k: v for k, v in c.note_meta_json.items() if k != "rascunho_ia"}


def out(c: Consultation, user_names: dict[int, str] | None = None) -> dict:
    meta = dict(c.ai_meta_json or {})
    meta.pop("saida", None)
    segments = _segments(c)
    return {
        "id": c.id,
        "patient_id": c.patient_id,
        "encounter_id": c.encounter_id,
        "tipo": c.kind,
        "especialidade": c.specialty,
        "especialidade_rotulo": knowledge.SPECIALTIES.get(c.specialty, c.specialty),
        "idioma_paciente": c.patient_language,
        "consentimento": consentimento.public((c.extra_json or {}).get("consentimento")),
        "status": c.status,
        "sandbox": bool(c.sandbox),
        "segmentos": [s.model_dump() for s in segments],
        "cobertura": relevancia.coverage(segments, c.note_json) if c.kind != "cirurgia" else None,
        "insights": c.insights_json,
        "ia": meta or None,
        "privacidade": privacidade.panel(c.kind) | {"expurgada_em": (c.extra_json or {}).get("expurgada_em")},
        "nota": _nota(c),
        "nota_meta": _public_meta(c),
        "fhir_ref": c.fhir_ref,
        "roteiro_demo": (c.extra_json or {}).get("roteiro"),
        "opme_guia": (c.extra_json or {}).get("opme_guia"),
        "faturamento": (c.extra_json or {}).get("faturamento"),
        "justificativa_envio": (c.extra_json or {}).get("justificativa_envio"),
        "justificativa_pendencias": (c.extra_json or {}).get("justificativa_pendencias"),
        "pedidos_fhir": (c.extra_json or {}).get("pedidos_fhir"),
        "criado_por": (user_names or {}).get(c.created_by or 0),
        "created_at": c.created_at.isoformat(),
        "finished_at": c.finished_at.isoformat() if c.finished_at else None,
        "sent_at": c.sent_at.isoformat() if c.sent_at else None,
    }


# ---------- criar / listar ----------
def idioma_paciente(fhir: FhirSource, patient_id: str) -> dict:
    """Idioma preferido do paciente (Patient.communication) para pré-selecionar na abertura da consulta."""
    record, _, _ = _chart(fhir, patient_id)
    code, fonte = idiomas.do_paciente(record.patient)
    return {"idioma": code or idiomas.PADRAO, "rotulo": idiomas.rotulo(code or idiomas.PADRAO), "fonte": fonte}


def companions(fhir: FhirSource, patient_id: str) -> list[dict]:
    record, _, _ = _chart(fhir, patient_id)
    out_: list[dict] = []
    for rp in record.related_persons:
        rel = (rp.get("relationship") or [{}])[0]
        out_.append(
            {
                "id": rp["id"],
                "nome": ((rp.get("name") or [{}])[0]).get("text", "Acompanhante"),
                "parentesco": rel.get("text") or ((rel.get("coding") or [{}])[0]).get("display", ""),
            }
        )
    return out_


def _consent_record(fhir: FhirSource, user: User, patient_id: str, record: PatientRecord, body: ConsultCreate) -> dict:
    """Consentimento por finalidade: valida quem consentiu, monta o Consent R4 e o cria no FHIR com EHR_WRITEBACK."""
    cons = consentimento.normalize(body.consentimento)
    who_name = None
    if cons.quem != "paciente" and cons.related_person_id:
        related = next((rp for rp in record.related_persons if rp.get("id") == cons.related_person_id), None)
        if related is None:
            raise HTTPException(422, "Acompanhante ou responsável não encontrado no prontuário deste paciente.")
        who_name = ((related.get("name") or [{}])[0]).get("text")
    when = clock.now().isoformat()
    resource = consentimento.build_consent(patient_id, cons, who_name, when)
    ref = None
    if get_settings().ehr_writeback:
        resource = fhir.create(resource)
        ref = f"Consent/{resource['id']}"
        clear_chart_cache()  # o Consent entra na linha do tempo do paciente
    return {
        "quem": cons.quem,
        "quem_nome": who_name,
        "related_person_id": cons.related_person_id if cons.quem != "paciente" else None,
        "finalidades": cons.finalidades.model_dump(),
        "recusou": cons.recusou,
        "estado": "ativo",
        "em": when,
        "por": user.name,
        "fhir_ref": ref,
        "consent": resource,
    }


def create(db: Session, fhir: FhirSource, user: User, patient_id: str, body: ConsultCreate) -> dict:
    if body.especialidade not in knowledge.SPECIALTIES:
        raise HTTPException(422, "Especialidade desconhecida")
    record, events, ctx = _chart(fhir, patient_id)
    consultorio.check_start(db, record, body.sandbox)  # sandbox só em cenário sintético; plano de teste grátis
    consent = _consent_record(fhir, user, patient_id, record, body)
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
    extra["consentimento"] = consent
    c = Consultation(
        patient_id=patient_id,
        encounter_id=encounter_id,
        kind=body.tipo,
        specialty=body.especialidade,
        patient_language=body.idioma_paciente or idiomas.do_paciente(record.patient)[0] or idiomas.PADRAO,
        status="em_andamento",
        sandbox=body.sandbox,
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
        {
            "patient_id": patient_id,
            "tipo": c.kind,
            "especialidade": c.specialty,
            "idioma": c.patient_language,
            "sandbox": c.sandbox,
        },
        commit=False,
    )
    log_action(
        db,
        user,
        "consulta.consentimento",
        "consultation",
        c.id,
        {
            "patient_id": patient_id,
            "quem": consent["quem"],
            "finalidades": consent["finalidades"],
            "recusou": consent["recusou"],
            "fhir_ref": consent["fhir_ref"],
        },
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
def _require_capture(c: Consultation) -> consentimento.Flags:
    """A captura só segue com o consentimento em vigor e fora da pausa de privacidade."""
    f = consentimento.flags(c.extra_json)
    if not f.captura:
        raise HTTPException(403, "Consentimento revogado: a captura foi encerrada.")
    if f.pausado:
        raise HTTPException(409, "Pausa de privacidade ativa: retome a transcrição para continuar.")
    return f


def _require_ai(c: Consultation, what: str, purpose: str = "ia") -> None:
    if not getattr(consentimento.flags(c.extra_json), purpose):
        raise HTTPException(403, f"O paciente não autorizou {what}: só as regras do HousIA analisam esta consulta.")


def _add_segments(c: Consultation, segments: list[Segment], items: list[SegmentIn]) -> list[Segment]:
    """Acrescenta as falas (numeração, falante automático, relevância) sem confirmar a transação."""
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
        previous = next((s for s in reversed(segments) if s.origem != "marco"), None)
        seg = Segment(
            n=len(segments) + 1,
            t=item.t if item.t is not None else elapsed,
            falante=speaker,
            falante_auto=auto,
            texto=text,
            idioma=language,
            origem="digitado" if item.origem == "marco" else item.origem,  # marco só nasce em /marker
            relevancia=relevancia.classify(text, previous, speaker),
        )
        segments.append(seg)
        new.append(seg)
    c.segments_json = [s.model_dump() for s in segments]
    return new


def _refresh_insights(c: Consultation, record, events, ctx, segments: list[Segment]) -> Insights:
    started = time.perf_counter()
    ins = _insights(c, record, events, ctx, segments)
    ins.regras_ms = round((time.perf_counter() - started) * 1000)
    c.insights_json = ins.model_dump(mode="json")
    if c.kind == "cirurgia" and ins.relatorio_cirurgico and "prontidao_inicial" not in (c.extra_json or {}):
        # primeira leitura com ditado: base para medir quantas pendências foram resolvidas até o envio
        c.extra_json = (c.extra_json or {}) | {
            "prontidao_inicial": antiglosa.snapshot(ins.relatorio_cirurgico.prontidao)  # type: ignore[arg-type]
        }
    return ins


def append_segments(db: Session, fhir: FhirSource, user: User, cid: int, items: list[SegmentIn]) -> dict:
    c = _get(db, cid)
    if c.status != "em_andamento":
        raise HTTPException(409, "Consulta finalizada: não aceita novas falas.")
    _require_capture(c)
    record, events, ctx = _chart(fhir, c.patient_id)
    segments = _segments(c)
    new = _add_segments(c, segments, items)
    _refresh_insights(c, record, events, ctx, segments)
    db.commit()
    return {"novos": [s.model_dump() for s in new], "insights": c.insights_json, "total": len(segments)}


# ---------- transcrição editável (#2) ----------
NOTE_LABELS = {
    "queixa_principal": "Queixa principal",
    "hda": "HDA",
    "antecedentes": "Antecedentes",
    "medicacoes": "Medicações",
    "alergias": "Alergias",
    "exame_fisico": "Exame físico",
    "exames_complementares": "Exames complementares",
    "avaliacao": "Avaliação",
    "plano": "Plano",
    "cids": "CIDs",
    "cronologia": "Cronologia",
    "pendencias": "Pendências",
    "orientacoes_paciente": "Orientações ao paciente",
}
NOTE_PROVENANCE = {"copiloto", "incluidas", "condicoes_propostas"}  # proveniência, não texto da nota


def _cites(node, ref: str) -> bool:
    if isinstance(node, dict):
        return any((k == "refs" and ref in v) or _cites(v, ref) for k, v in node.items())
    if isinstance(node, list):
        return any(_cites(v, ref) for v in node)
    return False


def sections_citing(note: dict | None, ref: str) -> list[str]:
    """Seções da nota (ou campos do relatório cirúrgico) que citam a fala."""
    found: list[str] = []
    for key, value in (note or {}).items():
        if key in NOTE_PROVENANCE:
            continue
        if key == "relatorio_cirurgico" and isinstance(value, dict):
            for k2, v2 in value.items():
                if k2 == "campos" and isinstance(v2, list):
                    found += [f.get("rotulo") or f.get("campo", "") for f in v2 if _cites(f, ref)]
                elif _cites(v2, ref):
                    found.append(k2.replace("_", " ").capitalize())
        elif key == "secoes" and isinstance(value, list):
            labels = {s["key"]: s["rotulo"] for s in ((note or {}).get("modelo") or {}).get("secoes", [])}
            found += [labels.get(s.get("key"), str(s.get("key"))) for s in value if _cites(s, ref)]
        elif key == "modelo":
            continue
        elif _cites(value, ref):
            found.append(NOTE_LABELS.get(key, key.replace("_", " ").capitalize()))
    return list(dict.fromkeys(f for f in found if f))


def edit_segment(db: Session, fhir: FhirSource, user: User, cid: int, n: int, body: SegmentEdit) -> dict:
    """O profissional corrige o texto e/ou o falante de uma fala. As regras rodam de novo sobre a conversa corrigida;
    se a nota já existe, ela fica marcada como desatualizada com as seções que citam a fala."""
    c = _get(db, cid)
    if c.status == "enviada":
        raise HTTPException(409, "Nota já enviada ao prontuário: a transcrição não pode mais ser editada.")
    if body.texto is None and body.falante is None and body.idioma is None:
        raise HTTPException(422, "Informe o texto, o falante e/ou o idioma da fala.")
    segments = _segments(c)
    idx = next((i for i, s in enumerate(segments) if s.n == n), None)
    if idx is None:
        raise HTTPException(404, "Fala não encontrada")
    seg = segments[idx]
    if seg.origem == "marco":
        raise HTTPException(409, "Marcos de privacidade não são falas: não podem ser editados.")
    changed: list[str] = []
    if body.texto is not None:
        text = body.texto.strip()
        if not text:
            raise HTTPException(422, "O texto da fala não pode ficar vazio.")
        if text != seg.texto:
            seg.texto_original = seg.texto_original if seg.texto_original is not None else seg.texto
            seg.texto, seg.traducao = text, None  # a tradução antiga não vale para o texto novo
            seg.traducao_divergente = []
            changed.append("texto")
    if body.falante is not None and body.falante != seg.falante:
        seg.falante_original = seg.falante_original or seg.falante
        seg.falante, seg.falante_auto = body.falante, False
        if body.idioma is None:
            seg.idioma = c.patient_language if body.falante in PATIENT_SIDE else "pt-BR"
            seg.traducao, seg.traducao_divergente = None, []
        changed.append("falante")
    if body.idioma is not None and body.idioma != seg.idioma:
        seg.idioma, seg.traducao, seg.traducao_divergente = body.idioma, None, []  # a tradução é de outro idioma
        changed.append("idioma")
    if not changed:
        return out(c)
    previous = next((s for s in reversed(segments[:idx]) if s.origem != "marco"), None)
    seg.relevancia, seg.relevancia_origem = relevancia.classify(seg.texto, previous, seg.falante), "regra"
    now = clock.now().isoformat()
    seg.editado_por, seg.editado_em = user.name, now
    c.segments_json = [s.model_dump() for s in segments]
    record, events, ctx = _chart(fhir, c.patient_id)
    _refresh_insights(c, record, events, ctx, segments)
    if c.status == "em_andamento" and c.ai_meta_json:
        # a análise da IA vale só até a fala anterior à edição: o próximo ciclo reanalisa a conversa corrigida
        c.ai_meta_json = c.ai_meta_json | {"falas": min(c.ai_meta_json.get("falas", 0), n - 1)}
    stale = c.note_json is not None
    if stale:
        meta = dict(c.note_meta_json or {})
        before = meta.get("desatualizada") or {}
        sections = list(dict.fromkeys([*before.get("secoes", []), *sections_citing(c.note_json, fala_ref(n))]))
        meta["desatualizada"] = {
            "falas": sorted({*before.get("falas", []), n}),
            "secoes": sections,
            "em": now,
            "por": user.name,
        }
        c.note_meta_json = meta
    # o audit guarda qual fala e quais campos mudaram, nunca o texto (nem o antigo nem o novo)
    log_action(
        db,
        user,
        "consulta.fala_editada",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "n": n, "campos": changed, "nota_desatualizada": stale},
    )
    return out(c)


# ---------- marcos de privacidade (#13) ----------
def add_marker(db: Session, fhir: FhirSource, user: User, cid: int, kind: str) -> dict:
    """Pausa, retomada ou revogação: entra na transcrição como marco (não é fala) e no audit_log."""
    c = _get(db, cid)
    if c.status != "em_andamento":
        raise HTTPException(409, "Consulta finalizada: não aceita marcos de privacidade.")
    cons = dict((c.extra_json or {}).get("consentimento") or {})
    if not cons:
        raise HTTPException(409, "Esta consulta não tem consentimento registrado.")
    estado = cons.get("estado", "ativo")
    if estado == "revogado":
        raise HTTPException(409, "Consentimento já revogado: a captura foi encerrada.")
    if kind == "pausa" and estado == "pausado":
        raise HTTPException(409, "A transcrição já está em pausa.")
    if kind == "retomada" and estado != "pausado":
        raise HTTPException(409, "A transcrição não está em pausa.")
    segments = _segments(c)
    now = clock.now()
    seg = Segment(
        n=len(segments) + 1,
        t=max(0.0, (now - c.created_at).total_seconds()),
        falante="desconhecido",
        texto=consentimento.MARKER_TEXT[kind],
        idioma="pt-BR",
        origem="marco",
        marco=kind,  # type: ignore[arg-type]
        relevancia="administrativa",
    )
    segments.append(seg)
    c.segments_json = [s.model_dump() for s in segments]
    cons["estado"] = consentimento.MARKER_STATE[kind]
    cons.setdefault("marcos", [])
    cons["marcos"] = [*cons["marcos"], {"tipo": kind, "n": seg.n, "em": now.isoformat(), "por": user.name}]
    if kind == "revogado":
        resource = dict(cons.get("consent") or {}) | {"status": "inactive"}
        cons["consent"] = resource
        if cons.get("fhir_ref") and resource.get("id"):
            fhir.update(resource)
            clear_chart_cache()
    c.extra_json = (c.extra_json or {}) | {"consentimento": cons}
    record, events, ctx = _chart(fhir, c.patient_id)
    _refresh_insights(c, record, events, ctx, segments)
    log_action(
        db,
        user,
        "consulta.marco_privacidade",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "marco": kind, "n": seg.n},
    )
    return out(c)


def transcribe_audio(
    db: Session, fhir: FhirSource, user: User, cid: int, audio: bytes, content_type: str, speaker: str
) -> dict:
    c = _get(db, cid)
    _require_capture(c)
    _require_ai(c, "enviar o áudio para transcrição no servidor", "stt")
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


def apply_insight_action(
    db: Session, fhir: FhirSource, user: User, cid: int, item_key: str, action: str, reason: str
) -> dict:
    """Aceitar, descartar (com motivo), marcar como feita / não se aplica ou fixar um insight; vale para o resto da
    consulta. A ação sobre um alerta crítico atualiza o Alert correspondente (reconhecido ou descartado)."""
    c = _get(db, cid)
    if c.kind == "cirurgia":
        raise HTTPException(409, "O modo cirúrgico não tem insights para decidir item a item.")
    if c.status != "em_andamento":
        raise HTTPException(409, "Consulta finalizada: os insights não podem mais ser alterados.")
    if action not in acoes.ACTIONS:
        raise HTTPException(422, "Ação inválida.")
    if action == "descartar" and len(acoes.efetivo(reason)) < acoes.MIN_MOTIVO:
        raise HTTPException(422, "Para descartar um insight, registre um motivo (mín. 10 caracteres).")
    record, events, ctx = _chart(fhir, c.patient_id)
    segments = _segments(c)
    found = acoes.find(_insights(c, record, events, ctx, segments), item_key)
    if found is None:
        raise HTTPException(422, "Esse item não aparece mais na análise.")
    kind, item = found
    if action == "feita" and kind != "pergunta":
        raise HTTPException(422, "Só uma pergunta pode ser marcada como feita.")
    if (
        action in acoes.HIDING
        and getattr(item, "severidade", None) == "critico"
        and len(acoes.efetivo(reason)) < acoes.MIN_MOTIVO
    ):
        # alerta crítico some da lista (e da trava de envio) só com motivo: "não se aplica" sem motivo não escapa dela
        raise HTTPException(422, "Para tirar um alerta crítico da consulta, registre o motivo (mín. 10 caracteres).")
    label = acoes.label_of(kind, item)
    c.insight_actions_json = acoes.record(
        c.insight_actions_json, kind, item, action, reason, user.name, clock.now().isoformat(), label
    )
    c.insights_json = _insights(c, record, events, ctx, segments).model_dump(mode="json")
    _alert_follows_action(db, user, c, item_key, action, reason)
    log_action(
        db,
        user,
        "consulta.insight_acao",
        "consultation",
        c.id,
        {
            "patient_id": c.patient_id,
            "tipo": kind,
            "acao": action,
            "origem": getattr(item, "origem", "regra"),
            "item": item_key,
            "motivo": reason.strip() or None,
        },
    )
    return {"insights": c.insights_json, "total": len(segments)}


ALERT_STATUS = {
    "aceitar": "reconhecido",
    "feita": "reconhecido",
    "descartar": "descartado",
    "nao_se_aplica": "descartado",
}


def _alert_follows_action(db: Session, user: User, c: Consultation, item_key: str, action: str, reason: str) -> None:
    status = ALERT_STATUS.get(action)
    alert_id = ((c.insight_actions_json or {}).get("alertas") or {}).get(item_key)
    alert = db.get(Alert, alert_id) if alert_id and status else None
    if alert is None or alert.status == status:
        return
    previous = alert.status
    alert.status, alert.acted_by, alert.acted_at = status, user.id, clock.now()
    justification = reason.strip() or ("Não se aplica ao paciente (consulta)." if status == "descartado" else None)
    log_action(
        db,
        user,
        f"alerta.{status}",
        "alert",
        alert.id,
        {
            "patient_id": alert.patient_id,
            "de": previous,
            "para": status,
            "justificativa": justification,
            "severidade": alert.severity,
            "titulo": alert.title,
            "origem": "consulta",
        },
        commit=False,
    )


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
def _require_not_purged(c: Consultation) -> None:
    """Depois do expurgo (#31), reanalisar rodaria o motor em falas vazias e apagaria os insights que ficam."""
    if (c.extra_json or {}).get("expurgada_em"):
        raise HTTPException(409, "Transcrição expurgada pelo prazo de retenção: não há texto para analisar.")


def analyze(db: Session, fhir: FhirSource, user: User, cid: int, force: bool = False) -> dict:
    c = _get(db, cid)
    _require_not_purged(c)
    _require_ai(c, "a análise por IA externa")
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
        # relevância: a IA reclassifica, mas fala com medicamento, dose, alergia ou sinal de alarme nunca vira social
        if relevancia.apply_ai(segments, {r.n: r.relevancia for r in result.output.relevancia}):
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
    final = _decided(c, merged, engine)
    c.insights_json = final.model_dump(mode="json")
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


def translate_targets(c: Consultation, segments: list[Segment], ns: list[int] | None) -> dict[int, str]:
    """Destino de cada fala ainda sem tradução: fala estrangeira vai para pt-BR; a do profissional (pt-BR) vai para o
    idioma do paciente. Cada fala traz o próprio idioma (acompanhante pode falar outro idioma que o paciente)."""
    targets: dict[int, str] = {}
    for s in segments:
        if s.origem == "marco" or s.traducao or (ns is not None and s.n not in ns):
            continue
        if s.idioma != idiomas.PADRAO:
            targets[s.n] = idiomas.PADRAO
        elif c.patient_language != idiomas.PADRAO and s.falante not in PATIENT_SIDE:
            targets[s.n] = c.patient_language
    return targets


def translate(db: Session, fhir: FhirSource, user: User, cid: int, ns: list[int] | None) -> dict:
    c = _get(db, cid)
    _require_not_purged(c)
    _require_ai(c, "a tradução por IA externa", "traducao")
    record, events, ctx = _chart(fhir, c.patient_id)
    segments = _segments(c)
    targets = translate_targets(c, segments, ns)
    if not targets:
        return {"traducoes": {}, "insights": c.insights_json}
    try:
        done = ai.run_translation(segments, targets, _pseudo(record))
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    for s in segments:
        if s.n in done:
            s.traducao = done[s.n]
            # números, doses e medicamentos conferidos por regra entre a fala e a tradução (não depende do LLM)
            s.traducao_divergente = (
                []
                if idiomas.placeholder(s.traducao)
                else fidelidade.divergencias(s.texto, s.traducao, s.idioma, targets[s.n])
            )
    c.segments_json = [s.model_dump() for s in segments]
    # com a tradução, as regras passam a ler a fala estrangeira pela tradução (Fala/N preservada)
    ins = _refresh_insights(c, record, events, ctx, segments)
    log_action(
        db,
        user,
        "consulta.traducao",
        "consultation",
        c.id,
        {
            "falas": len(done),
            "idioma": c.patient_language,
            "divergentes": sum(1 for s in segments if s.n in done and s.traducao_divergente),
        },
    )
    return {
        "traducoes": {str(k): v for k, v in done.items()},
        "divergentes": {str(s.n): s.traducao_divergente for s in segments if s.n in done and s.traducao_divergente},
        "insights": ins.model_dump(mode="json"),
    }


# ---------- nota, revisão e envio ----------
def _generate_note(
    c: Consultation,
    engine: Engine,
    chart: tuple[PatientRecord, list[ClinicalEvent], PatientContext],
    segments: list[Segment],
    pseudo: Pseudonymizer,
    valid: set[str],
    fallback: bool,
    rules_only: bool = False,
):
    """(nota, descartados, resultado, insights cirúrgicos ou None). Com `fallback`, só as regras: a IA não é chamada."""
    if c.kind == "cirurgia":
        if fallback:
            merged, result = ai.surgical_fallback(engine.run())
        else:
            merged, result = ai.run_surgical(engine, engine.run(), pseudo, valid, rules_only)
        note: dict = {"relatorio_cirurgico": merged.relatorio_cirurgico.model_dump(mode="json")}  # type: ignore[union-attr]
        return note, merged.descartados, result, merged
    record, events, ctx = chart
    ins = Insights.model_validate(c.insights_json) if c.insights_json else engine.run()
    if ins.falas_analisadas != len(segments):
        ins = _insights(c, record, events, ctx, segments)
        c.insights_json = ins.model_dump(mode="json")
    note_model, result, dropped = ai.run_note(engine, ins, pseudo, valid, c.patient_language, rules_only, fallback)
    note = hipoteses.apply_to_note(note_model.model_dump(mode="json"), hipoteses.apply(ins, c.extra_json))
    note = _apply_copilot(note, (c.extra_json or {}).get("copiloto", {}).get("insercoes", []), valid)
    note = notas.ensure(note) | {"modelo": note_templates.snapshot(note_templates.select(c.kind, c.specialty))}
    return note, dropped, result, None


FALLBACK_NOTICE = "IA indisponível: a nota foi montada só pelas regras, sem o texto corrido da IA. Revise com atenção."


def finish(db: Session, fhir: FhirSource, user: User, cid: int, body: FinishIn | None = None) -> dict:
    """Encerra e gera a nota. As falas do corpo (a fila de captura que ainda não tinha chegado) entram na mesma
    transação, antes da nota: se a geração falhar, nada delas fica gravado."""
    c = _get(db, cid)
    if c.status == "enviada":
        raise HTTPException(409, "Nota já enviada ao prontuário.")
    record, events, ctx = _chart(fhir, c.patient_id)
    segments = _segments(c)
    pending = body.segmentos if body else []
    if pending:
        if c.status != "em_andamento":
            raise HTTPException(409, "Consulta finalizada: não aceita novas falas.")
        _require_capture(c)
        _add_segments(c, segments, pending)
    if not [s for s in segments if s.origem != "marco"]:
        raise HTTPException(422, "A consulta não tem falas transcritas.")
    rules_only = not consentimento.flags(c.extra_json).ia
    engine = _engine(c, record, events, ctx, segments)
    valid = _valid_refs(record, events, ctx, segments)
    pseudo = _pseudo(record)
    fallback = False
    try:
        note, dropped, result, merged = _generate_note(
            c, engine, (record, events, ctx), segments, pseudo, valid, False, rules_only
        )
    except LLMError as exc:
        # sem o fornecedor de IA a nota sai das regras (modo regras_fallback): o atendimento não fica sem documento
        fallback = True
        try:
            note, dropped, result, merged = _generate_note(
                c, engine, (record, events, ctx), segments, pseudo, valid, True, rules_only
            )
        except LLMError as exc2:
            db.rollback()  # as falas do corpo não ficam gravadas sem a nota
            log_action(db, user, "consulta.erro_llm", "consultation", c.id, {"erro": str(exc2)})
            raise HTTPException(502, str(exc2)) from exc2
        log_action(db, user, "consulta.erro_llm", "consultation", c.id, {"erro": str(exc), "fallback": "regras"})
    if merged is not None:
        c.insights_json = merged.model_dump(mode="json")
        snap = antiglosa.snapshot(merged.relatorio_cirurgico.prontidao)  # type: ignore[union-attr, arg-type]
        _audit_readiness(db, user, c, "finish", (c.extra_json or {}).get("prontidao_inicial"), snap)
        c.extra_json = (c.extra_json or {}) | {"prontidao_finish": snap}
    c.note_json = note
    c.note_meta_json = {
        "modo": "regras" if rules_only else result.mode,
        "sem_ia": rules_only,  # paciente recusou a IA externa: nota só das regras, com a marca "sem IA"
        "modelo": result.model,
        "prompt_version": result.prompt_version,
        "gerada_em": clock.now().isoformat(),
        "descartados": dropped,
        "cobertura": relevancia.coverage(segments, note) if c.kind != "cirurgia" else None,
        "revisada_por": None,
        "revisada_em": None,
        "atestacao": False,
        "aviso": FALLBACK_NOTICE if fallback else None,
        # cópia imutável do que a IA gerou: base do diff por seção e da prova de supervisão humana (#14)
        "rascunho_ia": copy.deepcopy(note),
        "versoes": [],
    }
    c.status = "finalizada"
    c.finished_at = clock.now()
    c.extra_json = (c.extra_json or {}) | {"nota_original": note, "versoes_nota": []}  # base da métrica "% editado"
    log_action(
        db,
        user,
        "consulta.finalizada",
        "consultation",
        c.id,
        {
            "patient_id": c.patient_id,
            "falas": len(segments),
            "falas_no_corpo": len(pending),
            "modo": "regras" if rules_only else result.mode,
            "sem_ia": rules_only,
            "itens_descartados": len(dropped),
            "especialidade": c.specialty,
            "tipo": c.kind,
            "duracao_s": int((c.finished_at - c.created_at).total_seconds()),
        }
        | _acceptance(c),
    )
    return out(c)


def _acceptance(c: Consultation) -> dict:
    """Aceite por camada (regra x IA) para o painel Impacto: perguntas feitas e hipóteses decididas."""
    if c.kind == "cirurgia" or not c.insights_json:
        return {}
    ins = hipoteses.apply(Insights.model_validate(c.insights_json), c.extra_json)
    perguntas = {layer: {"sugeridas": 0, "aceitas": 0} for layer in ("regra", "ia")}
    for q in ins.perguntas:
        perguntas[q.origem]["sugeridas"] += 1
    for q in ins.perguntas_abordadas:
        perguntas[q.origem]["sugeridas"] += 1
        perguntas[q.origem]["aceitas"] += 1
    hip = {layer: {"sugeridas": 0, "aceitas": 0} for layer in ("regra", "ia")}
    for h in ins.hipoteses:
        hip[h.origem]["sugeridas"] += 1
        hip[h.origem]["aceitas"] += h.decisao in ("confirmada", "provisoria")
    return {
        "aceite_perguntas": perguntas,
        "aceite_hipoteses": hip,
        "aceite_insights": acoes.acceptance(c.insight_actions_json),
    }


MAX_VERSIONS = 30  # versões da nota guardadas (a mais antiga sai primeiro; o rascunho da IA nunca sai)
MIN_JUSTIFICATION_NOTA = 10  # justificativa mínima para enviar com pendência crítica de segurança aberta


def _check_cids(cids: list[dict], previous: list[dict], sex: str) -> list[dict]:
    """CID incluído ou trocado na revisão existe na tabela oficial; um só principal; a descrição é a oficial."""
    known = {x.get("codigo") for x in previous}
    out_: list[dict] = []
    for item in cids:
        code = norm_icd(item.get("codigo"))
        if item.get("codigo") not in known:
            problem = icd_problem(code, sex, item.get("tipo") in ("principal", "hipotese"))
            if problem:
                raise HTTPException(422, f"CID {item.get('codigo')!r} recusado: {problem}.")
            entry = icd_entry(code)
            item = item | {"codigo": entry.code, "descricao": entry.description}  # type: ignore[union-attr]
        out_.append(item)
    if sum(1 for x in out_ if x.get("tipo") == "principal") > 1:
        raise HTTPException(422, "Só pode haver um CID principal.")
    return out_


def _reconcile_note(previous: dict, edited: dict) -> dict:
    """Frases com fonte depois da edição: as intactas mantêm refs e origem; as alteradas viram "profissional"."""
    result = dict(edited)
    for key in notas.SECTION_KEYS:
        if isinstance(edited.get(key), dict):
            result[key] = notas.reconcile_section(previous.get(key), edited[key])
    return result


def _invalidate_attestation(c: Consultation) -> None:
    """A nota mudou depois de o profissional atestar a revisão: a atestação não vale mais para o texto novo."""
    if (c.note_meta_json or {}).get("atestacao"):
        c.note_meta_json = (c.note_meta_json or {}) | {"atestacao": False}


def update_note(db: Session, user: User, cid: int, note: dict, fhir: FhirSource | None = None) -> dict:
    c = _get(db, cid)
    if c.note_json is None:
        raise HTTPException(409, "Finalize a consulta antes de revisar a nota.")
    if c.status == "enviada":
        raise HTTPException(409, "Nota já enviada ao prontuário.")
    sex = _chart(fhir, c.patient_id)[0].sex if fhir is not None else ""
    note = dict(note)
    attested = note.pop("atestacao", False) is True  # "Revisei e assumo a responsabilidade": só o valor true vale
    changes: list[dict] = []
    previous = c.note_json or {}
    try:
        if c.kind == "cirurgia":
            edited = SurgicalReport.model_validate(note.get("relatorio_cirurgico"))
            report = _kept_orders(c, edited.model_dump(mode="json"))
            report = report | {
                "cids": _check_cids(
                    report.get("cids", []), (previous.get("relatorio_cirurgico") or {}).get("cids", []), sex
                )
            }
            report, changes = _review_opme(c, report)
            note = note | {"relatorio_cirurgico": report}
        else:
            ConsultNote.model_validate(note)
            # o modelo da nota é o que o servidor guarda (só muda por PUT /note-template); as seções obrigatórias
            # do modelo precisam estar preenchidas para a nota ser aprovada
            model = (note_templates.upgrade(c.note_json, c.kind, c.specialty) or {})["modelo"]
            note = note | {"modelo": model, "secoes": note_templates.complete_sections(note.get("secoes"), model)}
            _require_sections(note)
            # as propostas de Condition vêm das decisões do profissional na consulta, não da edição do texto
            note = note | {
                "condicoes_propostas": previous.get("condicoes_propostas", []),
                "copiloto": previous.get("copiloto", []),  # proveniência: não vem da edição do texto
                "incluidas": previous.get("incluidas", []),
                "cids": _check_cids(note.get("cids", []), previous.get("cids", []), sex),
            }
            note = _reconcile_note(previous, note)
    except ValidationError as exc:
        raise HTTPException(422, f"Nota em formato inválido: {exc.error_count()} erro(s)") from exc
    c.note_json = note
    if c.kind == "cirurgia":
        _rescore_note(c)
    now = clock.now()
    meta = c.note_meta_json or {}
    pct, changed = edit_percent((c.extra_json or {}).get("nota_original"), note)
    draft = meta.get("rascunho_ia") or (c.extra_json or {}).get("nota_original")
    sections = edit_sections(draft, c.note_json)  # sem texto: só o número de cada seção
    history = (c.extra_json or {}).get("versoes_nota", [])
    version = {
        "n": (history[-1]["n"] if history else 0) + 1,
        "por": user.name,
        "em": now.isoformat(),
        "editado_pct": pct,
        "atestada": attested,
        "secoes": {r["secao"]: r["pct_editado"] for r in sections},
    }
    c.extra_json = (c.extra_json or {}) | {"versoes_nota": [*history, version | {"nota": c.note_json}][-MAX_VERSIONS:]}
    c.note_meta_json = (
        meta
        | {
            "revisada_por": user.name,
            "revisada_em": now.isoformat(),
            "atestacao": attested,
            "desatualizada": None,  # a revisão após a edição da transcrição reconcilia a nota
            "versoes": [*meta.get("versoes", []), version][-MAX_VERSIONS:],
        }
    )
    payload = {
        "patient_id": c.patient_id,
        "editado_pct": pct,
        "palavras_alteradas": changed,
        "versao": version["n"],
        "atestada": attested,
        "secoes_editadas": {k: v for k, v in version["secoes"].items() if v > 0},
    } | ({"opme": changes} if changes else {})
    log_action(db, user, "consulta.nota_revisada", "consultation", c.id, payload)
    return out(c)


def _require_sections(note: dict) -> None:
    """Seção obrigatória do modelo vazia (ou só "Sem registro na consulta") impede a aprovação."""
    empty = note_templates.missing_required(note, note["modelo"])
    if empty:
        raise HTTPException(
            422,
            {
                "codigo": "secao_obrigatoria_vazia",
                "mensagem": f"Preencha as seções obrigatórias antes de aprovar: {', '.join(empty)}.",
                "secoes": empty,
            },
        )


def switch_note_template(db: Session, fhir: FhirSource, user: User, cid: int, template_id: str) -> dict:
    """Troca o modelo da nota na revisão: o texto já escrito fica, e as seções novas do modelo recebem o rascunho
    das regras (prontuário, campos da especialidade e falas citadas)."""
    c = _get(db, cid)
    if c.kind == "cirurgia":
        raise HTTPException(409, "O relatório cirúrgico tem modelo próprio por procedimento.")
    if c.note_json is None:
        raise HTTPException(409, "Finalize a consulta antes de escolher o modelo da nota.")
    if c.status == "enviada":
        raise HTTPException(409, "Nota já enviada ao prontuário.")
    tpl = note_templates.get(template_id)
    if tpl is None:
        raise HTTPException(422, "Modelo de nota desconhecido.")
    note = dict(_nota(c) or {})
    if note["modelo"]["id"] == tpl.id:
        return out(c)
    record, events, ctx = _chart(fhir, c.patient_id)
    segments = _segments(c)
    engine = _engine(c, record, events, ctx, segments)
    ins = Insights.model_validate(c.insights_json) if c.insights_json else engine.run()
    filled = {s["key"] for s in note["secoes"] if not note_templates.is_empty(s["texto"])}
    news = note_templates.fill_dynamic(tpl, engine, ins, only={s.key for s in tpl.secoes} - filled)
    model = note_templates.snapshot(tpl)
    kept = [s for s in note["secoes"] if s["key"] in filled]
    note = note | {"modelo": model, "secoes": note_templates.complete_sections(kept + news, model)}
    previous = c.note_json
    c.note_json = note
    log_action(
        db,
        user,
        "consulta.modelo_nota",
        "consultation",
        c.id,
        {
            "patient_id": c.patient_id,
            "de": (previous or {}).get("modelo", {}).get("id"),
            "para": tpl.id,
            "versao": tpl.versao,
        },
    )
    return out(c)


def recompute_surgical(db: Session, user: User, cid: int, note: dict) -> dict:
    """Recalcula o relatório cirúrgico editado (ainda não salvo): OPME conferido com guia, fala e etiquetas, pendências
    de campo obrigatório e de OPME, e a prontidão para faturamento. Não grava nada: a revisão grava ao salvar."""
    c = _get(db, cid)
    if c.kind != "cirurgia" or c.note_json is None:
        raise HTTPException(409, "O recálculo vale para o relatório cirúrgico já gerado.")
    if c.status == "enviada":
        raise HTTPException(409, "Nota já enviada ao prontuário.")
    try:
        edited = SurgicalReport.model_validate(note.get("relatorio_cirurgico"))
    except ValidationError as exc:
        raise HTTPException(422, f"Relatório em formato inválido: {exc.error_count()} erro(s)") from exc
    report = _kept_orders(c, edited.model_dump(mode="json"))
    report, _ = _review_opme(c, report)
    prefix = "Campo obrigatório não ditado:"
    kept = [p for p in report["pendencias"] if not p["texto"].startswith(prefix)]
    kept += [
        {
            "texto": f"{prefix} {f['rotulo']}. {f.get('motivo') or ''}".strip(),
            "severidade": "atencao",
            "refs": [],
            "categoria": None,
        }
        for f in report["campos"]
        if f.get("obrigatorio") and not (f.get("texto") or "").strip()
    ]
    kept.sort(key=lambda h: {"critico": 0, "atencao": 1, "info": 2}[h["severidade"]])
    model = SurgicalReport.model_validate(report | {"pendencias": kept})
    model.prontidao = antiglosa.evaluate(model, segments=_segments(c), **billing_params(c.extra_json or {}))
    return {"relatorio_cirurgico": model.model_dump(mode="json")}


def note_diff(db: Session, user: User, cid: int) -> dict:
    """Diff por seção entre o rascunho da IA (imutável) e a nota atual: a prova objetiva da supervisão humana."""
    c = _get(db, cid)
    meta = c.note_meta_json or {}
    draft = meta.get("rascunho_ia")
    if c.note_json is None or draft is None:
        raise HTTPException(409, "Finalize a consulta para comparar a nota com o rascunho da IA.")
    sections = edit_sections(draft, c.note_json, with_text=True)
    pct, changed = edit_percent(draft, c.note_json)
    return {
        "consulta_id": c.id,
        "editado_pct": pct,
        "palavras_alteradas": changed,
        "revisada_por": meta.get("revisada_por"),
        "revisada_em": meta.get("revisada_em"),
        "atestacao": bool(meta.get("atestacao")),
        "versoes": meta.get("versoes", []),
        "secoes": sections,
    }


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


def _require_review(c: Consultation) -> None:
    """Trava no servidor: a nota só sai (exportar ou enviar) revisada por uma pessoa e com a atestação."""
    if c.note_json is None:
        raise HTTPException(409, "Finalize e revise a nota antes de exportar ou enviar.")
    meta = c.note_meta_json or {}
    if not meta.get("revisada_por"):
        raise HTTPException(
            409,
            {
                "codigo": "revisao_pendente",
                "mensagem": "A nota ainda não foi revisada. Revise e salve a revisão antes de exportar ou enviar.",
            },
        )
    if not meta.get("atestacao"):
        raise HTTPException(
            409,
            {
                "codigo": "atestacao_pendente",
                "mensagem": "Falta a atestação: marque “Revisei e assumo a responsabilidade” ao salvar a revisão.",
            },
        )


def _allergy_registered(name: str, clauses: list[str]) -> bool:
    """A nota registra a alergia quando cita o alérgeno numa oração sem negação ("Nega alergia a camarão" não vale)."""
    for clause in clauses:
        position = clause.find(name) if name else -1
        if position >= 0 and not NEG_WORDS & set(clause[:position].split()[-4:]):
            return True
    return False


def clinical_pending(c: Consultation) -> list[dict]:
    """Pendências críticas de segurança ainda abertas na nota: [{codigo, texto, refs}].

    Consulta e visita: alergia relatada que a nota não registra e alerta crítico de medicamento (interação, conflito de
    alergia, tríplice ameaça renal…) que o profissional não aceitou nem descartou com motivo.
    Cirurgia: campo obrigatório vazio e OPME sem autorização (sem justificativa)."""
    note = c.note_json or {}
    found: list[dict] = []
    if c.kind == "cirurgia":
        report = note.get("relatorio_cirurgico") or {}
        for f in report.get("campos", []):
            if f.get("obrigatorio") and not (f.get("texto") or "").strip():
                found.append(
                    {
                        "codigo": "campo_obrigatorio_vazio",
                        "texto": f"Campo obrigatório vazio: {f['rotulo']}",
                        "refs": [],
                    }
                )
        for o in report.get("opme", []):
            if o.get("status") == "nao_autorizado" and not (o.get("justificativa") or "").strip():
                found.append(
                    {
                        "codigo": "opme_sem_autorizacao",
                        "texto": f"OPME sem autorização na guia: {o['descricao']}",
                        "refs": o.get("refs", []),
                    }
                )
        return found
    if not c.insights_json:
        return found
    ins = Insights.model_validate(c.insights_json)
    registered = text_clauses((note.get("alergias") or {}).get("texto", ""))
    for al in ins.alergias:
        names = [norm(x) for x in re.split(r"[/,]", al.alergeno) if x.strip()]
        if al.fonte == "relatado" and not any(_allergy_registered(n, registered) for n in names):
            found.append(
                {
                    "codigo": "alergia_nao_registrada",
                    "texto": f"Alergia relatada não registrada na nota: {al.alergeno}",
                    "refs": al.refs,
                }
            )
    for m in ins.sugestoes_medicamentos:
        if m.severidade == "critico" and m.acao not in ("aceitar", "feita"):  # descartado com motivo já saiu da lista
            found.append({"codigo": "alerta_critico_nao_aceito", "texto": m.texto, "refs": m.refs})
    return found


def _gate_clinical(db: Session, user: User, c: Consultation, justification: str | None) -> None:
    """Pendência crítica aberta bloqueia o envio; libera com justificativa (≥ 10 caracteres), auditada, como a alta."""
    pending = clinical_pending(c)
    if not pending:
        return
    text = (justification or "").strip()
    if len(acoes.efetivo(text)) < MIN_JUSTIFICATION_NOTA:
        raise HTTPException(
            409,
            {
                "codigo": "pendencia_critica",
                "mensagem": f"Há {len(pending)} pendência(s) crítica(s) de segurança abertas. Resolva-as ou envie com "
                f"uma justificativa de pelo menos {MIN_JUSTIFICATION_NOTA} caracteres.",
                "pendencias": pending,
            },
        )
    codes = sorted({p["codigo"] for p in pending})
    c.extra_json = (c.extra_json or {}) | {
        "justificativa_pendencias": {"texto": text, "por": user.name, "em": clock.now().isoformat(), "regras": codes}
    }
    log_action(
        db,
        user,
        "consulta.envio_com_pendencia",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "justificativa": text, "pendencias": codes, "quantidade": len(pending)},
        commit=False,
    )


def _gate_clinical_export(c: Consultation) -> None:
    """Copiar ou baixar a nota é sair dela do mesmo jeito que enviar: a pendência crítica só passa se o envio já foi
    liberado com justificativa que cobre todas as pendências de agora (a justificativa vem no /send)."""
    pending = clinical_pending(c)
    if not pending:
        return
    given = (c.extra_json or {}).get("justificativa_pendencias") or {}
    covered = len(acoes.efetivo(given.get("texto"))) >= MIN_JUSTIFICATION_NOTA and {
        p["codigo"] for p in pending
    } <= set(given.get("regras") or [])
    if not covered:
        raise HTTPException(
            409,
            {
                "codigo": "pendencia_critica",
                "mensagem": f"Há {len(pending)} pendência(s) crítica(s) de segurança abertas. Resolva-as ou envie com "
                "justificativa antes de copiar ou baixar a nota.",
                "pendencias": pending,
            },
        )


def _interpretacao(c: Consultation) -> str | None:
    """Linha "Consulta com interpretação por IA (es→pt-BR)": só com tradução de fato, um par por idioma de origem."""
    pairs: list[str] = []
    for s in _segments(c):
        if not s.traducao or idiomas.placeholder(s.traducao):
            continue
        src, dst = (s.idioma, idiomas.PADRAO) if s.idioma != idiomas.PADRAO else (idiomas.PADRAO, c.patient_language)
        pair = f"{src}→{dst}"
        if pair not in pairs:
            pairs.append(pair)
    return f"Consulta com interpretação por IA ({', '.join(sorted(pairs))})" if pairs else None


def _document(db: Session, fhir: FhirSource, user: User, c: Consultation) -> tuple[str, dict]:
    _require_review(c)
    record = load_record(fhir, c.patient_id)
    now = clock.now()
    role = {"medico": "Médico(a)", "enfermagem": "Enfermagem"}.get(user.role, user.role)
    author = f"{user.name} ({role})"
    note = reidentify(_nota(c), record)  # o prontuário recebe os nomes, não os tokens enviados à IA
    interp = _interpretacao(c)
    text = note_as_text(
        c.kind,
        record.name,
        author,
        _local(now),
        note,
        c.patient_language,
        _signed_orders(c),
        interpretacao=interp,
        selo=consultorio.selo_sandbox(selo_profissional(user)) if c.sandbox else selo_profissional(user),
    )
    doc = document_reference(
        interpretacao=interp,
        kind=c.kind,
        patient_id=c.patient_id,
        patient_name=record.name,
        encounter_id=c.encounter_id,
        author=author,
        when=now,
        text=text,
        consultation_id=c.id,
        loinc=note_templates.loinc_of(note),
        title=(note.get("modelo") or {}).get("titulo"),
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
    _require_review(c)
    _gate_clinical_export(c)
    text, doc = _document(db, fhir, user, c)
    log_action(db, user, "consulta.exportada", "consultation", c.id, {"patient_id": c.patient_id})
    orders = _orders(c, _author(doc), clock.now())
    result = {"texto": text, "recurso": doc, "pedidos": orders}
    if c.kind == "cirurgia" or orders:
        result["bundle"] = _bundle(c, load_record(fhir, c.patient_id), doc, orders)
    return result


def export_pdf(db: Session, fhir: FhirSource, user: User, cid: int) -> tuple[bytes, str]:
    """Nota revisada e atestada em PDF (mesmas travas da exportação em texto). Devolve o arquivo e o nome sugerido."""
    result = export(db, fhir, user, cid)
    c = _get(db, cid)
    log_action(db, user, "consulta.exportada_pdf", "consultation", c.id, {"patient_id": c.patient_id})
    title = KIND_TITLE.get(c.kind, "Nota clínica") + (" (sandbox)" if c.sandbox else "")
    return pdf.render(title, result["texto"], rodape=f"HousIA · consulta {c.id}"), f"nota-consulta-{c.id}.pdf"


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


def _timing(c: Consultation, moment: datetime) -> dict:
    """Do fim da consulta ao envio (ou à exportação), para o painel Impacto: tempo até o envio e mesmo dia."""
    return {
        "especialidade": c.specialty,
        "tipo": c.kind,
        "finalizada_em": c.finished_at.isoformat() if c.finished_at else None,
        "tempo_ate_envio_s": int((moment - c.finished_at).total_seconds()) if c.finished_at else None,
    }


def send_to_ehr(db: Session, fhir: FhirSource, user: User, cid: int, justification: str | None = None) -> dict:
    c = _get(db, cid)
    if c.status == "enviada" and c.fhir_ref and get_settings().ehr_writeback:
        raise HTTPException(409, f"Nota já enviada ({c.fhir_ref}).")
    if c.kind != "cirurgia" and c.note_json is not None:
        _require_sections(_nota(c) or {})
    _require_review(c)
    _gate_billing(db, user, c, justification)
    _gate_clinical(db, user, c, justification)
    record = load_record(fhir, c.patient_id)
    text, doc = _document(db, fhir, user, c)
    if not get_settings().ehr_writeback or c.sandbox:  # sandbox nunca escreve no prontuário (#34)
        log_action(
            db,
            user,
            "consulta.exportada",
            "consultation",
            c.id,
            {"patient_id": c.patient_id} | _timing(c, clock.now()),
        )
        orders = _orders(c, _author(doc), clock.now())
        return {
            "enviado": False,
            "motivo": (
                "Consulta de sandbox (cenário sintético): nunca é enviada ao prontuário. Copie o texto ou baixe o PDF."
                if c.sandbox
                else "Envio automático desligado (EHR_WRITEBACK=false): copie o texto ou baixe o FHIR."
            ),
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
        {
            "patient_id": c.patient_id,
            "fhir_ref": c.fhir_ref,
            "pedidos": orders,
        }
        | _timing(c, c.sent_at)
        | traceability,
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


# ---------- copiloto: discutir o caso / perguntar ao prontuário ----------
MAX_HISTORY = 60


def _copilot(c: Consultation) -> dict:
    data = (c.extra_json or {}).get("copiloto") or {}
    return {"mensagens": list(data.get("mensagens", [])), "insercoes": list(data.get("insercoes", []))}


def _save_copilot(c: Consultation, data: dict) -> None:
    data = {"mensagens": data["mensagens"][-MAX_HISTORY:], "insercoes": data["insercoes"]}
    c.extra_json = (c.extra_json or {}) | {"copiloto": data}


def _suggestions(c: Consultation) -> list[dict]:
    """Alerta crítico na tela: o copiloto sugere discutir (a pergunta já vem pronta)."""
    ins = Insights.model_validate(c.insights_json) if c.insights_json else None
    if ins is None or c.kind == "cirurgia":
        return []
    return [
        {"alerta": a.texto, "pergunta": "Quais os alertas críticos deste paciente e o que eles envolvem?"}
        for a in ins.alertas[:1]
        if a.severidade == "critico"
    ]


def copilot_state(db: Session, fhir: FhirSource, user: User, cid: int) -> dict:
    c = _get(db, cid)
    data = _copilot(c)
    return data | {"sugestoes": _suggestions(c), "llm_mode": get_settings().llm_mode}


def _note_target(destino: str) -> str:
    return destino if destino in copilot.NOTE_TARGETS or destino == "pendencia" else "avaliacao"


def _apply_copilot(note: dict, insertions: list[dict], valid: set[str] | None = None) -> dict:
    """Leva as inserções do copiloto para a nota, com as refs; a proveniência (origem "copiloto") fica em `copiloto`."""
    done = {i["id"] for i in note.get("copiloto", [])}
    note = dict(note)
    applied = list(note.get("copiloto", []))
    for ins in insertions:
        if ins["id"] in done:
            continue
        refs = [r for r in ins["refs"] if valid is None or r in valid]
        if ins["destino"] == "pendencia":
            note["pendencias"] = [*note.get("pendencias", []), {"texto": ins["texto"], "refs": refs}]
        else:
            note[ins["destino"]] = notas.append_text(
                note.get(ins["destino"]) or {"texto": "", "refs": []}, ins["texto"], refs, "ia"
            )
        applied.append({k: ins[k] for k in ("id", "destino", "texto", "refs", "origem", "em")})
    note["copiloto"] = applied
    return note


def _new_insertion(data: dict, msg_id: int, destino: str, texto: str, refs: list[str], by: str) -> tuple[dict, bool]:
    key = (msg_id, destino, texto)
    for i in data["insercoes"]:
        if (i["mensagem_id"], i["destino"], i["texto"]) == key:
            return i, False
    ins = {
        "id": f"cop{len(data['insercoes']) + 1}",
        "mensagem_id": msg_id,
        "destino": destino,
        "texto": texto,
        "refs": list(dict.fromkeys(refs)),
        "origem": "copiloto",
        "por": by,
        "em": clock.now().isoformat(),
    }
    data["insercoes"].append(ins)
    return ins, True


def _push_insertion(c: Consultation, data: dict, ins: dict) -> None:
    """Consulta já finalizada: a inserção entra na nota na hora; senão entra quando a nota for gerada."""
    if c.note_json is not None and c.kind != "cirurgia":
        c.note_json = _apply_copilot(c.note_json, [ins])
        _invalidate_attestation(c)


def ask(db: Session, fhir: FhirSource, user: User, cid: int, question: str, via: str = "texto") -> dict:
    c = _get(db, cid)
    question = question.strip()
    if not question:
        raise HTTPException(422, "Escreva ou dite a pergunta.")
    if c.status == "enviada":
        raise HTTPException(409, "Consulta já enviada ao prontuário.")
    record, events, ctx = _chart(fhir, c.patient_id)
    segments = _segments(c)
    engine = _engine(c, record, events, ctx, segments)
    if c.kind == "cirurgia":
        raise HTTPException(409, "O copiloto não está disponível no centro cirúrgico.")
    ins = Insights.model_validate(c.insights_json) if c.insights_json else _insights(c, record, events, ctx, segments)
    actx = copilot.Ctx(engine=engine, ins=ins, q=question, n=copilot.norm(question))
    valid = _valid_refs(record, events, ctx, segments)
    data = _copilot(c)
    msg_id = len(data["mensagens"]) + 1
    pseudo = _pseudo(record)
    action = None
    cmd = copilot.parse_command(question)
    if cmd:
        # comando: o texto vem do profissional (ou da última resposta do copiloto, se ele só disse "adicionar ao plano")
        last = next((m for m in reversed(data["mensagens"]) if m["resposta"]["frases"]), None)
        texto, refs, ref_msg = cmd.texto, [], msg_id
        if not texto and last:
            frases = last["resposta"]["frases"]
            texto, refs, ref_msg = (
                " ".join(f["texto"] for f in frases),
                [r for f in frases for r in f["refs"]],
                last["id"],
            )
        if not texto:
            resp = copilot.Resposta(
                intencao="comando", aviso='Diga o que adicionar (ex.: "adicionar ao plano repetir potássio").'
            )
        else:
            new, _ = _new_insertion(data, ref_msg, cmd.destino, texto, [r for r in refs if r in valid], user.name)
            _push_insertion(c, data, new)
            label = "plano" if cmd.destino == "plano" else "pendências"
            resp = copilot.Resposta(
                intencao="comando",
                frases=[copilot.Frase(texto=f"Adicionado a {label} da nota: {texto}", fonte="Comando do profissional")],
                acao={"tipo": cmd.destino, "insercao": new["id"]},
            )
        action = resp.acao
        result = None
    else:
        resp = copilot.answer_by_rules(actx)
        result = None
        if resp is None and not consentimento.flags(c.extra_json).ia:
            resp = copilot.Resposta(
                intencao="sem_resposta",
                aviso="O paciente não autorizou IA externa: respondo só por regra (exames e valores, alergias, o que "
                "mudou, 'posso usar X?', dose por TFG, interação entre medicamentos e alertas).",
            )
        if resp is None:
            try:
                resp, result = copilot.run_ai(actx, pseudo, valid, copilot.rules_tools(actx))
            except LLMError as exc:
                log_action(db, user, "consulta.erro_llm", "consultation", c.id, {"erro": str(exc)})
                raise HTTPException(502, str(exc)) from exc
        resp.frases, dropped = (
            copilot.validate(resp.frases, valid) if resp.intencao not in ("ia", "sem_resposta") else (resp.frases, [])
        )
        resp.descartados += dropped
    resp = copilot.Resposta.model_validate(reidentify(resp.model_dump(), record))
    message = {
        "id": msg_id,
        "pergunta": question,
        "via": via,
        "em": clock.now().isoformat(),
        "resposta": resp.model_dump(),
    }
    data["mensagens"].append(message)
    _save_copilot(c, data)
    log_action(
        db,
        user,
        "consulta.copiloto",
        "consultation",
        c.id,
        {
            "patient_id": c.patient_id,
            "intencao": resp.intencao,
            "modo": resp.modo,
            "via": via,
            "frases": len(resp.frases),
            "itens_descartados": len(resp.descartados),
            "pseudonimizacoes": pseudo.replacements,
            **({"acao": action["tipo"]} if action else {}),
        },
    )
    return {"mensagem": message, "insercoes": data["insercoes"], "sugestoes": _suggestions(c)}


def insert_into_note(
    db: Session, fhir: FhirSource, user: User, cid: int, msg_id: int, destino: str, frases: list[int] | None
) -> dict:
    """ "Inserir na nota": leva as frases do copiloto com as refs (origem "copiloto") para a seção escolhida."""
    c = _get(db, cid)
    if c.status == "enviada":
        raise HTTPException(409, "Nota já enviada ao prontuário.")
    if c.kind == "cirurgia":
        raise HTTPException(409, "O copiloto não está disponível no centro cirúrgico.")
    data = _copilot(c)
    msg = next((m for m in data["mensagens"] if m["id"] == msg_id), None)
    if msg is None:
        raise HTTPException(404, "Resposta do copiloto não encontrada")
    all_frases = msg["resposta"]["frases"]
    chosen = [
        all_frases[i] for i in (frases if frases is not None else range(len(all_frases))) if 0 <= i < len(all_frases)
    ]
    if not chosen:
        raise HTTPException(422, "Nada para inserir na nota.")
    record, events, ctx = _chart(fhir, c.patient_id)
    valid = _valid_refs(record, events, ctx, _segments(c))
    refs = [r for f in chosen for r in f["refs"] if r in valid]
    texto = " ".join(f["texto"] for f in chosen)
    ins, created = _new_insertion(data, msg_id, _note_target(destino), texto, refs, user.name)
    if created:
        _push_insertion(c, data, ins)
        _save_copilot(c, data)
        log_action(
            db,
            user,
            "consulta.copiloto_inserido",
            "consultation",
            c.id,
            {"patient_id": c.patient_id, "destino": ins["destino"], "refs": len(ins["refs"])},
        )
    return {"insercao": ins, "criada": created, "insercoes": data["insercoes"], "nota": _nota(c)}


def include_segment(db: Session, user: User, cid: int, n: int, destino: str) -> dict:
    """ "Incluir em…": leva a fala clínica que ficou fora da nota para a seção escolhida, citando Fala/N."""
    c = _get(db, cid)
    if c.kind == "cirurgia":
        raise HTTPException(409, "A cobertura da nota não se aplica ao centro cirúrgico.")
    if c.note_json is None:
        raise HTTPException(409, "Finalize a consulta antes de incluir falas na nota.")
    if c.status == "enviada":
        raise HTTPException(409, "Nota já enviada ao prontuário.")
    segments = _segments(c)
    seg = next((s for s in segments if s.n == n), None)
    if seg is None:
        raise HTTPException(404, "Fala não encontrada")
    if destino not in relevancia.NOTE_SECTIONS and destino != "pendencia":
        raise HTTPException(422, "Seção da nota inválida.")
    note = relevancia.include(c.note_json, seg, destino, user.name, clock.now().isoformat())
    c.note_json = note
    _invalidate_attestation(c)
    log_action(
        db,
        user,
        "consulta.fala_incluida_na_nota",
        "consultation",
        c.id,
        {"patient_id": c.patient_id, "fala": n, "destino": destino},
    )
    return {
        "nota": _nota(c),
        "cobertura": relevancia.coverage(segments, c.note_json),
        "incluidas": note["incluidas"],
    }

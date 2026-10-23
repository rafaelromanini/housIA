"""Cartão "Antes de entrar" (#44): leitura dos registros do HousIA, resumo opcional da IA e auditoria."""

from __future__ import annotations

import json
import re

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.consult import pre_consulta
from app.consult.chart import ChartView, fmt_date
from app.consult.schemas import Segment
from app.db.models import Consultation, DischargeAssessment, DischargeDocument, JourneyTask, MedrecRun, User
from app.fhir_client.client import FhirSource
from app.llm.client import LLMError, generate_structured
from app.llm.pseudonymize import Pseudonymizer
from app.planos import service as planos
from app.timeline.record import PatientRecord

NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


class _ResumoOut(BaseModel):
    frases: list[pre_consulta.PreResumo]


def _mock_resumo(variables: dict) -> dict:
    """Em mock, as frases são as próprias linhas do cartão (as mesmas do resumo por regras)."""
    pick = [x for x in variables["cartao"].get("destaques", []) if x["refs"]][:3]
    return {"frases": [{"texto": x["texto"], "refs": x["refs"]} for x in pick]}


def _sources(db: Session, patient_id: str, skip_consultation: int | None) -> pre_consulta.Sources:
    src = pre_consulta.Sources()
    rows = db.scalars(
        select(Consultation)
        .where(
            Consultation.patient_id == patient_id,
            Consultation.status.in_(("finalizada", "enviada")),
            Consultation.kind != "cirurgia",
            Consultation.sandbox.is_(False),
        )
        .order_by(Consultation.finished_at.desc())
    ).all()
    for c in rows:
        if c.id == skip_consultation or not c.note_json:
            continue
        src.last_note = {
            "id": c.id,
            "data": fmt_date(c.finished_at) or "",
            "pendencias": [
                {"texto": p.get("texto", ""), "refs": p.get("refs", [])}
                for p in c.note_json.get("pendencias", [])
                if p.get("texto")
            ],
        }
        break
    tasks = db.scalars(
        select(JourneyTask).where(JourneyTask.patient_id == patient_id, JourneyTask.status == "aberta")
    ).all()
    src.tasks = [{"texto": t.title, "refs": list(t.source_refs or []), "prazo": fmt_date(t.due_at)} for t in tasks]
    if planos.tem_modulo(db, "alta"):  # a avaliação de alta é do módulo hospitalar do plano
        src.discharge = _discharge(db, patient_id)
    return src


def _discharge(db: Session, patient_id: str) -> dict | None:
    a = db.scalars(
        select(DischargeAssessment)
        .where(DischargeAssessment.patient_id == patient_id)
        .order_by(DischargeAssessment.created_at.desc())
    ).first()
    if a is None or (clock.now() - a.created_at).days > pre_consulta.MAX_DAYS_DISCHARGE:
        return None
    doc = db.scalars(
        select(DischargeDocument)
        .where(DischargeDocument.assessment_id == a.id)
        .order_by(DischargeDocument.created_at.desc())
    ).first()
    content = (doc.content_json if doc else {}) or {}
    plan = (content.get("soap") or {}).get("plano")
    alarms = (content.get("orientacoes") or {}).get("sinais_de_alerta") or []
    run = db.scalars(
        select(MedrecRun).where(MedrecRun.patient_id == patient_id).order_by(MedrecRun.created_at.desc())
    ).first()
    findings = (
        [f for f in ((run.findings_json or {}).get("achados") or []) if f.get("severidade") in ("critico", "atencao")]
        if run
        else []
    )
    comps = (a.lace_components_json or {}).get("componentes") or {}
    return {
        "id": a.id,
        "data": fmt_date(a.created_at),
        "lace": a.lace_score,
        "banda": (a.lace_components_json or {}).get("banda", ""),
        "risco": a.final_risk,
        "lace_refs": [r for comp in comps.values() for r in comp.get("refs", [])][:4],
        "plano": {"texto": plan["texto"], "refs": plan.get("refs", [])} if plan and plan.get("texto") else None,
        "alarmes": [{"texto": s["texto"], "refs": s.get("refs", [])} for s in alarms[:4] if s.get("texto")],
        "achados": [
            {"titulo": f["titulo"], "severidade": f["severidade"], "refs": f.get("refs", [])} for f in findings[:3]
        ],
    }


def _resumo(pre: pre_consulta.PreConsulta, record: PatientRecord, specialty: str) -> None:
    """Resumo de até 3 frases. Real: LLM sobre o cartão pseudonimizado; frase sem ref do cartão ou número novo cai."""
    base = pre_consulta.rules_summary(pre)
    pseudo = Pseudonymizer(**record.identifiers_for_pseudonymization())
    lines = [x for part in (pre.motivo, pre.mudou, pre.pendencias, pre.ultima_alta) for x in part if not x.tratada]
    cartao = pseudo.apply({"destaques": [{"texto": x.texto, "refs": x.refs} for x in lines[:20]]})
    try:
        result = generate_structured(
            "consult_pre_consulta", {"especialidade": specialty, "cartao": cartao}, _ResumoOut, _mock_resumo, "low"
        )
    except LLMError:
        pre.resumo_ia, pre.modo_resumo = base, "regras"
        return
    valid = pre_consulta.all_refs(pre)
    allowed = set(NUMBER.findall(json.dumps(cartao, ensure_ascii=False)))
    kept = [
        f
        for f in result.output.frases
        if f.refs and set(f.refs) <= valid and set(NUMBER.findall(f.texto)) <= allowed  # o LLM não calcula
    ][:3]
    pre.resumo_ia = kept or base
    pre.modo_resumo = "ia" if result.mode == "real" and kept else "regras"


def get(
    db: Session,
    fhir: FhirSource,
    user: User,
    patient_id: str,
    consulta: int | None = None,
    resumo: bool = False,
) -> dict:
    from app.consult import service

    record, events, ctx = service._chart(fhir, patient_id)
    chart = ChartView(record, events, ctx, clock.now())
    segments: list[Segment] | None = None
    encounter_id = None
    specialty = "clinica_medica"
    if consulta is not None:
        c = service._get(db, consulta)  # filtro por organização: de outra, 404
        if c.patient_id != patient_id:
            raise HTTPException(404, "Consulta não encontrada")
        segments = service._segments(c)
        encounter_id, specialty = c.encounter_id, c.specialty
    pre = pre_consulta.build(chart, patient_id, _sources(db, patient_id, consulta), segments, encounter_id)
    if resumo:
        _resumo(pre, record, specialty)
    log_action(
        db,
        user,
        "consulta.pre_consulta",
        "patient",
        patient_id,
        {
            "linhas": len(pre.mudou) + len(pre.pendencias) + len(pre.ultima_alta) + len(pre.motivo),
            "pendencias": len(pre.pendencias),
            "tratadas": sum(x.tratada for x in pre.pendencias),
            "com_resumo": resumo,
            "consulta": consulta,
        },
    )
    return pre.model_dump()

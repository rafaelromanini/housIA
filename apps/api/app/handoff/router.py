from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_action
from app.auth.deps import get_current_user
from app.db.models import HandoffReport, User
from app.db.session import get_db
from app.fhir_client.client import FhirSource, get_fhir
from app.handoff.schemas import Audience, HandoffCheckRequest, HandoffRequest
from app.handoff.service import check_verbal_handoff, generate_handoff, report_out
from app.llm.client import LLMError
from app.planos.service import exige_modulo

router = APIRouter(tags=["passagem de plantão"], dependencies=[Depends(exige_modulo("passagem_plantao"))])


@router.post("/patients/{patient_id}/handoff")
def create_handoff(
    patient_id: str,
    body: HandoffRequest,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(get_current_user),
) -> dict:
    """Gera (ou devolve do cache) a passagem de plantão para o público pedido."""
    try:
        return generate_handoff(db, fhir, user, patient_id, body)
    except LLMError as exc:
        db.rollback()
        log_action(db, user, "handoff.erro_llm", "patient", patient_id, {"erro": str(exc)})
        raise HTTPException(502, str(exc)) from exc


@router.get("/patients/{patient_id}/handoff/latest")
def latest_handoff(
    patient_id: str,
    audience: Audience,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict | None:
    report = db.scalar(
        select(HandoffReport)
        .where(HandoffReport.patient_id == patient_id, HandoffReport.audience == audience)
        .order_by(HandoffReport.id.desc())
    )
    if report is None:
        return None
    log_action(db, user, "handoff.visualizado", "handoff_report", report.id, {"patient_id": patient_id})
    return report_out(report, cached=True)


@router.post("/patients/{patient_id}/handoff/check")
def check_handoff(
    patient_id: str,
    body: HandoffCheckRequest,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(get_current_user),
) -> dict:
    """Confere a passagem verbal: fatos relevantes mencionados, omitidos ou contraditos no relato."""
    try:
        return check_verbal_handoff(db, fhir, user, patient_id, body)
    except LLMError as exc:
        log_action(db, user, "handoff.erro_llm", "patient", patient_id, {"erro": str(exc)})
        raise HTTPException(502, str(exc)) from exc

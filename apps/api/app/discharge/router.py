from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_action
from app.auth.deps import get_current_user, require_roles
from app.db.models import DischargeAssessment, DischargeDocument, User
from app.db.session import get_db
from app.discharge.documents import Language
from app.discharge.scoring import SocialChecklist, prefill
from app.discharge.service import (
    assessment_out,
    confirm_discharge,
    create_assessment,
    documents_out,
    generate_documents,
    register_override,
)
from app.fhir_client.client import FhirSource, get_fhir
from app.llm.client import LLMError
from app.patients.service import load_record
from app.planos.service import exige_modulo

router = APIRouter(tags=["planejamento de alta"], dependencies=[Depends(exige_modulo("alta"))])


class AssessmentIn(BaseModel):
    social_checklist: SocialChecklist


class OverrideIn(BaseModel):
    justification: str


@router.get("/patients/{patient_id}/discharge-assessment/prefill")
def get_prefill(patient_id: str, fhir: FhirSource = Depends(get_fhir), _: User = Depends(get_current_user)) -> dict:
    """Respostas sugeridas a partir do prontuário (moradores, cuidador, Morse). Sempre confirmar."""
    return prefill(load_record(fhir, patient_id))


@router.post("/patients/{patient_id}/discharge-assessment")
def post_assessment(
    patient_id: str,
    body: AssessmentIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        return create_assessment(db, fhir, user, patient_id, body.social_checklist)
    except LLMError as exc:
        db.rollback()
        log_action(db, user, "discharge.erro_llm", "patient", patient_id, {"erro": str(exc)})
        raise HTTPException(502, str(exc)) from exc


@router.get("/patients/{patient_id}/discharge-assessment/latest")
def latest_assessment(
    patient_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> dict | None:
    a = db.scalar(
        select(DischargeAssessment)
        .where(DischargeAssessment.patient_id == patient_id)
        .order_by(DischargeAssessment.id.desc())
    )
    return assessment_out(db, a) if a else None


@router.post("/discharge-assessments/{assessment_id}/override")
def post_override(
    assessment_id: int,
    body: OverrideIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("medico")),
) -> dict:
    """Override humano da trava suave: exige justificativa (mín. 20 caracteres), gravada e auditada."""
    return register_override(db, user, assessment_id, body.justification)


@router.post("/discharge-assessments/{assessment_id}/confirm")
def post_confirm(
    assessment_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("medico"))
) -> dict:
    """Confirma a alta no HousIA (não escreve no prontuário). Bloqueada (409) se risco alto sem override."""
    return confirm_discharge(db, user, assessment_id)


class DocumentsIn(BaseModel):
    idioma: Language = "pt-BR"


@router.post("/patients/{patient_id}/discharge-documents")
def post_documents(
    patient_id: str,
    body: DocumentsIn,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(get_current_user),
) -> dict:
    """Sumário de alta (SOAP) para o prontuário + orientações ao paciente/cuidador em linguagem simples."""
    try:
        return generate_documents(db, fhir, user, patient_id, body.idioma)
    except LLMError as exc:
        db.rollback()
        log_action(db, user, "discharge.erro_llm", "patient", patient_id, {"erro": str(exc)})
        raise HTTPException(502, str(exc)) from exc


@router.get("/patients/{patient_id}/discharge-documents/latest")
def latest_documents(
    patient_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> dict | None:
    doc = db.scalar(
        select(DischargeDocument)
        .where(DischargeDocument.patient_id == patient_id)
        .order_by(DischargeDocument.id.desc())
    )
    return documents_out(doc) if doc else None

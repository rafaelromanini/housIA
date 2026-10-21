from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import log_action
from app.auth.deps import get_current_user
from app.db.models import MedrecRun, User
from app.db.session import get_db
from app.fhir_client.client import FhirSource, get_fhir
from app.llm.client import LLMError
from app.medrec.service import run_medrec, run_out
from app.planos.service import exige_modulo

router = APIRouter(tags=["reconciliação medicamentosa"], dependencies=[Depends(exige_modulo("reconciliacao"))])


class MedrecBody(BaseModel):
    origem: str | None = Field(None, alias="from", description="Encounter do setor de origem ou nome do setor")
    destino: str | None = Field(None, alias="to", description="Encounter do setor de destino ou nome do setor")

    model_config = {"populate_by_name": True}


@router.post("/patients/{patient_id}/medrec")
def create_medrec(
    patient_id: str,
    body: MedrecBody,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    user: User = Depends(get_current_user),
) -> dict:
    """Reconcilia a prescrição de origem com a de destino (padrão: setor anterior → setor atual)."""
    try:
        return run_medrec(db, fhir, user, patient_id, body.origem, body.destino)
    except LLMError as exc:
        db.rollback()
        log_action(db, user, "medrec.erro_llm", "patient", patient_id, {"erro": str(exc)})
        raise HTTPException(502, str(exc)) from exc


@router.get("/patients/{patient_id}/medrec/latest")
def latest_medrec(
    patient_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict | None:
    run = db.scalar(select(MedrecRun).where(MedrecRun.patient_id == patient_id).order_by(MedrecRun.id.desc()))
    if run is None:
        return None
    log_action(db, user, "medrec.visualizada", "medrec_run", run.id, {"patient_id": patient_id})
    return run_out(run)

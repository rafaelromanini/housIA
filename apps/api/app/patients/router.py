from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.fhir_client.client import FhirSource, get_fhir
from app.patients.service import alert_counts, bed_panel, latest_discharge_risk, load_record
from app.seed.scenarios import scenario_meta
from app.timeline.context import build_context
from app.timeline.events import KINDS, ClinicalEvent, build_timeline

router = APIRouter(prefix="/patients", tags=["pacientes"])


@router.get("")
def list_patients(
    db: Session = Depends(get_db), fhir: FhirSource = Depends(get_fhir), _: User = Depends(get_current_user)
) -> list[dict]:
    """Painel de leitos: pacientes internados com setor, leito, dias de internação, alertas e risco de alta."""
    return bed_panel(fhir, db)


@router.get("/{patient_id}")
def get_patient(
    patient_id: str,
    db: Session = Depends(get_db),
    fhir: FhirSource = Depends(get_fhir),
    _: User = Depends(get_current_user),
) -> dict:
    record = load_record(fhir, patient_id)
    sector = record.current_sector
    ctx = build_context(record)
    return {
        "id": record.id,
        "nome": record.name,
        "idade": record.age,
        "sexo": record.sex,
        "setor": sector.ward if sector else None,
        "leito": sector.bed if sector else None,
        "internado_em": record.admission_start.isoformat() if record.admission_start else None,
        "dias_internacao": record.length_of_stay_days,
        "tipo_internacao": "urgência" if record.admission_urgent else "eletiva",
        "motivo": record.admission_reason,
        "encounter_id": record.hospitalization["id"] if record.hospitalization else None,
        "setores": [
            {
                "encounter_id": s.encounter_id,
                "setor": s.ward,
                "leito": s.bed,
                "inicio": s.start.isoformat() if s.start else None,
                "fim": s.end.isoformat() if s.end else None,
            }
            for s in record.sectors
        ],
        "atendimentos_urgencia_anteriores": [
            {
                "source_ref": f"Encounter/{e['id']}",
                "inicio": e["period"]["start"],
                "motivo": ((e.get("reasonCode") or [{}])[0]).get("text", ""),
            }
            for e in record.previous_emergency_visits
        ],
        "contexto": ctx.model_dump(),
        "alertas_abertos": alert_counts(db).get(record.id, {"critico": 0, "atencao": 0, "info": 0}),
        "risco_alta": latest_discharge_risk(db).get(record.id),
        "cenario": scenario_meta().get(record.id) if record.scenario_tag else None,
    }


@router.get("/{patient_id}/timeline", response_model=list[ClinicalEvent])
def get_timeline(
    patient_id: str,
    kinds: str | None = Query(None, description=f"Filtro por tipo, separado por vírgula: {', '.join(KINDS)}"),
    since: datetime | None = None,
    fhir: FhirSource = Depends(get_fhir),
    _: User = Depends(get_current_user),
) -> list[ClinicalEvent]:
    """Eventos em ordem cronológica crescente; cada um traz `source_ref` (recurso FHIR de origem)."""
    record = load_record(fhir, patient_id)
    events = build_timeline(record)
    if kinds:
        wanted = {k.strip() for k in kinds.split(",") if k.strip()}
        events = [e for e in events if e.kind in wanted]
    if since:
        events = [e for e in events if e.timestamp >= since]
    return events

from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.db.models import Alert, DischargeAssessment
from app.fhir_client import codes
from app.fhir_client.client import FhirSource
from app.planos import service as planos
from app.seed.scenarios import scenario_meta
from app.timeline.record import PatientRecord, parse_dt, ref_id


def load_record(fhir: FhirSource, patient_id: str) -> PatientRecord:
    record = PatientRecord.load(fhir, patient_id)
    if record is None:
        raise HTTPException(404, "Paciente não encontrado")
    return record


def _age(birth: str | None, today: date) -> int | None:
    if not birth:
        return None
    b = date.fromisoformat(birth)
    return today.year - b.year - ((today.month, today.day) < (b.month, b.day))


def _scenario_tag(patient: dict) -> str | None:
    for tag in patient.get("meta", {}).get("tag", []):
        if tag.get("system") == codes.TAGS and tag.get("code", "").startswith("cenario"):
            return tag["code"]
    return None


def alert_counts(db: Session) -> dict[str, dict[str, int]]:
    rows = db.execute(
        select(Alert.patient_id, Alert.severity, func.count())
        .where(Alert.status == "aberto")
        .group_by(Alert.patient_id, Alert.severity)
    ).all()
    out: dict[str, dict[str, int]] = {}
    for pid, sev, n in rows:
        out.setdefault(pid, {"critico": 0, "atencao": 0, "info": 0})[sev] = n
    return out


def latest_discharge_risk(db: Session) -> dict[str, str]:
    if not planos.tem_modulo(db, "alta"):  # plano sem o módulo de alta não expõe o risco antigo (#35)
        return {}
    sub = (
        select(DischargeAssessment.patient_id, func.max(DischargeAssessment.id).label("mid"))
        .group_by(DischargeAssessment.patient_id)
        .subquery()
    )
    rows = db.execute(
        select(DischargeAssessment.patient_id, DischargeAssessment.final_risk).join(
            sub, DischargeAssessment.id == sub.c.mid
        )
    ).all()
    return {pid: risk for pid, risk in rows}


def bed_panel(fhir: FhirSource, db: Session) -> list[dict]:
    """Linhas do painel de leitos: só pacientes com setor ativo (sub-encounter em andamento)."""
    now = clock.now()
    locations = fhir.locations()
    patients = {p["id"]: p for p in fhir.list_patients()}
    encounters = fhir.active_encounters()
    hosp_by_id = {e["id"]: e for e in encounters if not e.get("partOf")}
    alerts = alert_counts(db)
    risks = latest_discharge_risk(db)
    scenarios = scenario_meta()
    rows = []
    for enc in encounters:
        parent_id = ref_id(enc.get("partOf"))
        if not parent_id:
            continue
        pid = ref_id(enc.get("subject"))
        patient = patients.get(pid or "")
        if patient is None:
            continue
        loc_entry = (enc.get("location") or [{}])[0]
        bed_loc = locations.get(ref_id(loc_entry.get("location")) or "", {})
        ward_loc = locations.get(ref_id(bed_loc.get("partOf")) or "", {})
        hosp = hosp_by_id.get(parent_id) or fhir.get("Encounter", parent_id) or {}
        start = parse_dt(hosp.get("period", {}).get("start"))
        sector_start = parse_dt(enc.get("period", {}).get("start"))
        tag = _scenario_tag(patient)
        name = (patient.get("name") or [{}])[0]
        rows.append(
            {
                "id": pid,
                "nome": name.get("text") or " ".join(name.get("given", []) + [name.get("family", "")]),
                "idade": _age(patient.get("birthDate"), now.date()),
                "sexo": {"male": "M", "female": "F"}.get(patient.get("gender", ""), "—"),
                "setor": ward_loc.get("name") or enc.get("serviceType", {}).get("text", "—"),
                "leito": bed_loc.get("name", "").replace("Leito ", ""),
                "internado_em": start.isoformat() if start else None,
                "dias_internacao": (now - start).days if start else 0,
                "horas_no_setor": int((now - sector_start).total_seconds() // 3600) if sector_start else None,
                "motivo": ((hosp.get("reasonCode") or [{}])[0]).get("text", ""),
                "alertas_abertos": alerts.get(pid, {"critico": 0, "atencao": 0, "info": 0}),
                "risco_alta": risks.get(pid),
                "cenario": scenarios.get(pid, {}).get("titulo") if tag else None,
            }
        )
    rows.sort(key=lambda r: (r["setor"], r["leito"]))
    return rows

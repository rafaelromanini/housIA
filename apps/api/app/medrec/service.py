"""Pilar 3 — orquestra regras determinísticas + hipóteses da IA e persiste em medrec_runs."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import clock
from app.alerts.service import upsert_alert
from app.audit.service import log_action
from app.config import get_settings
from app.db.models import MedrecRun, User
from app.fhir_client.client import FhirSource
from app.llm.client import generate_structured
from app.llm.pseudonymize import Pseudonymizer
from app.medrec.hypotheses import MedrecLLMOutput, detect_symptoms, mock_hypotheses
from app.medrec.rules import SEV_ORDER, default_transition, proposal_review, resolve_sector, run_rules
from app.patients.service import load_record
from app.timeline.events import build_timeline, window
from app.timeline.record import Sector

PROMPT = "medrec"


def _sector_out(s: Sector | None) -> dict | None:
    if s is None:
        return None
    return {
        "encounter_id": s.encounter_id,
        "setor": s.ward,
        "leito": s.bed,
        "inicio": s.start.isoformat() if s.start else None,
        "fim": s.end.isoformat() if s.end else None,
    }


def _local(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(get_settings().timezone)).strftime("%d/%m %H:%M")


def run_out(run: MedrecRun) -> dict:
    return {"id": run.id, "patient_id": run.patient_id, "created_at": run.created_at.isoformat(), **run.findings_json}


def run_medrec(
    db: Session, fhir: FhirSource, user: User, patient_id: str, origin_key: str | None, dest_key: str | None
) -> dict:
    now = clock.now()
    record = load_record(fhir, patient_id)
    default_origin, default_dest = default_transition(record)
    dest = resolve_sector(record, dest_key) if dest_key else default_dest
    origin = resolve_sector(record, origin_key) if origin_key else (default_origin if not dest_key else None)
    if dest is None or (origin_key and origin is None):
        raise HTTPException(422, "Origem/destino não encontrados entre os setores desta internação.")
    if origin is not None and origin.encounter_id == dest.encounter_id:
        origin = None

    rows, findings = run_rules(record, origin, dest)
    proposals, proposal_findings = proposal_review(
        record, [m for m in record.meds_of_encounter(dest.encounter_id) if m.status == "active"]
    )
    findings = sorted(findings + proposal_findings, key=lambda f: (SEV_ORDER[f.severidade], f.tipo))
    events = build_timeline(record)
    symptoms = detect_symptoms(events, now)
    meds_by_ref = {m.ref: m for m in record.medications}
    transfer_time = dest.start if origin else None

    # ---------- camada de IA ----------
    hypotheses: list[dict] = []
    dropped: list[dict] = []
    llm_meta = {"modo": None, "modelo": None, "prompt_version": None}
    relevant_rows = [r for r in rows if r.status in ("omitida", "suspensa", "nova", "alterada")]
    if symptoms and (relevant_rows or any(f.tipo == "interacao" for f in findings)):
        pseudo = Pseudonymizer(**record.identifiers_for_pseudonymization())
        notes = [e for e in window(events, now, 48) if e.kind == "note"]
        # última administração registrada de cada medicamento envolvido na transição
        ingredient_of = {m.ref: m.ingredient for m in record.medications}
        involved = {r.principio_ativo for r in relevant_rows} | {
            ing for f in findings if f.tipo == "interacao" for ing in f.medicamentos
        }
        last_admin = {}
        for e in events:
            ing = ingredient_of.get((e.value or {}).get("pedido") or "")
            if e.kind == "med_admin" and ing in involved and e.timestamp <= now:
                last_admin[ing] = e
        variables = pseudo.apply(
            {
                "transicao": {
                    "origem": _sector_out(origin),
                    "destino": _sector_out(dest),
                    "horario_transferencia": _local(transfer_time) if transfer_time else None,
                },
                "linhas": [
                    r.model_dump(exclude={"origem", "destino"})
                    | {
                        "origem": r.origem.texto if r.origem else None,
                        "destino": r.destino.texto if r.destino else None,
                    }
                    for r in relevant_rows
                ],
                "achados": [f.model_dump() for f in findings if f.tipo != "alto_alerta"],
                "sinais": [
                    {"sinal": s.sinal, "registro": s.texto, "ref": s.ref, "horario": _local(s.timestamp)}
                    for s in symptoms
                ],
                "evolucoes": [{"ref": n.source_ref, "horario": _local(n.timestamp), "texto": n.text} for n in notes],
                "ultimas_administracoes": [
                    {"medicamento": ing, "ref": e.source_ref, "horario": _local(e.timestamp), "registro": e.summary}
                    for ing, e in last_admin.items()
                ],
            }
        )
        result = generate_structured(
            PROMPT,
            variables,
            MedrecLLMOutput,
            mock=lambda _v: mock_hypotheses(rows, findings, symptoms, meds_by_ref, transfer_time),
        )
        valid = (
            {s.ref for s in symptoms}
            | {e.source_ref for e in last_admin.values()}
            | {r for row in rows for r in row.refs}
            | {r for f in findings for r in f.refs}
            | {n.source_ref for n in notes}
        )
        for h in result.output.hipoteses:
            invalid = [r for r in h.refs if r not in valid]
            if not h.refs or invalid:
                dropped.append({"texto": h.texto, "refs_invalidas": invalid})
            else:
                hypotheses.append(h.model_dump())
        llm_meta = {"modo": result.mode, "modelo": result.model, "prompt_version": result.prompt_version}

    findings_out = []
    claimed: set[int] = set()
    for f in findings:
        item = f.model_dump()
        if f.severidade in ("critico", "atencao"):
            alert = upsert_alert(
                db,
                patient_id=patient_id,
                source="medrec",
                severity=f.severidade,
                title=f.titulo,
                detail=f.detalhe,
                refs=f.refs,
                claimed=claimed,
            )
            item["alert_id"] = alert.id
        findings_out.append(item)

    payload = {
        "origem": _sector_out(origin),
        "destino": _sector_out(dest),
        "linhas": [r.model_dump() for r in rows],
        "prescricoes_propostas": proposals,
        "achados": findings_out,
        "hipoteses": hypotheses,
        "sinais_detectados": [s.model_dump(mode="json") for s in symptoms],
        "meta": {
            **llm_meta,
            "gerado_em": now.isoformat(),
            "descartados": dropped,
            "aviso": "Tabelas de interação e de MPP são uma base curada de DEMONSTRAÇÃO, não exaustiva.",
        },
    }
    run = MedrecRun(
        patient_id=patient_id,
        from_encounter_or_location=origin.encounter_id if origin else "-",
        to_encounter_or_location=dest.encounter_id,
        findings_json=payload,
        created_by=user.id,
        created_at=now,
    )
    db.add(run)
    db.flush()
    log_action(
        db,
        user,
        "medrec.executada",
        "medrec_run",
        run.id,
        {
            "patient_id": patient_id,
            "origem": run.from_encounter_or_location,
            "destino": run.to_encounter_or_location,
            "criticos": sum(f.severidade == "critico" for f in findings),
            "hipoteses": len(hypotheses),
            "hipoteses_descartadas": len(dropped),
            **llm_meta,
        },
        commit=False,
    )
    db.commit()
    return run_out(run)

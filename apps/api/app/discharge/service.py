"""Pilar 2 — "Waze" da alta: score de reinternação, sugestões e trava suave com override auditado."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.alerts.service import upsert_alert
from app.audit.service import log_action
from app.config import get_settings
from app.consult import idiomas
from app.db.models import Alert, DischargeAssessment, DischargeDocument, DischargeOverride, User
from app.discharge.documents import DischargeDocsOutput, home_meds, mock_documents
from app.discharge.scoring import SocialChecklist, final_risk, lace, lace_band, social_band, social_score
from app.discharge.suggestions import DischargeLLMOutput, mock_suggestions
from app.fhir_client.client import FhirSource
from app.llm.client import generate_structured
from app.llm.pseudonymize import Pseudonymizer
from app.patients.service import load_record
from app.reference import social_weights
from app.timeline.context import build_context
from app.timeline.events import build_timeline
from app.timeline.record import MedInfo, PatientRecord

PROMPT = "discharge"
MIN_JUSTIFICATION = 20
ALERT_TITLE = "Alta de alto risco de reinternação: exige reavaliação ou justificativa registrada"


def _new_meds(record: PatientRecord) -> list[MedInfo]:
    """Medicamentos iniciados durante a internação (não na admissão) sem uso prévio do mesmo princípio ativo."""
    start = record.admission_start
    if start is None:
        return []
    out = []
    for m in record.active_meds:
        if not m.start or m.start < start + timedelta(hours=12):
            continue
        earlier = [o for o in record.medications if o.ingredient == m.ingredient and o.start and o.start < m.start]
        if not earlier:
            out.append(m)
    return out


def _overrides(db: Session, assessment_id: int) -> list[dict]:
    rows = db.execute(
        select(DischargeOverride, User)
        .join(User, User.id == DischargeOverride.user_id)
        .where(DischargeOverride.assessment_id == assessment_id)
        .order_by(DischargeOverride.id)
    ).all()
    return [
        {
            "id": o.id,
            "usuario": u.name,
            "papel": u.role,
            "justificativa": o.justification,
            "created_at": o.created_at.isoformat(),
        }
        for o, u in rows
    ]


def assessment_out(db: Session, a: DischargeAssessment) -> dict:
    overrides = _overrides(db, a.id)
    locked = a.final_risk == "alto" and not overrides
    confirmed_by = db.get(User, a.confirmed_by) if a.confirmed_by else None
    extra = a.suggestions_json or {}
    return {
        "id": a.id,
        "patient_id": a.patient_id,
        "encounter_id": a.encounter_id,
        "created_at": a.created_at.isoformat(),
        "checklist": a.social_checklist_json,
        "lace": {
            "score": a.lace_score,
            "banda": a.lace_components_json.get("banda"),
            "componentes": a.lace_components_json.get("componentes"),
        },
        "social": {
            "score": a.social_score,
            "banda": extra.get("social_banda"),
            "itens": extra.get("social_itens", []),
            "aviso": social_weights()["_aviso"],
            "faixas": social_weights()["faixas"],
        },
        "final_risk": a.final_risk,
        "explicacao": extra.get("explicacao"),
        "sugestoes": extra.get("sugestoes", []),
        "descartados": extra.get("descartados", []),
        "meta": extra.get("meta", {}),
        "trava": {
            "ativa": locked and a.confirmed_at is None,
            "motivo": "Risco ALTO: aplique as sugestões e reavalie, ou registre uma justificativa (override)."
            if locked
            else None,
        },
        "overrides": overrides,
        "confirmada": {"em": a.confirmed_at.isoformat(), "por": confirmed_by.name if confirmed_by else None}
        if a.confirmed_at
        else None,
    }


def create_assessment(db: Session, fhir: FhirSource, user: User, patient_id: str, checklist: SocialChecklist) -> dict:
    now = clock.now()
    record = load_record(fhir, patient_id)
    record.now = now
    lace_score, components = lace(record)
    social, social_items = social_score(checklist)
    risk, explanation = final_risk(lace_score, social)
    meds = record.active_meds
    new_meds = _new_meds(record)
    ctx = build_context(record)
    pending = record.pending_exams

    pseudo = Pseudonymizer(**record.identifiers_for_pseudonymization())
    variables = pseudo.apply(
        {
            "risco": {
                "final": risk,
                "explicacao": explanation,
                "lace": lace_score,
                "lace_componentes": components,
                "social": social,
                "itens_sociais_de_risco": social_items,
            },
            "checklist": {k: v for k, v in checklist.model_dump().items() if v is not None}
            | {"refs_disponiveis": [f"Checklist/{k}" for k in checklist.model_dump()]},
            "contexto": {
                "idade": record.age,
                "motivo_internacao": record.admission_reason,
                "diagnosticos": [{"ref": d.source_ref, "texto": d.summary} for d in ctx.diagnosticos],
                "medicamentos_ativos": [{"ref": m.ref, "texto": m.text} for m in meds],
                "iniciados_nesta_internacao": [{"ref": m.ref, "texto": m.text} for m in new_meds],
                "exames_pendentes": [
                    {"ref": e.ref, "texto": e.texto, "proposto_no_relatorio_cirurgico": e.proposto} for e in pending
                ],
            },
        }
    )
    result = generate_structured(
        PROMPT,
        variables,
        DischargeLLMOutput,
        mock=lambda _v: mock_suggestions(checklist.model_dump(), components, social_items, meds, new_meds, pending),
    )
    valid = {f"Checklist/{k}" for k in checklist.model_dump()} | {m.ref for m in record.medications} | ctx.refs()
    valid |= {e.ref for e in pending}
    valid |= {r for comp in components.values() for r in comp.get("refs", [])}
    suggestions, dropped = [], []
    for s in result.output.sugestoes:
        invalid = [r for r in s.refs if r not in valid]
        (dropped if not s.refs or invalid else suggestions).append(
            s.model_dump() | ({"refs_invalidas": invalid} if invalid or not s.refs else {})
        )

    assessment = DischargeAssessment(
        patient_id=patient_id,
        encounter_id=record.hospitalization["id"] if record.hospitalization else None,
        social_checklist_json=checklist.model_dump(),
        lace_components_json={"componentes": components, "banda": lace_band(lace_score)},
        lace_score=lace_score,
        social_score=social,
        final_risk=risk,
        suggestions_json={
            "sugestoes": suggestions,
            "descartados": dropped,
            "explicacao": explanation,
            "social_itens": social_items,
            "social_banda": social_band(social),
            "meta": {"modo": result.mode, "modelo": result.model, "prompt_version": result.prompt_version},
        },
        created_by=user.id,
        created_at=now,
    )
    db.add(assessment)
    db.flush()
    if risk == "alto":
        upsert_alert(
            db,
            patient_id=patient_id,
            source="discharge",
            severity="critico",
            title=ALERT_TITLE,
            detail=explanation,
            refs=[r for comp in components.values() for r in comp.get("refs", [])][:6],
        )
    log_action(
        db,
        user,
        "discharge.avaliacao",
        "discharge_assessment",
        assessment.id,
        {
            "patient_id": patient_id,
            "lace": lace_score,
            "social": social,
            "risco": risk,
            "sugestoes": len(suggestions),
            "sugestoes_descartadas": len(dropped),
            "modelo": result.model,
        },
        commit=False,
    )
    db.commit()
    return assessment_out(db, assessment)


def _get(db: Session, assessment_id: int) -> DischargeAssessment:
    a = db.get(DischargeAssessment, assessment_id)
    if a is None:
        raise HTTPException(404, "Avaliação de alta não encontrada")
    return a


def register_override(db: Session, user: User, assessment_id: int, justification: str) -> dict:
    a = _get(db, assessment_id)
    text = justification.strip()
    if len(text) < MIN_JUSTIFICATION:
        raise HTTPException(422, f"A justificativa precisa ter pelo menos {MIN_JUSTIFICATION} caracteres.")
    if a.final_risk != "alto":
        raise HTTPException(409, "Override só se aplica a avaliações de risco alto.")
    override = DischargeOverride(assessment_id=a.id, user_id=user.id, justification=text, created_at=clock.now())
    db.add(override)
    db.flush()
    for alert in db.scalars(
        select(Alert).where(Alert.patient_id == a.patient_id, Alert.source == "discharge", Alert.status == "aberto")
    ):
        alert.status, alert.acted_by, alert.acted_at = "reconhecido", user.id, clock.now()
    log_action(
        db,
        user,
        "discharge.override",
        "discharge_assessment",
        a.id,
        {
            "patient_id": a.patient_id,
            "override_id": override.id,
            "justificativa": text,
            "risco": a.final_risk,
            "lace": a.lace_score,
            "social": a.social_score,
        },
        commit=False,
    )
    db.commit()
    return assessment_out(db, a)


def confirm_discharge(db: Session, user: User, assessment_id: int) -> dict:
    a = _get(db, assessment_id)
    if a.confirmed_at:
        return assessment_out(db, a)
    if a.final_risk == "alto" and not _overrides(db, a.id):
        log_action(
            db,
            user,
            "discharge.confirmacao_bloqueada",
            "discharge_assessment",
            a.id,
            {"patient_id": a.patient_id, "risco": a.final_risk},
        )
        raise HTTPException(
            409, "Alta travada: risco ALTO. Aplique as sugestões e reavalie, ou registre um override com justificativa."
        )
    a.confirmed_at, a.confirmed_by = clock.now(), user.id
    log_action(
        db,
        user,
        "discharge.confirmada",
        "discharge_assessment",
        a.id,
        {"patient_id": a.patient_id, "risco": a.final_risk, "com_override": a.final_risk == "alto"},
        commit=False,
    )
    db.commit()
    return assessment_out(db, a)


def _local(dt) -> str:
    return dt.astimezone(ZoneInfo(get_settings().timezone)).strftime("%d/%m %H:%M")


def documents_out(doc: DischargeDocument) -> dict:
    return {
        "id": doc.id,
        "patient_id": doc.patient_id,
        "assessment_id": doc.assessment_id,
        "idioma": doc.language,
        "created_at": doc.created_at.isoformat(),
        "model": doc.model,
        "prompt_version": doc.prompt_version,
        **doc.content_json,
    }


def implant_card(record) -> list[dict]:
    """Cartão do implante (rastreio): vem do Device FHIR registrado na cirurgia, nunca do LLM."""
    return [
        {
            "descricao": i.descricao,
            "lote": i.lote,
            "serie": i.serie,
            "validade": i.validade,
            "registro_anvisa": i.registro_anvisa,
            "gtin": i.gtin,
            "procedimento": i.procedimento,
            "colocado_em": i.colocado_em.isoformat() if i.colocado_em else None,
            "refs": i.refs,
        }
        for i in record.implants
    ]


def generate_documents(db: Session, fhir: FhirSource, user: User, patient_id: str, language: str) -> dict:
    now = clock.now()
    record = load_record(fhir, patient_id)
    record.now = now
    events = [e for e in build_timeline(record) if e.timestamp <= now]
    latest = db.scalar(
        select(DischargeAssessment)
        .where(DischargeAssessment.patient_id == patient_id)
        .order_by(DischargeAssessment.id.desc())
    )
    assessment = assessment_out(db, latest) if latest else None
    meds = home_meds(record)
    ctx = build_context(record)

    recent = [e for e in events if e.timestamp >= now - timedelta(hours=24) and e.kind in ("vital", "lab", "note")]
    pseudo = Pseudonymizer(**record.identifiers_for_pseudonymization())
    variables = pseudo.apply(
        {
            "internacao": {
                "ref": f"Encounter/{record.hospitalization['id']}" if record.hospitalization else None,
                "idade": record.age,
                "sexo": record.sex,
                "motivo": record.admission_reason,
                "dias": record.length_of_stay_days,
                "diagnosticos": [{"ref": d.source_ref, "texto": d.summary} for d in ctx.diagnosticos],
                "alergias": [{"ref": a.source_ref, "texto": a.summary} for a in ctx.alergias]
                or "sem alergias registradas",
                "procedimentos": [{"ref": e.source_ref, "texto": e.summary} for e in events if e.kind == "procedure"],
            },
            "registros": [
                {
                    "ref": e.source_ref,
                    "horario": _local(e.timestamp),
                    "tipo": e.kind,
                    "resumo": e.summary,
                    **({"texto": e.text} if e.text else {}),
                }
                for e in recent
            ],
            "medicamentos": [{"ref": m.ref, "texto": m.text, "classe": m.drug_class} for m in meds],
            "avaliacao": {
                "risco": assessment["final_risk"],
                "lace": assessment["lace"]["score"],
                "social": assessment["social"]["score"],
                "checklist": assessment["checklist"],
                "sugestoes": [{"texto": s["texto"], "refs": s["refs"]} for s in assessment["sugestoes"]],
            }
            if assessment
            else "sem avaliação de alta registrada",
        }
    ) | {"idioma": f"{idiomas.rotulo(language)} ({language})"}
    result = generate_structured(
        "discharge_docs",
        variables,
        DischargeDocsOutput,
        mock=lambda _v: mock_documents(record, events, assessment),
    )

    valid = {e.source_ref for e in events} | ctx.refs() | {m.ref for m in record.medications}
    valid |= {f"Checklist/{k}" for k in (assessment or {}).get("checklist", {})}
    if record.hospitalization:
        valid.add(f"Encounter/{record.hospitalization['id']}")
    dropped: list[dict] = []
    content = result.output.model_dump()

    def clean_section(section: dict, where: str) -> dict:
        bad = [r for r in section["refs"] if r not in valid]
        if bad:
            dropped.append({"onde": where, "refs_invalidas": bad})
        section["refs"] = [r for r in section["refs"] if r in valid]
        section["sem_fonte"] = not section["refs"]
        return section

    for key in ("subjetivo", "objetivo", "avaliacao", "plano"):
        clean_section(content["soap"][key], f"soap.{key}")
    ori = content["orientacoes"]
    clean_section(ori["resumo_internacao"], "orientacoes.resumo_internacao")
    clean_section(ori["retorno"], "orientacoes.retorno")
    for key in ("medicamentos", "sinais_de_alerta", "cuidados_em_casa"):
        kept = []
        for item in ori[key]:
            bad = [r for r in item["refs"] if r not in valid]
            if bad or not item["refs"]:
                dropped.append(
                    {
                        "onde": f"orientacoes.{key}",
                        "texto": item.get("texto") or item.get("medicamento"),
                        "refs_invalidas": bad,
                    }
                )
            else:
                kept.append(item)
        ori[key] = kept
    content["descartados"] = dropped
    content["cartao_implante"] = implant_card(record)
    content["meta"] = {
        "modo": result.mode,
        "gerado_em": now.isoformat(),
        "traducao_disponivel": result.mode == "real" or language == "pt-BR",
    }

    doc = DischargeDocument(
        patient_id=patient_id,
        assessment_id=latest.id if latest else None,
        language=language,
        content_json=content,
        model=result.model,
        prompt_version=result.prompt_version,
        created_by=user.id,
        created_at=now,
    )
    db.add(doc)
    db.flush()
    log_action(
        db,
        user,
        "discharge.documentos",
        "discharge_document",
        doc.id,
        {
            "patient_id": patient_id,
            "idioma": language,
            "modelo": result.model,
            "itens_descartados": len(dropped),
        },
        commit=False,
    )
    db.commit()
    return documents_out(doc)

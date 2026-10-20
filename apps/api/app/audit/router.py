import csv
import io
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.audit import impact
from app.audit.service import log_action
from app.auth.deps import require_roles
from app.config import get_settings
from app.consult import knowledge
from app.db.models import (
    AIFeedback,
    Alert,
    AuditLog,
    DischargeAssessment,
    DischargeOverride,
    HandoffReport,
    ImpactBaseline,
    MedrecRun,
    User,
)
from app.db.session import get_db

router = APIRouter(prefix="/audit", tags=["auditoria"])
gestor = require_roles("gestor")


def _range(since: date | None, until: date | None) -> tuple[datetime, datetime]:
    """Datas do filtro são dias no fuso do hospital (America/Sao_Paulo por padrão)."""
    tz = ZoneInfo(get_settings().timezone)
    end = datetime.combine(until, time.max, tzinfo=tz) if until else clock.now() + timedelta(minutes=1)
    start = datetime.combine(since, time.min, tzinfo=tz) if since else end - timedelta(days=30)
    return start.astimezone(UTC), end.astimezone(UTC)


@router.get("")
def list_audit(
    action: str | None = Query(None, description="Prefixo da ação, ex.: discharge. ou handoff.gerado"),
    patient_id: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: int = Query(100, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
    _: User = Depends(gestor),
) -> dict:
    start, end = _range(since, until)
    q = (
        select(AuditLog, User)
        .outerjoin(User, User.id == AuditLog.user_id)
        .where(AuditLog.created_at >= start, AuditLog.created_at <= end)
        .order_by(AuditLog.id.desc())
    )
    if action:
        q = q.where(AuditLog.action.startswith(action))
    rows = db.execute(q).all()
    if patient_id:
        rows = [
            (a, u)
            for a, u in rows
            if (a.payload_json or {}).get("patient_id") == patient_id
            or (a.entity == "patient" and a.entity_id == patient_id)
        ]
    return {"total": len(rows), "items": [_entry(a, u) for a, u in rows[offset : offset + limit]]}


def _entry(a: AuditLog, u: User | None) -> dict:
    return {
        "id": a.id,
        "created_at": a.created_at.isoformat(),
        "user": {"id": u.id, "name": u.name, "role": u.role} if u else None,
        "action": a.action,
        "entity": a.entity,
        "entity_id": a.entity_id,
        "payload": a.payload_json,
    }


@router.get("/metrics")
def metrics(
    since: date | None = None,
    until: date | None = None,
    uso_regular_min: int = Query(impact.USO_REGULAR_MIN, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(gestor),
) -> dict:
    start, end = _range(since, until)

    def count(model, *conds) -> int:
        return db.scalar(select(func.count()).select_from(model).where(*conds)) or 0

    alerts_in = (Alert.created_at >= start, Alert.created_at <= end)
    status_rows = db.execute(select(Alert.status, func.count()).where(*alerts_in).group_by(Alert.status)).all()
    source_rows = db.execute(
        select(Alert.source, func.count()).where(*alerts_in, Alert.severity == "critico").group_by(Alert.source)
    ).all()
    dropped = 0
    for (payload,) in db.execute(
        select(AuditLog.payload_json).where(
            AuditLog.action == "handoff.gerado", AuditLog.created_at >= start, AuditLog.created_at <= end
        )
    ):
        dropped += int((payload or {}).get("itens_descartados", 0))
    feedback: dict[str, dict[str, int]] = {}
    for kind, useful, n in db.execute(
        select(AIFeedback.kind, AIFeedback.useful, func.count())
        .where(AIFeedback.created_at >= start, AIFeedback.created_at <= end, AIFeedback.kind != "nps")
        .group_by(AIFeedback.kind, AIFeedback.useful)
    ):
        feedback.setdefault(kind, {"util": 0, "nao_util": 0})["util" if useful else "nao_util"] += n
    total_useful = sum(v["util"] for v in feedback.values())
    total_feedback = total_useful + sum(v["nao_util"] for v in feedback.values())
    statuses = {s: n for s, n in status_rows}
    acted = statuses.get("reconhecido", 0) + statuses.get("descartado", 0)
    return {
        "periodo": {"inicio": start.isoformat(), "fim": end.isoformat()},
        "feedback_ia": feedback,
        "taxa_utilidade_ia": round(total_useful / total_feedback, 3) if total_feedback else None,
        "taxa_aceitacao_alertas": round(statuses.get("reconhecido", 0) / acted, 3) if acted else None,
        "alertas_criticos": count(Alert, *alerts_in, Alert.severity == "critico"),
        "alertas_criticos_por_origem": {s: n for s, n in source_rows},
        "alertas_por_status": statuses,
        "overrides": count(
            DischargeOverride, DischargeOverride.created_at >= start, DischargeOverride.created_at <= end
        ),
        "altas_alto_risco": count(
            DischargeAssessment,
            DischargeAssessment.final_risk == "alto",
            DischargeAssessment.created_at >= start,
            DischargeAssessment.created_at <= end,
        ),
        "altas_confirmadas": count(
            DischargeAssessment,
            DischargeAssessment.confirmed_at.is_not(None),
            DischargeAssessment.confirmed_at >= start,
            DischargeAssessment.confirmed_at <= end,
        ),
        "passagens_geradas": count(HandoffReport, HandoffReport.created_at >= start, HandoffReport.created_at <= end),
        "passagens_do_cache": count(
            AuditLog, AuditLog.action == "handoff.cache_hit", AuditLog.created_at >= start, AuditLog.created_at <= end
        ),
        "reconciliacoes": count(MedrecRun, MedrecRun.created_at >= start, MedrecRun.created_at <= end),
        "itens_ia_descartados_sem_fonte": dropped,
        "consulta_ia": impact.public(impact.compute(db, start, end, uso_regular_min)),
    }


def _report(db: Session, since: date | None, until: date | None, uso_regular_min: int = impact.USO_REGULAR_MIN) -> dict:
    start, end = _range(since, until)
    return impact.compute(db, start, end, uso_regular_min)


@router.get("/metrics/export.csv")
def metrics_csv(
    since: date | None = None, until: date | None = None, db: Session = Depends(get_db), _: User = Depends(gestor)
) -> Response:
    """Painel Impacto em CSV: uma linha por métrica, com a fórmula e o número de linhas do audit_log que a compõem."""
    out = io.StringIO()
    fields = ["metrica", "rotulo", "valor", "unidade", "numerador", "denominador", "linhas_audit", "formula"]
    writer = csv.DictWriter(out, fieldnames=fields, delimiter=";")
    writer.writeheader()
    writer.writerows(impact.flat_rows(_report(db, since, until)))
    return Response(
        "\ufeff" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="impacto-housia.csv"'},
    )


@router.get("/metrics/measure-report")
def metrics_measure_report(
    since: date | None = None, until: date | None = None, db: Session = Depends(get_db), _: User = Depends(gestor)
) -> dict:
    """Painel Impacto como FHIR R4 MeasureReport (summary)."""
    return impact.measure_report(_report(db, since, until), clock.now())


@router.get("/metrics/{metrica}/linhas")
def metric_lines(
    metrica: str,
    since: date | None = None,
    until: date | None = None,
    uso_regular_min: int = Query(impact.USO_REGULAR_MIN, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(gestor),
) -> dict:
    """Drill-down: as linhas do audit_log que compõem o número de uma métrica do painel Impacto."""
    report = _report(db, since, until, uso_regular_min)
    m = report["metricas"].get(metrica)
    if m is None:
        raise HTTPException(404, f"Métrica desconhecida. Use uma de: {', '.join(report['metricas'])}")
    rows = impact.lines(db, report, metrica)
    return {
        "metrica": metrica,
        "rotulo": m["rotulo"],
        "valor": m["valor"],
        "unidade": m["unidade"],
        "formula": m["formula"],
        "total": len(rows),
        "items": [_entry(a, u) for a, u in rows],
    }


class BaselineIn(BaseModel):
    setor: str = Field(min_length=1, max_length=60, description="Especialidade da consulta (ex.: clinica_geral)")
    minutos_antes: float = Field(
        gt=0, le=600, description="Tempo médio de documentação por atendimento antes do HousIA"
    )
    observacao: str | None = Field(None, max_length=500)


@router.get("/impact/baseline")
def get_baselines(db: Session = Depends(get_db), _: User = Depends(gestor)) -> list[dict]:
    """Linha de base vigente por setor (a mais recente) e quem informou."""
    rows = impact.baselines(db)
    users = {u.id: u.name for u in db.scalars(select(User))}
    return [
        {
            "id": b.id,
            "setor": b.sector,
            "rotulo": impact.setor_label(b.sector),
            "minutos_antes": b.minutes_before,
            "observacao": b.note,
            "por": users.get(b.created_by or 0),
            "em": b.created_at.isoformat(),
        }
        for b in rows.values()
    ]


@router.post("/impact/baseline")
def post_baseline(body: BaselineIn, db: Session = Depends(get_db), user: User = Depends(gestor)) -> dict:
    """Formulário de linha de base: "tempo médio antes do HousIA" de um setor; gera os minutos e horas devolvidas."""
    if body.setor not in knowledge.SPECIALTIES:
        raise HTTPException(422, f"Setor desconhecido. Use uma especialidade de: {', '.join(knowledge.SPECIALTIES)}")
    b = ImpactBaseline(
        sector=body.setor,
        minutes_before=body.minutos_antes,
        note=(body.observacao or "").strip() or None,
        created_by=user.id,
        created_at=clock.now(),
    )
    db.add(b)
    db.flush()
    log_action(
        db,
        user,
        "impacto.linha_de_base",
        "impact_baseline",
        b.id,
        {"setor": b.sector, "minutos_antes": b.minutes_before, "observacao": b.note},
    )
    return {"id": b.id, "setor": b.sector, "minutos_antes": b.minutes_before}

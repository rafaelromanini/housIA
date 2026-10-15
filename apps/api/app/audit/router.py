from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.auth.deps import require_roles
from app.config import get_settings
from app.db.models import Alert, AuditLog, DischargeAssessment, DischargeOverride, HandoffReport, MedrecRun, User
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
    items = [
        {
            "id": a.id,
            "created_at": a.created_at.isoformat(),
            "user": {"id": u.id, "name": u.name, "role": u.role} if u else None,
            "action": a.action,
            "entity": a.entity,
            "entity_id": a.entity_id,
            "payload": a.payload_json,
        }
        for a, u in rows[offset : offset + limit]
    ]
    return {"total": len(rows), "items": items}


@router.get("/metrics")
def metrics(
    since: date | None = None,
    until: date | None = None,
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
    return {
        "periodo": {"inicio": start.isoformat(), "fim": end.isoformat()},
        "alertas_criticos": count(Alert, *alerts_in, Alert.severity == "critico"),
        "alertas_criticos_por_origem": {s: n for s, n in source_rows},
        "alertas_por_status": {s: n for s, n in status_rows},
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
    }

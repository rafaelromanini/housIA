from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.audit.service import log_action
from app.auth.deps import get_current_user
from app.db.models import Alert, User
from app.db.session import get_db

router = APIRouter(tags=["alertas"])


class AlertStatusIn(BaseModel):
    status: Literal["reconhecido", "descartado", "aberto"]
    justificativa: str | None = None


def alert_out(a: Alert) -> dict:
    return {
        "id": a.id,
        "patient_id": a.patient_id,
        "source": a.source,
        "severity": a.severity,
        "title": a.title,
        "detail": a.detail,
        "source_refs": a.source_refs,
        "status": a.status,
        "acted_by": a.acted_by,
        "acted_at": a.acted_at.isoformat() if a.acted_at else None,
        "created_at": a.created_at.isoformat(),
    }


@router.get("/patients/{patient_id}/alerts")
def list_alerts(
    patient_id: str,
    status: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[dict]:
    q = select(Alert).where(Alert.patient_id == patient_id).order_by(Alert.id.desc())
    if status:
        q = q.where(Alert.status == status)
    return [alert_out(a) for a in db.scalars(q)]


@router.post("/alerts/{alert_id}/status")
def set_alert_status(
    alert_id: int,
    body: AlertStatusIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(404, "Alerta não encontrado")
    if body.status == "descartado" and len((body.justificativa or "").strip()) < 10:
        raise HTTPException(422, "Para descartar um alerta, registre uma justificativa (mín. 10 caracteres).")
    previous = alert.status
    alert.status = body.status
    alert.acted_by = user.id
    alert.acted_at = clock.now()
    log_action(
        db,
        user,
        f"alerta.{body.status}",
        "alert",
        alert.id,
        {
            "patient_id": alert.patient_id,
            "de": previous,
            "para": body.status,
            "justificativa": body.justificativa,
            "severidade": alert.severity,
            "titulo": alert.title,
        },
    )
    return alert_out(alert)

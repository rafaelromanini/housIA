from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.db.models import Alert


def upsert_alert(
    db: Session,
    *,
    patient_id: str,
    source: str,
    severity: str,
    title: str,
    detail: str = "",
    refs: list[str] | None = None,
) -> Alert:
    """Cria o alerta, ou devolve o aberto/reconhecido equivalente (evita duplicar a cada regeneração)."""
    title = title[:300]
    existing = db.scalar(
        select(Alert).where(
            Alert.patient_id == patient_id,
            Alert.source == source,
            Alert.title == title,
            Alert.status.in_(("aberto", "reconhecido")),
        )
    )
    if existing:
        return existing
    alert = Alert(
        patient_id=patient_id,
        source=source,
        severity=severity,
        title=title,
        detail=detail,
        source_refs=refs or [],
        status="aberto",
        created_at=clock.now(),
    )
    db.add(alert)
    db.flush()
    return alert

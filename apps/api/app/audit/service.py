from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AuditLog, User


def log_action(
    db: Session,
    user: User | None,
    action: str,
    entity: str,
    entity_id: str | int | None = None,
    payload: dict[str, Any] | None = None,
    commit: bool = True,
) -> AuditLog:
    """Registra uma ação no audit_log. Nunca grava texto clínico bruto de prompts aqui."""
    entry = AuditLog(
        user_id=user.id if user else None,
        action=action,
        entity=entity,
        entity_id=str(entity_id) if entity_id is not None else None,
        payload_json=payload or {},
    )
    db.add(entry)
    if commit:
        db.commit()
    return entry

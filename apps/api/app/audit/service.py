from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.audit import chain
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
        organization_id=user.organization_id if user else db.info.get("org_id"),
        action=action,
        entity=entity,
        entity_id=str(entity_id) if entity_id is not None else None,
        payload_json=payload or {},
        created_at=clock.now(),
    )
    chain.selar(db, entry)  # encadeia ao último hash da organização (#53)
    db.add(entry)
    if commit:
        db.commit()
    return entry


JANELA_LEITURA_S = 300


def log_leitura(
    db: Session,
    user: User,
    action: str,
    entity: str,
    entity_id: str | int | None,
    payload: dict[str, Any] | None = None,
    janela_s: int = JANELA_LEITURA_S,
) -> bool:
    """Audita uma LEITURA sem inflar o log: a mesma pessoa lendo o mesmo recurso dentro da janela vira um registro só.
    Devolve True quando gravou."""
    db.flush()
    desde = clock.now() - timedelta(seconds=janela_s)
    eid = str(entity_id) if entity_id is not None else None
    recentes = db.scalars(
        select(AuditLog.payload_json).where(
            AuditLog.user_id == user.id,
            AuditLog.action == action,
            AuditLog.entity == entity,
            AuditLog.entity_id == eid,
            AuditLog.created_at >= desde,
        )
    )
    # só repete quando o conteúdo lido também é o mesmo (ler outro tipo de documento da consulta é outro acesso)
    if any((p or {}) == (payload or {}) for p in recentes):
        return False
    log_action(db, user, action, entity, entity_id, payload)
    return True

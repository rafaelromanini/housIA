"""Cadastro brasileiro da organização e do profissional (#52): CNES, CBO e CNS vindos do banco."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.br import identificadores as ids
from app.db.models import Organization, User


def do_profissional(db: Session, user: User) -> tuple[str | None, str | None, str | None, str | None]:
    """(cns, cbo, cnes, nome da organização); valor guardado que não valida conta como ausente."""
    org = db.get(Organization, user.organization_id) if user.organization_id else None

    def ok(fn, valor):
        try:
            return fn(valor) if valor else None
        except ValueError:
            return None

    return (
        ok(ids.validar_cns, user.cns),
        ok(ids.validar_cbo, user.cbo),
        ok(ids.validar_cnes, org.cnes if org else None),
        (org.nome if org else None),
    )

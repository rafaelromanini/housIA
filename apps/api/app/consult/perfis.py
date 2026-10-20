"""Perfis de Smart Apps no servidor (#21): o que cada usuário esconde, por especialidade.

Vale o perfil do usuário na especialidade da consulta; sem ele, o perfil geral do usuário (especialidade vazia); sem
nenhum, todos os apps aparecem. O servidor guarda só os apps escondidos: app novo no registro aparece para todos.
"""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.consult import apps, knowledge
from app.db.models import SmartAppProfile, User


def _check_specialty(specialty: str) -> str:
    if specialty and specialty not in knowledge.SPECIALTIES:
        raise HTTPException(422, f"Especialidade desconhecida: {specialty}")
    return specialty


def _row(db: Session, user: User, specialty: str) -> SmartAppProfile | None:
    return db.scalar(
        select(SmartAppProfile).where(SmartAppProfile.user_id == user.id, SmartAppProfile.especialidade == specialty)
    )


def effective(db: Session, user: User, specialty: str = "") -> dict:
    """Perfil que vale para o usuário nesta especialidade, com a origem (para a tela explicar)."""
    specialty = _check_specialty(specialty)
    chain = ([(specialty, "especialidade")] if specialty else []) + [("", "usuario")]
    for scope, origin in chain:
        row = _row(db, user, scope)
        if row is not None:
            known = {a.manifest.id for a in apps.all_apps()}
            return {"especialidade": specialty, "ocultos": [i for i in row.ocultos if i in known], "origem": origin}
    return {"especialidade": specialty, "ocultos": [], "origem": "padrao"}


def save(db: Session, user: User, specialty: str, hidden: list[str]) -> dict:
    specialty = _check_specialty(specialty)
    known = {a.manifest.id for a in apps.all_apps()}
    unknown = sorted(set(hidden) - known)
    if unknown:
        raise HTTPException(422, f"Smart App desconhecido: {', '.join(unknown)}")
    row = _row(db, user, specialty)
    ordered = sorted(set(hidden))
    if row is None:
        db.add(SmartAppProfile(user_id=user.id, especialidade=specialty, ocultos=ordered))
    else:
        row.ocultos = ordered
    db.commit()
    return effective(db, user, specialty)


def listing(db: Session, user: User, specialty: str = "") -> list[dict]:
    hidden = set(effective(db, user, specialty)["ocultos"])
    return [{**m, "oculto": m["id"] in hidden} for m in apps.catalog(specialty or None)]

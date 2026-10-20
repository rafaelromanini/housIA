"""Tokens de uso único enviados por e-mail (guardamos só o hash; o token vale uma vez e expira)."""

import hashlib
import secrets
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.db.models import AuthToken

TTL = {"verify": timedelta(hours=48), "reset": timedelta(hours=1), "invite": timedelta(days=7)}


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue(db: Session, kind: str, **fields) -> str:
    raw = secrets.token_urlsafe(32)
    db.add(AuthToken(kind=kind, token_hash=_hash(raw), expires_at=clock.now() + TTL[kind], **fields))
    db.commit()
    return raw


def peek(db: Session, kind: str, raw: str) -> AuthToken:
    """Valida sem queimar (tela de aceite do convite). Qualquer falha responde o mesmo 400."""
    tok = db.scalar(select(AuthToken).where(AuthToken.token_hash == _hash(raw), AuthToken.kind == kind))
    if tok is None or tok.used_at is not None or tok.expires_at < clock.now():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Link inválido, já usado ou expirado")
    return tok


def consume(db: Session, kind: str, raw: str) -> AuthToken:
    """Valida e queima o token (o commit fica com quem chamou)."""
    tok = peek(db, kind, raw)
    tok.used_at = clock.now()
    return tok

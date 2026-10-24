"""Audit_log à prova de adulteração (#53): encadeamento de hash, verificação e bloqueio de UPDATE/DELETE.

Cada linha guarda `prev_hash` (o hash da linha anterior da mesma organização) e `hash` = HMAC-SHA256 da forma canônica
do conteúdo + `prev_hash`. Alterar uma linha muda o hash dela; apagar uma do meio quebra o elo da seguinte; reescrever a
cadeia inteira exige a chave (`AUDIT_CHAIN_KEY`, ou JWT_SECRET), que não está no banco. Limites assumidos e declarados:
apagar as últimas linhas (cauda) só se detecta comparando com a cabeça (`cabeca`) guardada fora do banco, por isso a
exportação a devolve assinada; quem tem a chave e o banco refaz tudo.

Linhas sem hash que vêm ANTES da primeira linha encadeada são "anteriores ao encadeamento" (migração): contadas, não
quebram. Linha sem hash DEPOIS de uma encadeada é quebra (alguém limpou o hash).
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AuditLog


class AuditoriaImutavel(RuntimeError):
    """O audit_log só recebe INSERT."""


def chave() -> bytes:
    s = get_settings()
    return (s.audit_chain_key or s.jwt_secret).encode("utf-8")


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def canonico(a: AuditLog, prev_hash: str | None) -> str:
    return json.dumps(
        {
            "o": a.organization_id,
            "u": a.user_id,
            "a": a.action,
            "e": a.entity,
            "i": a.entity_id,
            "p": a.payload_json or {},
            "t": _iso(a.created_at),
            "prev": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def calcular(a: AuditLog, prev_hash: str | None) -> str:
    return hmac.new(chave(), canonico(a, prev_hash).encode("utf-8"), hashlib.sha256).hexdigest()


def _da_org(organization_id: int | None):
    return (
        AuditLog.organization_id.is_(None) if organization_id is None else AuditLog.organization_id == organization_id
    )


def selar(db: Session, entry: AuditLog) -> None:
    """Encadeia a linha nova à última da mesma organização (organização e data já fixas em `log_action`)."""
    if db.get_bind().dialect.name == "postgresql":  # serializa a cadeia da organização até o fim da transação
        db.execute(text("select pg_advisory_xact_lock(:a, :b)"), {"a": 5301, "b": entry.organization_id or 0})
    db.flush()  # a sessão não faz autoflush: as linhas pendentes precisam entrar na consulta abaixo
    last = db.scalar(select(AuditLog.hash).where(_da_org(entry.organization_id)).order_by(AuditLog.id.desc()).limit(1))
    entry.prev_hash = last
    entry.hash = calcular(entry, last)


def verificar(db: Session, organization_id: int | None) -> dict[str, Any]:
    """Percorre a cadeia da organização e aponta a primeira linha quebrada (ou None)."""
    total = encadeadas = antigas = 0
    last: str | None = None
    quebra: dict[str, Any] | None = None
    started = False
    rows = db.scalars(select(AuditLog).where(_da_org(organization_id)).order_by(AuditLog.id))
    for a in rows:
        total += 1
        if a.hash is None:
            if started and quebra is None:
                quebra = {"id": a.id, "motivo": "linha sem hash depois do início do encadeamento (hash removido)"}
            elif not started:
                antigas += 1
            continue
        started = True
        encadeadas += 1
        if quebra is None:
            if not hmac.compare_digest(a.hash, calcular(a, a.prev_hash)):
                quebra = {"id": a.id, "motivo": "conteúdo da linha diferente do hash gravado (linha alterada)"}
            elif a.prev_hash != last:
                quebra = {"id": a.id, "motivo": "elo quebrado: linha anterior apagada, trocada ou reordenada"}
        last = a.hash
    return {
        "integra": quebra is None,
        "total": total,
        "encadeadas": encadeadas,
        "anteriores_ao_encadeamento": antigas,
        "primeira_quebra": quebra,
        "cabeca": last,
        "algoritmo": "HMAC-SHA256",
        "limite": "Apagar apenas as últimas linhas só é detectável comparando a cabeça com a guardada fora do banco.",
    }


# ---------- bloqueio no nível da aplicação ----------
@event.listens_for(Session, "before_flush")
def _so_insert(session: Session, _ctx, _instances) -> None:
    for obj in session.deleted:
        if isinstance(obj, AuditLog):
            raise AuditoriaImutavel("O audit_log não pode ser apagado.")
    for obj in session.dirty:
        if isinstance(obj, AuditLog) and session.is_modified(obj, include_collections=False):
            raise AuditoriaImutavel("O audit_log não pode ser alterado.")


@event.listens_for(Session, "do_orm_execute")
def _so_insert_em_massa(state) -> None:
    if (state.is_update or state.is_delete) and getattr(state.statement, "table", None) is not None:
        if state.statement.table.name == AuditLog.__tablename__:
            raise AuditoriaImutavel("O audit_log só recebe INSERT.")

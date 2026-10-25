"""Recibo de cada chamada ao provedor de IA, gravado no audit_log (#54) sem nenhum texto clínico.

O generate_structured só acumula o recibo na requisição em curso; quem tem a sessão do banco (get_db, ao fim da
requisição) descarrega no audit_log com a organização da sessão. Fora de uma requisição (tarefas, testes de serviço)
o recibo vai só para o log da aplicação.
"""

import contextvars
import logging
from typing import Any

logger = logging.getLogger("housia.llm")
ACAO = "llm.chamada"

_pendentes: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar("llm_recibos", default=None)
# último recibo da requisição/thread (consulta por testes e por quem quiser exibir)
ultimo: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("llm_ultimo_recibo", default=None)


def iniciar() -> None:
    """Chamado no início de cada requisição HTTP (middleware): cria a fila daquela requisição."""
    _pendentes.set([])


def registrar(recibo: dict[str, Any]) -> None:
    ultimo.set(recibo)
    fila = _pendentes.get()
    if fila is None:
        logger.info("Recibo de IA (fora de requisição): %s", recibo)
    else:
        fila.append(recibo)


def descarregar(db) -> None:
    """Grava os recibos pendentes no audit_log da organização da sessão. Nunca derruba a requisição."""
    fila = _pendentes.get()
    if not fila:
        return
    itens = list(fila)
    fila.clear()
    org = db.info.get("org_id")
    try:
        from app.audit.service import log_action

        gravou = False
        for r in itens:
            if org is None:
                logger.info("Recibo de IA sem organização: %s", r)
                continue
            log_action(db, None, ACAO, "llm", None, r, commit=False)
            gravou = True
        if gravou:
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("Falha ao gravar o recibo de IA no audit_log")

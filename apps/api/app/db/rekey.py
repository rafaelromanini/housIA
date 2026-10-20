"""Cifra o que ficou em JSON puro e recifra o que está com chave antiga (python -m app.db.rekey; também no startup).

A migration só cifra com a chave presente no `alembic upgrade`. Quem liga `DATA_ENCRYPTION_KEY` depois, ou troca a
chave de cifrar (rotação), passa por aqui para que nenhuma linha fique em texto aberto ou presa à chave antiga.
"""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import crypto
from app.db import session as db_session

logger = logging.getLogger("housia")
TABLE = "consultations"
COLUMNS = (
    "segments_json",
    "insights_json",
    "note_json",
    "ai_meta_json",
    "note_meta_json",
    "extra_json",
    "insight_actions_json",
)


def _redo(raw: str, current: str) -> str | None:
    """O valor cifrado com a chave atual; None se já está."""
    if crypto.is_encrypted(raw):
        if raw[len(crypto.PREFIX) :].partition(":")[0] == current:
            return None
        return crypto.encrypt(crypto.decrypt(raw))
    return crypto.encrypt(crypto.decrypt(raw))


def run(db: Session) -> int:
    """Devolve quantas linhas foram reescritas. Linha que não decifra é registrada (sem conteúdo) e pulada."""
    current = crypto.current_key_id()
    if current is None:
        return 0
    rows = db.execute(text(f"SELECT id, {', '.join(COLUMNS)} FROM {TABLE}")).mappings().all()
    done = 0
    for row in rows:
        values: dict[str, str] = {}
        for col in COLUMNS:
            raw = row[col]
            if raw is None or not isinstance(raw, str):
                continue
            try:
                new = _redo(raw, current)
            except Exception:  # noqa: BLE001 - chave ausente/errada ou dado adulterado: não derruba os demais
                logger.error(
                    "Consulta %s: coluna %s não pôde ser recifrada (chave ausente ou dado adulterado).", row["id"], col
                )
                continue
            if new is not None:
                values[col] = new
        if values:
            sets = ", ".join(f"{c} = :{c}" for c in values)
            db.execute(text(f"UPDATE {TABLE} SET {sets} WHERE id = :id"), values | {"id": row["id"]})
            done += 1
    if done:
        db.commit()
    return done


def main() -> int:
    with db_session.SessionLocal() as db:
        n = run(db)
    print(f"consultas recifradas: {n}")
    return n


if __name__ == "__main__":
    main()

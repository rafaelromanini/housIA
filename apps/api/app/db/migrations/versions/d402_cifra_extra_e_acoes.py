"""privacidade: extra_json (cópias da nota, copiloto) e insight_actions_json também viram texto cifrado (AES-GCM)

Revision ID: d402
Revises: d401
Create Date: 2026-10-20 13:00:00
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db import crypto

revision: str = "d402"
down_revision: str | None = "d401"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = {"extra_json": False, "insight_actions_json": True}  # coluna -> aceita NULL


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _convert(to_type: sa.types.TypeEngine, from_type: sa.types.TypeEngine, using: str) -> None:
    if op.get_bind().dialect.name == "postgresql":
        for col in COLUMNS:
            op.execute(f"ALTER TABLE consultations ALTER COLUMN {col} TYPE {using.format(col=col)}")
    else:
        with op.batch_alter_table("consultations") as batch:
            for col, nullable in COLUMNS.items():
                batch.alter_column(col, existing_type=from_type, type_=to_type, existing_nullable=nullable)


def _rewrite(transform) -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT id, {', '.join(COLUMNS)} FROM consultations")).mappings().all()
    for row in rows:
        values = {}
        for col in COLUMNS:
            raw = row[col]
            if raw is None:
                continue
            new = transform(raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False))
            if new is not None:
                values[col] = new
        if values:
            sets = ", ".join(f"{c} = :{c}" for c in values)
            bind.execute(sa.text(f"UPDATE consultations SET {sets} WHERE id = :id"), values | {"id": row["id"]})


def upgrade() -> None:
    _convert(sa.Text(), sa.JSON(), "TEXT USING {col}::text")
    if crypto.enabled():  # sem chave os dados seguem em JSON; `python -m app.db.rekey` (e o startup) cifram depois
        _rewrite(lambda raw: None if crypto.is_encrypted(raw) else crypto.encrypt(json.loads(raw)))


def downgrade() -> None:
    _rewrite(lambda raw: json.dumps(crypto.decrypt(raw), ensure_ascii=False) if crypto.is_encrypted(raw) else None)
    _convert(_json(), sa.Text(), "JSONB USING {col}::jsonb")

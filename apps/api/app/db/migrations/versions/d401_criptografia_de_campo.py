"""privacidade: colunas clínicas da consulta viram texto cifrado (AES-GCM) e os dados existentes são cifrados

Revision ID: d401
Revises: d201
Create Date: 2026-10-20 12:00:00
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db import crypto

revision: str = "d401"
down_revision: str | None = "d201"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ["segments_json", "insights_json", "note_json", "ai_meta_json", "note_meta_json"]


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _convert(to_type: sa.types.TypeEngine, using: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for col in COLUMNS:
            op.execute(f"ALTER TABLE consultations ALTER COLUMN {col} TYPE {using.format(col=col)}")
    else:
        with op.batch_alter_table("consultations") as batch:
            for col in COLUMNS:
                batch.alter_column(col, existing_type=sa.JSON(), type_=to_type, existing_nullable=True)


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
    _convert(sa.Text(), "TEXT USING {col}::text")
    if crypto.enabled():  # sem chave os dados seguem em JSON e são cifrados no próximo `alembic upgrade` com chave
        _rewrite(lambda raw: None if crypto.is_encrypted(raw) else crypto.encrypt(json.loads(raw)))


def downgrade() -> None:
    _rewrite(lambda raw: json.dumps(crypto.decrypt(raw), ensure_ascii=False) if crypto.is_encrypted(raw) else None)
    _convert(_json(), "JSONB USING {col}::jsonb")

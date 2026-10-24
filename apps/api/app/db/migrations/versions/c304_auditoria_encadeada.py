"""Auditoria à prova de adulteração (#53): prev_hash/hash no audit_log e gatilhos contra UPDATE/DELETE

Revision ID: c304
Revises: c303
Create Date: 2026-10-24 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.audit import trigger

revision: str = "c304"
down_revision: str | None = "c303"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("prev_hash", sa.String(length=64), nullable=True))
    op.add_column("audit_log", sa.Column("hash", sa.String(length=64), nullable=True))
    # as colunas novas entram antes dos gatilhos; linhas antigas ficam sem hash ("anteriores ao encadeamento")
    for sql in trigger.statements(op.get_bind().dialect.name):
        op.execute(sql)


def downgrade() -> None:
    for sql in trigger.statements(op.get_bind().dialect.name, up=False):
        op.execute(sql)
    op.drop_column("audit_log", "hash")
    op.drop_column("audit_log", "prev_hash")

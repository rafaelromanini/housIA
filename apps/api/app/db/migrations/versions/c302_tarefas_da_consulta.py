"""Pendências da consulta viram tarefas: ligação com a consulta, criticidade e chave de idempotência (#47)

Revision ID: c302
Revises: c301
Create Date: 2026-10-23 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c302"
down_revision: str | None = "c301"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("journey_tasks", sa.Column("consultation_id", sa.Integer(), nullable=True))
    op.add_column("journey_tasks", sa.Column("critical", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("journey_tasks", sa.Column("chave", sa.String(length=160), nullable=True))
    op.create_index("ix_journey_tasks_consultation_id", "journey_tasks", ["consultation_id"])


def downgrade() -> None:
    op.drop_index("ix_journey_tasks_consultation_id", table_name="journey_tasks")
    op.drop_column("journey_tasks", "chave")
    op.drop_column("journey_tasks", "critical")
    op.drop_column("journey_tasks", "consultation_id")

"""Memória de estilo da nota por profissional (só preferências de forma, sem texto clínico)

Revision ID: c301
Revises: e351
Create Date: 2026-10-23 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c301"
down_revision: str | None = "e351"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "estilo_notas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("dados_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_estilo_notas_user"),
    )
    op.create_index("ix_estilo_notas_organization_id", "estilo_notas", ["organization_id"])
    op.create_index("ix_estilo_notas_user_id", "estilo_notas", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_estilo_notas_user_id", table_name="estilo_notas")
    op.drop_index("ix_estilo_notas_organization_id", table_name="estilo_notas")
    op.drop_table("estilo_notas")

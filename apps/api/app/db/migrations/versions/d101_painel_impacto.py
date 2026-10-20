"""painel Impacto: nota NPS no feedback e linha de base por setor

Revision ID: d101
Revises: d001
Create Date: 2026-10-17 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d101"
down_revision: str | None = "d001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_feedback", sa.Column("score", sa.Integer(), nullable=True))
    op.create_table(
        "impact_baselines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sector", sa.String(length=60), nullable=False),
        sa.Column("minutes_before", sa.Float(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_impact_baselines_sector"), "impact_baselines", ["sector"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_impact_baselines_sector"), table_name="impact_baselines")
    op.drop_table("impact_baselines")
    op.drop_column("ai_feedback", "score")

"""consulta com IA: decisões do profissional sobre cada insight (aceitar, descartar, fixar)

Revision ID: d201
Revises: d101
Create Date: 2026-10-17 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d201"
down_revision: str | None = "d101"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column("consultations", sa.Column("insight_actions_json", _json(), nullable=True))


def downgrade() -> None:
    op.drop_column("consultations", "insight_actions_json")

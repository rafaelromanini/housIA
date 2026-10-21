"""Modo consultório: consulta de sandbox (fora do contador do plano)

Revision ID: e341
Revises: e331
Create Date: 2026-10-20 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e341"
down_revision: str | None = "e331"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("consultations") as batch:
        batch.add_column(sa.Column("sandbox", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_index("ix_consultations_sandbox", ["sandbox"])


def downgrade() -> None:
    with op.batch_alter_table("consultations") as batch:
        batch.drop_index("ix_consultations_sandbox")
        batch.drop_column("sandbox")

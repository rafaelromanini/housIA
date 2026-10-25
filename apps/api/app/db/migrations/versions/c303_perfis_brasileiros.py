"""Perfis brasileiros (#52): CNES da organização e CNS/CBO do profissional

Revision ID: c303
Revises: c302
Create Date: 2026-10-24 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c303"
down_revision: str | None = "c302"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("cnes", sa.String(length=7), nullable=True))
    op.add_column("users", sa.Column("cns", sa.String(length=15), nullable=True))
    op.add_column("users", sa.Column("cbo", sa.String(length=6), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "cbo")
    op.drop_column("users", "cns")
    op.drop_column("organizations", "cnes")

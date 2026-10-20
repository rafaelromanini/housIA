"""Smart Apps como plug-ins: perfis de Smart Apps por usuário e especialidade no servidor

Revision ID: c201
Revises: d402
Create Date: 2026-10-20 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c201"
down_revision: str | None = "d402"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "smart_app_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("especialidade", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("ocultos", _json(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "especialidade", name="uq_smart_app_profile"),
    )
    op.create_index("ix_smart_app_profiles_user_id", "smart_app_profiles", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_smart_app_profiles_user_id", table_name="smart_app_profiles")
    op.drop_table("smart_app_profiles")

"""Perfil de voz do profissional para a diarização (#40): só o embedding, cifrado, com o registro do consentimento

Revision ID: c305
Revises: c304
Create Date: 2026-10-24 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c305"
down_revision: str | None = "c304"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "perfis_voz",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("embedding_json", sa.Text(), nullable=False),
        sa.Column("termo_versao", sa.String(length=20), nullable=False),
        sa.Column("consentimento_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_perfis_voz_user"),
    )
    op.create_index("ix_perfis_voz_organization_id", "perfis_voz", ["organization_id"])
    op.create_index("ix_perfis_voz_user_id", "perfis_voz", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_perfis_voz_user_id", table_name="perfis_voz")
    op.drop_index("ix_perfis_voz_organization_id", table_name="perfis_voz")
    op.drop_table("perfis_voz")

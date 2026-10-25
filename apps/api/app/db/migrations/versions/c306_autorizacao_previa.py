"""Autorização prévia (#56): pedido com pacote de justificativa revisado e andamento junto da operadora

Revision ID: c306
Revises: c305
Create Date: 2026-10-24 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c306"
down_revision: str | None = "c305"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "autorizacoes_previas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("consultation_id", sa.Integer(), sa.ForeignKey("consultations.id"), nullable=False),
        sa.Column("patient_id", sa.String(length=64), nullable=False),
        sa.Column("regra_id", sa.String(length=60), nullable=False),
        sa.Column("regra_versao", sa.String(length=40), nullable=False),
        sa.Column("procedimento", sa.String(length=160), nullable=False),
        sa.Column("operadora", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="rascunho"),
        sa.Column("pacote_json", sa.Text(), nullable=False),
        sa.Column("pacote_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("revisado_por", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("revisado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("protocolo", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enviado_em", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decidido_em", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("consultation_id", "regra_id", name="uq_autorizacao_consulta_regra"),
    )
    op.create_index("ix_autorizacoes_previas_organization_id", "autorizacoes_previas", ["organization_id"])
    op.create_index("ix_autorizacoes_previas_consultation_id", "autorizacoes_previas", ["consultation_id"])
    op.create_index("ix_autorizacoes_previas_patient_id", "autorizacoes_previas", ["patient_id"])


def downgrade() -> None:
    op.drop_index("ix_autorizacoes_previas_patient_id", table_name="autorizacoes_previas")
    op.drop_index("ix_autorizacoes_previas_consultation_id", table_name="autorizacoes_previas")
    op.drop_index("ix_autorizacoes_previas_organization_id", table_name="autorizacoes_previas")
    op.drop_table("autorizacoes_previas")

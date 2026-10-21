"""Envio estruturado ao prontuário: fila ehr_outbox com status, tentativas e último erro

Revision ID: c202
Revises: e341
Create Date: 2026-10-21 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c202"
down_revision: str | None = "e341"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ehr_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("consultation_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pendente"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),  # EncryptedJson: texto cifrado
        sa.Column("fhir_ref", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["consultation_id"], ["consultations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("consultation_id", name="uq_ehr_outbox_consultation"),
    )
    op.create_index("ix_ehr_outbox_organization_id", "ehr_outbox", ["organization_id"])
    op.create_index("ix_ehr_outbox_status", "ehr_outbox", ["status"])


def downgrade() -> None:
    op.drop_index("ix_ehr_outbox_organization_id", table_name="ehr_outbox")
    op.drop_index("ix_ehr_outbox_status", table_name="ehr_outbox")
    op.drop_table("ehr_outbox")

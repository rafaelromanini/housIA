"""consultas com ia (transcrição, smart apps, nota e envio ao prontuário)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16 01:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "consultations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.String(length=64), nullable=False),
        sa.Column("encounter_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("specialty", sa.String(length=40), nullable=False),
        sa.Column("patient_language", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("segments_json", _json(), nullable=False),
        sa.Column("insights_json", _json(), nullable=True),
        sa.Column("ai_meta_json", _json(), nullable=True),
        sa.Column("note_json", _json(), nullable=True),
        sa.Column("note_meta_json", _json(), nullable=True),
        sa.Column("extra_json", _json(), nullable=False),
        sa.Column("fhir_ref", sa.String(length=120), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_consultations_patient_id"), "consultations", ["patient_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_consultations_patient_id"), table_name="consultations")
    op.drop_table("consultations")

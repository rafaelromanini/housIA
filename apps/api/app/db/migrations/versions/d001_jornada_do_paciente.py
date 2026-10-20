"""jornada do paciente (contatos pós-alta e tarefas de acompanhamento)

Revision ID: d001
Revises: 0003
Create Date: 2026-10-16 03:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d001"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "journey_contacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.String(length=64), nullable=False),
        sa.Column("encounter_id", sa.String(length=64), nullable=True),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("reached", sa.Boolean(), nullable=False),
        sa.Column("answers_json", _json(), nullable=False),
        sa.Column("alarm_signs_json", _json(), nullable=False),
        sa.Column("pendencies_json", _json(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("fhir_ref", sa.String(length=120), nullable=True),
        sa.Column("alert_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["alert_id"], ["alerts.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_journey_contacts_patient_id"), "journey_contacts", ["patient_id"], unique=False)
    op.create_index(op.f("ix_journey_contacts_created_at"), "journey_contacts", ["created_at"], unique=False)
    op.create_table(
        "journey_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.String(length=64), nullable=False),
        sa.Column("rule", sa.String(length=60), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source_refs", _json(), nullable=False),
        sa.Column("fhir_ref", sa.String(length=120), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_by", sa.Integer(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closing_note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["closed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_journey_tasks_patient_id"), "journey_tasks", ["patient_id"], unique=False)
    op.create_index(op.f("ix_journey_tasks_rule"), "journey_tasks", ["rule"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_journey_tasks_rule"), table_name="journey_tasks")
    op.drop_index(op.f("ix_journey_tasks_patient_id"), table_name="journey_tasks")
    op.drop_table("journey_tasks")
    op.drop_index(op.f("ix_journey_contacts_created_at"), table_name="journey_contacts")
    op.drop_index(op.f("ix_journey_contacts_patient_id"), table_name="journey_contacts")
    op.drop_table("journey_contacts")

"""Autocadastro e multi-organização: organizations, dados do conselho no usuário, auth_tokens e organization_id

Revision ID: e331
Revises: c201
Create Date: 2026-10-20 12:00:00
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "e331"
down_revision: str | None = "c201"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = [
    "handoff_reports",
    "alerts",
    "discharge_assessments",
    "discharge_overrides",
    "medrec_runs",
    "audit_log",
    "ai_feedback",
    "discharge_documents",
    "consultations",
    "journey_contacts",
    "journey_tasks",
    "impact_baselines",
]
DEFAULT_ORG = "Hospital Demonstração"


def upgrade() -> None:
    org = op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("nome", sa.String(length=160), nullable=False),
        sa.Column("tipo", sa.String(length=20), nullable=False, server_default="hospital"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("organization_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("conselho", sa.String(length=10), nullable=True))
        batch.add_column(sa.Column("conselho_numero", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("uf", sa.String(length=2), nullable=True))
        batch.add_column(sa.Column("email_verificado", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("conselho_verificado", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_foreign_key("fk_users_organization", "organizations", ["organization_id"], ["id"])
        batch.create_index("ix_users_organization_id", ["organization_id"])
    for table in TENANT_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("organization_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(f"fk_{table}_organization", "organizations", ["organization_id"], ["id"])
            batch.create_index(f"ix_{table}_organization_id", ["organization_id"])

    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(length=200), nullable=True),
        sa.Column("role", sa.String(length=20), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_tokens_kind", "auth_tokens", ["kind"])
    op.create_index("ix_auth_tokens_token_hash", "auth_tokens", ["token_hash"], unique=True)

    # Organização padrão: os usuários e os dados que já existiam passam a pertencer a ela
    op.bulk_insert(org, [{"nome": DEFAULT_ORG, "tipo": "hospital", "created_at": datetime.now(UTC)}])
    conn = op.get_bind()
    org_id = conn.execute(sa.text("SELECT id FROM organizations WHERE nome = :n"), {"n": DEFAULT_ORG}).scalar_one()
    for table in ["users", *TENANT_TABLES]:
        conn.execute(sa.text(f"UPDATE {table} SET organization_id = :o WHERE organization_id IS NULL"), {"o": org_id})


def downgrade() -> None:
    op.drop_index("ix_auth_tokens_token_hash", table_name="auth_tokens")
    op.drop_index("ix_auth_tokens_kind", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    for table in reversed(TENANT_TABLES):
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_organization_id")
            batch.drop_constraint(f"fk_{table}_organization", type_="foreignkey")
            batch.drop_column("organization_id")
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_organization_id")
        batch.drop_constraint("fk_users_organization", type_="foreignkey")
        for col in ("conselho_verificado", "email_verificado", "uf", "conselho_numero", "conselho", "organization_id"):
            batch.drop_column(col)
    op.drop_table("organizations")

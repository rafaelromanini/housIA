"""Planos e teste grátis: catálogo de planos e direito de uso por organização

Revision ID: e351
Revises: c202
Create Date: 2026-10-21 09:00:00
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "e351"
down_revision: str | None = "c202"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODULOS = ["passagem_plantao", "reconciliacao", "alta"]
PLANOS = [
    ("teste", "Teste grátis", 10, []),
    ("profissional", "Profissional", None, []),
    ("clinica", "Clínica", None, ["passagem_plantao", "reconciliacao"]),
    ("hospital", "Hospital", None, MODULOS),
]


def upgrade() -> None:
    plans = op.create_table(
        "plans",
        sa.Column("codigo", sa.String(20), primary_key=True),
        sa.Column("nome", sa.String(60), nullable=False),
        sa.Column("limite_consultas", sa.Integer(), nullable=True),
        sa.Column("modulos_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "entitlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, unique=True),
        sa.Column("plano", sa.String(20), sa.ForeignKey("plans.codigo"), nullable=False),
        sa.Column("limite_consultas", sa.Integer(), nullable=True),
        sa.Column("modulos_json", sa.JSON(), nullable=False),
        sa.Column("inicio", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fim", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.bulk_insert(
        plans, [{"codigo": c, "nome": n, "limite_consultas": lim, "modulos_json": m} for c, n, lim, m in PLANOS]
    )
    # organizações existentes: consultório começa no teste grátis, hospital no plano hospitalar
    bind = op.get_bind()
    agora = datetime.now(UTC)
    por_codigo = {c: (lim, m) for c, _n, lim, m in PLANOS}
    ents = sa.table(
        "entitlements",
        sa.column("organization_id", sa.Integer),
        sa.column("plano", sa.String),
        sa.column("limite_consultas", sa.Integer),
        sa.column("modulos_json", sa.JSON),
        sa.column("inicio", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    for org_id, tipo in bind.execute(sa.text("SELECT id, tipo FROM organizations")).fetchall():
        codigo = "teste" if tipo == "consultorio" else "hospital"
        lim, mods = por_codigo[codigo]
        op.bulk_insert(
            ents,
            [
                {
                    "organization_id": org_id,
                    "plano": codigo,
                    "limite_consultas": lim,
                    "modulos_json": mods,
                    "inicio": agora,
                    "updated_at": agora,
                }
            ],
        )


def downgrade() -> None:
    op.drop_table("entitlements")
    op.drop_table("plans")

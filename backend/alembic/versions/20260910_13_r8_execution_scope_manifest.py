"""Add explicit R8 current-scope manifests, including valid empty scopes."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_13"
down_revision = "20260910_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "current_execution_scope",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("entity_kind", sa.String(length=40), nullable=False),
        sa.Column("scope_key", sa.String(length=256), nullable=False),
        sa.Column("source_revision", sa.String(length=256), nullable=False),
        sa.Column("source_generation_id", sa.BigInteger(), nullable=True),
        sa.Column("result_ready", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["source_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("entity_kind", "scope_key", name="uq_current_execution_scope_kind_key"),
    )
    op.create_index(
        "ix_current_execution_scope_revision",
        "current_execution_scope",
        ["entity_kind", "source_revision"],
    )


def downgrade() -> None:
    op.drop_table("current_execution_scope")

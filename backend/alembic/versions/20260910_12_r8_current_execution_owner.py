"""Add one compact current owner for the R8 execution contour."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_12"
down_revision = "20260910_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "current_execution_row",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("entity_kind", sa.String(length=40), nullable=False),
        sa.Column("business_identity", sa.String(length=256), nullable=False),
        sa.Column("scope_key", sa.String(length=256), nullable=False),
        sa.Column("source_revision", sa.String(length=256), nullable=False),
        sa.Column("source_generation_id", sa.BigInteger(), nullable=True),
        sa.Column("result_status", sa.String(length=16), nullable=False, server_default="accepted"),
        sa.Column("result_ready", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("manual_input", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["source_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "entity_kind", "scope_key", "business_identity",
            name="uq_current_execution_row_identity",
        ),
        sa.CheckConstraint(
            "result_status IN ('accepted', 'closed')",
            name="ck_current_execution_row_status",
        ),
    )
    op.create_index(
        "ix_current_execution_row_scope_status",
        "current_execution_row",
        ["entity_kind", "scope_key", "result_status"],
    )
    op.create_index(
        "ix_current_execution_row_revision",
        "current_execution_row",
        ["entity_kind", "source_revision"],
    )
    op.create_index(
        "ix_current_execution_row_source_generation_id",
        "current_execution_row",
        ["source_generation_id"],
    )

    op.create_table(
        "current_execution_change",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("current_row_id", sa.BigInteger(), nullable=True),
        sa.Column("entity_kind", sa.String(length=40), nullable=False),
        sa.Column("business_identity", sa.String(length=256), nullable=False),
        sa.Column("scope_key", sa.String(length=256), nullable=False),
        sa.Column("source_revision", sa.String(length=256), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False, server_default="recalculation"),
        sa.Column("before_payload", sa.JSON(), nullable=True),
        sa.Column("after_payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["current_row_id"], ["current_execution_row.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_current_execution_change_identity",
        "current_execution_change",
        ["entity_kind", "business_identity", "id"],
    )
    op.create_index(
        "ix_current_execution_change_scope",
        "current_execution_change",
        ["scope_key", "id"],
    )

    # The upgrade intentionally does not guess a current row from arbitrary
    # historical copies.  The accepted pointer is the only valid source for a
    # subsequent worker publication; ambiguous legacy copies remain visible to
    # the preflight and block publication instead of being selected as latest.


def downgrade() -> None:
    op.drop_table("current_execution_change")
    op.drop_table("current_execution_row")

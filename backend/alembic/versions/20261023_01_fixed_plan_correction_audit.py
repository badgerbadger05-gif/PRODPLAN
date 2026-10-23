"""Add durable audit for explicit corrections of fixed plan groups."""

from alembic import op
import sqlalchemy as sa


revision = "20261023_01"
down_revision = "20261009_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "production_plan_correction",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("group_scope_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("plan_ids", sa.JSON(), nullable=False),
        sa.Column("primary_plan_id", sa.Integer(), nullable=False),
        sa.Column("expected_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("expected_source_revision", sa.String(length=256), nullable=False),
        sa.Column("published_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("target_payload", sa.JSON(), nullable=False),
        sa.Column("before_payload", sa.JSON(), nullable=False),
        sa.Column("after_payload", sa.JSON(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=False),
        sa.Column("corrected_by", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(source_hash) = 64",
            name="ck_production_plan_correction_source_hash",
        ),
        sa.CheckConstraint(
            "length(group_scope_hash) = 64",
            name="ck_production_plan_correction_group_hash",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_production_plan_correction_request_hash",
        ),
        sa.ForeignKeyConstraint(
            ["primary_plan_id"], ["production_plan_header.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["expected_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["published_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "source_hash",
            name="uq_production_plan_correction_source",
        ),
    )
    op.create_index(
        "ix_production_plan_correction_published_generation",
        "production_plan_correction",
        ["published_generation_id"],
    )

    op.create_table(
        "production_plan_correction_line",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("correction_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("plan_line_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("bucket_date", sa.Date(), nullable=False),
        sa.Column("existed_before", sa.Boolean(), nullable=False),
        sa.Column("allocation_role", sa.String(length=32), nullable=False),
        sa.Column("before_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("after_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("accepted_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("before_remaining_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("after_remaining_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("after_overproduction_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("target_cell_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("before_qty >= 0", name="ck_plan_correction_line_before_qty"),
        sa.CheckConstraint("after_qty >= 0", name="ck_plan_correction_line_after_qty"),
        sa.CheckConstraint("accepted_qty >= 0", name="ck_plan_correction_line_accepted_qty"),
        sa.CheckConstraint(
            "before_remaining_qty >= 0",
            name="ck_plan_correction_line_before_remaining",
        ),
        sa.CheckConstraint(
            "after_remaining_qty >= 0",
            name="ck_plan_correction_line_after_remaining",
        ),
        sa.CheckConstraint(
            "after_overproduction_qty >= 0",
            name="ck_plan_correction_line_after_overproduction",
        ),
        sa.CheckConstraint(
            "target_cell_qty >= 0",
            name="ck_plan_correction_line_target_cell_qty",
        ),
        sa.ForeignKeyConstraint(
            ["correction_id"], ["production_plan_correction.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["production_plan_header.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["plan_line_id"], ["production_plan_line.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["item_id"], ["items.item_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "correction_id",
            "plan_line_id",
            name="uq_production_plan_correction_line",
        ),
    )
    op.create_index(
        "ix_production_plan_correction_line_correction",
        "production_plan_correction_line",
        ["correction_id"],
    )
    op.create_index(
        "ix_production_plan_correction_line_plan",
        "production_plan_correction_line",
        ["plan_id", "plan_line_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_production_plan_correction_line_plan",
        table_name="production_plan_correction_line",
    )
    op.drop_index(
        "ix_production_plan_correction_line_correction",
        table_name="production_plan_correction_line",
    )
    op.drop_table("production_plan_correction_line")
    op.drop_index(
        "ix_production_plan_correction_published_generation",
        table_name="production_plan_correction",
    )
    op.drop_table("production_plan_correction")

"""Add immutable successor-MRP plan lineage.

Revision ID: 20260804_01
Revises: 20260802_01
"""

import sqlalchemy as sa
from alembic import op


revision = "20260804_01"
down_revision = "20260802_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("specifications") as batch_op:
        batch_op.add_column(sa.Column("content_hash", sa.String(length=64), nullable=True))
        batch_op.create_index(
            "ix_specifications_content_hash", ["content_hash"], unique=False
        )

    op.create_table(
        "specification_revision",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("spec_id", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=32), server_default="odata", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["spec_id"], ["specifications.spec_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("spec_id", "content_hash", name="uq_specification_revision_content"),
    )
    op.create_index("ix_specification_revision_spec_id", "specification_revision", ["spec_id"])
    op.create_index("ix_specification_revision_content_hash", "specification_revision", ["content_hash"])
    op.create_index(
        "ix_specification_revision_spec_created",
        "specification_revision",
        ["spec_id", "created_at"],
    )

    op.create_table(
        "specification_rebase_queue",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("spec_id", sa.Integer(), nullable=False),
        sa.Column("revision_id", sa.BigInteger(), nullable=False),
        sa.Column("old_content_hash", sa.String(length=64), nullable=False),
        sa.Column("new_content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_specification_rebase_queue_status",
        ),
        sa.ForeignKeyConstraint(["revision_id"], ["specification_revision.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["spec_id"], ["specifications.spec_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_id", name="uq_specification_rebase_queue_revision"),
    )
    op.create_index("ix_specification_rebase_queue_spec_id", "specification_rebase_queue", ["spec_id"])
    op.create_index("ix_specification_rebase_queue_revision_id", "specification_rebase_queue", ["revision_id"])
    op.create_index(
        "ix_specification_rebase_queue_status_detected",
        "specification_rebase_queue",
        ["status", "detected_at"],
    )

    with op.batch_alter_table("mrp_freeze_component") as batch_op:
        batch_op.alter_column(
            "spec_version",
            existing_type=sa.String(length=50),
            type_=sa.String(length=64),
            existing_nullable=True,
        )

    with op.batch_alter_table("production_products") as batch_op:
        batch_op.add_column(
            sa.Column("spec_revision_hash", sa.String(length=64), nullable=True)
        )
        batch_op.create_index(
            "ix_production_products_spec_revision_hash",
            ["spec_revision_hash"],
            unique=False,
        )

    with op.batch_alter_table("production_plan_header") as batch_op:
        batch_op.add_column(sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("closed_reason", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("predecessor_plan_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("predecessor_run_id", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("lineage_reason", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("lineage_context", sa.JSON(), nullable=True))
        batch_op.create_foreign_key(
            "fk_production_plan_header_predecessor_plan",
            "production_plan_header",
            ["predecessor_plan_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_production_plan_header_predecessor_run",
            "planning_run",
            ["predecessor_run_id"],
            ["run_id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_production_plan_header_predecessor_run",
            ["predecessor_run_id"],
        )
        batch_op.create_index(
            "ix_production_plan_header_predecessor_plan_id",
            ["predecessor_plan_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_production_plan_header_predecessor_run_id",
            ["predecessor_run_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("production_plan_header") as batch_op:
        batch_op.drop_index("ix_production_plan_header_predecessor_run_id")
        batch_op.drop_index("ix_production_plan_header_predecessor_plan_id")
        batch_op.drop_constraint(
            "uq_production_plan_header_predecessor_run", type_="unique"
        )
        batch_op.drop_constraint(
            "fk_production_plan_header_predecessor_run", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_production_plan_header_predecessor_plan", type_="foreignkey"
        )
        batch_op.drop_column("lineage_context")
        batch_op.drop_column("lineage_reason")
        batch_op.drop_column("predecessor_run_id")
        batch_op.drop_column("predecessor_plan_id")
        batch_op.drop_column("closed_reason")
        batch_op.drop_column("closed_at")

    with op.batch_alter_table("mrp_freeze_component") as batch_op:
        batch_op.alter_column(
            "spec_version",
            existing_type=sa.String(length=64),
            type_=sa.String(length=50),
            existing_nullable=True,
        )

    with op.batch_alter_table("production_products") as batch_op:
        batch_op.drop_index("ix_production_products_spec_revision_hash")
        batch_op.drop_column("spec_revision_hash")

    op.drop_index(
        "ix_specification_rebase_queue_status_detected",
        table_name="specification_rebase_queue",
    )
    op.drop_index("ix_specification_rebase_queue_revision_id", table_name="specification_rebase_queue")
    op.drop_index("ix_specification_rebase_queue_spec_id", table_name="specification_rebase_queue")
    op.drop_table("specification_rebase_queue")
    op.drop_index(
        "ix_specification_revision_spec_created", table_name="specification_revision"
    )
    op.drop_index("ix_specification_revision_content_hash", table_name="specification_revision")
    op.drop_index("ix_specification_revision_spec_id", table_name="specification_revision")
    op.drop_table("specification_revision")

    with op.batch_alter_table("specifications") as batch_op:
        batch_op.drop_index("ix_specifications_content_hash")
        batch_op.drop_column("content_hash")

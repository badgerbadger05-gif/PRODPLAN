"""Persist the immutable physical boundary used by an MRP freeze.

Revision ID: 20260725_01
Revises: 20260724_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260725_01"
down_revision = "20260724_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mrp_freeze_baseline",
        sa.Column("baseline_at", sa.TIMESTAMP(), nullable=True),
    )
    op.add_column(
        "mrp_freeze_baseline",
        sa.Column("physical_import_batch_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_mrp_freeze_baseline_physical_import_batch_id",
        "mrp_freeze_baseline",
        ["physical_import_batch_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_mrp_freeze_baseline_physical_import_batch_id",
        "mrp_freeze_baseline",
        "physical_import_batch",
        ["physical_import_batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_mrp_freeze_baseline_physical_import_batch_id",
        "mrp_freeze_baseline",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_mrp_freeze_baseline_physical_import_batch_id",
        table_name="mrp_freeze_baseline",
    )
    op.drop_column("mrp_freeze_baseline", "physical_import_batch_id")
    op.drop_column("mrp_freeze_baseline", "baseline_at")

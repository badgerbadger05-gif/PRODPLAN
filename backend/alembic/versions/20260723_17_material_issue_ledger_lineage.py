"""Pin new production material issues to an Item Ledger generation.

Revision ID: 20260723_17
Revises: 20260723_16
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_17"
down_revision = "20260723_16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "production_material_issues",
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_prod_mat_issue_ledger_gen",
        "production_material_issues",
        "ledger_generation",
        ["ledger_generation_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index(
        "ix_prod_mat_issue_ledger_gen",
        "production_material_issues", ["ledger_generation_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_prod_mat_issue_ledger_gen", table_name="production_material_issues")
    op.drop_constraint("fk_prod_mat_issue_ledger_gen", "production_material_issues", type_="foreignkey")
    op.drop_column("production_material_issues", "ledger_generation_id")

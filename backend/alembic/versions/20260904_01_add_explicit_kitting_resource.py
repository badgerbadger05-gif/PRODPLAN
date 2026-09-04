"""Add the explicit kitting property to production resources.

Revision ID: 20260904_01
Revises: 20260903_04
"""

from alembic import op
import sqlalchemy as sa


revision = "20260904_01"
down_revision = "20260903_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("production_resources") as batch:
        batch.add_column(
            sa.Column(
                "is_kitting",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    # Owner decision 2026-09-04: warehouses 3 and 4 are dedicated kitting
    # resources; the existing fastener resource 14 remains a normal workshop.
    op.execute(
        "UPDATE production_resources SET is_kitting = true "
        "WHERE resource_name IN ("
        "'Комплектовка — склад №3', 'Комплектовка — склад №4'"
        ")"
    )
    op.execute(
        "UPDATE production_resources SET is_kitting = false "
        "WHERE resource_id = 14"
    )


def downgrade() -> None:
    with op.batch_alter_table("production_resources") as batch:
        batch.drop_column("is_kitting")

"""add requested_qty and planned_qty to planned_order

Revision ID: 20251119_01
Revises: 20251009_07
Create Date: 2025-11-19 09:58:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251119_01"
down_revision = "20251009_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # planned_order
    op.execute(
        sa.text(
            """
            ALTER TABLE planned_order
                ADD COLUMN IF NOT EXISTS requested_qty NUMERIC(15, 3);
            ALTER TABLE planned_order
                ADD COLUMN IF NOT EXISTS planned_qty NUMERIC(15, 3);
            """
        )
    )
    # planned_purchase
    op.execute(
        sa.text(
            """
            ALTER TABLE planned_purchase
                ADD COLUMN IF NOT EXISTS requested_qty NUMERIC(15, 3);
            ALTER TABLE planned_purchase
                ADD COLUMN IF NOT EXISTS planned_qty NUMERIC(15, 3);
            """
        )
    )

    op.execute(
        sa.text(
            """
            UPDATE planned_order
               SET requested_qty = qty,
                   planned_qty = qty
             WHERE requested_qty IS NULL
                OR planned_qty IS NULL;
            UPDATE planned_purchase
               SET requested_qty = qty,
                   planned_qty = qty
             WHERE requested_qty IS NULL
                OR planned_qty IS NULL;
            """
        )
    )

    op.execute(
        sa.text(
            """
            ALTER TABLE planned_order
                ALTER COLUMN requested_qty SET NOT NULL;
            ALTER TABLE planned_order
                ALTER COLUMN planned_qty SET NOT NULL;
            ALTER TABLE planned_purchase
                ALTER COLUMN requested_qty SET NOT NULL;
            ALTER TABLE planned_purchase
                ALTER COLUMN planned_qty SET NOT NULL;
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            ALTER TABLE planned_purchase
                DROP COLUMN IF EXISTS planned_qty;
            ALTER TABLE planned_purchase
                DROP COLUMN IF EXISTS requested_qty;
            ALTER TABLE planned_order
                DROP COLUMN IF EXISTS planned_qty;
            ALTER TABLE planned_order
                DROP COLUMN IF EXISTS requested_qty;
            """
        )
    )

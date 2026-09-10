"""Allow one stable assignment pair to retain mixed exact/FIFO provenance."""

from alembic import op


revision = "20260910_07"
down_revision = "20260910_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reservation_consumption_allocation") as batch:
        batch.drop_constraint(
            "ck_reservation_consumption_allocation_match_rule", type_="check"
        )
        batch.create_check_constraint(
            "ck_reservation_consumption_allocation_match_rule",
            "match_rule IN ('pegged', 'fifo', 'mixed')",
        )


def downgrade() -> None:
    with op.batch_alter_table("reservation_consumption_allocation") as batch:
        batch.drop_constraint(
            "ck_reservation_consumption_allocation_match_rule", type_="check"
        )
        batch.create_check_constraint(
            "ck_reservation_consumption_allocation_match_rule",
            "match_rule IN ('pegged', 'fifo')",
        )

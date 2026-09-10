"""Separate material-consumption and current replenishment basis rows."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_05"
down_revision = "20260910_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reservation_consumption_allocation",
        sa.Column("allocation_role", sa.String(length=32), nullable=True),
    )
    op.execute(
        "UPDATE reservation_consumption_allocation SET allocation_role = "
        "CASE WHEN is_current THEN 'replenishment_receipt' "
        "ELSE 'material_consumption' END WHERE allocation_role IS NULL"
    )
    op.alter_column("reservation_consumption_allocation", "allocation_role", nullable=False)
    op.drop_constraint(
        "uq_res_consumption_generation_sle_reservation",
        "reservation_consumption_allocation",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_res_consumption_generation_role_sle_reservation",
        "reservation_consumption_allocation",
        ["ledger_generation_id", "allocation_role", "sle_id", "reservation_id"],
    )
    op.drop_index(
        "uq_res_consumption_current_sle_reservation",
        table_name="reservation_consumption_allocation",
    )
    op.create_index(
        "uq_res_consumption_current_sle_reservation",
        "reservation_consumption_allocation",
        ["allocation_role", "sle_id", "reservation_id"],
        unique=True,
        postgresql_where=sa.text("is_current = true"),
        sqlite_where=sa.text("is_current = 1"),
    )
    op.create_check_constraint(
        "ck_reservation_consumption_allocation_role",
        "reservation_consumption_allocation",
        "allocation_role IN ('material_consumption', 'replenishment_receipt')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_reservation_consumption_allocation_role",
        "reservation_consumption_allocation",
        type_="check",
    )
    op.drop_index(
        "uq_res_consumption_current_sle_reservation",
        table_name="reservation_consumption_allocation",
    )
    op.create_index(
        "uq_res_consumption_current_sle_reservation",
        "reservation_consumption_allocation",
        ["sle_id", "reservation_id"],
        unique=True,
        postgresql_where=sa.text("is_current = true"),
        sqlite_where=sa.text("is_current = 1"),
    )
    op.drop_constraint(
        "uq_res_consumption_generation_role_sle_reservation",
        "reservation_consumption_allocation",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_res_consumption_generation_sle_reservation",
        "reservation_consumption_allocation",
        ["ledger_generation_id", "sle_id", "reservation_id"],
    )
    op.drop_column("reservation_consumption_allocation", "allocation_role")

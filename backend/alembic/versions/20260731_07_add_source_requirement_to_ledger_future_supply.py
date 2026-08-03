"""Track source requirement identity for future-supply evidence rows.

Revision ID: 20260731_07
Revises: 20260731_06
"""

from alembic import op
import sqlalchemy as sa


revision = "20260731_07"
down_revision = "20260731_06"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    columns = {row["name"] for row in inspector.get_columns(table_name)}
    return column_name in columns


def _has_index(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    indexes = {row["name"] for row in inspector.get_indexes(table_name)}
    return index_name in indexes


def _has_fk(inspector: sa.Inspector, table_name: str, constraint_name: str) -> bool:
    names = {row["name"] for row in inspector.get_foreign_keys(table_name)}
    return constraint_name in names


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ledger_future_supply" not in set(inspector.get_table_names()):
        return

    if not _has_column(inspector, "ledger_future_supply", "source_requirement_id"):
        op.add_column(
            "ledger_future_supply",
            sa.Column("source_requirement_id", sa.Integer(), nullable=True),
        )

    if not _has_fk(inspector, "ledger_future_supply", "fk_ledger_future_supply_source_requirement_id"):
        op.create_foreign_key(
            "fk_ledger_future_supply_source_requirement_id",
            "ledger_future_supply",
            "mrp_requirement",
            ["source_requirement_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    if not _has_index(
        inspector,
        "ledger_future_supply",
        "ix_ledger_future_supply_source_requirement_id",
    ):
        op.create_index(
            "ix_ledger_future_supply_source_requirement_id",
            "ledger_future_supply",
            ["source_requirement_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ledger_future_supply" not in set(inspector.get_table_names()):
        return

    if _has_index(
        inspector,
        "ledger_future_supply",
        "ix_ledger_future_supply_source_requirement_id",
    ):
        op.drop_index(
            "ix_ledger_future_supply_source_requirement_id",
            table_name="ledger_future_supply",
        )

    if _has_fk(
        inspector,
        "ledger_future_supply",
        "fk_ledger_future_supply_source_requirement_id",
    ):
        op.drop_constraint(
            "fk_ledger_future_supply_source_requirement_id",
            "ledger_future_supply",
            type_="foreignkey",
        )

    if _has_column(inspector, "ledger_future_supply", "source_requirement_id"):
        op.drop_column("ledger_future_supply", "source_requirement_id")

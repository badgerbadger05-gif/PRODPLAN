"""Add canonical reservation consumption allocation projection table.

Revision ID: 20260731_09
Revises: 20260731_08
"""

from alembic import op
import sqlalchemy as sa


revision = "20260731_09"
down_revision = "20260731_08"
branch_labels = None
depends_on = None


def _bigint() -> sa.BigInteger:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _has_table(inspector: sa.Inspector, table: str) -> bool:
    return table in set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_table(inspector, "reservation_consumption_allocation"):
        return

    op.create_table(
        "reservation_consumption_allocation",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "ledger_generation_id",
            _bigint(),
            sa.ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reservation_id",
            _bigint(),
            sa.ForeignKey("reservation_entry.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "sle_id",
            _bigint(),
            sa.ForeignKey("stock_ledger_entry.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "requirement_id",
            sa.Integer(),
            sa.ForeignKey("mrp_requirement.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("allocated_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("match_rule", sa.String(16), nullable=False),
        sa.Column("fact_ref", sa.String(64), nullable=False, server_default=""),
        sa.Column("fact_line_ref", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.item_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("characteristic_ref", sa.String(36), nullable=False, server_default=""),
        sa.Column("organization_ref", sa.String(36), nullable=False, server_default=""),
        sa.Column("planning_stock_pool", sa.String(64), nullable=False, server_default="default"),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("event_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("ingested_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "match_rule IN ('pegged', 'fifo')",
            name="ck_reservation_consumption_allocation_match_rule",
        ),
        sa.CheckConstraint(
            "allocated_qty > 0",
            name="ck_reservation_consumption_allocation_qty_positive",
        ),
        sa.UniqueConstraint(
            "ledger_generation_id",
            "idempotency_key",
            name="uq_reservation_consumption_allocation_generation_idempotency",
        ),
        sa.UniqueConstraint(
            "ledger_generation_id",
            "reservation_id",
            "sle_id",
            name="uq_res_consumption_generation_sle_reservation",
        ),
    )
    op.create_index(
        "ix_reservation_consumption_allocation_generation",
        "reservation_consumption_allocation",
        ["ledger_generation_id"],
    )
    op.create_index(
        "ix_reservation_consumption_allocation_reservation",
        "reservation_consumption_allocation",
        ["reservation_id"],
    )
    op.create_index(
        "ix_reservation_consumption_allocation_sle",
        "reservation_consumption_allocation",
        ["sle_id"],
    )
    op.create_index(
        "ix_reservation_consumption_allocation_requirement",
        "reservation_consumption_allocation",
        ["requirement_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)
    if inspector is None or _has_table(inspector, "reservation_consumption_allocation"):
        op.drop_table("reservation_consumption_allocation")

"""add item-ledger tables (Increment 1, additive schema only)

Revision ID: 20260721_01
Revises: 20260720_03

Additive-only item-центричный двойной леджер (design §2): four ledger-1 tables
(stock_ledger_entry / stock_bin / stock_recorder_pull / stock_ledger_anchor),
three ledger-2 tables (reservation_entry / reservation_event /
reservation_coverage), and the finished_goods warehouse-policy flag (§2.5). No
logic reads or writes them yet — zero behavior change. Inspector-guarded so a
re-run of ``upgrade head`` is a no-op (idempotent); down/up both work.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260721_01"
down_revision = "20260720_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)

    existing_tables = set() if inspector is None else set(inspector.get_table_names())

    # --- stock_warehouses.is_finished_goods (§2.5) ---
    sw_columns = (
        set()
        if inspector is None
        else {row["name"] for row in inspector.get_columns("stock_warehouses")}
    )
    if inspector is None or "is_finished_goods" not in sw_columns:
        op.add_column(
            "stock_warehouses",
            sa.Column("is_finished_goods", sa.Boolean(), nullable=False, server_default="false"),
        )

    # --- stock_ledger_entry ---
    if inspector is None or "stock_ledger_entry" not in existing_tables:
        op.create_table(
            "stock_ledger_entry",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("characteristic_ref", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("organization_ref", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("warehouse_ref1c", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("qty_after", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("posting_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.Column("record_type", sa.String(length=16), nullable=False, server_default=""),
            sa.Column("movement_kind", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("recorder_type", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("recorder_ref", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("line_no", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("ingest_source", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.UniqueConstraint(
                "recorder_type", "recorder_ref", "line_no",
                name="ux_stock_ledger_entry_recorder_line",
            ),
        )
        op.create_index("ix_stock_ledger_entry_item_id", "stock_ledger_entry", ["item_id"])
        op.create_index(
            "ix_stock_ledger_entry_ledger_key",
            "stock_ledger_entry",
            ["item_id", "characteristic_ref", "organization_ref", "warehouse_ref1c", "posting_at"],
        )
        op.create_index("ix_stock_ledger_entry_posting_at", "stock_ledger_entry", ["posting_at"])
        op.create_index("ix_stock_ledger_entry_recorder", "stock_ledger_entry", ["recorder_type", "recorder_ref"])

    # --- stock_bin ---
    if inspector is None or "stock_bin" not in existing_tables:
        op.create_table(
            "stock_bin",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("characteristic_ref", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("organization_ref", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("warehouse_ref1c", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("on_hand", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("reconcile_pending_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("last_entry_id", sa.BigInteger(), nullable=True),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.UniqueConstraint(
                "item_id", "characteristic_ref", "organization_ref", "warehouse_ref1c",
                name="ux_stock_bin_ledger_key",
            ),
        )
        op.create_index("ix_stock_bin_item_id", "stock_bin", ["item_id"])
        op.create_index(
            "ix_stock_bin_ledger_key",
            "stock_bin",
            ["item_id", "characteristic_ref", "organization_ref", "warehouse_ref1c"],
        )

    # --- stock_recorder_pull ---
    if inspector is None or "stock_recorder_pull" not in existing_tables:
        op.create_table(
            "stock_recorder_pull",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("recorder_type", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("recorder_ref", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("line_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pulled"),
            sa.Column("pulled_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("recorder_type", "recorder_ref", name="ux_stock_recorder_pull_recorder"),
        )
        op.create_index("ix_stock_recorder_pull_pulled_at", "stock_recorder_pull", ["pulled_at"])

    # --- stock_ledger_anchor ---
    if inspector is None or "stock_ledger_anchor" not in existing_tables:
        op.create_table(
            "stock_ledger_anchor",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("characteristic_ref", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("organization_ref", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("warehouse_ref1c", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("anchor_period", sa.Date(), nullable=False),
            sa.Column("anchor_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.Column("balance_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="balance_seed"),
            sa.Column("entry_id", sa.BigInteger(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.UniqueConstraint(
                "item_id", "characteristic_ref", "organization_ref", "warehouse_ref1c", "anchor_period",
                name="ux_stock_ledger_anchor_key_period",
            ),
        )
        op.create_index("ix_stock_ledger_anchor_item_id", "stock_ledger_anchor", ["item_id"])
        op.create_index(
            "ix_stock_ledger_anchor_ledger_key",
            "stock_ledger_anchor",
            ["item_id", "characteristic_ref", "organization_ref", "warehouse_ref1c"],
        )

    # --- reservation_entry ---
    if inspector is None or "reservation_entry" not in existing_tables:
        op.create_table(
            "reservation_entry",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("characteristic_ref", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("organization_ref", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("planning_stock_pool", sa.String(length=64), nullable=False, server_default="default"),
            sa.Column("run_id", sa.Integer(), nullable=True),
            sa.Column("freeze_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("requirement_id", sa.Integer(), nullable=False),
            sa.Column("priority_period_from", sa.Date(), nullable=False),
            sa.Column("priority_period_to", sa.Date(), nullable=False),
            sa.Column("realization_mode", sa.String(length=10), nullable=False, server_default="consume"),
            sa.Column("reserved_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("realized_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("covered_on_hand_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("covered_incoming_supplier_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("covered_incoming_wip_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("uncovered_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("lifecycle_status", sa.String(length=20), nullable=False, server_default="active"),
            sa.Column("coverage_state", sa.String(length=20), nullable=False, server_default="uncovered"),
            sa.Column("opened_at", sa.TIMESTAMP(), nullable=True),
            sa.Column("closed_at", sa.TIMESTAMP(), nullable=True),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.ForeignKeyConstraint(["run_id"], ["planning_run.run_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["requirement_id"], ["mrp_requirement.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("requirement_id", "realization_mode", name="ux_reservation_entry_req_mode"),
        )
        op.create_index("ix_reservation_entry_item_id", "reservation_entry", ["item_id"])
        op.create_index("ix_reservation_entry_run_id", "reservation_entry", ["run_id"])
        op.create_index("ix_reservation_entry_requirement", "reservation_entry", ["requirement_id"])
        op.create_index("ix_reservation_entry_run_version", "reservation_entry", ["run_id", "freeze_version"])
        op.create_index(
            "ix_reservation_entry_pool",
            "reservation_entry",
            ["item_id", "characteristic_ref", "organization_ref", "planning_stock_pool", "lifecycle_status"],
        )

    # --- reservation_event ---
    if inspector is None or "reservation_event" not in existing_tables:
        op.create_table(
            "reservation_event",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("reservation_id", sa.BigInteger(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("characteristic_ref", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("organization_ref", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("planning_stock_pool", sa.String(length=64), nullable=False, server_default="default"),
            sa.Column("event_kind", sa.String(length=20), nullable=False, server_default=""),
            sa.Column("reserved_delta", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("realized_delta", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("sle_id", sa.BigInteger(), nullable=True),
            sa.Column("fact_ref", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("fact_line_ref", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("match_rule", sa.String(length=20), nullable=False, server_default=""),
            sa.Column("cycle_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("idempotency_key", sa.String(length=120), nullable=False),
            sa.Column("event_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["reservation_id"], ["reservation_entry.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.ForeignKeyConstraint(["sle_id"], ["stock_ledger_entry.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("idempotency_key", name="ux_reservation_event_idempotency"),
        )
        op.create_index("ix_reservation_event_item_id", "reservation_event", ["item_id"])
        op.create_index("ix_reservation_event_reservation", "reservation_event", ["reservation_id"])
        op.create_index("ix_reservation_event_sle", "reservation_event", ["sle_id"])
        op.create_index(
            "ix_reservation_event_pool",
            "reservation_event",
            ["item_id", "characteristic_ref", "organization_ref", "planning_stock_pool"],
        )

    # --- reservation_coverage ---
    if inspector is None or "reservation_coverage" not in existing_tables:
        op.create_table(
            "reservation_coverage",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("reservation_id", sa.BigInteger(), nullable=False),
            sa.Column("source_kind", sa.String(length=20), nullable=False, server_default=""),
            sa.Column("source_ref", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("source_line_ref", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("pin_kind", sa.String(length=10), nullable=False, server_default="floating"),
            sa.Column("alloc_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("fact_at_freeze", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("covered_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("realized_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("evaporated_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("cycle_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("computed_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["reservation_id"], ["reservation_entry.id"], ondelete="CASCADE"),
            sa.UniqueConstraint(
                "reservation_id", "source_kind", "source_ref", "source_line_ref", "pin_kind",
                name="ux_reservation_coverage_source",
            ),
        )
        op.create_index("ix_reservation_coverage_reservation", "reservation_coverage", ["reservation_id"])


def downgrade() -> None:
    # Drop in reverse FK-dependency order.
    op.drop_table("reservation_coverage")
    op.drop_table("reservation_event")
    op.drop_table("reservation_entry")
    op.drop_table("stock_ledger_anchor")
    op.drop_table("stock_recorder_pull")
    op.drop_table("stock_bin")
    op.drop_table("stock_ledger_entry")
    op.drop_column("stock_warehouses", "is_finished_goods")

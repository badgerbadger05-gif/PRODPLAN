"""Collapse reservations to one reserve plus immutable replenishment demand.

Revision ID: 20260726_01
Revises: 20260725_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_01"
down_revision = "20260725_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reservation_entry",
        sa.Column(
            "covered_from_stock_at_freeze_qty",
            sa.DECIMAL(15, 3),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "reservation_entry",
        sa.Column(
            "replenishment_required_qty",
            sa.DECIMAL(15, 3),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "reservation_entry",
        sa.Column(
            "replenishment_received_qty",
            sa.DECIMAL(15, 3),
            nullable=False,
            server_default="0",
        ),
    )

    # Ledger generations are rebuildable projections. Keep exactly the
    # replenishment row (make/buy) and remove the parallel consume calculator.
    op.execute(
        """
        DELETE FROM reservation_entry
        WHERE realization_mode = 'consume'
          AND EXISTS (
              SELECT 1
              FROM reservation_entry sibling
              WHERE sibling.ledger_generation_id =
                    reservation_entry.ledger_generation_id
                AND sibling.requirement_id = reservation_entry.requirement_id
                AND sibling.realization_mode IN ('make', 'buy')
          )
        """
    )
    # Перенос данных ниже написан на диалекте PostgreSQL (UPDATE ... FROM).
    # На чистой БД переносить нечего, поэтому на других диалектах (SQLite в
    # тесте воспроизводимости схемы) блок пропускается; схема от него не зависит.
    if op.get_bind().dialect.name == "postgresql":
        _backfill_replenishment_rows()

    op.drop_constraint(
        "ux_reservation_entry_req_mode",
        "reservation_entry",
        type_="unique",
    )
    op.create_unique_constraint(
        "ux_reservation_entry_requirement",
        "reservation_entry",
        ["ledger_generation_id", "requirement_id"],
    )
    op.create_check_constraint(
        "ck_reservation_entry_replenishment_flow",
        "reservation_entry",
        "realization_mode IN ('make', 'buy')",
    )


def _backfill_replenishment_rows() -> None:
    """PostgreSQL-only data migration (UPDATE ... FROM)."""
    op.execute(
        """
        UPDATE reservation_entry r
        SET realization_mode = CASE
            WHEN lower(coalesce(
                    (SELECT replenishment_method FROM items WHERE item_id = req.item_id),
                    (SELECT replenishment_method FROM items WHERE item_id = r.item_id),
                    ''
                )) LIKE '%покуп%'
                OR lower(coalesce(
                    (SELECT replenishment_method FROM items WHERE item_id = req.item_id),
                    (SELECT replenishment_method FROM items WHERE item_id = r.item_id),
                    ''
                )) LIKE '%закуп%'
                OR lower(coalesce(
                    (SELECT replenishment_method FROM items WHERE item_id = req.item_id),
                    (SELECT replenishment_method FROM items WHERE item_id = r.item_id),
                    ''
                )) LIKE '%purchase%'
                OR lower(coalesce(
                    (SELECT replenishment_method FROM items WHERE item_id = req.item_id),
                    (SELECT replenishment_method FROM items WHERE item_id = r.item_id),
                    ''
                )) LIKE '%buy%'
                THEN 'buy'
            ELSE 'make'
        END
        FROM mrp_requirement req
        WHERE req.id = r.requirement_id
          AND r.realization_mode = 'consume'
        """
    )
    op.execute(
        """
        UPDATE reservation_entry r
        SET reserved_qty = CASE
                WHEN COALESCE(req.total_required_qty, 0) > 0 THEN req.total_required_qty
                ELSE 0
            END,
            covered_from_stock_at_freeze_qty = CASE
                WHEN COALESCE(req.total_required_qty, 0) > COALESCE(req.net_required_qty, 0)
                    THEN COALESCE(req.total_required_qty, 0) - COALESCE(req.net_required_qty, 0)
                ELSE 0
            END,
            replenishment_required_qty = CASE
                WHEN COALESCE(req.net_required_qty, 0) > 0 THEN req.net_required_qty
                ELSE 0
            END,
            replenishment_received_qty = CASE
                WHEN COALESCE(req.net_required_qty, 0) <= 0 THEN 0
                WHEN COALESCE(r.realized_qty, 0) <= 0 THEN 0
                WHEN COALESCE(r.realized_qty, 0) >= COALESCE(req.net_required_qty, 0)
                    THEN req.net_required_qty
                ELSE r.realized_qty
            END,
            realized_qty = CASE
                WHEN COALESCE(req.net_required_qty, 0) <= 0 THEN 0
                WHEN COALESCE(r.realized_qty, 0) <= 0 THEN 0
                WHEN COALESCE(r.realized_qty, 0) >= COALESCE(req.net_required_qty, 0)
                    THEN req.net_required_qty
                ELSE r.realized_qty
            END
        FROM mrp_requirement req
        WHERE req.id = r.requirement_id
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_reservation_entry_replenishment_flow",
        "reservation_entry",
        type_="check",
    )
    op.drop_constraint(
        "ux_reservation_entry_requirement",
        "reservation_entry",
        type_="unique",
    )
    op.create_unique_constraint(
        "ux_reservation_entry_req_mode",
        "reservation_entry",
        ["ledger_generation_id", "requirement_id", "realization_mode"],
    )
    op.drop_column("reservation_entry", "replenishment_received_qty")
    op.drop_column("reservation_entry", "replenishment_required_qty")
    op.drop_column("reservation_entry", "covered_from_stock_at_freeze_qty")

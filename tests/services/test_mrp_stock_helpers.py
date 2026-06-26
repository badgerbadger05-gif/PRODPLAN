"""Tests for mrp_stock_helpers — time-aware WIP and ignored-warehouse stock."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.models import (
    IgnoredWarehouse,
    Item,
    ItemWarehouseStock,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    StockWarehouse,
)
from app.services.mrp_stock_helpers import (
    active_wip_eta_by_item,
    consume_wip_at_or_before,
    effective_stock_by_item_all,
)


def _mk_item(db, *, code: str, stock: float = 0.0) -> Item:
    item = Item(
        item_code=code,
        item_name=f"Item {code}",
        item_article=code,
        unit="шт",
        stock_qty=stock,
        status="active",
    )
    db.add(item)
    db.flush()
    return item


# ---------------------------------------------------------------------------
# consume_wip_at_or_before — pure function, no DB
# ---------------------------------------------------------------------------


def test_undated_wip_is_consumed_first_and_late_wip_does_not_help_early_buckets():
    wip = [(None, 5.0), (date(2026, 8, 1), 10.0)]
    # Early bucket can only touch undated WIP.
    residual = consume_wip_at_or_before(wip, date(2026, 7, 15), qty_needed=12.0)
    assert residual == 7.0
    assert wip == [(None, 0.0), (date(2026, 8, 1), 10.0)]
    # Later bucket sees the dated WIP.
    residual = consume_wip_at_or_before(wip, date(2026, 9, 1), qty_needed=7.0)
    assert residual == 0.0
    assert wip == [(None, 0.0), (date(2026, 8, 1), 3.0)]


def test_wip_consumed_chronologically_when_multiple_etas_qualify():
    wip = [(date(2026, 7, 1), 4.0), (date(2026, 8, 1), 6.0)]
    residual = consume_wip_at_or_before(wip, date(2026, 9, 1), qty_needed=7.0)
    assert residual == 0.0
    # Earliest ETA exhausted first, then later ETA consumes the rest.
    assert wip == [(date(2026, 7, 1), 0.0), (date(2026, 8, 1), 3.0)]


def test_empty_wip_returns_full_qty_unchanged():
    wip: list = []
    residual = consume_wip_at_or_before(wip, date(2026, 7, 15), qty_needed=4.0)
    assert residual == 4.0


# ---------------------------------------------------------------------------
# effective_stock_by_item_all — DB-driven, excludes ignored warehouses
# ---------------------------------------------------------------------------


def test_effective_stock_returns_aggregated_when_no_ignored_warehouses(db_session):
    db = db_session
    a = _mk_item(db, code="STK-A", stock=100.0)
    b = _mk_item(db, code="STK-B", stock=50.0)
    db.commit()

    result = effective_stock_by_item_all(db)
    assert result[a.item_id] == 100.0
    assert result[b.item_id] == 50.0


def test_effective_stock_subtracts_ignored_warehouses(db_session):
    db = db_session
    # Item with 100 total: 30 in a "normal" warehouse, 70 in brak isolator.
    item = _mk_item(db, code="STK-IGN", stock=100.0)

    db.add(StockWarehouse(warehouse_ref1c="wh-normal", warehouse_name="Normal", is_selected=True))
    db.add(StockWarehouse(warehouse_ref1c="wh-isolator", warehouse_name="Brak", is_selected=True))
    db.add(IgnoredWarehouse(warehouse_ref1c="wh-isolator", warehouse_name="Brak", reason="defective"))
    db.add(ItemWarehouseStock(item_id=item.item_id, warehouse_ref1c="wh-normal", qty=30.0))
    db.add(ItemWarehouseStock(item_id=item.item_id, warehouse_ref1c="wh-isolator", qty=70.0))
    db.commit()

    result = effective_stock_by_item_all(db)
    # Only the non-ignored warehouse counts.
    assert result[item.item_id] == 30.0


def test_effective_stock_falls_back_to_item_stock_qty_for_unbroken_items(db_session):
    """Items without any item_warehouse_stock rows fall back to Item.stock_qty
    so a partially-synced DB doesn't blank coverage."""
    db = db_session
    has_breakdown = _mk_item(db, code="STK-WB", stock=100.0)
    no_breakdown = _mk_item(db, code="STK-NB", stock=42.0)

    db.add(IgnoredWarehouse(warehouse_ref1c="wh-x", warehouse_name="X", reason="brak"))
    db.add(ItemWarehouseStock(item_id=has_breakdown.item_id, warehouse_ref1c="wh-y", qty=10.0))
    db.commit()

    result = effective_stock_by_item_all(db)
    assert result[has_breakdown.item_id] == 10.0  # via breakdown
    assert result[no_breakdown.item_id] == 42.0   # via fallback


# ---------------------------------------------------------------------------
# active_wip_eta_by_item — DB-driven, time-aware WIP
# ---------------------------------------------------------------------------


def _mk_active_wip(
    db,
    item: Item,
    *,
    remaining: float,
    planned_finish: date | None,
    order_state_key: str | None = None,
    deletion_mark: bool = False,
) -> ProductionProduct:
    order = ProductionOrder(
        order_number=f"WIP-{item.item_id}-{remaining}-{planned_finish}",
        order_date=datetime(2026, 5, 20),
        is_posted=True,
        deletion_mark=deletion_mark,
        order_state_key=order_state_key,
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=remaining,
        produced_qty=0,
        remaining_qty=remaining,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="ready",
            issue_status="not_requested",
            planned_finish_date=planned_finish,
        )
    )
    return product


def test_active_wip_eta_includes_remaining_and_finish_date(db_session):
    db = db_session
    item = _mk_item(db, code="WIP-ETA", stock=0.0)
    _mk_active_wip(db, item, remaining=4.0, planned_finish=date(2026, 8, 1))
    _mk_active_wip(db, item, remaining=6.0, planned_finish=None)
    db.commit()

    result = active_wip_eta_by_item(db)
    entries = result[item.item_id]
    # Sorted with None (date.min) first, then by ETA asc.
    assert entries == [(None, 6.0), (date(2026, 8, 1), 4.0)]


def test_active_wip_eta_skips_deleted_but_includes_done_orders(db_session):
    db = db_session
    item = _mk_item(db, code="WIP-FILT", stock=0.0)
    _mk_active_wip(db, item, remaining=4.0, planned_finish=date(2026, 8, 1))
    # deleted — must not appear
    _mk_active_wip(db, item, remaining=100.0, planned_finish=None, deletion_mark=True)
    # done — still counts as supply for repeated MRP calculations
    _mk_active_wip(
        db, item, remaining=50.0, planned_finish=None,
        order_state_key="ad28565a-991b-11eb-e39a-fa163e61326a",
    )
    db.commit()

    result = active_wip_eta_by_item(db)
    entries = result.get(item.item_id, [])
    assert entries == [(None, 50.0), (date(2026, 8, 1), 4.0)]


def test_active_wip_eta_skips_zero_remaining(db_session):
    db = db_session
    item = _mk_item(db, code="WIP-Z", stock=0.0)
    product = _mk_active_wip(db, item, remaining=5.0, planned_finish=None)
    product.remaining_qty = 0  # fully produced
    db.commit()

    result = active_wip_eta_by_item(db)
    assert item.item_id not in result


def test_active_wip_eta_counts_completed_zero_remaining_by_produced_qty(db_session):
    db = db_session
    item = _mk_item(db, code="WIP-DONE-Z", stock=0.0)
    product = _mk_active_wip(
        db,
        item,
        remaining=5.0,
        planned_finish=None,
        order_state_key="ad28565a-991b-11eb-e39a-fa163e61326a",
    )
    product.produced_qty = 5.0
    product.remaining_qty = 0.0
    db.commit()

    result = active_wip_eta_by_item(db)
    assert result[item.item_id] == [(None, 5.0)]

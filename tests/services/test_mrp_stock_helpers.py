"""Tests for mrp_stock_helpers — time-aware WIP and ignored-warehouse stock."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app import models
from app.models import (
    IgnoredWarehouse,
    Item,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    StockWarehouse,
)
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C
from app.services.planning_truth import PlanningTruthUnavailable
from app.services.mrp_stock_helpers import (
    active_wip_eta_by_item,
    consume_wip_at_or_before,
    effective_stock_by_item_all,
    planning_stock_by_item,
)


def _mk_item(db, *, code: str, stock: float = 0.0) -> Item:
    item = Item(
        item_code=code,
        item_name=f"Item {code}",
        item_article=code,
        unit="шт",
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


def test_effective_stock_reads_only_accepted_generation_and_contour(
    db_session,
    building_ledger_generation,
):
    db = db_session
    item = _mk_item(db, code="STK-IGN", stock=100.0)
    db.add(StockWarehouse(warehouse_ref1c="wh-normal", warehouse_name="Normal", is_selected=True))
    db.add(StockWarehouse(warehouse_ref1c="wh-isolator", warehouse_name="Brak", is_selected=True))
    db.add(IgnoredWarehouse(warehouse_ref1c="wh-isolator", warehouse_name="Brak", reason="defective"))
    db.add_all([
        models.StockBin(
            ledger_generation_id=building_ledger_generation.id,
            item_id=item.item_id,
            organization_ref=DEFAULT_ORGANIZATION_REF1C,
            warehouse_ref1c="wh-normal",
            on_hand=30,
        ),
        models.StockBin(
            ledger_generation_id=building_ledger_generation.id,
            item_id=item.item_id,
            organization_ref="7f9a3a7a-c7f1-11ed-a8cd-0242ac100014",
            warehouse_ref1c="wh-isolator",
            on_hand=70,
        ),
    ])
    building_ledger_generation.status = "accepted"
    building_ledger_generation.accepted_at = datetime(2026, 7, 26)
    building_ledger_generation.cutoff = datetime(2026, 7, 26)
    pointer = db.get(models.PlanningTruthState, 1)
    pointer.current_generation_id = building_ledger_generation.id
    db.flush()

    result = effective_stock_by_item_all(db)
    assert result[item.item_id] == 30.0


def test_effective_stock_excludes_foreign_organization_stock(db_session, building_ledger_generation):
    db = db_session
    item = _mk_item(db, code="STK-FORG", stock=0.0)
    db.add(StockWarehouse(warehouse_ref1c="wh-normal", warehouse_name="Normal", is_selected=True))
    db.add_all([
        models.StockBin(
            ledger_generation_id=building_ledger_generation.id,
            item_id=item.item_id,
            organization_ref=DEFAULT_ORGANIZATION_REF1C,
            warehouse_ref1c="wh-normal",
            on_hand=12,
        ),
        models.StockBin(
            ledger_generation_id=building_ledger_generation.id,
            item_id=item.item_id,
            organization_ref="7f9a3a7a-c7f1-11ed-a8cd-0242ac100015",
            warehouse_ref1c="wh-normal",
            on_hand=99,
        ),
    ])
    building_ledger_generation.status = "accepted"
    building_ledger_generation.accepted_at = datetime(2026, 7, 26)
    building_ledger_generation.cutoff = datetime(2026, 7, 26)
    pointer = db.get(models.PlanningTruthState, 1)
    pointer.current_generation_id = building_ledger_generation.id
    db.flush()

    result = effective_stock_by_item_all(db)
    assert result[item.item_id] == 12.0


def test_planning_stock_is_empty_when_warehouse_policy_selects_nothing(
    db_session,
    building_ledger_generation,
):
    item = _mk_item(db_session, code="STK-NONE", stock=0.0)
    db_session.add(
        StockWarehouse(
            warehouse_ref1c="wh-unselected",
            warehouse_name="Not in contour",
            is_selected=False,
        )
    )
    db_session.add(
        models.StockBin(
            ledger_generation_id=building_ledger_generation.id,
            item_id=item.item_id,
            organization_ref=DEFAULT_ORGANIZATION_REF1C,
            warehouse_ref1c="wh-unselected",
            on_hand=25,
        )
    )
    db_session.flush()

    assert planning_stock_by_item(
        db_session,
        int(building_ledger_generation.id),
    ) == {}


def test_effective_stock_fails_closed_without_published_ledger(db_session):
    _mk_item(db_session, code="STK-NO-TRUTH", stock=42.0)
    with pytest.raises(PlanningTruthUnavailable):
        effective_stock_by_item_all(db_session)


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


def test_active_wip_eta_skips_deleted_and_done_orders(db_session):
    db = db_session
    item = _mk_item(db, code="WIP-FILT", stock=0.0)
    _mk_active_wip(db, item, remaining=4.0, planned_finish=date(2026, 8, 1))
    # deleted — must not appear
    _mk_active_wip(db, item, remaining=100.0, planned_finish=None, deletion_mark=True)
    # Done output must arrive through warehouse stock, never WIP.
    _mk_active_wip(
        db, item, remaining=50.0, planned_finish=None,
        order_state_key="ad28565a-991b-11eb-e39a-fa163e61326a",
    )
    db.commit()

    result = active_wip_eta_by_item(db)
    entries = result.get(item.item_id, [])
    assert entries == [(date(2026, 8, 1), 4.0)]


def test_active_wip_eta_ignores_corrupt_zero_remaining_cache(db_session):
    db = db_session
    item = _mk_item(db, code="WIP-Z", stock=0.0)
    product = _mk_active_wip(db, item, remaining=5.0, planned_finish=None)
    product.remaining_qty = 0  # corrupt legacy cache: accepted produced is zero
    db.commit()

    result = active_wip_eta_by_item(db)
    assert result[item.item_id] == [(None, 5.0)]


def test_active_wip_eta_excludes_completed_zero_remaining_even_with_produced_qty(db_session):
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
    assert item.item_id not in result

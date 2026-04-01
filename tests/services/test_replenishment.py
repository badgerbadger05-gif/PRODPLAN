from __future__ import annotations

from types import SimpleNamespace

from app.models import Item, PlanningRun, PlannedOrder, PlannedPurchase, PlannedRework, Unit
from app.services.order_quantity_calculator import OrderQuantityCalculator
from app.services.planning_service import build_planned_orders_and_purchases
from app.services.replenishment import (
    REPLENISHMENT_FLOW_PRODUCTION,
    REPLENISHMENT_FLOW_PURCHASE,
    REPLENISHMENT_FLOW_REWORK,
    classify_replenishment_flow,
)


def _mk_run(db) -> PlanningRun:
    run = PlanningRun(
        status="IN_PROGRESS",
        started_by="test",
        horizon_days=10,
        pinned=False,
        config_version_id=None,
        config_snapshot={"production": {"lot_sizing": {"min_batch": 1, "multiple": 1, "rounding": "ceil"}}},
        warnings=[],
        kpi={},
    )
    db.add(run)
    db.flush()
    return run


def test_classify_replenishment_flow_detects_purchase_synonyms():
    assert classify_replenishment_flow("Закупка") == REPLENISHMENT_FLOW_PURCHASE
    assert classify_replenishment_flow("покупное изделие") == REPLENISHMENT_FLOW_PURCHASE
    assert classify_replenishment_flow("Purchase") == REPLENISHMENT_FLOW_PURCHASE
    assert classify_replenishment_flow("buy") == REPLENISHMENT_FLOW_PURCHASE


def test_classify_replenishment_flow_defaults_unknown_values_to_production_and_detects_rework():
    assert classify_replenishment_flow(None) == REPLENISHMENT_FLOW_PRODUCTION
    assert classify_replenishment_flow("") == REPLENISHMENT_FLOW_PRODUCTION
    assert classify_replenishment_flow("Производство") == REPLENISHMENT_FLOW_PRODUCTION
    assert classify_replenishment_flow("Переработка") == REPLENISHMENT_FLOW_REWORK
    assert classify_replenishment_flow("unknown-method") == REPLENISHMENT_FLOW_PRODUCTION


def test_build_planned_orders_uses_shared_purchase_classifier(db_session):
    db = db_session

    unit = Unit(unit_ref1c="u", unit_name="шт", short_name="шт", precision=0)
    db.add(unit)

    item = Item(
        item_code="BUY-1",
        item_name="Buy item",
        item_article="BUY-1",
        replenishment_method="Закупка",
        replenishment_time=5,
        unit="u",
        stock_qty=0,
        status="active",
    )
    db.add(item)
    db.flush()

    run = _mk_run(db)
    item_cache = {item.item_id: item}
    units_by_ref = {"u": unit}
    calc = OrderQuantityCalculator(
        snapshot=run.config_snapshot,
        default_spec_map={},
        spec_by_id={},
        components_loader=lambda _sid: [],
        item_by_id=item_cache,
        units_by_ref=units_by_ref,
        res_by_id={},
        production_kinds_by_resource={},
        stock_by_item={item.item_id: 0.0},
        wip_by_item={},
        horizon_days=run.horizon_days or 0,
        total_demand_by_item={item.item_id: 4.0},
    )

    build_planned_orders_and_purchases(
        db,
        run,
        {str(item.item_id): {"2025-01-10": 4.0}},
        calc,
        priority_manager=SimpleNamespace(),
        item_cache=item_cache,
        units_by_ref=units_by_ref,
    )

    assert db.query(PlannedOrder).filter(PlannedOrder.run_id == run.run_id).count() == 0
    row = db.query(PlannedPurchase).filter(PlannedPurchase.run_id == run.run_id).one()
    assert float(row.requested_qty) == 4.0
    assert float(row.planned_qty) == 4.0
    assert row.lead_time_days == 5


def test_build_planned_orders_keeps_production_flow_for_regular_items(db_session):
    db = db_session

    unit = Unit(unit_ref1c="u-prod", unit_name="шт", short_name="шт", precision=0)
    db.add(unit)

    item = Item(
        item_code="PROD-1",
        item_name="Production item",
        item_article="PROD-1",
        replenishment_method="Производство",
        unit="u-prod",
        stock_qty=0,
        status="active",
    )
    db.add(item)
    db.flush()

    run = _mk_run(db)
    item_cache = {item.item_id: item}
    units_by_ref = {"u-prod": unit}
    calc = OrderQuantityCalculator(
        snapshot=run.config_snapshot,
        default_spec_map={},
        spec_by_id={},
        components_loader=lambda _sid: [],
        item_by_id=item_cache,
        units_by_ref=units_by_ref,
        res_by_id={},
        production_kinds_by_resource={},
        stock_by_item={item.item_id: 0.0},
        wip_by_item={},
        horizon_days=run.horizon_days or 0,
        total_demand_by_item={item.item_id: 4.0},
    )

    build_planned_orders_and_purchases(
        db,
        run,
        {str(item.item_id): {"2025-01-10": 4.0}},
        calc,
        priority_manager=SimpleNamespace(),
        item_cache=item_cache,
        units_by_ref=units_by_ref,
    )

    row = db.query(PlannedOrder).filter(PlannedOrder.run_id == run.run_id).one()
    assert float(row.requested_qty) == 4.0
    assert float(row.planned_qty) == 4.0
    assert db.query(PlannedPurchase).filter(PlannedPurchase.run_id == run.run_id).count() == 0
    assert db.query(PlannedRework).filter(PlannedRework.run_id == run.run_id).count() == 0

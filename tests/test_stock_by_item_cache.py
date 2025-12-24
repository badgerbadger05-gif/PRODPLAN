import datetime

from types import SimpleNamespace


from app.models import (
    Item,
    Unit,
    Specification,
    DefaultSpecification,
    SpecComponent,
    PlanningRun,
    PlannedOrder,
)

from app.services.order_quantity_calculator import OrderQuantityCalculator
from app.services.planning_service import build_planned_orders_and_purchases


def _mk_run(db, snapshot=None) -> PlanningRun:
    run = PlanningRun(
        status="IN_PROGRESS",
        started_by="test",
        horizon_days=10,
        pinned=False,
        config_version_id=None,
        config_snapshot=snapshot or {"production": {"lot_sizing": {"min_batch": 1, "multiple": 1, "rounding": "ceil"}}},
        warnings=[],
        kpi={},
        started_at=datetime.datetime.utcnow(),
    )
    db.add(run)
    db.flush()
    return run


def test_parent_can_be_falsely_blocked_when_component_stock_missing_in_cache(db_session):
    db = db_session

    # Unit (discrete)
    u = Unit(unit_ref1c="u", unit_name="шт", short_name="шт", precision=0)
    db.add(u)

    # Items
    parent = Item(
        item_code="PARENT",
        item_name="Parent",
        item_article="PARENT",
        replenishment_method="Производство",
        unit="u",
        stock_qty=0,
        status="active",
    )
    child = Item(
        item_code="CHILD",
        item_name="Child",
        item_article="CHILD",
        replenishment_method="Производство",
        unit="u",
        stock_qty=100,
        status="active",
    )
    db.add_all([parent, child])
    db.flush()

    # Spec + default spec + BOM: parent -> child (1 per unit)
    spec = Specification(spec_code="S", spec_name="Spec")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=child.item_id, quantity=1))
    db.flush()

    units_by_ref = {"u": u}
    item_cache = {parent.item_id: parent}
    default_spec_map = {parent.item_id: spec.spec_id}
    spec_by_id = {spec.spec_id: spec}

    def components_loader(spec_id: int):
        return db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()

    # net requirements only for parent
    net_req = {str(parent.item_id): {"2025-01-01": 10.0}}

    # --- Case 1: BUGGY/old behavior: stock_by_item built only from item_cache (child missing => treated as 0)
    run1 = _mk_run(db)
    calc_missing = OrderQuantityCalculator(
        snapshot=run1.config_snapshot,
        default_spec_map=default_spec_map,
        spec_by_id=spec_by_id,
        components_loader=components_loader,
        item_by_id=item_cache,
        units_by_ref=units_by_ref,
        res_by_id={},
        production_kinds_by_resource={},
        stock_by_item={parent.item_id: 0.0},
        wip_by_item={},
        horizon_days=run1.horizon_days or 0,
        total_demand_by_item={parent.item_id: 10.0},
    )
    out1 = build_planned_orders_and_purchases(
        db,
        run1,
        net_req,
        calc_missing,
        priority_manager=SimpleNamespace(),
        item_cache=item_cache,
        units_by_ref=units_by_ref,
    )
    assert any(w.get("code") == "STOCK_CACHE_MISS" for w in out1.get("warnings", []))
    assert db.query(PlannedOrder).filter(PlannedOrder.run_id == run1.run_id).count() == 0

    # --- Case 2: FIXED behavior: stock_by_item includes components too
    run2 = _mk_run(db)
    calc_full = OrderQuantityCalculator(
        snapshot=run2.config_snapshot,
        default_spec_map=default_spec_map,
        spec_by_id=spec_by_id,
        components_loader=components_loader,
        item_by_id=item_cache,
        units_by_ref=units_by_ref,
        res_by_id={},
        production_kinds_by_resource={},
        stock_by_item={parent.item_id: 0.0, child.item_id: 100.0},
        wip_by_item={},
        horizon_days=run2.horizon_days or 0,
        total_demand_by_item={parent.item_id: 10.0},
    )
    out2 = build_planned_orders_and_purchases(
        db,
        run2,
        net_req,
        calc_full,
        priority_manager=SimpleNamespace(),
        item_cache=item_cache,
        units_by_ref=units_by_ref,
    )
    assert not any(w.get("code") == "COMPONENT_SHORTAGE_BLOCKED" for w in out2.get("warnings", []))
    assert db.query(PlannedOrder).filter(PlannedOrder.run_id == run2.run_id).count() == 1


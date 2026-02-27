import datetime

from types import SimpleNamespace


from app.models import (
    Item,
    Unit,
    Specification,
    DefaultSpecification,
    SpecComponent,
    ProductionOrder,
    ProductionProduct,
    PlanningRun,
    PlannedOrder,
)

from app.services.order_quantity_calculator import OrderQuantityCalculator
from app.services.planning_service import (
    build_planned_orders_and_purchases,
    _build_component_reservations_from_active_1c,
    _get_active_1c_remaining_by_item,
)


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


def test_active_1c_remaining_reduces_requested_qty_for_production_order(db_session):
    db = db_session

    u = Unit(unit_ref1c="u2", unit_name="шт", short_name="шт", precision=0)
    db.add(u)

    parent = Item(
        item_code="PARENT-A",
        item_name="Parent A",
        item_article="PARENT-A",
        replenishment_method="Производство",
        unit="u2",
        stock_qty=0,
        status="active",
    )
    db.add(parent)
    db.flush()

    run = _mk_run(db)
    units_by_ref = {"u2": u}
    item_cache = {parent.item_id: parent}

    calc = OrderQuantityCalculator(
        snapshot=run.config_snapshot,
        default_spec_map={},
        spec_by_id={},
        components_loader=lambda _sid: [],
        item_by_id=item_cache,
        units_by_ref=units_by_ref,
        res_by_id={},
        production_kinds_by_resource={},
        stock_by_item={parent.item_id: 0.0},
        wip_by_item={},
        horizon_days=run.horizon_days or 0,
        total_demand_by_item={parent.item_id: 10.0},
    )

    net_req = {str(parent.item_id): {"2025-01-01": 10.0}}
    out = build_planned_orders_and_purchases(
        db,
        run,
        net_req,
        calc,
        priority_manager=SimpleNamespace(),
        item_cache=item_cache,
        units_by_ref=units_by_ref,
        active_remaining_by_item={parent.item_id: 7.0},
    )

    assert out.get("warnings") is not None
    row = db.query(PlannedOrder).filter(PlannedOrder.run_id == run.run_id).one()
    assert float(row.requested_qty) == 3.0
    assert float(row.planned_qty) == 3.0


def test_get_active_1c_remaining_by_item_filters_done_deleted_and_nonpositive(db_session):
    db = db_session

    item = Item(
        item_code="REM-ITEM",
        item_name="Remaining Item",
        item_article="REM-ITEM",
        replenishment_method="Производство",
        unit="u",
        stock_qty=0,
        status="active",
    )
    db.add(item)
    db.flush()

    active = ProductionOrder(
        order_number="A-1",
        order_date=datetime.datetime(2026, 1, 1),
        order_ref1c="A-1-ref",
        deletion_mark=False,
        order_state_key="some-active-state",
        is_posted=True,
    )
    done = ProductionOrder(
        order_number="D-1",
        order_date=datetime.datetime(2026, 1, 1),
        order_ref1c="D-1-ref",
        deletion_mark=False,
        order_state_key="ad28565a-991b-11eb-e39a-fa163e61326a",
        is_posted=True,
    )
    deleted = ProductionOrder(
        order_number="X-1",
        order_date=datetime.datetime(2026, 1, 1),
        order_ref1c="X-1-ref",
        deletion_mark=True,
        order_state_key="some-active-state",
        is_posted=True,
    )
    db.add_all([active, done, deleted])
    db.flush()

    db.add_all(
        [
            ProductionProduct(order_id=active.order_id, item_id=item.item_id, quantity=10.0, produced_qty=5.0, remaining_qty=5.0),
            ProductionProduct(order_id=done.order_id, item_id=item.item_id, quantity=10.0, produced_qty=3.0, remaining_qty=7.0),
            ProductionProduct(order_id=deleted.order_id, item_id=item.item_id, quantity=10.0, produced_qty=1.0, remaining_qty=9.0),
            ProductionProduct(order_id=active.order_id, item_id=item.item_id, quantity=10.0, produced_qty=10.0, remaining_qty=0.0),
        ]
    )
    db.commit()

    rem = _get_active_1c_remaining_by_item(db)
    assert rem.get(item.item_id) == 5.0


def test_recursive_component_reservation_with_cycle_guard(db_session):
    db = db_session

    a = Item(item_code="A-CYC", item_name="A", item_article="A", replenishment_method="Производство", unit="u", stock_qty=0, status="active")
    b = Item(item_code="B-CYC", item_name="B", item_article="B", replenishment_method="Производство", unit="u", stock_qty=0, status="active")
    c = Item(item_code="C-CYC", item_name="C", item_article="C", replenishment_method="Производство", unit="u", stock_qty=0, status="active")
    db.add_all([a, b, c])
    db.flush()

    spec_a = Specification(spec_code="S-A", spec_name="Spec A")
    spec_b = Specification(spec_code="S-B", spec_name="Spec B")
    db.add_all([spec_a, spec_b])
    db.flush()

    db.add_all(
        [
            DefaultSpecification(item_id=a.item_id, spec_id=spec_a.spec_id),
            DefaultSpecification(item_id=b.item_id, spec_id=spec_b.spec_id),
            SpecComponent(spec_id=spec_a.spec_id, item_id=b.item_id, quantity=2.0),
            SpecComponent(spec_id=spec_b.spec_id, item_id=c.item_id, quantity=3.0),
            SpecComponent(spec_id=spec_b.spec_id, item_id=a.item_id, quantity=1.0),  # cycle edge B -> A
        ]
    )

    order = ProductionOrder(
        order_number="CYC-1",
        order_date=datetime.datetime(2026, 1, 2),
        order_ref1c="CYC-1-ref",
        deletion_mark=False,
        order_state_key="active-state",
        is_posted=True,
    )
    db.add(order)
    db.flush()
    db.add(
        ProductionProduct(
            order_id=order.order_id,
            item_id=a.item_id,
            quantity=10.0,
            produced_qty=8.0,
            remaining_qty=2.0,
        )
    )
    db.commit()

    default_spec_map = {a.item_id: spec_a.spec_id, b.item_id: spec_b.spec_id}

    def components_loader(spec_id: int):
        return db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()

    reserved, warnings = _build_component_reservations_from_active_1c(
        db=db,
        default_spec_map=default_spec_map,
        components_loader=components_loader,
        max_depth=20,
    )

    # A remaining=2 => reserve B=2*2=4, then C=4*3=12; cycle B->A is skipped.
    assert reserved.get(b.item_id) == 4.0
    assert reserved.get(c.item_id) == 12.0
    assert not reserved.get(a.item_id)
    assert any(w.get("code") == "ACTIVE_1C_BOM_CYCLE_SKIPPED" for w in warnings)


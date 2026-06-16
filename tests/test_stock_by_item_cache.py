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
    SupplierOrder,
    SupplierOrderItem,
    PlanningRun,
    PlannedOrder,
    PlannedPurchase,
    PlannedRework,
    ProductionKind,
    ProductionPlanEntry,
    ProductionResource,
    ProductionStage,
    ResourceProductionKind,
)

from app.services.order_quantity_calculator import OrderQuantityCalculator
from app.services.planning_service import (
    build_planned_orders_and_purchases,
    compute_planning_preview,
    get_run_purchases,
    _build_component_reservations_from_active_1c,
    _get_active_1c_remaining_by_item,
    _get_active_production_remaining_by_item,
    _get_active_supplier_remaining_by_item_date,
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


def test_component_limit_is_cumulative_across_multiple_buckets(db_session):
    db = db_session

    u = Unit(unit_ref1c="u-cum", unit_name="шт", short_name="шт", precision=0)
    db.add(u)

    parent = Item(
        item_code="PARENT-CUM",
        item_name="Parent Cumulative",
        item_article="PARENT-CUM",
        replenishment_method="Производство",
        unit="u-cum",
        stock_qty=0,
        status="active",
    )
    child = Item(
        item_code="CHILD-CUM",
        item_name="Child Cumulative",
        item_article="CHILD-CUM",
        replenishment_method="Производство",
        unit="u-cum",
        stock_qty=2,
        status="active",
    )
    db.add_all([parent, child])
    db.flush()

    spec = Specification(spec_code="S-CUM", spec_name="Spec Cumulative")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=child.item_id, quantity=1.0))
    db.flush()

    run = _mk_run(db)
    units_by_ref = {"u-cum": u}
    item_cache = {parent.item_id: parent, child.item_id: child}

    def components_loader(spec_id: int):
        return db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()

    calc = OrderQuantityCalculator(
        snapshot=run.config_snapshot,
        default_spec_map={parent.item_id: spec.spec_id},
        spec_by_id={spec.spec_id: spec},
        components_loader=components_loader,
        item_by_id=item_cache,
        units_by_ref=units_by_ref,
        res_by_id={},
        production_kinds_by_resource={},
        stock_by_item={parent.item_id: 0.0, child.item_id: 2.0},
        wip_by_item={},
        horizon_days=run.horizon_days or 0,
        total_demand_by_item={parent.item_id: 10.0},
    )

    out = build_planned_orders_and_purchases(
        db,
        run,
        {
            str(parent.item_id): {
                "2025-01-01": 5.0,
                "2025-01-08": 5.0,
            }
        },
        calc,
        priority_manager=SimpleNamespace(),
        item_cache=item_cache,
        units_by_ref=units_by_ref,
    )

    rows = (
        db.query(PlannedOrder)
        .filter(PlannedOrder.run_id == run.run_id, PlannedOrder.item_id == parent.item_id)
        .order_by(PlannedOrder.need_date.asc())
        .all()
    )
    assert len(rows) == 1
    assert float(rows[0].planned_qty) == 2.0
    assert sum(float(r.planned_qty) for r in rows) == 2.0
    assert any(w.get("code") == "COMPONENT_SHORTAGE_PARTIAL" for w in out.get("warnings", []))
    assert any(w.get("code") == "COMPONENT_SHORTAGE_BLOCKED" for w in out.get("warnings", []))


def test_active_1c_remaining_already_netted_upstream_not_double_subtracted(db_session):
    """WIP netting is the responsibility of compute_planning_preview, which
    chronologically subtracts active production remaining_qty from gross
    demand and passes the residual under `net_requirements`. The
    build_planned_orders_and_purchases function must NOT subtract WIP again
    or it would double-count (and worse: read the same dict for every
    bucket without consuming it). The `active_remaining_by_item` argument
    is kept on the signature for backward compatibility but no longer
    influences production-flow requested_qty."""
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
        total_demand_by_item={parent.item_id: 3.0},
    )

    # Gross demand was 10; upstream WIP netting already subtracted 7 active
    # remaining. net_requirements carries the post-WIP residual 3.0.
    net_req = {str(parent.item_id): {"2025-01-01": 3.0}}
    out = build_planned_orders_and_purchases(
        db,
        run,
        net_req,
        calc,
        priority_manager=SimpleNamespace(),
        item_cache=item_cache,
        units_by_ref=units_by_ref,
        # Even with active_remaining_by_item passed, the function should NOT
        # subtract again — that would yield a PlannedOrder of qty 0.
        active_remaining_by_item={parent.item_id: 7.0},
    )

    assert out.get("warnings") is not None
    row = db.query(PlannedOrder).filter(PlannedOrder.run_id == run.run_id).one()
    assert float(row.requested_qty) == 3.0
    assert float(row.planned_qty) == 3.0


def test_purchase_flow_normalizes_fractional_qty_for_discrete_units(db_session):
    db = db_session

    u = Unit(unit_ref1c="u-pcs", unit_name="шт", short_name="шт", precision=0)
    db.add(u)

    item = Item(
        item_code="BUY-DISCRETE",
        item_name="Buy Discrete",
        item_article="BUY-DISCRETE",
        replenishment_method="Покупка",
        replenishment_time=5,
        unit="u-pcs",
        stock_qty=0,
        status="active",
    )
    db.add(item)
    db.flush()

    run = _mk_run(db)
    units_by_ref = {"u-pcs": u}
    item_cache = {item.item_id: item}

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
        total_demand_by_item={item.item_id: 7.9},
    )

    net_req = {str(item.item_id): {"2025-01-01": 7.9}}
    build_planned_orders_and_purchases(
        db,
        run,
        net_req,
        calc,
        priority_manager=SimpleNamespace(),
        item_cache=item_cache,
        units_by_ref=units_by_ref,
    )

    row = db.query(PlannedPurchase).filter(PlannedPurchase.run_id == run.run_id).one()
    assert float(row.requested_qty) == 7.0
    assert float(row.planned_qty) == 7.0
    assert float(row.qty) == 7.0


def test_purchase_flow_preserves_fractional_qty_for_metric_units(db_session):
    db = db_session

    u = Unit(unit_ref1c="u-kg", unit_name="кг", short_name="кг", precision=3)
    db.add(u)

    item = Item(
        item_code="BUY-METRIC",
        item_name="Buy Metric",
        item_article="BUY-METRIC",
        replenishment_method="Покупка",
        replenishment_time=5,
        unit="u-kg",
        stock_qty=0,
        status="active",
    )
    db.add(item)
    db.flush()

    run = _mk_run(db)
    units_by_ref = {"u-kg": u}
    item_cache = {item.item_id: item}

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
        total_demand_by_item={item.item_id: 7.9},
    )

    net_req = {str(item.item_id): {"2025-01-01": 7.9}}
    build_planned_orders_and_purchases(
        db,
        run,
        net_req,
        calc,
        priority_manager=SimpleNamespace(),
        item_cache=item_cache,
        units_by_ref=units_by_ref,
    )

    row = db.query(PlannedPurchase).filter(PlannedPurchase.run_id == run.run_id).one()
    assert float(row.requested_qty) == 7.9
    assert float(row.planned_qty) == 7.9
    assert float(row.qty) == 7.9


def test_active_supplier_order_reduces_purchase_need_once_by_delivery_date(db_session):
    db = db_session

    u = Unit(unit_ref1c="u-sup", unit_name="шт", short_name="шт", precision=0)
    db.add(u)

    item = Item(
        item_code="BUY-SUP",
        item_name="Buy Supplier Covered",
        item_article="BUY-SUP",
        replenishment_method="Покупка",
        replenishment_time=5,
        unit="u-sup",
        stock_qty=0,
        status="active",
    )
    db.add(item)
    db.flush()

    run = _mk_run(db)
    units_by_ref = {"u-sup": u}
    item_cache = {item.item_id: item}

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
        total_demand_by_item={item.item_id: 10.0},
    )

    build_planned_orders_and_purchases(
        db,
        run,
        {
            str(item.item_id): {
                "2025-01-10": 5.0,
                "2025-01-20": 5.0,
            }
        },
        calc,
        priority_manager=SimpleNamespace(),
        item_cache=item_cache,
        units_by_ref=units_by_ref,
        supplier_remaining_by_item_date={
            item.item_id: [
                (datetime.date(2025, 1, 10), 6.0),
                (datetime.date(2025, 1, 25), 100.0),
            ]
        },
    )

    rows = (
        db.query(PlannedPurchase)
        .filter(PlannedPurchase.run_id == run.run_id)
        .order_by(PlannedPurchase.need_date.asc())
        .all()
    )
    assert len(rows) == 1
    assert rows[0].need_date.isoformat() == "2025-01-20"
    # PlannedPurchase semantics (consistent with period_plan_service):
    # - requested_qty = gross net demand BEFORE supplier-order netting,
    #   kept for the «Покрыто поставщиком» diagnostic
    #   (supplier_covered_qty = requested_qty - qty).
    # - planned_qty / qty = residual AFTER supplier coverage, this is what
    #   actually gets ordered.
    assert float(rows[0].requested_qty) == 5.0  # original bucket demand
    assert float(rows[0].planned_qty) == 4.0    # after 1 unit supplier coverage
    assert float(rows[0].qty) == 4.0


def test_purchase_results_marks_late_supplier_order_coverage(db_session):
    db = db_session

    u = Unit(unit_ref1c="u-late", unit_name="шт", short_name="шт", precision=0)
    item = Item(
        item_code="BUY-LATE",
        item_name="Buy Late Supplier",
        item_article="BUY-LATE",
        replenishment_method="Покупка",
        replenishment_time=5,
        unit="u-late",
        stock_qty=0,
        status="active",
    )
    db.add_all([u, item])
    db.flush()

    run = _mk_run(db)
    db.add(
        PlannedPurchase(
            run_id=run.run_id,
            item_id=item.item_id,
            requested_qty=5,
            planned_qty=5,
            qty=5,
            need_date=datetime.date(2025, 1, 10),
            order_date=datetime.date(2025, 1, 5),
            lead_time_days=5,
            bucket_date=datetime.date(2025, 1, 10),
        )
    )
    order = SupplierOrder(
        order_number="ЗСНФ-LATE",
        order_date=datetime.datetime(2025, 1, 1, 10, 0),
        order_ref1c="late-order-ref",
        order_state_key="state-in-work",
        order_state_name="В пути",
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    db.add(
        SupplierOrderItem(
            order_id=order.order_id,
            item_id_ref=item.item_id,
            line_number=1,
            quantity=5,
            received_qty=0,
            remaining_qty=5,
            delivery_date=datetime.datetime(2025, 1, 17),
        )
    )
    db.commit()

    result = get_run_purchases(db, run.run_id, limit=100, offset=0)

    assert result["total"] == 1
    row = result["rows"][0]
    assert row["late_supplier_order"] is True
    assert "Покрыто заказом, но опоздание 7 дн." in row["badge"]
    assert "ЗСНФ-LATE" in row["badge"]

    # Regression: badge/late_supplier_order/turning_blank_priority must survive
    # serialization through the PurchaseCategoryGroupOrder response model used
    # by /purchases/grouped-by-category. FastAPI drops fields not declared on
    # the response model, so the schema must explicitly list them.
    from app.schemas import PurchaseCategoryGroupOrder
    from app.services.planning_service import get_run_purchases_grouped_by_category

    grouped = get_run_purchases_grouped_by_category(db, run.run_id)
    assert grouped["total_orders"] == 1
    order_row = grouped["groups"][0]["orders"][0]
    serialized = PurchaseCategoryGroupOrder(**order_row).model_dump()
    assert serialized["late_supplier_order"] is True
    assert "Покрыто заказом, но опоздание 7 дн." in (serialized["badge"] or "")
    assert "ЗСНФ-LATE" in (serialized["badge"] or "")


def test_turning_item_net_requirement_collapses_to_first_need_date_and_moves_blank(db_session):
    db = db_session
    today = datetime.date.today()

    turning_kind = ProductionKind(ref_1c="turn-kind", name="Токарные работы")
    blank_kind = ProductionKind(ref_1c="blank-kind", name="Заготовительные работы")
    turning_area = ProductionResource(resource_name="Токарный участок", capacity=8, daily_work_hours=8, buffer_days=0)
    blank_area = ProductionResource(resource_name="Заготовительный участок", capacity=8, daily_work_hours=8, buffer_days=5)
    blank_stage = ProductionStage(stage_name="Заготовка", stage_order=1, stage_ref1c="blank-stage")
    db.add_all([turning_kind, blank_kind, turning_area, blank_area, blank_stage])
    db.flush()
    db.add(ResourceProductionKind(resource_id=turning_area.resource_id, production_kind_id=turning_kind.id))
    db.add(ResourceProductionKind(resource_id=blank_area.resource_id, production_kind_id=blank_kind.id))

    parent = Item(
        item_code="TURN-PARENT",
        item_name="Turning Parent",
        item_article="TURN-PARENT",
        replenishment_method="Производство",
        stock_qty=0,
        status="active",
    )
    blank = Item(
        item_code="TURN-BLANK",
        item_name="Turning Blank",
        item_article="TURN-BLANK",
        replenishment_method="Производство",
        stock_qty=0,
        status="active",
    )
    db.add_all([parent, blank])
    db.flush()

    spec = Specification(spec_code="TURN-SPEC", spec_name="Turning Spec", production_kind_id=turning_kind.id)
    blank_spec = Specification(spec_code="BLANK-SPEC", spec_name="Blank Spec", production_kind_id=blank_kind.id)
    db.add_all([spec, blank_spec])
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(DefaultSpecification(item_id=blank.item_id, spec_id=blank_spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=blank.item_id, quantity=2.0, stage_id=blank_stage.stage_id))
    db.add_all(
        [
            ProductionPlanEntry(item_id=parent.item_id, date=today + datetime.timedelta(days=2), planned_qty=3),
            ProductionPlanEntry(item_id=parent.item_id, date=today + datetime.timedelta(days=7), planned_qty=4),
        ]
    )
    db.commit()

    result = compute_planning_preview(
        db,
        horizon_days=20,
        config_overrides={"safety_stock_percent": 0, "toggles": {"include_wip": False}},
    )

    first_need = (today + datetime.timedelta(days=2)).isoformat()
    later_need = (today + datetime.timedelta(days=7)).isoformat()
    parent_net = result["net"][str(parent.item_id)]
    blank_net = result["net"][str(blank.item_id)]

    assert parent_net == {first_need: 7.0}
    assert blank_net == {first_need: 14.0}
    assert later_need not in parent_net
    assert later_need not in blank_net
    assert any(
        w.get("code") == "TURNING_BLANK_PRIORITY"
        and w.get("item_id") == blank.item_id
        and w.get("parent_item_id") == parent.item_id
        and w.get("need_date") == first_need
        for w in result.get("warnings", [])
    )


def test_bom_explosion_shifts_child_need_date_by_parent_lead_time(db_session):
    """Classical MRP lead-time offsetting: the child component's need_date
    is shifted back by the PARENT's production buffer_days. Across multiple
    BOM levels the buffers accumulate (grandparent + parent + ... ), so the
    leaf material gets a need_date far enough back to cover the full chain.

    Earlier code used `resolve_buffer_days(child_id)` at every level, which
    shifted by the wrong link and effectively lost one level of lead time
    per BOM hop (a 3-level chain with buffers 7/5/3 mis-shifted by 12
    days).
    """
    db = db_session
    today = datetime.date.today()

    # Two production stages with different lead times.
    top_kind = ProductionKind(ref_1c="top-kind", name="Сборка")
    sub_kind = ProductionKind(ref_1c="sub-kind", name="Узлы")
    # buffer_days here represents the workshop's production lead time.
    top_area = ProductionResource(resource_name="Сборка", capacity=8, daily_work_hours=8, buffer_days=7)
    sub_area = ProductionResource(resource_name="Узлы", capacity=8, daily_work_hours=8, buffer_days=5)
    db.add_all([top_kind, sub_kind, top_area, sub_area])
    db.flush()
    db.add(ResourceProductionKind(resource_id=top_area.resource_id, production_kind_id=top_kind.id))
    db.add(ResourceProductionKind(resource_id=sub_area.resource_id, production_kind_id=sub_kind.id))

    top = Item(
        item_code="LT-TOP",
        item_name="Top Product",
        item_article="LT-TOP",
        replenishment_method="Производство",
        stock_qty=0,
        status="active",
    )
    sub = Item(
        item_code="LT-SUB",
        item_name="Sub Assembly",
        item_article="LT-SUB",
        replenishment_method="Производство",
        stock_qty=0,
        status="active",
    )
    leaf = Item(
        item_code="LT-LEAF",
        item_name="Leaf Material",
        item_article="LT-LEAF",
        replenishment_method="Покупка",   # purchased leaf, no production buffer
        stock_qty=0,
        status="active",
    )
    db.add_all([top, sub, leaf])
    db.flush()

    top_spec = Specification(spec_code="TOP", spec_name="Top Spec", production_kind_id=top_kind.id)
    sub_spec = Specification(spec_code="SUB", spec_name="Sub Spec", production_kind_id=sub_kind.id)
    db.add_all([top_spec, sub_spec])
    db.flush()
    db.add(DefaultSpecification(item_id=top.item_id, spec_id=top_spec.spec_id))
    db.add(DefaultSpecification(item_id=sub.item_id, spec_id=sub_spec.spec_id))
    db.add(SpecComponent(spec_id=top_spec.spec_id, item_id=sub.item_id, quantity=1.0))
    db.add(SpecComponent(spec_id=sub_spec.spec_id, item_id=leaf.item_id, quantity=1.0))

    # Top is needed in 30 days — well past any buffer, so the shift is not
    # clamped by `today` for any level.
    top_need = today + datetime.timedelta(days=30)
    db.add(ProductionPlanEntry(item_id=top.item_id, date=top_need, planned_qty=1))
    db.commit()

    result = compute_planning_preview(
        db,
        horizon_days=60,
        config_overrides={"safety_stock_percent": 0, "toggles": {"include_wip": False}},
    )

    # Top stays at its plan date — it's the root of the explosion.
    assert result["net"][str(top.item_id)] == {top_need.isoformat(): 1.0}

    # Sub is shifted back by TOP's buffer (7 days) — the time it takes to
    # assemble the top product. Sub must be ready 7 days before top ships.
    sub_need = top_need - datetime.timedelta(days=7)
    assert result["net"][str(sub.item_id)] == {sub_need.isoformat(): 1.0}

    # Leaf is shifted back by SUB's buffer (5 days). Total accumulated
    # offset from top: 7 + 5 = 12 days. The pre-fix code would have shifted
    # leaf by leaf's own (purchase-flow) buffer of 0 — dropping 12 days of
    # lead time.
    leaf_need = sub_need - datetime.timedelta(days=5)
    assert result["net"][str(leaf.item_id)] == {leaf_need.isoformat(): 1.0}


def test_non_turning_item_keeps_requirement_buckets(db_session):
    db = db_session
    today = datetime.date.today()

    parent = Item(
        item_code="REG-PARENT",
        item_name="Regular Parent",
        item_article="REG-PARENT",
        replenishment_method="Производство",
        stock_qty=0,
        status="active",
    )
    component = Item(
        item_code="REG-COMP",
        item_name="Regular Component",
        item_article="REG-COMP",
        replenishment_method="Производство",
        stock_qty=0,
        status="active",
    )
    db.add_all([parent, component])
    db.flush()
    spec = Specification(spec_code="REG-SPEC", spec_name="Regular Spec")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=2.0))
    db.add_all(
        [
            ProductionPlanEntry(item_id=parent.item_id, date=today + datetime.timedelta(days=2), planned_qty=3),
            ProductionPlanEntry(item_id=parent.item_id, date=today + datetime.timedelta(days=7), planned_qty=4),
        ]
    )
    db.commit()

    result = compute_planning_preview(
        db,
        horizon_days=20,
        config_overrides={"safety_stock_percent": 0, "toggles": {"include_wip": False}},
    )

    first_need = (today + datetime.timedelta(days=2)).isoformat()
    later_need = (today + datetime.timedelta(days=7)).isoformat()

    assert result["net"][str(parent.item_id)] == {first_need: 3.0, later_need: 4.0}
    assert result["net"][str(component.item_id)] == {first_need: 6.0, later_need: 8.0}
    assert not any(w.get("code") == "TURNING_BLANK_PRIORITY" for w in result.get("warnings", []))


def test_active_supplier_remaining_filters_new_cancelled_deleted_and_missing_date(db_session):
    db = db_session

    item = Item(
        item_code="SUP-REM",
        item_name="Supplier Remaining",
        item_article="SUP-REM",
        replenishment_method="Покупка",
        unit="u",
        stock_qty=0,
        status="active",
    )
    db.add(item)
    db.flush()

    def add_order(number, state_name, deletion_mark, remaining_qty, delivery_date, state_key_marker="default"):
        order = SupplierOrder(
            order_number=number,
            order_date=datetime.datetime(2026, 1, 1),
            order_ref1c=f"{number}-ref",
            is_posted=True,
            order_state_key=f"{number}-state" if state_key_marker == "default" else state_key_marker,
            order_state_name=state_name,
            deletion_mark=deletion_mark,
        )
        db.add(order)
        db.flush()
        db.add(
            SupplierOrderItem(
                order_id=order.order_id,
                item_id_ref=item.item_id,
                line_number=1,
                quantity=10.0,
                received_qty=10.0 - remaining_qty,
                remaining_qty=remaining_qty,
                delivery_date=delivery_date,
            )
        )

    # Учитываются только фазы «в пути» / «на складе» (deny-by-default).
    add_order("IN-TRANSIT", "В пути", False, 4.0, datetime.datetime(2026, 1, 10))
    add_order("IN-STOCK", "Принят на склад", False, 3.0, datetime.datetime(2026, 1, 11))
    # «В закупку» теперь относится к фазе «Нет товара» и НЕ нетует.
    add_order("PURCHASING", "В закупку", False, 5.0, datetime.datetime(2026, 1, 12))
    # Незамапленное/пустое состояние → UNKNOWN → не нетует.
    add_order("UNKNOWN", None, False, 3.0, datetime.datetime(2026, 1, 13))
    add_order("LEGACY", None, False, 12.0, datetime.datetime(2026, 1, 14), state_key_marker=None)
    add_order("NEW", "Новый заказ", False, 5.0, datetime.datetime(2026, 1, 10))
    add_order("CANCEL", "Отменён", False, 6.0, datetime.datetime(2026, 1, 10))
    add_order("DONE", "Завершён", False, 9.0, datetime.datetime(2026, 1, 10))
    add_order("DONE-OK", "Завершен успешно", False, 10.0, datetime.datetime(2026, 1, 10))
    add_order("ACCOUNTING", "Бухгалтерия", False, 11.0, datetime.datetime(2026, 1, 10))
    add_order("DELETED", "В пути", True, 7.0, datetime.datetime(2026, 1, 10))
    add_order("NODATE", "В пути", False, 8.0, None)
    db.commit()

    rem = _get_active_supplier_remaining_by_item_date(db)
    assert rem == {
        item.item_id: [
            (datetime.date(2026, 1, 10), 4.0),
            (datetime.date(2026, 1, 11), 3.0),
        ]
    }


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


def test_active_remaining_counts_mrp_sourced_production_orders(db_session):
    """
    Plan rule: внутренние MRP-заказы (source='mrp') должны учитываться в
    следующих MRP-расчётах наравне с 1С-заказами. Their order_state_key is
    NULL (we never set it for internal orders), so the same DONE_STATE_KEY
    filter that lets active 1C orders through must also let MRP-source ones
    through. Verify both via the new generic name and the legacy alias.
    """
    db = db_session

    item = Item(
        item_code="MRP-ACT",
        item_name="MRP active",
        item_article="MRP-ACT",
        replenishment_method="Производство",
        unit="u",
        stock_qty=0,
        status="active",
    )
    db.add(item)
    db.flush()

    run = _mk_run(db)
    internal_order = ProductionOrder(
        order_number=f"MRP-{run.run_id}-test",
        order_date=datetime.datetime(2026, 5, 20),
        order_ref1c=None,             # MRP-source orders are not in 1C yet
        deletion_mark=False,
        order_state_key=None,         # No 1C state -> passes DONE filter
        is_posted=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db.add(internal_order)
    db.flush()
    db.add(
        ProductionProduct(
            order_id=internal_order.order_id,
            item_id=item.item_id,
            quantity=8.0,
            produced_qty=0.0,
            remaining_qty=8.0,
        )
    )
    db.commit()

    rem_new = _get_active_production_remaining_by_item(db)
    assert rem_new.get(item.item_id) == 8.0
    rem_legacy = _get_active_1c_remaining_by_item(db)
    assert rem_legacy.get(item.item_id) == 8.0


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


def test_rework_flow_creates_full_order_when_components_are_sufficient(db_session):
    db = db_session

    u = Unit(unit_ref1c="u-rw-full", unit_name="шт", short_name="шт", precision=0)
    db.add(u)

    parent = Item(
        item_code="RW-FULL",
        item_name="Rework Full",
        item_article="RW-FULL",
        replenishment_method="Переработка",
        replenishment_time=2,
        unit="u-rw-full",
        stock_qty=0,
        status="active",
    )
    component = Item(
        item_code="RW-COMP-FULL",
        item_name="Rework Component Full",
        item_article="RW-COMP-FULL",
        replenishment_method="Покупка",
        unit="u-rw-full",
        stock_qty=20,
        status="active",
    )
    db.add_all([parent, component])
    db.flush()

    spec = Specification(spec_code="RW-S-FULL", spec_name="Rework Full Spec")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=2.0))
    db.flush()

    run = _mk_run(db)
    units_by_ref = {"u-rw-full": u}
    item_cache = {parent.item_id: parent, component.item_id: component}

    def components_loader(spec_id: int):
        return db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()

    calc = OrderQuantityCalculator(
        snapshot=run.config_snapshot,
        default_spec_map={parent.item_id: spec.spec_id},
        spec_by_id={spec.spec_id: spec},
        components_loader=components_loader,
        item_by_id=item_cache,
        units_by_ref=units_by_ref,
        res_by_id={},
        production_kinds_by_resource={},
        stock_by_item={parent.item_id: 0.0, component.item_id: 20.0},
        wip_by_item={},
        horizon_days=run.horizon_days or 0,
        total_demand_by_item={parent.item_id: 5.0},
    )

    out = build_planned_orders_and_purchases(
        db,
        run,
        {str(parent.item_id): {"2025-01-01": 5.0}},
        calc,
        priority_manager=SimpleNamespace(),
        item_cache=item_cache,
        units_by_ref=units_by_ref,
    )

    row = db.query(PlannedRework).filter(PlannedRework.run_id == run.run_id).one()
    assert float(row.requested_qty) == 5.0
    assert float(row.planned_qty) == 5.0
    assert float(row.qty) == 5.0
    assert row.spec_id == spec.spec_id
    assert row.component_blocked is False
    assert row.component_partial is False
    assert (row.shortage or {}).get("planned_qty") == 5.0
    assert not any(w.get("code", "").startswith("REWORK_COMPONENT_SHORTAGE_") for w in out.get("warnings", []))


def test_rework_flow_marks_partial_when_components_limit_qty(db_session):
    db = db_session

    u = Unit(unit_ref1c="u-rw-part", unit_name="шт", short_name="шт", precision=0)
    db.add(u)

    parent = Item(
        item_code="RW-PART",
        item_name="Rework Partial",
        item_article="RW-PART",
        replenishment_method="Переработка",
        replenishment_time=1,
        unit="u-rw-part",
        stock_qty=0,
        status="active",
    )
    component = Item(
        item_code="RW-COMP-PART",
        item_name="Rework Component Part",
        item_article="RW-COMP-PART",
        replenishment_method="Покупка",
        unit="u-rw-part",
        stock_qty=6,
        status="active",
    )
    db.add_all([parent, component])
    db.flush()

    spec = Specification(spec_code="RW-S-PART", spec_name="Rework Partial Spec")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=2.0))
    db.flush()

    run = _mk_run(db)
    units_by_ref = {"u-rw-part": u}
    item_cache = {parent.item_id: parent, component.item_id: component}

    def components_loader(spec_id: int):
        return db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()

    calc = OrderQuantityCalculator(
        snapshot=run.config_snapshot,
        default_spec_map={parent.item_id: spec.spec_id},
        spec_by_id={spec.spec_id: spec},
        components_loader=components_loader,
        item_by_id=item_cache,
        units_by_ref=units_by_ref,
        res_by_id={},
        production_kinds_by_resource={},
        stock_by_item={parent.item_id: 0.0, component.item_id: 6.0},
        wip_by_item={},
        horizon_days=run.horizon_days or 0,
        total_demand_by_item={parent.item_id: 5.0},
    )

    out = build_planned_orders_and_purchases(
        db,
        run,
        {str(parent.item_id): {"2025-01-01": 5.0}},
        calc,
        priority_manager=SimpleNamespace(),
        item_cache=item_cache,
        units_by_ref=units_by_ref,
    )

    row = db.query(PlannedRework).filter(PlannedRework.run_id == run.run_id).one()
    assert float(row.requested_qty) == 5.0
    assert float(row.planned_qty) == 3.0
    assert float(row.qty) == 3.0
    assert row.component_blocked is False
    assert row.component_partial is True
    assert any(w.get("code") == "REWORK_COMPONENT_SHORTAGE_PARTIAL" for w in out.get("warnings", []))


def test_rework_flow_keeps_blocked_row_with_zero_qty_when_components_missing(db_session):
    db = db_session

    u = Unit(unit_ref1c="u-rw-block", unit_name="шт", short_name="шт", precision=0)
    db.add(u)

    parent = Item(
        item_code="RW-BLOCK",
        item_name="Rework Blocked",
        item_article="RW-BLOCK",
        replenishment_method="Переработка",
        replenishment_time=1,
        unit="u-rw-block",
        stock_qty=0,
        status="active",
    )
    component = Item(
        item_code="RW-COMP-BLOCK",
        item_name="Rework Component Blocked",
        item_article="RW-COMP-BLOCK",
        replenishment_method="Покупка",
        unit="u-rw-block",
        stock_qty=0,
        status="active",
    )
    db.add_all([parent, component])
    db.flush()

    spec = Specification(spec_code="RW-S-BLOCK", spec_name="Rework Blocked Spec")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=2.0))
    db.flush()

    run = _mk_run(db)
    units_by_ref = {"u-rw-block": u}
    item_cache = {parent.item_id: parent, component.item_id: component}

    def components_loader(spec_id: int):
        return db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()

    calc = OrderQuantityCalculator(
        snapshot=run.config_snapshot,
        default_spec_map={parent.item_id: spec.spec_id},
        spec_by_id={spec.spec_id: spec},
        components_loader=components_loader,
        item_by_id=item_cache,
        units_by_ref=units_by_ref,
        res_by_id={},
        production_kinds_by_resource={},
        stock_by_item={parent.item_id: 0.0, component.item_id: 0.0},
        wip_by_item={},
        horizon_days=run.horizon_days or 0,
        total_demand_by_item={parent.item_id: 5.0},
    )

    out = build_planned_orders_and_purchases(
        db,
        run,
        {str(parent.item_id): {"2025-01-01": 5.0}},
        calc,
        priority_manager=SimpleNamespace(),
        item_cache=item_cache,
        units_by_ref=units_by_ref,
    )

    row = db.query(PlannedRework).filter(PlannedRework.run_id == run.run_id).one()
    assert float(row.requested_qty) == 5.0
    assert float(row.planned_qty) == 0.0
    assert float(row.qty) == 0.0
    assert row.component_blocked is True
    assert row.component_partial is False
    assert any(w.get("code") == "REWORK_COMPONENT_SHORTAGE_BLOCKED" for w in out.get("warnings", []))


def test_control_run_keeps_production_orders_identical_with_purchase_and_rework_flows(db_session):
    db = db_session

    u = Unit(unit_ref1c="u-ctrl", unit_name="шт", short_name="шт", precision=0)
    db.add(u)

    production_item = Item(
        item_code="CTRL-PROD",
        item_name="Control Production",
        item_article="CTRL-PROD",
        replenishment_method="Производство",
        unit="u-ctrl",
        stock_qty=0,
        status="active",
    )
    purchase_item = Item(
        item_code="CTRL-BUY",
        item_name="Control Purchase",
        item_article="CTRL-BUY",
        replenishment_method="Покупка",
        replenishment_time=3,
        unit="u-ctrl",
        stock_qty=0,
        status="active",
    )
    rework_item = Item(
        item_code="CTRL-RW",
        item_name="Control Rework",
        item_article="CTRL-RW",
        replenishment_method="Переработка",
        replenishment_time=1,
        unit="u-ctrl",
        stock_qty=0,
        status="active",
    )
    rework_component = Item(
        item_code="CTRL-RW-COMP",
        item_name="Control Rework Component",
        item_article="CTRL-RW-COMP",
        replenishment_method="Покупка",
        unit="u-ctrl",
        stock_qty=50,
        status="active",
    )
    db.add_all([production_item, purchase_item, rework_item, rework_component])
    db.flush()

    rework_spec = Specification(spec_code="CTRL-RW-SPEC", spec_name="Control Rework Spec")
    db.add(rework_spec)
    db.flush()
    db.add(DefaultSpecification(item_id=rework_item.item_id, spec_id=rework_spec.spec_id))
    db.add(SpecComponent(spec_id=rework_spec.spec_id, item_id=rework_component.item_id, quantity=2.0))
    db.flush()

    units_by_ref = {"u-ctrl": u}
    item_cache = {
        production_item.item_id: production_item,
        purchase_item.item_id: purchase_item,
        rework_item.item_id: rework_item,
        rework_component.item_id: rework_component,
    }

    def components_loader(spec_id: int):
        return db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()

    def build_projection(run, net_req, total_demand):
        calc = OrderQuantityCalculator(
            snapshot=run.config_snapshot,
            default_spec_map={rework_item.item_id: rework_spec.spec_id},
            spec_by_id={rework_spec.spec_id: rework_spec},
            components_loader=components_loader,
            item_by_id=item_cache,
            units_by_ref=units_by_ref,
            res_by_id={},
            production_kinds_by_resource={},
            stock_by_item={
                production_item.item_id: 0.0,
                purchase_item.item_id: 0.0,
                rework_item.item_id: 0.0,
                rework_component.item_id: 50.0,
            },
            wip_by_item={},
            horizon_days=run.horizon_days or 0,
            total_demand_by_item=total_demand,
        )

        build_planned_orders_and_purchases(
            db,
            run,
            net_req,
            calc,
            priority_manager=SimpleNamespace(),
            item_cache=item_cache,
            units_by_ref=units_by_ref,
        )

        rows = (
            db.query(PlannedOrder)
            .filter(PlannedOrder.run_id == run.run_id)
            .order_by(PlannedOrder.item_id.asc(), PlannedOrder.need_date.asc())
            .all()
        )
        return [
            {
                "item_id": int(row.item_id),
                "requested_qty": float(row.requested_qty),
                "planned_qty": float(row.planned_qty),
                "qty": float(row.qty),
                "need_date": row.need_date.isoformat() if row.need_date else None,
                "bucket_date": row.bucket_date.isoformat() if row.bucket_date else None,
            }
            for row in rows
        ]

    baseline_run = _mk_run(db)
    baseline_projection = build_projection(
        run=baseline_run,
        net_req={str(production_item.item_id): {"2025-01-10": 8.0}},
        total_demand={production_item.item_id: 8.0},
    )

    mixed_run = _mk_run(db)
    mixed_projection = build_projection(
        run=mixed_run,
        net_req={
            str(production_item.item_id): {"2025-01-10": 8.0},
            str(purchase_item.item_id): {"2025-01-11": 5.0},
            str(rework_item.item_id): {"2025-01-12": 6.0},
        },
        total_demand={
            production_item.item_id: 8.0,
            purchase_item.item_id: 5.0,
            rework_item.item_id: 6.0,
        },
    )

    assert baseline_projection == mixed_projection
    assert mixed_projection == [
        {
            "item_id": production_item.item_id,
            "requested_qty": 8.0,
            "planned_qty": 8.0,
            "qty": 8.0,
            "need_date": "2025-01-10",
            "bucket_date": "2025-01-10",
        }
    ]
    assert db.query(PlannedPurchase).filter(PlannedPurchase.run_id == mixed_run.run_id).count() == 1
    assert db.query(PlannedRework).filter(PlannedRework.run_id == mixed_run.run_id).count() == 1


import datetime as _dt
import json
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    DefaultSpecification,
    Employee,
    Item,
    PlannedOrder,
    PlannedPurchase,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SpecComponent,
    Specification,
    SupplierOrder,
    SupplierOrderItem,
)
from app.routers.production_control import list_employees
from app.services.production_control_journal import create_orders_from_mrp, list_journal
from app.services.production_control_material_availability import preview_materials
from app.services.production_control_material_issues import create_material_issues


def test_list_employees_returns_active_synced_employees(db_session):
    db_session.add_all([
        Employee(
            employee_ref1c="11111111-1111-1111-1111-111111111111",
            employee_code="0001",
            employee_name="Иванов Иван",
            deletion_mark=False,
        ),
        Employee(
            employee_ref1c="22222222-2222-2222-2222-222222222222",
            employee_code="0002",
            employee_name="Петров Петр",
            deletion_mark=True,
        ),
    ])
    db_session.commit()

    result = list_employees(db=db_session)

    assert result["total"] == 1
    assert result["rows"][0]["employee_name"] == "Иванов Иван"
    assert result["rows"][0]["employee_ref1c"] == "11111111-1111-1111-1111-111111111111"


def test_journal_and_material_issue_are_scoped_to_order_line(db_session):
    parent = Item(
        item_code="P-001",
        item_name="Деталь",
        item_article="ART-P",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    component = Item(
        item_code="C-001",
        item_name="Комплектующее",
        item_article="ART-C",
        unit="м",
        stock_qty=10,
        status="active",
    )
    db_session.add_all([parent, component])
    db_session.flush()

    spec = Specification(spec_name="Спецификация детали", spec_ref1c="spec-001")
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=2.5))

    order = ProductionOrder(
        order_number="1839",
        order_date=datetime(2026, 5, 18),
        order_ref1c="order-001",
        is_posted=True,
        deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=8,
        produced_qty=0,
        remaining_qty=8,
    )
    db_session.add(product)
    db_session.commit()

    journal = list_journal(db_session)
    assert journal["total"] == 1
    assert journal["rows"][0]["order_number"] == "1839"
    assert journal["rows"][0]["item_article"] == "ART-P"
    # Default per plan-aligned status set: 'shortage' until coverage is
    # evaluated (was 'new' under the legacy workshop-progress set).
    assert journal["rows"][0]["status"] == "shortage"
    # 1C-synced order: source defaults to '1c', order_ref1c populated.
    # Frontend uses these to hide the "Export to 1C" button on 1C rows.
    assert journal["rows"][0]["order_source"] == "1c"
    assert journal["rows"][0]["order_ref1c"] == "order-001"

    materials = preview_materials(db_session, product.product_id)
    assert materials["components"][0]["component_item_id"] == component.item_id
    assert materials["components"][0]["required_qty"] == 20

    created = create_material_issues(db_session, [product.product_id], initiated_by="кладовщик")
    assert len(created["created"]) == 1
    assert created["created"][0]["lines_count"] == 1

    journal_after = list_journal(db_session)
    assert journal_after["rows"][0]["issue_status"] == "requested"
    # Creating a material-issue draft moves the line from 'shortage' to
    # 'to_move' ("документы созданы, ждём проведения") per plan.
    assert journal_after["rows"][0]["status"] == "to_move"
    assert journal_after["rows"][0]["issue_count"] == 1


def _make_planned_order(db, item, qty=4) -> PlannedOrder:
    run = PlanningRun(
        status="DONE",
        config_snapshot=json.dumps({}),
    )
    db.add(run)
    db.flush()
    planned = PlannedOrder(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=qty,
        planned_qty=qty,
        qty=qty,
        need_date=_dt.date(2026, 6, 1),
        bucket_date=_dt.date(2026, 6, 1),
    )
    db.add(planned)
    db.flush()
    return planned


def test_production_order_carries_source_tagging(db_session):
    """
    Internal MRP-originated production orders must be distinguishable from
    1C-synced orders, and the planned_order they were generated from must be
    traceable from the production_products line.
    """
    item = Item(
        item_code="MRP-SRC",
        item_name="Source-tagged item",
        item_article="SRC",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    planned = _make_planned_order(db_session, item)

    order = ProductionOrder(
        order_number="PRODPLAN-0001",
        order_date=datetime(2026, 5, 20),
        order_ref1c=None,
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=planned.run_id,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=4,
        produced_qty=0,
        remaining_qty=4,
        source_planned_order_id=planned.order_id,
    )
    db_session.add(product)
    db_session.commit()

    refetched = db_session.query(ProductionOrder).filter_by(order_id=order.order_id).one()
    assert refetched.source == "mrp"
    assert refetched.source_run_id == planned.run_id
    refetched_product = db_session.query(ProductionProduct).filter_by(product_id=product.product_id).one()
    assert refetched_product.source_planned_order_id == planned.order_id


def test_partial_unique_source_planned_order_blocks_duplicates(db_session):
    """
    Idempotency rule from the plan: one PlannedOrder must not back more than
    one internal production line. The partial unique index on
    production_products(source_planned_order_id) enforces it at the DB layer.
    """
    item = Item(
        item_code="MRP-IDEM",
        item_name="Idempotency item",
        item_article="IDEM",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    planned = _make_planned_order(db_session, item)

    order_one = ProductionOrder(
        order_number="PRODPLAN-0010",
        order_date=datetime(2026, 5, 20),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=planned.run_id,
    )
    order_two = ProductionOrder(
        order_number="PRODPLAN-0011",
        order_date=datetime(2026, 5, 20),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=planned.run_id,
    )
    db_session.add_all([order_one, order_two])
    db_session.flush()

    db_session.add(
        ProductionProduct(
            order_id=order_one.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=4,
            produced_qty=0,
            remaining_qty=4,
            source_planned_order_id=planned.order_id,
        )
    )
    db_session.commit()

    # Same planned_order again => IntegrityError from the partial unique index.
    db_session.add(
        ProductionProduct(
            order_id=order_two.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=4,
            produced_qty=0,
            remaining_qty=4,
            source_planned_order_id=planned.order_id,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # NULL source_planned_order_id (1C-synced) must NOT be constrained.
    db_session.add(
        ProductionProduct(
            order_id=order_two.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=4,
            produced_qty=0,
            remaining_qty=4,
            source_planned_order_id=None,
        )
    )
    db_session.commit()


def test_create_material_issues_is_idempotent_per_product(db_session):
    """
    Re-clicking "prepare issue" must not create a duplicate draft document for
    the same production line. The second call should reuse the existing one
    and report it in `reused` rather than `created`. The partial unique index
    enforces this at the DB layer too.
    """
    parent = Item(
        item_code="P-IDEM",
        item_name="Parent",
        item_article="ART-P-IDEM",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    comp = Item(
        item_code="C-IDEM",
        item_name="Component",
        item_article="ART-C-IDEM",
        unit="м",
        stock_qty=10,
        status="active",
    )
    db_session.add_all([parent, comp])
    db_session.flush()

    spec = Specification(spec_name="Idem spec", spec_ref1c="spec-idem")
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))

    order = ProductionOrder(
        order_number="IDEM-001",
        order_date=datetime(2026, 5, 20),
        order_ref1c="order-idem",
        is_posted=True,
        deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
    )
    db_session.add(product)
    db_session.commit()

    first = create_material_issues(db_session, [product.product_id], initiated_by="op1")
    assert len(first["created"]) == 1
    assert first.get("reused", []) == []

    second = create_material_issues(db_session, [product.product_id], initiated_by="op2")
    # Same product, still in draft -> no new issue created, existing one
    # reported as reused.
    assert second["created"] == []
    assert len(second["reused"]) == 1
    assert second["reused"][0]["issue_id"] == first["created"][0]["issue_id"]

    # And only one row physically exists.
    from app.models import ProductionMaterialIssue
    assert (
        db_session.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .count()
        == 1
    )


def test_create_orders_from_mrp_materializes_planned_orders(db_session):
    """
    POST /v1/production-control/orders/from-mrp must turn selected
    planned_order rows into internal production orders tagged source='mrp',
    with the source_planned_order_id back-link and an initial line state.
    Second call for the same planned_orders is a no-op (reused).
    """
    item = Item(
        item_code="MRP-ITEM",
        item_name="Item from MRP",
        item_article="ART-MRP",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    run = PlanningRun(status="DONE", config_snapshot=json.dumps({}))
    db_session.add(run)
    db_session.flush()
    planned_a = PlannedOrder(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=10,
        planned_qty=10,
        qty=10,
        need_date=_dt.date(2026, 6, 1),
        start_date=_dt.date(2026, 5, 25),
        finish_date=_dt.date(2026, 5, 31),
        bucket_date=_dt.date(2026, 6, 1),
    )
    planned_b = PlannedOrder(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=4,
        planned_qty=4,
        qty=4,
        need_date=_dt.date(2026, 6, 5),
        bucket_date=_dt.date(2026, 6, 5),
    )
    db_session.add_all([planned_a, planned_b])
    db_session.commit()

    first = create_orders_from_mrp(
        db_session,
        [planned_a.order_id, planned_b.order_id],
        initiated_by="planner",
    )
    assert first["status"] == "ok"
    assert first["errors"] == []
    assert first["reused"] == []
    assert {row["planned_order_id"] for row in first["created"]} == {
        planned_a.order_id,
        planned_b.order_id,
    }
    for row in first["created"]:
        order = (
            db_session.query(ProductionOrder)
            .filter(ProductionOrder.order_id == row["order_id"])
            .one()
        )
        assert order.source == "mrp"
        assert order.source_run_id == run.run_id
        assert order.is_posted is False
        assert order.order_ref1c is None
        product = (
            db_session.query(ProductionProduct)
            .filter(ProductionProduct.product_id == row["product_id"])
            .one()
        )
        assert product.source_planned_order_id == row["planned_order_id"]
        assert float(product.quantity) == row["qty"]
        assert float(product.remaining_qty) == row["qty"]
        # ProductionOrderLineState seeded with status='shortage' per plan.
        from app.models import ProductionOrderLineState as POLS
        state = (
            db_session.query(POLS)
            .filter(POLS.product_id == product.product_id)
            .one()
        )
        assert state.status == "shortage"
        assert state.issue_status == "not_requested"

    # Second call must be a no-op.
    second = create_orders_from_mrp(
        db_session,
        [planned_a.order_id, planned_b.order_id],
    )
    assert second["created"] == []
    assert len(second["reused"]) == 2
    assert (
        db_session.query(ProductionOrder)
        .filter(ProductionOrder.source == "mrp", ProductionOrder.source_run_id == run.run_id)
        .count()
        == 2
    )


def test_create_orders_from_mrp_skips_invalid_inputs(db_session):
    """
    Bad planned_order ids and 0-qty rows must be reported as errors instead of
    aborting the whole batch.
    """
    item = Item(
        item_code="MRP-SKIP",
        item_name="Skip item",
        item_article="SKIP",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    run = PlanningRun(status="DONE", config_snapshot=json.dumps({}))
    db_session.add(run)
    db_session.flush()
    zero_qty = PlannedOrder(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=0,
        planned_qty=0,
        qty=0,
        need_date=_dt.date(2026, 6, 1),
        bucket_date=_dt.date(2026, 6, 1),
    )
    db_session.add(zero_qty)
    db_session.commit()

    result = create_orders_from_mrp(db_session, [zero_qty.order_id, 999_999])
    assert result["created"] == []
    assert result["reused"] == []
    assert len(result["errors"]) == 2
    # Nothing committed for the invalid batch.
    assert (
        db_session.query(ProductionOrder).filter(ProductionOrder.source == "mrp").count() == 0
    )


# ---------------------------------------------------------------------------
# Coverage evaluation in preview_materials
# ---------------------------------------------------------------------------


def _make_basic_spec(db, parent_name="Parent", child_specs=()):
    """Helper that wires Item + Specification + SpecComponents + DefaultSpecification."""
    parent = Item(
        item_code=f"P-{parent_name}",
        item_name=parent_name,
        item_article=f"ART-{parent_name}",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db.add(parent)
    db.flush()
    spec = Specification(spec_name=f"Spec {parent_name}", spec_ref1c=f"spec-{parent_name}")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))

    components: list[Item] = []
    for code, name, stock, qty_per_unit in child_specs:
        comp = Item(
            item_code=code,
            item_name=name,
            item_article=code,
            unit="м",
            stock_qty=stock,
            status="active",
        )
        db.add(comp)
        db.flush()
        db.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=qty_per_unit))
        components.append(comp)
    return parent, spec, components


def _make_internal_order_for(db, parent, qty=2):
    order = ProductionOrder(
        order_number=f"COV-{parent.item_id}",
        order_date=datetime(2026, 5, 20),
        is_posted=False,
        deletion_mark=False,
        source="1c",
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=qty,
        produced_qty=0,
        remaining_qty=qty,
    )
    db.add(product)
    db.commit()
    return order, product


def test_preview_materials_marks_ready_when_stock_covers_all(db_session):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="ReadyParent",
        child_specs=[
            ("RC1", "Comp A enough", 100, 2),  # need 2*2 = 4, have 100
            ("RC2", "Comp B enough", 100, 3),  # need 3*2 = 6, have 100
        ],
    )
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    preview = preview_materials(db_session, product.product_id, refresh_state=True)

    assert preview["coverage"] == "ready"
    for c in preview["components"]:
        assert c["coverage"] == "ok"
        assert c["missing_qty"] == 0
        assert c["eta_dates"] == []
    # Line status auto-bumped from default 'shortage' to 'ready'.
    state = (
        db_session.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert state.status == "ready"


def test_preview_materials_marks_shortage_and_includes_supplier_eta(db_session):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="ShortageParent",
        child_specs=[
            ("SC1", "No stock comp", 0, 1),  # need 1*2 = 2, have 0
        ],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    # An open supplier order with a future delivery date covering this component.
    sup_order = SupplierOrder(
        order_number="ЗАКП-COVER-001",
        order_date=datetime(2026, 5, 10, 10),
        order_ref1c="sup-ref-cover-001",
        order_state_key="state-in-work",
        order_state_name="В закупку",
        deletion_mark=False,
    )
    db_session.add(sup_order)
    db_session.flush()
    db_session.add(
        SupplierOrderItem(
            order_id=sup_order.order_id,
            item_id_ref=comp.item_id,
            line_number=1,
            quantity=10,
            received_qty=0,
            remaining_qty=10,
            delivery_date=datetime(2026, 6, 1),
        )
    )
    db_session.commit()

    preview = preview_materials(db_session, product.product_id, refresh_state=True)
    assert preview["coverage"] == "shortage"
    only_comp = preview["components"][0]
    assert only_comp["coverage"] == "shortage"
    assert only_comp["missing_qty"] == 2
    # ETA from supplier_order pipe.
    eta = only_comp["eta_dates"]
    assert len(eta) >= 1
    sup_etas = [e for e in eta if e["source"] == "supplier_order"]
    assert sup_etas
    assert sup_etas[0]["ref"] == "ЗАКП-COVER-001"
    assert sup_etas[0]["date"] == "2026-06-01"


def test_preview_materials_marks_partial_when_some_stock(db_session):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="PartialParent",
        child_specs=[
            ("PC1", "Comp full", 100, 1),  # need 2, have 100 -> ok
            ("PC2", "Comp partial", 3, 2),  # need 4, have 3 -> partial
        ],
    )
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    preview = preview_materials(db_session, product.product_id, refresh_state=True)
    assert preview["coverage"] == "partial"
    by_name = {c["item_name"]: c for c in preview["components"]}
    assert by_name["Comp full"]["coverage"] == "ok"
    assert by_name["Comp partial"]["coverage"] == "partial"
    assert by_name["Comp partial"]["missing_qty"] == 1
    state = (
        db_session.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert state.status == "partial"


def test_preview_materials_does_not_override_post_coverage_status(db_session):
    """
    Once a line has progressed past coverage (to_move / assembled / produced),
    re-running preview_materials must not regress it back to shortage/partial/
    ready even if the stock numbers say so.
    """
    parent, _spec, _comps = _make_basic_spec(
        db_session,
        parent_name="StickyParent",
        child_specs=[("STC1", "Comp empty", 0, 1)],
    )
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    # Manually push the state past the coverage band.
    state = (
        db_session.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one_or_none()
    )
    if state is None:
        # Lazy-create through preview, then move forward.
        preview_materials(db_session, product.product_id, refresh_state=True)
        state = (
            db_session.query(ProductionOrderLineState)
            .filter_by(product_id=product.product_id)
            .one()
        )
    state.status = "to_move"
    db_session.commit()

    preview_materials(db_session, product.product_id, refresh_state=True)
    refreshed = (
        db_session.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert refreshed.status == "to_move"


# ---------------------------------------------------------------------------
# Warehouse settings
# ---------------------------------------------------------------------------


def test_workshop_warehouse_binding_lifecycle(db_session):
    from app.models import ProductionResource, WorkshopWarehouseBinding
    from app.services.production_control_settings import (
        delete_workshop_binding,
        list_settings,
        upsert_workshop_binding,
    )

    workshop = ProductionResource(resource_name="Цех сварки")
    db_session.add(workshop)
    db_session.flush()

    # Initially empty.
    settings = list_settings(db_session)
    assert settings["workshop_warehouse_bindings"] == []
    assert settings["ignored_warehouses"] == []

    # Create.
    created = upsert_workshop_binding(
        db_session,
        workshop.resource_id,
        "11111111-1111-1111-1111-111111111111",
    )
    assert created["workshop_id"] == workshop.resource_id
    assert created["warehouse_ref1c"] == "11111111-1111-1111-1111-111111111111"
    assert created["workshop_name"] == "Цех сварки"

    # Idempotent upsert: same workshop, new warehouse — should update, not create a 2nd row.
    updated = upsert_workshop_binding(
        db_session,
        workshop.resource_id,
        "22222222-2222-2222-2222-222222222222",
    )
    assert updated["warehouse_ref1c"] == "22222222-2222-2222-2222-222222222222"
    assert (
        db_session.query(WorkshopWarehouseBinding)
        .filter_by(workshop_id=workshop.resource_id)
        .count()
        == 1
    )

    # Unknown workshop -> ValueError.
    with pytest.raises(ValueError):
        upsert_workshop_binding(db_session, 999_999, "33333333-3333-3333-3333-333333333333")

    # Delete.
    result = delete_workshop_binding(db_session, workshop.resource_id)
    assert result["deleted"] == 1
    settings_after = list_settings(db_session)
    assert settings_after["workshop_warehouse_bindings"] == []


def test_replace_settings_matches_frontend_contract(db_session):
    from app.models import IgnoredWarehouse, ProductionResource, StockWarehouse, WorkshopWarehouseBinding
    from app.services.production_control_settings import list_settings, replace_settings

    workshop_a = ProductionResource(resource_name="Цех А")
    workshop_b = ProductionResource(resource_name="Цех Б")
    warehouse_a = StockWarehouse(
        warehouse_ref1c="wh-a",
        warehouse_code="A",
        warehouse_name="Склад А",
        is_selected=True,
    )
    warehouse_b = StockWarehouse(
        warehouse_ref1c="wh-b",
        warehouse_code="B",
        warehouse_name="Склад Б",
        is_selected=True,
    )
    workshop_location = StockWarehouse(
        warehouse_ref1c="wh-workshop",
        warehouse_code="W",
        warehouse_name="Участок сборки модулей",
        is_selected=True,
    )
    employee_location = StockWarehouse(
        warehouse_ref1c="wh-person",
        warehouse_code="P",
        warehouse_name="Иванов Иван Иванович",
        is_selected=True,
    )
    db_session.add_all([workshop_a, workshop_b, warehouse_a, warehouse_b, workshop_location, employee_location])
    db_session.flush()

    saved = replace_settings(
        db_session,
        workshop_warehouses=[
            {"resource_id": workshop_a.resource_id, "warehouse_ref1c": "wh-a", "production_warehouse_ref1c": "wh-workshop"},
            {"resource_id": workshop_b.resource_id, "warehouse_ref1c": "wh-b"},
        ],
        ignored_warehouses=[{"warehouse_ref1c": "wh-b"}],
    )
    assert [row["warehouse_ref1c"] for row in saved["warehouses"]] == ["wh-a", "wh-b", "wh-workshop"]
    assert "wh-person" not in {row["warehouse_ref1c"] for row in saved["warehouses"]}
    assert saved["workshop_warehouses"] == saved["workshop_warehouse_bindings"]
    assert len(saved["workshop_warehouses"]) == 2
    assert saved["workshop_warehouses"][0]["resource_id"] == saved["workshop_warehouses"][0]["workshop_id"]
    assert saved["workshop_warehouses"][0]["production_warehouse_ref1c"] == "wh-workshop"
    assert db_session.query(WorkshopWarehouseBinding).count() == 2
    assert db_session.query(IgnoredWarehouse).count() == 1

    replaced = replace_settings(
        db_session,
        workshop_warehouses=[{"resource_id": workshop_a.resource_id, "warehouse_ref1c": "wh-b"}],
        ignored_warehouses=[],
    )
    assert len(replaced["workshop_warehouses"]) == 1
    assert replaced["workshop_warehouses"][0]["warehouse_ref1c"] == "wh-b"
    assert db_session.query(WorkshopWarehouseBinding).count() == 1
    assert db_session.query(IgnoredWarehouse).count() == 0
    assert "warehouses" in list_settings(db_session)


def test_ignored_warehouse_lifecycle(db_session):
    from app.services.production_control_settings import (
        delete_ignored_warehouse,
        list_settings,
        upsert_ignored_warehouse,
    )

    settings = list_settings(db_session)
    assert settings["ignored_warehouses"] == []

    added = upsert_ignored_warehouse(
        db_session,
        "deadbeef-0000-0000-0000-deadbeefcafe",
        warehouse_name="Изолятор брака",
        reason="Бракованные комплектующие",
    )
    assert added["warehouse_ref1c"] == "deadbeef-0000-0000-0000-deadbeefcafe"
    assert added["warehouse_name"] == "Изолятор брака"
    assert added["reason"] == "Бракованные комплектующие"

    # Update existing.
    updated = upsert_ignored_warehouse(
        db_session,
        "deadbeef-0000-0000-0000-deadbeefcafe",
        warehouse_name="Изолятор брака (обновлено)",
    )
    assert updated["warehouse_name"] == "Изолятор брака (обновлено)"

    listed = list_settings(db_session)["ignored_warehouses"]
    assert len(listed) == 1

    delete_ignored_warehouse(db_session, "deadbeef-0000-0000-0000-deadbeefcafe")
    assert list_settings(db_session)["ignored_warehouses"] == []


def test_create_material_issues_uses_workshop_binding_when_warehouse_not_pinned(db_session):
    """
    If the caller does not supply warehouse_ref1c, fall back to the
    workshop->warehouse binding from settings (plan: "привязка участок ->
    склад получатель"). If the caller does supply one, it wins.
    """
    from app.models import ProductionResource, WorkshopWarehouseBinding

    workshop = ProductionResource(resource_name="Цех сборки")
    db_session.add(workshop)
    db_session.flush()

    parent = Item(
        item_code="WH-PARENT",
        item_name="Parent",
        item_article="WH-P",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    comp = Item(
        item_code="WH-COMP",
        item_name="Comp",
        item_article="WH-C",
        unit="м",
        stock_qty=100,
        status="active",
    )
    db_session.add_all([parent, comp])
    db_session.flush()
    spec = Specification(spec_name="WH spec", spec_ref1c="wh-spec")
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))

    order = ProductionOrder(
        order_number="WH-001",
        order_date=datetime(2026, 5, 20),
        is_posted=True,
        deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
    )
    db_session.add(product)
    db_session.flush()
    state = ProductionOrderLineState(
        product_id=product.product_id,
        status="shortage",
        issue_status="not_requested",
        workshop_id=workshop.resource_id,
    )
    db_session.add(state)
    db_session.add(
        WorkshopWarehouseBinding(
            workshop_id=workshop.resource_id,
            warehouse_ref1c="aaaa1111-aaaa-1111-aaaa-111111111111",
        )
    )
    db_session.commit()

    res_default = create_material_issues(
        db_session,
        [product.product_id],
        initiated_by="op",
    )
    assert len(res_default["created"]) == 1
    issue_id = res_default["created"][0]["issue_id"]
    from app.models import ProductionMaterialIssue
    issue = db_session.query(ProductionMaterialIssue).filter_by(issue_id=issue_id).one()
    assert issue.warehouse_ref1c == "aaaa1111-aaaa-1111-aaaa-111111111111"

    # Caller-supplied warehouse_ref1c wins over the binding. Create a second
    # product to avoid the active-issue idempotency lock.
    product2 = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=2,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
    )
    db_session.add(product2)
    db_session.flush()
    db_session.add(
        ProductionOrderLineState(
            product_id=product2.product_id,
            status="shortage",
            issue_status="not_requested",
            workshop_id=workshop.resource_id,
        )
    )
    db_session.commit()

    res_explicit = create_material_issues(
        db_session,
        [product2.product_id],
        initiated_by="op",
        warehouse_ref1c="bbbb2222-bbbb-2222-bbbb-222222222222",
    )
    assert len(res_explicit["created"]) == 1
    issue2 = (
        db_session.query(ProductionMaterialIssue)
        .filter_by(issue_id=res_explicit["created"][0]["issue_id"])
        .one()
    )
    assert issue2.warehouse_ref1c == "bbbb2222-bbbb-2222-bbbb-222222222222"


def test_create_material_issues_does_not_auto_select_destination_as_source(db_session):
    from app.models import (
        ItemWarehouseStock,
        ProductionMaterialIssue,
        ProductionResource,
        StockWarehouse,
        WorkshopWarehouseBinding,
    )

    workshop = ProductionResource(resource_name="Сварочный участок")
    db_session.add(workshop)
    db_session.flush()

    parent = Item(item_code="SRC-PARENT", item_name="Parent", item_article="SRC-P", unit="шт", stock_qty=0, status="active")
    comp_a = Item(item_code="SRC-A", item_name="Comp A", item_article="SRC-A", unit="шт", stock_qty=0, status="active")
    comp_b = Item(item_code="SRC-B", item_name="Comp B", item_article="SRC-B", unit="шт", stock_qty=0, status="active")
    db_session.add_all([parent, comp_a, comp_b])
    db_session.flush()
    spec = Specification(spec_name="SRC spec", spec_ref1c="src-spec")
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add_all([
        SpecComponent(spec_id=spec.spec_id, item_id=comp_a.item_id, quantity=1),
        SpecComponent(spec_id=spec.spec_id, item_id=comp_b.item_id, quantity=1),
    ])

    order = ProductionOrder(order_number="SRC-001", order_date=datetime(2026, 5, 20), is_posted=True, deletion_mark=False)
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(order_id=order.order_id, item_id=parent.item_id, line_number=1, quantity=1, produced_qty=0, remaining_qty=1)
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, workshop_id=workshop.resource_id))
    db_session.add(WorkshopWarehouseBinding(workshop_id=workshop.resource_id, warehouse_ref1c="WH-DEST"))
    db_session.add_all([
        StockWarehouse(warehouse_ref1c="WH-DEST", warehouse_code="DEST", warehouse_name="Участок сварочный", is_selected=True),
        StockWarehouse(warehouse_ref1c="WH-A", warehouse_code="A", warehouse_name="Склад А", is_selected=True),
        StockWarehouse(warehouse_ref1c="WH-B", warehouse_code="B", warehouse_name="Склад Б", is_selected=True),
    ])
    db_session.add_all([
        ItemWarehouseStock(item_id=comp_a.item_id, warehouse_ref1c="WH-DEST", qty=10),
        ItemWarehouseStock(item_id=comp_b.item_id, warehouse_ref1c="WH-DEST", qty=10),
        ItemWarehouseStock(item_id=comp_a.item_id, warehouse_ref1c="WH-A", qty=5),
        ItemWarehouseStock(item_id=comp_b.item_id, warehouse_ref1c="WH-B", qty=5),
    ])
    db_session.commit()

    result = create_material_issues(db_session, [product.product_id], initiated_by="op")
    [created] = result["created"]
    issue = db_session.query(ProductionMaterialIssue).filter_by(issue_id=created["issue_id"]).one()

    assert issue.warehouse_ref1c == "WH-DEST"
    assert issue.source_warehouse_ref1c is None
    assert {row["ref1c"] for row in created["warehouse_candidates"]} == {"WH-A", "WH-B"}


# ---------------------------------------------------------------------------
# Coverage with ignored_warehouses + per-warehouse stock breakdown
# ---------------------------------------------------------------------------


def test_preview_materials_excludes_ignored_warehouses_from_stock(db_session):
    """
    Plan rule: "Игнорируемые склады нужны, чтобы не задавать лишние вопросы
    по остаткам, например если компонент лежит в изоляторе брака."

    With item_warehouse_stock populated AND an ignored_warehouses entry, the
    coverage calculation must use the per-warehouse breakdown and exclude
    stock sitting in ignored warehouses, even if Item.stock_qty (aggregated)
    suggests there's enough.
    """
    from app.models import ItemWarehouseStock
    from app.services.production_control_settings import upsert_ignored_warehouse

    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="IgnoreCheckParent",
        child_specs=[("IGNCMP", "Ignored-stock comp", 0, 1)],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    # Aggregated says we have 10 — but it's all in the brak isolator.
    comp.stock_qty = 10
    db_session.add(
        ItemWarehouseStock(
            item_id=comp.item_id,
            warehouse_ref1c="brak-warehouse-guid",
            qty=10,
        )
    )
    db_session.commit()

    # Before adding to the ignore list: coverage should be 'ready' (10 >= 2).
    preview = preview_materials(db_session, product.product_id, refresh_state=True)
    assert preview["coverage"] == "ready"
    assert preview["components"][0]["available_qty"] == 10
    assert preview["components"][0]["coverage"] == "ok"

    # Bump state back into the coverage band so the next preview can refresh
    # it (sticky-status guarantee from PR #4).
    state = (
        db_session.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    state.status = "shortage"
    db_session.commit()

    # Mark brak warehouse as ignored — that stock should drop out.
    upsert_ignored_warehouse(
        db_session,
        "brak-warehouse-guid",
        warehouse_name="Изолятор брака",
        reason="Бракованные комплектующие",
    )

    preview_after = preview_materials(db_session, product.product_id, refresh_state=True)
    assert preview_after["coverage"] == "shortage"
    only_comp = preview_after["components"][0]
    assert only_comp["available_qty"] == 0
    assert only_comp["missing_qty"] == 2
    assert only_comp["coverage"] == "shortage"


def test_preview_materials_falls_back_to_aggregated_when_no_breakdown(db_session):
    """
    With ignored_warehouses configured but no item_warehouse_stock rows for
    the component (e.g. stock hasn't been re-synced yet after the migration),
    fall back to aggregated Item.stock_qty so coverage doesn't collapse to 0
    during the rollout.
    """
    from app.services.production_control_settings import upsert_ignored_warehouse

    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="FallbackParent",
        child_specs=[("FBCMP", "Aggregated-only comp", 0, 1)],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)
    comp.stock_qty = 5  # aggregated value, no per-warehouse rows
    db_session.commit()

    upsert_ignored_warehouse(db_session, "some-other-warehouse")

    preview = preview_materials(db_session, product.product_id)
    # With no breakdown rows, fallback returns 5 -> need 2 -> ok.
    assert preview["coverage"] == "ready"
    assert preview["components"][0]["available_qty"] == 5
    assert preview["components"][0]["coverage"] == "ok"


def test_preview_materials_mixes_breakdown_and_aggregated_fallback(db_session):
    """
    Component A has per-warehouse breakdown with everything in an ignored
    warehouse -> 0 available. Component B has no breakdown rows at all ->
    falls back to aggregated Item.stock_qty.

    Order-level coverage aggregates per the plan: any 'shortage' -> shortage.
    So this case is a 'shortage' (blocked on comp A) despite comp B being
    fully covered. The point of the test is the per-component values:
    breakdown path is authoritative when present, aggregated is the fallback.
    """
    from app.models import ItemWarehouseStock
    from app.services.production_control_settings import upsert_ignored_warehouse

    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="MixParent",
        child_specs=[
            ("MIXA", "Comp A all-ignored", 50, 1),  # need 1*2 = 2
            ("MIXB", "Comp B aggregated only", 50, 1),  # need 1*2 = 2
        ],
    )
    comp_a, _comp_b = comps
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    db_session.add(
        ItemWarehouseStock(
            item_id=comp_a.item_id,
            warehouse_ref1c="brak-mix-guid",
            qty=50,
        )
    )
    db_session.commit()
    upsert_ignored_warehouse(db_session, "brak-mix-guid")

    preview = preview_materials(db_session, product.product_id)
    assert preview["coverage"] == "shortage"  # blocked by comp A
    by_name = {c["item_name"]: c for c in preview["components"]}
    assert by_name["Comp A all-ignored"]["available_qty"] == 0
    assert by_name["Comp A all-ignored"]["coverage"] == "shortage"
    assert by_name["Comp B aggregated only"]["available_qty"] == 50
    assert by_name["Comp B aggregated only"]["coverage"] == "ok"

import datetime as _dt
import json
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    DefaultSpecification,
    Item,
    PlannedOrder,
    PlanningRun,
    ProductionOrder,
    ProductionProduct,
    SpecComponent,
    Specification,
)
from app.services.production_control import (
    create_material_issues,
    create_orders_from_mrp,
    list_journal,
    preview_materials,
)


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

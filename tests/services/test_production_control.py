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
from app.services.production_control import create_material_issues, list_journal, preview_materials


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
    assert journal["rows"][0]["status"] == "new"

    materials = preview_materials(db_session, product.product_id)
    assert materials["components"][0]["component_item_id"] == component.item_id
    assert materials["components"][0]["required_qty"] == 20

    created = create_material_issues(db_session, [product.product_id], initiated_by="кладовщик")
    assert len(created["created"]) == 1
    assert created["created"][0]["lines_count"] == 1

    journal_after = list_journal(db_session)
    assert journal_after["rows"][0]["issue_status"] == "requested"
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

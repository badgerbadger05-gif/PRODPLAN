from datetime import datetime

from app.models import (
    DefaultSpecification,
    Item,
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

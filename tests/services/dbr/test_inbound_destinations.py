from datetime import date, datetime

from app.models import (
    Item,
    ProductionOrder,
    ProductionProduct,
    SupplierOrder,
    SupplierOrderItem,
)
from app.services.dbr import adapters


def test_open_inbound_uses_exact_destination_without_fanout(db_session):
    item = Item(item_code="SHARED", item_name="Общая позиция")
    db_session.add(item)
    db_session.flush()

    production_order = ProductionOrder(
        order_number="P1",
        order_date=datetime(2026, 8, 1),
        order_ref1c="p1",
        deletion_mark=False,
    )
    supplier_order = SupplierOrder(
        order_number="S1",
        order_date=datetime(2026, 8, 2),
        order_ref1c="s1",
        deletion_mark=False,
    )
    db_session.add_all([production_order, supplier_order])
    db_session.flush()
    db_session.add_all(
        [
            ProductionProduct(
                order_id=production_order.order_id,
                item_id=item.item_id,
                quantity=5,
                produced_qty=0,
                remaining_qty=5,
                destination_warehouse_ref1c="W2",
            ),
            ProductionProduct(
                order_id=production_order.order_id,
                item_id=item.item_id,
                quantity=11,
                produced_qty=0,
                remaining_qty=11,
                destination_warehouse_ref1c=None,
            ),
            SupplierOrderItem(
                order_id=supplier_order.order_id,
                item_id_ref=item.item_id,
                quantity=7,
                received_qty=0,
                remaining_qty=7,
                delivery_date=datetime(2026, 8, 5),
                destination_warehouse_ref1c="W4",
            ),
        ]
    )
    db_session.flush()

    inbound, diagnostics = adapters.open_inbound_with_diagnostics(
        db_session,
        {("SHARED", "W2"), ("SHARED", "W4")},
        {"SHARED": item.item_id},
        {item.item_id: "SHARED"},
    )

    assert inbound == [
        ("SHARED", "W2", date(2026, 8, 1), 5.0),
        ("SHARED", "W4", date(2026, 8, 5), 7.0),
    ]
    assert sum(row[3] for row in inbound) == 12
    assert diagnostics == {
        "included": 2,
        "excluded_null_destination": 1,
        "excluded_destination_not_needed": 0,
        "excluded_missing_eta": 0,
    }


def test_open_inbound_excludes_destination_not_requested(db_session):
    item = Item(item_code="ONLY-W2", item_name="Позиция")
    order = ProductionOrder(
        order_number="P2",
        order_date=datetime(2026, 8, 1),
        order_ref1c="p2",
        deletion_mark=False,
    )
    db_session.add_all([item, order])
    db_session.flush()
    db_session.add(
        ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            quantity=3,
            produced_qty=0,
            remaining_qty=3,
            destination_warehouse_ref1c="W4",
        )
    )
    db_session.flush()

    inbound, diagnostics = adapters.open_inbound_with_diagnostics(
        db_session,
        {("ONLY-W2", "W2")},
        {"ONLY-W2": item.item_id},
        {item.item_id: "ONLY-W2"},
    )

    assert inbound == []
    assert diagnostics["excluded_destination_not_needed"] == 1

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.models import (
    DbrDrumSchedule,
    DbrSupermarketPosition,
    IgnoredWarehouse,
    Item,
    ItemWarehouseStock,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    StockWarehouse,
    SupplierOrder,
    SupplierOrderItem,
)
from app.services.dbr import feeder_nfp_service


def _position(db, item, warehouse, supply="manufacture", source_schedule=None):
    row = DbrSupermarketPosition(
        item_id=item.item_id,
        warehouse_ref1c=warehouse,
        supply_type=supply,
        mode="shelf",
        adu=1,
        commonality=1,
        rt_days=1,
        batch_days=1,
        q_batch=10,
        k_var=Decimal("0.5"),
        supply_risk_pct=0,
        red_qty=10,
        yellow_qty=10,
        green_qty=10,
        target_qty=30,
        source_schedule_id=source_schedule.id if source_schedule else None,
        data_quality=[],
        calculation_snapshot={},
    )
    db.add(row)
    db.flush()
    return row


def _active_schedule(db):
    schedule = DbrDrumSchedule(
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="active",
    )
    db.add(schedule)
    db.flush()
    return schedule


def _reserve(db, component, warehouse, qty, suffix=""):
    parent = Item(item_code=f"PARENT-{component.item_id}{suffix}", item_name="Parent")
    order = ProductionOrder(
        order_number=f"RES-{component.item_id}{suffix}",
        order_date=datetime(2026, 8, 1),
        order_ref1c=f"res-{component.item_id}{suffix}",
        deletion_mark=False,
    )
    db.add_all([parent, order])
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        quantity=1,
        produced_qty=0,
        remaining_qty=1,
    )
    db.add(product)
    db.flush()
    db.add(ProductionOrderLineState(product_id=product.product_id, status="assembled"))
    issue = ProductionMaterialIssue(
        document_number=f"ISS-{component.item_id}{suffix}",
        product_id=product.product_id,
        order_id=order.order_id,
        status="posted",
        direction="issue",
        warehouse_ref1c=warehouse,
        source_warehouse_ref1c="SOURCE",
    )
    db.add(issue)
    db.flush()
    db.add(
        ProductionMaterialIssueLine(
            issue_id=issue.issue_id,
            component_item_id=component.item_id,
            required_qty=qty,
            issued_qty=qty,
            line_status="issued",
        )
    )


def test_manufacture_exact_stock_reservation_inbound_and_zero_nfp(db_session):
    schedule = _active_schedule(db_session)
    item = Item(item_code="MAKE", item_name="Make")
    db_session.add(item)
    db_session.flush()
    position = _position(db_session, item, "w3", source_schedule=schedule)
    stock_updated_at = datetime(2026, 8, 2, 9, 0)
    db_session.add_all(
        [
            ItemWarehouseStock(
                item_id=item.item_id, warehouse_ref1c="W3", qty=5,
                updated_at=stock_updated_at,
            ),
            ItemWarehouseStock(item_id=item.item_id, warehouse_ref1c="OTHER", qty=100),
        ]
    )
    inbound_order = ProductionOrder(
        order_number="IN-M", order_date=datetime(2026, 8, 1),
        order_ref1c="in-m", deletion_mark=False,
        updated_at=datetime(2026, 8, 2, 10, 0),
    )
    db_session.add(inbound_order)
    db_session.flush()
    db_session.add_all(
        [
            ProductionProduct(
                order_id=inbound_order.order_id, item_id=item.item_id, quantity=3,
                produced_qty=0, remaining_qty=3, destination_warehouse_ref1c="W3",
                updated_at=datetime(2026, 8, 2, 11, 0),
            ),
            ProductionProduct(
                order_id=inbound_order.order_id, item_id=item.item_id, quantity=7,
                produced_qty=0, remaining_qty=7, destination_warehouse_ref1c=None,
                updated_at=datetime(2026, 8, 2, 12, 0),
            ),
        ]
    )
    _reserve(db_session, item, "W3", 8)
    db_session.flush()

    live = feeder_nfp_service.live_nfp_rows(db_session, [position])[position.id]

    assert live["stock_qty"] == 5
    assert live["open_supply_qty"] == 3
    assert live["qualified_demand_qty"] == 8
    assert live["nfp"] == 0
    assert live["zone"] == "Red"
    assert live["penetration"] == pytest.approx(1.0)
    assert live["is_complete"] is False
    assert "open_supply_destination_missing" in live["missing_reasons"]
    assert live["timestamps"]["stock_as_of"] == stock_updated_at
    assert live["timestamps"]["supply_as_of"] == datetime(2026, 8, 2, 12, 0)


def test_purchase_stock_selected_nonignored_and_exact_supply(db_session):
    schedule = _active_schedule(db_session)
    item = Item(item_code="BUY", item_name="Buy")
    db_session.add(item)
    db_session.flush()
    position = _position(db_session, item, "W4", supply="purchase", source_schedule=schedule)
    db_session.add_all(
        [
            StockWarehouse(warehouse_ref1c="a", warehouse_name="A", is_selected=True),
            StockWarehouse(warehouse_ref1c="B", warehouse_name="B", is_selected=True),
            StockWarehouse(warehouse_ref1c="C", warehouse_name="C", is_selected=False),
            IgnoredWarehouse(warehouse_ref1c="b"),
            ItemWarehouseStock(
                item_id=item.item_id, warehouse_ref1c="A", qty=10,
                updated_at=datetime(2026, 8, 3, 9, 0),
            ),
            ItemWarehouseStock(item_id=item.item_id, warehouse_ref1c="B", qty=20),
            ItemWarehouseStock(item_id=item.item_id, warehouse_ref1c="C", qty=30),
        ]
    )
    order = SupplierOrder(
        order_number="IN-P", order_date=datetime(2026, 8, 1),
        order_ref1c="in-p", deletion_mark=False,
        updated_at=datetime(2026, 8, 3, 10, 0),
    )
    db_session.add(order)
    db_session.flush()
    db_session.add_all(
        [
            SupplierOrderItem(
                order_id=order.order_id, item_id_ref=item.item_id, quantity=5,
                received_qty=0, remaining_qty=5, destination_warehouse_ref1c="W4",
                updated_at=datetime(2026, 8, 3, 11, 0),
            ),
            SupplierOrderItem(
                order_id=order.order_id, item_id_ref=item.item_id, quantity=4,
                received_qty=0, remaining_qty=4, destination_warehouse_ref1c=None,
                updated_at=datetime(2026, 8, 3, 12, 0),
            ),
        ]
    )
    _reserve(db_session, item, "A", 4, suffix="-A")
    _reserve(db_session, item, "B", 6, suffix="-B")
    db_session.flush()

    live = feeder_nfp_service.live_nfp_rows(db_session, [position])[position.id]

    assert live["stock_qty"] == 10
    assert live["open_supply_qty"] == 5
    assert live["qualified_demand_qty"] == 4
    assert live["nfp"] == 11
    assert live["zone"] == "Yellow"
    assert live["is_complete"] is False
    assert live["timestamps"]["stock_as_of"] == datetime(2026, 8, 3, 9, 0)
    assert live["timestamps"]["supply_as_of"] == datetime(2026, 8, 3, 12, 0)


def test_stale_schedule_marker(db_session):
    active = _active_schedule(db_session)
    old = DbrDrumSchedule(
        period_from=date(2026, 7, 1), period_to=date(2026, 7, 31), status="superseded"
    )
    item = Item(item_code="STALE", item_name="Stale")
    db_session.add_all([old, item])
    db_session.flush()
    position = _position(db_session, item, "W3", source_schedule=old)

    live = feeder_nfp_service.live_nfp_rows(db_session, [position])[position.id]

    assert active.id != old.id
    assert live["is_complete"] is False
    assert "stale_schedule" in live["missing_reasons"]

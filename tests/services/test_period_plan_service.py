"""Tests for create_mrp_snapshot_from_period_plan — purchase allocation
with supplier-order netting.
"""

import datetime
from datetime import date

import pytest

from app.models import (
    DefaultSpecification,
    Item,
    MrpRequirement,
    PlannedPurchase,
    PlanningRun,
    ProductionPlanHeader,
    ProductionPlanLine,
    SpecComponent,
    Specification,
    SupplierOrder,
    SupplierOrderItem,
)
from app.services.period_plan_service import create_mrp_snapshot_from_period_plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_purchased_item(db, code: str, stock: float = 0.0) -> Item:
    item = Item(
        item_code=code,
        item_name=f"Закупаемая деталь {code}",
        item_article=code,
        unit="шт",
        stock_qty=stock,
        replenishment_method="Покупка",
        replenishment_time=3,
        status="active",
    )
    db.add(item)
    db.flush()
    return item


def _make_fixed_plan(
    db,
    item: Item,
    bucket_date: date,
    qty: float,
    period_from: date | None = None,
    period_to: date | None = None,
) -> ProductionPlanHeader:
    if period_from is None:
        period_from = bucket_date
    if period_to is None:
        period_to = bucket_date
    plan = ProductionPlanHeader(
        name="Test plan",
        period_from=period_from,
        period_to=period_to,
        status="fixed",
        created_by="test",
    )
    db.add(plan)
    db.flush()
    db.add(
        ProductionPlanLine(
            plan_id=plan.id,
            item_id=item.item_id,
            bucket_date=bucket_date,
            qty=qty,
        )
    )
    db.flush()
    return plan


def _make_supplier_order(
    db,
    item: Item,
    remaining_qty: float,
    delivery_date: date,
    state_name: str = "подтверждён",
    state_key: str = "some-key-001",
) -> SupplierOrder:
    so = SupplierOrder(
        order_number="SO-TEST",
        order_date=datetime.datetime(2026, 1, 1),
        order_ref1c=f"ref-{id(item)}-{delivery_date}",
        is_posted=True,
        deletion_mark=False,
        order_state_key=state_key,
        order_state_name=state_name,
    )
    db.add(so)
    db.flush()
    db.add(
        SupplierOrderItem(
            order_id=so.order_id,
            item_id_ref=item.item_id,
            quantity=remaining_qty,
            received_qty=0.0,
            remaining_qty=remaining_qty,
            delivery_date=datetime.datetime.combine(delivery_date, datetime.time()),
        )
    )
    db.flush()
    return so


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPurchaseAllocationNoSupplierOrders:
    """When no supplier orders exist, full net demand becomes PlannedPurchase."""

    def test_creates_planned_purchase_for_full_net_qty(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-001", stock=0.0)
        plan = _make_fixed_plan(db_session, item, bucket, qty=50.0)

        result = create_mrp_snapshot_from_period_plan(db_session, plan.id)

        assert result["purchase_count"] == 1

        pp = db_session.query(PlannedPurchase).filter_by(run_id=result["run_id"]).one()
        assert float(pp.requested_qty) == 50.0
        assert float(pp.planned_qty) == 50.0
        assert float(pp.qty) == 50.0

    def test_source_mrp_requirement_id_is_linked(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-002")
        plan = _make_fixed_plan(db_session, item, bucket, qty=30.0)

        result = create_mrp_snapshot_from_period_plan(db_session, plan.id)

        pp = db_session.query(PlannedPurchase).filter_by(run_id=result["run_id"]).one()
        req = db_session.query(MrpRequirement).filter_by(run_id=result["run_id"]).one()
        assert pp.source_mrp_requirement_id == req.id

    def test_stock_reduces_net_demand_and_planned_purchase(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-003", stock=15.0)
        plan = _make_fixed_plan(db_session, item, bucket, qty=50.0)

        result = create_mrp_snapshot_from_period_plan(db_session, plan.id)

        assert result["purchase_count"] == 1
        pp = db_session.query(PlannedPurchase).filter_by(run_id=result["run_id"]).one()
        # net = 50 - 15 = 35; no supplier → planned_qty = 35
        assert float(pp.planned_qty) == pytest.approx(35.0)
        assert float(pp.requested_qty) == pytest.approx(35.0)


class TestPurchaseAllocationSupplierFullyCoversDemand:
    """Supplier order fully covers bucket demand → no PlannedPurchase row."""

    def test_no_planned_purchase_created(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-FULL")
        plan = _make_fixed_plan(db_session, item, bucket, qty=20.0)
        # Supplier delivers on or before the bucket date — covers all 20 units
        _make_supplier_order(db_session, item, remaining_qty=20.0, delivery_date=bucket)

        result = create_mrp_snapshot_from_period_plan(db_session, plan.id)

        assert result["purchase_count"] == 0
        assert db_session.query(PlannedPurchase).filter_by(run_id=result["run_id"]).count() == 0

    def test_supplier_surplus_does_not_create_negative_purchase(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-SURPLUS")
        plan = _make_fixed_plan(db_session, item, bucket, qty=10.0)
        _make_supplier_order(db_session, item, remaining_qty=50.0, delivery_date=bucket)

        result = create_mrp_snapshot_from_period_plan(db_session, plan.id)

        assert result["purchase_count"] == 0

    def test_supplier_early_delivery_covers_demand(self, db_session):
        """Supplier arriving before bucket date still covers that bucket."""
        bucket = date(2026, 6, 9)
        item = _make_purchased_item(db_session, "BUY-EARLY")
        plan = _make_fixed_plan(db_session, item, bucket, qty=15.0)
        _make_supplier_order(db_session, item, remaining_qty=15.0, delivery_date=date(2026, 6, 2))

        result = create_mrp_snapshot_from_period_plan(db_session, plan.id)

        assert result["purchase_count"] == 0

    def test_supplier_late_delivery_does_not_cover(self, db_session):
        """Supplier arriving after bucket date cannot cover that bucket's demand."""
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-LATE")
        plan = _make_fixed_plan(
            db_session, item, bucket, qty=20.0,
            period_from=date(2026, 6, 2), period_to=date(2026, 6, 30),
        )
        # Supplier delivers after the bucket → cannot cover week-1 demand
        _make_supplier_order(db_session, item, remaining_qty=20.0, delivery_date=date(2026, 6, 16))

        result = create_mrp_snapshot_from_period_plan(db_session, plan.id)

        # Week-1 demand unmet; supplier delivers outside period_to would be ignored,
        # but delivery_date(June 16) <= period_to(June 30) so the order IS loaded —
        # yet it cannot cover the June-2 bucket because delivery is June 16 > June 2.
        assert result["purchase_count"] == 1
        pp = db_session.query(PlannedPurchase).filter_by(run_id=result["run_id"]).one()
        assert float(pp.planned_qty) == pytest.approx(20.0)


class TestPurchaseAllocationSupplierPartiallyCoversDemand:
    """Supplier order covers only part of demand → PlannedPurchase for remainder."""

    def test_planned_purchase_qty_is_remainder(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-PART")
        plan = _make_fixed_plan(db_session, item, bucket, qty=30.0)
        _make_supplier_order(db_session, item, remaining_qty=10.0, delivery_date=bucket)

        result = create_mrp_snapshot_from_period_plan(db_session, plan.id)

        assert result["purchase_count"] == 1
        pp = db_session.query(PlannedPurchase).filter_by(run_id=result["run_id"]).one()
        # requested = full bucket net demand; planned = net after supplier
        assert float(pp.requested_qty) == pytest.approx(30.0)
        assert float(pp.planned_qty) == pytest.approx(20.0)
        assert float(pp.qty) == pytest.approx(20.0)

    def test_coverage_qty_reflects_full_demand(self, db_session):
        """MrpRequirement.covered_qty must equal total net demand (supplier + planned purchase)."""
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-COV")
        plan = _make_fixed_plan(db_session, item, bucket, qty=40.0)
        _make_supplier_order(db_session, item, remaining_qty=15.0, delivery_date=bucket)

        result = create_mrp_snapshot_from_period_plan(db_session, plan.id)

        req = db_session.query(MrpRequirement).filter_by(run_id=result["run_id"]).one()
        assert float(req.covered_qty) == pytest.approx(40.0)
        assert float(req.remaining_qty) == pytest.approx(0.0)


class TestPurchaseAllocationExcludedStates:
    """Orders in excluded states must not be used as supply."""

    @pytest.mark.parametrize("state_name", ["новый заказ", "отменен", "завершен", "бухгалтерия"])
    def test_excluded_state_order_not_consumed(self, db_session, state_name):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, f"BUY-EXC-{state_name[:4]}")
        plan = _make_fixed_plan(db_session, item, bucket, qty=25.0)
        _make_supplier_order(
            db_session, item, remaining_qty=25.0, delivery_date=bucket,
            state_name=state_name, state_key="exc-key",
        )

        result = create_mrp_snapshot_from_period_plan(db_session, plan.id)

        # Excluded order → full demand still needs PlannedPurchase
        assert result["purchase_count"] == 1
        pp = db_session.query(PlannedPurchase).filter_by(run_id=result["run_id"]).one()
        assert float(pp.planned_qty) == pytest.approx(25.0)

    def test_deleted_order_not_consumed(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-DEL")
        plan = _make_fixed_plan(db_session, item, bucket, qty=20.0)

        so = SupplierOrder(
            order_number="SO-DEL",
            order_date=datetime.datetime(2026, 1, 1),
            order_ref1c="ref-deleted",
            is_posted=True,
            deletion_mark=True,  # deleted!
            order_state_key="some-key",
            order_state_name="подтверждён",
        )
        db_session.add(so)
        db_session.flush()
        db_session.add(
            SupplierOrderItem(
                order_id=so.order_id,
                item_id_ref=item.item_id,
                quantity=20.0,
                received_qty=0.0,
                remaining_qty=20.0,
                delivery_date=datetime.datetime(2026, 6, 2),
            )
        )
        db_session.flush()

        result = create_mrp_snapshot_from_period_plan(db_session, plan.id)

        assert result["purchase_count"] == 1
        pp = db_session.query(PlannedPurchase).filter_by(run_id=result["run_id"]).one()
        assert float(pp.planned_qty) == pytest.approx(20.0)


class TestPurchaseAllocationMultiBucket:
    """Supplier quantity spans multiple demand buckets chronologically."""

    def test_supplier_consumed_across_buckets(self, db_session):
        """Supplier with 25 units: covers week-1 (20 units) → 5 left for week-2.
        Week-2 needs 30 → PlannedPurchase(25) for week-2."""
        period_from = date(2026, 6, 2)
        period_to = date(2026, 6, 16)
        item = _make_purchased_item(db_session, "BUY-MULTI")

        plan = ProductionPlanHeader(
            name="Multi-bucket plan",
            period_from=period_from,
            period_to=period_to,
            status="fixed",
            created_by="test",
        )
        db_session.add(plan)
        db_session.flush()
        week1 = date(2026, 6, 2)
        week2 = date(2026, 6, 9)
        db_session.add_all([
            ProductionPlanLine(plan_id=plan.id, item_id=item.item_id, bucket_date=week1, qty=20.0),
            ProductionPlanLine(plan_id=plan.id, item_id=item.item_id, bucket_date=week2, qty=30.0),
        ])
        db_session.flush()

        # Supplier delivers 25 units at week1 — covers all of week1 (20) and 5 of week2
        _make_supplier_order(db_session, item, remaining_qty=25.0, delivery_date=week1)

        result = create_mrp_snapshot_from_period_plan(db_session, plan.id)

        purchases = (
            db_session.query(PlannedPurchase)
            .filter_by(run_id=result["run_id"])
            .order_by(PlannedPurchase.need_date)
            .all()
        )
        # Week-1: fully covered → no purchase; Week-2: 25 remaining, supplier gave 5 → buy 25
        assert len(purchases) == 1
        assert float(purchases[0].need_date.strftime("%Y-%m-%d").replace("-", "")) == pytest.approx(
            float(week2.strftime("%Y-%m-%d").replace("-", ""))
        )
        assert float(purchases[0].requested_qty) == pytest.approx(30.0)
        assert float(purchases[0].planned_qty) == pytest.approx(25.0)

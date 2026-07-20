"""Tests for the fixed-MRP execution ledger (PHASE 2).

populate_executed_qty recomputes MrpRequirement.executed_qty from actual
production (produced_qty) and receipt (received_qty) facts. It is a
recompute-aggregate, idempotent computation: it zeroes executed_qty and
re-derives it wholesale, never reading executed_qty as input.
"""

from datetime import date, datetime

from app.models import (
    Item,
    MrpRequirement,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SupplierOrder,
    SupplierOrderItem,
)
from app.services.mrp_execution_ledger import populate_executed_qty


# ---------------------------------------------------------------------------
# Helpers (mirror tests/services/test_mrp_reconciliation.py)
# ---------------------------------------------------------------------------

def _make_production_item(db, code: str, stock: float = 0.0) -> Item:
    item = Item(
        item_code=code,
        item_name=f"Изделие {code}",
        item_article=code,
        unit="шт",
        stock_qty=stock,
        replenishment_method="Производство",
        replenishment_time=0,
        status="active",
    )
    db.add(item)
    db.flush()
    return item


def _make_purchased_item(db, code: str, stock: float = 0.0) -> Item:
    item = Item(
        item_code=code,
        item_name=f"Деталь {code}",
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


def _make_run(db, *, period_from: date, period_to: date) -> PlanningRun:
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        pinned=True,
        source_plan_id=None,
        period_from=period_from,
        period_to=period_to,
    )
    db.add(run)
    db.flush()
    return run


def _make_req(db, run, item, *, net, bom_level=0, status="open") -> MrpRequirement:
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=net,
        net_required_qty=net,
        covered_qty=0.0,
        remaining_qty=net,
        period_from=run.period_from,
        period_to=run.period_to,
        bom_level=bom_level,
        status=status,
    )
    db.add(req)
    db.flush()
    return req


def _make_production_line(
    db,
    item,
    *,
    quantity,
    produced,
    req=None,
    source="mrp",
    status="in_progress",
    order_ref1c=None,
    deletion_mark=False,
    order_number=None,
) -> ProductionProduct:
    order = ProductionOrder(
        order_number=order_number or f"PO-{item.item_code}-{quantity}-{produced}",
        order_date=datetime(2026, 6, 1),
        is_posted=False,
        deletion_mark=deletion_mark,
        source=source,
        order_ref1c=order_ref1c,
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=quantity,
        produced_qty=produced,
        remaining_qty=max(quantity - produced, 0.0),
        source_mrp_requirement_id=req.id if req else None,
    )
    db.add(product)
    db.flush()
    if status is not None:
        db.add(ProductionOrderLineState(product_id=product.product_id, status=status))
        db.flush()
    return product


def _make_receipt(
    db,
    item,
    *,
    received,
    quantity=None,
    state_name="Принят на склад",
    deletion_mark=False,
    order_ref1c=None,
) -> SupplierOrderItem:
    order = SupplierOrder(
        order_number=f"SO-{item.item_code}-{received}",
        order_date=datetime(2026, 6, 1),
        order_ref1c=order_ref1c,
        deletion_mark=deletion_mark,
        order_state_name=state_name,
    )
    db.add(order)
    db.flush()
    qty = quantity if quantity is not None else received
    line = SupplierOrderItem(
        order_id=order.order_id,
        item_id_ref=item.item_id,
        line_number=1,
        quantity=qty,
        received_qty=received,
        remaining_qty=max(qty - received, 0.0),
    )
    db.add(line)
    db.flush()
    return line


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_linked_production_executed_is_min_produced_quantity(db_session):
    item = _make_production_item(db_session, "L-PROD")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req = _make_req(db_session, run, item, net=40)
    _make_production_line(db_session, item, quantity=40, produced=25, req=req)
    db_session.commit()

    summary = populate_executed_qty(db_session, [run.run_id])
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 25.0
    assert summary["items_touched"] == 1
    assert abs(summary["total_executed"] - 25.0) < 1e-6


def test_linked_receipt_executed_reflects_received_qty(db_session):
    item = _make_purchased_item(db_session, "L-RECV")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req = _make_req(db_session, run, item, net=50)
    _make_receipt(db_session, item, received=30, quantity=50)
    db_session.commit()

    populate_executed_qty(db_session, [run.run_id])
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 30.0


def test_fifo_oldest_plan_first_across_two_runs(db_session):
    item = _make_production_item(db_session, "FIFO-ITEM")
    old_run = _make_run(db_session, period_from=date(2026, 5, 1), period_to=date(2026, 5, 31))
    new_run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    old_req = _make_req(db_session, old_run, item, net=40)
    new_req = _make_req(db_session, new_run, item, net=40)
    # Unlinked 1C production pool of 50 for the item.
    _make_production_line(db_session, item, quantity=50, produced=50, req=None, source="1c")
    db_session.commit()

    populate_executed_qty(db_session, [old_run.run_id, new_run.run_id])
    db_session.commit()

    db_session.refresh(old_req)
    db_session.refresh(new_req)
    # Older plan is filled to its net first; the remainder spills to the newer.
    assert float(old_req.executed_qty) == 40.0
    assert float(new_req.executed_qty) == 10.0


def test_cap_at_net_for_production_and_receipt(db_session):
    prod = _make_production_item(db_session, "CAP-PROD")
    buy = _make_purchased_item(db_session, "CAP-BUY")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    prod_req = _make_req(db_session, run, prod, net=40)
    buy_req = _make_req(db_session, run, buy, net=40)
    # Linked over-production: min(produced, quantity)=100, capped at net 40.
    _make_production_line(db_session, prod, quantity=100, produced=100, req=prod_req)
    # Receipt pool of 100, capped at net 40 by FIFO capacity.
    _make_receipt(db_session, buy, received=100, quantity=100)
    db_session.commit()

    populate_executed_qty(db_session, [run.run_id])
    db_session.commit()

    db_session.refresh(prod_req)
    db_session.refresh(buy_req)
    assert float(prod_req.executed_qty) == 40.0
    assert float(buy_req.executed_qty) == 40.0


def test_idempotent_double_run(db_session):
    item = _make_production_item(db_session, "IDEM-ITEM")
    buy = _make_purchased_item(db_session, "IDEM-BUY")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    prod_req = _make_req(db_session, run, item, net=40)
    buy_req = _make_req(db_session, run, buy, net=50)
    _make_production_line(db_session, item, quantity=40, produced=15, req=prod_req)
    _make_production_line(db_session, item, quantity=100, produced=100, req=None, source="1c")
    _make_receipt(db_session, buy, received=30, quantity=50)
    db_session.commit()

    populate_executed_qty(db_session, [run.run_id])
    db_session.commit()
    first = {
        int(r.id): float(r.executed_qty)
        for r in db_session.query(MrpRequirement).filter(MrpRequirement.run_id == run.run_id).all()
    }

    populate_executed_qty(db_session, [run.run_id])
    db_session.commit()
    second = {
        int(r.id): float(r.executed_qty)
        for r in db_session.query(MrpRequirement).filter(MrpRequirement.run_id == run.run_id).all()
    }

    assert first == second
    # prod_req: linked 15 (capped none) then pool spills to fill to net 40.
    assert first[prod_req.id] == 40.0
    assert first[buy_req.id] == 30.0


def test_cancelled_line_excluded(db_session):
    item = _make_production_item(db_session, "CANCEL-ITEM")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req = _make_req(db_session, run, item, net=40)
    _make_production_line(db_session, item, quantity=40, produced=30, req=req, status="cancelled")
    db_session.commit()

    populate_executed_qty(db_session, [run.run_id])
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 0.0


def test_unlinked_pool_not_double_counted_across_two_plans(db_session):
    item = _make_production_item(db_session, "POOL-ITEM")
    run_a = _make_run(db_session, period_from=date(2026, 5, 1), period_to=date(2026, 5, 31))
    run_b = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req_a = _make_req(db_session, run_a, item, net=40)
    req_b = _make_req(db_session, run_b, item, net=40)
    # A single unlinked 1C production fact of 40 for the item.
    _make_production_line(db_session, item, quantity=40, produced=40, req=None, source="1c")
    db_session.commit()

    summary = populate_executed_qty(db_session, [run_a.run_id, run_b.run_id])
    db_session.commit()

    db_session.refresh(req_a)
    db_session.refresh(req_b)
    # The 40-unit fact is consumed once by the older plan, not counted in both.
    assert float(req_a.executed_qty) == 40.0
    assert float(req_b.executed_qty) == 0.0
    assert abs(summary["total_executed"] - 40.0) < 1e-6


def test_closed_requirements_are_not_populated(db_session):
    item = _make_production_item(db_session, "CLOSED-ITEM")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req = _make_req(db_session, run, item, net=40, status="closed")
    _make_production_line(db_session, item, quantity=40, produced=40, req=req)
    db_session.commit()

    summary = populate_executed_qty(db_session, [run.run_id])
    db_session.commit()

    db_session.refresh(req)
    # Closed requirements are skipped entirely (a later phase owns them).
    assert float(req.executed_qty) == 0.0
    assert summary["items_touched"] == 0

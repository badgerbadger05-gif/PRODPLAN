"""Tests for MRP reconciliation and the covered_qty rollback that feeds it."""

from datetime import date

from app.models import (
    Item,
    MrpRequirement,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionProduct,
)
from app.services.mrp_reconciliation import reconcile_snapshot
from app.services.period_plan_service import create_mrp_snapshot_from_period_plan
from app.services.production_control_journal import (
    create_production_orders_from_mrp_requirements,
    update_line_state,
    update_product_quantity,
)


# ---------------------------------------------------------------------------
# Helpers
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


def _make_requirement_with_line(db, item, *, net, covered, produced, quantity):
    """A standalone requirement + materialized production line (no snapshot)."""
    run = PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={}, pinned=True)
    db.add(run)
    db.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=net,
        net_required_qty=net,
        covered_qty=covered,
        remaining_qty=max(net - covered, 0.0),
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        bom_level=0,
    )
    db.add(req)
    db.flush()
    order = ProductionOrder(
        order_number=f"O-{item.item_code}",
        order_date=date(2026, 6, 1),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
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
        source_mrp_requirement_id=req.id,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="in_progress",
            issue_status="not_requested",
        )
    )
    db.flush()
    return req, product


# ---------------------------------------------------------------------------
# Part 1 — covered_qty rollback
# ---------------------------------------------------------------------------

def test_closing_partial_line_releases_requirement_coverage(db_session):
    item = _make_production_item(db_session, "P-CLOSE")
    req, product = _make_requirement_with_line(
        db_session, item, net=40, covered=40, produced=10, quantity=40
    )

    update_line_state(db_session, product.product_id, {"status": "completed"})

    db_session.refresh(req)
    db_session.refresh(product)
    assert float(product.remaining_qty) == 0.0
    # 30 un-produced units released back into the requirement's open demand.
    assert float(req.covered_qty) == 10.0
    assert float(req.remaining_qty) == 30.0


def test_closing_fully_produced_line_releases_nothing(db_session):
    item = _make_production_item(db_session, "P-FULL")
    req, product = _make_requirement_with_line(
        db_session, item, net=40, covered=40, produced=40, quantity=40
    )

    update_line_state(db_session, product.product_id, {"status": "completed"})

    db_session.refresh(req)
    assert float(req.covered_qty) == 40.0
    assert float(req.remaining_qty) == 0.0


def test_reducing_quantity_releases_coverage(db_session):
    item = _make_production_item(db_session, "P-REDUCE")
    req, product = _make_requirement_with_line(
        db_session, item, net=40, covered=40, produced=10, quantity=40
    )

    update_product_quantity(db_session, product.product_id, 25)

    db_session.refresh(req)
    assert float(req.covered_qty) == 25.0
    assert float(req.remaining_qty) == 15.0


def test_cancel_then_terminal_again_is_idempotent(db_session):
    item = _make_production_item(db_session, "P-IDEM")
    req, product = _make_requirement_with_line(
        db_session, item, net=40, covered=40, produced=10, quantity=40
    )

    update_line_state(db_session, product.product_id, {"status": "cancelled"})
    update_line_state(db_session, product.product_id, {"status": "completed"})

    db_session.refresh(req)
    # Release happens once: a second terminal transition must not double-release.
    assert float(req.covered_qty) == 10.0
    assert float(req.remaining_qty) == 30.0


# ---------------------------------------------------------------------------
# Part 2 — reconciliation top-up
# ---------------------------------------------------------------------------

def test_reconcile_tops_up_after_partial_close(db_session):
    item = _make_production_item(db_session, "P-RECON", stock=0.0)
    plan = ProductionPlanHeader(
        name="План июнь",
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        status="fixed",
        created_by="test",
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(
        ProductionPlanLine(
            plan_id=plan.id, item_id=item.item_id, bucket_date=date(2026, 6, 15), qty=40
        )
    )
    db_session.commit()

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
    run_id = snap["run_id"]
    req = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == item.item_id)
        .one()
    )
    assert float(req.net_required_qty) == 40.0

    # Materialize the production order from the requirement → becomes open WIP.
    create_production_orders_from_mrp_requirements(db_session, [req.id])
    db_session.commit()

    # With the full order open as WIP, reconciliation finds no gap.
    res = reconcile_snapshot(db_session, run_id)
    assert res["production_added"] == []

    # Produce 10, close the line: 30 un-produced units are released; the 10
    # produced are returned to stock (simulate the stock effect).
    product = (
        db_session.query(ProductionProduct)
        .filter(ProductionProduct.source_mrp_requirement_id == req.id)
        .one()
    )
    product.produced_qty = 10
    product.remaining_qty = 30
    db_session.flush()
    update_line_state(db_session, product.product_id, {"status": "completed"})
    item.stock_qty = 10
    db_session.commit()

    # Now reconciliation must create a catch-up order for the residual 30.
    res = reconcile_snapshot(db_session, run_id)
    added = res["production_added"]
    assert len(added) == 1
    assert added[0]["item_id"] == item.item_id
    assert abs(added[0]["qty"] - 30.0) < 1e-6

    # Running again is idempotent: the new order is open WIP, so no further gap.
    res = reconcile_snapshot(db_session, run_id)
    assert res["production_added"] == []

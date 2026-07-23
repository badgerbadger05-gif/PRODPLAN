"""Tests for create_mrp_snapshot_from_period_plan — purchase allocation
with supplier-order netting.
"""

import datetime
from datetime import date
from types import SimpleNamespace

import pytest

from app.models import (
    DefaultSpecification,
    Item,
    MrpRequirement,
    PlannedOrder,
    PlannedPurchase,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionProduct,
    SpecComponent,
    Specification,
    SyncLink,
    SupplierOrder,
    SupplierOrderItem,
)
from app.services.period_plan_service import (
    _compute_legacy_period_plan_execution_journal,
    build_period_plan_execution_snapshot,
    create_mrp_snapshot_from_period_plan,
    get_period_plan_execution_journal,
)


@pytest.fixture(autouse=True)
def _accepted_planning_truth(monkeypatch):
    """Legacy journal scenarios explicitly run under an accepted truth."""
    monkeypatch.setattr(
        "app.services.planning_truth.require_accepted_truth",
        lambda db, consumer, **kwargs: SimpleNamespace(
            status="accepted", generation_id=1, cutoff=None, reason=None
        ),
    )


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


def test_execution_journal_is_explicitly_unavailable_without_accepted_truth(
    db_session, monkeypatch
):
    from app.services import planning_truth

    monkeypatch.setattr(
        planning_truth,
        "require_accepted_truth",
        lambda db, consumer, **kwargs: planning_truth.require_accepted(db),
    )
    item = _make_purchased_item(db_session, "TRUTH-GUARD")
    plan = _make_fixed_plan(db_session, item, date(2026, 7, 1), qty=7.0)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=7,
        net_required_qty=7,
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=0,
    ))
    db_session.commit()

    result = get_period_plan_execution_journal(db_session, plan.id, run_id=run.run_id)

    assert result["truth_status"] == "uninitialized"
    assert result["summary"]["execution_pct"] is None
    assert result["summary"]["execution_completed_qty"] is None
    assert result["rows"][0]["completed_qty"] is None
    assert result["rows"][0]["coverage_pct"] is None


def test_execution_journal_repeated_get_reads_snapshot_without_computation_or_writes(
    db_session, monkeypatch
):
    from app.services import period_plan_service, planning_truth

    item = _make_purchased_item(db_session, "SNAPSHOT-READ")
    plan = _make_fixed_plan(db_session, item, date(2026, 7, 1), qty=3.0)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db_session.add(run)
    db_session.commit()
    payload = {
        "plan": {"id": plan.id},
        "run_id": run.run_id,
        "truth_status": "accepted",
        "ledger_generation": 7,
        "cutoff": "2026-07-23T12:00:00",
        "rows": [{"req_id": 11, "completed_qty": 2.0}],
        "summary": {"execution_pct": 66.7},
    }
    monkeypatch.setattr(
        planning_truth,
        "get_latest_read_snapshot",
        lambda *args, **kwargs: SimpleNamespace(payload=payload),
    )
    monkeypatch.setattr(
        period_plan_service,
        "_compute_legacy_period_plan_execution_journal",
        lambda *args, **kwargs: pytest.fail("GET must not compute execution"),
    )

    before_new = set(db_session.new)
    first = get_period_plan_execution_journal(db_session, plan.id, run_id=run.run_id)
    second = get_period_plan_execution_journal(db_session, plan.id, run_id=run.run_id)

    assert first == payload
    assert second == payload
    assert set(db_session.new) == before_new
    assert not db_session.dirty


def test_execution_journal_missing_current_snapshot_is_unavailable(
    db_session, monkeypatch
):
    from app.services import planning_truth

    item = _make_purchased_item(db_session, "SNAPSHOT-MISSING")
    plan = _make_fixed_plan(db_session, item, date(2026, 7, 1), qty=4.0)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db_session.add(run)
    db_session.commit()
    accepted = SimpleNamespace(
        status="accepted",
        generation_id=9,
        cutoff=datetime.datetime(2026, 7, 23, 12, 0),
        reason=None,
    )
    monkeypatch.setattr(
        planning_truth, "get_latest_read_snapshot", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(planning_truth, "get_truth_state", lambda db: accepted)

    result = get_period_plan_execution_journal(db_session, plan.id, run_id=run.run_id)

    assert result["truth_status"] == "unavailable"
    assert result["ledger_generation"] == 9
    assert result["summary"]["execution_pct"] is None
    assert "snapshot is missing" in result["truth_reason"]


def test_legacy_nonzero_aggregates_cannot_publish_execution_snapshot(db_session):
    from app import models

    item = _make_purchased_item(db_session, "LEGACY-NOT-TRUTH")
    plan = _make_fixed_plan(db_session, item, date(2026, 7, 1), qty=5.0)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db_session.add(run)
    db_session.flush()
    order = ProductionOrder(
        order_number="LEGACY-AGGREGATE",
        order_date=datetime.datetime(2026, 7, 1),
        order_ref1c="legacy-aggregate-ref",
        source="1c",
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=999,
        produced_qty=999,
        remaining_qty=0,
    ))
    db_session.commit()

    with pytest.raises(NotImplementedError, match="Item Ledger allocation builder"):
        build_period_plan_execution_snapshot(db_session, plan.id, run_id=run.run_id)

    assert db_session.query(models.PlanningReadSnapshot).count() == 0


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
    state_name: str = "В пути",
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

def test_execution_journal_marks_production_order_opened_in_1c(db_session):
    bucket = date(2026, 6, 2)
    item = Item(
        item_code="MAKE-1C",
        item_name="Производимая деталь",
        item_article="MAKE-1C",
        unit="шт",
        stock_qty=0,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    plan = _make_fixed_plan(db_session, item, bucket, qty=12.0)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        started_at=datetime.datetime(2026, 5, 26, 5, 25),
        finished_at=datetime.datetime(2026, 5, 26, 5, 25),
    )
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=12,
        net_required_qty=12,
        covered_qty=12,
        remaining_qty=0,
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    order = ProductionOrder(
        order_number="MRP-R-OPEN",
        order_date=datetime.datetime(2026, 5, 27),
        order_ref1c="order-ref-opened",
        is_posted=True,
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=12,
        produced_qty=0,
        remaining_qty=12,
        source_mrp_requirement_id=req.id,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, status="shortage"))
    db_session.commit()

    journal = _compute_legacy_period_plan_execution_journal(db_session, plan.id, run_id=run.run_id)

    row = journal["rows"][0]
    assert row["ordered_qty"] == 12
    assert row["completed_qty"] == 0
    assert row["status"] == "ordered"
    work_item = row["work_items"][0]
    assert work_item["type"] == "production_order"
    assert work_item["product_id"] == product.product_id
    assert work_item["order_ref1c"] == "order-ref-opened"
    assert work_item["one_c_opened"] is True


def test_root_production_plan_is_not_netted_by_finished_goods_stock_or_wip(db_session):
    """A fixed release plan must create the full top-level production task."""
    bucket = date(2026, 9, 4)
    finished_good = Item(
        item_code="FG-PLAN-FULL",
        item_name="Готовая техника по плану",
        item_article="FG-PLAN-FULL",
        unit="шт",
        stock_qty=20.0,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(finished_good)
    db_session.flush()
    plan = _make_fixed_plan(db_session, finished_good, bucket, qty=75.0)

    # An open order from an earlier period must not reduce this period's
    # approved production programme either.
    old_order = ProductionOrder(
        order_number="OLD-FG-ORDER",
        order_date=datetime.datetime(2026, 8, 15),
        is_posted=True,
        deletion_mark=False,
        source="1c",
    )
    db_session.add(old_order)
    db_session.flush()
    db_session.add(
        ProductionProduct(
            order_id=old_order.order_id,
            item_id=finished_good.item_id,
            line_number=1,
            quantity=10.0,
            produced_qty=0.0,
            remaining_qty=10.0,
        )
    )
    db_session.flush()

    result = create_mrp_snapshot_from_period_plan(db_session, plan.id)

    req = db_session.query(MrpRequirement).filter_by(
        run_id=result["run_id"], item_id=finished_good.item_id,
    ).one()
    proposal = db_session.query(PlannedOrder).filter_by(
        run_id=result["run_id"], item_id=finished_good.item_id,
    ).one()
    assert float(req.total_required_qty) == pytest.approx(75.0)
    assert float(req.net_required_qty) == pytest.approx(75.0)
    assert float(proposal.qty) == pytest.approx(75.0)


def test_execution_journal_counts_direct_completed_1c_order_by_item(db_session):
    bucket = date(2026, 6, 19)
    item = Item(
        item_code="MAKE-DIRECT-1C",
        item_name="Прямой выпуск из 1С",
        item_article="MAKE-DIRECT-1C",
        unit="шт",
        stock_qty=0,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    plan = _make_fixed_plan(
        db_session,
        item,
        bucket,
        qty=20.0,
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
    )
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        started_at=datetime.datetime(2026, 6, 2, 5, 25),
        finished_at=datetime.datetime(2026, 6, 2, 5, 25),
    )
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=20,
        net_required_qty=20,
        covered_qty=0,
        remaining_qty=20,
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    order = ProductionOrder(
        order_number="1C-DIRECT-DONE",
        order_date=datetime.datetime(2026, 6, 19, 12, 0),
        order_ref1c="direct-1c-done-ref",
        is_posted=True,
        deletion_mark=False,
        source="1c",
        order_state_key="ad28565a-991b-11eb-e39a-fa163e61326a",
        order_state_name="Завершен",
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=20,
        produced_qty=20,
        remaining_qty=0,
        source_mrp_requirement_id=None,
    )
    db_session.add(product)
    db_session.commit()

    row = _compute_legacy_period_plan_execution_journal(db_session, plan.id, run_id=run.run_id)["rows"][0]

    assert row["ordered_qty"] == 20
    assert row["completed_qty"] == 20
    assert row["remaining_qty"] == 0
    assert row["unassigned_qty"] == 0
    assert row["coverage_pct"] == 100
    assert row["status"] == "covered"
    assert row["work_items"][0]["order_number"] == "1C-DIRECT-DONE"


def test_execution_journal_does_not_allocate_direct_1c_output_to_past_plan(db_session):
    item = Item(
        item_code="MAKE-DIRECT-FIFO",
        item_name="Выпуск по планам FIFO",
        unit="шт",
        stock_qty=0,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    june_plan = _make_fixed_plan(
        db_session, item, date(2026, 6, 1), qty=20,
        period_from=date(2026, 6, 1), period_to=date(2026, 6, 30),
    )
    july_plan = _make_fixed_plan(
        db_session, item, date(2026, 7, 1), qty=20,
        period_from=date(2026, 7, 1), period_to=date(2026, 7, 31),
    )
    runs_and_reqs = []
    for plan in (june_plan, july_plan):
        run = PlanningRun(
            status="FIXED_SNAPSHOT",
            source_plan_id=plan.id,
            period_from=plan.period_from,
            period_to=plan.period_to,
            started_at=datetime.datetime(2026, 6, 1),
            finished_at=datetime.datetime(2026, 6, 1),
        )
        db_session.add(run)
        db_session.flush()
        req = MrpRequirement(
            run_id=run.run_id,
            item_id=item.item_id,
            total_required_qty=20,
            net_required_qty=20,
            covered_qty=0,
            remaining_qty=20,
            period_from=plan.period_from,
            period_to=plan.period_to,
            bom_level=0,
        )
        db_session.add(req)
        runs_and_reqs.append((run, req))
    db_session.flush()
    order = ProductionOrder(
        order_number="1C-DIRECT-FIFO",
        order_date=datetime.datetime(2026, 7, 10),
        order_ref1c="direct-fifo-ref",
        is_posted=True,
        deletion_mark=False,
        source="1c",
        order_state_key="ad28565a-991b-11eb-e39a-fa163e61326a",
        order_state_name="Завершен",
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=25,
        produced_qty=25,
        remaining_qty=0,
    ))
    db_session.commit()

    june_row = _compute_legacy_period_plan_execution_journal(
        db_session, june_plan.id, run_id=runs_and_reqs[0][0].run_id,
    )["rows"][0]
    july_row = _compute_legacy_period_plan_execution_journal(
        db_session, july_plan.id, run_id=runs_and_reqs[1][0].run_id,
    )["rows"][0]

    assert june_row["completed_qty"] == 0
    assert june_row["remaining_qty"] == 20
    assert july_row["completed_qty"] == 20
    assert july_row["remaining_qty"] == 0


def test_execution_journal_does_not_count_planned_task_as_ordered(db_session):
    bucket = date(2026, 6, 2)
    item = Item(
        item_code="MAKE-PLAN",
        item_name="Плановая производимая деталь",
        item_article="MAKE-PLAN",
        unit="шт",
        stock_qty=0,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    plan = _make_fixed_plan(db_session, item, bucket, qty=10.0)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        started_at=datetime.datetime(2026, 5, 26, 5, 25),
        finished_at=datetime.datetime(2026, 5, 26, 5, 25),
    )
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=10,
        net_required_qty=10,
        covered_qty=0,
        remaining_qty=10,
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    db_session.add(
        PlannedOrder(
            run_id=run.run_id,
            item_id=item.item_id,
            requested_qty=10,
            planned_qty=10,
            qty=10,
            need_date=bucket,
            start_date=bucket,
            finish_date=bucket,
            bucket_date=bucket,
            demand_ref=f"mrp_requirement:{req.id}",
            demand_date=bucket,
        )
    )
    db_session.commit()

    row = _compute_legacy_period_plan_execution_journal(db_session, plan.id, run_id=run.run_id)["rows"][0]

    assert row["ordered_qty"] == 0
    assert row["completed_qty"] == 0
    assert row["remaining_qty"] == 10
    assert row["status"] == "none"
    assert row["need_date"] == bucket.isoformat()
    assert row["work_items"][0]["type"] == "planned_order"
    assert row["work_items"][0]["qty"] == 10


def test_execution_journal_uses_existing_orders_as_progress_base_when_net_is_zero(db_session):
    bucket = date(2026, 6, 2)
    item = Item(
        item_code="MAKE-WIP",
        item_name="Деталь уже в заказах",
        item_article="MAKE-WIP",
        unit="шт",
        stock_qty=0,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    plan = _make_fixed_plan(db_session, item, bucket, qty=94.0)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        started_at=datetime.datetime(2026, 5, 26, 5, 25),
        finished_at=datetime.datetime(2026, 5, 26, 5, 25),
    )
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=94,
        net_required_qty=0,
        covered_qty=0,
        remaining_qty=0,
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=1,
    )
    db_session.add(req)
    db_session.flush()
    order = ProductionOrder(
        order_number="MRP-R-WIP",
        order_date=datetime.datetime(2026, 5, 27),
        order_ref1c="order-ref-wip",
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=65,
        produced_qty=0,
        remaining_qty=65,
        source_mrp_requirement_id=req.id,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, status="shortage"))
    db_session.commit()

    row = _compute_legacy_period_plan_execution_journal(db_session, plan.id, run_id=run.run_id)["rows"][0]

    assert row["net_qty"] == 0
    assert row["ordered_qty"] == 65
    assert row["completed_qty"] == 0
    assert row["remaining_qty"] == 65
    assert row["progress_base_qty"] == 65
    assert row["coverage_pct"] == 0


def test_execution_journal_ignores_cancelled_and_unopened_production_rows(db_session):
    bucket = date(2026, 6, 2)
    item = Item(
        item_code="MAKE-CANCELLED",
        item_name="Деталь с отмененными дублями",
        item_article="MAKE-CANCELLED",
        unit="шт",
        stock_qty=0,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    plan = _make_fixed_plan(db_session, item, bucket, qty=86.0)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        started_at=datetime.datetime(2026, 5, 26, 5, 25),
        finished_at=datetime.datetime(2026, 5, 26, 5, 25),
    )
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=86,
        net_required_qty=81,
        covered_qty=81,
        remaining_qty=0,
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=1,
    )
    db_session.add(req)
    db_session.flush()

    active_order = ProductionOrder(
        order_number="MRP-R-ACTIVE",
        order_date=datetime.datetime(2026, 5, 27),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    cancelled_order = ProductionOrder(
        order_number="MRP-R-CANCELLED",
        order_date=datetime.datetime(2026, 5, 27),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db_session.add_all([active_order, cancelled_order])
    db_session.flush()

    active_product = ProductionProduct(
        order_id=active_order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=81,
        produced_qty=0,
        remaining_qty=81,
        source_mrp_requirement_id=req.id,
    )
    cancelled_product = ProductionProduct(
        order_id=cancelled_order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=141,
        produced_qty=0,
        remaining_qty=0,
        source_mrp_requirement_id=req.id,
    )
    db_session.add_all([active_product, cancelled_product])
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=active_product.product_id, status="partial"))
    db_session.add(ProductionOrderLineState(product_id=cancelled_product.product_id, status="cancelled"))
    db_session.commit()

    row = _compute_legacy_period_plan_execution_journal(db_session, plan.id, run_id=run.run_id)["rows"][0]

    assert row["gross_qty"] == 86
    assert row["net_qty"] == 81
    assert row["ordered_qty"] == 0
    assert row["completed_qty"] == 0
    assert row["remaining_qty"] == 81
    assert row["unassigned_qty"] == 81
    assert row["coverage_pct"] == 0
    assert len(row["work_items"]) == 1
    assert row["work_items"][0]["product_id"] == active_product.product_id
    assert row["work_items"][0]["one_c_opened"] is False


def test_execution_journal_counts_supplier_order_accepted_to_stock_as_completed(db_session):
    bucket = date(2026, 6, 2)
    item = _make_purchased_item(db_session, "BUY-DONE")
    plan = _make_fixed_plan(db_session, item, bucket, qty=10.0)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        started_at=datetime.datetime(2026, 5, 26, 5, 25),
        finished_at=datetime.datetime(2026, 5, 26, 5, 25),
    )
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=10,
        net_required_qty=10,
        covered_qty=0,
        remaining_qty=10,
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    purchase = PlannedPurchase(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=10,
        planned_qty=10,
        qty=10,
        need_date=bucket,
        order_date=bucket,
        lead_time_days=0,
        bucket_date=bucket,
        source_mrp_requirement_id=req.id,
    )
    db_session.add(purchase)
    db_session.flush()
    supplier_order = SupplierOrder(
        order_number="ЗП-ACCEPTED",
        order_date=datetime.datetime(2026, 6, 1),
        order_ref1c="supplier-order-accepted-ref",
        is_posted=True,
        deletion_mark=False,
        order_state_name="Принят на склад",
    )
    db_session.add(supplier_order)
    db_session.add(
        SyncLink(
            source_doctype="planned_purchase",
            source_id=purchase.purchase_id,
            target_entity="Document_ЗаказПоставщику",
            target_ref_key="supplier-order-accepted-ref",
            target_number="ЗП-ACCEPTED",
            status="success",
        )
    )
    db_session.commit()

    row = _compute_legacy_period_plan_execution_journal(db_session, plan.id, run_id=run.run_id)["rows"][0]

    assert row["ordered_qty"] == 10
    assert row["completed_qty"] == 10
    assert row["remaining_qty"] == 0
    assert row["coverage_pct"] == 100
    work_item = row["work_items"][0]
    assert work_item["type"] == "planned_purchase"
    assert work_item["one_c_opened"] is True
    assert work_item["order_ref1c"] == "supplier-order-accepted-ref"
    assert work_item["order_state"] == "Принят на склад"


class TestPurchaseAllocationNoSupplierOrders:
    """When no supplier orders exist, full net demand becomes PlannedPurchase."""

    def test_reuses_existing_fixed_snapshot_for_plan_recalculation(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-RECALC", stock=0.0)
        plan = _make_fixed_plan(db_session, item, bucket, qty=30.0)

        first = create_mrp_snapshot_from_period_plan(db_session, plan.id)
        line = db_session.query(ProductionPlanLine).filter_by(plan_id=plan.id, item_id=item.item_id).one()
        line.qty = 45
        db_session.commit()

        second = create_mrp_snapshot_from_period_plan(db_session, plan.id)

        assert second["run_id"] == first["run_id"]
        assert db_session.query(PlanningRun).filter_by(source_plan_id=plan.id, status="FIXED_SNAPSHOT").count() == 1
        req = db_session.query(MrpRequirement).filter_by(run_id=first["run_id"], item_id=item.item_id).one()
        assert float(req.total_required_qty) == pytest.approx(45.0)
        assert float(req.net_required_qty) == pytest.approx(45.0)
        assert db_session.query(PlannedPurchase).filter_by(run_id=first["run_id"], item_id=item.item_id).count() == 1

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

    @pytest.mark.parametrize("state_name", ["новый заказ", "в закупку", "отменен", "завершен", "бухгалтерия"])
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

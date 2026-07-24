"""Tests for create_mrp_snapshot_from_period_plan — purchase allocation
with supplier-order netting.
"""

import datetime
from datetime import date
from types import SimpleNamespace

import pytest

from app import models
from app.models import (
    DefaultSpecification,
    Item,
    LedgerGeneration,
    MrpRequirement,
    MrpExecutionAllocation,
    PlannedOrder,
    PlannedPurchase,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionProduct,
    PhysicalImportBatch,
    PlanningTruthState,
    PlanningReadSnapshot,
    StockBin,
    StockLedgerEntry,
    StockLedgerSupplierReceiptProvenance,
    SpecComponent,
    Specification,
    SyncLink,
    SupplierOrder,
    SupplierOrderItem,
)
from app.services import period_plan_service
from app.services import obligation_refresh_orchestrator
from app.services.period_plan_service import (
    _compute_legacy_period_plan_execution_journal,
    _build_execution_snapshot_rows,
    build_period_plan_execution_snapshot,
    create_mrp_snapshot_from_period_plan,
    get_period_plan_execution_journal,
    list_period_plans,
)
@pytest.fixture(autouse=True)
def _accepted_planning_truth(db_session):
    """Planning calculations run against one explicit accepted Ledger."""
    cutoff = datetime.datetime(2026, 7, 23)
    batch = PhysicalImportBatch(
        batch_key="period-plan-ledger",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    generation = LedgerGeneration(
        generation_key="period-plan-ledger",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={"replay_from": "2026-06-01T00:00:00"},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
            "planning_snapshots": True,
        },
        physical_import_batch=batch,
        algorithm_version="test",
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    db_session.flush()
    db_session.info["period_plan_ledger_generation_id"] = int(generation.id)


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
    if stock:
        db.add(StockBin(
            ledger_generation_id=int(db.info["period_plan_ledger_generation_id"]),
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c="",
            on_hand=stock,
        ))
        db.flush()
    return item


def _publish_plan(db, plan: ProductionPlanHeader):
    """Run the public atomic Ledger workflow with a deterministic retry key."""
    return create_mrp_snapshot_from_period_plan(
        db,
        plan.id,
        generation_key=f"period-plan-{plan.id}",
    )


def _planned_purchase(db, result):
    return db.query(PlannedPurchase).filter_by(run_id=result["run_id"]).one()


def test_publish_plan_prefers_reservation_lineage_refresh_over_add(db_session, monkeypatch):
    item = _make_purchased_item(db_session, "HIST-SLICE")
    plan = _make_fixed_plan(db_session, item, date(2026, 10, 1), qty=10.0)
    accepted = db_session.get(PlanningTruthState, 1).current_generation_id
    accepted_generation = db_session.get(LedgerGeneration, int(accepted))

    parent = models.PlanningRun(
        status="FIXED_SNAPSHOT", ledger_generation_id=None, source_plan_id=plan.id,
        period_from=plan.period_from, period_to=plan.period_to,
        started_at=accepted_generation.cutoff, fixed_at=accepted_generation.cutoff, finished_at=accepted_generation.cutoff,
    )
    db_session.add(parent)
    db_session.flush()
    req = models.MrpRequirement(
        run_id=parent.run_id, item_id=item.item_id, total_required_qty=0,
        net_required_qty=0, period_from=plan.period_from, period_to=plan.period_to,
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    db_session.add(models.ReservationEntry(
        ledger_generation_id=int(accepted), item_id=item.item_id, run_id=parent.run_id,
        freeze_version=0, requirement_id=req.id, priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
    ))
    db_session.flush()

    target = models.LedgerGeneration(
        generation_key="period-plan-refresh-target", status="building",
        cutoff=accepted_generation.cutoff, source_watermarks={
            "generation_kind": "obligation_refresh", "parent_generation_id": int(accepted)
        }, capabilities={}, physical_import_batch=accepted_generation.physical_import_batch,
        algorithm_version="tests/1",
    )
    db_session.add(target)
    db_session.flush()
    db_session.add(
        models.PlanningRun(
            status="FIXED_SNAPSHOT", source_plan_id=plan.id, ledger_generation_id=target.id,
            prior_run_id=parent.run_id, period_from=plan.period_from, period_to=plan.period_to,
            started_at=accepted_generation.cutoff, fixed_at=accepted_generation.cutoff,
            finished_at=accepted_generation.cutoff,
        )
    )
    db_session.flush()
    candidate_run_id = db_session.query(models.PlanningRun.run_id).filter(
        models.PlanningRun.source_plan_id == int(plan.id),
        models.PlanningRun.ledger_generation_id == target.id,
    ).scalar()

    observed = {}

    def fake_run_obligation_refresh(db, **kwargs):
        observed["add_plan_ids"] = tuple(kwargs["add_plan_ids"])
        return SimpleNamespace(
            candidate_run_ids=[int(candidate_run_id)], target_generation_id=int(target.id), published=False,
        )

    monkeypatch.setattr(obligation_refresh_orchestrator, "run_obligation_refresh", fake_run_obligation_refresh)
    result = _publish_plan(db_session, plan)

    assert observed["add_plan_ids"] == ()
    assert result["run_id"] == int(candidate_run_id)
    assert db_session.get(models.PlanningRun, result["run_id"]).prior_run_id == parent.run_id


def test_execution_journal_is_explicitly_unavailable_without_accepted_truth(
    db_session, monkeypatch
):
    from app.services import planning_truth

    monkeypatch.setattr(
        planning_truth,
        "require_accepted_truth",
        lambda db, consumer, **kwargs: planning_truth.require_accepted(db),
    )
    db_session.query(PlanningTruthState).delete()
    db_session.flush()
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

    result = build_period_plan_execution_snapshot(db_session, plan.id, run_id=run.run_id)

    current = db_session.query(PlanningTruthState.current_generation_id).scalar()
    assert result["run_id"] == int(run.run_id)
    assert result["ledger_generation"] == int(current)
    assert result["summary"]["total_items"] == 0
    assert result["summary"]["execution_by_flow"] == {}
    assert result["rows"] == []
    assert db_session.query(PlanningReadSnapshot).count() == 0


def test_execution_snapshot_persists_canonical_accepted_lineage(db_session):
    item = _make_purchased_item(db_session, "SNAPSHOT-LINEAGE")
    plan = _make_fixed_plan(db_session, item, date(2026, 7, 1), qty=5.0)
    generation_id = int(db_session.query(PlanningTruthState.current_generation_id).scalar())
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        ledger_generation_id=generation_id,
    )
    db_session.add(run)
    db_session.flush()

    payload = build_period_plan_execution_snapshot(
        db_session,
        plan.id,
        run_id=run.run_id,
        generation_id=generation_id,
        persist=True,
    )

    snapshot = db_session.query(PlanningReadSnapshot).one()
    generation = db_session.get(LedgerGeneration, generation_id)
    assert snapshot.consumer == "period_plan_execution"
    assert snapshot.snapshot_key == f"plan={plan.id};run={run.run_id}"
    assert snapshot.ledger_generation_id == generation_id
    assert snapshot.cutoff == generation.cutoff
    assert snapshot.truth_status == "accepted"
    assert snapshot.payload == payload


def test_period_plan_list_reads_progress_from_current_immutable_snapshot(db_session):
    item = _make_purchased_item(db_session, "LIST-PROGRESS")
    plan = _make_fixed_plan(db_session, item, date(2026, 7, 1), qty=5.0)
    generation_id = int(
        db_session.query(PlanningTruthState.current_generation_id).scalar()
    )
    generation = db_session.get(LedgerGeneration, generation_id)
    db_session.add(PlanningReadSnapshot(
        consumer="period_plan_execution",
        snapshot_key=f"plan={plan.id};run=77",
        ledger_generation_id=generation_id,
        cutoff=generation.cutoff,
        truth_status="accepted",
        payload={
            "plan": {"id": plan.id},
            "truth_status": "accepted",
            "summary": {
                "execution_pct": 62.5,
                "execution_completed_qty": 5,
                "execution_base_qty": 8,
            },
        },
        published_at=datetime.datetime(2026, 7, 24),
    ))
    db_session.commit()

    result = list_period_plans(db_session)

    row = next(row for row in result["rows"] if row["id"] == plan.id)
    assert row["execution_pct"] == 62.5
    assert row["execution_completed_qty"] == 5
    assert row["execution_base_qty"] == 8
    assert row["execution_status"] == "accepted"
    assert row["execution_reason"] is None
    assert row["execution_generation_id"] == generation_id


@pytest.mark.parametrize(
    ("match_status", "expected_qty", "expected_available"),
    [
        ("exact", 4.0, True),
        ("unmatched", 4.0, True),
    ],
)
def test_purchase_execution_uses_only_exact_supplier_receipt_coverage(
    db_session, match_status, expected_qty, expected_available
):
    item = _make_purchased_item(db_session, f"BUY-RECEIPT-{match_status}")
    generation_id = int(
        db_session.query(PlanningTruthState.current_generation_id).scalar()
    )
    batch = db_session.query(PhysicalImportBatch).one()
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
    )
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=10,
        net_required_qty=10,
        period_from=run.period_from,
        period_to=run.period_to,
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    sle = StockLedgerEntry(
        ingest_batch_id=batch.id,
        source_content_hash=f"receipt-{match_status}",
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="main",
        qty=4,
        qty_after=4,
        posting_at=datetime.datetime(2026, 7, 10),
        record_type="Receipt",
        movement_kind="receipt",
        recorder_type="Document_ПриобретениеТоваровУслуг",
        recorder_ref=f"receipt-{match_status}",
        line_no="1",
        ingest_source="pull",
    )
    db_session.add(sle)
    db_session.flush()
    db_session.add(StockLedgerSupplierReceiptProvenance(
        ledger_generation_id=generation_id,
        stock_ledger_entry_id=sle.id,
        receipt_doc_type=sle.recorder_type,
        receipt_doc_ref=sle.recorder_ref,
        receipt_doc_line_no=sle.line_no,
        supplier_order_ref="order-1" if match_status == "exact" else None,
        supplier_order_line_no="1" if match_status == "exact" else None,
        operation_kind="supplier_receipt",
        evidence_hash="evidence",
        evidence_payload={},
        match_rule="typed_order_line",
        match_status=match_status,
        ambiguity_count=0,
        reason=None if match_status == "exact" else "no exact typed supplier order line",
    ))
    db_session.add(MrpExecutionAllocation(
        ledger_generation_id=generation_id,
        cycle_id="supplier-test",
        requirement_id=req.id,
        fact_type="supplier_receipt",
        allocation_kind="execution",
        fact_ref=sle.recorder_ref,
        fact_line_ref=f"1#sle:{sle.id}",
        allocated_qty=4,
        stock_ledger_entry_id=sle.id,
        origin_requirement_id=req.id,
    ))
    db_session.flush()

    rows, _meta = _build_execution_snapshot_rows(
        db_session,
        run,
        requirement_ids=[req.id],
        items_by_requirement={
            req.id: {
                "item_id": item.item_id,
                "item_code": item.item_code,
                "item_article": item.item_article,
                "item_name": item.item_name,
                "gross_required_qty": 10,
                "net_required_qty": 10,
                "bom_level": 0,
            }
        },
        generation_id=generation_id,
        root_item_ids_by_item={item.item_id: []},
    )

    assert rows[0]["completed_qty"] == expected_qty
    assert rows[0]["execution_available"] is expected_available
    assert rows[0]["execution_source"] == "supplier_receipt_coverage"


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

    result = _publish_plan(db_session, plan)

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

    def test_exact_retry_keeps_published_snapshot_immutable(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-RECALC", stock=0.0)
        plan = _make_fixed_plan(db_session, item, bucket, qty=30.0)

        first = _publish_plan(db_session, plan)
        line = db_session.query(ProductionPlanLine).filter_by(plan_id=plan.id, item_id=item.item_id).one()
        line.qty = 45
        db_session.commit()

        second = _publish_plan(db_session, plan)

        assert second["run_id"] == first["run_id"]
        assert db_session.query(PlanningRun).filter_by(source_plan_id=plan.id, status="FIXED_SNAPSHOT").count() == 1
        req = db_session.query(MrpRequirement).filter_by(run_id=first["run_id"], item_id=item.item_id).one()
        assert float(req.total_required_qty) == pytest.approx(30.0)
        assert float(req.net_required_qty) == pytest.approx(30.0)
        assert db_session.query(PlannedPurchase).filter_by(run_id=first["run_id"], item_id=item.item_id).count() == 1

    def test_creates_planned_purchase_for_full_net_qty(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-001", stock=0.0)
        plan = _make_fixed_plan(db_session, item, bucket, qty=50.0)

        result = _publish_plan(db_session, plan)

        pp = _planned_purchase(db_session, result)
        assert float(pp.requested_qty) == 50.0
        assert float(pp.planned_qty) == 50.0
        assert float(pp.qty) == 50.0

    def test_source_mrp_requirement_id_is_linked(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-002")
        plan = _make_fixed_plan(db_session, item, bucket, qty=30.0)

        result = _publish_plan(db_session, plan)

        pp = db_session.query(PlannedPurchase).filter_by(run_id=result["run_id"]).one()
        req = db_session.query(MrpRequirement).filter_by(run_id=result["run_id"]).one()
        assert pp.source_mrp_requirement_id == req.id

    def test_parent_generation_stock_is_not_reused_as_candidate_stock(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-003", stock=15.0)
        plan = _make_fixed_plan(db_session, item, bucket, qty=50.0)

        result = _publish_plan(db_session, plan)

        pp = _planned_purchase(db_session, result)
        # The fixture's StockBin belongs to the accepted parent.  The new
        # candidate may consume only rows stamped with its own generation.
        assert float(pp.planned_qty) == pytest.approx(50.0)
        assert float(pp.requested_qty) == pytest.approx(50.0)


class TestLegacySupplierFactsAreNotSupply:
    """Mutable supplier-order counters never reduce Ledger obligations."""

    def test_no_planned_purchase_created(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-FULL")
        plan = _make_fixed_plan(db_session, item, bucket, qty=20.0)
        _make_supplier_order(db_session, item, remaining_qty=20.0, delivery_date=bucket)

        result = _publish_plan(db_session, plan)
        assert float(_planned_purchase(db_session, result).planned_qty) == pytest.approx(20.0)

    def test_supplier_surplus_does_not_create_negative_purchase(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-SURPLUS")
        plan = _make_fixed_plan(db_session, item, bucket, qty=10.0)
        _make_supplier_order(db_session, item, remaining_qty=50.0, delivery_date=bucket)

        result = _publish_plan(db_session, plan)
        assert float(_planned_purchase(db_session, result).planned_qty) == pytest.approx(10.0)

    def test_supplier_early_delivery_covers_demand(self, db_session):
        """Supplier arriving before bucket date still covers that bucket."""
        bucket = date(2026, 6, 9)
        item = _make_purchased_item(db_session, "BUY-EARLY")
        plan = _make_fixed_plan(db_session, item, bucket, qty=15.0)
        _make_supplier_order(db_session, item, remaining_qty=15.0, delivery_date=date(2026, 6, 2))

        result = _publish_plan(db_session, plan)
        assert float(_planned_purchase(db_session, result).planned_qty) == pytest.approx(15.0)

    def test_supplier_late_delivery_does_not_cover(self, db_session):
        """Supplier arriving after bucket date cannot cover that bucket's demand."""
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-LATE")
        plan = _make_fixed_plan(
            db_session, item, bucket, qty=20.0,
            period_from=date(2026, 6, 2), period_to=date(2026, 6, 30),
        )
        _make_supplier_order(db_session, item, remaining_qty=20.0, delivery_date=date(2026, 6, 16))

        result = _publish_plan(db_session, plan)
        assert float(_planned_purchase(db_session, result).planned_qty) == pytest.approx(20.0)


class TestLegacySupplierPartialFactsAreNotSupply:

    def test_planned_purchase_qty_is_remainder(self, db_session):
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-PART")
        plan = _make_fixed_plan(db_session, item, bucket, qty=30.0)
        _make_supplier_order(db_session, item, remaining_qty=10.0, delivery_date=bucket)

        result = _publish_plan(db_session, plan)
        assert float(_planned_purchase(db_session, result).planned_qty) == pytest.approx(30.0)

    def test_coverage_qty_reflects_full_demand(self, db_session):
        """MrpRequirement.covered_qty must equal total net demand (supplier + planned purchase)."""
        bucket = date(2026, 6, 2)
        item = _make_purchased_item(db_session, "BUY-COV")
        plan = _make_fixed_plan(db_session, item, bucket, qty=40.0)
        _make_supplier_order(db_session, item, remaining_qty=15.0, delivery_date=bucket)

        result = _publish_plan(db_session, plan)
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

        result = _publish_plan(db_session, plan)

        pp = _planned_purchase(db_session, result)
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

        result = _publish_plan(db_session, plan)

        pp = _planned_purchase(db_session, result)
        assert float(pp.planned_qty) == pytest.approx(20.0)


class TestLedgerOnlyMultiBucketAllocation:

    def test_supplier_consumed_across_buckets(self, db_session):
        """Legacy supplier counters do not leak into either demand bucket."""
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

        _make_supplier_order(db_session, item, remaining_qty=25.0, delivery_date=week1)

        result = _publish_plan(db_session, plan)
        purchases = (
            db_session.query(PlannedPurchase)
            .filter_by(run_id=result["run_id"])
            .order_by(PlannedPurchase.need_date)
            .all()
        )
        assert [float(row.planned_qty) for row in purchases] == pytest.approx([20.0, 30.0])

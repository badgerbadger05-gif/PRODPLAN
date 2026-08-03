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
    AssemblyRate,
    Item,
    LedgerGeneration,
    MrpRequirement,
    PlannedOrder,
    PlannedPurchase,
    ProductionMaterialCustodyProjectionManifest,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionProduct,
    ProductionResource,
    PhysicalImportBatch,
    ClosedPlanSnapshot,
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
    _build_execution_snapshot_rows,
    build_period_plan_execution_snapshot,
    create_mrp_snapshot_from_period_plan,
    get_period_plan_matrix,
    get_period_plan_execution_journal,
    list_period_plans,
)


def test_period_plan_percent_adapter_uses_canonical_clamp_and_zero_base():
    assert period_plan_service._rounded_replenishment_pct(0, 0) is None
    assert period_plan_service._rounded_replenishment_pct(10, 5) == 50.0
    assert period_plan_service._rounded_replenishment_pct(10, 12) == 100.0


@pytest.fixture(autouse=True)
def _accepted_planning_truth(db_session):
    """Planning calculations run against one explicit accepted Ledger."""
    cutoff = datetime.datetime(2026, 7, 23)
    batch = PhysicalImportBatch(
        batch_key="period-plan-ledger",
        status="completed",
        cutoff=cutoff,
        source_watermarks={"opening_at": "2025-01-01T00:00:00+00:00"},
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
    db_session.add_all([
        generation,
        models.StockWarehouse(
            warehouse_ref1c="WH-PERIOD-PLAN",
            warehouse_name="Period planning contour",
            is_selected=True,
            is_finished_goods=False,
        ),
    ])
    db_session.flush()
    db_session.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    db_session.add(
        ProductionMaterialCustodyProjectionManifest(
            ledger_generation_id=int(generation.id),
            cutoff=generation.cutoff,
            status="complete",
            is_baseline=True,
            source_event_high_watermark_id=0,
            observed_at=generation.cutoff,
            built_at=generation.cutoff,
        )
    )
    resource = ProductionResource(
        resource_name="Period plan assembly",
        planning_range=30,
        capacity=100,
    )
    db_session.add(resource)
    db_session.flush()
    db_session.info["period_plan_ledger_generation_id"] = int(generation.id)
    db_session.info["assembly_resource_id"] = int(resource.resource_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_purchased_item(db, code: str, stock: float = 0.0) -> Item:
    item = Item(
        item_code=code,
        item_name=f"Закупаемая деталь {code}",
        item_article=code,
        unit="шт",
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
    assert result["rows"] == []
    assert result["total"] == 0


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
    before_new = set(db_session.new)
    first = get_period_plan_execution_journal(db_session, plan.id, run_id=run.run_id)
    second = get_period_plan_execution_journal(db_session, plan.id, run_id=run.run_id)

    assert first["truth_status"] == payload["truth_status"]
    assert first["total"] == 1
    assert first["limit"] == 100
    assert first["offset"] == 0
    assert first["rows"][0]["req_id"] == 11
    assert first["rows"][0]["status"] == "execution_unavailable"
    assert first["rows"][0]["status_label"] == "Исполнение недоступно"
    assert second == first
    assert set(db_session.new) == before_new
    assert not db_session.dirty


def test_execution_journal_filters_sorts_and_pages_on_backend(
    db_session, monkeypatch
):
    from app.services import planning_truth

    item = _make_purchased_item(db_session, "SERVER-QUERY")
    plan = _make_fixed_plan(db_session, item, date(2026, 7, 1), qty=3.0)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db_session.add(run)
    db_session.commit()
    rows = [
        {"req_id": 1, "item_id": 1, "bom_level": 0, "status": "net_zero", "remaining_qty": 0},
        {"req_id": 2, "item_id": 2, "bom_level": 1, "status": "none", "remaining_qty": 9},
        {"req_id": 3, "item_id": 3, "bom_level": 1, "status": "ordered", "remaining_qty": 5},
        {"req_id": 4, "item_id": 4, "bom_level": 2, "status": "partial", "remaining_qty": 2},
        {"req_id": 5, "item_id": 5, "bom_level": 2, "status": "covered", "remaining_qty": 0},
    ]
    monkeypatch.setattr(
        planning_truth,
        "get_latest_read_snapshot",
        lambda *args, **kwargs: SimpleNamespace(payload={
            "plan": {"id": plan.id},
            "run_id": run.run_id,
            "truth_status": "accepted",
            "ledger_generation": 7,
            "cutoff": "2026-07-23T12:00:00",
            "rows": rows,
            "summary": {},
        }),
    )

    result = get_period_plan_execution_journal(
        db_session,
        plan.id,
        run_id=run.run_id,
        status="incomplete",
        include_net_zero=False,
        sort_by="remaining_qty",
        sort_dir="desc",
        limit=1,
        offset=1,
    )

    assert result["total"] == 3
    assert result["limit"] == 1
    assert result["offset"] == 1
    assert [row["req_id"] for row in result["rows"]] == [3]
    assert result["rows"][0]["status_label"] == "Оформлено"
    assert result["summary"]["total_items"] == 3
    assert result["facets"]["bom_levels"] == [0, 1, 2]


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


def test_execution_journal_reads_closed_plan_snapshot_without_current_truth(db_session):
    item = _make_purchased_item(db_session, "CLOSED-HISTORY")
    generation_id = int(db_session.info["period_plan_ledger_generation_id"])
    plan = _make_fixed_plan(db_session, item, date(2026, 7, 1), qty=1.0)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        ledger_generation_id=generation_id,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=1,
        net_required_qty=1,
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=0,
    ))
    db_session.flush()

    payload = build_period_plan_execution_snapshot(
        db_session,
        plan.id,
        run_id=run.run_id,
        persist=True,
    )
    generation = db_session.get(LedgerGeneration, generation_id)
    assert generation is not None

    db_session.add(ClosedPlanSnapshot(
        plan_id=plan.id,
        run_id=run.run_id,
        ledger_generation_id=generation_id,
        cutoff=generation.cutoff,
        payload=payload,
        closed_at=datetime.datetime(2026, 7, 24, 12, 0),
    ))
    run.status = "CLOSED"
    db_session.commit()

    # Verify reads are served from closed history even when truth is unavailable.
    db_session.query(PlanningTruthState).delete()
    db_session.commit()

    explicit = get_period_plan_execution_journal(
        db_session, plan.id, run_id=run.run_id,
    )
    neutral = get_period_plan_execution_journal(db_session, plan.id)

    assert explicit["run_id"] == int(run.run_id)
    assert explicit["summary"]["execution_pct"] == payload["summary"]["execution_pct"]
    assert explicit["summary"]["execution_completed_qty"] == payload["summary"]["execution_completed_qty"]
    assert explicit["ledger_generation"] == payload["ledger_generation"]
    assert neutral["run_id"] == int(run.run_id)


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


@pytest.mark.parametrize("match_status", ["exact", "unmatched"])
def test_purchase_execution_reads_buy_reservation_fold_not_allocation_projection(
    db_session, match_status
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
    reservation = models.ReservationEntry(
        ledger_generation_id=generation_id,
        item_id=item.item_id,
        run_id=run.run_id,
        freeze_version=0,
        requirement_id=req.id,
        priority_period_from=run.period_from,
        priority_period_to=run.period_to,
            realization_mode="buy",
            reserved_qty=10,
            replenishment_required_qty=10,
            replenishment_received_qty=4,
            realized_qty=4,
    )
    db_session.add(reservation)
    db_session.flush()
    db_session.add(models.ReservationEvent(
        ledger_generation_id=generation_id,
        reservation_id=reservation.id,
        item_id=item.item_id,
        event_kind="realize",
        realized_delta=4,
        sle_id=sle.id,
        fact_ref=sle.recorder_ref,
        fact_line_ref=sle.line_no,
        match_rule="fifo",
        cycle_id="supplier-test",
        idempotency_key=f"realize:{reservation.id}:{sle.id}",
    ))
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

    assert rows[0]["completed_qty"] == 4.0
    assert rows[0]["execution_available"] is True
    assert rows[0]["execution_source"] == "supplier_receipt_coverage"


def _make_fixed_plan(
    db,
    item: Item,
    bucket_date: date,
    qty: float,
    period_from: date | None = None,
    period_to: date | None = None,
) -> ProductionPlanHeader:
    if (
        db.query(AssemblyRate)
        .filter(AssemblyRate.item_id == int(item.item_id))
        .count()
        == 0
    ):
        db.add(
            AssemblyRate(
                resource_id=int(db.info["assembly_resource_id"]),
                item_id=int(item.item_id),
                qty_per_capacity=1,
            )
        )
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
        fixed_at=datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc),
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

def test_known_rework_freezes_requirement_without_creating_an_executor(db_session):
    item = _make_purchased_item(db_session, "KNOWN-REWORK")
    item.replenishment_method = "Переработка"
    plan = _make_fixed_plan(db_session, item, date(2026, 7, 28), qty=7.0)

    result = _publish_plan(db_session, plan)

    run_id = result["run_id"]
    requirement = db_session.query(MrpRequirement).filter_by(run_id=run_id).one()
    assert requirement.net_required_qty == 7
    assert db_session.query(PlannedOrder).filter_by(run_id=run_id).count() == 0
    assert db_session.query(PlannedPurchase).filter_by(run_id=run_id).count() == 0
    assert db_session.query(models.PlannedRework).filter_by(run_id=run_id).count() == 0


def test_unknown_replenishment_method_still_blocks_publication(db_session):
    item = _make_purchased_item(db_session, "UNKNOWN-ROUTE")
    item.replenishment_method = "неизвестный маршрут"
    plan = _make_fixed_plan(db_session, item, date(2026, 7, 28), qty=7.0)

    with pytest.raises(ValueError, match="Unsupported replenishment flow"):
        _publish_plan(db_session, plan)

def test_root_production_plan_is_not_netted_by_finished_goods_stock_or_wip(db_session):
    """A fixed release plan must create the full top-level production task."""
    bucket = date(2026, 9, 4)
    finished_good = Item(
        item_code="FG-PLAN-FULL",
        item_name="Готовая техника по плану",
        item_article="FG-PLAN-FULL",
        unit="шт",
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


def test_get_period_plan_matrix_returns_server_totals(db_session):
    item = _make_purchased_item(db_session, "MATRIX-SUMS")
    plan = _make_fixed_plan(
        db_session,
        item,
        date(2026, 8, 7),
        qty=4.0,
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
    )
    db_session.add(ProductionPlanLine(
        plan_id=plan.id,
        item_id=item.item_id,
        bucket_date=date(2026, 8, 14),
        qty=6.0,
    ))
    db_session.commit()

    result = get_period_plan_matrix(db_session, plan.id)
    row = result["rows"][0]

    assert result["bucket_totals"] == {
        "2026-08-07": 4.0,
        "2026-08-14": 6.0,
        "2026-08-21": 0.0,
        "2026-08-28": 0.0,
    }
    assert row["total_qty"] == pytest.approx(10.0)
    assert result["grand_total"] == pytest.approx(10.0)
    assert result["total_qty"] == pytest.approx(10.0)
    assert result["total"] == 1


def test_get_period_plan_matrix_hides_forecasts_for_fixed_plans(db_session):
    item = _make_purchased_item(db_session, "MATRIX-FORECAST")
    plan = _make_fixed_plan(
        db_session,
        item,
        date(2026, 8, 7),
        qty=4.0,
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
    )
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(PlannedOrder(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=4.0,
        planned_qty=4.0,
        qty=4.0,
        need_date=date(2026, 8, 7),
        bucket_date=date(2026, 8, 7),
        start_date=date(2026, 8, 7),
        finish_date=date(2026, 8, 10),
    ))
    db_session.commit()

    result = get_period_plan_matrix(db_session, plan.id)

    assert result["rows"][0]["bucket_forecasts"] == {}

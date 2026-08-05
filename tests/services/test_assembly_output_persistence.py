from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.assembly_output_persistence import materialize_assembly_output_allocations
from app.services.item_ledger.assembly_queue_snapshot import build_assembly_queue_snapshot
from app.services.item_ledger.drum_schedule_persistence import materialize_drum_schedule


def _building_generation(db, *, key: str, cutoff):
    physical = models.PhysicalImportBatch(
        batch_key=f"assembly-output-physical-{key}",
        status="completed",
        source_watermarks={"source": key},
        cutoff=cutoff,
        completed_at=cutoff,
    )
    db.add(physical)
    db.flush()
    generation = models.LedgerGeneration(
        generation_key=f"assembly-output-generation-{key}",
        status="building",
        cutoff=cutoff,
        capabilities={},
        source_watermarks={},
        physical_import_batch_id=int(physical.id),
        algorithm_version="tests",
    )
    db.add(generation)
    db.flush()
    return generation


def _item(db, code: str):
    row = models.Item(item_code=code, item_name=code)
    db.add(row)
    db.flush()
    return row


def _sline(
    db,
    *,
    batch,
    item,
    qty,
    at,
    recorder="REC",
    movement_kind="assembly_in",
    content_hash="c" * 64,
):
    row = models.StockLedgerEntry(
        ingest_batch_id=int(batch.id),
        source_content_hash=content_hash,
        item_id=int(item.item_id),
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="",
        qty=Decimal(str(qty)),
        qty_after=Decimal(str(qty)),
        posting_at=at,
        record_type="Receipt",
        movement_kind=movement_kind,
        recorder_type="Production",
        recorder_ref=recorder,
        line_no="1",
        ingest_source="pull",
        active=True,
    )
    db.add(row)
    db.flush()
    return row


def _plan_with_run(
    db,
    *,
    generation,
    plan_status,
    run_status="FIXED_SNAPSHOT",
    item,
    qty,
    period_from: date | None = None,
    period_to: date | None = None,
    fixed_at: datetime | None = None,
):
    plan_period_from = period_from if period_from is not None else date(2026, 7, 1)
    plan_period_to = period_to if period_to is not None else date(2026, 12, 31)
    fixation = fixed_at or datetime(2026, 6, 30, tzinfo=timezone.utc)

    plan = models.ProductionPlanHeader(
        name=f"plan-{item.item_code}-{plan_period_from.isoformat()}",
        period_from=plan_period_from,
        period_to=plan_period_to,
        status=plan_status,
        fixed_at=fixation if plan_status == "fixed" else None,
    )
    db.add(plan)
    db.flush()

    run = models.PlanningRun(
        status=run_status,
        config_snapshot={},
        ledger_generation_id=int(generation.id),
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
        source_plan_id=int(plan.id),
        period_from=plan_period_from,
        period_to=plan_period_to,
        fixed_at=fixation if run_status == "FIXED_SNAPSHOT" else None,
    )
    db.add(run)
    db.flush()

    line = models.ProductionPlanLine(
        plan_id=int(plan.id),
        item_id=int(item.item_id),
        bucket_date=plan_period_from,
        qty=Decimal(str(qty)),
    )
    db.add(line)
    db.flush()
    return plan, run, line


def test_visible_only_positive_assembly_in(db_session):
    cutoff = datetime(2026, 7, 31, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="visible", cutoff=cutoff)
    item = _item(db_session, "ASM-VIS")

    _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="10",
        at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        movement_kind="assembly_in",
        recorder="visible-1",
    )
    _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="-5",
        at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        movement_kind="assembly_in",
        recorder="visible-2",
    )
    _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="7",
        at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        movement_kind="assembly_in",
        recorder="visible-3",
    )
    _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="11",
        at=datetime(2026, 7, 12, tzinfo=timezone.utc),
        movement_kind="receipt",
        recorder="visible-4",
    )

    result = materialize_assembly_output_allocations(db_session, generation.id)

    assert result["facts"] == 1
    assert result["allocations"] == 0
    assert Decimal(result["fact_qty"]) == Decimal("10")
    assert Decimal(result["surplus_total"]) == Decimal("10")
    assert db_session.query(models.AssemblyOutputFactDecision).filter_by(
        ledger_generation_id=generation.id,
    ).count() == 1


def test_fifo_across_two_live_fixed_plans(db_session):
    cutoff = datetime(2026, 7, 31, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="fifo", cutoff=cutoff)
    item = _item(db_session, "ASM-FIFO")

    _, _, line_one = _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        item=item,
        qty=Decimal("2"),
        period_from=date(2026, 7, 1),
    )
    _, _, line_two = _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        item=item,
        qty=Decimal("8"),
        period_from=date(2026, 8, 1),
    )

    _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="7",
        at=datetime(2026, 7, 5, tzinfo=timezone.utc),
    )

    result = materialize_assembly_output_allocations(db_session, generation.id)
    allocations = db_session.query(models.AssemblyOutputAllocation).filter_by(
        ledger_generation_id=generation.id,
    ).order_by(models.AssemblyOutputAllocation.allocation_ordinal.asc()).all()

    assert result["facts"] == 1
    assert result["allocations"] == 2
    assert [int(a.plan_line_id) for a in allocations] == [int(line_one.id), int(line_two.id)]
    assert [Decimal(a.allocated_qty) for a in allocations] == [Decimal("2"), Decimal("5")]
    assert Decimal(result["surplus_total"]) == Decimal("0")


def test_historical_output_before_plan_fixation_cannot_close_new_plan(db_session):
    cutoff = datetime(2026, 7, 31, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="eligibility", cutoff=cutoff)
    item = _item(db_session, "ASM-ELIGIBILITY")
    _, _, line = _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        item=item,
        qty=Decimal("10"),
        fixed_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )
    old_fact = _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="7",
        at=datetime(2026, 7, 9, tzinfo=timezone.utc),
        recorder="eligibility-old",
        content_hash="1" * 64,
    )
    new_fact = _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="4",
        at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        recorder="eligibility-new",
        content_hash="2" * 64,
    )

    result = materialize_assembly_output_allocations(db_session, generation.id)
    allocations = (
        db_session.query(models.AssemblyOutputAllocation)
        .filter_by(ledger_generation_id=generation.id)
        .order_by(models.AssemblyOutputAllocation.allocation_ordinal.asc())
        .all()
    )
    decisions = {
        int(row.stock_ledger_entry_id): row
        for row in db_session.query(models.AssemblyOutputFactDecision)
        .filter_by(ledger_generation_id=generation.id)
        .all()
    }

    assert len(allocations) == 1
    assert int(allocations[0].stock_ledger_entry_id) == int(new_fact.id)
    assert int(allocations[0].plan_line_id) == int(line.id)
    assert Decimal(allocations[0].allocated_qty) == Decimal("4")
    assert Decimal(decisions[int(old_fact.id)].surplus_qty) == Decimal("7")
    assert Decimal(result["fact_qty"]) == Decimal("11")
    assert Decimal(result["allocated_qty"]) == Decimal("4")
    assert Decimal(result["surplus_total"]) == Decimal("7")


def test_fixed_plan_without_fixation_boundary_fails_closed(db_session):
    cutoff = datetime(2026, 7, 31, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="missing-boundary", cutoff=cutoff)
    item = _item(db_session, "ASM-NO-BOUNDARY")
    plan, run, _line = _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        item=item,
        qty=Decimal("5"),
    )
    plan.fixed_at = None
    run.fixed_at = None
    _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="5",
        at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        recorder="missing-boundary",
    )

    with pytest.raises(ValueError, match="lacks fixed_at"):
        materialize_assembly_output_allocations(db_session, generation.id)


def test_exact_manufacture_provenance_allocates_oldest_first_inside_plan(db_session):
    cutoff = datetime(2026, 7, 31, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="exact", cutoff=cutoff)
    item = _item(db_session, "ASM-EXACT")
    _, _, old_line = _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        item=item,
        qty=Decimal("10"),
        period_from=date(2026, 6, 1),
        fixed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    exact_plan, exact_run, exact_line = _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        item=item,
        qty=Decimal("3"),
        period_from=date(2026, 7, 1),
        fixed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    second_exact_line = models.ProductionPlanLine(
        plan_id=int(exact_plan.id),
        item_id=int(item.item_id),
        bucket_date=date(2026, 7, 2),
        qty=Decimal("2"),
    )
    db_session.add(second_exact_line)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=int(exact_run.run_id),
        item_id=int(item.item_id),
        total_required_qty=Decimal("3"),
        net_required_qty=Decimal("3"),
        period_from=exact_plan.period_from,
        period_to=exact_plan.period_to,
        bom_level=0,
        status="open",
    )
    db_session.add(requirement)
    db_session.flush()
    order = models.ProductionOrder(
        order_number="MRP-EXACT",
        order_date=datetime(2026, 7, 2, tzinfo=timezone.utc),
        source="mrp",
        source_run_id=int(exact_run.run_id),
        deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
    product = models.ProductionProduct(
        order_id=int(order.order_id),
        item_id=int(item.item_id),
        line_number=1,
        quantity=Decimal("3"),
        produced_qty=Decimal("0"),
        remaining_qty=Decimal("3"),
        source_mrp_requirement_id=int(requirement.id),
        ledger_generation_id=int(generation.id),
    )
    db_session.add(product)
    db_session.flush()
    recorder = "exact-recorder"
    db_session.add(
        models.ProductionManufacture(
            product_id=int(product.product_id),
            order_id=int(order.order_id),
            qty=Decimal("5"),
            status="exported",
            exported_ref1c=recorder,
        )
    )
    fact = _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="5",
        at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        recorder=recorder,
        content_hash="3" * 64,
    )

    first = materialize_assembly_output_allocations(db_session, generation.id)
    second = materialize_assembly_output_allocations(db_session, generation.id)
    allocations = (
        db_session.query(models.AssemblyOutputAllocation)
        .filter_by(ledger_generation_id=generation.id)
        .order_by(models.AssemblyOutputAllocation.allocation_ordinal.asc())
        .all()
    )
    decision = (
        db_session.query(models.AssemblyOutputFactDecision)
        .filter_by(
            ledger_generation_id=generation.id,
            stock_ledger_entry_id=fact.id,
        )
        .one()
    )

    assert second == first
    assert [int(row.plan_line_id) for row in allocations] == [
        int(exact_line.id),
        int(second_exact_line.id),
    ]
    assert int(old_line.id) not in {int(row.plan_line_id) for row in allocations}
    assert [row.match_rule for row in allocations] == ["exact", "exact"]
    assert [Decimal(row.allocated_qty) for row in allocations] == [
        Decimal("3"),
        Decimal("2"),
    ]
    assert decision.link_kind == "exact_plan_line"
    assert decision.decision_status == "allocatable"
    assert Decimal(decision.surplus_qty) == Decimal("0")
    assert decision.evidence_payload["exact_plan_line_ids"] == [
        int(exact_line.id),
        int(second_exact_line.id),
    ]
    assert Decimal(first["fact_qty"]) == (
        Decimal(first["allocated_qty"]) + Decimal(first["surplus_total"])
    )



def test_caps_line_and_fact_keep_surplus(db_session):
    cutoff = datetime(2026, 7, 31, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="caps", cutoff=cutoff)
    item = _item(db_session, "ASM-CAPS")

    _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        item=item,
        qty=Decimal("3"),
        period_from=date(2026, 7, 1),
    )
    _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        item=item,
        qty=Decimal("1"),
        period_from=date(2026, 7, 1),
    )

    fact_one = _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="5",
        at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        recorder="caps-1",
    )
    fact_two = _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="4",
        at=datetime(2026, 7, 4, tzinfo=timezone.utc),
        recorder="caps-2",
    )
    _ = (fact_one, fact_two)

    result = materialize_assembly_output_allocations(db_session, generation.id)
    rows = db_session.query(models.AssemblyOutputAllocation).filter_by(
        ledger_generation_id=generation.id,
    ).order_by(
        models.AssemblyOutputAllocation.stock_ledger_entry_id.asc(),
        models.AssemblyOutputAllocation.allocation_ordinal.asc(),
    ).all()
    decisions = db_session.query(models.AssemblyOutputFactDecision).filter_by(
        ledger_generation_id=generation.id,
    ).order_by(models.AssemblyOutputFactDecision.stock_ledger_entry_id.asc()).all()

    assert result["facts"] == 2
    assert result["allocations"] == 2
    assert [Decimal(r.allocated_qty) for r in rows] == [Decimal("3"), Decimal("1")]
    assert Decimal(result["surplus_total"]) == Decimal("5")
    assert len(rows) == 2
    assert Decimal(decisions[0].surplus_qty) == Decimal("1")
    assert Decimal(decisions[1].surplus_qty) == Decimal("4")


def test_only_live_fixed_plans_are_used(db_session):
    cutoff = datetime(2026, 7, 31, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="closed", cutoff=cutoff)
    item = _item(db_session, "ASM-CLOSED")

    _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        run_status="FIXED_SNAPSHOT",
        item=item,
        qty=Decimal("10"),
        period_from=date(2026, 7, 1),
    )
    _plan_with_run(
        db_session,
        generation=generation,
        plan_status="closed",
        run_status="ARCHIVED",  # not a live snapshot route
        item=item,
        qty=Decimal("99"),
        period_from=date(2026, 6, 1),
    )

    _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="7",
        at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        recorder="closed",
    )

    result = materialize_assembly_output_allocations(db_session, generation.id)
    rows = db_session.query(models.AssemblyOutputAllocation).filter_by(
        ledger_generation_id=generation.id,
    ).all()

    assert result["allocations"] == 1
    assert sum(Decimal(row.allocated_qty) for row in rows) == Decimal("7")


def test_rerun_is_idempotent_and_drift_conflict_raises(db_session):
    cutoff = datetime(2026, 7, 31, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="rerun", cutoff=cutoff)
    item = _item(db_session, "ASM-RERUN")

    _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        item=item,
        qty=Decimal("5"),
        period_from=date(2026, 7, 1),
    )
    sle = _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="4",
        at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        recorder="rerun",
    )

    first = materialize_assembly_output_allocations(db_session, generation.id)
    second = materialize_assembly_output_allocations(db_session, generation.id)

    assert second["fact_checksum"] == first["fact_checksum"]
    assert second["allocation_checksum"] == first["allocation_checksum"]
    assert second["batch_id"] == first["batch_id"]

    sle.qty = Decimal("6")
    db_session.flush()
    with pytest.raises(ValueError, match="drift"):
        materialize_assembly_output_allocations(db_session, generation.id)


def test_rerun_accepts_the_empty_batch_it_wrote_itself(db_session):
    """A close with no assembly facts must stay resumable.

    The pass then writes neither a decision nor an allocation, so its own
    COMPLETED batch is the only evidence it ever ran.  Refusing that batch as
    drift used to kill every resume of «Закрыть план» past this stage.
    """
    cutoff = datetime(2026, 7, 31, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="empty-rerun", cutoff=cutoff)
    item = _item(db_session, "ASM-EMPTY")
    _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        item=item,
        qty=Decimal("5"),
        period_from=date(2026, 7, 1),
    )

    first = materialize_assembly_output_allocations(db_session, generation.id)
    assert first["facts"] == 0
    assert first["allocations"] == 0

    second = materialize_assembly_output_allocations(db_session, generation.id)

    assert second == first
    assert db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=generation.id,
        stage="assembly_output_allocation",
    ).count() == 1

    # A fact that appears after the seal changes the rebuilt set, and that is
    # still drift — resuming never means silently re-allocating.
    _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="3",
        at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        recorder="late",
    )
    with pytest.raises(ValueError, match="drift"):
        materialize_assembly_output_allocations(db_session, generation.id)


def test_rerun_rewrites_rows_its_interrupted_worker_never_persisted(db_session):
    """Resume rebuilds only what is missing under the batch it already wrote."""
    cutoff = datetime(2026, 7, 31, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="partial-rerun", cutoff=cutoff)
    item = _item(db_session, "ASM-PARTIAL")
    _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        item=item,
        qty=Decimal("5"),
        period_from=date(2026, 7, 1),
    )
    _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="4",
        at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        recorder="partial",
    )

    first = materialize_assembly_output_allocations(db_session, generation.id)
    db_session.query(models.AssemblyOutputAllocation).delete()
    db_session.query(models.AssemblyOutputFactDecision).delete()
    db_session.query(models.ProductionPlanExecutionFact).delete()
    line = db_session.query(models.ProductionPlanLine).one()
    root = db_session.query(models.MrpRunRoot).one()
    line.accepted_output_qty = Decimal("0")
    line.remaining_output_qty = Decimal("5")
    root.accepted_qty = Decimal("0")
    root.remaining_qty = Decimal("5")
    db_session.flush()

    second = materialize_assembly_output_allocations(db_session, generation.id)

    assert second == first
    assert db_session.query(models.AssemblyOutputFactDecision).count() == 1
    allocation = db_session.query(models.AssemblyOutputAllocation).one()
    assert Decimal(allocation.allocated_qty) == Decimal("4")


def test_isolated_by_generation_physical_prefix(db_session):
    cutoff = datetime(2026, 7, 31, tzinfo=timezone.utc)
    generation_a = _building_generation(db_session, key="iso-a", cutoff=cutoff)
    generation_b = _building_generation(db_session, key="iso-b", cutoff=cutoff)
    item = _item(db_session, "ASM-ISO")

    _plan_with_run(
        db_session,
        generation=generation_a,
        plan_status="fixed",
        item=item,
        qty=Decimal("10"),
        period_from=date(2026, 7, 1),
    )
    _plan_with_run(
        db_session,
        generation=generation_b,
        plan_status="fixed",
        item=item,
        qty=Decimal("10"),
        period_from=date(2026, 7, 1),
    )

    _sline(
        db_session,
        batch=generation_a.physical_import_batch,
        item=item,
        qty="5",
        at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        recorder="iso-a",
    )
    _sline(
        db_session,
        batch=generation_b.physical_import_batch,
        item=item,
        qty="6",
        at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        recorder="iso-b",
    )

    first = materialize_assembly_output_allocations(db_session, generation_a.id)
    second = materialize_assembly_output_allocations(db_session, generation_b.id)

    assert first["ledger_generation_id"] == int(generation_a.id)
    assert second["ledger_generation_id"] == int(generation_b.id)
    assert first["fact_qty"] == "5"
    # Accepted assignments are persisted once. A later generation processes
    # only facts not already written to plan execution.
    assert second["fact_qty"] == "6"
    assert first["batch_id"] != second["batch_id"]


def test_live_plan_allocation_appends_only_new_facts_in_next_generation(
    db_session,
):
    cutoff_a = datetime(2026, 7, 10, tzinfo=timezone.utc)
    cutoff_b = datetime(2026, 7, 20, tzinfo=timezone.utc)
    generation_a = _building_generation(
        db_session,
        key="carry-a",
        cutoff=cutoff_a,
    )
    generation_b = _building_generation(
        db_session,
        key="carry-b",
        cutoff=cutoff_b,
    )
    item = _item(db_session, "ASM-CARRY")
    _plan, run, line = _plan_with_run(
        db_session,
        generation=generation_a,
        plan_status="fixed",
        item=item,
        qty=Decimal("10"),
        period_from=date(2026, 7, 1),
    )
    _sline(
        db_session,
        batch=generation_a.physical_import_batch,
        item=item,
        qty="5",
        at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        recorder="carry-a",
        content_hash="a" * 64,
    )

    materialize_assembly_output_allocations(db_session, generation_a.id)
    historical = (
        db_session.query(models.AssemblyOutputAllocation)
        .filter_by(
            ledger_generation_id=generation_a.id,
            plan_line_id=line.id,
        )
        .one()
    )
    assert historical.allocated_qty == Decimal("5")

    # The next generation sees only the new fact. The stored plan/run execution
    # already contains the first five and is advanced to seven exactly once.
    run.ledger_generation_id = int(generation_b.id)
    run.ledger_cutoff = cutoff_b
    _sline(
        db_session,
        batch=generation_b.physical_import_batch,
        item=item,
        qty="2",
        at=datetime(2026, 7, 12, tzinfo=timezone.utc),
        recorder="carry-b",
        content_hash="b" * 64,
    )

    materialize_assembly_output_allocations(db_session, generation_b.id)
    current = (
        db_session.query(models.AssemblyOutputAllocation)
        .filter_by(
            ledger_generation_id=generation_b.id,
            plan_line_id=line.id,
        )
        .all()
    )

    assert sum((row.allocated_qty for row in current), Decimal("0")) == Decimal("2")
    assert historical.allocated_qty == Decimal("5")
    assert {row.ledger_generation_id for row in current} == {generation_b.id}
    db_session.refresh(line)
    root = db_session.query(models.MrpRunRoot).filter_by(
        run_id=run.run_id, plan_line_id=line.id
    ).one()
    assert line.accepted_output_qty == Decimal("7")
    assert line.remaining_output_qty == Decimal("3")
    assert root.accepted_qty == Decimal("7")
    assert root.remaining_qty == Decimal("3")


def test_queue_rows_follow_allocations_and_feed_drum_schedule(db_session):
    cutoff = datetime(2026, 8, 3, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="drum-from-queue", cutoff=cutoff)
    item = _item(db_session, "ASM-DRUM")
    resource = models.ProductionResource(
        resource_name="Assembly",
        planning_range=1,
        capacity=Decimal("10"),
    )
    db_session.add(resource)
    db_session.flush()
    db_session.add(
        models.AssemblyRate(
            resource_id=resource.resource_id,
            item_id=item.item_id,
            qty_per_capacity=Decimal("1"),
        )
    )
    db_session.flush()

    _, _, line = _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        item=item,
        qty=Decimal("10"),
        period_from=date(2026, 8, 1),
    )

    _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="3",
        at=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )

    result = materialize_assembly_output_allocations(db_session, generation.id)
    queue = (
        db_session.query(models.AssemblyQueueLine)
        .filter_by(ledger_generation_id=generation.id, plan_line_id=line.id)
        .one()
    )
    assert queue.accepted_plan_output_qty == Decimal("3")
    assert queue.assembly_remaining_qty == Decimal("7")
    assert result["allocations"] == 1

    schedule = materialize_drum_schedule(db_session, generation.id)
    assert Decimal(schedule["total_open_qty"]) == Decimal("7")
    assert Decimal(schedule["total_slot_qty"]) == Decimal("7")
    assert db_session.query(models.DrumSlot).count() == 1
    assert db_session.query(models.DrumSlot).one().slot_qty == Decimal("7")


def test_fully_allocated_queue_line_is_fulfilled_and_excluded_from_snapshot(
    db_session,
):
    cutoff = datetime(2026, 8, 3, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="fulfilled-queue", cutoff=cutoff)
    item = _item(db_session, "ASM-FULFILLED")
    _, _, line = _plan_with_run(
        db_session,
        generation=generation,
        plan_status="fixed",
        item=item,
        qty=Decimal("3"),
        period_from=date(2026, 8, 1),
    )
    _sline(
        db_session,
        batch=generation.physical_import_batch,
        item=item,
        qty="3",
        at=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )

    materialize_assembly_output_allocations(db_session, generation.id)
    queue = db_session.query(models.AssemblyQueueLine).filter_by(
        ledger_generation_id=generation.id,
        plan_line_id=line.id,
    ).one()
    assert queue.assembly_remaining_qty == Decimal("0")
    assert queue.line_status == "fulfilled"

    snapshot = build_assembly_queue_snapshot(db_session, generation.id)
    assert snapshot.payload["rows"] == []
    assert snapshot.payload["total_rows"] == 0
    assert snapshot.payload["total_queue_qty"] == 0.0

    repeated = materialize_assembly_output_allocations(db_session, generation.id)
    assert repeated["allocations"] == 1

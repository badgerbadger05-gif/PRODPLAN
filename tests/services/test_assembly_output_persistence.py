from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.assembly_output_persistence import materialize_assembly_output_allocations


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
):
    plan_period_from = period_from if period_from is not None else date(2026, 7, 1)
    plan_period_to = period_to if period_to is not None else date(2026, 12, 31)

    plan = models.ProductionPlanHeader(
        name=f"plan-{item.item_code}-{plan_period_from.isoformat()}",
        period_from=plan_period_from,
        period_to=plan_period_to,
        status=plan_status,
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
    # A later physical batch names the full immutable prefix, not only its delta.
    assert second["fact_qty"] == "11"
    assert first["batch_id"] != second["batch_id"]


def test_live_plan_allocation_is_rebuilt_in_next_generation_without_mutating_history(
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

    # A live fixed plan is projected again in the next generation. The new
    # projection sees the immutable physical prefix; the old allocation stays
    # generation-scoped history.
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

    assert sum((row.allocated_qty for row in current), Decimal("0")) == Decimal("7")
    assert historical.allocated_qty == Decimal("5")
    assert {row.ledger_generation_id for row in current} == {generation_b.id}

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger import assembly_queue_snapshot
from app.services.item_ledger import drum_schedule_persistence
from app.services.item_ledger.drum_schedule_persistence import (
    materialize_drum_schedule,
)
from app.services.item_ledger.shelf_projection_persistence import (
    materialize_shelf_projections,
)


def _building_generation(db, *, key: str, cutoff):
    physical = models.PhysicalImportBatch(
        batch_key=f"assembly-{key}",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    db.add(physical)
    db.flush()
    generation = models.LedgerGeneration(
        generation_key=f"assembly-generation-{key}",
        status="building",
        cutoff=cutoff,
        capabilities={},
        source_watermarks={},
        physical_import_batch_id=physical.id,
        algorithm_version="test",
    )
    db.add(generation)
    db.flush()
    return generation


def _production_plan(db, *, start: date, end: date, status: str = "fixed"):
    return models.ProductionPlanHeader(
        name=f"assembly-plan-{start.isoformat()}",
        period_from=start,
        period_to=end,
        status=status,
        fixed_at=(datetime(2026, 7, 1, tzinfo=timezone.utc) if status == "fixed" else None),
    )


def _run(db, *, generation, plan, status: str = "FIXED_SNAPSHOT"):
    run = models.PlanningRun(
        status=status,
        config_snapshot={},
        ledger_generation_id=int(generation.id),
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db.add(run)
    db.flush()
    return run


def _plan_line(db, *, plan, item, bucket_date: date, qty):
    row = models.ProductionPlanLine(
        plan_id=plan.id,
        item_id=item.item_id,
        bucket_date=bucket_date,
        qty=Decimal(str(qty)),
    )
    db.add(row)
    db.flush()
    return row


def _stock_ledger_entry(db, *, generation, item, tag: str):
    source_content_hash = (tag * 64)[:64]
    row = models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash=source_content_hash,
        item_id=item.item_id,
        qty=Decimal("1.000"),
        posting_at=generation.cutoff,
        recorder_type="assembly-output-test",
        recorder_ref=f"{tag}-{generation.id}",
        line_no="1",
        active=True,
    )
    db.add(row)
    db.flush()
    return row


def test_assembly_queue_rejects_divergent_run_and_plan_periods(db_session):
    cutoff = datetime(2026, 7, 30, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="period-mismatch", cutoff=cutoff)
    item = models.Item(item_code="FG-PERIOD", item_name="Period mismatch")
    plan = _production_plan(
        db_session,
        start=date(2026, 8, 1),
        end=date(2026, 8, 31),
    )
    db_session.add_all((item, plan))
    db_session.flush()
    run = _run(db_session, generation=generation, plan=plan)
    run.period_from = date(2026, 9, 1)
    run.period_to = date(2026, 9, 30)
    _plan_line(db_session, plan=plan, item=item, bucket_date=date(2026, 8, 5), qty=1)
    db_session.flush()

    with pytest.raises(ValueError, match="assembly queue period mismatch"):
        assembly_queue_snapshot._build_rows(db_session, int(generation.id))


def _allocation(
    db,
    *,
    generation,
    plan,
    line,
    item,
    qty,
    tag: str,
    match_rule: str = "fifo",
):
    sle = _stock_ledger_entry(db, generation=generation, item=item, tag=tag)
    row = models.AssemblyOutputAllocation(
        ledger_generation_id=int(generation.id),
        stock_ledger_entry_id=int(sle.id),
        plan_id=int(plan.id),
        plan_line_id=int(line.id),
        allocated_qty=Decimal(str(qty)),
        match_rule=match_rule,
    )
    db.add(row)
    db.flush()
    return row


def test_assembly_queue_snapshot_prefers_fifo_across_live_plans_and_sums_allocations(db_session):
    cutoff = datetime(2026, 7, 30, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="fifo", cutoff=cutoff)

    item = models.Item(item_code="FG-1", item_name="Finished Good")
    db_session.add(item)

    plan_old = _production_plan(
        db_session,
        start=date(2026, 8, 1),
        end=date(2026, 8, 31),
    )
    plan_new = _production_plan(
        db_session,
        start=date(2026, 9, 1),
        end=date(2026, 9, 30),
    )
    plan_closed = _production_plan(
        db_session,
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
        status="closed",
    )
    db_session.add_all((plan_old, plan_new, plan_closed, item))
    db_session.flush()

    run_old = _run(db_session, generation=generation, plan=plan_old)
    run_new = _run(db_session, generation=generation, plan=plan_new)
    run_closed = _run(db_session, generation=generation, plan=plan_closed)

    old_line = _plan_line(
        db_session,
        plan=plan_old,
        item=item,
        bucket_date=date(2026, 8, 3),
        qty="12",
    )
    new_line = _plan_line(
        db_session,
        plan=plan_new,
        item=item,
        bucket_date=date(2026, 9, 2),
        qty="7",
    )
    closed_line = _plan_line(
        db_session,
        plan=plan_closed,
        item=item,
        bucket_date=date(2026, 7, 15),
        qty="20",
    )

    _allocation(
        db_session,
        generation=generation,
        plan=plan_old,
        line=old_line,
        item=item,
        qty="3",
        tag="A",
        match_rule="fifo",
    )
    _allocation(
        db_session,
        generation=generation,
        plan=plan_old,
        line=old_line,
        item=item,
        qty="1",
        tag="B",
        match_rule="fifo",
    )
    _allocation(
        db_session,
        generation=generation,
        plan=plan_new,
        line=new_line,
        item=item,
        qty="2",
        tag="C",
        match_rule="fifo",
    )
    _allocation(
        db_session,
        generation=generation,
        plan=plan_closed,
        line=closed_line,
        item=item,
        qty="9",
        tag="D",
        match_rule="fifo",
    )

    snapshot = assembly_queue_snapshot.build_assembly_queue_snapshot(db_session, generation.id)

    assert snapshot.consumer == assembly_queue_snapshot.CONSUMER
    assert snapshot.snapshot_key == assembly_queue_snapshot.SNAPSHOT_KEY
    assert snapshot.truth_status == "accepted"
    assert snapshot.payload["total_rows"] == 2
    assert snapshot.payload["total_queue_qty"] == 13.0

    rows = snapshot.payload["rows"]
    assert [row["plan_id"] for row in rows] == [plan_old.id, plan_new.id]
    assert [row["plan_line_id"] for row in rows] == [old_line.id, new_line.id]
    assert rows[0]["item_code"] == "FG-1"
    assert rows[0]["bucket_date"] == "2026-08-03"
    assert rows[0]["priority_key"] == [
        plan_old.period_from.isoformat(),
        plan_old.period_to.isoformat(),
        int(plan_old.id),
        int(old_line.id),
    ]
    assert rows[0]["planned_output_qty"] == 12.0
    assert rows[0]["accepted_plan_output_qty"] == 4.0
    assert rows[0]["assembly_remaining_qty"] == 8.0
    assert rows[1]["priority_key"] == [
        plan_new.period_from.isoformat(),
        plan_new.period_to.isoformat(),
        int(plan_new.id),
        int(new_line.id),
    ]
    assert rows[1]["planned_output_qty"] == 7.0
    assert rows[1]["accepted_plan_output_qty"] == 2.0
    assert rows[1]["assembly_remaining_qty"] == 5.0

    db_rows = (
        db_session.query(models.PlanningReadRow)
        .filter_by(snapshot_id=snapshot.id)
        .order_by(models.PlanningReadRow.sort_key.asc())
        .all()
    )
    assert [row.row_key for row in db_rows] == [f"plan-line:{old_line.id}", f"plan-line:{new_line.id}"]
    assert db_rows[0].item_id == item.item_id


def test_assembly_queue_snapshot_excludes_zero_or_negative_remaining_rows(db_session):
    cutoff = datetime(2026, 7, 30, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="zero", cutoff=cutoff)

    item = models.Item(item_code="FG-Z", item_name="Zero Item")
    db_session.add(item)
    plan = _production_plan(db_session, start=date(2026, 8, 1), end=date(2026, 8, 31))
    db_session.add(plan)
    db_session.flush()
    run = _run(db_session, generation=generation, plan=plan)

    zero_line = _plan_line(db_session, plan=plan, item=item, bucket_date=date(2026, 8, 3), qty="10")
    positive_line = _plan_line(db_session, plan=plan, item=item, bucket_date=date(2026, 8, 7), qty="9")
    depleted_line = _plan_line(db_session, plan=plan, item=item, bucket_date=date(2026, 8, 12), qty="6")

    _allocation(db_session, generation=generation, plan=plan, line=zero_line, item=item, qty="10", tag="X")
    _allocation(db_session, generation=generation, plan=plan, line=depleted_line, item=item, qty="12", tag="Y")
    _allocation(db_session, generation=generation, plan=plan, line=positive_line, item=item, qty="4", tag="Z")

    snapshot = assembly_queue_snapshot.build_assembly_queue_snapshot(db_session, generation.id)

    rows = snapshot.payload["rows"]
    assert len(rows) == 1
    assert rows[0]["plan_line_id"] == int(positive_line.id)
    assert rows[0]["planned_output_qty"] == 9.0
    assert rows[0]["accepted_plan_output_qty"] == 4.0
    assert rows[0]["assembly_remaining_qty"] == 5.0
    assert snapshot.payload["total_rows"] == 1
    assert snapshot.payload["total_queue_qty"] == 5.0


def test_canonical_drum_persists_normalized_queue_slots_and_gap(db_session, monkeypatch):
    cutoff = datetime(2026, 7, 27, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="drum", cutoff=cutoff)
    item = models.Item(item_code="FG-DRUM", item_name="Drum item")
    resource = models.ProductionResource(
        resource_name="Assembly",
        planning_range=2,
        capacity=Decimal("5"),
    )
    plan = _production_plan(
        db_session,
        start=date(2026, 8, 1),
        end=date(2026, 8, 31),
    )
    db_session.add_all([item, resource, plan])
    db_session.flush()
    run = _run(db_session, generation=generation, plan=plan)
    line = _plan_line(
        db_session,
        plan=plan,
        item=item,
        bucket_date=date(2026, 8, 3),
        qty="12",
    )
    db_session.add(
        models.AssemblyRate(
            resource_id=resource.resource_id,
            item_id=item.item_id,
            qty_per_capacity=Decimal("1"),
        )
    )
    component = models.Item(
        item_code="COMP-SHELF",
        item_name="Shelf component",
        replenishment_method="Производство",
    )
    db_session.add(component)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=component.item_id,
        total_required_qty=Decimal("12"),
        net_required_qty=Decimal("12"),
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=1,
    )
    db_session.add(requirement)
    db_session.flush()
    db_session.add_all(
        [
            models.ReservationEntry(
                ledger_generation_id=generation.id,
                item_id=component.item_id,
                run_id=run.run_id,
                requirement_id=requirement.id,
                priority_period_from=plan.period_from,
                priority_period_to=plan.period_to,
                realization_mode="make",
                reserved_qty=Decimal("12"),
                replenishment_required_qty=Decimal("12"),
            ),
                models.MrpFreezeComponent(
                run_id=run.run_id,
                freeze_version=1,
                parent_item_id=item.item_id,
                component_item_id=component.item_id,
                spec_ref="test",
                    norm_qty_per_unit=Decimal("2"),
                ),
                models.MrpFreezeComponentCumulative(
                    run_id=run.run_id,
                    freeze_version=1,
                    root_item_id=item.item_id,
                    component_item_id=component.item_id,
                    cumulative_norm_qty_per_root_unit=Decimal("2"),
                ),
            models.ShelfPolicy(
                item_id=component.item_id,
                warehouse_ref1c="SHELF",
                replenishment_time_days=5,
                review_cycle_days=3,
                safety_days=2,
                batch_multiple=Decimal("4"),
            ),
            models.StockBin(
                ledger_generation_id=generation.id,
                item_id=component.item_id,
                warehouse_ref1c="SHELF",
                on_hand=Decimal("3"),
            ),
            models.StockBin(
                ledger_generation_id=generation.id,
                item_id=component.item_id,
                warehouse_ref1c="OTHER",
                on_hand=Decimal("4"),
            ),
        ]
    )
    db_session.flush()

    first = materialize_drum_schedule(db_session, generation.id)
    monkeypatch.setattr(
        drum_schedule_persistence,
        "materialize_assembly_queue_lines",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("completed drum checkpoint must not rebuild its input")
        ),
    )
    second = materialize_drum_schedule(db_session, generation.id)
    shelf = materialize_shelf_projections(db_session, generation.id)

    assert first == second
    queue = db_session.query(models.AssemblyQueueLine).one()
    assert queue.plan_line_id == line.id
    assert queue.assembly_remaining_qty == Decimal("12")
    assert [
        row.slot_qty
        for row in db_session.query(models.DrumSlot).order_by(
            models.DrumSlot.slot_ordinal
        )
    ] == [Decimal("5"), Decimal("5")]
    gap = db_session.query(models.DrumCapacityGap).one()
    assert gap.plan_line_id == line.id
    assert gap.gap_qty == Decimal("2")
    assert Decimal(first["total_open_qty"]) == Decimal("12")
    assert shelf["projection_rows"] == 1
    shelf_row = db_session.query(models.ShelfProjection).one()
    assert shelf_row.target_qty == Decimal("12")
    assert shelf_row.projected_qty == Decimal("3")
    assert shelf_row.transfer_qty == Decimal("0")
    assert shelf_row.pull_qty == Decimal("9")
    assert shelf_row.materialized_qty == Decimal("12")


def test_queue_line_without_rate_is_excluded_from_drum_only(db_session):
    cutoff = datetime(2026, 7, 27, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="no-drum-rate", cutoff=cutoff)
    item = models.Item(item_code="FG-NO-RATE", item_name="Queue only")
    plan = _production_plan(
        db_session,
        start=date(2026, 8, 1),
        end=date(2026, 8, 31),
    )
    db_session.add_all([item, plan])
    db_session.flush()
    _run(db_session, generation=generation, plan=plan)
    _plan_line(
        db_session,
        plan=plan,
        item=item,
        bucket_date=date(2026, 8, 3),
        qty="4",
    )
    db_session.flush()

    result = materialize_drum_schedule(db_session, generation.id)

    queue = db_session.query(models.AssemblyQueueLine).one()
    assert queue.assembly_remaining_qty == Decimal("4")
    assert db_session.query(models.DrumSlot).count() == 0
    assert db_session.query(models.DrumCapacityGap).count() == 0
    assert result["excluded_lines"] == 1
    assert Decimal(result["excluded_open_qty"]) == Decimal("4")
    assert result["excluded_item_ids"] == [item.item_id]
    assert Decimal(result["total_open_qty"]) == Decimal("0")


def test_assembly_queue_snapshot_is_idempotent_for_same_inputs(db_session):
    cutoff = datetime(2026, 7, 30, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="idempotent", cutoff=cutoff)

    item = models.Item(item_code="FG-ID", item_name="Idem")
    db_session.add(item)
    plan = _production_plan(db_session, start=date(2026, 8, 1), end=date(2026, 8, 31))
    db_session.add(plan)
    db_session.flush()
    run = _run(db_session, generation=generation, plan=plan)
    line = _plan_line(db_session, plan=plan, item=item, bucket_date=date(2026, 8, 3), qty="11")
    _allocation(db_session, generation=generation, plan=plan, line=line, item=item, qty="2", tag="ID")

    first = assembly_queue_snapshot.build_assembly_queue_snapshot(db_session, generation.id)
    second = assembly_queue_snapshot.build_assembly_queue_snapshot(db_session, generation.id)

    assert second.id == first.id
    assert db_session.query(models.PlanningReadSnapshot).count() == 1
    assert (
        db_session.query(models.PlanningReadRow)
        .filter_by(snapshot_id=first.id)
        .count()
        == 1
    )


def test_assembly_queue_snapshot_ignores_live_plan_changes_after_materialization(db_session):
    cutoff = datetime(2026, 7, 30, tzinfo=timezone.utc)
    generation = _building_generation(db_session, key="conflict", cutoff=cutoff)

    item = models.Item(item_code="FG-CF", item_name="Conflict")
    db_session.add(item)
    plan = _production_plan(db_session, start=date(2026, 8, 1), end=date(2026, 8, 31))
    db_session.add(plan)
    db_session.flush()
    run = _run(db_session, generation=generation, plan=plan)
    line = _plan_line(db_session, plan=plan, item=item, bucket_date=date(2026, 8, 9), qty="10")
    _allocation(db_session, generation=generation, plan=plan, line=line, item=item, qty="3", tag="C")

    first = assembly_queue_snapshot.build_assembly_queue_snapshot(db_session, generation.id)
    assert first.id > 0

    db_session.query(models.ProductionPlanLine).filter_by(id=line.id).update(
        {models.ProductionPlanLine.qty: Decimal("20")}
    )
    db_session.flush()
    repeated = assembly_queue_snapshot.build_assembly_queue_snapshot(db_session, generation.id)
    assert repeated.id == first.id

    row = (
        db_session.query(models.PlanningReadRow)
        .filter_by(snapshot_id=first.id)
        .one()
    )
    payload = dict(row.payload or {})
    payload["assembly_remaining_qty"] = 0.0
    row.payload = payload
    db_session.flush()

    with pytest.raises(ValueError, match="conflicts"):
        assembly_queue_snapshot.build_assembly_queue_snapshot(db_session, generation.id)

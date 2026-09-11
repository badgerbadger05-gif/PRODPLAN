from datetime import date
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    drum_slot_identity,
    get_current_execution_scope,
    invalidate_current_execution_scope,
    load_current_execution_rows,
    order_execution_queue,
    publish_current_execution_scope,
    require_current_execution_scope,
)


def _seed_generation_execution_rows(db, *, generation_id: int, queue_line_id: int):
    """Seed identical staged queue/readiness/drum rows with a generation-local queue id."""
    from datetime import date

    queue = models.AssemblyQueueLine(
        id=queue_line_id,
        ledger_generation_id=generation_id,
        planning_run_id=101,
        plan_id=201,
        plan_line_id=301,
        item_id=401,
        bucket_date=date(2026, 9, 10),
        period_from=date(2026, 9, 10),
        period_to=date(2026, 9, 10),
        planned_output_qty=Decimal("10"),
        accepted_plan_output_qty=Decimal("0"),
        assembly_remaining_qty=Decimal("10"),
        original_priority=["2026-09-10", 201, 301],
        sort_key="2026-09-10|201|301",
        line_status="open",
    )
    db.add(queue)
    db.flush()
    db.add(models.AssemblyReadiness(
        ledger_generation_id=generation_id,
        assembly_queue_line_id=queue_line_id,
        status="ready",
        open_qty=Decimal("10"),
        ready_qty=Decimal("10"),
        transferable_qty=Decimal("0"),
        kitting_qty=Decimal("0"),
        committed_qty=Decimal("0"),
        launchable_qty=Decimal("10"),
        evidence_signature=f"r8-{generation_id}",
    ))
    schedule = models.DrumSchedule(
        ledger_generation_id=generation_id,
        status="completed",
        algorithm_version="tests/r8",
        schedule_from=date(2026, 9, 10),
        schedule_to=date(2026, 9, 10),
        working_days=["2026-09-10"],
        resource_horizon_ends={},
        resource_daily_capacities={},
        queue_signature=f"q-{generation_id}",
        slot_signature=f"s-{generation_id}",
        gap_signature=f"g-{generation_id}",
        slot_row_count=1,
        gap_row_count=0,
        total_open_qty=Decimal("10"),
        total_slot_qty=Decimal("10"),
        total_gap_qty=Decimal("0"),
        metrics={
            "total_open_qty": "10",
            "total_slot_qty": "10",
            "total_gap_qty": "0",
            "excluded_lines": 1,
            "excluded_open_qty": "10",
            "excluded_item_ids": [401],
        },
    )
    db.add(schedule)
    db.flush()
    db.add(models.DrumSlot(
        drum_schedule_id=schedule.id,
        assembly_queue_line_id=queue_line_id,
        plan_id=201,
        plan_line_id=301,
        item_id=401,
        resource_id=501,
        slot_date=date(2026, 9, 10),
        auto_slot_date=date(2026, 9, 10),
        slot_qty=Decimal("10"),
        capacity_load=Decimal("1"),
        planned_output_qty=Decimal("10"),
        accepted_plan_output_qty=Decimal("0"),
        assembly_remaining_qty=Decimal("10"),
        slot_ordinal=0,
        original_priority=["2026-09-10", 201, 301],
        readiness_phase="now",
    ))
    policy = db.get(models.ShelfPolicy, 601)
    if policy is None:
        policy = models.ShelfPolicy(
            id=601,
            item_id=401,
            warehouse_ref1c="r8-warehouse",
            replenishment_time_days=1,
            review_cycle_days=1,
            safety_days=1,
            batch_multiple=Decimal("1"),
            active=True,
        )
        db.add(policy)
        db.flush()
    db.add(models.ShelfProjection(
        ledger_generation_id=generation_id,
        shelf_policy_id=601,
        item_id=401,
        warehouse_ref1c="r8-warehouse",
        as_of_date=date(2026, 9, 10),
        protection_until=date(2026, 9, 11),
        target_qty=Decimal("10"),
        shelf_physical_qty=Decimal("2"),
        other_stock_qty=Decimal("0"),
        confirmed_open_production_qty=Decimal("0"),
        projected_qty=Decimal("2"),
        gap_qty=Decimal("8"),
        transfer_qty=Decimal("0"),
        unlaunched_mrp_qty=Decimal("0"),
        pull_qty=Decimal("8"),
        materialized_qty=Decimal("0"),
        demand_manifest=["r8"],
    ))
    db.flush()


def test_r8_generation_local_queue_ids_do_not_churn_current_readiness_or_drum(
    db_session,
):
    """Technical generation copies must not become current business changes."""
    from datetime import datetime, timezone
    from app.services.item_ledger.current_execution import publish_current_execution_from_generation

    cutoff = datetime(2026, 9, 10, tzinfo=timezone.utc)
    physical1 = models.PhysicalImportBatch(
        batch_key="r8-stable-queue-id-1", status="completed", cutoff=cutoff, source_watermarks={}
    )
    generation1 = models.LedgerGeneration(
        generation_key="r8-stable-queue-id-g1", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, source_watermarks={}, capabilities={},
        physical_import_batch=physical1, algorithm_version="tests/r8",
    )
    db_session.add_all([physical1, generation1])
    db_session.flush()
    _seed_generation_execution_rows(db_session, generation_id=int(generation1.id), queue_line_id=1001)
    publish_current_execution_from_generation(db_session, int(generation1.id))
    db_session.flush()
    first = {
        (row.entity_kind, row.business_identity): (int(row.id), row.updated_at)
        for row in db_session.query(models.CurrentExecutionRow).all()
    }
    first_changes = db_session.query(models.CurrentExecutionChange).count()

    physical2 = models.PhysicalImportBatch(
        batch_key="r8-stable-queue-id-2", status="completed", cutoff=cutoff, source_watermarks={}
    )
    generation2 = models.LedgerGeneration(
        generation_key="r8-stable-queue-id-g2", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, source_watermarks={}, capabilities={},
        physical_import_batch=physical2, algorithm_version="tests/r8",
    )
    db_session.add_all([physical2, generation2])
    db_session.flush()
    _seed_generation_execution_rows(db_session, generation_id=int(generation2.id), queue_line_id=2001)
    publish_current_execution_from_generation(db_session, int(generation2.id))
    db_session.flush()

    current = db_session.query(models.CurrentExecutionRow).all()
    assert {(row.entity_kind, row.business_identity): (int(row.id), row.updated_at) for row in current} == first
    assert db_session.query(models.CurrentExecutionChange).count() == first_changes
    readiness = next(row for row in current if row.entity_kind == "assembly_readiness")
    drum_slot = next(row for row in current if row.entity_kind == "drum_slot")
    excluded = next(row for row in current if row.entity_kind == "drum_excluded")
    queue = next(row for row in current if row.entity_kind == "assembly_queue")
    assert readiness.payload["queue_line_id"] == queue.id
    assert drum_slot.payload["queue_line_id"] == queue.id
    assert excluded.payload["queue_line_id"] == queue.id
    assert get_current_execution_scope(
        db_session, entity_kind="assembly_readiness", scope_key="assembly:all-live-plans"
    ).source_generation_id == generation2.id
    assert get_current_execution_scope(
        db_session, entity_kind="drum_slot", scope_key="drum:all-live-plans"
    ).source_generation_id == generation2.id


def _queue(identity: str, *, period: str, plan_id: int, line_id: int, qty: str = "1"):
    return {
        "entity_kind": "assembly_queue",
        "business_identity": identity,
        "scope_key": "assembly:all-live-plans",
        "payload": {
            "plan_id": plan_id,
            "plan_line_id": line_id,
            "period_from": period,
            "period_to": period,
            "assembly_remaining_qty": qty,
            "original_priority": [period, period, plan_id, line_id],
        },
    }


def test_r8_current_owner_is_stable_across_100_noop_recalculations(db_session):
    rows = [_queue("queue:1", period="2026-09-10", plan_id=1, line_id=1)]
    first = publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        rows=rows,
    )
    current = db_session.query(models.CurrentExecutionRow).one()
    row_id = int(current.id)
    updated_at = current.updated_at
    change_count = db_session.query(models.CurrentExecutionChange).count()

    for index in range(100):
        result = publish_current_execution_scope(
            db_session,
            source_revision=f"accepted:g{index + 2}",
            scope_key="assembly:all-live-plans",
            rows=rows,
        )
        assert result.idempotent is True

    current = db_session.query(models.CurrentExecutionRow).one()
    assert int(current.id) == row_id
    assert current.updated_at == updated_at
    assert db_session.query(models.CurrentExecutionRow).count() == 1
    assert db_session.query(models.CurrentExecutionChange).count() == change_count
    assert first.changed_rows == 1


def test_r8_queue_order_is_oldest_first_with_decimal_and_equal_date_tiebreak():
    rows = [
        _queue("queue:2", period="2026-09-10", plan_id=2, line_id=1, qty="1.000"),
        _queue("queue:1", period="2026-09-10", plan_id=1, line_id=9, qty="1.000"),
        _queue("queue:0", period="2026-09-09", plan_id=9, line_id=1, qty="0.001"),
    ]
    ordered = order_execution_queue(rows)
    assert [row["business_identity"] for row in ordered] == [
        "queue:0", "queue:1", "queue:2"
    ]
    assert all(Decimal(str(row["payload"]["assembly_remaining_qty"])) > 0 for row in ordered)


def test_r8_stale_or_not_ready_publication_fails_closed(db_session):
    with pytest.raises(CurrentExecutionUnavailable, match="ready"):
        publish_current_execution_scope(
            db_session,
            source_revision="accepted:g1",
            scope_key="assembly:all-live-plans",
            rows=[_queue("queue:1", period="2026-09-10", plan_id=1, line_id=1)],
            result_ready=False,
        )


def test_r8_complete_scope_cannot_clear_a_foreign_mrp_shelf(db_session):
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="shelf:mrp:1",
        rows=[{
            "entity_kind": "shelf_projection",
            "business_identity": "shelf:item:10:warehouse:W1",
            "scope_key": "shelf:mrp:1",
            "payload": {"item_id": 10, "gap_qty": "2"},
        }],
    )
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="shelf:mrp:2",
        rows=[{
            "entity_kind": "shelf_projection",
            "business_identity": "shelf:item:10:warehouse:W1",
            "scope_key": "shelf:mrp:2",
            "payload": {"item_id": 10, "gap_qty": "3"},
        }],
    )
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g2",
        scope_key="shelf:mrp:1",
        rows=[],
        complete_scope=True,
        entity_kinds=("shelf_projection",),
    )
    current = load_current_execution_rows(
        db_session, entity_kind="shelf_projection", scope_key="shelf:mrp:2"
    )
    assert [row.business_identity for row in current] == ["shelf:item:10:warehouse:W1"]
    assert current[0].scope_key == "shelf:mrp:2"
    assert current[0].payload["gap_qty"] == "3"


def test_r8_manual_input_is_part_of_current_business_state(db_session):
    row = _queue("queue:1", period="2026-09-10", plan_id=1, line_id=1)
    row["manual_input"] = {"slot_date": date(2026, 9, 14).isoformat(), "moved_by": "master"}
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        rows=[row],
    )
    current = load_current_execution_rows(db_session, entity_kind="assembly_queue")
    assert current[0].manual_input["moved_by"] == "master"


def test_r8_closure_hides_current_row_but_preserves_bounded_change_history(db_session):
    row = _queue("queue:closed", period="2026-09-10", plan_id=1, line_id=1)
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        rows=[row],
    )
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g2",
        scope_key="assembly:all-live-plans",
        rows=[],
        complete_scope=True,
        entity_kinds=("assembly_queue",),
    )
    assert load_current_execution_rows(db_session, entity_kind="assembly_queue") == []
    current = db_session.query(models.CurrentExecutionRow).one()
    assert current.result_status == "closed"
    assert db_session.query(models.CurrentExecutionChange).count() == 2


def test_r8_empty_readiness_scope_does_not_close_queue_scope(db_session):
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        entity_kinds=("assembly_queue",),
        rows=[_queue("queue:1", period="2026-09-10", plan_id=1, line_id=1)],
    )
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        entity_kinds=("assembly_readiness",),
        rows=[],
        complete_scope=True,
    )
    assert [row.business_identity for row in load_current_execution_rows(
        db_session, entity_kind="assembly_queue"
    )] == ["queue:1"]


def test_r8_manual_tile_survives_new_generation_queue_ids(db_session):
    identity = drum_slot_identity(77, 0)
    first = {
        "entity_kind": "drum_slot",
        "business_identity": identity,
        "scope_key": "drum:all-live-plans",
        "payload": {
            "plan_line_id": 77,
            "queue_line_id": 101,
            "slot_ordinal": 0,
            "resource_id": 5,
            "slot_date": "2026-09-10",
        },
        "manual_input": {
            "slot_date": "2026-09-12",
            "resource_id": 5,
            "moved_by": "master",
        },
    }
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="drum:all-live-plans",
        rows=[first],
        entity_kinds=("drum_slot",),
    )
    current_id = int(db_session.query(models.CurrentExecutionRow).one().id)
    second = {
        **first,
        "payload": {**first["payload"], "queue_line_id": 202, "slot_date": "2026-09-11"},
    }
    second.pop("manual_input")
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g2",
        scope_key="drum:all-live-plans",
        rows=[second],
        entity_kinds=("drum_slot",),
    )
    current = db_session.query(models.CurrentExecutionRow).one()
    assert int(current.id) == current_id
    assert current.payload["queue_line_id"] == 202
    assert current.payload["slot_date"] == "2026-09-11"
    assert current.manual_input["slot_date"] == "2026-09-12"


def test_r8_dependency_invalidation_makes_current_read_fail_closed(db_session):
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        rows=[_queue("queue:1", period="2026-09-10", plan_id=1, line_id=1)],
    )
    assert invalidate_current_execution_scope(
        db_session,
        entity_kind="assembly_queue",
        scope_key="assembly:all-live-plans",
        source_revision="dependency:capacity:v2",
        reason="capacity_changed",
    ) is True
    assert get_current_execution_scope(
        db_session,
        entity_kind="assembly_queue",
        scope_key="assembly:all-live-plans",
    ).result_ready is False
    assert load_current_execution_rows(db_session, entity_kind="assembly_queue") == []


def test_r8_scope_pointer_drift_fails_closed_even_when_manifest_is_ready(db_session):
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        rows=[_queue("queue:1", period="2026-09-10", plan_id=1, line_id=1)],
        entity_kinds=("assembly_queue",),
    )
    manifest = get_current_execution_scope(
        db_session,
        entity_kind="assembly_queue",
        scope_key="assembly:all-live-plans",
    )
    manifest.source_generation_id = 999999
    db_session.flush()
    with pytest.raises(CurrentExecutionUnavailable, match="not accepted"):
        require_current_execution_scope(
            db_session,
            entity_kind="assembly_queue",
            scope_key="assembly:all-live-plans",
        )


def test_r8_missing_manifest_fails_closed_instead_of_using_legacy_snapshot(db_session):
    with pytest.raises(CurrentExecutionUnavailable, match="manifest"):
        require_current_execution_scope(
            db_session,
            entity_kind="assembly_queue",
            scope_key="assembly:all-live-plans",
        )


def test_r8_manifest_without_accepted_generation_provenance_is_stale(db_session, building_ledger_generation):
    generation = building_ledger_generation
    generation.status = "accepted"
    generation.cutoff = generation.physical_import_batch.cutoff
    generation.accepted_at = generation.cutoff
    db_session.flush()
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        rows=[_queue("queue:1", period="2026-09-10", plan_id=1, line_id=1)],
        entity_kinds=("assembly_queue",),
    )
    with pytest.raises(CurrentExecutionUnavailable, match="stale"):
        require_current_execution_scope(
            db_session,
            entity_kind="assembly_queue",
            scope_key="assembly:all-live-plans",
        )


def test_r8_truth_pointer_move_without_atomic_scope_publication_is_stale(db_session, building_ledger_generation):
    generation = building_ledger_generation
    generation.status = "accepted"
    generation.cutoff = generation.physical_import_batch.cutoff
    generation.accepted_at = generation.cutoff
    db_session.flush()
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        source_generation_id=int(generation.id),
        scope_key="assembly:all-live-plans",
        rows=[_queue("queue:1", period="2026-09-10", plan_id=1, line_id=1)],
        entity_kinds=("assembly_queue",),
    )
    replacement = models.LedgerGeneration(
        generation_key="r8-unpublished-pointer-target",
        status="accepted",
        cutoff=generation.cutoff,
        accepted_at=generation.accepted_at,
        source_watermarks={},
        capabilities={},
        physical_import_batch_id=generation.physical_import_batch_id,
        algorithm_version="tests/r8",
    )
    db_session.add(replacement)
    db_session.flush()
    truth_pointer = db_session.get(models.PlanningTruthState, 1)
    truth_pointer.current_generation_id = int(replacement.id)
    # The contract is about an accepted truth pointer moving independently of
    # the already-published scope.  Keep the two persisted values explicit so
    # a reader regression cannot pass by silently observing the same target.
    manifest = get_current_execution_scope(
        db_session,
        entity_kind="assembly_queue",
        scope_key="assembly:all-live-plans",
    )
    assert truth_pointer.current_generation_id == int(replacement.id)
    assert manifest is not None
    assert manifest.source_generation_id == int(generation.id)
    with pytest.raises(CurrentExecutionUnavailable, match="stale"):
        require_current_execution_scope(
            db_session,
            entity_kind="assembly_queue",
            scope_key="assembly:all-live-plans",
        )


def test_r8_scope_manifest_persists_read_summary_for_current_get(db_session):
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        scope_key="assembly:all-live-plans",
        rows=[_queue("queue:1", period="2026-09-10", plan_id=1, line_id=1, qty="2")],
        entity_kinds=("assembly_queue",),
        summary={"total_rows": 1, "total_queue_qty": "2"},
    )
    manifest = get_current_execution_scope(
        db_session,
        entity_kind="assembly_queue",
        scope_key="assembly:all-live-plans",
    )
    assert manifest.summary == {"total_rows": 1, "total_queue_qty": "2"}

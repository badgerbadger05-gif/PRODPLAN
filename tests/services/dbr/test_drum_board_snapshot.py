"""Ledger-bound DBR drum board candidate/read contract."""

from datetime import date, datetime, timezone

import pytest

from app import models
from app.services import planning_truth
from app.services.dbr import drum_board_snapshot


def _world(db, *, exact=True):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key=f"drum-board-{exact}", status="completed", cutoff=cutoff,
        source_watermarks={}, completed_at=cutoff,
    )
    generation = models.LedgerGeneration(
        generation_key=f"drum-board-{exact}", status="building", cutoff=cutoff,
        physical_import_batch=batch, source_watermarks={}, capabilities={}, algorithm_version="tests",
    )
    resource = models.ProductionResource(resource_name="Assembly", capacity=1)
    item = models.Item(item_code="DRUM-1", item_name="Original display name")
    db.add_all([batch, generation, resource, item]); db.flush()
    run = models.PlanningRun(
        status="BUILDING_SNAPSHOT", ledger_generation_id=generation.id,
        ledger_cutoff=cutoff, config_snapshot={}, active_freeze_version=3,
        fixed_at=cutoff, finished_at=cutoff, pinned=True,
    )
    db.add(run); db.flush()
    schedule = models.DbrDrumSchedule(
        ledger_generation_id=generation.id if exact else generation.id + 999,
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31), status="active",
        config_snapshot={"calendar_fallback": False},
    )
    db.add(schedule); db.flush()
    db.add(models.DbrDrumScheduleProgram(
        schedule_id=schedule.id, program_id=1, source_run_id=run.run_id,
        ledger_generation_id=generation.id, freeze_version=3,
    ))
    db.add(models.DbrDrumSlot(
        schedule_id=schedule.id, slot_date=date(2026, 8, 4), planned_date=date(2026, 8, 3),
        resource_id=resource.resource_id, item_id=item.item_id, qty=7, produced_qty=5,
        kit_status="green", release_status="pending", source_run_id=run.run_id,
        ledger_generation_id=generation.id, freeze_version=3, position=2,
    ))
    db.add(models.DbrDrumCapacityGap(
        schedule_id=schedule.id, gap_date=date(2026, 8, 5), resource_id=resource.resource_id,
        item_id=item.item_id, required_qty=9, takt_qty=7, gap_qty=2,
    ))
    db.flush()
    return generation, schedule, run, item


def _accept(db, generation):
    db.query(models.PlanningRun).filter_by(ledger_generation_id=generation.id).update({
        "status": "FIXED_SNAPSHOT",
    })
    generation.status = "accepted"
    generation.accepted_at = generation.cutoff
    generation.capabilities = {
        planning_truth.CAPABILITY_PHYSICAL_LEDGER: True,
        planning_truth.CAPABILITY_RESERVATION_REPLAY: True,
        planning_truth.CAPABILITY_PLANNING_SNAPSHOTS: True,
        planning_truth.CAPABILITY_DBR_DRUM_BOARD: True,
    }
    planning_truth.publish_generation(db, generation)
    snapshot = db.query(models.PlanningReadSnapshot).filter_by(
        consumer=drum_board_snapshot.CONSUMER, ledger_generation_id=generation.id,
    ).one()
    snapshot.truth_status = "accepted"
    snapshot.reason = None
    snapshot.published_at = generation.cutoff
    db.flush()


def test_candidate_captures_plan_fields_but_never_legacy_gate_or_produced_facts(db_session):
    generation, _schedule, _run, item = _world(db_session)
    snapshot = drum_board_snapshot.build_drum_board_candidate_snapshot(db_session, generation.id)
    assert snapshot is not None
    payload_before = snapshot.payload
    assert payload_before["slots"][0]["item_name"] == "Original display name"
    assert payload_before["slots"][0]["produced_qty"] is None
    assert payload_before["slots"][0]["kit_status"] == "unknown"
    assert payload_before["kpi"]["fact_qty"] is None
    assert payload_before["kpi"]["green"] is None

    slot = db_session.query(models.DbrDrumSlot).one()
    item.item_name = "Mutated master"
    slot.produced_qty = 999
    slot.kit_status = "red"
    _accept(db_session, generation)
    read = drum_board_snapshot.read_drum_board_snapshot(db_session)
    assert read["slots"][0]["item_name"] == "Original display name"
    assert read["slots"][0]["produced_qty"] is None
    assert read["slots"][0]["kit_status"] == "unknown"
    assert read["kpi"]["fact_qty"] is None


def test_candidate_returns_none_when_no_active_exact_target_schedule(db_session):
    generation, schedule, _run, _item = _world(db_session)
    schedule.status = "superseded"
    db_session.flush()
    assert drum_board_snapshot.build_drum_board_candidate_snapshot(db_session, generation.id) is None
    assert db_session.query(models.PlanningReadSnapshot).count() == 0


def test_candidate_rejects_slot_mixed_or_stale_lineage(db_session):
    generation, _schedule, _run, _item = _world(db_session)
    slot = db_session.query(models.DbrDrumSlot).one()
    slot.freeze_version = 99
    db_session.flush()
    with pytest.raises(drum_board_snapshot.DbrDrumBoardCandidateError, match="foreign or stale"):
        drum_board_snapshot.build_drum_board_candidate_snapshot(db_session, generation.id)

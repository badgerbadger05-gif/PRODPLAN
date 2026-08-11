from datetime import date, datetime, timezone
from calendar import monthrange
from decimal import Decimal
from hashlib import sha256
import json

import pytest
from sqlalchemy import text

from app import models
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C
from app.services.item_ledger.candidate_realization_replay import (
    CandidateRealizationReplayError,
    replay_candidate_realizations,
)
from app.services.item_ledger.assembly_output_persistence import (
    materialize_assembly_output_allocations,
)
from app.services.item_ledger.assembly_queue_snapshot import (
    build_assembly_queue_snapshot,
)
from app.services.item_ledger.obligation_generation import (
    carry_forward_retained_reservations,
)


def _seal(
    target,
    entries,
    parent_id=1,
    replay_from="2026-07-01T00:00:00+00:00",
):
    manifest = {
        "version": 1,
        "entries": entries,
        "add_request": {"plan_ids": [], "horizon_days": None, "config_version_id": None, "config_snapshot": {}},
    }
    target.source_watermarks = {
        "generation_kind": "obligation_refresh",
        "parent_generation_id": parent_id,
        "replay_from": replay_from,
        "obligation_refresh_manifest": manifest,
        "obligation_refresh_manifest_hash": sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def _world(
    db,
    *,
    candidate_periods=None,
    fact_rows=None,
    parent_replay_from="2026-07-01T00:00:00+00:00",
):
    cutoff = datetime(2026, 7, 31, 23, 59)
    physical = models.PhysicalImportBatch(
        batch_key="candidate-replay-physical", status="completed", cutoff=cutoff,
        source_watermarks={},
    )
    parent = models.LedgerGeneration(
        generation_key="candidate-replay-parent", status="accepted", cutoff=cutoff,
        source_watermarks={"replay_from": parent_replay_from}, capabilities={},
        physical_import_batch=physical,
        algorithm_version="tests", accepted_at=cutoff,
    )
    target = models.LedgerGeneration(
        generation_key="candidate-replay-target", status="building", cutoff=cutoff,
        source_watermarks={}, capabilities={}, physical_import_batch=physical,
        algorithm_version="tests", replay_version="tests",
    )
    item = models.Item(item_code="CAND-REPLAY", item_name="Candidate replay")
    db.add_all([
        physical,
        parent,
        target,
        item,
    ]); db.flush()
    existing_wh = db.query(models.StockWarehouse).filter_by(warehouse_ref1c="WH").one_or_none()
    if existing_wh is None:
        db.add(models.StockWarehouse(
            warehouse_ref1c="WH",
            warehouse_name="Outside planning contour",
            is_selected=False,
            is_finished_goods=False,
        ))
    else:
        existing_wh.is_selected = False
        existing_wh.is_finished_goods = False
        if not str(existing_wh.warehouse_name or "").strip():
            existing_wh.warehouse_name = "Outside planning contour"
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))

    candidate_runs = []
    parent_reservations = []
    for n, period in enumerate(
        tuple(candidate_periods)
        if candidate_periods is not None
        else (date(2026, 7, 10), date(2026, 7, 20)),
        start=1,
    ):
        period_to = date(period.year, period.month, monthrange(period.year, period.month)[1])
        plan = models.ProductionPlanHeader(
            name=f"candidate {n}", period_from=period, period_to=period_to, status="fixed",
        )
        db.add(plan)
        db.flush()
        old = models.PlanningRun(
            status="FIXED_SNAPSHOT", ledger_generation_id=parent.id, source_plan_id=None,
            period_from=plan.period_from, period_to=plan.period_to, config_snapshot={},
        )
        run = models.PlanningRun(
            status="BUILDING_SNAPSHOT", ledger_generation_id=target.id,
            source_plan_id=plan.id, period_from=plan.period_from, period_to=plan.period_to,
            config_snapshot={},
        )
        db.add_all([old, run]); db.flush()
        requirement = models.MrpRequirement(
            run_id=run.run_id, item_id=item.item_id, total_required_qty=Decimal("5"),
            net_required_qty=Decimal("5"),
            period_from=period, period_to=period_to, bom_level=0,
        )
        parent_requirement = models.MrpRequirement(
            run_id=old.run_id, item_id=item.item_id, total_required_qty=Decimal("5"),
            net_required_qty=Decimal("5"),
            period_from=period, period_to=period_to, bom_level=0,
        )
        db.add_all([requirement, parent_requirement]); db.flush()
        db.add(models.ReservationEntry(
            ledger_generation_id=target.id, item_id=item.item_id, characteristic_ref="", organization_ref="",
            planning_stock_pool="selected", run_id=run.run_id, freeze_version=1,
            requirement_id=requirement.id, priority_period_from=period, priority_period_to=period_to,
            realization_mode="make", reserved_qty=Decimal("5"), realized_qty=0, lifecycle_status="active",
        ))
        old_reservation = models.ReservationEntry(
            ledger_generation_id=parent.id, item_id=item.item_id, characteristic_ref="", organization_ref="",
            planning_stock_pool="selected", run_id=old.run_id, freeze_version=1,
            requirement_id=parent_requirement.id, priority_period_from=period, priority_period_to=period_to,
            realization_mode="make", reserved_qty=Decimal("5"), realized_qty=0, lifecycle_status="active",
        )
        db.add(old_reservation)
        candidate_runs.append((run, plan, old))
        parent_reservations.append(old_reservation)
    db.flush()
    _seal(target, [
        {"action": "add", "plan_id": plan.id, "parent_run_id": None, "candidate_run_id": run.run_id}
        for run, plan, old in candidate_runs
    ], parent_id=parent.id, replay_from=parent_replay_from)
    # A pre-period fact and two candidate-period facts.  All are visible through
    # the shared immutable physical prefix; the adapter owns the lower bound.
    for index, (at, qty) in enumerate(
        tuple(fact_rows)
        if fact_rows is not None
        else (
            (datetime(2026, 7, 5), "5"),
            (datetime(2026, 7, 12), "5"),
            (datetime(2026, 7, 21), "5"),
        ),
        start=1,
    ):
        db.add(models.StockLedgerEntry(
            ingest_batch_id=physical.id, source_content_hash=f"candidate-replay-{index}", item_id=item.item_id,
            characteristic_ref="", organization_ref=DEFAULT_ORGANIZATION_REF1C, warehouse_ref1c="WH",
            qty=Decimal(qty), qty_after=Decimal(qty),
            posting_at=at, record_type="Receipt", movement_kind="assembly_in", recorder_type="Production",
            recorder_ref=f"REC-{index}", line_no="1", ingest_source="pull", active=True,
        ))
    db.flush()
    return parent, target, candidate_runs, parent_reservations


def test_candidate_replay_uses_replay_from_boundary_from_accepted_lineage(db_session):
    parent, target, candidates, parent_reservations = _world(db_session)
    calls = {"commit": 0, "rollback": 0}
    db_session.commit = lambda: calls.__setitem__("commit", calls["commit"] + 1)
    db_session.rollback = lambda: calls.__setitem__("rollback", calls["rollback"] + 1)

    result = replay_candidate_realizations(db_session, target.id)
    db_session.flush()

    assert calls == {"commit": 0, "rollback": 0}
    assert result["replay_from"] == "2026-07-01T00:00:00+00:00"
    assert Decimal(result["allocated_qty"]) == Decimal("10")
    assert result["excluded_pre_replay_facts"] == 0
    target_reservations = db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=target.id
    ).order_by(models.ReservationEntry.priority_period_from).all()
    assert [row.realized_qty for row in target_reservations] == [Decimal("5"), Decimal("5")]
    assert [row.realized_qty for row in parent_reservations] == [Decimal("0"), Decimal("0")]
    assert db_session.query(models.ReservationEvent).filter_by(ledger_generation_id=parent.id).count() == 0
    batch = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=target.id, stage="reservation_replay"
    ).one()
    assert batch.status == "completed"


def test_candidate_replay_includes_july_fact_for_september_target_and_closes_both_fifo(db_session):
    parent, target, candidates, parent_reservations = _world(
        db_session,
        candidate_periods=(date(2026, 9, 10), date(2026, 9, 20)),
        fact_rows=((datetime(2026, 7, 5, 10, 0), "10"),),
    )

    result = replay_candidate_realizations(db_session, target.id)
    db_session.flush()

    assert result["replay_from"] == "2026-07-01T00:00:00+00:00"
    assert Decimal(result["allocated_qty"]) == Decimal("10")
    assert result["excluded_pre_replay_facts"] == 0
    target_reservations = db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=target.id
    ).order_by(models.ReservationEntry.priority_period_from).all()
    assert [row.realized_qty for row in target_reservations] == [Decimal("5"), Decimal("5")]
    assert [row.realized_qty for row in parent_reservations] == [Decimal("0"), Decimal("0")]
    batch = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=target.id, stage="reservation_replay"
    ).one()
    assert batch.status == "completed"


def test_candidate_replay_retry_is_idempotent(db_session):
    _parent, target, _candidates, _parent_reservations = _world(db_session)
    first = replay_candidate_realizations(db_session, target.id)
    before = [(row.id, row.realized_delta) for row in db_session.query(models.ReservationEvent).order_by(models.ReservationEvent.id)]
    second = replay_candidate_realizations(db_session, target.id)
    after = [(row.id, row.realized_delta) for row in db_session.query(models.ReservationEvent).order_by(models.ReservationEvent.id)]

    assert second["events_inserted"] == 0
    assert first["allocation_checksum"] == second["allocation_checksum"]
    assert after == before


def test_retained_and_candidate_replay_partition_one_sle_and_keep_open_output(
    db_session,
):
    parent, target, candidates, _parent_reservations = _world(
        db_session,
        candidate_periods=(date(2026, 9, 10),),
        fact_rows=((datetime(2026, 7, 5, 10, 0), "5"),),
    )
    candidate, candidate_plan, old_candidate = candidates[0]
    item = db_session.query(models.Item).filter_by(item_code="CAND-REPLAY").one()
    sle = db_session.query(models.StockLedgerEntry).one()
    sle.source_content_hash = sha256(b"candidate-replay-assembly-output").hexdigest()
    # The physical output may only satisfy plans that were already fixed when
    # it was posted.  Keep this fixture's intended retained-first FIFO scenario
    # without reintroducing the old unbounded historical allocation.
    candidate_plan.fixed_at = datetime(2026, 7, 1, tzinfo=timezone.utc)

    retained_plan = models.ProductionPlanHeader(
        name="retained august",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="fixed",
        fixed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    db_session.add(retained_plan)
    db_session.flush()
    retained_line = models.ProductionPlanLine(
        plan_id=retained_plan.id,
        item_id=item.item_id,
        bucket_date=date(2026, 8, 1),
        qty=Decimal("10"),
    )
    candidate_line = models.ProductionPlanLine(
        plan_id=candidate_plan.id,
        item_id=item.item_id,
        bucket_date=candidate_plan.period_from,
        qty=Decimal("10"),
    )
    retained_run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        ledger_generation_id=parent.id,
        source_plan_id=retained_plan.id,
        period_from=retained_plan.period_from,
        period_to=retained_plan.period_to,
        config_snapshot={},
    )
    db_session.add_all([retained_line, candidate_line, retained_run])
    db_session.flush()
    retained_requirement = models.MrpRequirement(
        run_id=retained_run.run_id,
        item_id=item.item_id,
        total_required_qty=Decimal("5"),
        net_required_qty=Decimal("5"),
        period_from=retained_plan.period_from,
        period_to=retained_plan.period_to,
        bom_level=0,
    )
    db_session.add(retained_requirement)
    db_session.flush()
    retained_reservation = models.ReservationEntry(
        ledger_generation_id=parent.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="selected",
        run_id=retained_run.run_id,
        freeze_version=1,
        requirement_id=retained_requirement.id,
        priority_period_from=retained_plan.period_from,
        priority_period_to=retained_plan.period_to,
        realization_mode="make",
        reserved_qty=Decimal("5"),
        realized_qty=Decimal("5"),
        replenishment_required_qty=Decimal("5"),
        replenishment_received_qty=Decimal("5"),
        lifecycle_status="active",
    )
    db_session.add(retained_reservation)
    db_session.flush()
    db_session.add(models.ReservationEvent(
        ledger_generation_id=parent.id,
        reservation_id=retained_reservation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="selected",
        event_kind="realize",
        reserved_delta=Decimal("5"),
        realized_delta=Decimal("5"),
        sle_id=sle.id,
        fact_ref=sle.recorder_ref,
        fact_line_ref=sle.line_no,
        match_rule="fifo",
        cycle_id=f"historical-replay:g{parent.id}",
        idempotency_key=f"parent:sle{sle.id}:r{retained_reservation.id}",
        event_at=sle.posting_at,
    ))
    db_session.flush()
    _seal(
        target,
        [
            {
                "action": "retain",
                "plan_id": retained_plan.id,
                "parent_run_id": retained_run.run_id,
                "candidate_run_id": None,
            },
            {
                "action": "add",
                "plan_id": candidate_plan.id,
                "parent_run_id": None,
                "candidate_run_id": candidate.run_id,
            },
        ],
        parent_id=parent.id,
    )
    carry_forward_retained_reservations(
        db_session,
        parent_generation_id=parent.id,
        target_generation_id=target.id,
        retained_run_ids=(retained_run.run_id,),
    )

    first = replay_candidate_realizations(db_session, target.id)
    second = replay_candidate_realizations(db_session, target.id)

    target_events = db_session.query(models.ReservationEvent).filter(
        models.ReservationEvent.ledger_generation_id == target.id,
        models.ReservationEvent.sle_id == sle.id,
    ).all()
    assert Decimal(first["allocated_qty"]) == Decimal("5")
    assert second["events_inserted"] == 0
    assert len(target_events) == 1
    assert sum((row.realized_delta for row in target_events), Decimal("0")) == Decimal("5")

    materialize_assembly_output_allocations(db_session, target.id)
    output_allocations = db_session.query(models.AssemblyOutputAllocation).filter_by(
        ledger_generation_id=target.id,
        stock_ledger_entry_id=sle.id,
    ).all()
    assert sum((row.allocated_qty for row in output_allocations), Decimal("0")) == Decimal("5")
    queue = build_assembly_queue_snapshot(db_session, target.id)
    assert queue.payload["total_rows"] == 2
    assert queue.payload["total_queue_qty"] == 15.0


def test_candidate_replay_rejects_empty_manifest_and_cross_generation_reservation(db_session):
    parent, target, candidates, _parent_reservations = _world(db_session)
    _seal(target, [], parent_id=parent.id)
    with pytest.raises(CandidateRealizationReplayError, match="must have entries"):
        replay_candidate_realizations(db_session, target.id)

    _seal(target, [
        {"action": "add", "plan_id": plan.id, "parent_run_id": None, "candidate_run_id": run.run_id}
        for run, plan, old in candidates
    ], parent_id=parent.id)
    rogue = db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=target.id
    ).first()
    rogue.run_id = candidates[0][2].run_id
    with pytest.raises(CandidateRealizationReplayError, match="unsealed run"):
        replay_candidate_realizations(db_session, target.id)


def test_candidate_replay_obeys_outer_rollback(db_session):
    _parent, target, _candidates, _parent_reservations = _world(db_session)
    db_session.commit()
    outer = db_session.begin()
    db_session.execute(text("UPDATE ledger_generation SET id = id WHERE id = :id"), {"id": target.id})
    replay_candidate_realizations(db_session, target.id)
    outer.rollback()
    assert db_session.query(models.ReservationEvent).count() == 0
    assert db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=target.id, stage="reservation_replay"
    ).count() == 0

from datetime import date, datetime, timezone

import pytest

from app import models
from app.services.item_ledger.historical_obligations import (
    HistoricalObligationAmbiguity,
    materialize_historical_obligations,
    select_historical_obligation_runs,
)


CUTOFF = datetime(2026, 7, 23, tzinfo=timezone.utc)


def _generation(db, *, status="building"):
    batch = models.PhysicalImportBatch(
        batch_key=f"obligation-{status}",
        status="completed",
        cutoff=CUTOFF,
        source_watermarks={},
        completed_at=CUTOFF,
    )
    generation = models.LedgerGeneration(
        generation_key=f"obligation-{status}",
        status=status,
        cutoff=CUTOFF,
        source_watermarks={},
        capabilities={},
        physical_import_batch=batch,
        algorithm_version="test/1",
        accepted_at=CUTOFF if status == "accepted" else None,
    )
    db.add(generation)
    db.flush()
    return generation


def _plan(db, *, status="fixed", start=date(2026, 6, 1), end=date(2026, 6, 30)):
    plan = models.ProductionPlanHeader(
        name=f"{status}-{start}",
        period_from=start,
        period_to=end,
        status=status,
        fixed_at=CUTOFF if status in {"fixed", "archived"} else None,
    )
    db.add(plan)
    db.flush()
    return plan


def _run(db, plan, *, status="FIXED_SNAPSHOT", prior=None, period=None):
    period = period or (plan.period_from, plan.period_to)
    run = models.PlanningRun(
        status=status,
        config_snapshot={},
        source_plan_id=plan.id,
        period_from=period[0],
        period_to=period[1],
        fixed_at=CUTOFF,
        prior_run_id=prior.run_id if prior else None,
    )
    db.add(run)
    db.flush()
    return run


def _requirement(db, run, *, status="open", gross=10, net=8):
    item = models.Item(
        item_code=f"I-{run.run_id}-{status}",
        item_name="Item",
        replenishment_method="Покупка",
    )
    db.add(item)
    db.flush()
    req = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=gross,
        net_required_qty=net,
        covered_qty=0,
        remaining_qty=net,
        period_from=run.period_from,
        period_to=run.period_to,
        bom_level=1,
        freeze_version=3,
        status=status,
    )
    db.add(req)
    db.flush()
    db.add(
        models.MrpRequirementBucket(
            requirement_id=req.id,
            run_id=run.run_id,
            item_id=item.item_id,
            bucket_date=run.period_to,
            gross_qty=gross,
            net_qty=net,
        )
    )
    db.flush()
    return req


def test_archived_fixed_plan_and_closed_requirement_are_materialized(db_session):
    generation = _generation(db_session)
    plan = _plan(db_session, status="archived")
    run = _run(db_session, plan, status="CLOSED")
    req = _requirement(db_session, run, status="closed")

    result = materialize_historical_obligations(db_session, generation.id)

    entry = db_session.query(models.ReservationEntry).one()
    assert result["selected_run_ids"] == [run.run_id]
    assert entry.requirement_id == req.id
    assert float(entry.reserved_qty) == 10
    assert float(entry.realized_qty) == 0


def test_draft_source_less_and_nonterminal_runs_are_excluded(db_session):
    draft = _plan(db_session, status="draft")
    _run(db_session, draft)
    fixed = _plan(db_session)
    for status in ("PENDING", "RUNNING", "FAILED"):
        _run(db_session, fixed, status=status)
    db_session.add(
        models.PlanningRun(
            status="FIXED_SNAPSHOT",
            config_snapshot={},
            source_plan_id=None,
            period_from=date(2026, 6, 1),
            period_to=date(2026, 6, 30),
            fixed_at=CUTOFF,
        )
    )
    db_session.flush()

    assert select_historical_obligation_runs(db_session, CUTOFF) == []


def test_latest_fixed_snapshot_is_canonical_duplicate(db_session):
    plan = _plan(db_session)
    old = _run(db_session, plan)
    latest = _run(db_session, plan, prior=old)

    selected = select_historical_obligation_runs(db_session, CUTOFF)

    assert [row.run_id for row in selected] == [latest.run_id]


def test_inconsistent_run_period_fails_closed(db_session):
    plan = _plan(db_session)
    _run(
        db_session,
        plan,
        period=(date(2026, 5, 1), date(2026, 5, 31)),
    )

    with pytest.raises(HistoricalObligationAmbiguity):
        select_historical_obligation_runs(db_session, CUTOFF)


def test_accepted_generation_is_immutable(db_session):
    generation = _generation(db_session, status="accepted")

    with pytest.raises(ValueError, match="BUILDING"):
        materialize_historical_obligations(db_session, generation.id)

    assert db_session.query(models.ReservationEntry).count() == 0


def test_rerun_is_idempotent_with_completed_manifest(db_session):
    generation = _generation(db_session)
    run = _run(db_session, _plan(db_session))
    _requirement(db_session, run)

    first = materialize_historical_obligations(db_session, generation.id)
    second = materialize_historical_obligations(db_session, generation.id)

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert first["batch_id"] == second["batch_id"]
    assert db_session.query(models.ReservationEntry).count() == 1
    assert db_session.query(models.ReservationEvent).count() == 1
    assert db_session.query(models.LedgerBuildBatch).count() == 1
    assert db_session.query(models.LedgerBuildBatch).one().stage == (
        "reservation_materialize"
    )


def test_previous_period_outstanding_is_not_dropped(db_session):
    generation = _generation(db_session)
    old_plan = _plan(
        db_session,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
    )
    run = _run(db_session, old_plan)
    req = _requirement(db_session, run, status="closed", gross=17, net=17)

    materialize_historical_obligations(db_session, generation.id)

    entry = db_session.query(models.ReservationEntry).filter_by(
        requirement_id=req.id
    ).one()
    assert float(entry.reserved_qty) == 17
    assert entry.lifecycle_status == "active"


def test_future_period_fixed_by_cutoff_is_included(db_session):
    generation = _generation(db_session)
    future_plan = _plan(
        db_session,
        start=date(2026, 9, 1),
        end=date(2026, 9, 30),
    )
    run = _run(db_session, future_plan)
    req = _requirement(db_session, run, gross=23, net=23)

    result = materialize_historical_obligations(db_session, generation.id)

    assert result["selected_run_ids"] == [run.run_id]
    assert db_session.query(models.ReservationEntry).filter_by(
        requirement_id=req.id
    ).count() == 1


def test_plan_fixed_after_cutoff_is_excluded(db_session):
    plan = _plan(db_session)
    plan.fixed_at = datetime(2026, 7, 24, tzinfo=timezone.utc)
    run = _run(db_session, plan)
    run.fixed_at = plan.fixed_at
    _requirement(db_session, run)
    db_session.flush()

    assert select_historical_obligation_runs(db_session, CUTOFF) == []

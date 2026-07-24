from datetime import date, datetime, timedelta, timezone

import pytest

from app import models
from app.services.item_ledger.reservation import BUY, CONSUME
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


def _requirement_with_method(
    db,
    run,
    *,
    status="open",
    gross=10,
    net=8,
    replenishment_method="Покупка",
):
    item = models.Item(
        item_code=f"I-{run.run_id}-{status}-M",
        item_name="Item",
        replenishment_method=replenishment_method,
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
    db.add(models.MrpRequirementBucket(
        requirement_id=req.id,
        run_id=run.run_id,
        item_id=item.item_id,
        bucket_date=run.period_to,
        gross_qty=gross,
        net_qty=net,
    ))
    db.flush()
    return req


def _set_bucket_date(req: models.MrpRequirement, bucket_date: date) -> models.MrpRequirementBucket:
    bucket = req.buckets[0]
    bucket.bucket_date = bucket_date
    return bucket


def _entry_by_mode(
    db, req: models.MrpRequirement, mode: str
) -> models.ReservationEntry | None:
    return (
        db.query(models.ReservationEntry)
        .filter_by(requirement_id=int(req.id), realization_mode=mode)
        .one_or_none()
    )


def _assert_purchased_contract_entry_sizes(
    db, req: models.MrpRequirement, *, gross: float, net: float
) -> None:
    consume_entry = _entry_by_mode(db, req, CONSUME)
    buy_entry = _entry_by_mode(db, req, BUY)
    assert consume_entry is not None
    assert buy_entry is not None
    assert float(consume_entry.reserved_qty) == float(gross)
    assert float(buy_entry.reserved_qty) == float(net)
    assert consume_entry.lifecycle_status == "active"
    assert buy_entry.lifecycle_status == "active"


def test_archived_fixed_plan_and_closed_requirement_are_materialized(db_session):
    generation = _generation(db_session)
    plan = _plan(db_session, status="archived")
    run = _run(db_session, plan, status="CLOSED")
    req = _requirement(db_session, run, status="closed")

    result = materialize_historical_obligations(db_session, generation.id)

    assert result["selected_run_ids"] == [run.run_id]
    consume_entry = _entry_by_mode(db_session, req, CONSUME)
    buy_entry = _entry_by_mode(db_session, req, BUY)
    assert consume_entry is not None
    assert buy_entry is not None
    assert consume_entry.requirement_id == req.id
    assert buy_entry.requirement_id == req.id
    assert float(consume_entry.reserved_qty) == 10
    assert float(buy_entry.reserved_qty) == 8
    assert float(consume_entry.realized_qty) == 0
    assert float(buy_entry.realized_qty) == 0


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
    assert db_session.query(models.ReservationEntry).count() == 2
    assert db_session.query(models.ReservationEvent).count() == 2
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

    _assert_purchased_contract_entry_sizes(db_session, req, gross=17, net=17)


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
    _assert_purchased_contract_entry_sizes(db_session, req, gross=23, net=23)


def test_plan_fixed_after_cutoff_is_excluded(db_session):
    plan = _plan(db_session)
    plan.fixed_at = datetime(2026, 7, 24, tzinfo=timezone.utc)
    run = _run(db_session, plan)
    run.fixed_at = plan.fixed_at
    _requirement(db_session, run)
    db_session.flush()

    assert select_historical_obligation_runs(db_session, CUTOFF) == []


def test_legacy_bucket_outside_requirement_period_is_materialized(db_session):
    generation = _generation(db_session)
    plan = _plan(db_session)
    run = _run(db_session, plan)
    req = _requirement(db_session, run, gross=12, net=12)
    legacy_date = run.period_from - timedelta(days=1)
    _set_bucket_date(req, legacy_date)

    result = materialize_historical_obligations(db_session, generation.id)
    batch = db_session.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.id == result["batch_id"]
    ).one()

    assert legacy_date.isoformat() in batch.metrics["selected_bucket_dates"]
    assert batch.metrics["legacy_out_of_period_bucket_ids"] == [
        int(req.buckets[0].id)
    ]
    assert batch.metrics["legacy_out_of_period_bucket_dates"] == [
        legacy_date.isoformat()
    ]
    assert result["selected_bucket_ids"] == [int(req.buckets[0].id)]
    assert db_session.query(models.ReservationEntry).filter_by(
        requirement_id=req.id
    ).count() == 2


def test_legacy_net_mismatch_is_audited_for_all_requirements(db_session):
    generation = _generation(db_session)
    plan = _plan(
        db_session,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )
    run = _run(db_session, plan)
    req_make = _requirement_with_method(
        db_session,
        run,
        gross=10,
        net=4,
        replenishment_method="Производство",
    )
    req_consume = _requirement_with_method(
        db_session,
        run,
        gross=8,
        net=3,
        status="open-consume",
        replenishment_method="Покупка",
    )
    req_make.buckets[0].net_qty = 7
    req_consume.buckets[0].net_qty = 5
    db_session.flush()

    result = materialize_historical_obligations(db_session, generation.id)
    batch = db_session.query(models.LedgerBuildBatch).filter_by(
        id=result["batch_id"]
    ).one()
    details = batch.metrics["legacy_net_phasing_mismatch_details"]
    detail_ids = [row["requirement_id"] for row in details]

    assert batch.metrics["legacy_net_phasing_mismatch_count"] == 2
    assert set(batch.metrics["legacy_net_phasing_requirement_ids"]) == {
        int(req_make.id),
        int(req_consume.id),
    }
    assert detail_ids == batch.metrics["legacy_net_phasing_requirement_ids"]
    assert {
        (row["requirement_id"], row["requirement_net"], row["bucket_net"])
        for row in details
    } == {
        (int(req_make.id), "4", "7"),
        (int(req_consume.id), "3", "5"),
    }


def test_gross_bucket_mismatch_still_fails(db_session):
    generation = _generation(db_session)
    plan = _plan(
        db_session,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )
    run = _run(db_session, plan)
    req = _requirement_with_method(
        db_session,
        run,
        gross=10,
        net=10,
        replenishment_method="Производство",
    )
    db_session.query(models.MrpRequirementBucket).filter(
        models.MrpRequirementBucket.requirement_id == req.id
    ).update({models.MrpRequirementBucket.gross_qty: 9})
    db_session.flush()

    with pytest.raises(HistoricalObligationAmbiguity):
        materialize_historical_obligations(db_session, generation.id)


def test_historical_obligation_manifest_preserves_bucket_dates(db_session):
    generation = _generation(db_session)
    plan = _plan(db_session, start=date(2026, 6, 1), end=date(2026, 6, 30))
    run = _run(db_session, plan)
    req_in = _requirement(db_session, run, gross=5, net=5)
    req_out = _requirement(db_session, run, status="closed", gross=3, net=3)
    _set_bucket_date(req_out, date(2026, 5, 15))

    result = materialize_historical_obligations(db_session, generation.id)
    _assert_purchased_contract_entry_sizes(db_session, req_in, gross=5, net=5)
    _assert_purchased_contract_entry_sizes(db_session, req_out, gross=3, net=3)

    batch = db_session.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.id == result["batch_id"]
    ).one()

    assert batch.metrics["selected_bucket_dates"] == [
        req_in.buckets[0].bucket_date.isoformat(),
        req_out.buckets[0].bucket_date.isoformat(),
    ]
    assert batch.metrics["legacy_out_of_period_bucket_ids"] == [
        int(req_out.buckets[0].id)
    ]
    assert batch.metrics["legacy_out_of_period_bucket_dates"] == [
        req_out.buckets[0].bucket_date.isoformat()
    ]

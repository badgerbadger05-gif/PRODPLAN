"""``retire`` must be a first-class manifest action for every consumer.

The manifest emits ``action="retire"`` (it is how «Закрыть план» retires a plan's
obligation), but three readers only knew ``retain``/``refresh``/``add`` and died
on ``int(None)`` while parsing the entry:

* :func:`obligation_refresh_manifest._existing_result` — the re-read that any
  retry/resume of an interrupted plan closure performs first;
* :func:`mrp_freeze.freeze_candidate_snapshots`;
* :func:`mrp_result_snapshot._require_sealed_candidate_manifest`.

The last two are reached whenever one build both adds and retires a plan.
"""

from datetime import date, datetime as dt, timezone
from decimal import Decimal

import pytest

from app import models
from app.services import obligation_refresh_orchestrator as workflow
from app.services.obligation_refresh_manifest import (
    ObligationRefreshManifestError,
    create_obligation_refresh_manifest,
)
from app.services.period_plan_service import (
    _close_refresh_generation_key,
    close_fixed_plan,
    fix_period_plan,
)


CUTOFF = dt(2026, 7, 23, 12, tzinfo=timezone.utc)


def _accepted_world(db):
    """One accepted generation + one purchased item, the shared starting truth."""
    physical = models.PhysicalImportBatch(
        batch_key="retire-physical", status="completed", cutoff=CUTOFF,
        source_watermarks={"opening_at": "2025-01-01T00:00:00+00:00"},
        completed_at=CUTOFF,
    )
    accepted = models.LedgerGeneration(
        generation_key="retire-accepted", status="accepted", cutoff=CUTOFF,
        source_watermarks={"replay_from": "2026-07-01T00:00:00+00:00"},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
        physical_import_batch=physical, algorithm_version="test", accepted_at=CUTOFF,
    )
    item = models.Item(
        item_code="RETIRE-BUY", item_name="покупная деталь",
        replenishment_method="Покупка", replenishment_time=3, status="active",
    )
    db.add_all([physical, accepted, item])
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=accepted.id))
    resource = models.ProductionResource(
        resource_name="Retire assembly",
        planning_range=30,
        capacity=Decimal("100"),
    )
    db.add(resource)
    db.flush()
    db.add(models.AssemblyRate(
        resource_id=int(resource.resource_id),
        item_id=int(item.item_id),
        qty_per_capacity=Decimal("1"),
    ))
    db.commit()
    return accepted, item


def _draft_plan(db, item, *, name, period_from=date(2026, 8, 1), qty="5"):
    plan = models.ProductionPlanHeader(
        name=name, status="draft",
        period_from=period_from, period_to=date(2026, 8, 31),
    )
    db.add(plan)
    db.flush()
    db.add(models.ProductionPlanLine(
        plan_id=plan.id, item_id=item.item_id,
        bucket_date=period_from, qty=Decimal(qty),
    ))
    db.commit()
    return plan


def _parent_run(db, accepted, plan):
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT", ledger_generation_id=accepted.id,
        source_plan_id=plan.id, period_from=plan.period_from,
        period_to=plan.period_to, config_snapshot={}, horizon_days=30,
        started_at=CUTOFF, fixed_at=CUTOFF, finished_at=CUTOFF,
        pinned=True, active_freeze_version=1,
    )
    db.add(run)
    db.commit()
    return run


# ---------------------------------------------------------------------------
# (a) the sealed manifest can be re-read — resume of an interrupted closure
# ---------------------------------------------------------------------------

def test_sealed_retire_manifest_is_readable_on_retry(db_session):
    accepted, item = _accepted_world(db_session)
    plan = _draft_plan(db_session, item, name="retire me")
    plan.status = "fixed"
    db_session.commit()
    _parent_run(db_session, accepted, plan)
    target = models.LedgerGeneration(
        generation_key="retire-target", status="building", cutoff=CUTOFF,
        source_watermarks={
            "generation_kind": "obligation_refresh",
            "parent_generation_id": accepted.id,
        },
        capabilities={},
        physical_import_batch_id=accepted.physical_import_batch_id,
        algorithm_version="test",
    )
    db_session.add(target)
    db_session.commit()

    options = dict(
        started_by="retire-worker", horizon_days=30, config_version_id=None,
        config_snapshot={}, retire_plan_ids=(plan.id,),
    )
    first = create_obligation_refresh_manifest(
        db_session, accepted.id, target.id, (), **options
    )
    db_session.commit()

    assert first.created is True
    assert [entry["action"] for entry in first.entries] == ["retire"]

    # Same key, same request: the sealed set must be returned, not re-parsed
    # into "candidate identity is malformed".
    second = create_obligation_refresh_manifest(
        db_session, accepted.id, target.id, (), **options
    )
    assert second.created is False
    assert second.content_hash == first.content_hash
    assert second.entries == first.entries

    # …and a retry that changes the retire request is still refused.
    with pytest.raises(ObligationRefreshManifestError, match="conflicting retry"):
        create_obligation_refresh_manifest(
            db_session, accepted.id, target.id, (),
            **{**options, "retire_plan_ids": ()},
        )


# ---------------------------------------------------------------------------
# (b) «Закрыть план» publishes the retire, and the manifest re-read that a
#     resume performs no longer dies on the sealed retire entry
# ---------------------------------------------------------------------------

def test_close_fixed_plan_publishes_the_retire_and_reseals_its_manifest(db_session):
    accepted, item = _accepted_world(db_session)
    plan = _draft_plan(db_session, item, name="close me")
    fix_period_plan(db_session, plan.id, fixed_by="tester")
    db_session.commit()
    run = db_session.query(models.PlanningRun).filter_by(
        source_plan_id=plan.id, status="FIXED_SNAPSHOT",
    ).one()
    parent_generation_id = int(
        db_session.query(models.PlanningTruthState.current_generation_id).scalar()
    )
    key = _close_refresh_generation_key(
        run_id=int(run.run_id), parent_generation_id=parent_generation_id,
    )

    result = close_fixed_plan(db_session, int(run.run_id))

    assert result["status"] == "closed"
    db_session.expire_all()
    target = db_session.query(models.LedgerGeneration).filter_by(
        generation_key=key
    ).one()
    assert str(db_session.get(models.PlanningRun, run.run_id).status) == "CLOSED"
    assert str(db_session.get(models.ProductionPlanHeader, plan.id).status) == "closed"
    assert int(
        db_session.query(models.PlanningTruthState.current_generation_id).scalar()
    ) == int(target.id)
    # The sealed manifest that any resume of this key has to re-read carries the
    # retire entry — the exact shape all three consumers used to choke on.
    entries = dict(target.source_watermarks)["obligation_refresh_manifest"]["entries"]
    assert [entry["action"] for entry in entries] == ["retire"]
    assert entries[0]["parent_run_id"] == int(run.run_id)
    assert entries[0]["candidate_run_id"] is None


# ---------------------------------------------------------------------------
# (в) retire together with add — the freeze and the MRP snapshot see the entry
# ---------------------------------------------------------------------------

def test_refresh_can_retire_one_plan_while_adding_another(db_session):
    accepted, item = _accepted_world(db_session)
    retired_plan = _draft_plan(db_session, item, name="retired")
    retired_plan.status = "fixed"
    db_session.commit()
    retired_run = _parent_run(db_session, accepted, retired_plan)
    added_plan = _draft_plan(db_session, item, name="added", qty="7")
    added_plan.status = "fixed"
    db_session.commit()

    result = workflow.run_obligation_refresh(
        db_session,
        parent_generation_id=accepted.id,
        generation_key="retire-and-add",
        add_plan_ids=(added_plan.id,),
        retire_plan_ids=(retired_plan.id,),
        started_by="test",
        horizon_days=30,
        config_snapshot={},
        accepted_at=dt(2026, 7, 24, tzinfo=timezone.utc),
    )

    assert result.published is True
    db_session.expire_all()
    assert str(db_session.get(models.PlanningRun, retired_run.run_id).status) == "CLOSED"
    assert str(db_session.get(models.ProductionPlanHeader, retired_plan.id).status) == "closed"
    added_run = db_session.query(models.PlanningRun).filter_by(
        source_plan_id=added_plan.id, status="FIXED_SNAPSHOT",
    ).one()
    assert int(added_run.ledger_generation_id) == int(result.target_generation_id)
    # The MRP result snapshot of the added candidate was persisted despite the
    # retire entry sharing the sealed manifest.
    assert db_session.query(models.PlanningReadSnapshot).filter_by(
        ledger_generation_id=int(result.target_generation_id),
        consumer="mrp_result",
    ).count() == 1

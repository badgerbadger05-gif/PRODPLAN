"""Period-plan API/service contract for immutable Ledger snapshot publication."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import models
from app.services import period_plan_service as service


def _truth_and_plan(db):
    cutoff = datetime(2026, 7, 23, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="period-refresh-physical",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    generation = models.LedgerGeneration(
        generation_key="period-refresh-parent",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        algorithm_version="test",
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=physical,
    )
    item = models.Item(item_code="PERIOD-REFRESH", item_name="period refresh")
    db.add_all([physical, generation, item])
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    plan = models.ProductionPlanHeader(
        name="August", status="fixed",
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
    )
    db.add(plan)
    db.flush()
    db.add(models.ProductionPlanLine(
        plan_id=plan.id, item_id=item.item_id,
        bucket_date=plan.period_from, qty=10,
    ))
    db.commit()
    return generation, plan


def test_snapshot_requires_explicit_generation_key(db_session):
    _generation, plan = _truth_and_plan(db_session)
    with pytest.raises(ValueError, match="generation_key is required"):
        service.create_mrp_snapshot_from_period_plan(
            db_session, plan.id, generation_key=""
        )


def test_snapshot_uses_current_truth_and_returns_published_plan_candidate(
    db_session, monkeypatch
):
    generation, plan = _truth_and_plan(db_session)
    published = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        ledger_generation_id=generation.id,
        config_snapshot={},
        started_at=datetime.now(timezone.utc),
        pinned=True,
    )
    db_session.add(published)
    db_session.flush()
    observed = {}

    def fake_refresh(db, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(
            target_generation_id=generation.id,
            candidate_run_ids=(published.run_id,),
            published=True,
        )

    monkeypatch.setattr(
        "app.services.obligation_refresh_orchestrator.run_obligation_refresh",
        fake_refresh,
    )
    result = service.create_mrp_snapshot_from_period_plan(
        db_session,
        plan.id,
        generation_key="period-refresh-1",
        started_by="test",
    )

    assert observed == {}
    assert result == {
        "status": "ok",
        "generation_key": "period-refresh-1",
        "ledger_generation_id": generation.id,
        "run_id": published.run_id,
        "published": False,
        "immutable": True,
    }


def test_run_list_and_delete_guard_use_only_exact_current_published_truth(db_session):
    current, plan = _truth_and_plan(db_session)
    old = models.LedgerGeneration(
        generation_key="period-refresh-old",
        status="accepted",
        cutoff=current.cutoff,
        accepted_at=current.accepted_at,
        algorithm_version="test",
        source_watermarks={},
        capabilities={},
        physical_import_batch_id=current.physical_import_batch_id,
    )
    db_session.add(old)
    db_session.flush()
    current_run = models.PlanningRun(
        status="FIXED_SNAPSHOT", source_plan_id=plan.id,
        ledger_generation_id=current.id, config_snapshot={}, pinned=True,
    )
    stale_run = models.PlanningRun(
        status="SUPERSEDED", source_plan_id=plan.id,
        ledger_generation_id=old.id, config_snapshot={}, pinned=True,
    )
    legacy_success = models.PlanningRun(
        status="SUCCESS", source_plan_id=plan.id,
        ledger_generation_id=current.id, config_snapshot={},
    )
    db_session.add_all([current_run, stale_run, legacy_success])
    db_session.flush()
    db_session.add(models.PlanningLivePointer(plan_id=plan.id, run_id=current_run.run_id))
    db_session.commit()

    assert [row["run_id"] for row in service.list_mrp_runs_for_plan(
        db_session, plan.id
    )["rows"]] == [current_run.run_id]
    with pytest.raises(ValueError, match="зафиксированных расчётов"):
        service.delete_period_plan(db_session, plan.id)


def test_run_list_keeps_a_run_inherited_through_a_physical_refresh(db_session):
    """A fact-only fork inherits obligations instead of re-anchoring them.

    Resolving the list by ``ledger_generation_id == accepted`` returned an empty
    run list for every plan on the live contour, because the hourly refresh had
    moved the pointer far past the generation that froze the runs.
    """
    anchor, plan = _truth_and_plan(db_session)
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT", source_plan_id=plan.id,
        ledger_generation_id=anchor.id, ledger_cutoff=anchor.cutoff,
        config_snapshot={}, pinned=True,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(models.PlanningLivePointer(plan_id=plan.id, run_id=run.run_id))
    child = models.LedgerGeneration(
        generation_key="period-refresh-fact-fork",
        status="accepted",
        cutoff=datetime(2026, 7, 24, tzinfo=timezone.utc),
        accepted_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        algorithm_version="test",
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": int(anchor.id),
        },
        capabilities={"physical_ledger": True},
        physical_import_batch_id=anchor.physical_import_batch_id,
    )
    db_session.add(child)
    db_session.flush()
    db_session.get(models.PlanningTruthState, 1).current_generation_id = int(child.id)
    db_session.commit()

    assert [row["run_id"] for row in service.list_mrp_runs_for_plan(
        db_session, plan.id
    )["rows"]] == [run.run_id]


def test_delete_guard_also_preserves_historical_snapshot_lineage(db_session):
    current, plan = _truth_and_plan(db_session)
    historical = models.LedgerGeneration(
        generation_key="period-refresh-history-only",
        status="accepted",
        cutoff=current.cutoff,
        accepted_at=current.accepted_at,
        algorithm_version="test",
        source_watermarks={},
        capabilities={},
        physical_import_batch_id=current.physical_import_batch_id,
    )
    db_session.add(historical)
    db_session.flush()
    db_session.add(models.PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        ledger_generation_id=historical.id,
        config_snapshot={},
        pinned=True,
    ))
    db_session.commit()

    with pytest.raises(ValueError, match="зафиксированных расчётов"):
        service.delete_period_plan(db_session, plan.id)


def _fact_only_fork(db, parent, *, key="period-refresh-fact-fork"):
    """A physical refresh: new accepted pointer, obligations untouched."""
    cutoff = parent.cutoff + timedelta(days=1)
    batch = models.PhysicalImportBatch(
        batch_key=f"{key}-physical",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    child = models.LedgerGeneration(
        generation_key=key,
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        algorithm_version="test",
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": int(parent.id),
        },
        capabilities={"physical_ledger": True},
        physical_import_batch=batch,
    )
    db.add_all([batch, child])
    db.flush()
    db.get(models.PlanningTruthState, 1).current_generation_id = child.id
    db.flush()
    return child


def _fixed_run_with_stale_owner_stamp(db, generation, plan):
    """A live fixed run whose owners still carry the publication generation.

    That is the normal steady state after the compact-owner cutover: the owner
    rows keep the generation of the publication that wrote them while the
    accepted pointer moves on with every physical refresh.
    """
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        config_snapshot={},
        started_at=datetime.now(timezone.utc),
        pinned=True,
    )
    db.add(run)
    db.flush()
    item = db.query(models.Item).filter_by(item_code="PERIOD-REFRESH").one()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=10,
        net_required_qty=10,
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=0,
    )
    db.add(requirement)
    db.flush()
    db.add(models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        run_id=run.run_id,
        freeze_version=1,
        requirement_id=requirement.id,
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
        realization_mode="buy",
        lifecycle_status="active",
        owner_kind="current",
        is_current=True,
        current_identity=f"reservation:req:{requirement.id}:mode:buy",
    ))
    db.flush()
    return run


def test_mrp_snapshot_is_idempotent_when_the_pointer_moved_past_the_owner_stamp(
    db_session, monkeypatch
):
    """Recalculating MRP must not 400 after a physical refresh.

    The snapshot gate decided "this plan has a current snapshot" from
    ``reservation_entry.ledger_generation_id == pointer``, while the refresh
    manifest decides it from the sealed lineage.  Once the pointer advanced
    past the owner stamp the two disagreed: the gate forked a refresh and the
    manifest refused it with "add plan already has current FIXED_SNAPSHOT".
    """
    generation, plan = _truth_and_plan(db_session)
    published = _fixed_run_with_stale_owner_stamp(db_session, generation, plan)
    child = _fact_only_fork(db_session, generation)
    db_session.commit()

    def _must_not_fork(db, **kwargs):
        raise AssertionError(
            "a live snapshot was mistaken for a missing one and forked a refresh"
        )

    monkeypatch.setattr(
        "app.services.obligation_refresh_orchestrator.run_obligation_refresh",
        _must_not_fork,
    )

    result = service.create_mrp_snapshot_from_period_plan(
        db_session,
        plan.id,
        generation_key="period-refresh-after-fact-fork",
        started_by="test",
    )

    assert result["immutable"] is True
    assert result["published"] is False
    assert result["run_id"] == published.run_id
    assert result["ledger_generation_id"] == child.id


def test_snapshot_gate_and_refresh_manifest_name_the_same_live_run(db_session):
    """One entity, one formula: both gates read the sealed live scope."""
    from app.services.obligation_refresh_manifest import _current_parents
    from app.services.planning_run_candidate import _resolve_parent_generation_id

    generation, plan = _truth_and_plan(db_session)
    published = _fixed_run_with_stale_owner_stamp(db_session, generation, plan)
    child = _fact_only_fork(db_session, generation)
    db_session.commit()

    # The snapshot gate's view...
    assert _resolve_parent_generation_id(db_session, published) == int(child.id)
    # ...and the refresh manifest's view.
    assert {
        int(row.run_id) for row in _current_parents(db_session, int(child.id))
    } == {int(published.run_id)}

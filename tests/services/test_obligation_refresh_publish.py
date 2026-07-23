from datetime import date, datetime, timezone

import pytest

from app import models
from app.services.obligation_refresh_publish import (
    ObligationRefreshPublishError,
    publish_obligation_refresh_batch,
)
from app.services.planning_run_candidate import create_candidate_run


def _generation(db, *, key, status, cutoff, watermarks=None):
    physical = models.PhysicalImportBatch(
        batch_key=f"physical:{key}", status="completed", cutoff=cutoff,
        source_watermarks={}, completed_at=cutoff,
    )
    row = models.LedgerGeneration(
        generation_key=key, status=status, cutoff=cutoff,
        source_watermarks=watermarks or {}, capabilities={},
        physical_import_batch=physical, algorithm_version="tests/1",
        accepted_at=cutoff if status == "accepted" else None,
    )
    db.add(row)
    db.flush()
    return row


def _capabilities():
    return {"ledger": "complete", "mrp": "snapshot"}


def _seal_build(db, target, candidates, cutoff):
    target.capabilities = _capabilities()
    for stage in ("physical_import", "reservation_materialize", "reservation_replay", "snapshot_build"):
        metrics = {}
        if stage == "snapshot_build":
            metrics = {
                "candidate_run_ids": [row.run_id for row in candidates],
                "future_supply_captured": True,
            }
        db.add(models.LedgerBuildBatch(
            ledger_generation_id=target.id, stage=stage, batch_key=f"{target.id}:{stage}",
            status="completed", algorithm_version="tests/1", metrics=metrics,
            completed_at=cutoff,
        ))
    db.flush()


def _batch(db, count=2):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    parent_generation = _generation(db, key="accepted", status="accepted", cutoff=cutoff)
    db.add(models.PlanningTruthState(id=1, current_generation_id=parent_generation.id))
    db.flush()
    parents = []
    for index in range(count):
        plan = models.ProductionPlanHeader(
            name=f"source {index}", period_from=date(2026, 7, 1), period_to=date(2026, 7, 31),
        )
        db.add(plan)
        db.flush()
        parent = models.PlanningRun(
            status="FIXED_SNAPSHOT", ledger_generation_id=parent_generation.id,
            source_plan_id=plan.id, period_from=plan.period_from, period_to=plan.period_to,
            horizon_days=90, config_snapshot={"source": index}, pinned=True,
            fixed_at=cutoff, finished_at=cutoff,
        )
        db.add(parent)
        db.flush()
        parents.append(parent)
    target = _generation(
        db, key="refresh", status="building", cutoff=cutoff,
        watermarks={"generation_kind": "obligation_refresh", "parent_generation_id": parent_generation.id},
    )
    # An obligation refresh reuses the accepted immutable physical prefix.
    target.physical_import_batch_id = parent_generation.physical_import_batch_id
    db.flush()
    candidates = [create_candidate_run(db, row.run_id, target.id, "test") for row in parents]
    _seal_build(db, target, candidates, cutoff)
    db.commit()
    return cutoff, parent_generation, target, parents, candidates


def _publish(db, parent, target, cutoff):
    return publish_obligation_refresh_batch(
        db, parent_generation_id=parent.id, target_generation_id=target.id,
        accepted_at=cutoff, capabilities=_capabilities(),
    )


def test_publish_requires_candidate_for_every_active_source_plan(db_session):
    cutoff, parent, target, _parents, candidates = _batch(db_session)
    db_session.delete(candidates[-1])
    db_session.flush()

    with pytest.raises(ObligationRefreshPublishError, match="complete candidate batch"):
        _publish(db_session, parent, target, cutoff)

    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id
    assert db_session.get(models.LedgerGeneration, target.id).status == "building"


def test_publish_is_atomic_under_caller_rollback(db_session):
    cutoff, parent, target, parents, candidates = _batch(db_session)

    result = _publish(db_session, parent, target, cutoff)
    assert result.published is True
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == target.id
    assert [row.status for row in parents] == ["SUPERSEDED", "SUPERSEDED"]
    assert [row.status for row in candidates] == ["FIXED_SNAPSHOT", "FIXED_SNAPSHOT"]

    db_session.rollback()
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id
    assert db_session.get(models.LedgerGeneration, target.id).status == "building"
    assert [db_session.get(models.PlanningRun, row.run_id).status for row in parents] == [
        "FIXED_SNAPSHOT", "FIXED_SNAPSHOT"
    ]
    assert [db_session.get(models.PlanningRun, row.run_id).status for row in candidates] == [
        "BUILDING_SNAPSHOT", "BUILDING_SNAPSHOT"
    ]


def test_publish_exact_retry_is_noop_but_mixed_state_is_rejected(db_session):
    cutoff, parent, target, _parents, _candidates = _batch(db_session)
    first = _publish(db_session, parent, target, cutoff)
    db_session.commit()

    retry = _publish(db_session, parent, target, cutoff)
    assert retry.published is False
    assert retry.parent_run_ids == first.parent_run_ids
    assert retry.candidate_run_ids == first.candidate_run_ids

    target.capabilities = {"ledger": "different"}
    db_session.flush()
    with pytest.raises(ObligationRefreshPublishError, match="mixed or partial"):
        _publish(db_session, parent, target, cutoff)


def test_publish_rejects_candidate_with_external_export_link(db_session):
    cutoff, parent, target, _parents, candidates = _batch(db_session)
    order = models.ProductionOrder(
        order_number="candidate-export", order_date=cutoff,
        order_ref1c="candidate-export-ref", source="mrp", source_run_id=candidates[0].run_id,
    )
    db_session.add(order)
    db_session.flush()

    with pytest.raises(ObligationRefreshPublishError, match="external export"):
        _publish(db_session, parent, target, cutoff)


@pytest.mark.parametrize("mutation, error", [
    ("empty_capabilities", "pre-sealed"),
    ("partial_checkpoint", "reservation_replay"),
    ("missing_manifest", "candidate manifest"),
])
def test_publish_requires_sealed_complete_build(db_session, mutation, error):
    cutoff, parent, target, _parents, _candidates = _batch(db_session)
    if mutation == "empty_capabilities":
        target.capabilities = {}
    elif mutation == "partial_checkpoint":
        row = db_session.query(models.LedgerBuildBatch).filter_by(
            ledger_generation_id=target.id, stage="reservation_replay"
        ).one()
        row.status = "building"
        row.completed_at = None
    else:
        row = db_session.query(models.LedgerBuildBatch).filter_by(
            ledger_generation_id=target.id, stage="snapshot_build"
        ).one()
        row.metrics = {"future_supply_captured": True}
    db_session.flush()

    with pytest.raises(ObligationRefreshPublishError, match=error):
        _publish(db_session, parent, target, cutoff)


def test_exact_retry_allows_legitimate_export_after_publication(db_session):
    cutoff, parent, target, _parents, candidates = _batch(db_session)
    _publish(db_session, parent, target, cutoff)
    db_session.commit()
    db_session.add(models.ProductionOrder(
        order_number="post-publish", order_date=cutoff, order_ref1c="post-publish-ref",
        source="mrp", source_run_id=candidates[0].run_id,
    ))
    db_session.commit()

    assert _publish(db_session, parent, target, cutoff).published is False

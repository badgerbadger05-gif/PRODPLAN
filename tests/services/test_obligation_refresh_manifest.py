from datetime import date, datetime, timezone

import pytest

from app import models
from app.services.obligation_refresh_manifest import (
    MANIFEST_HASH_KEY,
    MANIFEST_KEY,
    ObligationRefreshManifestError,
    create_obligation_refresh_manifest,
)


def _generation(db, key, status, cutoff, watermarks=None, physical=None):
    physical = physical or models.PhysicalImportBatch(
        batch_key=f"physical:{key}", status="completed", cutoff=cutoff,
        source_watermarks={}, completed_at=cutoff,
    )
    row = models.LedgerGeneration(
        generation_key=key, status=status, cutoff=cutoff,
        source_watermarks=watermarks or {}, capabilities={},
        physical_import_batch=physical, algorithm_version="test/1",
        accepted_at=cutoff if status == "accepted" else None,
    )
    db.add(row)
    db.flush()
    return row


def _fixture(db, plans=2):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    accepted = _generation(db, "accepted-manifest", "accepted", cutoff)
    db.add(models.PlanningTruthState(id=1, current_generation_id=accepted.id))
    parents = []
    for index in range(plans):
        plan = models.ProductionPlanHeader(
            name=f"plan {index}", period_from=date(2026, 7, 1),
            period_to=date(2026, 7, 31), status="fixed",
        )
        db.add(plan)
        db.flush()
        run = models.PlanningRun(
            status="FIXED_SNAPSHOT", ledger_generation_id=accepted.id,
            source_plan_id=plan.id, period_from=plan.period_from, period_to=plan.period_to,
            horizon_days=30, config_snapshot={"parent": index}, pinned=True,
            fixed_at=cutoff, finished_at=cutoff,
        )
        db.add(run)
        parents.append((plan, run))
    db.flush()
    target = _generation(
        db, "target-manifest", "building", cutoff,
        {"generation_kind": "obligation_refresh", "parent_generation_id": accepted.id},
        physical=accepted.physical_import_batch,
    )
    db.commit()
    return accepted, target, parents


def _create(db, accepted, target, add_ids=(), **overrides):
    options = {
        "started_by": "manifest-worker", "horizon_days": 45,
        "config_version_id": None, "config_snapshot": {"sealed": 1},
    }
    options.update(overrides)
    return create_obligation_refresh_manifest(
        db, accepted.id, target.id, add_ids, **options,
    )


def test_manifest_refreshes_every_current_plan_and_adds_fixed_plan(db_session):
    accepted, target, parents = _fixture(db_session)
    added = models.ProductionPlanHeader(
        name="new fixed", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31), status="fixed",
    )
    db_session.add(added)
    db_session.commit()

    result = _create(db_session, accepted, target, [added.id])

    assert result.created is True
    assert [(row["action"], row["plan_id"], row["parent_run_id"])
            for row in result.entries] == [
        ("refresh", parents[0][0].id, parents[0][1].run_id),
        ("refresh", parents[1][0].id, parents[1][1].run_id),
        ("add", added.id, None),
    ]
    manifest = target.source_watermarks[MANIFEST_KEY]
    assert target.source_watermarks[MANIFEST_HASH_KEY] == result.content_hash
    assert manifest["add_request"]["plan_ids"] == [added.id]
    assert {row.source_plan_id for row in db_session.query(models.PlanningRun).filter_by(
        ledger_generation_id=target.id, status="BUILDING_SNAPSHOT"
    )} == {parents[0][0].id, parents[1][0].id, added.id}


def test_manifest_allows_first_add_only_and_exact_retry(db_session):
    accepted, target, _parents = _fixture(db_session, plans=0)
    plan = models.ProductionPlanHeader(
        name="first", period_from=date(2026, 7, 1), period_to=date(2026, 7, 31), status="fixed",
    )
    db_session.add(plan)
    db_session.commit()
    first = _create(db_session, accepted, target, [plan.id])
    db_session.commit()

    again = _create(db_session, accepted, target, [plan.id])
    assert again.created is False
    assert again.content_hash == first.content_hash
    assert again.entries == first.entries


def test_manifest_never_omits_current_plan_and_conflicting_retry_is_rejected(db_session):
    accepted, target, parents = _fixture(db_session)
    result = _create(db_session, accepted, target)
    refreshed = {row["plan_id"] for row in result.entries if row["action"] == "refresh"}
    assert refreshed == {plan.id for plan, _run in parents}
    db_session.commit()

    with pytest.raises(ObligationRefreshManifestError, match="conflicting retry"):
        _create(db_session, accepted, target, horizon_days=99)


def test_manifest_conflicting_add_and_outer_rollback(db_session):
    accepted, target, parents = _fixture(db_session, plans=1)
    with pytest.raises(ObligationRefreshManifestError, match="already has current"):
        _create(db_session, accepted, target, [parents[0][0].id])

    plan = models.ProductionPlanHeader(
        name="rollback", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31), status="fixed",
    )
    db_session.add(plan)
    db_session.flush()
    result = _create(db_session, accepted, target, [plan.id])
    run_ids = [row["candidate_run_id"] for row in result.entries]
    db_session.rollback()

    assert all(db_session.get(models.PlanningRun, run_id) is None for run_id in run_ids)
    restored = db_session.get(models.LedgerGeneration, target.id)
    assert MANIFEST_KEY not in restored.source_watermarks


def test_manifest_retry_rejects_tampered_candidate_set(db_session):
    accepted, target, _parents = _fixture(db_session, plans=1)
    result = _create(db_session, accepted, target)
    db_session.commit()
    candidate = db_session.get(
        models.PlanningRun, int(result.entries[0]["candidate_run_id"])
    )
    candidate.prior_run_id = None
    db_session.flush()

    with pytest.raises(
        ObligationRefreshManifestError, match="candidate lineage conflicts"
    ):
        _create(db_session, accepted, target)

from datetime import datetime, timedelta, timezone

import pytest

from app import models
from app.services import planning_truth


def _generation(key: str, *, cutoff_hour: int = 9) -> models.LedgerGeneration:
    cutoff = datetime(2026, 7, 23, cutoff_hour, 0, tzinfo=timezone.utc)
    return models.LedgerGeneration(
        generation_key=key,
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={"source": key},
        capabilities={planning_truth.CAPABILITY_PLANNING_SNAPSHOTS: True},
        physical_import_batch=models.PhysicalImportBatch(
            batch_key=f"batch-{key}", status="completed", cutoff=cutoff,
            source_watermarks={},
        ),
        algorithm_version="truth-invalidation-test/1",
    )


def _published_truth(db):
    generation = _generation("accepted-one")
    planning_truth.publish_generation(db, generation)
    snapshot = planning_truth.publish_read_snapshot(
        db,
        consumer="truth-invalidation-test",
        snapshot_key="saved:v1",
        payload={"rows": [{"id": 1, "qty": 7}], "meta": {"frozen": True}},
        required_capabilities=(planning_truth.CAPABILITY_PLANNING_SNAPSHOTS,),
        reason="immutable accepted read",
    )
    db.commit()
    return generation, snapshot


@pytest.mark.parametrize("target_status", ["stale", "rejected"])
def test_invalidation_keeps_pointer_and_all_reads_fail_closed(db_session, target_status):
    generation, snapshot = _published_truth(db_session)
    original_payload = {"rows": [{"id": 1, "qty": 7}], "meta": {"frozen": True}}
    original_cutoff = generation.cutoff
    reason = f"operator invalidated as {target_status}"

    state = planning_truth.invalidate_current_generation(
        db_session,
        expected_generation_id=generation.id,
        status=target_status,
        reason=reason,
    )
    db_session.commit()

    pointer = db_session.get(models.PlanningTruthState, 1)
    assert pointer.current_generation_id == generation.id
    assert state.truth_status == target_status
    assert state.ready is False
    assert state.ledger_generation == generation.id
    assert state.cutoff == original_cutoff
    assert state.reason == reason
    current = planning_truth.get_truth_state(db_session)
    assert (current.ledger_generation, current.cutoff, current.reason) == (
        generation.id, original_cutoff, reason,
    )
    with pytest.raises(planning_truth.PlanningTruthUnavailable) as required:
        planning_truth.require_accepted_truth(db_session, "mutation-consumer")
    assert required.value.as_dict()["ledger_generation"] == generation.id
    assert required.value.as_dict()["cutoff"] == original_cutoff
    assert required.value.as_dict()["reason"] == reason
    with pytest.raises(planning_truth.PlanningTruthUnavailable) as latest:
        planning_truth.get_latest_read_snapshot(
            db_session, consumer="truth-invalidation-test", snapshot_key="saved:v1",
        )
    assert latest.value.as_dict()["ledger_generation"] == generation.id

    with pytest.raises(planning_truth.PlanningTruthUnavailable):
        planning_truth.publish_read_snapshot(
            db_session,
            consumer="truth-invalidation-test",
            snapshot_key="forbidden-after-invalidation",
            payload={"rows": []},
        )
    db_session.rollback()
    persisted = db_session.get(models.PlanningReadSnapshot, snapshot.id)
    assert persisted.payload == original_payload
    assert persisted.truth_status == "accepted"
    assert db_session.query(models.PlanningReadSnapshot).count() == 1


def test_exact_repeat_is_idempotent_but_conflicting_repeat_is_rejected(db_session):
    generation, _ = _published_truth(db_session)
    first = planning_truth.invalidate_current_generation(
        db_session, expected_generation_id=generation.id,
        status="stale", reason="source watermark regressed",
    )
    repeat = planning_truth.invalidate_current_generation(
        db_session, expected_generation_id=generation.id,
        status="stale", reason="source watermark regressed",
    )
    assert first == repeat
    with pytest.raises(planning_truth.PlanningTruthInvalidationConflict):
        planning_truth.invalidate_current_generation(
            db_session, expected_generation_id=generation.id,
            status="rejected", reason="different decision",
        )
    with pytest.raises(planning_truth.PlanningTruthInvalidationConflict):
        planning_truth.invalidate_current_generation(
            db_session, expected_generation_id=generation.id,
            status="stale", reason="different reason",
        )


@pytest.mark.parametrize("status", ["accepted", "building", "", "unknown"])
def test_invalidation_rejects_unsupported_status(db_session, status):
    generation, _ = _published_truth(db_session)
    with pytest.raises(ValueError, match="stale or rejected"):
        planning_truth.invalidate_current_generation(
            db_session, expected_generation_id=generation.id,
            status=status, reason="valid reason",
        )


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_invalidation_requires_nonblank_reason(db_session, reason):
    generation, _ = _published_truth(db_session)
    with pytest.raises(ValueError, match="nonblank"):
        planning_truth.invalidate_current_generation(
            db_session, expected_generation_id=generation.id,
            status="stale", reason=reason,
        )


def test_invalidation_rejects_wrong_target_and_nonaccepted_current(db_session):
    generation, _ = _published_truth(db_session)
    with pytest.raises(planning_truth.PlanningTruthInvalidationConflict, match="expected"):
        planning_truth.invalidate_current_generation(
            db_session, expected_generation_id=generation.id + 100,
            status="stale", reason="wrong target",
        )
    generation.status = "building"
    db_session.commit()
    with pytest.raises(planning_truth.PlanningTruthInvalidationConflict, match="not accepted"):
        planning_truth.invalidate_current_generation(
            db_session, expected_generation_id=generation.id,
            status="stale", reason="cannot invalidate build",
        )


def test_later_accepted_publication_replaces_invalidated_pointer(db_session):
    generation, _ = _published_truth(db_session)
    planning_truth.invalidate_current_generation(
        db_session, expected_generation_id=generation.id,
        status="rejected", reason="bad replay evidence",
    )
    replacement = _generation("accepted-two", cutoff_hour=10)
    state = planning_truth.publish_generation(db_session, replacement)
    db_session.commit()

    assert state.ready is True
    assert state.ledger_generation == replacement.id
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == replacement.id
    assert db_session.get(models.LedgerGeneration, generation.id).status == "rejected"


def test_configured_freshness_threshold_returns_stale_without_mutating_history(
    db_session, monkeypatch
):
    generation, _ = _published_truth(db_session)
    monkeypatch.setenv(planning_truth.TRUTH_MAX_AGE_SECONDS_ENV, "3600")

    state = planning_truth.get_readiness(
        db_session,
        now=generation.accepted_at.replace(tzinfo=timezone.utc)
        + timedelta(seconds=3601),
    )

    assert state.truth_status == "stale"
    assert state.ready is False
    assert state.ledger_generation == generation.id
    assert "max_age_seconds=3600" in state.reason
    assert generation.status == "accepted"
    with pytest.raises(planning_truth.PlanningTruthUnavailable) as unavailable:
        planning_truth.require_accepted_truth(db_session, "freshness-test")
    assert unavailable.value.as_dict()["truth_status"] == "stale"


@pytest.mark.parametrize("raw", ["0", "-1", "not-a-number"])
def test_invalid_freshness_threshold_fails_closed_configuration(
    db_session, monkeypatch, raw
):
    _published_truth(db_session)
    monkeypatch.setenv(planning_truth.TRUTH_MAX_AGE_SECONDS_ENV, raw)

    with pytest.raises(RuntimeError, match="positive integer"):
        planning_truth.get_readiness(db_session)

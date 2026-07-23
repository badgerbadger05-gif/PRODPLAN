from datetime import datetime, timezone

import pytest

from app import models
from app.services import planning_truth


def _generation(**overrides):
    values = {
        "generation_key": "replay-20260723-v1",
        "status": "building",
        "cutoff": datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
        "source_watermarks": {"1c": "2026-07-23T12:00:00+00:00"},
        "capabilities": {
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": False,
            "planning_snapshots": False,
        },
        "algorithm_version": "ledger-replay/1",
        "replay_version": "historical/1",
    }
    values.update(overrides)
    values.setdefault(
        "physical_import_batch",
        models.PhysicalImportBatch(
            batch_key=f"physical-{values['generation_key']}",
            status="completed",
            source_watermarks={},
        ),
    )
    return models.LedgerGeneration(**values)


def test_missing_state_is_explicitly_uninitialized_and_fail_closed(db_session):
    state = planning_truth.get_truth_state(db_session)

    assert state.truth_status == "uninitialized"
    assert state.ready is False
    assert state.ledger_generation is None
    with pytest.raises(planning_truth.PlanningTruthUnavailable) as raised:
        planning_truth.require_accepted(db_session)
    assert raised.value.as_dict()["code"] == "planning_truth_unavailable"
    assert raised.value.as_dict()["truth_status"] == "uninitialized"
    with pytest.raises(planning_truth.PlanningTruthUnavailable) as consumer_error:
        planning_truth.require_accepted_truth(db_session, "period-plan-report")
    assert consumer_error.value.as_dict()["consumer"] == "period-plan-report"


@pytest.mark.parametrize("status", ["building", "stale", "rejected", "uninitialized"])
def test_nonaccepted_published_pointer_never_becomes_ready(db_session, status):
    generation = _generation(status=status, reason=f"{status} for test")
    db_session.add(generation)
    db_session.flush()
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    db_session.commit()

    state = planning_truth.get_readiness(db_session)

    assert state.truth_status == status
    assert state.ready is False
    assert state.source_watermarks["1c"].startswith("2026-07-23")
    with pytest.raises(planning_truth.PlanningTruthUnavailable):
        planning_truth.require_accepted(db_session)


def test_publish_and_require_return_complete_accepted_identity(db_session):
    generation = _generation(status="accepted")

    published = planning_truth.publish_generation(db_session, generation)
    db_session.commit()
    required = planning_truth.require_accepted_truth(db_session, "test-consumer")

    assert published.ready is True
    assert required.truth_status == "accepted"
    assert required.ledger_generation == generation.id
    assert required.generation_key == "replay-20260723-v1"
    assert required.cutoff == datetime(2026, 7, 23, 12, 0)
    assert required.algorithm_version == "ledger-replay/1"
    assert required.replay_version == "historical/1"
    assert required.capabilities["physical_ledger"] is True
    assert required.accepted_at is not None


def test_malformed_accepted_generation_is_rejected_fail_closed(db_session):
    generation = _generation(status="accepted", cutoff=None)
    db_session.add(generation)
    db_session.flush()
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    db_session.commit()

    state = planning_truth.get_readiness(db_session)

    assert state.truth_status == "rejected"
    assert state.ready is False
    assert "missing cutoff" in state.reason
    with pytest.raises(planning_truth.PlanningTruthUnavailable):
        planning_truth.require_accepted(db_session)


def test_publish_refuses_nonaccepted_generation(db_session):
    with pytest.raises(ValueError, match="only an accepted"):
        planning_truth.publish_generation(db_session, _generation(status="building"))


def test_required_capabilities_fail_closed_even_when_generation_is_accepted(db_session):
    generation = _generation(status="accepted")
    planning_truth.publish_generation(db_session, generation)
    db_session.commit()

    with pytest.raises(planning_truth.PlanningTruthUnavailable) as raised:
        planning_truth.require_accepted_truth(
            db_session,
            "period-plan-execution",
            required_capabilities=("physical_ledger", "execution_allocations"),
        )

    state = raised.value.state
    assert state.truth_status == "accepted"
    assert state.ready is False
    assert state.reason == (
        "Accepted Ledger generation lacks capabilities: execution_allocations"
    )
    assert raised.value.as_dict()["consumer"] == "period-plan-execution"


def test_required_capabilities_allow_only_declared_layers(db_session):
    generation = _generation(status="accepted")
    planning_truth.publish_generation(db_session, generation)
    db_session.commit()

    state = planning_truth.require_accepted_truth(
        db_session,
        "reservation-replay-report",
        required_capabilities=("physical_ledger", "reservation_replay"),
    )

    assert state.ready is True


def test_read_snapshot_publish_is_idempotent_and_latest_is_generation_bound(db_session):
    generation = _generation(status="accepted")
    planning_truth.publish_generation(db_session, generation)
    first = planning_truth.publish_read_snapshot(
        db_session,
        consumer="period-plan",
        snapshot_key="plan-1-v1",
        payload={"rows": [{"item": "A", "executed": "1.000"}]},
        required_capabilities=("physical_ledger",),
    )
    again = planning_truth.publish_read_snapshot(
        db_session,
        consumer="period-plan",
        snapshot_key="plan-1-v1",
        payload={"rows": [{"item": "A", "executed": "1.000"}]},
        required_capabilities=("physical_ledger",),
    )
    db_session.commit()

    latest = planning_truth.get_latest_read_snapshot(
        db_session,
        consumer="period-plan",
        snapshot_key="plan-1-v1",
        required_capabilities=("physical_ledger",),
    )

    assert again.id == first.id
    assert latest.id == first.id
    assert latest.ledger_generation_id == generation.id
    assert latest.truth_status == "accepted"
    assert planning_truth.get_latest_read_snapshot(
        db_session,
        consumer="period-plan",
        snapshot_key="another-filter",
    ) is None


def test_read_snapshot_identity_cannot_be_overwritten(db_session):
    planning_truth.publish_generation(db_session, _generation(status="accepted"))
    planning_truth.publish_read_snapshot(
        db_session,
        consumer="period-plan",
        snapshot_key="plan-1-v1",
        payload={"total": 1},
    )

    with pytest.raises(planning_truth.PlanningSnapshotConflict):
        planning_truth.publish_read_snapshot(
            db_session,
            consumer="period-plan",
            snapshot_key="plan-1-v1",
            payload={"total": 2},
        )


def test_latest_snapshot_does_not_fall_back_to_previous_generation(db_session):
    first_generation = _generation(status="accepted")
    planning_truth.publish_generation(db_session, first_generation)
    planning_truth.publish_read_snapshot(
        db_session,
        consumer="period-plan",
        snapshot_key="generation-one",
        payload={"total": 1},
    )
    second_generation = _generation(
        generation_key="replay-20260723-v2",
        status="accepted",
    )
    switched = planning_truth.publish_generation(db_session, second_generation)
    assert switched.generation_id == second_generation.id
    db_session.commit()

    latest = planning_truth.get_latest_read_snapshot(
        db_session,
        consumer="period-plan",
    )

    assert latest is None


def test_snapshot_publish_and_read_require_capabilities(db_session):
    planning_truth.publish_generation(db_session, _generation(status="accepted"))

    with pytest.raises(planning_truth.PlanningTruthUnavailable) as publish_error:
        planning_truth.publish_read_snapshot(
            db_session,
            consumer="period-plan",
            snapshot_key="blocked",
            payload={},
            required_capabilities=("planning_snapshots",),
        )
    assert "planning_snapshots" in publish_error.value.state.reason

    with pytest.raises(planning_truth.PlanningTruthUnavailable):
        planning_truth.get_latest_read_snapshot(
            db_session,
            consumer="period-plan",
            required_capabilities=("planning_snapshots",),
        )

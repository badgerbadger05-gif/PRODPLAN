from datetime import datetime, timezone

import pytest

from app import models
from app.services import planning_truth
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    load_current_execution_rows,
    publish_current_execution_scope,
    require_current_execution_scope,
)


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
            planning_truth.CAPABILITY_RESERVATION_CONSUMPTION_ALLOCATION: False,
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
    assert (
        required.capabilities[planning_truth.CAPABILITY_RESERVATION_CONSUMPTION_ALLOCATION]
        is False
    )
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


def _publish_current(db_session, generation, rows):
    return publish_current_execution_scope(
        db_session,
        source_revision=f"accepted:g{generation.id}:period_plan_execution",
        source_generation_id=generation.id,
        scope_key="period-plan:all-live-plans",
        entity_kinds=("period_plan_execution",),
        rows=[
            {
                "entity_kind": "period_plan_execution",
                "business_identity": identity,
                "scope_key": "period-plan:all-live-plans",
                "payload": payload,
            }
            for identity, payload in rows
        ],
    )


def test_current_scope_publication_is_idempotent_and_generation_bound(db_session):
    generation = _generation(status="accepted")
    first = _publish_current(db_session, generation, [("plan:1:req:1", {"qty": "1.000"})])
    again = _publish_current(db_session, generation, [("plan:1:req:1", {"qty": "1.000"})])

    scope = require_current_execution_scope(
        db_session,
        entity_kind="period_plan_execution",
        scope_key="period-plan:all-live-plans",
    )
    assert first.changed_rows == 1
    assert again.idempotent is True
    assert scope.source_generation_id == generation.id
    assert len(load_current_execution_rows(
        db_session,
        entity_kind="period_plan_execution",
        scope_key="period-plan:all-live-plans",
    )) == 1


def test_current_scope_replaces_content_and_keeps_one_business_identity(db_session):
    generation = _generation(status="accepted")
    _publish_current(db_session, generation, [("plan:1:req:1", {"qty": "1.000"})])
    result = _publish_current(db_session, generation, [("plan:1:req:1", {"qty": "2.000"})])

    rows = load_current_execution_rows(
        db_session,
        entity_kind="period_plan_execution",
        scope_key="period-plan:all-live-plans",
    )
    assert result.changed_rows == 1
    assert len(rows) == 1
    assert rows[0].payload["qty"] == "2.000"


def test_current_scope_does_not_fallback_after_truth_pointer_moves(db_session):
    first_generation = _generation(status="accepted")
    _publish_current(db_session, first_generation, [("plan:1:req:1", {"qty": "1.000"})])
    second_generation = _generation(
        generation_key="replay-20260723-v2", status="accepted"
    )
    planning_truth.publish_generation(db_session, second_generation)

    with pytest.raises(CurrentExecutionUnavailable, match="stale"):
        require_current_execution_scope(
            db_session,
            entity_kind="period_plan_execution",
            scope_key="period-plan:all-live-plans",
        )


def test_current_scope_requires_explicit_entity_kind_for_empty_publication(db_session):
    generation = _generation(status="accepted")
    with pytest.raises(CurrentExecutionUnavailable, match="entity kinds"):
        publish_current_execution_scope(
            db_session,
            source_revision=f"accepted:g{generation.id}:period_plan_execution",
            source_generation_id=generation.id,
            scope_key="period-plan:all-live-plans",
            rows=[],
        )

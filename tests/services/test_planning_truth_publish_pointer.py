"""The truth pointer may only be moved by a build that still owns its parent.

``publish_generation`` used to read the pointer with a plain ``db.get`` and
overwrite it unconditionally, so two publishers (physical refresh and
obligation refresh, which do not even share an advisory lock) could each fork
from generation N and the loser would silently discard the winner's truth.
"""

from datetime import date, datetime, timezone

import pytest

from app import models
from app.services.item_ledger.generation_lifecycle import accept_generation_build
from app.services.planning_truth import (
    PlanningTruthPublishConflict,
    publish_generation,
)

from tests.services.test_generation_lifecycle import _synthetic


def _accepted(db, key: str, *, cutoff: datetime | None = None):
    cutoff = cutoff or datetime(2026, 7, 31, 23, 59)
    batch = models.PhysicalImportBatch(
        batch_key=f"physical-{key}",
        status="completed",
        cutoff=cutoff,
        completed_at=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=f"generation-{key}",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        physical_import_batch=batch,
        algorithm_version="test",
        replay_version="test",
        source_watermarks={},
        capabilities={"physical_ledger": True},
    )
    db.add(generation)
    db.flush()
    return generation


def _pointer(db, generation):
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None:
        pointer = models.PlanningTruthState(id=1)
        db.add(pointer)
    pointer.current_generation_id = int(generation.id)
    db.flush()
    return pointer


def test_publish_moves_the_pointer_when_the_expected_parent_still_holds(db_session):
    parent = _accepted(db_session, "cas-parent")
    _pointer(db_session, parent)
    child = _accepted(db_session, "cas-child")

    readiness = publish_generation(
        db_session, child, expected_parent_id=int(parent.id)
    )

    assert readiness.ledger_generation == child.id
    assert db_session.get(
        models.PlanningTruthState, 1
    ).current_generation_id == child.id


def test_publish_refuses_to_overwrite_a_pointer_that_moved_on(db_session):
    parent = _accepted(db_session, "cas-stale-parent")
    winner = _accepted(db_session, "cas-winner")
    loser = _accepted(db_session, "cas-loser")
    # Another publisher already advanced truth away from ``parent``.
    _pointer(db_session, winner)

    with pytest.raises(PlanningTruthPublishConflict) as excinfo:
        publish_generation(db_session, loser, expected_parent_id=int(parent.id))

    assert str(parent.id) in str(excinfo.value)
    assert db_session.get(
        models.PlanningTruthState, 1
    ).current_generation_id == winner.id


def test_republishing_the_same_generation_is_idempotent(db_session):
    parent = _accepted(db_session, "cas-idem-parent")
    child = _accepted(db_session, "cas-idem-child")
    _pointer(db_session, child)

    readiness = publish_generation(
        db_session, child, expected_parent_id=int(parent.id)
    )

    assert readiness.ledger_generation == child.id


def test_publish_without_an_expected_parent_still_initialises_the_pointer(db_session):
    genesis = _accepted(db_session, "cas-genesis")

    readiness = publish_generation(db_session, genesis)

    assert readiness.ledger_generation == genesis.id


def test_accept_defaults_the_compare_and_set_to_the_sealed_parent(db_session):
    """A refresh build re-checks the pointer at publication, not only at fork."""
    parent = _accepted(db_session, "accept-cas-parent")
    generation, _requirement = _synthetic(db_session, "accept-cas")
    generation.source_watermarks = {
        **dict(generation.source_watermarks or {}),
        "parent_generation_id": int(parent.id),
    }
    # The pointer moved to a third generation while this candidate was building.
    intruder = _accepted(db_session, "accept-cas-intruder")
    _pointer(db_session, intruder)
    db_session.flush()

    with pytest.raises(PlanningTruthPublishConflict):
        accept_generation_build(
            db_session,
            generation.id,
            replay_from=datetime(2026, 7, 1),
        )

    db_session.expire_all()
    assert db_session.get(
        models.PlanningTruthState, 1
    ).current_generation_id == intruder.id
    assert db_session.get(models.LedgerGeneration, generation.id).status == "building"

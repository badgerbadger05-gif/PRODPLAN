from datetime import datetime, timedelta, timezone

import pytest

from app import models
from app.services.item_ledger.generation_bootstrap import (
    ALGORITHM_VERSION,
    GenerationBootstrapError,
    create_historical_generation,
    historical_generation_status,
    resume_historical_generation_import,
)


def _range():
    historical_from = datetime(2022, 12, 1, tzinfo=timezone.utc)
    replay_from = datetime(2026, 6, 1, tzinfo=timezone.utc)
    cutoff = datetime(2026, 7, 23, tzinfo=timezone.utc)
    return historical_from, replay_from, cutoff


def test_bootstrap_is_idempotent_and_does_not_publish(db_session):
    historical_from, replay_from, cutoff = _range()

    first = create_historical_generation(
        db_session,
        generation_key="historical-20260723",
        historical_from_exclusive=historical_from,
        replay_from=replay_from,
        cutoff=cutoff,
    )
    repeated = create_historical_generation(
        db_session,
        generation_key="historical-20260723",
        historical_from_exclusive=historical_from,
        replay_from=replay_from,
        cutoff=cutoff,
    )

    assert first.created is True
    assert repeated.created is False
    assert repeated.ledger_generation_id == first.ledger_generation_id
    generation = db_session.get(models.LedgerGeneration, first.ledger_generation_id)
    boundary = db_session.get(
        models.PhysicalImportBatch, first.physical_import_batch_id
    )
    assert generation.status == "building"
    assert generation.algorithm_version == ALGORITHM_VERSION
    assert generation.capabilities == {}
    assert boundary.status == "completed"
    assert boundary.cutoff == historical_from.replace(tzinfo=None)
    assert db_session.get(models.PlanningTruthState, 1) is None


def test_bootstrap_rejects_conflicting_or_partial_lineage(db_session):
    historical_from, replay_from, cutoff = _range()
    first = create_historical_generation(
        db_session,
        generation_key="first",
        historical_from_exclusive=historical_from,
        replay_from=replay_from,
        cutoff=cutoff,
    )

    with pytest.raises(GenerationBootstrapError, match="already exists"):
        create_historical_generation(
            db_session,
            generation_key="second",
            historical_from_exclusive=historical_from,
            replay_from=replay_from,
            cutoff=cutoff,
        )
    with pytest.raises(GenerationBootstrapError, match="different"):
        create_historical_generation(
            db_session,
            generation_key="first",
            historical_from_exclusive=historical_from,
            replay_from=replay_from + timedelta(days=1),
            cutoff=cutoff,
        )

    generation = db_session.get(models.LedgerGeneration, first.ledger_generation_id)
    generation.status = "rejected"
    partial = models.PhysicalImportBatch(
        batch_key="partial", status="building", source_watermarks={}
    )
    db_session.add(partial)
    db_session.commit()
    with pytest.raises(GenerationBootstrapError, match="incomplete"):
        create_historical_generation(
            db_session,
            generation_key="third",
            historical_from_exclusive=historical_from,
            replay_from=replay_from,
            cutoff=cutoff,
        )


def test_idempotent_bootstrap_detects_interleaved_physical_batch(db_session):
    historical_from, replay_from, cutoff = _range()
    create_historical_generation(
        db_session,
        generation_key="interleave-test",
        historical_from_exclusive=historical_from,
        replay_from=replay_from,
        cutoff=cutoff,
    )
    db_session.add(
        models.PhysicalImportBatch(
            batch_key="external",
            status="completed",
            cutoff=cutoff,
            source_watermarks={"source": "external"},
            completed_at=cutoff,
        )
    )
    db_session.commit()

    with pytest.raises(GenerationBootstrapError, match="interleaved"):
        create_historical_generation(
            db_session,
            generation_key="interleave-test",
            historical_from_exclusive=historical_from,
            replay_from=replay_from,
            cutoff=cutoff,
        )


def test_bootstrap_rejects_inconsistent_or_mutating_truth_pointer(db_session):
    historical_from, replay_from, cutoff = _range()
    boundary = models.PhysicalImportBatch(
        batch_key="accepted-boundary",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    accepted = models.LedgerGeneration(
        generation_key="accepted",
        status="accepted",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=boundary,
        algorithm_version="test",
    )
    db_session.add(accepted)
    db_session.flush()
    db_session.add(
        models.PlanningTruthState(id=1, current_generation_id=accepted.id)
    )
    db_session.add(
        models.LedgerBuildBatch(
            ledger_generation_id=accepted.id,
            stage="snapshot_build",
            batch_key="active",
            status="building",
            algorithm_version="test",
            metrics={},
        )
    )
    db_session.commit()

    with pytest.raises(GenerationBootstrapError, match="active mutation"):
        create_historical_generation(
            db_session,
            generation_key="new",
            historical_from_exclusive=historical_from,
            replay_from=replay_from,
            cutoff=cutoff,
        )


def test_status_is_persisted_only_and_import_uses_frozen_range(
    db_session, monkeypatch
):
    historical_from, replay_from, cutoff = _range()
    created = create_historical_generation(
        db_session,
        generation_key="status-test",
        historical_from_exclusive=historical_from,
        replay_from=replay_from,
        cutoff=cutoff,
    )
    captured = {}

    def fake_import(db, **kwargs):
        captured.update(kwargs)
        return "result"

    monkeypatch.setattr(
        "app.services.item_ledger.generation_bootstrap."
        "run_historical_physical_import",
        fake_import,
    )
    assert resume_historical_generation_import(
        db_session,
        ledger_generation_id=created.ledger_generation_id,
        client=object(),
        max_windows=2,
    ) == "result"
    assert captured["from_exclusive"] == historical_from
    assert captured["to_inclusive"] == cutoff
    assert captured["max_windows"] == 2

    status = historical_generation_status(
        db_session, created.ledger_generation_id
    )
    assert status["status"] == "building"
    assert status["replay_from"] == replay_from.isoformat()
    assert status["physical_checkpoints"] == 0
    assert status["has_incomplete_checkpoint"] is False


@pytest.mark.parametrize(
    "historical_from,replay_from,cutoff",
    [
        (
            datetime(2026, 1, 1),
            datetime(2026, 2, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 1, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 2, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 1, tzinfo=timezone.utc),
        ),
    ],
)
def test_bootstrap_rejects_ambiguous_dates(
    db_session, historical_from, replay_from, cutoff
):
    with pytest.raises(ValueError):
        create_historical_generation(
            db_session,
            generation_key="invalid",
            historical_from_exclusive=historical_from,
            replay_from=replay_from,
            cutoff=cutoff,
        )


def test_bootstrap_allows_opening_and_replay_to_share_exact_boundary(db_session):
    opening = datetime(2026, 6, 1, tzinfo=timezone.utc)
    cutoff = datetime(2026, 7, 24, tzinfo=timezone.utc)

    created = create_historical_generation(
        db_session,
        generation_key="same-opening-replay-boundary",
        historical_from_exclusive=opening,
        replay_from=opening,
        cutoff=cutoff,
    )

    assert created.historical_from_exclusive == opening
    assert created.replay_from == opening

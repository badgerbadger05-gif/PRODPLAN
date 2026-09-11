from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from app import models
from app.services import planning_truth
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    load_current_execution_rows,
    publish_current_execution_scope,
    require_current_execution_scope,
)


def _generation(db, key):
    cutoff = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    generation = models.LedgerGeneration(
        generation_key=key,
        status="accepted",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=models.PhysicalImportBatch(
            batch_key=f"batch-{key}",
            status="completed",
            cutoff=cutoff,
            source_watermarks={},
            completed_at=cutoff,
        ),
        algorithm_version="test/1",
        accepted_at=cutoff,
    )
    planning_truth.publish_generation(db, generation)
    db.flush()
    return generation


def _publish(db, generation, rows):
    return publish_current_execution_scope(
        db,
        source_revision=f"accepted:g{generation.id}:purchase_control_journal",
        source_generation_id=generation.id,
        scope_key="purchase:all-live-plans",
        entity_kinds=("purchase_control_journal",),
        rows=[
            {
                "entity_kind": "purchase_control_journal",
                "business_identity": identity,
                "scope_key": "purchase:all-live-plans",
                "payload": payload,
            }
            for identity, payload in rows
        ],
    )


def test_current_scope_replacement_is_stable_across_generations(db_session):
    generation_a = _generation(db_session, "current-a")
    first = _publish(db_session, generation_a, [("purchase:1", {"qty": 4})])
    row_a = load_current_execution_rows(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )[0]

    generation_b = _generation(db_session, "current-b")
    second = _publish(db_session, generation_b, [("purchase:1", {"qty": 4})])
    row_b = load_current_execution_rows(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )[0]
    scope = require_current_execution_scope(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )

    assert first.changed_rows == 1
    assert second.idempotent is True
    assert row_b.id == row_a.id
    assert row_b.source_generation_id == generation_a.id
    assert scope.source_generation_id == generation_b.id


def test_current_scope_rejects_duplicate_identity_before_dml(db_session):
    generation = _generation(db_session, "current-duplicate")
    with pytest.raises(CurrentExecutionUnavailable, match="duplicate"):
        _publish(
            db_session,
            generation,
            [("purchase:1", {"qty": 4}), ("purchase:1", {"qty": 5})],
        )

    assert db_session.query(models.CurrentExecutionScope).count() == 0
    assert db_session.query(models.CurrentExecutionRow).count() == 0


def test_current_scope_preserves_empty_manifest_and_fails_closed_when_missing(db_session):
    generation = _generation(db_session, "current-empty")
    result = _publish(db_session, generation, [])
    scope = require_current_execution_scope(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )

    assert result.changed_rows == 0
    assert result.idempotent is True
    assert scope.result_ready is True
    assert load_current_execution_rows(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    ) == []


def test_planning_run_retains_nullable_generation_lineage(db_session):
    generation = _generation(db_session, "run-lineage")
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
    )
    closed = models.PlanningRun(status="CLOSED", config_snapshot={})
    db_session.add_all([run, closed])
    db_session.flush()

    assert run.ledger_generation_id == generation.id
    assert run.ledger_cutoff == generation.cutoff
    assert closed.ledger_generation_id is None
    assert closed.ledger_cutoff is None


def test_migration_still_declares_historical_storage_contract():
    path = (
        Path(__file__).resolve().parents[2]
        / "backend/alembic/versions/20260723_10_planning_read_storage.py"
    )
    spec = spec_from_file_location("planning_read_storage_migration", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "20260723_10"
    assert module.down_revision == "20260723_09"
    source = path.read_text()
    assert "ledger_generation_id" in source
    assert "ledger_cutoff" in source

from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app import models
from app.services import planning_truth


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


def test_same_logical_snapshot_key_is_distinct_per_generation(db_session):
    generation_a = _generation(db_session, "read-a")
    snapshot_a = planning_truth.publish_read_snapshot(
        db_session,
        consumer="purchase-journal",
        snapshot_key="journal-v1",
        payload={"generation": "a"},
    )
    generation_b = _generation(db_session, "read-b")
    snapshot_b = planning_truth.publish_read_snapshot(
        db_session,
        consumer="purchase-journal",
        snapshot_key="journal-v1",
        payload={"generation": "b"},
    )

    assert snapshot_a.id != snapshot_b.id
    assert snapshot_a.ledger_generation_id == generation_a.id
    assert snapshot_b.ledger_generation_id == generation_b.id
    assert (
        planning_truth.get_latest_read_snapshot(
            db_session,
            consumer="purchase-journal",
            snapshot_key="journal-v1",
        ).id
        == snapshot_b.id
    )


def test_row_and_root_membership_uniqueness_and_cascade(db_session):
    _generation(db_session, "read-rows")
    snapshot = planning_truth.publish_read_snapshot(
        db_session,
        consumer="purchase-journal",
        snapshot_key="journal-v1",
        payload={},
    )
    row = models.PlanningReadRow(
        snapshot=snapshot,
        row_key="purchase:1",
        row_kind="purchase",
        payload={"qty": 4},
    )
    db_session.add(row)
    db_session.flush()
    member = models.PlanningReadRootMember(
        snapshot=snapshot,
        row=row,
        root_key="root:42",
        payload={},
    )
    db_session.add(member)
    db_session.commit()

    db_session.add(
        models.PlanningReadRow(
            snapshot=snapshot,
            row_key="purchase:1",
            row_kind="purchase",
            payload={},
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

    snapshot = db_session.query(models.PlanningReadSnapshot).one()
    row = db_session.query(models.PlanningReadRow).one()
    db_session.add(
        models.PlanningReadRootMember(
            snapshot=snapshot,
            row=row,
            root_key="root:42",
            payload={},
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()

    snapshot = db_session.query(models.PlanningReadSnapshot).one()
    db_session.delete(snapshot)
    db_session.flush()
    assert db_session.query(models.PlanningReadRow).count() == 0
    assert db_session.query(models.PlanningReadRootMember).count() == 0


def test_planning_run_has_nullable_generation_lineage(db_session):
    generation = _generation(db_session, "run-lineage")
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
    )
    legacy = models.PlanningRun(status="CLOSED", config_snapshot={})
    db_session.add_all([run, legacy])
    db_session.flush()

    assert run.ledger_generation_id == generation.id
    assert run.ledger_cutoff == generation.cutoff
    assert legacy.ledger_generation_id is None
    assert legacy.ledger_cutoff is None


def test_migration_declares_storage_contract():
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
    assert "planning_read_row" in source
    assert "planning_read_root_member" in source
    assert "uq_planning_read_snapshot_consumer_key_generation" in source

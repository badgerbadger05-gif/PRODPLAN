"""Contract tests for /api/v1/production-control/assembly-queue."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.routers.production_control import (
    get_assembly_queue,
    get_drum_schedule,
    get_shelf_projections,
    router as production_control_router,
)
from app.services import planning_truth


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(production_control_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _accepted_generation(db, *, with_assembly_queue_capability: bool = True):
    cutoff = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="assembly-queue-router-physical",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="assembly-queue-router-generation",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        capabilities={},
        source_watermarks={},
        physical_import_batch=physical,
        algorithm_version="tests/assembly-queue-router",
    )
    db.add_all([physical, generation])
    db.flush()
    generation.capabilities = {
        planning_truth.CAPABILITY_PHYSICAL_LEDGER: True,
        planning_truth.CAPABILITY_RESERVATION_REPLAY: True,
        planning_truth.CAPABILITY_PLANNING_SNAPSHOTS: True,
        planning_truth.CAPABILITY_ASSEMBLY_QUEUE: with_assembly_queue_capability,
        planning_truth.CAPABILITY_DRUM_SCHEDULE: True,
        planning_truth.CAPABILITY_SHELF_PROJECTION: True,
    }
    planning_truth.publish_generation(db, generation)
    return generation, cutoff


def _publish_snapshot(db, payload):
    planning_truth.publish_read_snapshot(
        db,
        consumer="assembly_queue",
        snapshot_key="current:v1",
        payload=payload,
        required_capabilities=(
            planning_truth.CAPABILITY_PHYSICAL_LEDGER,
            planning_truth.CAPABILITY_RESERVATION_REPLAY,
            planning_truth.CAPABILITY_PLANNING_SNAPSHOTS,
            planning_truth.CAPABILITY_ASSEMBLY_QUEUE,
        ),
    )
    db.flush()


def test_assembly_queue_returns_strict_payload_for_accepted_snapshot(client, db_session):
    generation, _ = _accepted_generation(db_session)
    payload = {
        "rows": [
            {
                "run_id": 3001,
                "plan_id": 4001,
                "plan_line_id": 4101,
                "item_id": 5001,
                "item_code": "FG-1",
                "item_name": "Finished good 1",
                "bucket_date": "2026-08-03",
                "period_from": "2026-08-01",
                "period_to": "2026-08-31",
                "planned_output_qty": 10.0,
                "accepted_plan_output_qty": 3.0,
                "assembly_remaining_qty": 7.0,
                "priority_key": ["2026-08-01", "2026-08-31", 4001, 4101],
            },
            {
                "run_id": 3002,
                "plan_id": 4002,
                "plan_line_id": 4102,
                "item_id": 5002,
                "item_code": "FG-2",
                "item_name": "Finished good 2",
                "bucket_date": "2026-09-05",
                "period_from": "2026-09-01",
                "period_to": "2026-09-30",
                "planned_output_qty": 20.0,
                "accepted_plan_output_qty": 5.0,
                "assembly_remaining_qty": 15.0,
                "priority_key": ["2026-09-01", "2026-09-30", 4002, 4102],
            },
        ],
        "total_rows": 2,
        "total_queue_qty": 22.0,
    }
    _publish_snapshot(db_session, payload)
    db_session.commit()

    response = client.get("/api/v1/production-control/assembly-queue")
    assert response.status_code == 200
    assert response.json() == {
        **payload,
        "truth_meta": {
            "ledger_generation": int(generation.id),
            "cutoff": generation.cutoff.isoformat(),
            "truth_status": "accepted",
            "truth_reason": None,
        },
    }


def test_assembly_queue_router_rejects_missing_assembly_queue_capability(
    db_session,
):
    _accepted_generation(db_session, with_assembly_queue_capability=False)
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        get_assembly_queue(db=db_session)
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "planning_truth_unavailable"
    assert exc.value.detail["ready"] is False


def test_assembly_queue_router_rejects_missing_snapshot_even_with_capability(
    db_session,
):
    _accepted_generation(db_session)
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        get_assembly_queue(db=db_session)
    detail = exc.value.detail
    assert detail["code"] == "assembly_queue_unavailable"


def test_drum_router_reads_only_persisted_accepted_schedule(client, db_session):
    generation, cutoff = _accepted_generation(db_session)
    db_session.add(
        models.DrumSchedule(
            ledger_generation_id=generation.id,
            status="completed",
            algorithm_version="tests/1",
            schedule_from=cutoff.date(),
            schedule_to=cutoff.date(),
            queue_signature="q" * 64,
            slot_signature="s" * 64,
            gap_signature="g" * 64,
            slot_row_count=0,
            gap_row_count=0,
            total_open_qty=0,
            total_slot_qty=0,
            total_gap_qty=0,
            metrics={},
        )
    )
    db_session.commit()

    response = client.get("/api/v1/production-control/drum")

    assert response.status_code == 200
    assert response.json() == {
        "schedule_from": cutoff.date().isoformat(),
        "schedule_to": cutoff.date().isoformat(),
        "slots": [],
        "gaps": [],
        "total_open_qty": 0.0,
        "total_slot_qty": 0.0,
        "total_gap_qty": 0.0,
        "truth_meta": {
            "ledger_generation": generation.id,
            "cutoff": generation.cutoff.isoformat(),
            "truth_status": "accepted",
            "truth_reason": None,
        },
    }


def test_drum_router_fails_closed_without_persisted_schedule(db_session):
    _accepted_generation(db_session)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        get_drum_schedule(db=db_session)

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "drum_schedule_unavailable"


def test_shelves_router_reads_empty_persisted_projection(client, db_session):
    generation, _ = _accepted_generation(db_session)
    db_session.commit()

    response = client.get("/api/v1/production-control/shelves")

    assert response.status_code == 200
    assert response.json()["rows"] == []
    assert response.json()["total_rows"] == 0
    assert response.json()["truth_meta"]["ledger_generation"] == generation.id


def test_shelves_router_fails_closed_without_capability(db_session):
    generation, _ = _accepted_generation(db_session)
    generation.capabilities = {
        **generation.capabilities,
        planning_truth.CAPABILITY_SHELF_PROJECTION: False,
    }
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        get_shelf_projections(db=db_session)

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "planning_truth_unavailable"

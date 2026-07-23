"""The DBR feeder mount GETs read one persisted Ledger-bound snapshot."""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.routers.dbr import router as dbr_router
from app.services import planning_truth
from app.services.dbr import (
    cockpit_snapshot_service,
    feeder_material_service,
    feeder_position_service,
    feeder_signal_service,
    processing_board_service,
)


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
    app.include_router(dbr_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client


def _accepted_generation(db, suffix: str = "one"):
    cutoff = datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key=f"dbr-cockpit-import-{suffix}",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=f"dbr-cockpit-generation-{suffix}",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        physical_import_batch=batch,
        algorithm_version="tests/dbr-cockpit",
        source_watermarks={},
        capabilities={
            planning_truth.CAPABILITY_PHYSICAL_LEDGER: True,
            planning_truth.CAPABILITY_RESERVATION_REPLAY: True,
            planning_truth.CAPABILITY_PLANNING_SNAPSHOTS: True,
        },
    )
    db.add(generation)
    db.flush()
    planning_truth.publish_generation(db, generation)
    db.commit()
    return generation


def _publish_cockpit(db):
    payload = {
        "positions": [{
            "id": 11,
            "item_code": "P-11",
            "item_name": "Position",
            "is_active": True,
            "mode": "shelf",
            "supply_type": "purchase",
            "warehouse_ref1c": "WH",
            "live_nfp": {"zone": "red", "nfp": 2},
        }],
        "signals": [{
            "id": 21,
            "item_code": "S-21",
            "item_name": "Signal",
            "status": "Open",
            "zone": "red",
            "signal_type": "Пополнение",
            "deficit_lines": [],
        }],
        "deficits": {
            "deficits": [{"item": "MAT", "short_qty": 4}],
            "kpis": {"deficit_materials": 1, "queue_open": 1},
        },
        "processing_board": {
            "positions": [{"position_id": 31}],
            "positions_total": 1,
            "overdue_positions": 0,
        },
        "meta": {"source_schedule_id": 7},
    }
    planning_truth.publish_read_snapshot(
        db,
        consumer=cockpit_snapshot_service.CONSUMER,
        snapshot_key="test-cockpit",
        payload=payload,
        required_capabilities=cockpit_snapshot_service.REQUIRED_CAPABILITIES,
    )
    db.commit()


def test_mount_gets_read_snapshot_without_invoking_live_calculators(
    client,
    db_session,
    monkeypatch,
):
    _accepted_generation(db_session)
    _publish_cockpit(db_session)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("live DBR calculator called from GET")

    monkeypatch.setattr(feeder_position_service, "query_position_views", forbidden)
    monkeypatch.setattr(feeder_signal_service, "list_signals", forbidden)
    monkeypatch.setattr(feeder_material_service, "get_deficits", forbidden)
    monkeypatch.setattr(processing_board_service, "processing_board", forbidden)

    positions = client.get(
        "/api/v1/dbr/feeder/positions",
        params={"include_live_nfp": "true", "active_only": "true"},
    )
    signals = client.get("/api/v1/dbr/feeder/signals", params={"status": "Open"})
    deficits = client.get("/api/v1/dbr/feeder/deficits")
    board = client.get("/api/v1/dbr/feeder/processing/board")

    assert positions.status_code == signals.status_code == 200
    assert deficits.status_code == board.status_code == 200
    assert positions.json()[0]["live_nfp"]["nfp"] == 2
    assert signals.json()[0]["id"] == 21
    assert deficits.json()["deficits"][0]["short_qty"] == 4
    assert board.json()["positions"][0]["position_id"] == 31


def test_explicit_builder_fails_closed_without_calling_legacy_calculators(
    db_session,
    monkeypatch,
):
    _accepted_generation(db_session)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy DBR calculator called by snapshot builder")

    monkeypatch.setattr(feeder_position_service, "query_position_views", forbidden)
    monkeypatch.setattr(feeder_signal_service, "list_signals", forbidden)
    monkeypatch.setattr(feeder_material_service, "get_deficits", forbidden)
    monkeypatch.setattr(processing_board_service, "processing_board", forbidden)

    before = db_session.query(models.PlanningReadSnapshot).count()
    with pytest.raises(
        cockpit_snapshot_service.DbrCockpitSnapshotUnavailable,
        match="Ledger-native DBR cockpit builder is not implemented",
    ):
        cockpit_snapshot_service.build_cockpit_snapshot(db_session)

    assert db_session.query(models.PlanningReadSnapshot).count() == before == 0


@pytest.mark.parametrize("path", [
    "/api/v1/dbr/feeder/positions",
    "/api/v1/dbr/feeder/signals",
    "/api/v1/dbr/feeder/deficits",
    "/api/v1/dbr/feeder/processing/board",
])
def test_mount_gets_fail_closed_when_current_generation_has_no_snapshot(
    client,
    db_session,
    path,
):
    generation = _accepted_generation(db_session)

    response = client.get(path)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "dbr_cockpit_snapshot_unavailable"
    assert detail["status"] == "unavailable"
    assert detail["ledger_generation"] == generation.id
    assert "No DBR feeder cockpit snapshot" in detail["reason"]

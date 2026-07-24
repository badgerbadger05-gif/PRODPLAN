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
    feeder_chain_service,
    feeder_material_service,
    feeder_nfp_service,
    feeder_position_service,
    feeder_signal_service,
    processing_board_service,
    processing_materialize_preview,
    processing_trip_manifest,
    purchase_snapshot_service,
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
            planning_truth.CAPABILITY_DBR_FEEDER_COCKPIT: True,
            planning_truth.CAPABILITY_DBR_PURCHASE_COCKPIT: True,
        },
    )
    db.add(generation)
    db.flush()
    planning_truth.publish_generation(db, generation)
    db.commit()
    return generation


def _publish_cockpit(db, *, include_position_id: bool = True):
    position = {
        "item_id": 501,
        "planning_stock_pool": "main",
        "item_code": "P-11",
        "item_name": "Position",
        "is_active": True,
        "mode": "shelf",
        "supply_type": "purchase",
        "warehouse_ref1c": "WH",
        "live_nfp": {"zone": "red", "nfp": 2},
    }
    if include_position_id:
        position["id"] = 11
    payload = {
        "positions": [position],
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


def _publish_purchase(db):
    planning_truth.publish_read_snapshot(
        db,
        consumer=purchase_snapshot_service.CONSUMER,
        snapshot_key="test-purchase",
        payload={"meta": {"read_only": True}, "rows": [{
            "item_code": "BUY-1", "reservation_ids": [41], "to_order_qty": 7,
        }]},
        required_capabilities=purchase_snapshot_service.REQUIRED_CAPABILITIES,
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
    cockpit = client.get("/api/v1/dbr/feeder/cockpit")

    assert positions.status_code == signals.status_code == 200
    assert deficits.status_code == board.status_code == 200
    positions_payload = positions.json()
    signals_payload = signals.json()
    deficits_payload = deficits.json()
    board_payload = board.json()
    assert positions_payload["rows"][0]["live_nfp"]["nfp"] == 2
    assert signals_payload["rows"][0]["id"] == 21
    assert deficits_payload["rows"]["deficits"][0]["short_qty"] == 4
    assert board_payload["rows"]["positions"][0]["position_id"] == 31
    assert positions_payload["truth_meta"]["truth_status"] == "accepted"
    assert positions_payload["truth_meta"]["ledger_generation"] is not None
    assert cockpit.status_code == 200
    assert cockpit.json()["meta"]["truth_status"] == "accepted"
    assert cockpit.json()["meta"]["ledger_generation"] is not None
    assert cockpit.json()["meta"]["snapshot_id"] is not None


def test_position_detail_reads_immutable_current_snapshot_and_ignores_live_query(
    client, db_session, monkeypatch,
):
    _accepted_generation(db_session, "position-detail")
    _publish_cockpit(db_session, include_position_id=False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy position or live NFP calculator called")

    monkeypatch.setattr(feeder_position_service, "get_position_view", forbidden)
    monkeypatch.setattr(feeder_nfp_service, "live_nfp_rows", forbidden)

    listed = client.get("/api/v1/dbr/feeder/positions", params={"include_live_nfp": "true"})
    assert listed.status_code == 200, listed.text
    stable_id = listed.json()["rows"][0]["id"]
    assert isinstance(stable_id, int) and stable_id > 0
    assert stable_id <= (2**53 - 1)

    detail = client.get(
        f"/api/v1/dbr/feeder/positions/{stable_id}",
        params={"include_live_nfp": "false"},
    )
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["id"] == stable_id
    assert payload["item_code"] == "P-11"
    assert payload["live_nfp"] == {"zone": "red", "nfp": 2}
    assert payload["snapshot_meta"]["truth_status"] == "accepted"
    assert payload["snapshot_meta"]["ledger_generation"] is not None
    assert payload["snapshot_meta"]["snapshot_id"] is not None
    persisted = db_session.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == cockpit_snapshot_service.CONSUMER,
    ).one()
    assert "snapshot_id" not in persisted.payload["meta"]
    assert "id" not in persisted.payload["positions"][0]


def test_position_detail_404_only_after_exact_snapshot_is_available(client, db_session):
    _accepted_generation(db_session, "position-missing")
    _publish_cockpit(db_session)
    response = client.get("/api/v1/dbr/feeder/positions/999999")
    assert response.status_code == 404
    assert "current accepted DBR cockpit snapshot" in response.json()["detail"]


def test_position_detail_snapshot_unavailable_is_structured_503(
    client, monkeypatch,
):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy position calculator called")

    monkeypatch.setattr(feeder_position_service, "get_position_view", forbidden)
    monkeypatch.setattr(feeder_nfp_service, "live_nfp_rows", forbidden)
    response = client.get("/api/v1/dbr/feeder/positions/11")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "dbr_cockpit_snapshot_unavailable"


def test_signal_detail_reads_exact_snapshot_and_never_calls_live_service(
    client, db_session, monkeypatch,
):
    _accepted_generation(db_session, "signal-detail")
    _publish_cockpit(db_session)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("live signal service called")

    monkeypatch.setattr(feeder_signal_service, "get_signal", forbidden)
    response = client.get("/api/v1/dbr/feeder/signals/21")
    assert response.status_code == 200, response.text
    assert response.json()["item_code"] == "S-21"
    assert response.json()["snapshot_meta"]["truth_status"] == "accepted"
    missing = client.get("/api/v1/dbr/feeder/signals/999999")
    assert missing.status_code == 404


@pytest.mark.parametrize(("method", "path", "operation"), [
    ("post", "/api/v1/dbr/feeder/signals/preview", "signals_preview"),
    ("get", "/api/v1/dbr/feeder/processing/trip-manifest", "processing_trip_manifest"),
    ("get", "/api/v1/dbr/feeder/processing/trip-manifest/print", "processing_trip_manifest_print"),
    ("post", "/api/v1/dbr/feeder/chain/preview", "chain_preview"),
    ("post", "/api/v1/dbr/feeder/processing/chain/preview", "processing_chain_preview"),
    ("post", "/api/v1/dbr/feeder/chain/refresh", "chain_refresh"),
    ("get", "/api/v1/dbr/feeder/signals/21/processing-order-preview", "processing_order_preview"),
])
def test_retired_live_feeder_reads_are_stable_503_without_service_calls(
    client, db_session, monkeypatch, method, path, operation,
):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("retired live feeder service called")

    monkeypatch.setattr(feeder_signal_service, "preview_signals", forbidden)
    monkeypatch.setattr(feeder_chain_service, "preview_chain_signals", forbidden)
    monkeypatch.setattr(feeder_chain_service, "preview_processing_chain_signals", forbidden)
    monkeypatch.setattr(feeder_chain_service, "refresh_chain_signals", forbidden)
    monkeypatch.setattr(processing_trip_manifest, "build_manifest", forbidden)
    monkeypatch.setattr(processing_trip_manifest, "render_manifest_html", forbidden)
    monkeypatch.setattr(processing_materialize_preview, "preview_processing_signal", forbidden)
    before = db_session.query(models.DbrFeederSignal).count()

    response = getattr(client, method)(path)

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == {
        "code": "dbr_feeder_live_read_retired",
        "consumer": cockpit_snapshot_service.CONSUMER,
        "operation": operation,
        "status": "unavailable",
        "read_only": True,
        "reason": "Live feeder calculation is retired; wait for a snapshot-native implementation",
    }
    assert db_session.query(models.DbrFeederSignal).count() == before


def test_purchase_cockpit_reads_saved_rows_and_legacy_endpoints_are_retired(client, db_session):
    _accepted_generation(db_session, "purchase")
    _publish_purchase(db_session)
    response = client.get("/api/v1/dbr/purchase/cockpit")
    assert response.status_code == 200
    assert response.json()["rows"][0]["to_order_qty"] == 7
    assert response.json()["meta"]["truth_status"] == "accepted"
    preview = client.get("/api/v1/dbr/purchase-plan/preview")
    materialize = client.post("/api/v1/dbr/purchase-plan/materialize", json={"dry_run": True})
    assert preview.status_code == materialize.status_code == 503
    assert preview.json()["detail"]["code"] == "dbr_purchase_legacy_preview_retired"
    assert materialize.json()["detail"]["read_only"] is True


@pytest.mark.parametrize("signal_ids", [None, []])
def test_purchase_launch_http_rejects_null_or_empty_selection(client, signal_ids):
    response = client.post(
        "/api/v1/dbr/feeder/purchase/launch",
        json={"signal_ids": signal_ids, "dry_run": True},
    )

    assert response.status_code == 422


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

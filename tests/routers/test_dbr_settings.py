import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import DbrAssemblyRate, DbrSettings, Item, ProductionResource
from app.routers.dbr import router as dbr_router
from app.services.dbr import settings_service


@pytest.fixture()
def db_session():
    """Router endpoints are sync ``def`` and run in a Starlette worker thread.
    conftest's engine uses SingletonThreadPool (a per-thread in-memory DB), so
    the request thread would not see tables created on the main thread. Use a
    StaticPool in-memory engine here so a single connection/DB is shared across
    threads for the duration of the test.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
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
    app.dependency_overrides.clear()


def _mk_resource(db, name="Участок сборки снегоходов"):
    r = ProductionResource(resource_name=name)
    db.add(r)
    db.flush()
    return r


def _mk_item(db, code="НФ-00009114", name="Снегоход"):
    i = Item(item_code=code, item_name=name)
    db.add(i)
    db.flush()
    return i


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def test_get_settings_returns_defaults_without_creating_row(client, db_session):
    resp = client.get("/api/v1/dbr/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 1
    assert data["frozen_days"] == 3
    assert data["gate_horizon_workdays"] == 10
    assert data["rt_machining_days"] == 7
    assert data["rt_welding_days"] == 15
    assert data["rt_painting_days"] == 21
    assert data["batch_days_turning"] == 10
    assert data["batch_days_bending"] == 7
    assert data["batch_days_welding"] == 5
    assert data["batch_days_paint_black"] == 2
    assert data["batch_days_paint_color"] == 3
    assert data["rt_processing_days"] == 25
    assert data["processing_trip_interval_days"] == 7
    assert data["processing_roundtrip_days"] == 14
    assert data["feeder_chain_enabled"] is False
    assert data["feeder_load_horizon_weeks"] == 4
    assert str(data["shelf_threshold_qty"]) in ("5", "5.0", "5.000")
    assert data["w2_warehouse_ref1c"] is None
    assert data["fastener_categories"] == []
    assert db_session.query(DbrSettings).count() == 0


def test_put_settings_patches_fields(client):
    resp = client.put(
        "/api/v1/dbr/settings",
        json={
            "frozen_days": 5,
            "feeder_chain_enabled": False,
            "rt_processing_days": 20,
            "processing_trip_interval_days": 5,
            "processing_roundtrip_days": 12,
            "w2_warehouse_ref1c": "WH-2-REF",
            "w3_warehouse_ref1c": "WH-3-REF",
            "fastener_categories": ["Болты", "Гайки"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["frozen_days"] == 5
    assert data["feeder_chain_enabled"] is False
    assert data["rt_processing_days"] == 20
    assert data["processing_trip_interval_days"] == 5
    assert data["processing_roundtrip_days"] == 12
    assert data["w2_warehouse_ref1c"] == "WH-2-REF"
    assert data["w3_warehouse_ref1c"] == "WH-3-REF"
    assert data["fastener_categories"] == ["Болты", "Гайки"]
    # untouched defaults remain
    assert data["gate_horizon_workdays"] == 10

    # persisted across a fresh GET
    again = client.get("/api/v1/dbr/settings").json()
    assert again["frozen_days"] == 5
    assert again["w2_warehouse_ref1c"] == "WH-2-REF"
    assert again["processing_roundtrip_days"] == 12


def test_processing_chain_preview_endpoint_is_read_only(client):
    client.put(
        "/api/v1/dbr/settings",
        json={
            "w2_warehouse_ref1c": "W2",
            "w3_warehouse_ref1c": "W3",
            "w4_warehouse_ref1c": "W4",
        },
    )

    response = client.post("/api/v1/dbr/feeder/processing/chain/preview")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "dbr_feeder_live_read_retired"


def test_processing_trip_manifest_endpoints_are_read_only_and_printable(client):
    response = client.get("/api/v1/dbr/feeder/processing/trip-manifest")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "dbr_feeder_live_read_retired"

    printable = client.get("/api/v1/dbr/feeder/processing/trip-manifest/print")
    assert printable.status_code == 503
    assert printable.json()["detail"]["operation"] == "processing_trip_manifest_print"


def test_put_settings_persists_across_independent_request_sessions(tmp_path):
    """A shared-session dependency must not mask a missing request commit."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'dbr-router.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    testing_session = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    request_sessions = []

    def override_get_db():
        db = testing_session()
        request_sessions.append(db)
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(dbr_router, prefix="/api")
    app.dependency_overrides[get_db] = override_get_db

    try:
        with TestClient(app) as independent_client:
            response = independent_client.put(
                "/api/v1/dbr/settings", json={"frozen_days": 17}
            )
            assert response.status_code == 200

            response = independent_client.get("/api/v1/dbr/settings")
            assert response.status_code == 200
            assert response.json()["frozen_days"] == 17

        assert len(request_sessions) == 2
        assert request_sessions[0] is not request_sessions[1]
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_put_settings_commit_failure_is_not_reported_as_success(db_session):
    rollback_called = False
    original_rollback = db_session.rollback

    def fail_commit():
        raise RuntimeError("controlled commit failure")

    def track_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    db_session.commit = fail_commit
    db_session.rollback = track_rollback

    def override_get_db():
        yield db_session

    app = FastAPI()
    app.include_router(dbr_router, prefix="/api")
    app.dependency_overrides[get_db] = override_get_db

    try:
        with TestClient(app, raise_server_exceptions=False) as failing_client:
            response = failing_client.put(
                "/api/v1/dbr/settings", json={"frozen_days": 19}
            )
        assert response.status_code == 500
        assert rollback_called is True
    finally:
        app.dependency_overrides.clear()


def test_put_settings_omitted_warehouse_not_cleared(client):
    client.put("/api/v1/dbr/settings", json={"w2_warehouse_ref1c": "KEEP"})
    # Second PUT that does not mention w2 must not clear it.
    client.put("/api/v1/dbr/settings", json={"frozen_days": 9})
    data = client.get("/api/v1/dbr/settings").json()
    assert data["w2_warehouse_ref1c"] == "KEEP"
    assert data["frozen_days"] == 9


# --------------------------------------------------------------------------
# Assembly rates CRUD
# --------------------------------------------------------------------------


def test_assembly_rate_crud(client, db_session):
    resource = _mk_resource(db_session)
    item = _mk_item(db_session)
    db_session.commit()

    # empty initially
    assert client.get("/api/v1/dbr/assembly-rates").json() == []

    # upsert (insert)
    resp = client.put(
        "/api/v1/dbr/assembly-rates",
        json={"resource_id": resource.resource_id, "item_id": item.item_id, "qty_per_capacity": "3.000"},
    )
    assert resp.status_code == 200
    created = resp.json()
    assert created["resource_name"] == "Участок сборки снегоходов"
    assert created["item_code"] == "НФ-00009114"
    assert str(created["qty_per_capacity"]) in ("3", "3.0", "3.000")
    rate_id = created["id"]

    listed = client.get("/api/v1/dbr/assembly-rates").json()
    assert len(listed) == 1

    # upsert (update) — same pair, new qty, no duplicate row
    resp = client.put(
        "/api/v1/dbr/assembly-rates",
        json={"resource_id": resource.resource_id, "item_id": item.item_id, "qty_per_capacity": "7.500"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == rate_id
    listed = client.get("/api/v1/dbr/assembly-rates").json()
    assert len(listed) == 1
    assert str(listed[0]["qty_per_capacity"]) in ("7.5", "7.50", "7.500")

    # delete
    resp = client.delete(f"/api/v1/dbr/assembly-rates/{rate_id}")
    assert resp.status_code == 200
    assert client.get("/api/v1/dbr/assembly-rates").json() == []

    # delete missing -> 404
    assert client.delete(f"/api/v1/dbr/assembly-rates/{rate_id}").status_code == 404


@pytest.mark.parametrize("qty_per_capacity", [0, -1, "-0.001"])
def test_assembly_rate_rejects_non_positive_qty(client, db_session, qty_per_capacity):
    resource = _mk_resource(db_session)
    item = _mk_item(db_session)
    db_session.commit()

    resp = client.put(
        "/api/v1/dbr/assembly-rates",
        json={
            "resource_id": resource.resource_id,
            "item_id": item.item_id,
            "qty_per_capacity": qty_per_capacity,
        },
    )

    assert resp.status_code == 422
    assert db_session.query(DbrAssemblyRate).count() == 0


def test_assembly_rate_service_rejects_non_positive_qty(db_session):
    resource = _mk_resource(db_session)
    item = _mk_item(db_session)

    with pytest.raises(ValueError, match="greater than zero"):
        settings_service.upsert_assembly_rate(
            db_session,
            resource_id=resource.resource_id,
            item_id=item.item_id,
            qty_per_capacity=0,
        )


# --------------------------------------------------------------------------
# Category risks
# --------------------------------------------------------------------------


def test_category_risks_replace(client):
    resp = client.put(
        "/api/v1/dbr/category-risks",
        json={
            "rows": [
                {"item_group": "Трубы круглые", "receipt_warehouse_ref1c": "WH-1", "supply_risk_pct": "30.00"},
                {"item_group": "Болт", "receipt_warehouse_ref1c": "WH-4", "supply_risk_pct": "10.00"},
            ]
        },
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    groups = {r["item_group"] for r in rows}
    assert groups == {"Трубы круглые", "Болт"}

    # update one, add another, and delete the omitted row
    resp = client.put(
        "/api/v1/dbr/category-risks",
        json={
            "rows": [
                {"item_group": "Трубы круглые", "receipt_warehouse_ref1c": "WH-9", "supply_risk_pct": "25.00"},
                {"item_group": "Гайка", "receipt_warehouse_ref1c": "WH-4", "supply_risk_pct": "10.00"},
            ]
        },
    )
    rows = {r["item_group"]: r for r in resp.json()}
    assert set(rows) == {"Трубы круглые", "Гайка"}
    assert rows["Трубы круглые"]["receipt_warehouse_ref1c"] == "WH-9"

    resp = client.put("/api/v1/dbr/category-risks", json={"rows": []})
    assert resp.status_code == 200
    assert resp.json() == []

"""Router tests for the DBR drum endpoints (programs / build / board / slots).

Uses a StaticPool in-memory engine (same rationale as test_dbr_settings): sync
endpoints run in a worker thread and must see the same in-memory DB.
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    DbrAssemblyRate,
    DefaultSpecification,
    Item,
    ItemWarehouseStock,
    ProductionResource,
    SpecComponent,
    Specification,
    StockWarehouse,
)
from app.routers.dbr import router as dbr_router

W4 = "REF-W4"


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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


@pytest.fixture()
def seed(db_session):
    db = db_session
    res = ProductionResource(resource_name="Сборка", capacity=1)
    sled = Item(item_code="SLED", item_name="Снегоход")
    bolt = Item(item_code="BOLT", item_name="Болт", replenishment_method="Закупка")
    db.add_all([res, sled, bolt, StockWarehouse(warehouse_ref1c=W4, warehouse_name="Склад №4", is_selected=True)])
    db.flush()
    db.add(DbrAssemblyRate(resource_id=res.resource_id, item_id=sled.item_id, qty_per_capacity=10))
    spec = Specification(spec_name="Спека", spec_ref1c="S-SLED")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=sled.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=bolt.item_id, quantity=Decimal("4")))
    db.add(ItemWarehouseStock(item_id=bolt.item_id, warehouse_ref1c=W4, qty=100))
    db.commit()
    return {"sled_id": sled.item_id}


def _create_and_activate(client, sled_id):
    # frozen zone off so slot moves are unconstrained in the test.
    assert client.put("/api/v1/dbr/settings", json={"frozen_days": 0, "w4_warehouse_ref1c": W4}).status_code == 200
    resp = client.post(
        "/api/v1/dbr/programs",
        json={
            "from_date": "2026-07-16",
            "to_date": "2026-08-31",
            "title": "Тест",
            "items": [{"item_id": sled_id, "program_date": "2026-07-20", "qty": 12}],
        },
    )
    assert resp.status_code == 200, resp.text
    program_id = resp.json()["id"]
    assert client.post(f"/api/v1/dbr/programs/{program_id}/approve").status_code == 200

    build = client.post("/api/v1/dbr/drum/build", json={"program_id": program_id})
    assert build.status_code == 200, build.text
    schedule_id = build.json()["schedule"]["id"]
    assert client.post(f"/api/v1/dbr/drum/{schedule_id}/activate").status_code == 200
    return program_id, schedule_id


def test_program_crud_flow(client, seed):
    resp = client.post(
        "/api/v1/dbr/programs",
        json={"from_date": "2026-08-01", "to_date": "2026-08-31", "items": []},
    )
    assert resp.status_code == 200
    pid = resp.json()["id"]
    # cannot approve empty
    assert client.post(f"/api/v1/dbr/programs/{pid}/approve").status_code == 400
    # add an item, then approve
    upd = client.put(
        f"/api/v1/dbr/programs/{pid}",
        json={"items": [{"item_id": seed["sled_id"], "program_date": "2026-08-05", "qty": 3}]},
    )
    assert upd.status_code == 200 and len(upd.json()["items"]) == 1
    assert client.post(f"/api/v1/dbr/programs/{pid}/approve").json()["status"] == "approved"


def test_build_activate_board(client, seed):
    _, schedule_id = _create_and_activate(client, seed["sled_id"])
    board = client.get("/api/v1/dbr/drum/active/board")
    assert board.status_code == 200
    body = board.json()
    assert body["schedule"]["id"] == schedule_id
    assert body["slots"], "board should carry slots"
    assert sum(round(s["qty"]) for s in body["slots"]) == 12
    assert body["kpi"]["plan_qty"] == 12.0


def test_refresh_gate_endpoint(client, seed):
    _, schedule_id = _create_and_activate(client, seed["sled_id"])
    resp = client.post(f"/api/v1/dbr/drum/{schedule_id}/refresh-gate")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"updated", "green", "yellow", "red", "notes"}


def test_move_slot(client, seed):
    _, schedule_id = _create_and_activate(client, seed["sled_id"])
    slots = client.get("/api/v1/dbr/drum/active/board").json()["slots"]
    slot_id = slots[0]["id"]
    resp = client.post(f"/api/v1/dbr/drum/slots/{slot_id}/move", json={"new_date": "2026-08-10"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["moved"] is True


def test_release_is_stub(client, seed):
    _, schedule_id = _create_and_activate(client, seed["sled_id"])
    slots = client.get("/api/v1/dbr/drum/active/board").json()["slots"]
    slot_id = slots[0]["id"]
    resp = client.post(f"/api/v1/dbr/drum/slots/{slot_id}/release")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stub"] is True and body["release_status"] == "released"


def test_roll_forward_endpoint(client, seed):
    _, schedule_id = _create_and_activate(client, seed["sled_id"])
    resp = client.post(f"/api/v1/dbr/drum/{schedule_id}/roll-forward")
    assert resp.status_code == 200
    assert "moved" in resp.json()

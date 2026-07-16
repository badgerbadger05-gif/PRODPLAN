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
    DbrSupermarketPosition,
    DefaultSpecification,
    Item,
    ItemWarehouseStock,
    ProductionResource,
    SpecComponent,
    Specification,
    StockWarehouse,
)
from app.routers.dbr import router as dbr_router

W2 = "REF-W2"
W3 = "REF-W3"
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
    other_res = ProductionResource(resource_name="Другая сборка", capacity=1)
    sled = Item(item_code="SLED", item_name="Снегоход")
    bolt = Item(item_code="BOLT", item_name="Болт", replenishment_method="Закупка")
    db.add_all(
        [
            res,
            other_res,
            sled,
            bolt,
            StockWarehouse(
                warehouse_ref1c=W4,
                warehouse_name="Склад №4",
                is_selected=True,
            ),
        ]
    )
    db.flush()
    db.add(DbrAssemblyRate(resource_id=res.resource_id, item_id=sled.item_id, qty_per_capacity=10))
    spec = Specification(spec_name="Спека", spec_ref1c="S-SLED")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=sled.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=bolt.item_id, quantity=Decimal("4")))
    db.add(ItemWarehouseStock(item_id=bolt.item_id, warehouse_ref1c=W4, qty=100))
    db.commit()
    return {"sled_id": sled.item_id, "unassigned_resource_id": other_res.resource_id}


def _create_and_activate(client, sled_id):
    # frozen zone off so slot moves are unconstrained in the test.
    assert client.put(
        "/api/v1/dbr/settings",
        json={
            "frozen_days": 0,
            "w2_warehouse_ref1c": W2,
            "w3_warehouse_ref1c": W3,
            "w4_warehouse_ref1c": W4,
        },
    ).status_code == 200
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
    assert upd.json()["items"][0]["item_code"] == "SLED"
    assert upd.json()["items"][0]["item_name"] == "Снегоход"
    assert client.post(f"/api/v1/dbr/programs/{pid}/approve").json()["status"] == "approved"


def test_program_api_rejects_invalid_and_duplicate_items(client, seed):
    base = {
        "from_date": "2026-08-01",
        "to_date": "2026-08-31",
    }
    outside = client.post(
        "/api/v1/dbr/programs",
        json={**base, "items": [{"item_id": seed["sled_id"], "program_date": "2026-09-01", "qty": 1}]},
    )
    assert outside.status_code == 400
    assert "вне периода" in outside.json()["detail"]

    duplicate_row = {"item_id": seed["sled_id"], "program_date": "2026-08-05", "qty": 1}
    duplicate = client.post(
        "/api/v1/dbr/programs",
        json={**base, "items": [duplicate_row, {**duplicate_row, "qty": 2}]},
    )
    assert duplicate.status_code == 400
    assert "дубликат строки" in duplicate.json()["detail"]


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


def test_refresh_gate_rejects_missing_required_warehouse_role(client, seed):
    _, schedule_id = _create_and_activate(client, seed["sled_id"])
    assert client.put(
        "/api/v1/dbr/settings", json={"w3_warehouse_ref1c": None}
    ).status_code == 200

    response = client.post(f"/api/v1/dbr/drum/{schedule_id}/refresh-gate")

    assert response.status_code == 400
    assert "склад №3 (W3)" in response.json()["detail"]


def test_move_slot(client, seed):
    _, schedule_id = _create_and_activate(client, seed["sled_id"])
    slots = client.get("/api/v1/dbr/drum/active/board").json()["slots"]
    slot_id = slots[0]["id"]
    resp = client.post(f"/api/v1/dbr/drum/slots/{slot_id}/move", json={"new_date": "2026-08-10"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["moved"] is True


def test_move_slot_rejects_resource_not_assigned_to_sku(client, seed):
    _, _schedule_id = _create_and_activate(client, seed["sled_id"])
    slot = client.get("/api/v1/dbr/drum/active/board").json()["slots"][0]

    response = client.post(
        f"/api/v1/dbr/drum/slots/{slot['id']}/move",
        json={
            "new_date": "2026-08-10",
            "new_resource_id": seed["unassigned_resource_id"],
        },
    )

    assert response.status_code == 400
    assert "не назначено" in response.json()["detail"]
    unchanged = client.get("/api/v1/dbr/drum/active/board").json()["slots"][0]
    assert unchanged["resource_id"] == slot["resource_id"]


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


def test_feeder_positions_preview_rebuild_and_list(client, db_session, seed):
    _, schedule_id = _create_and_activate(client, seed["sled_id"])

    preview = client.post(
        "/api/v1/dbr/feeder/positions/preview", json={"schedule_id": schedule_id}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["positions"]
    assert db_session.query(DbrSupermarketPosition).count() == 0

    rebuilt = client.post(
        "/api/v1/dbr/feeder/positions/rebuild",
        json={
            "schedule_id": schedule_id,
            "expected_schedule_id": schedule_id,
        },
    )
    assert rebuilt.status_code == 200, rebuilt.text
    assert rebuilt.json()["created"] >= 1

    listed = client.get("/api/v1/dbr/feeder/positions?active_only=true")
    assert listed.status_code == 200
    assert len(listed.json()) == rebuilt.json()["created"]

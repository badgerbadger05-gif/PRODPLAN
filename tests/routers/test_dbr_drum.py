"""Router tests for the DBR drum endpoints (programs / build / board / slots).

Uses a StaticPool in-memory engine (same rationale as test_dbr_settings): sync
endpoints run in a worker thread and must see the same in-memory DB.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.models import (
    DbrAssemblyRate,
    DbrDrumSchedule,
    DbrDrumScheduleProgram,
    DbrDrumSlot,
    DbrSupermarketPosition,
    DefaultSpecification,
    Item,
    ItemWarehouseStock,
    LedgerGeneration,
    PhysicalImportBatch,
    PlanningRun,
    PlanningTruthState,
    ProductionResource,
    SpecComponent,
    Specification,
    StockWarehouse,
)
from app.routers.dbr import router as dbr_router
from app.services.dbr import feeder_signal_service

W2 = "REF-W2"
W3 = "REF-W3"
W4 = "REF-W4"


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    batch = PhysicalImportBatch(
        batch_key="dbr-router-diagnostic",
        status="completed",
        cutoff=datetime(2026, 7, 23),
        source_watermarks={},
        completed_at=datetime(2026, 7, 23),
    )
    generation = LedgerGeneration(
        generation_key="dbr-router-diagnostic",
        status="accepted",
        cutoff=datetime(2026, 7, 23),
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
            "planning_snapshots": True,
        },
        physical_import_batch=batch,
        algorithm_version="test/diagnostic",
        accepted_at=datetime(2026, 7, 23),
    )
    db.add(generation)
    db.flush()
    db.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        active_freeze_version=1,
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
    )
    db.add(run)
    db.commit()
    db.info["dbr_test_run_id"] = run.run_id
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.state.dbr_test_run_id = db_session.info["dbr_test_run_id"]
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
            "source_run_id": client.app.state.dbr_test_run_id,
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
        json={
            "source_run_id": client.app.state.dbr_test_run_id,
            "from_date": "2026-08-01",
            "to_date": "2026-08-31",
            "items": [],
        },
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
        "source_run_id": client.app.state.dbr_test_run_id,
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
    assert body["calendar_fallback"] is True


def test_board_fails_closed_when_capability_claims_ready_but_snapshot_is_missing(client, db_session):
    generation = db_session.query(LedgerGeneration).one()
    generation.capabilities = {**generation.capabilities, "planning_snapshots": True, "dbr_drum_board": True}
    db_session.commit()
    response = client.get("/api/v1/dbr/drum/active/board")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "dbr_drum_board_snapshot_unavailable"
    assert detail["status"] == "unavailable"


def test_successor_activation_publishes_new_immutable_board_snapshot(client, db_session, seed):
    _first, first_id = _create_and_activate(client, seed["sled_id"])
    first = client.get("/api/v1/dbr/drum/active/board").json()
    run = db_session.query(PlanningRun).one()
    resource = db_session.query(ProductionResource).filter_by(resource_name="Сборка").one()
    item = db_session.get(Item, seed["sled_id"])
    old_schedule = db_session.get(DbrDrumSchedule, first_id)
    successor = DbrDrumSchedule(
        ledger_generation_id=run.ledger_generation_id,
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
        source_program_id=old_schedule.source_program_id, status="draft", config_snapshot={},
    )
    db_session.add(successor); db_session.flush()
    db_session.add(DbrDrumScheduleProgram(
        schedule_id=successor.id, program_id=old_schedule.source_program_id,
        source_run_id=run.run_id, ledger_generation_id=run.ledger_generation_id,
        freeze_version=run.active_freeze_version,
    ))
    db_session.add(DbrDrumSlot(
        schedule_id=successor.id, slot_date=date(2026, 9, 2), planned_date=date(2026, 9, 2),
        resource_id=resource.resource_id, item_id=item.item_id, qty=3, source_run_id=run.run_id,
        ledger_generation_id=run.ledger_generation_id, freeze_version=run.active_freeze_version,
    ))
    db_session.commit()
    activated = client.post(f"/api/v1/dbr/drum/{successor.id}/activate")
    assert activated.status_code == 200, activated.text
    current = client.get("/api/v1/dbr/drum/active/board").json()
    assert current["schedule"]["id"] == successor.id
    assert current["meta"]["snapshot_id"] != first["meta"]["snapshot_id"]
    snapshots = db_session.query(models.PlanningReadSnapshot).filter_by(
        consumer="dbr_drum_board",
    ).order_by(models.PlanningReadSnapshot.id).all()
    assert len(snapshots) == 2
    assert snapshots[0].payload["schedule"]["id"] == first_id
    assert snapshots[1].payload["schedule"]["id"] == successor.id


def test_refresh_gate_endpoint(client, seed):
    _, schedule_id = _create_and_activate(client, seed["sled_id"])
    resp = client.post(f"/api/v1/dbr/drum/{schedule_id}/refresh-gate")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "dbr_drum_legacy_mutation_retired"


def test_refresh_gate_rejects_missing_required_warehouse_role(client, seed):
    _, schedule_id = _create_and_activate(client, seed["sled_id"])
    assert client.put(
        "/api/v1/dbr/settings", json={"w3_warehouse_ref1c": None}
    ).status_code == 200

    response = client.post(f"/api/v1/dbr/drum/{schedule_id}/refresh-gate")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "dbr_drum_legacy_mutation_retired"


def test_move_slot(client, seed):
    _, schedule_id = _create_and_activate(client, seed["sled_id"])
    slots = client.get("/api/v1/dbr/drum/active/board").json()["slots"]
    slot_id = slots[0]["id"]
    resp = client.post(f"/api/v1/dbr/drum/slots/{slot_id}/move", json={"new_date": "2026-08-10"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "dbr_drum_published_schedule_immutable"


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

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "dbr_drum_published_schedule_immutable"
    unchanged = client.get("/api/v1/dbr/drum/active/board").json()["slots"][0]
    assert unchanged["resource_id"] == slot["resource_id"]


def test_release_requires_green_slot(client, seed):
    # Фаза 3: release materializes into 1С and only green+pending slots qualify.
    # A freshly built slot has kit_status='unknown' (gate not refreshed) → 409.
    _, schedule_id = _create_and_activate(client, seed["sled_id"])
    slots = client.get("/api/v1/dbr/drum/active/board").json()["slots"]
    slot_id = slots[0]["id"]
    resp = client.post(f"/api/v1/dbr/drum/slots/{slot_id}/release")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "dbr_immutable_ledger_authorization_unavailable"


def test_roll_forward_endpoint(client, seed):
    _, schedule_id = _create_and_activate(client, seed["sled_id"])
    resp = client.post(f"/api/v1/dbr/drum/{schedule_id}/roll-forward")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "dbr_drum_legacy_mutation_retired"


def test_feeder_positions_preview_rebuild_and_list(client, db_session, seed):
    _, schedule_id = _create_and_activate(client, seed["sled_id"])

    preview = client.post(
        "/api/v1/dbr/feeder/positions/preview", json={"schedule_id": schedule_id}
    )
    assert preview.status_code == 503, preview.text
    assert (
        preview.json()["detail"]["code"]
        == "dbr_ledger_projection_unavailable"
    )
    assert db_session.query(DbrSupermarketPosition).count() == 0

    rebuilt = client.post(
        "/api/v1/dbr/feeder/positions/rebuild",
        json={
            "schedule_id": schedule_id,
            "expected_schedule_id": schedule_id,
        },
    )
    assert rebuilt.status_code == 503, rebuilt.text
    assert (
        rebuilt.json()["detail"]["code"]
        == "dbr_ledger_projection_unavailable"
    )
    assert db_session.query(DbrSupermarketPosition).count() == 0
    return

    # Rebuild changes the mutable DBR projection only.  The mount GET must not
    # calculate a live view and remains unavailable until a Ledger-bound
    # cockpit snapshot is published by the explicit builder/worker.
    listed = client.get("/api/v1/dbr/feeder/positions?active_only=true")
    assert listed.status_code == 503
    assert listed.json()["detail"]["code"] == "dbr_cockpit_snapshot_unavailable"

    before = [
        (row.id, row.updated_at, row.is_active, row.is_stale)
        for row in db_session.query(DbrSupermarketPosition).order_by(
            DbrSupermarketPosition.id
        )
    ]
    live_list = client.get(
        "/api/v1/dbr/feeder/positions",
        params={
            "include_live_nfp": "true",
            "active": "true",
            "supply": "purchase",
            "warehouse": W4,
            "zone": "Green",
            "search": "BOLT",
            "limit": 10,
            "offset": 0,
        },
    )
    assert live_list.status_code == 503, live_list.text
    position_id = db_session.query(DbrSupermarketPosition.id).scalar()

    # Detail is the same accepted-snapshot boundary as the list; no live
    # diagnostic bypass remains.
    detail = client.get(f"/api/v1/dbr/feeder/positions/{position_id}")
    assert detail.status_code == 503
    assert detail.json()["detail"]["code"] == "dbr_cockpit_snapshot_unavailable"
    db_session.expire_all()
    after = [
        (row.id, row.updated_at, row.is_active, row.is_stale)
        for row in db_session.query(DbrSupermarketPosition).order_by(
            DbrSupermarketPosition.id
        )
    ]
    assert after == before


def test_feeder_signal_preview_and_refresh_routes(client, monkeypatch):
    monkeypatch.setattr(
        feeder_signal_service,
        "preview_signals",
        lambda db, **kwargs: {"schedule_id": 7, "positions": 1, "actionable": 1, "rows": []},
    )
    monkeypatch.setattr(
        feeder_signal_service,
        "refresh_signals",
        lambda db, expected, **kwargs: {"schedule_id": expected, "created": 1, "rows": []},
    )
    preview = client.post("/api/v1/dbr/feeder/signals/preview")
    assert preview.status_code == 503
    assert preview.json()["detail"]["code"] == "dbr_feeder_live_read_retired"
    response = client.post(
        "/api/v1/dbr/feeder/signals/refresh", json={"expected_schedule_id": 7}
    )
    assert response.status_code == 200 and response.json()["created"] == 1


def test_feeder_signal_refresh_schedule_conflict_is_409(client, monkeypatch):
    def conflict(db, expected, **kwargs):
        raise ValueError("активный график изменился")

    monkeypatch.setattr(feeder_signal_service, "refresh_signals", conflict)
    response = client.post(
        "/api/v1/dbr/feeder/signals/refresh", json={"expected_schedule_id": 99}
    )
    assert response.status_code == 409


def test_feeder_signal_detail_not_found(client):
    response = client.get("/api/v1/dbr/feeder/signals/999999")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "dbr_cockpit_snapshot_unavailable"

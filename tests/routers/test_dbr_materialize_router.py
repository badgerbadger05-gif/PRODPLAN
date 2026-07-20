"""Router tests for the Фаза 3 materialization endpoints."""

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
    DbrDrumSchedule,
    DbrDrumSlot,
    DefaultSpecification,
    Item,
    ProductionResource,
    SpecComponent,
    Specification,
    WorkshopWarehouseBinding,
)
from app.routers.dbr import router as dbr_router
from app.services.dbr import materialize_service, settings_service


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
def client(db_session, monkeypatch):
    monkeypatch.setattr(
        materialize_service, "_load_odata_config",
        lambda: {"base_url": "http://demo/odata/unf_demo", "username": "u", "password": "p"},
    )
    app = FastAPI()
    app.include_router(dbr_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _slot(db, *, kit_status="green"):
    settings_service.get_or_create_settings(db)
    res = ProductionResource(resource_name="Сборка", capacity=1)
    item = Item(item_code="SLED", item_name="Снегоход", item_ref1c="item-ref", unit="unit-ref")
    db.add_all([res, item])
    db.flush()
    spec = Specification(spec_name="S", spec_ref1c="spec-ref")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db.add(WorkshopWarehouseBinding(
        workshop_id=res.resource_id, warehouse_ref1c="wip", production_warehouse_ref1c="prod"
    ))
    schedule = DbrDrumSchedule(period_from=date(2026, 8, 1), period_to=date(2026, 8, 31), status="active")
    db.add(schedule)
    db.flush()
    slot = DbrDrumSlot(
        schedule_id=schedule.id, slot_date=date(2026, 8, 10), planned_date=date(2026, 8, 10),
        resource_id=res.resource_id, item_id=item.item_id, qty=Decimal("2"), kit_status=kit_status,
    )
    db.add(slot)
    db.commit()
    return slot, schedule


def test_release_endpoint_defaults_to_dry_run(client, db_session):
    slot, _sch = _slot(db_session)
    resp = client.post(f"/api/v1/dbr/drum/slots/{slot.id}/release")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["created"] is False
    assert "payload" in body
    db_session.refresh(slot)
    assert slot.release_status == "pending"


def test_release_endpoint_red_slot_returns_409(client, db_session):
    slot, _sch = _slot(db_session, kit_status="red")
    resp = client.post(f"/api/v1/dbr/drum/slots/{slot.id}/release?dry_run=true")
    assert resp.status_code == 409
    assert "message" in resp.json()["detail"]


def test_release_day_endpoint_dry_run(client, db_session):
    slot, schedule = _slot(db_session)
    resp = client.post(
        f"/api/v1/dbr/drum/{schedule.id}/release-day",
        json={"day": "2026-08-10", "dry_run": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["previews"] == 1
    assert body["slots_total"] == 1


def test_launch_endpoint_missing_signal_404(client):
    resp = client.post("/api/v1/dbr/feeder/signals/999999/launch", json={"dry_run": True})
    assert resp.status_code == 404

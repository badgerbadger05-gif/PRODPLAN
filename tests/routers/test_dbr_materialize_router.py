"""Router tests for the Фаза 3 materialization endpoints."""

from datetime import date, datetime
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
    LedgerGeneration,
    PhysicalImportBatch,
    PlanningRun,
    PlanningTruthState,
    ProductionResource,
    SpecComponent,
    Specification,
    WorkshopWarehouseBinding,
)
from app.routers.dbr import router as dbr_router
from app.services.dbr import materialize_service, purchase_materialize_service, settings_service


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    cutoff = datetime(2026, 7, 23)
    batch = PhysicalImportBatch(
        batch_key="dbr-materialize-router",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    generation = LedgerGeneration(
        generation_key="dbr-materialize-router",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
        physical_import_batch=batch,
        algorithm_version="test/router",
    )
    db.add(generation)
    db.flush()
    db.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        active_freeze_version=1,
        ledger_generation_id=generation.id,
        ledger_cutoff=cutoff,
    )
    db.add(run)
    db.commit()
    db.info["dbr_lineage"] = (run.run_id, generation.id, 1)
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
    run_id, generation_id, freeze_version = db.info["dbr_lineage"]
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
    schedule = DbrDrumSchedule(
        ledger_generation_id=generation_id,
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="active",
    )
    db.add(schedule)
    db.flush()
    slot = DbrDrumSlot(
        schedule_id=schedule.id, slot_date=date(2026, 8, 10), planned_date=date(2026, 8, 10),
        resource_id=res.resource_id, item_id=item.item_id, qty=Decimal("2"), kit_status=kit_status,
        source_run_id=run_id,
        ledger_generation_id=generation_id,
        freeze_version=freeze_version,
    )
    db.add(slot)
    db.commit()
    return slot, schedule


def test_release_endpoint_is_retired_without_invoking_materializer(client, db_session, monkeypatch):
    slot, _sch = _slot(db_session)
    monkeypatch.setattr(materialize_service, "release_slot", lambda *_a, **_k: pytest.fail("must not materialize"))
    resp = client.post(f"/api/v1/dbr/drum/slots/{slot.id}/release")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "dbr_immutable_ledger_authorization_unavailable"
    db_session.refresh(slot)
    assert slot.release_status == "pending"


def test_release_endpoint_red_slot_is_retired(client, db_session):
    slot, _sch = _slot(db_session, kit_status="red")
    resp = client.post(f"/api/v1/dbr/drum/slots/{slot.id}/release?dry_run=true")
    assert resp.status_code == 503


def test_release_day_endpoint_is_retired_without_invoking_materializer(client, db_session, monkeypatch):
    slot, schedule = _slot(db_session)
    before = db_session.query(DbrDrumSlot).count()
    monkeypatch.setattr(materialize_service, "release_day", lambda *_a, **_k: pytest.fail("must not materialize"))
    resp = client.post(
        f"/api/v1/dbr/drum/{schedule.id}/release-day",
        json={"day": "2026-08-10", "dry_run": True},
    )
    assert resp.status_code == 503
    assert db_session.query(DbrDrumSlot).count() == before
    db_session.refresh(slot)
    assert slot.release_status == "pending"


def test_launch_endpoint_is_retired_without_invoking_materializer(client, db_session, monkeypatch):
    before = db_session.query(DbrDrumSlot).count()
    monkeypatch.setattr(materialize_service, "launch_signal", lambda *_a, **_k: pytest.fail("must not materialize"))
    resp = client.post("/api/v1/dbr/feeder/signals/999999/launch", json={"dry_run": True})
    assert resp.status_code == 503
    assert db_session.query(DbrDrumSlot).count() == before


def test_purchase_launch_is_retired_without_invoking_materializer(client, db_session, monkeypatch):
    before = db_session.query(DbrDrumSlot).count()
    monkeypatch.setattr(
        purchase_materialize_service,
        "launch_purchase_signals",
        lambda *_a, **_k: pytest.fail("must not materialize purchase signals"),
    )

    response = client.post(
        "/api/v1/dbr/feeder/purchase/launch",
        json={"signal_ids": [1], "dry_run": False},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "dbr_immutable_ledger_authorization_unavailable"
    assert db_session.query(DbrDrumSlot).count() == before

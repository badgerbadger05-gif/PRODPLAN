"""POST /api/v1/item-ledger/admin/physical-refresh/discard.

``discard_physical_refresh_candidate`` was written and tested but had no caller
at all, so a refresh that failed convergence kept its physical import batches
above the accepted parent's boundary and every later fork failed its own audit:
one bad three-hour cycle blocked the pipeline until someone unpicked it by hand.
"""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
import app.routers.item_ledger_admin as admin_mod
from app.services.item_ledger.physical_visibility import visible_sle_query

from tests.services.test_physical_refresh_discard import CUTOFF, _world


@pytest.fixture(autouse=True)
def _admin_perimeter(monkeypatch):
    monkeypatch.setenv("ITEM_LEDGER_ADMIN_TOKEN", "test-admin-token")


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _client(db):
    app = FastAPI()
    app.include_router(admin_mod.router, prefix="/api")

    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    return TestClient(app, headers={"X-PRODPLAN-ADMIN-TOKEN": "test-admin-token"})


def test_discard_endpoint_rolls_the_candidate_back_to_the_parent_boundary(db_session):
    parent, candidate, _kept, _item = _world(db_session)
    parent_boundary = int(parent.physical_import_batch_id)
    before = visible_sle_query(
        db_session, physical_import_batch_id=parent_boundary
    ).all()

    response = _client(db_session).post(
        "/api/v1/item-ledger/admin/physical-refresh/discard",
        json={
            "ledger_generation_id": int(candidate.id),
            "reason": "convergence failed; unblock the next refresh",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert body["parent_generation_id"] == int(parent.id)
    assert body["boundary_after"] == parent_boundary
    assert body["boundary_before"] > parent_boundary
    db_session.expire_all()
    assert db_session.get(models.LedgerGeneration, candidate.id).status == "rejected"
    # The pipeline is unblocked: the global terminal is the parent boundary again.
    assert db_session.query(models.PhysicalImportBatch).filter(
        models.PhysicalImportBatch.id > parent_boundary
    ).count() == 0
    after = visible_sle_query(
        db_session, physical_import_batch_id=parent_boundary
    ).all()
    assert [(row.recorder_ref, row.qty) for row in after] == [
        (row.recorder_ref, row.qty) for row in before
    ]


def test_discard_endpoint_refuses_the_current_accepted_truth(db_session):
    parent, _candidate, _kept, _item = _world(db_session)

    response = _client(db_session).post(
        "/api/v1/item-ledger/admin/physical-refresh/discard",
        json={
            "ledger_generation_id": int(parent.id),
            "reason": "should never be allowed",
        },
    )

    assert response.status_code == 409
    assert "ACCEPTED" in response.json()["detail"]
    db_session.expire_all()
    assert db_session.get(models.LedgerGeneration, parent.id).status == "accepted"


def test_discard_endpoint_requires_a_reason(db_session):
    _parent, candidate, _kept, _item = _world(db_session)

    response = _client(db_session).post(
        "/api/v1/item-ledger/admin/physical-refresh/discard",
        json={"ledger_generation_id": int(candidate.id), "reason": "   "},
    )

    assert response.status_code == 409
    db_session.expire_all()
    assert db_session.get(models.LedgerGeneration, candidate.id).status == "building"


def test_discard_endpoint_is_behind_the_admin_perimeter(db_session):
    _parent, candidate, _kept, _item = _world(db_session)
    app = FastAPI()
    app.include_router(admin_mod.router, prefix="/api")

    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    response = TestClient(app).post(
        "/api/v1/item-ledger/admin/physical-refresh/discard",
        json={"ledger_generation_id": int(candidate.id), "reason": "no token"},
    )

    assert response.status_code == 401

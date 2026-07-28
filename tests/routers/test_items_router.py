"""Contract tests for /api/v1/items partial updates.

PUT applies the whole `ItemUpdate`, so an omitted `stock_qty` falls back to its
0.0 default. PATCH exists so a browser can edit one planning attribute without
resending — and therefore without being able to clobber — the physical stock the
1C sync and the Item Ledger own.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.routers.items import router as items_router


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
    app.include_router(items_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def item(db_session):
    row = models.Item(
        item_code="000001",
        item_name="Кронштейн",
        item_article="ART-1",
        unit="шт",
        stock_qty=17.5,
        optimal_batch=None,
        status="active",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def test_patch_writes_only_the_sent_field(client, db_session, item):
    response = client.patch(
        f"/api/v1/items/{item.item_id}",
        json={"optimal_batch": 24},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["optimal_batch"] == 24
    # Everything else, physical stock first of all, is untouched.
    assert body["stock_qty"] == 17.5
    assert body["item_code"] == "000001"
    assert body["item_name"] == "Кронштейн"
    assert body["item_article"] == "ART-1"
    assert body["unit"] == "шт"
    assert body["status"] == "active"

    db_session.expire_all()
    stored = db_session.get(models.Item, item.item_id)
    assert float(stored.optimal_batch) == 24
    assert float(stored.stock_qty) == 17.5


def test_patch_rejects_stock_qty(client, db_session, item):
    response = client.patch(
        f"/api/v1/items/{item.item_id}",
        json={"optimal_batch": 5, "stock_qty": 0},
    )

    assert response.status_code == 422, response.text
    db_session.expire_all()
    stored = db_session.get(models.Item, item.item_id)
    # The whole patch is refused, not partially applied.
    assert stored.optimal_batch is None
    assert float(stored.stock_qty) == 17.5


def test_patch_rejects_unknown_field(client, item):
    response = client.patch(
        f"/api/v1/items/{item.item_id}",
        json={"stock_quantity": 3},
    )

    assert response.status_code == 422, response.text


def test_patch_clears_a_nullable_field_when_explicitly_sent(client, db_session, item):
    client.patch(f"/api/v1/items/{item.item_id}", json={"optimal_batch": 24})

    response = client.patch(
        f"/api/v1/items/{item.item_id}",
        json={"optimal_batch": None},
    )

    assert response.status_code == 200, response.text
    assert response.json()["optimal_batch"] is None
    assert response.json()["stock_qty"] == 17.5


def test_patch_missing_item_is_404(client):
    response = client.patch("/api/v1/items/424242", json={"optimal_batch": 1})

    assert response.status_code == 404


def test_put_still_replaces_the_whole_record(client, db_session, item):
    """Guards the reason PATCH exists: PUT defaults `stock_qty` to 0.0."""
    response = client.put(
        f"/api/v1/items/{item.item_id}",
        json={"item_code": "000001", "item_name": "Кронштейн", "optimal_batch": 24},
    )

    assert response.status_code == 200, response.text
    assert response.json()["stock_qty"] == 0.0

"""Paint↔weld registry endpoints: list, rebuild, orphans, manual CRUD, guard."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    DefaultSpecification,
    Item,
    PaintWeldPair,
    SpecComponent,
    Specification,
)
from app.routers.paint_weld import router


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


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _item(db, code, name, method="Производство"):
    item = Item(
        item_code=code, item_name=name, item_article=code, unit="шт",
        stock_qty=0, replenishment_method=method, replenishment_time=0, status="active",
    )
    db.add(item)
    db.flush()
    return item


def _seed_pair(db):
    painted = _item(db, "P1", "Вал, после покраски")
    welded = _item(db, "W1", "Вал, после сварки")
    spec = Specification(spec_code="s1", spec_name="s1", spec_ref1c="s1")
    db.add(spec)
    db.flush()
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=welded.item_id, quantity=1, component_type="Сборка"))
    db.add(DefaultSpecification(item_id=painted.item_id, spec_id=spec.spec_id))
    db.commit()
    return painted, welded


def test_rebuild_then_list(client, db_session):
    painted, welded = _seed_pair(db_session)

    r = client.post("/api/v1/paint-weld/pairs/rebuild")
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 1
    assert body["active_pairs"] == 1

    r = client.get("/api/v1/paint-weld/pairs")
    pairs = r.json()["pairs"]
    assert len(pairs) == 1
    assert pairs[0]["painted_item_id"] == painted.item_id
    assert pairs[0]["welded_item_id"] == welded.item_id
    assert pairs[0]["source"] == "auto"


def test_orphans_endpoint(client, db_session):
    _seed_pair(db_session)
    orphan = _item(db_session, "O1", "Балка, после сварки", method="Производство")
    db_session.commit()
    client.post("/api/v1/paint-weld/pairs/rebuild")

    r = client.get("/api/v1/paint-weld/orphans")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["examples"][0]["item_id"] == orphan.item_id


def test_manual_put_and_delete(client, db_session):
    painted = _item(db_session, "P2", "Ось, после покраски")
    welded = _item(db_session, "W2", "Ось, после сварки")
    db_session.commit()

    r = client.put(
        "/api/v1/paint-weld/pairs",
        json={"painted_item_id": painted.item_id, "welded_item_id": welded.item_id},
    )
    assert r.status_code == 200
    pair_id = r.json()["id"]
    assert r.json()["source"] == "manual"

    r = client.delete(f"/api/v1/paint-weld/pairs/{pair_id}")
    assert r.status_code == 200
    assert r.json()["is_active"] is False
    assert db_session.get(PaintWeldPair, pair_id).is_active is False


def test_manual_put_unknown_item_returns_400(client, db_session):
    painted = _item(db_session, "P3", "Гайка, после покраски")
    db_session.commit()
    r = client.put(
        "/api/v1/paint-weld/pairs",
        json={"painted_item_id": painted.item_id, "welded_item_id": 999999},
    )
    assert r.status_code == 400


def test_delete_unknown_pair_returns_404(client):
    r = client.delete("/api/v1/paint-weld/pairs/424242")
    assert r.status_code == 404


def test_guard_endpoint(client, db_session):
    painted, welded = _seed_pair(db_session)
    welded.stock_qty = 20
    db_session.commit()
    client.post("/api/v1/paint-weld/pairs/rebuild")

    r = client.get(f"/api/v1/paint-weld/guard?painted_item_id={painted.item_id}&qty=5")
    assert r.status_code == 200
    assert r.json()["verdict"] == "stock_covers"

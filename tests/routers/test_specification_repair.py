"""HTTP API ремонтного модуля (операция A): restage / move / add."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import Item, ProductionStage, SpecComponent, Specification
from app.routers.specification_repair import router

API = "/api/v1/specification-repair"


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
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _seed_move(db):
    a = Specification(spec_code="A", spec_name="A", spec_ref1c="a")
    b = Specification(spec_code="B", spec_name="B", spec_ref1c="b")
    part = Item(item_code="P", item_name="Деталь", item_ref1c="p")
    db.add_all([a, b, part])
    db.commit()
    comp = SpecComponent(spec_id=a.spec_id, item_id=part.item_id, quantity=1, component_type="Сборка")
    db.add(comp)
    db.commit()
    return a, b, part, comp


def test_move_dry_run_previews_without_change(client, db_session):
    a, b, part, comp = _seed_move(db_session)

    resp = client.post(f"{API}/move", json={"component_id": comp.component_id, "target_spec_id": b.spec_id})

    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True and body["to_spec_id"] == b.spec_id
    assert "pending_1c" in body and set(body["pending_1c"]["specs"]) == {a.spec_id, b.spec_id}
    # dry-run не изменил БД
    assert db_session.query(SpecComponent).filter_by(spec_id=a.spec_id).count() == 1
    assert db_session.query(SpecComponent).filter_by(spec_id=b.spec_id).count() == 0


def test_move_apply_relocates(client, db_session):
    a, b, part, comp = _seed_move(db_session)

    resp = client.post(
        f"{API}/move",
        json={"component_id": comp.component_id, "target_spec_id": b.spec_id, "dry_run": False},
    )

    assert resp.status_code == 200
    assert db_session.query(SpecComponent).filter_by(spec_id=a.spec_id).count() == 0
    assert db_session.query(SpecComponent).filter_by(spec_id=b.spec_id).count() == 1


def test_move_same_spec_returns_400(client, db_session):
    a, b, part, comp = _seed_move(db_session)
    resp = client.post(f"{API}/move", json={"component_id": comp.component_id, "target_spec_id": a.spec_id})
    assert resp.status_code == 400


def test_add_apply_inserts(client, db_session):
    sp = Specification(spec_code="S", spec_name="S", spec_ref1c="s")
    it = Item(item_code="X", item_name="X", item_ref1c="x")
    db_session.add_all([sp, it])
    db_session.commit()

    resp = client.post(
        f"{API}/add",
        json={"spec_id": sp.spec_id, "item_id": it.item_id, "quantity": 3, "dry_run": False},
    )

    assert resp.status_code == 200
    assert db_session.query(SpecComponent).filter_by(spec_id=sp.spec_id, item_id=it.item_id).count() == 1

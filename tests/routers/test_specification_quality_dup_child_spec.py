"""Этап 0: правило DUPLICATE_COMPONENT учитывает закреплённую спеку компонента.

Один и тот же компонент с разными Спецификация_Key (Сборка/Узел) — легальная
многоуровневость 1С, не дубль. Тот же компонент с одинаковой (или пустой) спекой
дважды — настоящий дубль.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import Item, SpecComponent, Specification
from app.routers.specification import router


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


def _seed(db, child_spec_refs):
    """Корень ROOT со спекой s-root и одним компонентом COMP, повторённым строками
    с указанными закреплёнными спеками."""
    root = Item(item_code="ROOT", item_name="Изделие", item_ref1c="r1", unit="шт")
    comp_item = Item(item_code="COMP", item_name="Компонент", item_ref1c="c1", unit="шт")
    db.add_all([root, comp_item])
    db.commit()

    # spec_code == item_code "ROOT" -> резолвится через fallback по коду.
    spec = Specification(spec_code="ROOT", spec_name="Спека ROOT", spec_ref1c="s-root")
    db.add(spec)
    db.commit()

    for ref in child_spec_refs:
        db.add(SpecComponent(
            spec_id=spec.spec_id,
            item_id=comp_item.item_id,
            quantity=1,
            component_type="Сборка",
            component_spec_ref1c=ref,
        ))
    db.commit()


def _dup_codes(payload):
    return [i for i in payload["issues"] if i.get("code") == "DUPLICATE_COMPONENT"]


def test_same_component_different_child_specs_not_duplicate(client, db_session):
    _seed(db_session, ["spec-x", "spec-y"])

    resp = client.get("/api/v1/specification/quality", params={"item_code": "ROOT"})

    assert resp.status_code == 200
    assert _dup_codes(resp.json()) == []


def test_same_component_same_child_spec_is_duplicate(client, db_session):
    _seed(db_session, ["spec-x", "spec-x"])

    resp = client.get("/api/v1/specification/quality", params={"item_code": "ROOT"})

    assert resp.status_code == 200
    assert len(_dup_codes(resp.json())) >= 1


def test_same_component_both_empty_child_spec_is_duplicate(client, db_session):
    _seed(db_session, [None, None])

    resp = client.get("/api/v1/specification/quality", params={"item_code": "ROOT"})

    assert resp.status_code == 200
    assert len(_dup_codes(resp.json())) >= 1

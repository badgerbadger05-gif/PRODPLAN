"""Регрессия: дерево спецификации отдаёт componentId/specId для ремонтного UI.

Ремонтные ручки (restage/move) адресуют строку состава по SpecComponent.component_id,
а add — по spec_id спеки-владельца. Узлы дерева обязаны нести эти id, иначе фронту
нечем вызвать ремонт.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import DefaultSpecification, Item, SpecComponent, Specification
from app.routers.specification import router

API = "/api/v1/specification"


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


def _seed(db):
    """Корень -> своя спека -> строка состава с деталью (у детали своей спеки нет)."""
    root = Item(item_code="ROOT", item_name="Изделие", item_ref1c="root")
    part = Item(item_code="PART", item_name="Деталь", item_ref1c="part")
    db.add_all([root, part])
    db.commit()
    spec = Specification(spec_code="ROOT", spec_name="Изделие", spec_ref1c="s-root")
    db.add(spec)
    db.commit()
    db.add(DefaultSpecification(item_id=root.item_id, spec_id=spec.spec_id))
    comp = SpecComponent(spec_id=spec.spec_id, item_id=part.item_id, quantity=2,
                         component_type="Сборка")
    db.add(comp)
    db.commit()
    return root, part, spec, comp


def test_root_node_carries_own_spec_id_and_null_component_id(client, db_session):
    root, part, spec, comp = _seed(db_session)

    resp = client.get(f"{API}/tree", params={"item_id": root.item_id})

    assert resp.status_code == 200
    node = resp.json()["nodes"][0]
    # корень — не строка состава, но несёт свою спеку (куда добавлять компоненты)
    assert node["componentId"] is None
    assert node["specId"] == spec.spec_id


def test_child_node_carries_component_id(client, db_session):
    root, part, spec, comp = _seed(db_session)

    resp = client.get(f"{API}/tree", params={"item_id": root.item_id, "depth": 1})

    assert resp.status_code == 200
    children = resp.json()["nodes"][0]["children"]
    child = next(n for n in children if n["type"] == "item")
    # строка состава адресуется по component_id для restage/move
    assert child["componentId"] == comp.component_id
    # у детали нет своей спеки -> specId пустой (нечего разворачивать/дополнять)
    assert child["specId"] is None


def test_search_returns_strict_item_and_meta_contract(client, db_session):
    root, _, spec, _ = _seed(db_session)

    response = client.get(f"{API}/search", params={"q": "ROOT", "limit": 10})

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "item_id": root.item_id,
                "item_code": "ROOT",
                "item_name": "Изделие",
                "item_article": None,
                "item_ref1c": "root",
                "unit": None,
                "unit_ref1c": None,
                "replenishment_method": None,
                "spec_id": spec.spec_id,
                "spec_code": "ROOT",
                "spec_name": "Изделие",
                "spec_ref1c": "s-root",
                "default_spec_count": 1,
                "has_children": True,
            }
        ],
        "meta": {"q": "ROOT", "count": 1, "limit": 10},
    }


def test_search_openapi_response_is_closed(client):
    schema = client.app.openapi()
    response = schema["paths"]["/api/v1/specification/search"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    assert response == {"$ref": "#/components/schemas/SpecificationSearchResponse"}
    schemas = schema["components"]["schemas"]
    for name in (
        "SpecificationSearchItemResponse",
        "SpecificationSearchMetaResponse",
        "SpecificationSearchResponse",
    ):
        assert schemas[name]["additionalProperties"] is False

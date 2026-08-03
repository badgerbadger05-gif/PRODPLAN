"""Workshop binding review endpoints: scopes, filters, counts, lines."""
from __future__ import annotations

from datetime import datetime

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
    ProductionKind,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ResourceProductionKind,
    Specification,
    WorkshopWarehouseBinding,
)
from app.routers.workshop_binding_review import router


@pytest.fixture()
def db_session():
    # StaticPool: TestClient handles requests in a different thread; the
    # default SingletonThreadPool would hand it a fresh empty :memory: DB.
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


def _mk_item(db, code: str, *, name: str | None = None) -> Item:
    item = Item(
        item_code=code,
        item_name=name or f"Item {code}",
        item_article=f"ART-{code}",
        unit="шт",
                status="active",
    )
    db.add(item)
    db.flush()
    return item


def _mk_spec(db, item: Item, *, name: str, kind: ProductionKind | None = None) -> Specification:
    spec = Specification(
        spec_name=name,
        spec_ref1c=f"sr-{name}",
        production_kind_id=kind.id if kind else None,
    )
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    return spec


def _mk_line(db, item: Item, *, workshop_id: int | None = None) -> ProductionProduct:
    order = ProductionOrder(
        order_number=f"O-{item.item_code}",
        order_date=datetime(2026, 6, 1),
        is_posted=True,
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=4,
        produced_qty=0,
        remaining_qty=4,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="shortage",
            issue_status="not_requested",
            workshop_id=workshop_id,
        )
    )
    return product


def _setup_dataset(db):
    """3 problem items + 1 fully bound item.

    A: spec without kind, 2 active lines  -> NO_PRODUCTION_KIND
    B: kind not bound to workshop, 1 line -> KIND_NOT_BOUND
    C: bound workshop without warehouse   -> NO_WAREHOUSE_BINDING (catalog+active)
    D: full chain, 1 line                 -> not listed
    E: no default spec, 1 line            -> NO_SPEC (active only)
    """
    item_a = _mk_item(db, "WBR-A", name="Рама без вида")
    _mk_spec(db, item_a, name="WBR Spec A")
    _mk_line(db, item_a)
    _mk_line(db, item_a)

    item_b = _mk_item(db, "WBR-B", name="Бампер с непривязанным видом")
    kind_b = ProductionKind(ref_1c="wbr-kind-b", name="Сварка WBR")
    db.add(kind_b)
    db.flush()
    _mk_spec(db, item_b, name="WBR Spec B", kind=kind_b)
    _mk_line(db, item_b)

    item_c = _mk_item(db, "WBR-C", name="Руль без склада")
    kind_c = ProductionKind(ref_1c="wbr-kind-c", name="Сборка WBR")
    resource_c = ProductionResource(resource_name="Участок без склада")
    db.add_all([kind_c, resource_c])
    db.flush()
    _mk_spec(db, item_c, name="WBR Spec C", kind=kind_c)
    db.add(ResourceProductionKind(resource_id=resource_c.resource_id, production_kind_id=kind_c.id))
    _mk_line(db, item_c)

    item_d = _mk_item(db, "WBR-D", name="Полностью привязанная деталь")
    kind_d = ProductionKind(ref_1c="wbr-kind-d", name="Токарка WBR")
    resource_d = ProductionResource(resource_name="Токарный WBR")
    db.add_all([kind_d, resource_d])
    db.flush()
    _mk_spec(db, item_d, name="WBR Spec D", kind=kind_d)
    db.add(ResourceProductionKind(resource_id=resource_d.resource_id, production_kind_id=kind_d.id))
    db.add(WorkshopWarehouseBinding(workshop_id=resource_d.resource_id, warehouse_ref1c="wh-wbr-d"))
    _mk_line(db, item_d)

    item_e = _mk_item(db, "WBR-E", name="Деталь без спецификации")
    _mk_line(db, item_e)

    db.commit()
    return item_a, item_b, item_c, item_d, item_e


def test_active_scope_lists_problem_items_with_counts(client, db_session):
    item_a, item_b, item_c, item_d, item_e = _setup_dataset(db_session)

    data = client.get("/api/v1/workshop-binding-review/items?scope=active").json()

    by_id = {row["item_id"]: row for row in data["items"]}
    assert set(by_id) == {item_a.item_id, item_b.item_id, item_c.item_id, item_e.item_id}
    assert data["total"] == 4
    assert data["counts_by_reason"] == {
        "NO_PRODUCTION_KIND": 1,
        "KIND_NOT_BOUND": 1,
        "NO_WAREHOUSE_BINDING": 1,
        "NO_SPEC": 1,
    }
    row_a = by_id[item_a.item_id]
    assert row_a["reason_code"] == "NO_PRODUCTION_KIND"
    assert row_a["active_lines"] == 2
    assert "1С" in row_a["recommendation"]
    row_b = by_id[item_b.item_id]
    assert row_b["production_kind_name"] == "Сварка WBR"
    assert "Ресурсы" in row_b["recommendation"]


def test_openapi_exposes_strict_review_contract(client):
    schema = client.app.openapi()
    items_response = schema["paths"]["/api/v1/workshop-binding-review/items"][
        "get"
    ]["responses"]["200"]["content"]["application/json"]["schema"]
    lines_response = schema["paths"][
        "/api/v1/workshop-binding-review/items/{item_id}/lines"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert items_response == {"$ref": "#/components/schemas/BindingReviewItemsResponse"}
    assert lines_response == {"$ref": "#/components/schemas/BindingReviewLinesResponse"}

    schemas = schema["components"]["schemas"]
    for name in (
        "BindingReviewItemResponse",
        "BindingReviewItemsResponse",
        "BindingReviewLineResponse",
        "BindingReviewLinesResponse",
    ):
        assert schemas[name]["additionalProperties"] is False
    assert schemas["BindingReviewItemResponse"]["properties"]["reason_code"][
        "enum"
    ] == [
        "NO_SPEC",
        "NO_PRODUCTION_KIND",
        "KIND_NOT_BOUND",
        "NO_WAREHOUSE_BINDING",
    ]
    assert schemas["BindingReviewItemsResponse"]["properties"]["scope"][
        "enum"
    ] == ["active", "catalog"]


def test_catalog_scope_skips_items_without_spec(client, db_session):
    item_a, item_b, item_c, item_d, item_e = _setup_dataset(db_session)

    data = client.get("/api/v1/workshop-binding-review/items?scope=catalog").json()

    by_id = {row["item_id"]: row for row in data["items"]}
    # E has no default spec -> not a catalog problem (purchased items have none).
    assert set(by_id) == {item_a.item_id, item_b.item_id, item_c.item_id}
    assert all(row["active_lines"] == 0 for row in data["items"])


def test_reason_filter_and_search(client, db_session):
    item_a, item_b, *_ = _setup_dataset(db_session)

    data = client.get(
        "/api/v1/workshop-binding-review/items?scope=active&reason_code=KIND_NOT_BOUND"
    ).json()
    assert [row["item_id"] for row in data["items"]] == [item_b.item_id]
    # Counts describe the whole scope, not the filtered page.
    assert data["counts_by_reason"]["NO_PRODUCTION_KIND"] == 1

    data = client.get("/api/v1/workshop-binding-review/items?scope=active&search=рама").json()
    assert [row["item_id"] for row in data["items"]] == [item_a.item_id]

    bad = client.get("/api/v1/workshop-binding-review/items?reason_code=NOPE")
    assert bad.status_code == 400


def test_pagination(client, db_session):
    _setup_dataset(db_session)
    data = client.get("/api/v1/workshop-binding-review/items?scope=active&limit=2&offset=0").json()
    assert len(data["items"]) == 2
    assert data["total"] == 4
    rest = client.get("/api/v1/workshop-binding-review/items?scope=active&limit=2&offset=2").json()
    assert len(rest["items"]) == 2
    assert {row["item_id"] for row in data["items"]}.isdisjoint(
        {row["item_id"] for row in rest["items"]}
    )


def test_item_lines_for_manual_assignment(client, db_session):
    item_a, *_ = _setup_dataset(db_session)

    data = client.get(f"/api/v1/workshop-binding-review/items/{item_a.item_id}/lines").json()
    assert data["total"] == 2
    assert all(row["workshop_id"] is None for row in data["rows"])
    assert all(row["quantity"] == 4 for row in data["rows"])


def test_manually_assigned_line_with_warehouse_is_not_a_problem(client, db_session):
    db = db_session
    item = _mk_item(db, "WBR-M", name="Назначенная вручную")
    resource = ProductionResource(resource_name="Ручной участок WBR")
    db.add(resource)
    db.flush()
    db.add(WorkshopWarehouseBinding(workshop_id=resource.resource_id, warehouse_ref1c="wh-wbr-m"))
    _mk_line(db, item, workshop_id=resource.resource_id)
    db.commit()

    data = client.get("/api/v1/workshop-binding-review/items?scope=active").json()
    assert data["total"] == 0

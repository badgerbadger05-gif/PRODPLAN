"""Router-тесты страницы «Проверка выпуска»."""

from datetime import datetime, timezone
from typing import Dict

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
    LedgerGeneration,
    PhysicalImportBatch,
    PlanningTruthState,
    SpecComponent,
    Specification,
    StockBin,
    StockWarehouse,
)
from app.routers.release_feasibility import router as release_feasibility_router
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C

CUTOFF = datetime(2026, 8, 21, tzinfo=timezone.utc)
MAIN_WAREHOUSE = "wh-main"


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = session_factory()
    # Проверка читает остаток принятого поколения Ledger.
    batch = PhysicalImportBatch(
        batch_key="release-feasibility-api", status="completed", cutoff=CUTOFF
    )
    generation = LedgerGeneration(
        generation_key="release-feasibility-api",
        status="accepted",
        cutoff=CUTOFF,
        accepted_at=CUTOFF,
        physical_import_batch=batch,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        algorithm_version="tests/release-feasibility",
    )
    session.add_all([batch, generation])
    session.flush()
    session.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    session.add(
        StockWarehouse(
            warehouse_ref1c=MAIN_WAREHOUSE,
            warehouse_name="Основной склад",
            is_selected=True,
        )
    )
    session.flush()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(release_feasibility_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _mk_item(db, code: str, article: str, *, stock: float = 0.0) -> Item:
    item = Item(
        item_code=code,
        item_name=f"Изделие {code}",
        item_article=article,
        unit="шт",
        status="active",
    )
    db.add(item)
    db.flush()
    if abs(float(stock)) > 1e-9:
        db.add(
            StockBin(
                ledger_generation_id=int(
                    db.query(PlanningTruthState).one().current_generation_id
                ),
                item_id=int(item.item_id),
                characteristic_ref="",
                organization_ref=DEFAULT_ORGANIZATION_REF1C,
                warehouse_ref1c=MAIN_WAREHOUSE,
                on_hand=float(stock),
            )
        )
        db.flush()
    return item


def _mk_spec(db, owner: Item, components: Dict[Item, float]) -> None:
    spec = Specification(spec_code=f"SP-{owner.item_code}", spec_name=f"Спека {owner.item_code}")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=int(owner.item_id), spec_id=int(spec.spec_id)))
    for comp, qty in components.items():
        db.add(SpecComponent(spec_id=int(spec.spec_id), item_id=int(comp.item_id), quantity=qty))
    db.flush()


def test_search_finds_item_by_article(client, db_session):
    _mk_item(db_session, "P1", "12-345")

    resp = client.get("/api/v1/release-feasibility/search", params={"q": "12-345"})

    assert resp.status_code == 200
    data = resp.json()
    assert [row["item_article"] for row in data["items"]] == ["12-345"]


def test_analyze_by_article_returns_blocking_rows(client, db_session):
    product = _mk_item(db_session, "P1", "12-345")
    material = _mk_item(db_session, "M1", "M-1", stock=4.0)
    _mk_spec(db_session, product, {material: 2.0})

    resp = client.get("/api/v1/release-feasibility/analyze", params={"article": "12-345", "qty": 10})

    assert resp.status_code == 200
    data = resp.json()
    assert data["root"]["item_article"] == "12-345"
    assert data["tree"] is None
    assert len(data["blocking"]) == 1
    row = data["blocking"][0]
    assert row["status"] == "shortage"
    assert row["required_qty"] == 20.0
    assert row["shortage_qty"] == 16.0
    assert data["summary"]["producible_qty"] == 2.0


def test_analyze_returns_tree_when_requested(client, db_session):
    product = _mk_item(db_session, "P1", "12-345")
    material = _mk_item(db_session, "M1", "M-1", stock=4.0)
    _mk_spec(db_session, product, {material: 2.0})

    resp = client.get(
        "/api/v1/release-feasibility/analyze",
        params={"item_id": int(product.item_id), "qty": 10, "include_tree": "true"},
    )

    assert resp.status_code == 200
    tree = resp.json()["tree"]
    assert tree["item_article"] == "12-345"
    assert [child["item_article"] for child in tree["children"]] == ["M-1"]


def test_ambiguous_article_returns_candidates(client, db_session):
    _mk_item(db_session, "P1", "12-345-01")
    _mk_item(db_session, "P2", "12-345-02")

    resp = client.get("/api/v1/release-feasibility/analyze", params={"article": "12-345", "qty": 1})

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert len(detail["candidates"]) == 2


def test_unknown_article_returns_404(client, db_session):
    resp = client.get("/api/v1/release-feasibility/analyze", params={"article": "нет-такого", "qty": 1})
    assert resp.status_code == 404


def test_qty_must_be_positive(client, db_session):
    _mk_item(db_session, "P1", "12-345")
    resp = client.get("/api/v1/release-feasibility/analyze", params={"article": "12-345", "qty": 0})
    assert resp.status_code == 422

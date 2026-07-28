"""Contract tests for /api/v1/planning-rates (drum & shelf reference data)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.routers.planning_rates import router as planning_rates_router


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
    app.include_router(planning_rates_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def contour(db_session):
    items = [
        models.Item(item_code="FG-A", item_name="Снегоход"),
        models.Item(item_code="FG-B", item_name="Мотобуксировщик"),
        models.Item(item_code="COMP-A", item_name="Рама"),
    ]
    resources = [
        models.ProductionResource(
            resource_name="Участок сборки снегоходов",
            capacity=Decimal("10"),
            planning_range=30,
        ),
        models.ProductionResource(
            resource_name="Участок сборки модулей",
            capacity=Decimal("4"),
            planning_range=14,
        ),
    ]
    warehouse = models.StockWarehouse(
        warehouse_ref1c="SHELF-REF",
        warehouse_name="Полка мехцеха",
    )
    db_session.add_all([*items, *resources, warehouse])
    db_session.commit()
    return {
        "fg_a": items[0].item_id,
        "fg_b": items[1].item_id,
        "comp": items[2].item_id,
        "resource_a": resources[0].resource_id,
        "resource_b": resources[1].resource_id,
    }


# ---------------------------------------------------------------------------
# Assembly rates
# ---------------------------------------------------------------------------


def test_assembly_rate_upsert_creates_then_updates_the_same_pair(client, contour):
    created = client.put(
        "/api/v1/planning-rates/assembly-rates",
        json={
            "rows": [
                {
                    "item_id": contour["fg_a"],
                    "resource_id": contour["resource_a"],
                    "qty_per_capacity": 1.5,
                },
                {
                    "item_id": contour["fg_b"],
                    "resource_id": contour["resource_a"],
                    "qty_per_capacity": 3,
                },
            ]
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert (body["created"], body["updated"]) == (2, 0)
    assert body["rows"][0]["item_code"] == "FG-A"
    assert body["rows"][0]["resource_name"] == "Участок сборки снегоходов"

    updated = client.put(
        "/api/v1/planning-rates/assembly-rates",
        json={
            "rows": [
                {
                    "item_id": contour["fg_a"],
                    "resource_id": contour["resource_a"],
                    "qty_per_capacity": 2,
                }
            ]
        },
    )
    assert updated.json()["created"] == 0
    assert updated.json()["updated"] == 1

    listed = client.get("/api/v1/planning-rates/assembly-rates").json()
    assert listed["total"] == 2
    assert {row["item_id"]: row["qty_per_capacity"] for row in listed["rows"]} == {
        contour["fg_a"]: 2.0,
        contour["fg_b"]: 3.0,
    }


def test_assembly_rate_list_filters_and_pages(client, contour):
    client.put(
        "/api/v1/planning-rates/assembly-rates",
        json={
            "rows": [
                {
                    "item_id": contour["fg_a"],
                    "resource_id": contour["resource_a"],
                    "qty_per_capacity": 1,
                },
                {
                    "item_id": contour["fg_b"],
                    "resource_id": contour["resource_b"],
                    "qty_per_capacity": 2,
                },
            ]
        },
    )

    filtered = client.get(
        "/api/v1/planning-rates/assembly-rates",
        params={"resource_id": contour["resource_b"]},
    ).json()
    assert filtered["total"] == 1
    assert filtered["rows"][0]["item_id"] == contour["fg_b"]

    page = client.get(
        "/api/v1/planning-rates/assembly-rates", params={"limit": 1, "offset": 1}
    ).json()
    assert page["total"] == 2
    assert len(page["rows"]) == 1
    assert page["limit"] == 1
    assert page["offset"] == 1


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        ({"qty_per_capacity": 0}, 422),
        ({"qty_per_capacity": -1}, 422),
        ({"item_id": 999_999}, 422),
        ({"resource_id": 999_999}, 422),
    ],
)
def test_assembly_rate_upsert_rejects_invalid_input(client, contour, mutate, expected):
    row = {
        "item_id": contour["fg_a"],
        "resource_id": contour["resource_a"],
        "qty_per_capacity": 1,
        **mutate,
    }
    response = client.put(
        "/api/v1/planning-rates/assembly-rates", json={"rows": [row]}
    )
    assert response.status_code == expected
    assert client.get("/api/v1/planning-rates/assembly-rates").json()["total"] == 0


def test_assembly_rate_upsert_rejects_duplicate_pair_in_one_payload(client, contour):
    row = {
        "item_id": contour["fg_a"],
        "resource_id": contour["resource_a"],
        "qty_per_capacity": 1,
    }
    response = client.put(
        "/api/v1/planning-rates/assembly-rates", json={"rows": [row, row]}
    )
    assert response.status_code == 422
    assert "duplicate" in response.json()["detail"]


def test_assembly_rate_delete_is_idempotent_only_once(client, contour):
    client.put(
        "/api/v1/planning-rates/assembly-rates",
        json={
            "rows": [
                {
                    "item_id": contour["fg_a"],
                    "resource_id": contour["resource_a"],
                    "qty_per_capacity": 1,
                }
            ]
        },
    )
    rate_id = client.get("/api/v1/planning-rates/assembly-rates").json()["rows"][0]["id"]

    assert client.delete(f"/api/v1/planning-rates/assembly-rates/{rate_id}").json() == {
        "deleted": True,
        "id": rate_id,
    }
    assert (
        client.delete(f"/api/v1/planning-rates/assembly-rates/{rate_id}").status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Shelf policies
# ---------------------------------------------------------------------------


def test_shelf_policy_crud_round_trip(client, contour):
    created = client.post(
        "/api/v1/planning-rates/shelf-policies",
        json={
            "item_id": contour["comp"],
            "warehouse_ref1c": "SHELF-REF",
            "replenishment_time_days": 5,
            "review_cycle_days": 3,
            "safety_days": 2,
            "batch_multiple": 4,
        },
    )
    assert created.status_code == 201
    policy = created.json()
    assert policy["item_code"] == "COMP-A"
    assert policy["warehouse_name"] == "Полка мехцеха"
    assert policy["active"] is True

    duplicate = client.post(
        "/api/v1/planning-rates/shelf-policies",
        json={"item_id": contour["comp"], "warehouse_ref1c": "SHELF-REF"},
    )
    assert duplicate.status_code == 409

    updated = client.put(
        f"/api/v1/planning-rates/shelf-policies/{policy['id']}",
        json={"safety_days": 7, "active": False},
    ).json()
    assert updated["safety_days"] == 7
    assert updated["active"] is False
    assert updated["replenishment_time_days"] == 5

    assert client.get(
        "/api/v1/planning-rates/shelf-policies", params={"active": True}
    ).json()["total"] == 0
    assert client.get(
        "/api/v1/planning-rates/shelf-policies", params={"active": False}
    ).json()["total"] == 1

    assert client.delete(
        f"/api/v1/planning-rates/shelf-policies/{policy['id']}"
    ).json()["deleted"] is True
    assert client.get("/api/v1/planning-rates/shelf-policies").json()["total"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"item_id": 999_999, "warehouse_ref1c": "SHELF-REF"},
        {"item_id": 1, "warehouse_ref1c": ""},
        {"item_id": 1, "warehouse_ref1c": "SHELF-REF", "batch_multiple": 0},
        {"item_id": 1, "warehouse_ref1c": "SHELF-REF", "safety_days": -1},
    ],
)
def test_shelf_policy_create_rejects_invalid_input(client, contour, payload):
    response = client.post("/api/v1/planning-rates/shelf-policies", json=payload)
    assert response.status_code == 422


def test_shelf_policy_delete_blocked_while_a_projection_references_it(
    client, db_session, contour
):
    policy_id = client.post(
        "/api/v1/planning-rates/shelf-policies",
        json={"item_id": contour["comp"], "warehouse_ref1c": "SHELF-REF"},
    ).json()["id"]

    physical = models.PhysicalImportBatch(
        batch_key="planning-rates-physical",
        status="completed",
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="planning-rates-generation",
        status="accepted",
        cutoff=datetime(2026, 8, 1, tzinfo=timezone.utc),
        capabilities={},
        source_watermarks={},
        physical_import_batch=physical,
        algorithm_version="tests/planning-rates",
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(
        models.ShelfProjection(
            ledger_generation_id=generation.id,
            shelf_policy_id=policy_id,
            item_id=contour["comp"],
            warehouse_ref1c="SHELF-REF",
            as_of_date=date(2026, 8, 1),
            protection_until=date(2026, 8, 10),
            target_qty=0,
            shelf_physical_qty=0,
            other_stock_qty=0,
            confirmed_open_production_qty=0,
            projected_qty=0,
            gap_qty=0,
            transfer_qty=0,
            unlaunched_mrp_qty=0,
            pull_qty=0,
            materialized_qty=0,
            demand_manifest=[],
        )
    )
    db_session.commit()

    blocked = client.delete(f"/api/v1/planning-rates/shelf-policies/{policy_id}")
    assert blocked.status_code == 409
    assert "deactivate" in blocked.json()["detail"]

    # Deactivation is the supported escape hatch.
    assert (
        client.put(
            f"/api/v1/planning-rates/shelf-policies/{policy_id}",
            json={"active": False},
        ).json()["active"]
        is False
    )


def test_shelf_policy_update_and_delete_report_missing_id(client, contour):
    assert (
        client.put(
            "/api/v1/planning-rates/shelf-policies/424242", json={"safety_days": 1}
        ).status_code
        == 404
    )
    assert (
        client.delete("/api/v1/planning-rates/shelf-policies/424242").status_code == 404
    )


# ---------------------------------------------------------------------------
# Production resources
# ---------------------------------------------------------------------------


def test_resources_list_reports_capacity_and_configured_takts(client, contour):
    client.put(
        "/api/v1/planning-rates/assembly-rates",
        json={
            "rows": [
                {
                    "item_id": contour["fg_a"],
                    "resource_id": contour["resource_a"],
                    "qty_per_capacity": 1,
                },
                {
                    "item_id": contour["fg_b"],
                    "resource_id": contour["resource_a"],
                    "qty_per_capacity": 3,
                },
            ]
        },
    )

    body = client.get("/api/v1/planning-rates/resources").json()
    assert body["total"] == 2
    by_id = {row["resource_id"]: row for row in body["rows"]}
    assert by_id[contour["resource_a"]]["capacity"] == 10.0
    assert by_id[contour["resource_a"]]["assembly_rate_count"] == 2
    assert by_id[contour["resource_b"]]["assembly_rate_count"] == 0
    assert by_id[contour["resource_b"]]["planning_range"] == 14


def test_resource_patch_updates_capacity_and_validates(client, contour):
    patched = client.patch(
        f"/api/v1/planning-rates/resources/{contour['resource_b']}",
        json={"capacity": 12.5, "planning_range": 21},
    ).json()
    assert patched["capacity"] == 12.5
    assert patched["planning_range"] == 21

    # Zero capacity is legal (a stopped workstation), negative is not.
    assert (
        client.patch(
            f"/api/v1/planning-rates/resources/{contour['resource_b']}",
            json={"capacity": 0},
        ).json()["capacity"]
        == 0.0
    )
    assert (
        client.patch(
            f"/api/v1/planning-rates/resources/{contour['resource_b']}",
            json={"capacity": -1},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/v1/planning-rates/resources/{contour['resource_b']}", json={}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            "/api/v1/planning-rates/resources/424242", json={"capacity": 1}
        ).status_code
        == 404
    )

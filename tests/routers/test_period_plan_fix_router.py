"""HTTP contract of the period-plan fixation routes.

`POST /period-plans/{id}/fix` is the flagship atomic action; the MRP-snapshot
route stays only as an idempotent recovery path and must not demand a
client-invented `generation_key` (it used to answer 422 to the shipped UI body).
"""

import datetime
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.models import (
    AssemblyRate,
    Item,
    LedgerGeneration,
    PhysicalImportBatch,
    PlanningRun,
    PlanningTruthState,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionResource,
    StockWarehouse,
)
from app.routers.plan import router as plan_router


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(plan_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def accepted_generation(db_session):
    cutoff = datetime.datetime(2026, 7, 23)
    batch = PhysicalImportBatch(
        batch_key="fix-router-ledger",
        status="completed",
        cutoff=cutoff,
        source_watermarks={"opening_at": "2025-01-01T00:00:00+00:00"},
        completed_at=cutoff,
    )
    generation = LedgerGeneration(
        generation_key="fix-router-ledger",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={"replay_from": "2026-06-01T00:00:00"},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
            "planning_snapshots": True,
        },
        physical_import_batch=batch,
        algorithm_version="test",
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    db_session.add(StockWarehouse(
        warehouse_ref1c="fix-router-warehouse",
        warehouse_code="FIX-ROUTER",
        warehouse_name="Fix router planning warehouse",
        is_selected=True,
        is_finished_goods=False,
    ))
    resource = ProductionResource(
        resource_name="Fix router assembly",
        planning_range=30,
        capacity=100,
    )
    db_session.add(resource)
    db_session.flush()
    db_session.info["fix_router_assembly_resource_id"] = int(resource.resource_id)
    db_session.commit()
    return generation


def _plan_with_line(db, *, status: str = "draft", qty: float = 7.0) -> ProductionPlanHeader:
    item = Item(
        item_code=f"ROUTER-FIX-{status}-{qty}",
        item_name="Деталь",
        unit="шт",
        stock_qty=0.0,
        replenishment_method="Покупка",
        replenishment_time=2,
        status="active",
    )
    db.add(item)
    db.flush()
    db.add(AssemblyRate(
        resource_id=int(db.info["fix_router_assembly_resource_id"]),
        item_id=int(item.item_id),
        qty_per_capacity=1,
    ))
    db.flush()
    plan = ProductionPlanHeader(
        name="Август", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        status=status, created_by="test",
    )
    db.add(plan)
    db.flush()
    db.add(ProductionPlanLine(
        plan_id=plan.id, item_id=item.item_id, bucket_date=date(2026, 8, 7), qty=qty,
    ))
    db.commit()
    return plan


def test_fix_route_publishes_the_snapshot_in_one_call(client, db_session, accepted_generation):
    plan = _plan_with_line(db_session)

    response = client.post(
        f"/api/v1/plan/period-plans/{plan.id}/fix", json={"fixed_by": "erp-shell"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "fixed"
    assert body["mrp"]["status"] == "ok"
    assert body["mrp"]["published"] is True
    run = db_session.query(PlanningRun).filter_by(run_id=body["mrp"]["run_id"]).one()
    assert run.status == "FIXED_SNAPSHOT"
    assert run.source_plan_id == int(plan.id)


def test_fix_route_rejects_an_empty_plan_and_keeps_it_draft(
    client, db_session, accepted_generation
):
    plan = _plan_with_line(db_session, qty=0.0)

    response = client.post(f"/api/v1/plan/period-plans/{plan.id}/fix", json={})

    assert response.status_code == 400
    assert "пустой план" in response.json()["detail"]
    db_session.expire_all()
    assert db_session.get(ProductionPlanHeader, plan.id).status == "draft"


def test_mrp_snapshot_route_accepts_a_body_without_generation_key(
    client, db_session, accepted_generation
):
    """Regression: the shipped UI body ``{started_by}`` used to answer 422."""
    plan = _plan_with_line(db_session, status="fixed")

    response = client.post(
        f"/api/v1/plan/period-plans/{plan.id}/mrp-snapshot",
        json={"started_by": "erp-shell"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["generation_key"] == (
        f"fix-period-plan:{int(plan.id)}:{int(accepted_generation.id)}"
    )
    assert body["run_id"] > 0


def test_mrp_snapshot_route_is_an_idempotent_recovery_path(
    client, db_session, accepted_generation
):
    plan = _plan_with_line(db_session, status="fixed")

    first = client.post(
        f"/api/v1/plan/period-plans/{plan.id}/mrp-snapshot", json={"started_by": "erp-shell"},
    )
    second = client.post(
        f"/api/v1/plan/period-plans/{plan.id}/mrp-snapshot", json={"started_by": "erp-shell"},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["run_id"] == first.json()["run_id"]
    assert second.json()["immutable"] is True
    assert db_session.query(PlanningRun).filter_by(
        source_plan_id=int(plan.id), status="FIXED_SNAPSHOT",
    ).count() == 1

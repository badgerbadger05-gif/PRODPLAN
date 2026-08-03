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
    ProductionMaterialCustodyProjectionManifest,
    LedgerGeneration,
    PlannedOrder,
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


def test_execution_journal_openapi_exposes_server_query_contract(client):
    operation = client.app.openapi()["paths"][
        "/api/v1/plan/period-plans/{plan_id}/execution-journal"
    ]["get"]
    params = {row["name"]: row for row in operation["parameters"]}
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]

    assert {
        "bom_level",
        "status",
        "include_net_zero",
        "sort_by",
        "sort_dir",
        "limit",
        "offset",
    }.issubset(params)
    assert response == {"$ref": "#/components/schemas/ExecutionJournalResponse"}
    assert client.app.openapi()["components"]["schemas"]["ExecutionJournalResponse"]["additionalProperties"] is False
    assert params["include_net_zero"]["schema"]["default"] is True
    assert params["limit"]["schema"]["default"] == 100
    assert params["offset"]["schema"]["default"] == 0


def test_execution_journal_route_returns_typed_payload_after_fix(client, db_session, accepted_generation):
    plan = _plan_with_line(db_session)

    fix_result = client.post(
        f"/api/v1/plan/period-plans/{plan.id}/fix",
        json={"fixed_by": "erp-shell"},
    )
    assert fix_result.status_code == 200, fix_result.text
    run_id = fix_result.json()["mrp"]["run_id"]

    response = client.get(
        f"/api/v1/plan/period-plans/{plan.id}/execution-journal",
        params={"run_id": run_id},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["rows"], list)
    assert body["run_id"] == int(run_id)


def test_matrix_route_returns_server_totals_for_draft_plans(client, db_session, accepted_generation):
    plan = _plan_with_line(db_session, status="draft", qty=4.0)
    item = db_session.query(Item).filter(Item.item_name == "Деталь").first()
    assert item is not None
    db_session.add(ProductionPlanLine(
        plan_id=plan.id,
        item_id=item.item_id,
        bucket_date=date(2026, 8, 14),
        qty=6.0,
    ))
    db_session.commit()

    response = client.get(f"/api/v1/plan/period-plans/{plan.id}/matrix")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["bucket_totals"] == {
        "2026-08-07": 4.0,
        "2026-08-14": 6.0,
        "2026-08-21": 0.0,
        "2026-08-28": 0.0,
    }
    assert body["grand_total"] == 10.0
    assert body["total_qty"] == 10.0
    assert body["total"] == 1


def test_matrix_route_hides_forecasts_for_fixed_plans(client, db_session, accepted_generation):
    plan = _plan_with_line(db_session, status="fixed", qty=4.0)
    line = db_session.query(ProductionPlanLine).filter_by(plan_id=plan.id).one()
    item = db_session.get(Item, line.item_id)
    assert item is not None
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(PlannedOrder(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=4.0,
        planned_qty=4.0,
        qty=4.0,
        need_date=date(2026, 8, 7),
        bucket_date=date(2026, 8, 7),
        start_date=date(2026, 8, 7),
        finish_date=date(2026, 8, 10),
    ))
    db_session.commit()

    response = client.get(f"/api/v1/plan/period-plans/{plan.id}/matrix")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["rows"][0]["bucket_forecasts"] == {}


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
    db_session.add(
        ProductionMaterialCustodyProjectionManifest(
            ledger_generation_id=int(generation.id),
            cutoff=generation.cutoff,
            status="complete",
            is_baseline=True,
            source_event_high_watermark_id=0,
            observed_at=generation.cutoff,
            built_at=generation.cutoff,
        )
    )
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
        fixed_at=(
            datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
            if status == "fixed"
            else None
        ),
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


def test_mrp_snapshot_route_request_contract_is_keyless(client):
    operation = client.app.openapi()["paths"][
        "/api/v1/plan/period-plans/{plan_id}/mrp-snapshot"
    ]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    if "$ref" in schema:
        components = client.app.openapi()["components"]["schemas"]
        schema = components[schema["$ref"].split("/")[-1]]
    assert "generation_key" not in (schema.get("properties") or {})


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

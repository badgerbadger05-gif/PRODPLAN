"""OpenAPI contract for the unified production journal."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.database import get_db
from app.routers.production_control import router
from app.services import planning_truth


def test_orders_journal_exposes_strict_typed_canonical_envelope():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        schema = client.app.openapi()

    operation = schema["paths"]["/api/v1/production-control/orders"]["get"]
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response == {"$ref": "#/components/schemas/ProductionOrderJournalResponse"}
    assert schema["components"]["schemas"]["ProductionOrderJournalResponse"]["additionalProperties"] is False
    assert "ProductionOrderPlanningResponse" not in schema["components"]["schemas"]
    assert "truth_meta" in schema["components"]["schemas"]["ProductionOrderJournalResponse"]["properties"]
    assert "planning" not in schema["components"]["schemas"]["ProductionOrderJournalRowResponse"]["properties"]
    root_options = schema["paths"]["/api/v1/production-control/orders/root-products"]["get"]
    root_options_response = (
        root_options["responses"]["200"]["content"]["application/json"]["schema"]
    )
    assert root_options_response == {
        "$ref": "#/components/schemas/ProductionControlRootProductOptionsResponse"
    }
    root_schema = schema["components"]["schemas"]["ProductionControlRootProductOptionsResponse"]
    assert root_schema["additionalProperties"] is False
    assert root_schema["properties"]["rows"]["type"] == "array"
    assert (
        root_schema["properties"]["rows"]["items"]["$ref"]
        == "#/components/schemas/ProductionControlRootProductOption"
    )
    assert "available_actions" in schema["components"]["schemas"]["ProductionOrderJournalRowResponse"]["properties"]
    contour = next(param for param in operation["parameters"] if param["name"] == "planning_contour")
    assert "dbr_feeder" not in contour["description"]

    employees_response = schema["paths"]["/api/v1/production-control/employees"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    assert employees_response == {
        "$ref": "#/components/schemas/ProductionEmployeeListResponse"
    }
    employee_list = schema["components"]["schemas"]["ProductionEmployeeListResponse"]
    employee_option = schema["components"]["schemas"]["ProductionEmployeeOptionResponse"]
    assert employee_list["additionalProperties"] is False
    assert employee_option["additionalProperties"] is False
    assert employee_option["properties"]["employee_type"]["enum"] == [
        "employee",
        "brigade",
    ]
    operations_response = schema["paths"][
        "/api/v1/production-control/orders/{product_id}/operations"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert operations_response == {
        "$ref": "#/components/schemas/ProductionOperationsResponse"
    }
    operations_list = schema["components"]["schemas"]["ProductionOperationsResponse"]
    operation_option = schema["components"]["schemas"][
        "ProductionOperationOptionResponse"
    ]
    assert operations_list["additionalProperties"] is False
    assert operation_option["additionalProperties"] is False

    paths = schema["paths"]
    assert "/api/v1/production-control/orders/{product_id}/close" in paths
    close_payload = paths["/api/v1/production-control/orders/{product_id}/close"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    if "$ref" in close_payload:
        close_payload = schema["components"]["schemas"][close_payload["$ref"].split("/")[-1]]
    assert close_payload["type"] == "object"
    assert "dry_run" in close_payload["properties"]
    assert "close_datetime" not in close_payload["properties"]
    # Правка количества к запуску разрешена, но только как явное действие
    # оператора над ещё не открытым в 1С заказом: контракт обязан нести строгий
    # payload, а не принимать произвольное тело.
    quantity_payload = paths["/api/v1/production-control/orders/{product_id}/quantity"]["patch"]["requestBody"]["content"]["application/json"]["schema"]
    if "$ref" in quantity_payload:
        quantity_payload = schema["components"]["schemas"][quantity_payload["$ref"].split("/")[-1]]
    assert quantity_payload["type"] == "object"
    assert "quantity" in quantity_payload["properties"]
    assert quantity_payload["properties"]["quantity"]["exclusiveMinimum"] == 0
    assert "/api/v1/production-control/orders/dedupe-mrp" not in paths
    assert "/api/v1/production-control/orders/{product_id}/materials/refresh" not in paths


def test_orders_journal_does_not_leak_internal_material_snapshot(monkeypatch):
    payload = {
        "product_id": 1,
        "order_id": 2,
        "order_number": "PP-1",
        "order_prodplan_number": None,
        "order_date": None,
        "order_source": "mrp",
        "source": "mrp",
        "order_ref1c": None,
        "order_one_c_number": None,
        "line_number": 1,
        "item_id": 3,
        "item_code": "ITEM",
        "item_name": "Item",
        "item_article": "ART",
        "optimal_batch": None,
        "unit": "шт",
        "quantity": 1.0,
        "produced_qty": 0.0,
        "remaining_qty": 1.0,
        "status": "shortage",
        "coverage_status": "shortage",
        "coverage_label": "Дефицит",
        "issue_status": "not_requested",
        "material_coverage_status": "shortage",
        "material_coverage_label": "Дефицит",
        "material_coverage_calculated_at": None,
        "material_coverage_snapshot": {"components": [{"secret": "internal"}]},
        "planned_start_date": None,
        "planned_finish_date": None,
        "forecast_date": None,
        "forecast_shift_days": None,
        "forecast_reason": None,
        "opened_at": None,
        "workshop_id": None,
        "workshop_name": None,
        "stage_id": None,
        "stage_name": None,
        "spec_id": None,
        "issue_count": 0,
        "route_sheet_printed_at": None,
        "comment": "",
        "source_run_id": None,
        "source_plan_id": None,
        "source_plan_name": None,
        "source_plan_period_from": None,
        "source_plan_period_to": None,
        "source_planned_order_id": None,
        "source_mrp_requirement_id": None,
        "source_mrp_allocation_key": None,
        "mrp_req_net_qty": None,
        "mrp_req_covered_qty": None,
        "mrp_req_remaining_qty": None,
        "launch_source": "mrp_remaining",
        "shelf_warehouse_ref1c": None,
        "shelf_pull_qty": None,
        "shelf_materialized_qty": None,
        "shelf_latest_start_date": None,
        "paint_weld_chain": None,
    }
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: object()
    monkeypatch.setattr(
        planning_truth,
        "require_accepted_truth",
        lambda *_args, **_kwargs: type(
            "Truth",
            (),
            {
                "ledger_generation": 1,
                "cutoff": None,
                "truth_status": "accepted",
                "reason": None,
            },
        )(),
    )
    monkeypatch.setattr(
        "app.routers.production_control.read_production_control_journal_snapshot",
        lambda *_args, **_kwargs: {
            "rows": [{key: value for key, value in payload.items() if key != "material_coverage_snapshot"}],
            "total": 1,
            "limit": 100,
            "offset": 0,
            "latest_run_id": None,
            "latest_source_plan_id": None,
        },
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/production-control/orders")

    assert response.status_code == 200
    assert "material_coverage_snapshot" not in response.json()["rows"][0]


def test_get_order_line_materials_returns_503_when_future_supply_capability_missing(monkeypatch):
    readiness = planning_truth.PlanningTruthReadiness(
        truth_status="accepted",
        ready=True,
        ledger_generation=1,
        generation_key="test",
        cutoff=None,
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "future_supply": False,
        },
        algorithm_version="test",
        replay_version="test",
        reason="Accepted Ledger generation lacks capabilities: future_supply",
        accepted_at=None,
    )

    def _materials_unavailable(*_args, **_kwargs):
        raise planning_truth.PlanningTruthUnavailable(
            readiness,
            consumer="production_control.material_coverage",
        )

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: object()
    monkeypatch.setattr("app.routers.production_control.get_materials_snapshot", _materials_unavailable)

    with TestClient(app) as client:
        response = client.get("/api/v1/production-control/orders/123/materials")

    assert response.status_code == 503
    payload = response.json()
    assert payload["detail"]["code"] == "planning_truth_unavailable"

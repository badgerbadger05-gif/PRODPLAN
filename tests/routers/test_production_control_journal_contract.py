"""OpenAPI contract for the unified production journal."""

from datetime import datetime, timezone

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
    from app.services.item_ledger.current_execution import CurrentExecutionUnavailable

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
        "app.services.item_ledger.current_execution.require_current_execution_scope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CurrentExecutionUnavailable("current execution manifest is missing")
        ),
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/production-control/orders")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "production_control_current_unavailable"


def test_get_order_line_materials_returns_503_when_future_supply_capability_missing(monkeypatch):
    readiness = planning_truth.PlanningTruthReadiness(
        truth_status="accepted",
        ready=True,
        ledger_generation=1,
        generation_key="test",
        cutoff=datetime(2026, 9, 7, 5, 32, 21, tzinfo=timezone.utc),
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "future_supply": False,
        },
        algorithm_version="test",
        replay_version="test",
        reason="Accepted Ledger generation lacks capabilities: future_supply",
        accepted_at=datetime(2026, 9, 7, 5, 33, tzinfo=timezone.utc),
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
    assert payload["detail"]["cutoff"] == "2026-09-07T05:32:21+00:00"

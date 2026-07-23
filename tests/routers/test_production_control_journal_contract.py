"""OpenAPI contract for the unified production journal."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.production_control import router


def test_orders_journal_exposes_strict_typed_envelope_and_dbr_filter():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as client:
        schema = client.app.openapi()

    operation = schema["paths"]["/api/v1/production-control/orders"]["get"]
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response == {"$ref": "#/components/schemas/ProductionOrderJournalResponse"}
    assert schema["components"]["schemas"]["ProductionOrderJournalResponse"]["additionalProperties"] is False
    assert schema["components"]["schemas"]["ProductionOrderPlanningResponse"]["additionalProperties"] is False
    assert "planning" in schema["components"]["schemas"]["ProductionOrderJournalRowResponse"]["properties"]
    contour = next(param for param in operation["parameters"] if param["name"] == "planning_contour")
    assert "dbr_feeder" in contour["description"]

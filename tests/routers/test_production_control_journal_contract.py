"""OpenAPI contract for the unified production journal."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.routers import production_control
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


def test_deprecated_single_material_issue_export_is_retired_without_calling_exporter(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("legacy single material-issue exporter must not be invoked")

    monkeypatch.setattr(production_control, "export_issue_to_1c", forbidden)
    app = FastAPI()
    app.include_router(router, prefix="/api")

    def override_get_db():
        yield object()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/production-control/material-issues/1/export-to-1c",
            json={"base_url": "http://example.invalid/odata", "entity_name": "Document_Test"},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "material_issue_legacy_single_export_retired"
    schema = app.openapi()
    assert schema["paths"]["/api/v1/production-control/material-issues/{issue_id}/export-to-1c"]["post"]["deprecated"] is True

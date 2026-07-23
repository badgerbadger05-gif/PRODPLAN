from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.planning_comparison import router


def test_comparison_router_contract_is_read_only_except_manual_capture():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    schema = TestClient(app).app.openapi()

    assert "get" in schema["paths"]["/api/v1/planning-comparison/input-fingerprint"]
    assert "post" in schema["paths"]["/api/v1/planning-comparison/captures"]
    assert "get" in schema["paths"]["/api/v1/planning-comparison/batches"]
    assert "get" in schema["paths"]["/api/v1/planning-comparison/batches/{batch_id}"]
    assert "delete" not in schema["paths"]["/api/v1/planning-comparison/batches/{batch_id}"]


def test_capture_request_constrains_identity_and_skew():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    assert client.post(
        "/api/v1/planning-comparison/captures",
        json={"capture_key": "x" * 129, "max_skew_seconds": 300},
    ).status_code == 422
    assert client.post(
        "/api/v1/planning-comparison/captures",
        json={"capture_key": "ok", "max_skew_seconds": 86401},
    ).status_code == 422

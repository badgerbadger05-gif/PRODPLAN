import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import odata


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(odata.router, prefix="/api")
    return TestClient(app)


def test_groups_combines_raw_cache_with_independent_selection(tmp_path, monkeypatch):
    groups_path = tmp_path / "groups.json"
    selected_path = tmp_path / "selected.json"
    groups_path.write_text(
        json.dumps(
            {
                "value": [
                    {
                        "Ref_Key": "g1",
                        "Code": "C",
                        "Description": "Name",
                        "IsFolder": True,
                    },
                    {"Ref_Key": "", "Code": "BAD", "Description": "Invalid"},
                ]
            }
        ),
        encoding="utf-8",
    )
    selected_path.write_text(json.dumps(["g1", "old-id"]), encoding="utf-8")
    monkeypatch.setattr(odata, "GROUPS_JSON", groups_path)
    monkeypatch.setattr(odata, "GROUPS_SELECTED", selected_path)

    with _client() as client:
        response = client.get("/api/v1/odata/groups")
        schema = client.app.openapi()

    assert response.status_code == 200
    assert response.json() == {
        "items": [{"id": "g1", "code": "C", "name": "Name"}],
        "selected_ids": ["g1", "old-id"],
    }
    openapi_response = schema["paths"]["/api/v1/odata/groups"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    assert openapi_response == {"$ref": "#/components/schemas/NomenclatureGroupsResponse"}
    schemas = schema["components"]["schemas"]
    assert schemas["NomenclatureGroupResponse"]["additionalProperties"] is False
    assert schemas["NomenclatureGroupsResponse"]["additionalProperties"] is False


def test_groups_preserves_selection_when_cache_is_corrupt(tmp_path, monkeypatch):
    groups_path = tmp_path / "groups.json"
    selected_path = tmp_path / "selected.json"
    groups_path.write_text("not-json", encoding="utf-8")
    selected_path.write_text(json.dumps(["old-id"]), encoding="utf-8")
    monkeypatch.setattr(odata, "GROUPS_JSON", groups_path)
    monkeypatch.setattr(odata, "GROUPS_SELECTED", selected_path)

    with _client() as client:
        response = client.get("/api/v1/odata/groups")

    assert response.status_code == 200
    assert response.json() == {"items": [], "selected_ids": ["old-id"]}

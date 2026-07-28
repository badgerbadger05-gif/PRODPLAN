"""``POST /v1/plan/export`` must produce the format it advertises.

The route always built CSV but echoed ``req.format`` back, so a client that
asked for ``xlsx`` received a CSV body labelled as a workbook.  It now generates
the requested format and refuses anything it cannot produce.
"""

import base64
import csv
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import get_db
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


@pytest.fixture(autouse=True)
def _pin_session_connection(db_session):
    # The TestClient serves requests from another thread; without an open
    # transaction the shared ``:memory:`` engine would hand it an empty database.
    db_session.execute(text("SELECT 1"))


def test_csv_export_returns_a_csv_body(client):
    response = client.post("/api/v1/plan/export", json={"format": "csv", "days": 3})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["format"] == "csv"
    assert body["filename"].endswith(".csv")
    assert "data" in body and "data_base64" not in body
    # Whatever the row count, the payload must parse as CSV.
    list(csv.reader(io.StringIO(body["data"])))


def test_xlsx_export_returns_a_real_workbook(client):
    openpyxl = pytest.importorskip("openpyxl")

    response = client.post("/api/v1/plan/export", json={"format": "xlsx", "days": 3})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["format"] == "xlsx"
    assert body["filename"].endswith(".xlsx")
    assert "data" not in body
    payload = base64.b64decode(body["data_base64"])
    # A ZIP container, i.e. an actual workbook and not CSV text in disguise.
    assert payload[:2] == b"PK"
    workbook = openpyxl.load_workbook(io.BytesIO(payload))
    assert workbook.active.title == "Plan"
    headers = [cell.value for cell in next(workbook.active.iter_rows(max_row=1))]
    assert headers[:4] == ["Изделие", "Артикул", "Код", "План на месяц"]


@pytest.mark.parametrize("bad_format", ["pdf", "json", "XLS"])
def test_unsupported_format_is_refused_instead_of_silently_returning_csv(
    client, bad_format
):
    response = client.post(
        "/api/v1/plan/export", json={"format": bad_format, "days": 3},
    )

    assert response.status_code == 422, response.text
    assert "формат" in response.json()["detail"].lower()


@pytest.mark.parametrize("body", [{"days": 3}, {"format": "", "days": 3}, {"format": "XLSX", "days": 3}])
def test_format_is_normalised_and_defaults_to_csv(client, body):
    response = client.post("/api/v1/plan/export", json=body)

    assert response.status_code == 200, response.text
    expected = "xlsx" if str(body.get("format") or "csv").lower() == "xlsx" else "csv"
    assert response.json()["format"] == expected

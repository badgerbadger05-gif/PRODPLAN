"""R9 purchase-control current anchor and transport contracts."""

from fastapi.testclient import TestClient
from types import SimpleNamespace

from app import models
from app.main import app
from app.routers.purchase_control import _canonical_purchase_sort
from app.routers.purchase_control import get_orders
from app.services.item_ledger import current_execution


def test_purchase_export_batch_is_current_anchor_only_after_r10_cutover():
    table = models.PurchaseExportBatch.__table__

    assert table.c.current_execution_scope_id.nullable is False
    assert table.c.current_execution_source_revision.nullable is False
    assert "planning_read_snapshot_id" not in table.c
    foreign_keys = {
        (fk.parent.name, fk.target_fullname, fk.ondelete)
        for fk in table.foreign_keys
    }
    assert ("current_execution_scope_id", "current_execution_scope.id", "RESTRICT") in foreign_keys
    assert not any(parent == "planning_read_snapshot_id" for parent, *_ in foreign_keys)


def test_purchase_sort_uses_typed_numeric_primary_and_tie_ascending():
    rows = [
        {"row_key": "ten", "remaining_qty": 10, "order_number": "2"},
        {"row_key": "two", "remaining_qty": 2, "order_number": "10"},
        {"row_key": "two-b", "remaining_qty": 2, "order_number": "2"},
    ]

    _canonical_purchase_sort(rows, field="remaining_qty", descending=False)

    assert [row["row_key"] for row in rows] == ["two-b", "two", "ten"]


def test_purchase_row_transport_contract_exposes_current_identity_and_revision():
    schema = TestClient(app).app.openapi()["components"]["schemas"]
    response = schema["PurchaseControlSelectionSummaryResponse"]["properties"]

    assert "current_identity" in response
    assert "current_identities" in response
    assert "source_revision" in response


def test_purchase_get_attaches_manifest_revision_to_stable_row(monkeypatch):
    manifest = SimpleNamespace(
        id=17,
        source_generation_id=42,
        source_revision="accepted:g42-noop-2",
        summary={"summary": {"total_rows": 1}},
    )
    row = SimpleNamespace(
        business_identity="purchase:req:9",
        source_revision="accepted:g41",
        payload={"row_key": "legacy-generation-row", "remaining_qty": 2},
    )
    monkeypatch.setattr(current_execution, "require_current_execution_scope", lambda *args, **kwargs: manifest)
    monkeypatch.setattr(current_execution, "load_current_execution_rows", lambda *args, **kwargs: [row])

    result = get_orders(db=object(), horizon_period_to=None, limit=100, offset=0)

    assert result["rows"][0]["current_identity"] == "purchase:req:9"
    assert result["rows"][0]["source_revision"] == "accepted:g42-noop-2"
    assert result["source_revision"] == "accepted:g42-noop-2"


def test_purchase_current_identity_filters_before_pagination(monkeypatch):
    manifest = SimpleNamespace(
        id=17,
        source_generation_id=42,
        source_revision="accepted:g42-deep-link",
        summary={"summary": {"total_rows": 2}},
    )
    rows = [
        SimpleNamespace(
            business_identity="purchase:req:1",
            source_revision="accepted:g42",
            payload={"row_key": "buy:1", "remaining_qty": 2, "item_name": "First"},
        ),
        SimpleNamespace(
            business_identity="purchase:req:2",
            source_revision="accepted:g42",
            payload={"row_key": "buy:2", "remaining_qty": 3, "item_name": "Second"},
        ),
    ]
    monkeypatch.setattr(current_execution, "require_current_execution_scope", lambda *args, **kwargs: manifest)
    monkeypatch.setattr(current_execution, "load_current_execution_rows", lambda *args, **kwargs: rows)

    result = get_orders(
        db=object(), current_identity="purchase:req:2", horizon_period_to=None,
        limit=1, offset=0,
    )

    assert result["total"] == 1
    assert result["rows"][0]["current_identity"] == "purchase:req:2"
    assert result["rows"][0]["item_name"] == "Second"

    missing = get_orders(
        db=object(), current_identity="purchase:req:missing", horizon_period_to=None,
        limit=1, offset=0,
    )
    assert missing["total"] == 0
    assert missing["rows"] == []

"""R9 purchase-control current anchor and transport contracts."""

from fastapi.testclient import TestClient

from app import models
from app.main import app
from app.routers.purchase_control import _canonical_purchase_sort


def test_purchase_export_batch_has_nullable_current_scope_anchor():
    table = models.PurchaseExportBatch.__table__

    assert table.c.current_execution_scope_id.nullable is True
    assert table.c.current_execution_source_revision.nullable is True
    # Historical batches keep their old FK, but current batches must be able
    # to omit it rather than pretending a CurrentExecutionScope id is a
    # PlanningReadSnapshot id.
    assert table.c.planning_read_snapshot_id.nullable is True
    foreign_keys = {
        (fk.parent.name, fk.target_fullname, fk.ondelete)
        for fk in table.foreign_keys
    }
    assert (
        "current_execution_scope_id",
        "current_execution_scope.id",
        "RESTRICT",
    ) in foreign_keys


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

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.services.item_ledger.current_physical import unconsumed_receipt_qty


def movement(id, quantity, *, kind="assembly_in", document=None, warehouse="WH"):
    return SimpleNamespace(id=id, item_id=9603, characteristic_ref="", organization_ref="ORG",
        warehouse_ref1c=warehouse, qty=Decimal(str(quantity)), movement_kind=kind,
        posting_at=datetime(2026, 6, min(id, 28), tzinfo=timezone.utc),
        recorder_type="Document_СборкаЗапасов", recorder_ref=document or str(id), line_no=str(id))


def test_cp303_signed_stock_depletes_receipts_including_opening_and_adjustment():
    rows = [movement(1, 37, kind="receipt"), movement(2, 100), movement(3, 61),
            movement(4, 44), movement(5, -215, kind="assembly_out"), movement(6, -10, kind="expense")]
    remaining = unconsumed_receipt_qty(rows)
    assert remaining == {4: Decimal("17")}
    assert sum(remaining.values()) == sum(row.qty for row in rows)


def test_internal_transfer_does_not_spend_existing_stock_again():
    rows = [movement(1, 17), movement(2, -10, kind="expense", document="transfer"),
            movement(3, 10, kind="receipt", document="transfer", warehouse="WH2")]
    rows[2].posting_at = rows[1].posting_at
    assert unconsumed_receipt_qty(rows) == {1: Decimal("17")}


def test_document_transport_uses_canonical_destination_net_output():
    rows = [movement(1, 100, document="assembly"),
            movement(2, -100, kind="assembly_out", document="assembly"),
            movement(3, 100, document="assembly", warehouse="WH2")]
    for row in rows:
        row.posting_at = rows[0].posting_at
    assert unconsumed_receipt_qty(rows) == {3: Decimal("100")}


def test_negative_opening_does_not_create_free_stock_after_receipt():
    rows = [movement(1, -8, kind="expense"), movement(2, 10)]
    assert unconsumed_receipt_qty(rows) == {2: Decimal("2")}

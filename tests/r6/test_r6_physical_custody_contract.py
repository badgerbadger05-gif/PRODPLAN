"""R6 red gate: current physical stock, senior holds, and custody boundaries.

These tests intentionally exercise the small canonical folds independently of
generation storage.  A generation is provenance for a result; it is not a
second physical quantity or a reason to release a reservation hold.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.item_ledger.current_physical import (
    CurrentPhysicalStateError,
    fold_current_stock,
    senior_hold_qty,
)


def test_compact_stock_fold_keeps_full_key_and_negative_physical_quantity():
    rows = [
        {"item_id": 10, "characteristic_ref": "", "organization_ref": "ORG-A", "warehouse_ref1c": "WH-1", "qty": "-3.125"},
        {"item_id": 10, "characteristic_ref": "", "organization_ref": "ORG-A", "warehouse_ref1c": "WH-1", "qty": "1.000"},
        {"item_id": 10, "characteristic_ref": "", "organization_ref": "ORG-B", "warehouse_ref1c": "WH-1", "qty": "5.000"},
        {"item_id": 10, "characteristic_ref": "", "organization_ref": "ORG-A", "warehouse_ref1c": "WH-2", "qty": "2.000"},
    ]

    result = fold_current_stock(rows)

    assert result[(10, "", "ORG-A", "WH-1")].on_hand == Decimal("-2.125")
    assert result[(10, "", "ORG-B", "WH-1")].on_hand == Decimal("5")
    assert result[(10, "", "ORG-A", "WH-2")].on_hand == Decimal("2")


def test_senior_hold_receipt_does_not_release_but_assigned_expense_does():
    allocations = [
        SimpleNamespace(allocation_role="replenishment_receipt", allocated_qty=Decimal("4")),
        SimpleNamespace(allocation_role="material_consumption", allocated_qty=Decimal("2")),
    ]

    assert senior_hold_qty(Decimal("5"), allocations) == Decimal("3")
    assert senior_hold_qty(Decimal("5"), allocations[:1]) == Decimal("5")
    assert senior_hold_qty(Decimal("2"), allocations) == Decimal("0")


def test_senior_hold_rejects_unknown_allocation_role():
    with pytest.raises(CurrentPhysicalStateError, match="allocation role"):
        senior_hold_qty(
            Decimal("1"),
            [SimpleNamespace(allocation_role="unknown", allocated_qty=Decimal("1"))],
        )


def test_conservation_is_role_separate_and_decimal_exact():
    allocations = [
        SimpleNamespace(allocation_role="material_consumption", allocated_qty=Decimal("1.125")),
        SimpleNamespace(allocation_role="replenishment_receipt", allocated_qty=Decimal("2.250")),
    ]
    assert senior_hold_qty(Decimal("4.000"), allocations) == Decimal("2.875")

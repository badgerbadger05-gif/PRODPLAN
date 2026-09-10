"""R6 red gate: current physical stock, senior holds, and custody boundaries.

These tests intentionally exercise the small canonical folds independently of
generation storage.  A generation is provenance for a result; it is not a
second physical quantity or a reason to release a reservation hold.
"""

from decimal import Decimal
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from app import models
from app.services.item_ledger.current_physical import (
    CurrentPhysicalStateError,
    fold_current_stock,
    senior_hold_qty,
)
from app.services.mrp_stock_helpers import planning_stock_by_item
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C
from app.services.production_material_custody_projection import (
    _late_events_behind_baseline,
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


def test_internal_transfer_and_return_keep_signs_and_foreign_organization_isolated():
    rows = [
        {"item_id": 12, "organization_ref": "ORG-A", "warehouse_ref1c": "WH-A", "qty": "-4"},
        {"item_id": 12, "organization_ref": "ORG-A", "warehouse_ref1c": "WH-B", "qty": "4"},
        {"item_id": 12, "organization_ref": "ORG-A", "warehouse_ref1c": "WH-B", "qty": "-1"},
        {"item_id": 12, "organization_ref": "ORG-B", "warehouse_ref1c": "WH-B", "qty": "9"},
    ]
    folded = fold_current_stock(rows)
    assert folded[(12, "", "ORG-A", "WH-A")].on_hand == Decimal("-4")
    assert folded[(12, "", "ORG-A", "WH-B")].on_hand == Decimal("3")
    assert folded[(12, "", "ORG-B", "WH-B")].on_hand == Decimal("9")


def test_current_reader_does_not_fall_back_to_requested_generation(db_session, building_ledger_generation):
    item = models.Item(item_code="R6-CURRENT", item_name="R6 current", unit="шт", status="active")
    db_session.add(item)
    db_session.flush()
    db_session.add(models.StockBin(
        ledger_generation_id=building_ledger_generation.id + 100,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c="WH-R6",
        on_hand=Decimal("-2.500"),
    ))
    db_session.flush()
    assert planning_stock_by_item(db_session, ledger_generation_id=building_ledger_generation.id)[item.item_id] == -2.5


def test_late_custody_event_is_explicitly_visible_for_baseline_rewind(db_session):
    event = models.ProductionMaterialCustodyEvent(
        product_id=1,
        component_item_id=2,
        source_kind="issue_created",
        effective_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        location_kind="workshop",
        warehouse_ref1c="WH-R6",
        delta_qty=Decimal("1"),
        idempotency_key="r6-late-custody",
    )
    db_session.add(event)
    db_session.commit()
    assert _late_events_behind_baseline(
        db_session,
        baseline_cutoff=datetime(2026, 7, 5, tzinfo=timezone.utc),
        baseline_high_watermark_id=0,
        target_high_watermark_id=event.id,
    ) == [event.id]

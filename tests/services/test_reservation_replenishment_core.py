from decimal import Decimal

from app.services.item_ledger.reservation import (
    freeze_reservation_amounts,
    replenishment_execution_pct,
    replenishment_remaining,
)


def test_freeze_keeps_full_reserve_and_only_missing_replenishment() -> None:
    frozen = freeze_reservation_amounts("100", "70")

    assert frozen.reserved_qty == Decimal("100")
    assert frozen.covered_from_stock_at_freeze_qty == Decimal("70")
    assert frozen.replenishment_required_qty == Decimal("30")


def test_freeze_never_overcovers_reserve() -> None:
    frozen = freeze_reservation_amounts("10", "25")

    assert frozen.covered_from_stock_at_freeze_qty == Decimal("10")
    assert frozen.replenishment_required_qty == Decimal("0")


def test_execution_uses_only_replenishment_denominator() -> None:
    assert replenishment_execution_pct("30", "12") == Decimal("40")
    assert replenishment_remaining("30", "12") == Decimal("18")


def test_no_replenishment_has_no_percentage() -> None:
    assert replenishment_execution_pct("0", "0") is None


def test_execution_is_capped_at_one_hundred_percent() -> None:
    assert replenishment_execution_pct("30", "50") == Decimal("100")
    assert replenishment_remaining("30", "50") == Decimal("0")

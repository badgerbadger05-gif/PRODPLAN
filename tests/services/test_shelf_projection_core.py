from datetime import date
from decimal import Decimal

from app.services.item_ledger.shelf_projection_core import (
    ShelfDemand,
    ShelfReceipt,
    project_shelf,
)


def test_shelf_target_is_protected_drum_demand_capped_by_mrp() -> None:
    result = project_shelf(
        (
            ShelfDemand(date(2026, 8, 3), Decimal("4"), ("a",)),
            ShelfDemand(date(2026, 8, 8), Decimal("9"), ("b",)),
        ),
        as_of=date(2026, 8, 1),
        replenishment_time_days=3,
        review_cycle_days=2,
        safety_days=2,
        batch_multiple=Decimal("5"),
        open_mrp_qty=Decimal("10"),
        shelf_physical_qty=Decimal("2"),
        other_stock_qty=Decimal("3"),
        confirmed_receipts=(ShelfReceipt(date(2026, 8, 1), Decimal("1")),),
    )

    assert result.target_qty == Decimal("10")
    assert result.projected_qty == Decimal("3")
    assert result.gap_qty == Decimal("7")
    assert result.transfer_qty == Decimal("3")
    assert result.pull_qty == Decimal("4")
    assert result.materialized_qty == Decimal("5")
    assert result.first_shortage_date == date(2026, 8, 3)
    assert result.latest_start_date == date(2026, 7, 31)


def test_shelf_batch_rounding_never_exceeds_unlaunched_mrp() -> None:
    result = project_shelf(
        (ShelfDemand(date(2026, 8, 2), Decimal("20"), ()),),
        as_of=date(2026, 8, 1),
        replenishment_time_days=1,
        review_cycle_days=0,
        safety_days=0,
        batch_multiple=Decimal("6"),
        open_mrp_qty=Decimal("7"),
        shelf_physical_qty=Decimal("0"),
        other_stock_qty=Decimal("0"),
        confirmed_receipts=(ShelfReceipt(date(2026, 8, 1), Decimal("2")),),
    )

    assert result.unlaunched_mrp_qty == Decimal("5")
    assert result.pull_qty == Decimal("5")
    assert result.materialized_qty == Decimal("5")


def test_projection_nets_earlier_drum_consumption_without_double_counting() -> None:
    """`shelf_projected_qty` is coverage that arrives before the need date.

    Ten pieces on the shelf serve the 8/2 slot first, so the 8/5 slot only sees
    what is left. The gap stays the honest four pieces — the earlier consumption
    must not be charged twice.
    """
    result = project_shelf(
        (
            ShelfDemand(date(2026, 8, 2), Decimal("6"), ()),
            ShelfDemand(date(2026, 8, 5), Decimal("8"), ()),
        ),
        as_of=date(2026, 8, 1),
        replenishment_time_days=2,
        review_cycle_days=0,
        safety_days=10,
        batch_multiple=Decimal("1"),
        open_mrp_qty=Decimal("100"),
        shelf_physical_qty=Decimal("10"),
        other_stock_qty=Decimal("0"),
    )

    assert result.target_qty == Decimal("14")
    assert result.projected_qty == Decimal("10")
    assert result.gap_qty == Decimal("4")
    assert result.first_shortage_date == date(2026, 8, 5)
    assert result.latest_start_date == date(2026, 8, 3)


def test_confirmed_receipt_landing_after_the_need_date_is_not_coverage() -> None:
    """A late confirmed order cannot repair a shortage that already happened."""
    result = project_shelf(
        (
            ShelfDemand(date(2026, 8, 3), Decimal("5"), ()),
            ShelfDemand(date(2026, 8, 12), Decimal("5"), ()),
        ),
        as_of=date(2026, 8, 1),
        replenishment_time_days=1,
        review_cycle_days=0,
        safety_days=13,
        batch_multiple=Decimal("1"),
        open_mrp_qty=Decimal("20"),
        shelf_physical_qty=Decimal("0"),
        other_stock_qty=Decimal("0"),
        confirmed_receipts=(ShelfReceipt(date(2026, 8, 10), Decimal("10")),),
    )

    # A flat `physical + confirmed` would have projected 10, closed the gap and
    # silently cancelled the mech-shop pull for the 8/3 slot.
    assert result.confirmed_open_production_qty == Decimal("10")
    assert result.target_qty == Decimal("10")
    assert result.projected_qty == Decimal("5")
    assert result.gap_qty == Decimal("5")
    assert result.unlaunched_mrp_qty == Decimal("10")
    assert result.pull_qty == Decimal("5")
    assert result.first_shortage_date == date(2026, 8, 3)
    assert result.latest_start_date == date(2026, 8, 2)


def test_undated_confirmed_production_cannot_be_fabricated_as_opening_balance() -> None:
    """Without a dated receipt, only physical shelf stock is coverage."""
    result = project_shelf(
        (ShelfDemand(date(2026, 8, 3), Decimal("9"), ()),),
        as_of=date(2026, 8, 1),
        replenishment_time_days=1,
        review_cycle_days=1,
        safety_days=1,
        batch_multiple=Decimal("1"),
        open_mrp_qty=Decimal("20"),
        shelf_physical_qty=Decimal("2"),
        other_stock_qty=Decimal("0"),
    )

    assert result.confirmed_open_production_qty == Decimal("0")
    assert result.projected_qty == Decimal("2")
    assert result.gap_qty == Decimal("7")
    assert result.first_shortage_date == date(2026, 8, 3)

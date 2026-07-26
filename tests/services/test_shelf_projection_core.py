from datetime import date
from decimal import Decimal

from app.services.item_ledger.shelf_projection_core import (
    ShelfDemand,
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
        confirmed_open_production_qty=Decimal("1"),
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
        confirmed_open_production_qty=Decimal("2"),
    )

    assert result.unlaunched_mrp_qty == Decimal("5")
    assert result.pull_qty == Decimal("5")
    assert result.materialized_qty == Decimal("5")

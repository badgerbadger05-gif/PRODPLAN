"""Pure shelf-buffer projection over persisted drum demand."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING


@dataclass(frozen=True)
class ShelfDemand:
    need_date: date
    qty: Decimal
    priority: tuple[object, ...]


@dataclass(frozen=True)
class ShelfProjectionResult:
    protection_until: date
    target_qty: Decimal
    shelf_physical_qty: Decimal
    other_stock_qty: Decimal
    confirmed_open_production_qty: Decimal
    projected_qty: Decimal
    gap_qty: Decimal
    transfer_qty: Decimal
    unlaunched_mrp_qty: Decimal
    pull_qty: Decimal
    materialized_qty: Decimal
    first_shortage_date: date | None
    latest_start_date: date | None


def _d(value: Decimal | int | float | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _round_up(value: Decimal, multiple: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    if multiple <= 0:
        raise ValueError("batch_multiple must be positive")
    return (value / multiple).to_integral_value(rounding=ROUND_CEILING) * multiple


def project_shelf(
    demands: tuple[ShelfDemand, ...],
    *,
    as_of: date,
    replenishment_time_days: int,
    review_cycle_days: int,
    safety_days: int,
    batch_multiple: Decimal,
    open_mrp_qty: Decimal,
    shelf_physical_qty: Decimal,
    other_stock_qty: Decimal,
    confirmed_open_production_qty: Decimal,
) -> ShelfProjectionResult:
    """Calculate timing only; never create demand beyond frozen MRP."""
    if min(replenishment_time_days, review_cycle_days, safety_days) < 0:
        raise ValueError("shelf protection days must be non-negative")
    open_qty = max(_d(open_mrp_qty), Decimal("0"))
    physical = _d(shelf_physical_qty)
    other = max(_d(other_stock_qty), Decimal("0"))
    confirmed = max(_d(confirmed_open_production_qty), Decimal("0"))
    protection_until = as_of + timedelta(
        days=replenishment_time_days + review_cycle_days + safety_days
    )
    ordered = sorted(
        (row for row in demands if _d(row.qty) > 0),
        key=lambda row: (row.need_date, row.priority),
    )
    protected_demand = sum(
        (_d(row.qty) for row in ordered if row.need_date <= protection_until),
        Decimal("0"),
    )
    target = min(protected_demand, open_qty)
    projected = physical + confirmed
    gap = max(target - projected, Decimal("0"))
    transfer = min(gap, other)
    unlaunched = max(open_qty - confirmed, Decimal("0"))
    pull = min(max(gap - transfer, Decimal("0")), unlaunched)
    materialized = min(_round_up(pull, _d(batch_multiple)), unlaunched)

    cumulative = Decimal("0")
    first_shortage: date | None = None
    for row in ordered:
        if row.need_date > protection_until:
            break
        cumulative += _d(row.qty)
        if cumulative > projected:
            first_shortage = row.need_date
            break
    latest_start = (
        first_shortage - timedelta(days=replenishment_time_days)
        if first_shortage is not None
        else None
    )
    return ShelfProjectionResult(
        protection_until=protection_until,
        target_qty=target,
        shelf_physical_qty=physical,
        other_stock_qty=other,
        confirmed_open_production_qty=confirmed,
        projected_qty=projected,
        gap_qty=gap,
        transfer_qty=transfer,
        unlaunched_mrp_qty=unlaunched,
        pull_qty=pull,
        materialized_qty=materialized,
        first_shortage_date=first_shortage,
        latest_start_date=latest_start,
    )

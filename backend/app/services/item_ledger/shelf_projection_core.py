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
class ShelfReceipt:
    """Confirmed replenishment that lands on the shelf on a known date.

    The canon counts a confirmed order as shelf coverage only when it arrives
    ``до нужной даты``.  An undated confirmed quantity therefore cannot be
    time-phased and is treated as an opening balance instead.
    """

    available_from: date
    qty: Decimal


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


def _project_timely_coverage(
    ordered: list[ShelfDemand],
    *,
    protection_until: date,
    opening_balance: Decimal,
    receipts: list[ShelfReceipt],
) -> tuple[Decimal, date | None]:
    """Coverage that reaches the shelf in time, and the first shortage date.

    ``shelf_projected_qty`` is the protected drum demand that the shelf balance
    plus timely confirmed receipts actually serve.  Quantity that only shows up
    after a need date stays out of the projection: it cannot repair a shortage
    that already happened, so it must not shrink ``shelf_gap_qty``.
    """
    balance = opening_balance
    covered = Decimal("0")
    first_shortage: date | None = None
    next_receipt = 0
    for row in ordered:
        if row.need_date > protection_until:
            break
        while (
            next_receipt < len(receipts)
            and receipts[next_receipt].available_from <= row.need_date
        ):
            balance += _d(receipts[next_receipt].qty)
            next_receipt += 1
        required = _d(row.qty)
        served = min(max(balance, Decimal("0")), required)
        covered += served
        balance -= served
        if served < required and first_shortage is None:
            first_shortage = row.need_date
    return covered, first_shortage


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
    saved_addressed_transfer_qty: Decimal = Decimal("0"),
    confirmed_receipts: tuple[ShelfReceipt, ...] = (),
) -> ShelfProjectionResult:
    """Calculate timing only; never create demand beyond frozen MRP."""
    if min(replenishment_time_days, review_cycle_days, safety_days) < 0:
        raise ValueError("shelf protection days must be non-negative")
    open_qty = max(_d(open_mrp_qty), Decimal("0"))
    physical = _d(shelf_physical_qty)
    other = max(_d(other_stock_qty), Decimal("0"))
    addressed_transfer = max(_d(saved_addressed_transfer_qty), Decimal("0"))
    dated_receipts = sorted(
        (row for row in confirmed_receipts if _d(row.qty) > 0),
        key=lambda row: row.available_from,
    )
    confirmed = sum(
        (_d(row.qty) for row in dated_receipts), Decimal("0")
    )
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
    # Canon "что считается покрытием полки": for every need date only what is
    # already on the shelf, or confirmed to land there before that date, counts
    # — net of the earlier drum consumption that ate the same balance first.
    # Walking the drum dates is therefore the projection; a flat
    # ``physical + confirmed`` would let a late receipt cover an earlier
    # shortage and silently suppress the mech-shop pull.
    projected, first_shortage = _project_timely_coverage(
        ordered,
        protection_until=protection_until,
        opening_balance=physical,
        receipts=dated_receipts,
    )
    gap = max(target - projected, Decimal("0"))
    # Only a saved addressable transfer for this requirement may use free stock
    # from another warehouse to reduce the pull. Unaddressed residue remains
    # informational and cannot stop mech-shop production.
    transfer = min(addressed_transfer, other)
    unlaunched = max(open_qty - confirmed, Decimal("0"))
    pull = min(max(gap - transfer, Decimal("0")), unlaunched)
    materialized = min(_round_up(pull, _d(batch_multiple)), unlaunched)

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

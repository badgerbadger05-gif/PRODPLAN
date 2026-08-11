"""Pure §16 reservation-consumption allocator.

The allocator has no persistence or ORM dependencies and accepts already filtered
consumption facts plus matching live reserves.  It applies addressed FIFO with a
single exact-addressed reservation run/component step, then falls back to global
FIFO within the same stock pool.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Literal, Optional, Tuple


MatchRule = Literal["fifo", "pegged"]


def _dec(value) -> Decimal:
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as error:
        raise ValueError(f"malformed decimal {value!r}") from error


@dataclass(frozen=True)
class Fact:
    fact_id: str
    item_id: int
    qty: Decimal
    posting_at: datetime
    pool: str
    characteristic_ref: str = ""
    organization_ref: str = ""
    requirement_id: Optional[int] = None
    run_id: Optional[int] = None
    reservation_id: Optional[str] = None


@dataclass(frozen=True)
class Reserve:
    reserve_id: str
    requirement_id: int
    run_id: int
    reserved_qty: Decimal
    baseline_at: datetime
    plan_period_from: date
    plan_period_to: date
    item_id: int
    pool: str
    characteristic_ref: str = ""
    organization_ref: str = ""


@dataclass(frozen=True)
class Allocation:
    fact_id: str
    reserve_id: str
    qty: Decimal
    match_rule: MatchRule
    is_addressed: bool = False


@dataclass(frozen=True)
class SurplusFact:
    fact_id: str
    qty: Decimal
    reason: str


@dataclass(frozen=True)
class ReserveRealization:
    reserve_id: str
    reserved_qty: Decimal
    realized_qty: Decimal


@dataclass(frozen=True)
class ConsumptionAllocationResult:
    allocations: Tuple[Allocation, ...]
    surplus: Tuple[SurplusFact, ...]
    realizations: Tuple[ReserveRealization, ...]
    fact_qty: Decimal
    allocated_qty: Decimal
    surplus_qty: Decimal


def _pool_key(value: Fact | Reserve) -> tuple[int, str, str, str]:
    return (
        int(value.item_id),
        str(value.characteristic_ref or ""),
        str(value.organization_ref or ""),
        str(value.pool),
    )


def _reserve_sort_key(reserve: Reserve) -> tuple[date, date, int, int, str]:
    return (
        reserve.plan_period_from,
        reserve.plan_period_to,
        int(reserve.run_id),
        int(reserve.requirement_id),
        str(reserve.reserve_id),
    )


def _fact_sort_key(fact: Fact) -> tuple[datetime, str]:
    return (fact.posting_at, str(fact.fact_id))


def _validate(facts: tuple[Fact, ...], reserves: tuple[Reserve, ...]) -> None:
    fact_ids = [fact.fact_id for fact in facts]
    if len(fact_ids) != len(set(fact_ids)):
        raise ValueError("fact_id must be unique")

    reserve_ids = [row.reserve_id for row in reserves]
    if len(reserve_ids) != len(set(reserve_ids)):
        raise ValueError("reserve_id must be unique")

    for fact in facts:
        if fact.posting_at is None:
            raise ValueError(f"fact {fact.fact_id}: posting_at is required")
        qty = _dec(fact.qty)
        if qty <= 0:
            raise ValueError(f"fact {fact.fact_id}: qty must be positive")

    for reserve in reserves:
        qty = _dec(reserve.reserved_qty)
        if qty <= 0:
            raise ValueError(f"reserve {reserve.reserve_id}: reserved_qty must be positive")
        if reserve.baseline_at is None:
            raise ValueError(
                f"reserve {reserve.reserve_id}: baseline_at is required"
            )


def _addressed_candidates(
    fact: Fact,
    compatible: list[Reserve],
) -> list[Reserve]:
    exact: list[Reserve] = []
    if fact.reservation_id is not None:
        exact = [row for row in compatible if row.reserve_id == fact.reservation_id]
    elif fact.run_id is not None and fact.requirement_id is not None:
        exact = [
            row
            for row in compatible
            if row.run_id == fact.run_id and row.requirement_id == fact.requirement_id
        ]
    elif fact.run_id is not None:
        exact = [row for row in compatible if row.run_id == fact.run_id]
    elif fact.requirement_id is not None:
        exact = [row for row in compatible if row.requirement_id == fact.requirement_id]
    return exact


def allocate_consumption_facts(
    facts: Iterable[Fact],
    reserves: Iterable[Reserve],
) -> ConsumptionAllocationResult:
    """Allocate positive consumption facts against senior reserves deterministically."""

    fact_rows = tuple(facts)
    reserve_rows = tuple(reserves)
    _validate(fact_rows, reserve_rows)

    ordered_reserves = tuple(sorted(reserve_rows, key=_reserve_sort_key))
    mutable_pool: dict[str, Decimal] = {
        row.reserve_id: _dec(row.reserved_qty) for row in ordered_reserves
    }
    realized = {row.reserve_id: Decimal("0") for row in ordered_reserves}

    allocations: list[Allocation] = []
    surplus: list[SurplusFact] = []

    def place(
        fact: Fact,
        qty: Decimal,
        candidates: Iterable[Reserve],
        rule: MatchRule,
        *,
        is_addressed: bool = False,
    ) -> Decimal:
        remaining = qty
        for reserve in candidates:
            if remaining <= 0:
                break
            available = mutable_pool.get(str(reserve.reserve_id), Decimal("0"))
            if available <= 0:
                continue
            take = min(remaining, available)
            allocations.append(
                Allocation(
                    fact_id=fact.fact_id,
                    reserve_id=str(reserve.reserve_id),
                    qty=take,
                    match_rule=rule,
                    is_addressed=is_addressed,
                )
            )
            mutable_pool[str(reserve.reserve_id)] = available - take
            realized[str(reserve.reserve_id)] += take
            remaining -= take
        return remaining

    sorted_facts = tuple(sorted(fact_rows, key=_fact_sort_key))
    for fact in sorted_facts:
        compatible = [
            reserve
            for reserve in ordered_reserves
            if _pool_key(reserve) == _pool_key(fact)
            and fact.posting_at > reserve.baseline_at
        ]
        exact = _addressed_candidates(fact, compatible)
        if len(exact) == 1:
            remaining = place(
                fact=fact,
                qty=_dec(fact.qty),
                candidates=exact,
                rule="pegged",
                is_addressed=True,
            )
        else:
            remaining = _dec(fact.qty)

        remaining = place(
            fact=fact,
            qty=remaining,
            candidates=compatible,
            rule="fifo",
        )

        if remaining > 0:
            surplus.append(SurplusFact(fact.fact_id, remaining, "no_live_replenishment_demand"))

    realizations = tuple(
        ReserveRealization(row.reserve_id, _dec(row.reserved_qty), realized[row.reserve_id])
        for row in ordered_reserves
    )
    fact_qty = sum((_dec(fact.qty) for fact in fact_rows), Decimal("0"))
    allocated_qty = sum((row.qty for row in allocations), Decimal("0"))
    surplus_qty = sum((row.qty for row in surplus), Decimal("0"))

    if fact_qty != allocated_qty + surplus_qty:
        raise AssertionError("fact conservation violated")

    if any(realized[row.reserve_id] > _dec(row.reserved_qty) for row in ordered_reserves):
        raise AssertionError("realized_qty exceeds reserved_qty")

    return ConsumptionAllocationResult(
        allocations=tuple(allocations),
        surplus=tuple(surplus),
        realizations=tuple(realizations),
        fact_qty=fact_qty,
        allocated_qty=allocated_qty,
        surplus_qty=surplus_qty,
    )

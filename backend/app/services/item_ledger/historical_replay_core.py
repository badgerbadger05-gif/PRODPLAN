"""Pure historical Item Ledger replay allocator.

The module deliberately has no persistence or ORM dependencies.  Callers load
accepted physical facts and frozen reservations, then persist the immutable
result in a separate orchestration layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Literal, Optional, Tuple


Mode = Literal["make", "buy"]
MatchRule = Literal["fifo", "pegged"]


@dataclass(frozen=True)
class Fact:
    fact_id: str
    item_id: int
    mode: Mode
    qty: Decimal
    posting_at: datetime
    characteristic_ref: str = ""
    organization_ref: str = ""
    planning_stock_pool: str = "selected"
    requirement_id: Optional[int] = None
    order_ref: Optional[str] = None
    is_reversal: bool = False


@dataclass(frozen=True)
class Reserve:
    reserve_id: str
    item_id: int
    mode: Mode
    reserved_qty: Decimal
    due_date: date
    plan_period_from: date
    plan_period_to: date
    run_id: int
    requirement_id: int
    bucket_date: Optional[date] = None
    bucket_id: Optional[int] = None
    characteristic_ref: str = ""
    organization_ref: str = ""
    planning_stock_pool: str = "selected"
    order_refs: Tuple[str, ...] = ()


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
class ReplayResult:
    allocations: Tuple[Allocation, ...]
    surplus: Tuple[SurplusFact, ...]
    realizations: Tuple[ReserveRealization, ...]
    fact_qty: Decimal
    allocated_qty: Decimal
    surplus_qty: Decimal


def _pool_key(value: Fact | Reserve) -> tuple[int, str, str, Mode]:
    return (
        int(value.item_id),
        str(value.organization_ref),
        str(value.planning_stock_pool),
        value.mode,
    )


def _reserve_key(reserve: Reserve) -> tuple:
    return (
        reserve.plan_period_from,
        reserve.plan_period_to,
        int(reserve.run_id),
        int(reserve.requirement_id),
        reserve.bucket_date or reserve.due_date,
        reserve.bucket_id if reserve.bucket_id is not None else -1,
        reserve.reserve_id,
    )


def _fact_key(fact: Fact) -> tuple:
    return (fact.posting_at, fact.fact_id)


def _is_addressed_match(fact: Fact, reserve: Reserve) -> bool:
    if fact.requirement_id is not None and fact.requirement_id == reserve.requirement_id:
        return True
    if (
        fact.requirement_id is None
        and fact.order_ref is not None
        and fact.order_ref in reserve.order_refs
    ):
        return True
    return False


def _validate(facts: tuple[Fact, ...], reserves: tuple[Reserve, ...]) -> None:
    fact_ids = [fact.fact_id for fact in facts]
    reserve_ids = [reserve.reserve_id for reserve in reserves]
    if len(fact_ids) != len(set(fact_ids)):
        raise ValueError("fact_id must be unique")
    if len(reserve_ids) != len(set(reserve_ids)):
        raise ValueError("reserve_id must be unique")
    for fact in facts:
        if fact.mode not in ("make", "buy"):
            raise ValueError(f"fact {fact.fact_id}: unsupported mode")
        if fact.is_reversal or fact.qty <= 0:
            raise ValueError(
                f"fact {fact.fact_id}: reversals and signed/non-positive quantities "
                "must be normalized explicitly before replay"
            )
    for reserve in reserves:
        if reserve.mode not in ("make", "buy"):
            raise ValueError(f"reserve {reserve.reserve_id}: unsupported mode")
        if reserve.reserved_qty < 0:
            raise ValueError(f"reserve {reserve.reserve_id}: reserved_qty must be non-negative")


def allocate_historical_facts(
    facts: Iterable[Fact],
    reserves: Iterable[Reserve],
) -> ReplayResult:
    """Allocate accepted historical facts without mutating either input.

    Positive replenishment always favors exact requirement/order matches (pegged),
    then continues with deterministic global FIFO. Unknown or ambiguous identity
    takes FIFO directly.
    """

    fact_rows = tuple(facts)
    reserve_rows = tuple(reserves)
    _validate(fact_rows, reserve_rows)

    ordered_reserves = tuple(sorted(reserve_rows, key=_reserve_key))
    remaining = {row.reserve_id: row.reserved_qty for row in ordered_reserves}
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
        left = qty
        for reserve in candidates:
            if left <= 0:
                break
            available = remaining[reserve.reserve_id]
            if available <= 0:
                continue
            take = min(left, available)
            allocations.append(
                Allocation(
                    fact.fact_id,
                    reserve.reserve_id,
                    take,
                    rule,
                    is_addressed=is_addressed,
                )
            )
            remaining[reserve.reserve_id] -= take
            realized[reserve.reserve_id] += take
            left -= take
        return left

    sorted_facts = tuple(sorted(fact_rows, key=_fact_key))
    leftovers = {fact.fact_id: fact.qty for fact in sorted_facts}

    for fact in sorted_facts:
        left = leftovers[fact.fact_id]
        compatible = [
            reserve
            for reserve in ordered_reserves
            if _pool_key(reserve) == _pool_key(fact)
        ]
        exact = [reserve for reserve in compatible if _is_addressed_match(fact, reserve)]
        # One requirement may legitimately have several dated reserve slices.
        # An order reference shared by different requirements is ambiguous and
        # must not invent a peg.
        exact_requirement_ids = {reserve.requirement_id for reserve in exact}
        address_is_unambiguous = bool(exact) and (
            fact.requirement_id is not None or len(exact_requirement_ids) == 1
        )
        if address_is_unambiguous:
            left = place(fact, left, exact, "pegged", is_addressed=True)

        if left > 0:
            left = place(fact, left, compatible, "fifo")

        if left > 0:
            surplus.append(
                SurplusFact(fact.fact_id, left, "no_live_replenishment_demand")
            )

    realization_rows = tuple(
        ReserveRealization(row.reserve_id, row.reserved_qty, realized[row.reserve_id])
        for row in ordered_reserves
    )
    fact_qty = sum((row.qty for row in fact_rows), Decimal("0"))
    allocated_qty = sum((row.qty for row in allocations), Decimal("0"))
    surplus_qty = sum((row.qty for row in surplus), Decimal("0"))

    if allocated_qty + surplus_qty != fact_qty:
        raise AssertionError("fact conservation violated")
    if any(row.realized_qty > row.reserved_qty for row in realization_rows):
        raise AssertionError("realized_qty exceeds reserved_qty")

    return ReplayResult(
        allocations=tuple(allocations),
        surplus=tuple(surplus),
        realizations=realization_rows,
        fact_qty=fact_qty,
        allocated_qty=allocated_qty,
        surplus_qty=surplus_qty,
    )

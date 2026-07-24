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


Mode = Literal["make", "consume"]
MatchRule = Literal["requirement", "order", "fifo"]


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


@dataclass(frozen=True)
class UnplannedFact:
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
    unplanned: Tuple[UnplannedFact, ...]
    realizations: Tuple[ReserveRealization, ...]
    fact_qty: Decimal
    allocated_qty: Decimal
    unplanned_qty: Decimal


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


def _validate(facts: tuple[Fact, ...], reserves: tuple[Reserve, ...]) -> None:
    fact_ids = [fact.fact_id for fact in facts]
    reserve_ids = [reserve.reserve_id for reserve in reserves]
    if len(fact_ids) != len(set(fact_ids)):
        raise ValueError("fact_id must be unique")
    if len(reserve_ids) != len(set(reserve_ids)):
        raise ValueError("reserve_id must be unique")
    for fact in facts:
        if fact.mode not in ("make", "consume"):
            raise ValueError(f"fact {fact.fact_id}: unsupported mode")
        if fact.is_reversal or fact.qty <= 0:
            raise ValueError(
                f"fact {fact.fact_id}: reversals and signed/non-positive quantities "
                "must be normalized explicitly before replay"
            )
    for reserve in reserves:
        if reserve.mode not in ("make", "consume"):
            raise ValueError(f"reserve {reserve.reserve_id}: unsupported mode")
        if reserve.reserved_qty < 0:
            raise ValueError(f"reserve {reserve.reserve_id}: reserved_qty must be non-negative")


def allocate_historical_facts(
    facts: Iterable[Fact],
    reserves: Iterable[Reserve],
) -> ReplayResult:
    """Allocate accepted historical facts without mutating either input.

    For ``consume`` facts, matching order is exact requirement, exact
    exported/source order, then fail-closed if still unaddressed.
    ``make`` facts bypass exact matching and use canonical FIFO only.  Canonical
    FIFO priority follows reserve plan periods and run identity before any
    posting date consideration.

    Unaddressed ``consume`` facts remain explicitly unplanned.
    """

    fact_rows = tuple(facts)
    reserve_rows = tuple(reserves)
    _validate(fact_rows, reserve_rows)

    ordered_reserves = tuple(sorted(reserve_rows, key=_reserve_key))
    remaining = {row.reserve_id: row.reserved_qty for row in ordered_reserves}
    realized = {row.reserve_id: Decimal("0") for row in ordered_reserves}
    allocations: list[Allocation] = []
    unplanned: list[UnplannedFact] = []

    def place(fact: Fact, qty: Decimal, candidates: Iterable[Reserve], rule: MatchRule) -> Decimal:
        left = qty
        for reserve in candidates:
            if left <= 0:
                break
            available = remaining[reserve.reserve_id]
            if available <= 0:
                continue
            take = min(left, available)
            allocations.append(Allocation(fact.fact_id, reserve.reserve_id, take, rule))
            remaining[reserve.reserve_id] -= take
            realized[reserve.reserve_id] += take
            left -= take
        return left

    sorted_facts = tuple(sorted(fact_rows, key=_fact_key))
    leftovers = {fact.fact_id: fact.qty for fact in sorted_facts}

    # Phase 1: consume claims are addressed by explicit links before FIFO.
    # Make facts intentionally skip exact match to preserve FIFO-first
    # reserve exhaustion regardless of document execution timing.
    for fact in sorted_facts:
        if fact.mode != "consume":
            continue
        compatible = [
            reserve
            for reserve in ordered_reserves
            if _pool_key(reserve) == _pool_key(fact)
        ]
        left = leftovers[fact.fact_id]

        if fact.requirement_id is not None:
            exact = [
                reserve
                for reserve in compatible
                if reserve.requirement_id == fact.requirement_id
            ]
            left = place(fact, left, exact, "requirement")

        if left > 0 and fact.order_ref:
            by_order = [
                reserve
                for reserve in compatible
                if fact.order_ref in reserve.order_refs
                and (
                    fact.requirement_id is None
                    or reserve.requirement_id != fact.requirement_id
                )
            ]
            left = place(fact, left, by_order, "order")
        leftovers[fact.fact_id] = left

    # Phase 2: only make/output may use pool FIFO.  Consume without remaining
    # exact address is deliberately left unplanned.
    for fact in sorted_facts:
        left = leftovers[fact.fact_id]
        compatible = [
            reserve
            for reserve in ordered_reserves
            if _pool_key(reserve) == _pool_key(fact)
        ]
        if left > 0 and fact.mode == "make":
            # Exact surplus and genuinely unaddressed output may satisfy the
            # next oldest make obligation in the same planning pool.
            left = place(fact, left, compatible, "fifo")

        if left > 0:
            reason = (
                "consume_fact_requires_address"
                if fact.mode == "consume" and not (fact.requirement_id or fact.order_ref)
                else "no_eligible_reserve_capacity"
            )
            unplanned.append(UnplannedFact(fact.fact_id, left, reason))

    realization_rows = tuple(
        ReserveRealization(row.reserve_id, row.reserved_qty, realized[row.reserve_id])
        for row in ordered_reserves
    )
    fact_qty = sum((row.qty for row in fact_rows), Decimal("0"))
    allocated_qty = sum((row.qty for row in allocations), Decimal("0"))
    unplanned_qty = sum((row.qty for row in unplanned), Decimal("0"))

    if allocated_qty + unplanned_qty != fact_qty:
        raise AssertionError("fact conservation violated")
    if any(row.realized_qty > row.reserved_qty for row in realization_rows):
        raise AssertionError("realized_qty exceeds reserved_qty")

    return ReplayResult(
        allocations=tuple(allocations),
        unplanned=tuple(unplanned),
        realizations=realization_rows,
        fact_qty=fact_qty,
        allocated_qty=allocated_qty,
        unplanned_qty=unplanned_qty,
    )

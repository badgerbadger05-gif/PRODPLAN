"""Pure deterministic allocation of accepted assembly-output facts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


ZERO = Decimal("0")


@dataclass(frozen=True)
class OutputFact:
    stock_ledger_entry_id: int
    item_id: int
    qty: Decimal
    exact_plan_line_ids: tuple[int, ...] = ()
    link_kind: str = "none"


@dataclass(frozen=True)
class QueueCandidate:
    plan_id: int
    plan_line_id: int
    item_id: int
    open_qty: Decimal


@dataclass(frozen=True)
class OutputAllocation:
    stock_ledger_entry_id: int
    plan_id: int
    plan_line_id: int
    qty: Decimal
    match_rule: str
    allocation_ordinal: int


@dataclass(frozen=True)
class OutputDecision:
    stock_ledger_entry_id: int
    decision_status: str
    link_kind: str
    allocations: tuple[OutputAllocation, ...]
    surplus_qty: Decimal
    reason: str | None = None


def allocate_output_fact(
    fact: OutputFact,
    candidates: Iterable[QueueCandidate],
) -> OutputDecision:
    """Allocate one fact to caller-ordered live plan rows.

    Candidate order is the canonical FIFO order. Exact provenance may select
    one row first; any excess continues through the same FIFO without exceeding
    either the fact or a plan-line remainder.
    """
    fact_qty = max(Decimal(fact.qty), ZERO)
    ordered = tuple(candidates)
    by_id = {row.plan_line_id: row for row in ordered}
    exact_ids = tuple(dict.fromkeys(int(value) for value in fact.exact_plan_line_ids))
    if len(exact_ids) > 1:
        return OutputDecision(
            stock_ledger_entry_id=fact.stock_ledger_entry_id,
            decision_status="ambiguous",
            link_kind=fact.link_kind,
            allocations=(),
            surplus_qty=fact_qty,
            reason="multiple exact plan-line candidates",
        )

    remaining = fact_qty
    open_by_line = {
        row.plan_line_id: max(Decimal(row.open_qty), ZERO)
        for row in ordered
        if row.item_id == fact.item_id
    }
    allocations: list[OutputAllocation] = []

    def take(row: QueueCandidate, rule: str) -> None:
        nonlocal remaining
        available = open_by_line.get(row.plan_line_id, ZERO)
        qty = min(remaining, available)
        if qty <= ZERO:
            return
        allocations.append(OutputAllocation(
            stock_ledger_entry_id=fact.stock_ledger_entry_id,
            plan_id=row.plan_id,
            plan_line_id=row.plan_line_id,
            qty=qty,
            match_rule=rule,
            allocation_ordinal=len(allocations),
        ))
        open_by_line[row.plan_line_id] = available - qty
        remaining -= qty

    exact_id = exact_ids[0] if exact_ids else None
    exact = by_id.get(exact_id) if exact_id is not None else None
    if exact is not None and exact.item_id == fact.item_id:
        take(exact, "exact")

    for row in ordered:
        if remaining <= ZERO:
            break
        take(row, "fifo")

    return OutputDecision(
        stock_ledger_entry_id=fact.stock_ledger_entry_id,
        decision_status="allocatable",
        link_kind=fact.link_kind,
        allocations=tuple(allocations),
        surplus_qty=remaining,
    )

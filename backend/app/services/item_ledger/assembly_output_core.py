"""Pure deterministic allocation of accepted assembly-output facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
    posting_at: datetime | None = None
    provenance_status: str = "none"
    provenance_reason: str | None = None


@dataclass(frozen=True)
class QueueCandidate:
    plan_id: int
    plan_line_id: int
    item_id: int
    open_qty: Decimal
    sort_key: str = ""
    eligible_from: datetime | None = None


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

    Candidate order is the canonical FIFO order. A fact with exact plan-line
    provenance may close only that line: its surplus must not be reassigned to
    another plan. FIFO is used only when exact plan provenance is absent.

    ``eligible_from`` is the plan's immutable fixation boundary. A physical
    output before that boundary cannot close the plan. ``None`` remains useful
    for pure-core callers; the persistence adapter fails closed and does not
    emit candidates without a historical boundary.
    """
    fact_qty = max(Decimal(fact.qty), ZERO)
    ordered = tuple(candidates)
    by_id = {row.plan_line_id: row for row in ordered}
    exact_ids = tuple(dict.fromkeys(int(value) for value in fact.exact_plan_line_ids))
    allocations: list[OutputAllocation] = []
    remaining = fact_qty
    open_by_line = {
        row.plan_line_id: max(Decimal(row.open_qty), ZERO)
        for row in ordered
        if row.item_id == fact.item_id and _eligible_for_fact(fact, row)
    }

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

    if fact.provenance_status in {"ambiguous", "invalid"}:
        return OutputDecision(
            stock_ledger_entry_id=fact.stock_ledger_entry_id,
            decision_status=fact.provenance_status,
            link_kind=fact.link_kind,
            allocations=(),
            surplus_qty=fact_qty,
            reason=fact.provenance_reason,
        )
    if len(exact_ids) > 1:
        exact_rows = tuple(
            row
            for row in ordered
            if row.item_id == fact.item_id
            and int(row.plan_line_id) in exact_ids
        )
        exact_rows = tuple(
            row
            for row in exact_rows
            if _eligible_for_fact(fact, row)
        )
        exact_plan_ids = {int(row.plan_id) for row in exact_rows}
        if len(exact_rows) > 0 and len(exact_plan_ids) == 1:
            for row in exact_rows:
                take(row, "exact")
            return OutputDecision(
                stock_ledger_entry_id=fact.stock_ledger_entry_id,
                decision_status="allocatable",
                link_kind=fact.link_kind,
                allocations=tuple(allocations),
                surplus_qty=remaining,
            )
        exact_rows_present = any(int(value) in by_id for value in exact_ids)
        if not exact_rows_present:
            return OutputDecision(
                stock_ledger_entry_id=fact.stock_ledger_entry_id,
                decision_status="invalid",
                link_kind=fact.link_kind,
                allocations=(),
                surplus_qty=fact_qty,
                reason="exact plan lines are not in the live-plan scope",
            )
        else:
            return OutputDecision(
                stock_ledger_entry_id=fact.stock_ledger_entry_id,
                decision_status="ambiguous",
                link_kind=fact.link_kind,
                allocations=(),
                surplus_qty=fact_qty,
                reason="multiple exact plan-line candidates",
            )

    exact_id = exact_ids[0] if exact_ids else None
    exact = by_id.get(exact_id) if exact_id is not None else None
    if exact_id is not None:
        if exact is None:
            return OutputDecision(
                stock_ledger_entry_id=fact.stock_ledger_entry_id,
                decision_status="invalid",
                link_kind=fact.link_kind,
                allocations=(),
                surplus_qty=fact_qty,
                reason="exact plan line is not in the live-plan scope",
            )
        if exact.item_id != fact.item_id:
            return OutputDecision(
                stock_ledger_entry_id=fact.stock_ledger_entry_id,
                decision_status="invalid",
                link_kind=fact.link_kind,
                allocations=(),
                surplus_qty=fact_qty,
                reason="exact plan line item differs from physical output item",
            )
        if not _eligible_for_fact(fact, exact):
            return OutputDecision(
                stock_ledger_entry_id=fact.stock_ledger_entry_id,
                decision_status="invalid",
                link_kind=fact.link_kind,
                allocations=(),
                surplus_qty=fact_qty,
                reason="physical output predates exact plan fixation",
            )
        take(exact, "exact")
    elif fact.provenance_status == "exact":
        return OutputDecision(
            stock_ledger_entry_id=fact.stock_ledger_entry_id,
            decision_status="invalid",
            link_kind=fact.link_kind,
            allocations=(),
            surplus_qty=fact_qty,
            reason=fact.provenance_reason or "exact provenance has no plan line",
        )
    else:
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


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=None)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _eligible_for_fact(fact: OutputFact, candidate: QueueCandidate) -> bool:
    if fact.posting_at is None or candidate.eligible_from is None:
        return True
    return _as_utc_naive(fact.posting_at) >= _as_utc_naive(candidate.eligible_from)

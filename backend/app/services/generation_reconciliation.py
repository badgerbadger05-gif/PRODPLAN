"""Read-only decision input for accepted, generation-scoped reconciliation.

This module deliberately stops at targets.  It never creates proposals and it
never reads the mutable execution/coverage caches on ``MrpRequirement``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from .. import models


_MAKE_FACT_TYPES = frozenset({"linked_production", "unlinked_production"})
_CONSUME_FACT_TYPES = frozenset({"component_consumption"})
_SAFE_FACT_TYPES = _MAKE_FACT_TYPES | _CONSUME_FACT_TYPES | frozenset({"supplier_receipt"})


class GenerationReconciliationMismatch(RuntimeError):
    """Accepted generation is internally inconsistent and cannot drive writes."""


@dataclass(frozen=True)
class ReconciliationTarget:
    requirement_id: int
    realization_mode: str
    reserved_qty: Decimal
    realized_qty: Decimal
    target_qty: Decimal


def _d(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _fold_events(
    entries: Iterable[models.ReservationEntry],
    events: Iterable[models.ReservationEvent],
) -> dict[int, tuple[Decimal, Decimal]]:
    folded = {int(entry.id): (Decimal("0"), Decimal("0")) for entry in entries}
    for event in events:
        reservation_id = int(event.reservation_id)
        if reservation_id not in folded:
            raise GenerationReconciliationMismatch(
                f"event {event.id} points outside accepted generation reservations"
            )
        reserved, realized = folded[reservation_id]
        folded[reservation_id] = (
            reserved + _d(event.reserved_delta),
            realized + _d(event.realized_delta),
        )
    return folded


def build_generation_targets(
    db: Session,
    *,
    ledger_generation_id: int,
    run_id: int | None = None,
) -> dict[tuple[int, str], ReconciliationTarget]:
    """Validate one accepted generation and return proposal sizing targets.

    ``make`` is sized from reservation outstanding (reserved minus realized).
    ``consume`` is sized from the generation-scoped uncovered projection.  The
    realization side is independently cross-checked against explainable
    execution allocations before any caller is allowed to mutate proposals.
    """
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None or str(generation.status or "") != "accepted":
        raise GenerationReconciliationMismatch(
            f"ledger generation {ledger_generation_id} is not accepted"
        )
    truth_state = db.get(models.PlanningTruthState, 1)
    if (
        truth_state is None
        or truth_state.current_generation_id is None
        or int(truth_state.current_generation_id) != int(ledger_generation_id)
    ):
        raise GenerationReconciliationMismatch(
            f"ledger generation {ledger_generation_id} is not the currently published truth"
        )

    entries_q = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.ledger_generation_id == int(ledger_generation_id),
    )
    if run_id is not None:
        entries_q = entries_q.filter(models.ReservationEntry.run_id == int(run_id))
    entries = entries_q.order_by(models.ReservationEntry.id.asc()).all()
    entry_ids = [int(entry.id) for entry in entries]
    requirement_ids = sorted({int(entry.requirement_id) for entry in entries})

    events = []
    if entry_ids:
        events = (
            db.query(models.ReservationEvent)
            .filter(
                models.ReservationEvent.ledger_generation_id == int(ledger_generation_id),
                models.ReservationEvent.reservation_id.in_(entry_ids),
            )
            .order_by(models.ReservationEvent.id.asc())
            .all()
        )
    folded = _fold_events(entries, events)

    allocation_totals: dict[tuple[int, str], Decimal] = {}
    if requirement_ids:
        allocations = (
            db.query(models.MrpExecutionAllocation)
            .filter(
                models.MrpExecutionAllocation.ledger_generation_id
                == int(ledger_generation_id),
                models.MrpExecutionAllocation.requirement_id.in_(requirement_ids),
                models.MrpExecutionAllocation.allocation_kind == "execution",
            )
            .all()
        )
        for allocation in allocations:
            fact_type = str(allocation.fact_type or "")
            if fact_type not in _SAFE_FACT_TYPES:
                raise GenerationReconciliationMismatch(
                    f"unsafe execution fact_type={fact_type!r} for allocation {allocation.id}"
                )
            if fact_type == "supplier_receipt":
                if str(allocation.allocation_kind or "") != "execution":
                    raise GenerationReconciliationMismatch(
                        "unsafe supplier_receipt execution mode for non-execution allocation "
                        f"{allocation.id}"
                    )
                mode = "buy"
            else:
                mode = "make" if fact_type in _MAKE_FACT_TYPES else "consume"
            key = (int(allocation.requirement_id), mode)
            allocation_totals[key] = allocation_totals.get(key, Decimal("0")) + _d(
                allocation.allocated_qty
            )

    grouped: dict[tuple[int, str], dict[str, Decimal]] = {}
    for entry in entries:
        mode = str(entry.realization_mode or "")
        if mode not in {"make", "consume", "buy"}:
            raise GenerationReconciliationMismatch(
                f"reservation {entry.id} has unsupported realization_mode={mode!r}"
            )
        folded_reserved, folded_realized = folded[int(entry.id)]
        # Materialized caches are useful corruption detectors, never inputs.
        if folded_reserved != _d(entry.reserved_qty) or folded_realized != _d(entry.realized_qty):
            raise GenerationReconciliationMismatch(
                f"reservation {entry.id} cache does not equal event fold"
            )
        key = (int(entry.requirement_id), mode)
        row = grouped.setdefault(
            key,
            {"reserved": Decimal("0"), "realized": Decimal("0"), "uncovered": Decimal("0")},
        )
        row["reserved"] += folded_reserved
        row["realized"] += folded_realized
        if str(entry.lifecycle_status or "") in {"active", "carried"}:
            row["uncovered"] += max(_d(entry.uncovered_qty), Decimal("0"))

    targets: dict[tuple[int, str], ReconciliationTarget] = {}
    for key, values in grouped.items():
        allocation_qty = allocation_totals.pop(key, Decimal("0"))
        if allocation_qty != values["realized"]:
            raise GenerationReconciliationMismatch(
                f"requirement {key[0]} mode {key[1]}: allocation={allocation_qty} "
                f"but realized events={values['realized']}"
            )
        target = (
            max(values["reserved"] - values["realized"], Decimal("0"))
            if key[1] == "make"
            else values["uncovered"]
        )
        targets[key] = ReconciliationTarget(
            requirement_id=key[0],
            realization_mode=key[1],
            reserved_qty=values["reserved"],
            realized_qty=values["realized"],
            target_qty=target,
        )

    if allocation_totals:
        key = sorted(allocation_totals)[0]
        raise GenerationReconciliationMismatch(
            f"execution allocation has no reservation for requirement {key[0]} mode {key[1]}"
        )
    return targets

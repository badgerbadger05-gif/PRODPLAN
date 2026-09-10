"""Transactional current replenishment application.

R0 owns the pure addressed/FIFO change planner.  This module is the single
database writer for the first current-execution slice: it applies that plan to
the already accepted generation's reservation allocation rows, folds the
result into the existing reservation execution fields, and records a small
source/revision marker in the same transaction.  It deliberately never creates
or copies a generation or snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Iterable, Literal

from sqlalchemy.orm import Session

from app import models

from .historical_replay_core import (
    Allocation,
    Fact,
    Reserve,
    plan_allocation_changes,
)


WRITER_KEY = "current_replenishment"
DistributionScope = tuple[int, str, str, str, str]


class CurrentReplenishmentError(ValueError):
    """A current application is unavailable and must fail closed."""


@dataclass(frozen=True)
class CurrentReplenishmentResult:
    generation_id: int
    source_key: str
    source_revision: int
    inserted: int
    updated: int
    deleted: int
    changed_pairs: int
    audit_events: int
    idempotent: bool = False


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _text(value: object) -> str:
    return str(value or "").strip()


def _distribution_scope(value: Fact | Reserve) -> tuple[int, str, str, str, str]:
    return (
        int(value.item_id),
        _text(value.characteristic_ref),
        _text(value.organization_ref),
        _text(value.planning_stock_pool),
        _text(value.mode),
    )


def _allocation_scope(
    row: models.ReservationConsumptionAllocation,
    entry: models.ReservationEntry,
) -> tuple[int, str, str, str, str]:
    return (
        int(row.item_id),
        _text(row.characteristic_ref),
        _text(row.organization_ref),
        _text(row.planning_stock_pool),
        _text(entry.realization_mode),
    )


def _scope_key(scope: DistributionScope) -> str:
    return json.dumps(list(scope), ensure_ascii=False, separators=(",", ":"))


def _input_checksum(
    facts: tuple[Fact, ...], reserves: tuple[Reserve, ...], scope_key: str
) -> str:
    payload = {
        "distribution_scope": scope_key,
        "facts": [
            {
                "id": str(row.fact_id),
                "item": int(row.item_id),
                "mode": row.mode,
                "qty": str(row.qty),
                "posting_at": row.posting_at.isoformat(),
                "characteristic": _text(row.characteristic_ref),
                "organization": _text(row.organization_ref),
                "pool": _text(row.planning_stock_pool),
                "requirement": row.requirement_id,
                "order": row.order_ref,
            }
            for row in sorted(facts, key=lambda item: str(item.fact_id))
        ],
        "reserves": [
            {
                "id": str(row.reserve_id),
                "item": int(row.item_id),
                "mode": row.mode,
                "qty": str(row.reserved_qty),
                "due": row.due_date.isoformat(),
                "from": row.plan_period_from.isoformat(),
                "to": row.plan_period_to.isoformat(),
                "run": int(row.run_id),
                "requirement": int(row.requirement_id),
                "characteristic": _text(row.characteristic_ref),
                "organization": _text(row.organization_ref),
                "pool": _text(row.planning_stock_pool),
            }
            for row in sorted(reserves, key=lambda item: str(item.reserve_id))
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _as_allocation(row: models.ReservationConsumptionAllocation) -> Allocation:
    return Allocation(
        fact_id=str(int(row.sle_id)),
        reserve_id=str(int(row.reservation_id)),
        qty=_decimal(row.allocated_qty),
        match_rule=_text(row.match_rule),
        is_addressed=_text(row.match_rule) == "pegged",
    )


def _require_complete_scope(
    facts: tuple[Fact, ...],
    reserves: tuple[Reserve, ...],
    complete_scope: bool,
    distribution_scope: DistributionScope | None,
) -> DistributionScope:
    if complete_scope is not True:
        raise CurrentReplenishmentError(
            "current replenishment requires an explicit complete scope"
        )
    fact_ids = [str(f.fact_id) for f in facts]
    reserve_ids = [str(r.reserve_id) for r in reserves]
    if len(fact_ids) != len(set(fact_ids)):
        raise CurrentReplenishmentError("complete scope contains duplicate facts")
    if len(reserve_ids) != len(set(reserve_ids)):
        raise CurrentReplenishmentError("complete scope contains duplicate reservations")
    scopes = {_distribution_scope(row) for row in (*facts, *reserves)}
    if len(scopes) > 1:
        raise CurrentReplenishmentError(
            "complete scope must cover exactly one distribution pool"
        )
    if not scopes:
        if distribution_scope is None:
            raise CurrentReplenishmentError(
                "empty complete scope requires distribution_scope"
            )
        return tuple(distribution_scope)
    derived = next(iter(scopes))
    if distribution_scope is not None and tuple(distribution_scope) != derived:
        raise CurrentReplenishmentError(
            "distribution_scope does not match complete scope inputs"
        )
    return derived


def _audit_change(
    db: Session,
    *,
    state: models.CurrentReplenishmentState,
    generation: models.LedgerGeneration,
    entry: models.ReservationEntry,
    fact_id: str,
    scope_key: str,
    source_revision: int,
    operation: str,
    before: Allocation | None,
    after: Allocation | None,
) -> None:
    """Audit one changed basis pair, never the unchanged assignment matrix."""

    db.add(
        models.CurrentReplenishmentAudit(
            state_id=int(state.id),
            ledger_generation_id=int(generation.id),
            scope_key=str(scope_key),
            source_revision=int(source_revision),
            sle_id=int(fact_id),
            reservation_id=int(entry.id),
            operation=str(operation),
            before_qty=before.qty if before is not None else None,
            after_qty=after.qty if after is not None else None,
            before_match_rule=before.match_rule if before is not None else None,
            after_match_rule=after.match_rule if after is not None else None,
        )
    )


def apply_current_replenishment(
    db: Session,
    *,
    generation_id: int,
    source_key: str,
    source_revision: int,
    facts: Iterable[Fact],
    reserves: Iterable[Reserve],
    complete_scope: bool,
    distribution_scope: DistributionScope | None = None,
    fail_after: Literal["assignments", "execution", "marker"] | None = None,
    writer: str = WRITER_KEY,
) -> CurrentReplenishmentResult:
    """Apply one complete accepted-fact scope atomically.

    The caller owns the surrounding transaction (the function intentionally
    does not commit).  PostgreSQL locks the generation row and all existing
    allocation rows before checking the source marker, which serializes two
    imports for the same allocation scope.  A stale revision is rejected and
    the exact same revision is an idempotent no-op.
    """

    fact_rows = tuple(facts)
    reserve_rows = tuple(reserves)
    distribution_scope = _require_complete_scope(
        fact_rows, reserve_rows, complete_scope, distribution_scope
    )
    canonical_scope_key = _scope_key(distribution_scope)
    input_checksum = _input_checksum(fact_rows, reserve_rows, canonical_scope_key)
    if _text(writer) != WRITER_KEY:
        raise CurrentReplenishmentError(
            "single current writer is current_replenishment; legacy writer is retired"
        )
    if not _text(source_key):
        raise CurrentReplenishmentError("source_key is required")
    try:
        revision = int(source_revision)
    except (TypeError, ValueError) as exc:
        raise CurrentReplenishmentError("source_revision must be an integer") from exc
    if revision < 0:
        raise CurrentReplenishmentError("source_revision must be non-negative")

    generation = (
        db.query(models.LedgerGeneration)
        .filter(models.LedgerGeneration.id == int(generation_id))
        .with_for_update()
        .one_or_none()
    )
    if generation is None:
        raise CurrentReplenishmentError(f"generation {generation_id} does not exist")
    if _text(generation.status) != "accepted":
        raise CurrentReplenishmentError("current replenishment requires accepted generation")

    state = (
        db.query(models.CurrentReplenishmentState)
        .filter(models.CurrentReplenishmentState.scope_key == canonical_scope_key)
        .with_for_update()
        .one_or_none()
    )
    if state is not None:
        if _text(state.writer_key) != WRITER_KEY:
            raise CurrentReplenishmentError("single current writer is current_replenishment")
        if _text(state.source_key) != _text(source_key):
            raise CurrentReplenishmentError(
                "source stream changed for canonical distribution scope"
            )
        previous_revision = int(state.source_revision)
        if revision < previous_revision:
            raise CurrentReplenishmentError(
                f"stale source revision {revision}; current is {previous_revision}"
            )
        if revision == previous_revision:
            if _text(state.scope_checksum) != input_checksum:
                raise CurrentReplenishmentError("same source revision has payload drift")
            if _text(state.status) != "completed":
                raise CurrentReplenishmentError("current source marker is still applying")
            generation_changed = int(state.ledger_generation_id) != int(generation.id)
            if generation_changed:
                state.ledger_generation_id = int(generation.id)
                state.updated_at = datetime.now(timezone.utc)
                db.flush()
            return CurrentReplenishmentResult(
                generation_id=int(generation.id),
                source_key=_text(source_key),
                source_revision=revision,
                inserted=0,
                updated=0,
                deleted=0,
                changed_pairs=0,
                audit_events=0,
                idempotent=not generation_changed,
            )
    else:
        state = models.CurrentReplenishmentState(
            scope_key=canonical_scope_key,
            source_key=_text(source_key),
            ledger_generation_id=int(generation.id),
            source_revision=revision,
            scope_checksum=input_checksum,
            writer_key=WRITER_KEY,
            status="applying",
        )
        db.add(state)
        db.flush()

    allocations = (
        db.query(models.ReservationConsumptionAllocation)
        .join(
            models.ReservationEntry,
            models.ReservationEntry.id
            == models.ReservationConsumptionAllocation.reservation_id,
        )
        .filter(
            models.ReservationConsumptionAllocation.is_current.is_(True),
            models.ReservationConsumptionAllocation.item_id == distribution_scope[0],
            models.ReservationConsumptionAllocation.characteristic_ref == distribution_scope[1],
            models.ReservationConsumptionAllocation.organization_ref == distribution_scope[2],
            models.ReservationConsumptionAllocation.planning_stock_pool == distribution_scope[3],
            models.ReservationEntry.realization_mode == distribution_scope[4],
        )
        .with_for_update()
        .order_by(models.ReservationConsumptionAllocation.id.asc())
        .all()
    )
    legacy_allocations = (
        db.query(models.ReservationConsumptionAllocation)
        .join(
            models.ReservationEntry,
            models.ReservationEntry.id
            == models.ReservationConsumptionAllocation.reservation_id,
        )
        .filter(
            models.ReservationConsumptionAllocation.ledger_generation_id
            == int(generation.id),
            models.ReservationConsumptionAllocation.is_current.is_(False),
            models.ReservationConsumptionAllocation.item_id == distribution_scope[0],
            models.ReservationConsumptionAllocation.characteristic_ref == distribution_scope[1],
            models.ReservationConsumptionAllocation.organization_ref == distribution_scope[2],
            models.ReservationConsumptionAllocation.planning_stock_pool == distribution_scope[3],
            models.ReservationEntry.realization_mode == distribution_scope[4],
        )
        .with_for_update()
        .all()
    )
    allocation_entries = (
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.id.in_(
                sorted({int(row.reservation_id) for row in allocations} | {
                    int(row.reserve_id) for row in reserve_rows
                })
            )
        )
        .with_for_update()
        .all()
        if allocations or reserve_rows
        else []
    )
    allocation_entry_by_id = {str(int(row.id)): row for row in allocation_entries}
    scoped_allocations = tuple(
        row
        for row in allocations
        if distribution_scope is not None
        and str(row.reservation_id) in allocation_entry_by_id
        and _allocation_scope(
            row, allocation_entry_by_id[str(row.reservation_id)]
        ) == distribution_scope
    )
    previous = tuple(_as_allocation(row) for row in scoped_allocations)
    try:
        plan = plan_allocation_changes(
            fact_rows,
            reserve_rows,
            previous_allocations=previous,
        )
    except (TypeError, ValueError) as exc:
        raise CurrentReplenishmentError(str(exc)) from exc

    entries = allocation_entries
    entry_by_id = {str(int(row.id)): row for row in entries}
    fact_by_id = {str(row.fact_id): row for row in fact_rows}
    reserve_by_id = {str(row.reserve_id): row for row in reserve_rows}
    allocation_by_key = {
        (str(int(row.sle_id)), str(int(row.reservation_id))): row for row in allocations
        if row in scoped_allocations
    }
    legacy_by_key = {
        (str(int(row.sle_id)), str(int(row.reservation_id))): row
        for row in legacy_allocations
    }
    audit_events = 0

    for old in plan.deletions:
        row = allocation_by_key[(old.fact_id, old.reserve_id)]
        entry = entry_by_id.get(old.reserve_id)
        if entry is None:
            raise CurrentReplenishmentError(
                f"allocation references missing reservation {old.reserve_id}"
            )
        db.delete(row)
        _audit_change(
            db,
            state=state,
            generation=generation,
            entry=entry,
            fact_id=old.fact_id,
            scope_key=canonical_scope_key,
            source_revision=revision,
            operation="delete",
            before=old,
            after=None,
        )
        audit_events += 1

    for update in plan.updates:
        row = allocation_by_key[(update.before.fact_id, update.before.reserve_id)]
        entry = entry_by_id.get(update.after.reserve_id)
        if entry is None:
            raise CurrentReplenishmentError(
                f"allocation references missing reservation {update.after.reserve_id}"
            )
        row.allocated_qty = update.after.qty
        row.match_rule = update.after.match_rule
        row.ledger_generation_id = int(generation.id)
        _audit_change(
            db,
            state=state,
            generation=generation,
            entry=entry,
            fact_id=update.after.fact_id,
            scope_key=canonical_scope_key,
            source_revision=revision,
            operation="update",
            before=update.before,
            after=update.after,
        )
        audit_events += 1

    for insertion in plan.insertions:
        fact = fact_by_id.get(insertion.fact_id)
        reserve = reserve_by_id.get(insertion.reserve_id)
        entry = entry_by_id.get(insertion.reserve_id)
        if fact is None or reserve is None or entry is None:
            raise CurrentReplenishmentError("allocation scope references unknown fact or reservation")
        key = (insertion.fact_id, insertion.reserve_id)
        row = legacy_by_key.get(key)
        if row is None:
            row = models.ReservationConsumptionAllocation(
                ledger_generation_id=int(generation.id),
                reservation_id=int(entry.id),
                sle_id=int(fact.fact_id),
                requirement_id=int(entry.requirement_id),
                allocated_qty=insertion.qty,
                match_rule=insertion.match_rule,
                fact_ref=_text(fact.fact_id),
                fact_line_ref="",
                item_id=int(entry.item_id),
                characteristic_ref=_text(entry.characteristic_ref),
                organization_ref=_text(entry.organization_ref),
                planning_stock_pool=_text(reserve.planning_stock_pool) or "default",
                idempotency_key=(
                    f"r4:{source_key}:{revision}:{insertion.fact_id}:{insertion.reserve_id}"
                ),
                is_current=True,
                event_at=fact.posting_at,
            )
            db.add(row)
        else:
            # A pre-R4 generation copy is promoted in place.  Its row ID is
            # retained; no duplicate current allocation is created.
            row.is_current = True
            row.ledger_generation_id = int(generation.id)
            row.allocated_qty = insertion.qty
            row.match_rule = insertion.match_rule
            row.idempotency_key = (
                f"r4:{source_key}:{revision}:{insertion.fact_id}:{insertion.reserve_id}"
            )
            row.planning_stock_pool = _text(reserve.planning_stock_pool) or "default"
        _audit_change(
            db,
            state=state,
            generation=generation,
            entry=entry,
            fact_id=insertion.fact_id,
            scope_key=canonical_scope_key,
            source_revision=revision,
            operation="insert",
            before=None,
            after=insertion,
        )
        audit_events += 1

    db.flush()
    if fail_after == "assignments":
        raise CurrentReplenishmentError("injected failure after assignments")

    realization_by_reserve = {
        str(row.reserve_id): _decimal(row.realized_qty)
        for row in plan.result.realizations
    }
    for reserve_id, realized in realization_by_reserve.items():
        entry = entry_by_id.get(reserve_id)
        reserve = reserve_by_id.get(reserve_id)
        if entry is None or reserve is None:
            raise CurrentReplenishmentError(f"realization references unknown reservation {reserve_id}")
        received = min(max(realized, Decimal("0")), _decimal(entry.replenishment_required_qty))
        if _decimal(entry.replenishment_received_qty) != received:
            entry.replenishment_received_qty = received
            entry.realized_qty = received
    db.flush()
    if fail_after == "execution":
        raise CurrentReplenishmentError("injected failure after execution")

    state.status = "completed"
    state.ledger_generation_id = int(generation.id)
    state.source_revision = revision
    state.scope_checksum = input_checksum
    state.changed_pairs = len(plan.insertions) + len(plan.updates) + len(plan.deletions)
    state.updated_at = datetime.now(timezone.utc)
    db.flush()
    if fail_after == "marker":
        raise CurrentReplenishmentError("injected failure after marker")

    return CurrentReplenishmentResult(
        generation_id=int(generation.id),
        source_key=_text(source_key),
        source_revision=revision,
        inserted=len(plan.insertions),
        updated=len(plan.updates),
        deleted=len(plan.deletions),
        changed_pairs=len(plan.insertions) + len(plan.updates) + len(plan.deletions),
        audit_events=audit_events,
    )


def read_current_replenishment(
    db: Session, *, generation_id: int, item_id: int | None = None
) -> list[dict[str, object]]:
    """Read the accepted current assignment basis without lineage traversal."""

    query = (
        db.query(models.ReservationConsumptionAllocation)
        .join(
            models.ReservationEntry,
            models.ReservationEntry.id
            == models.ReservationConsumptionAllocation.reservation_id,
        )
        .filter(
            models.ReservationConsumptionAllocation.is_current.is_(True),
        )
        .order_by(
            models.ReservationConsumptionAllocation.sle_id.asc(),
            models.ReservationConsumptionAllocation.reservation_id.asc(),
        )
    )
    if item_id is not None:
        query = query.filter(models.ReservationConsumptionAllocation.item_id == int(item_id))
    return [
        {
            "id": int(row.id),
            "sle_id": int(row.sle_id),
            "reservation_id": int(row.reservation_id),
            "requirement_id": int(row.requirement_id),
            "allocated_qty": _decimal(row.allocated_qty),
            "match_rule": _text(row.match_rule),
            "item_id": int(row.item_id),
            "planning_stock_pool": _text(row.planning_stock_pool),
        }
        for row in query.all()
    ]


def reject_legacy_supplier_receipt_writer(
    db: Session, reservation: models.ReservationEntry
) -> None:
    """Retire ReservationEvent as a second writer after R4 owns a scope.

    ``ReservationEvent`` remains historical evidence for pre-R4 generation
    builds.  Once the accepted current writer has a completed marker for this
    business scope, a supplier-receipt rebuild must not fold the same receipt
    into the reservation a second time.
    """

    scope = (
        int(reservation.item_id),
        _text(reservation.characteristic_ref),
        _text(reservation.organization_ref),
        _text(reservation.planning_stock_pool),
        _text(reservation.realization_mode),
    )
    state = (
        db.query(models.CurrentReplenishmentState)
        .filter(models.CurrentReplenishmentState.scope_key == _scope_key(scope))
        .first()
    )
    if state is not None and _text(state.status) == "completed":
        raise CurrentReplenishmentError(
            "supplier receipt ReservationEvent writer is retired for an R4 current scope"
        )


def apply_current_replenishment_for_accepted_generation(
    db: Session, *, generation_id: int, source_revision: int | None = None
) -> tuple[CurrentReplenishmentResult, ...]:
    """Publish current supplier-receipt replenishment at physical acceptance.

    This is the production orchestration adapter: it reads only the accepted
    generation's positive receipt facts and its frozen buy reservations, then
    delegates all persistence to :func:`apply_current_replenishment` in the
    caller's transaction.  A single item cannot be silently fanned out to
    multiple pools because the physical receipt has no pool identity; such an
    ambiguous input fails closed.
    """

    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or _text(generation.status) != "accepted":
        raise CurrentReplenishmentError(
            "current replenishment publication requires an accepted generation"
        )
    reservations = tuple(
        db.query(models.ReservationEntry)
        .filter(
            models.ReservationEntry.ledger_generation_id == int(generation_id),
            models.ReservationEntry.lifecycle_status == "active",
            models.ReservationEntry.realization_mode == "buy",
            models.ReservationEntry.replenishment_required_qty > 0,
        )
        .order_by(models.ReservationEntry.id.asc())
        .all()
    )
    if not reservations:
        return ()
    item_scopes: dict[int, set[DistributionScope]] = {}
    for row in reservations:
        item_scopes.setdefault(int(row.item_id), set()).add(
            (
                int(row.item_id),
                _text(row.characteristic_ref),
                _text(row.organization_ref),
                _text(row.planning_stock_pool),
                _text(row.realization_mode),
            )
        )
    for item_id, scopes in item_scopes.items():
        if len(scopes) > 1:
            raise CurrentReplenishmentError(
                f"receipt facts for item {item_id} have ambiguous distribution pools"
            )
    facts_by_item: dict[int, tuple[Fact, ...]] = {}
    sle_rows = (
        db.query(models.StockLedgerEntry)
        .filter(
            models.StockLedgerEntry.ingest_batch_id == generation.physical_import_batch_id,
            models.StockLedgerEntry.active.is_(True),
            models.StockLedgerEntry.movement_kind == "receipt",
            models.StockLedgerEntry.qty > 0,
        )
        .order_by(models.StockLedgerEntry.id.asc())
        .all()
    )
    for item_id, scope_set in item_scopes.items():
        scope = next(iter(scope_set))
        facts_by_item[item_id] = tuple(
            Fact(
                fact_id=str(row.id),
                item_id=int(row.item_id),
                mode="buy",
                qty=_decimal(row.qty),
                posting_at=row.posting_at,
                characteristic_ref=scope[1],
                organization_ref=scope[2],
                planning_stock_pool=scope[3],
            )
            for row in sle_rows
            if int(row.item_id) == item_id
        )
    revision = int(source_revision if source_revision is not None else generation.id)
    result: list[CurrentReplenishmentResult] = []
    for item_id, scope_set in sorted(item_scopes.items()):
        scope = next(iter(scope_set))
        reserve_rows = tuple(
            Reserve(
                reserve_id=str(row.id),
                item_id=int(row.item_id),
                mode="buy",
                reserved_qty=_decimal(row.replenishment_required_qty),
                due_date=row.priority_period_to,
                plan_period_from=row.priority_period_from,
                plan_period_to=row.priority_period_to,
                run_id=int(row.run_id or 0),
                requirement_id=int(row.requirement_id),
                characteristic_ref=scope[1],
                organization_ref=scope[2],
                planning_stock_pool=scope[3],
            )
            for row in reservations
            if int(row.item_id) == item_id
        )
        facts = facts_by_item[item_id]
        if not facts:
            # An accepted complete scope with no physical receipt explicitly
            # clears only this scope; it is not an unavailable empty import.
            facts = ()
        result.append(
            apply_current_replenishment(
                db,
                generation_id=int(generation_id),
                source_key="accepted-physical-receipts",
                source_revision=revision,
                facts=facts,
                reserves=reserve_rows,
                distribution_scope=scope,
                complete_scope=True,
            )
        )
    return tuple(result)

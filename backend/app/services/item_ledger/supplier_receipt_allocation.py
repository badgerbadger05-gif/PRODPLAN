"""Supplier receipt provenance and coverage reshaping for one Ledger generation.

The caller supplies normalized 1C document evidence.  This module deliberately
does not read OData or legacy ``received_qty`` projections.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Literal, Mapping

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from .reservation import (
    append_realization_event,
    fold_reservation_entry,
    replenishment_remaining,
)

from .historical_replay_core import known_at_freeze
from .physical import canonical_content_hash, canonical_decimal
from .physical_visibility import visible_sles_for_generation
from .current_replenishment import reject_legacy_supplier_receipt_writer

logger = logging.getLogger(__name__)

RECEIPT_OPERATION = "8d97069c"
CORRECTION_OPERATION = "8d96f940"
SUPPLIER_RETURN_OPERATION = "8d96f436"
TRANSFER_OPERATION = "8d970232"
SUPPLIER_ORDER_TYPE = "Document_ЗаказПоставщику"
#: ``receipt_doc_type`` values that name the writer instead of the 1C
#: document the fact was recorded by.  The bounded physical refresh stored
#: this marker before the one-row contract; such a row cannot be matched to
#: its SLE as stored and is resolved from the SLE it already references.
WRITER_MARKER_DOCUMENT_TYPES = frozenset({"bounded_physical_refresh"})

_OPERATION_NAMES = {
    RECEIPT_OPERATION: frozenset({
        "приобретение у поставщика",
        "поступление от поставщика",
        "поступлениеотпоставщика",
        "закупка у поставщика",
    }),
    CORRECTION_OPERATION: frozenset({
        "корректировка приобретения",
        "корректировка поступления",
        "корректировка по согласованию сторон",
        # УНФ ЗСМ: перечисление ВидыОперацийКорректировкаПоступления
        # называет ту же операцию «СогласованноеИзменение» (наблюдено на
        # Document_КорректировкаПоступления ЗСНФ-000001 с основанием
        # ПриходнаяНакладная и ключом 8d96f940-…).
        "согласованное изменение",
        "согласованноеизменение",
    }),
    SUPPLIER_RETURN_OPERATION: frozenset({
        "возврат поставщику",
        "возвратпоставщику",
    }),
    TRANSFER_OPERATION: frozenset({
        "перемещение товаров",
        "перемещение запасов",
        "перемещение",
    }),
}

_OPERATION_KINDS = {
    RECEIPT_OPERATION: "supplier_receipt",
    CORRECTION_OPERATION: "correction",
    SUPPLIER_RETURN_OPERATION: "supplier_return",
    TRANSFER_OPERATION: "transfer",
}

#: One canonical ``(operation_key, operation_name)`` per kind, for a writer
#: that determined the operation itself instead of copying it off a 1C
#: document line.  The normalizer resolves a row back to its operation from
#: exactly these two fields, so a writer that invents its own marker produces
#: a row nothing can rebuild - which is what "unsupported supplier document
#: operation 'bounded_physical_refresh'" meant.
_CANONICAL_OPERATION_BY_KIND = {
    "supplier_receipt": (RECEIPT_OPERATION, "приобретение у поставщика"),
    "correction": (CORRECTION_OPERATION, "корректировка приобретения"),
    "supplier_return": (SUPPLIER_RETURN_OPERATION, "возврат поставщику"),
}


def resolves_to_documented_operation(operation_key: str, operation_name: str) -> bool:
    """Whether this key/name pair names one documented 1C operation."""
    key = str(operation_key or "").strip().lower()
    name = " ".join(str(operation_name or "").strip().lower().split())
    return sum(
        1 for prefix, names in _OPERATION_NAMES.items()
        if key.startswith(prefix) and name in names
    ) == 1


def canonical_operation_for_kind(kind: str) -> tuple[str, str]:
    """The documented operation key/name for one supplier operation kind."""
    try:
        return _CANONICAL_OPERATION_BY_KIND[str(kind)]
    except KeyError as exc:
        raise SupplierReceiptEvidenceError(
            f"unsupported supplier operation kind {kind!r}"
        ) from exc


class SupplierReceiptEvidenceError(ValueError):
    """Normalized evidence is unsupported or contradicts the physical Ledger."""


@dataclass(frozen=True)
class SupplierDocumentEvidence:
    receipt_doc_type: str
    receipt_doc_ref: str
    receipt_doc_line_no: str
    operation_key: str
    operation_name: str
    supplier_order_type: str
    supplier_order_ref: str
    supplier_order_line_no: str
    item_id: int
    characteristic_ref: str
    warehouse_ref1c: str
    signed_qty: Decimal
    correction_receipt_ref: str | None = None


@dataclass(frozen=True)
class ReceiptFact:
    sle_id: int
    posting_at: datetime
    signed_qty: Decimal
    item_id: int
    supplier_order_ref: str
    supplier_order_line_no: str
    receipt_ref: str
    receipt_line_no: str
    correction_receipt_ref: str | None = None
    # ``posting_at`` is when the source movement happened.  ``known_at`` is
    # when PRODPLAN accepted the evidence; reports choose the axis explicitly.
    known_at: datetime | None = None
    planning_stock_pool: str = "default"
    # The 1C document type of ``supplier_order_ref``.  Set by the normalizer
    # for an exact line only - the only type that can be exact - so a writer
    # that persists the fact records which order document it matched.  It is
    # evidence metadata, not allocation input: two facts that allocate the
    # same way are equal whether or not one was re-read from persisted rows.
    supplier_order_type: str = field(default="", compare=False)
    # Decision §51: the physical import batch that made the fact known - the
    # as-known axis of the freeze boundary.  Provenance, not allocation
    # identity, so it does not take part in equality.
    ingest_batch_id: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class NormalizedSupplierReceiptFact:
    """One canonical evidence-to-SLE normalization result.

    The matcher is read-only and deliberately returns the status alongside
    the allocator fact.  Callers must not infer ``exact`` from the raw
    supplier-order reference: a document line ``0`` is exact only when the
    canonical order lookup resolves one positive order line.
    """

    fact: ReceiptFact
    evidence: SupplierDocumentEvidence
    operation: str
    match_status: str
    canonical_line_no: str | None
    match_rule: str
    ambiguity_count: int
    reason: str | None


@dataclass(frozen=True)
class CoverageAllocation:
    fact: ReceiptFact
    reservation: models.ReservationEntry
    qty: Decimal
    match_rule: str = "fifo"
    # For a signed correction/return, the current positive basis remains the
    # original receipt.  The event fact is retained by provenance/audit.
    basis_fact: ReceiptFact | None = None
    basis_events: tuple[tuple[ReceiptFact, Decimal], ...] = ()


@dataclass(frozen=True)
class SupplierReceiptReplayResult:
    """Final positive basis after replaying every signed supplier event."""

    allocations: tuple[CoverageAllocation, ...]
    surplus_qty: Decimal
    convergence_boundary: int
    processed_fact_ids: tuple[int, ...]
    unmatched_return_qty: Decimal = Decimal("0")


HistoryMode = Literal["as_occurred", "as_known"]


def _history_time(fact: ReceiptFact, history_mode: HistoryMode) -> datetime:
    if history_mode == "as_occurred":
        return fact.posting_at
    if history_mode == "as_known":
        if fact.known_at is None:
            raise ValueError("history_mode as_known requires known_at")
        return fact.known_at
    raise ValueError("history_mode must be as_occurred or as_known")


@dataclass(frozen=True)
class SupplierReceiptBuildResult:
    provenance_count: int
    exact_fact_count: int
    allocation_count: int
    surplus_qty: Decimal


def _text(value: object) -> str:
    return str(value or "").strip()


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _normalized_type(value: object) -> str:
    result = _text(value)
    for prefix in ("StandardODATA.", "StandardODATA/"):
        if result.startswith(prefix):
            result = result[len(prefix):]
            break
    return result


def _order_line_key(item_id: int, order_ref: object, line_no: object) -> tuple[int, str, str] | None:
    normalized_order = _text(order_ref)
    normalized_line = _text(line_no)
    if not normalized_order or normalized_line in ("", "0"):
        return None
    return int(item_id), normalized_order, normalized_line


def _operation_prefix(row: SupplierDocumentEvidence) -> str:
    key = _text(row.operation_key).lower()
    name = " ".join(_text(row.operation_name).lower().split())
    matches = [
        prefix for prefix, names in _OPERATION_NAMES.items()
        if key.startswith(prefix) and name in names
    ]
    if len(matches) != 1:
        raise SupplierReceiptEvidenceError(
            f"unsupported supplier document operation {row.operation_key!r} "
            f"({row.operation_name!r})"
        )
    return matches[0]


def _entry_key(entry: models.ReservationEntry) -> int:
    entry_id = getattr(entry, "id", None)
    return int(entry_id) if entry_id is not None else id(entry)


def _entry_outstanding(entry: models.ReservationEntry) -> Decimal:
    if not hasattr(entry, "replenishment_required_qty"):
        return _decimal(entry.reserved_qty)
    return replenishment_remaining(
        entry.replenishment_required_qty,
        entry.replenishment_received_qty,
    )


def _buy_reservation_order(entry: models.ReservationEntry) -> tuple:
    return (
        entry.priority_period_from,
        entry.priority_period_to,
        int(entry.run_id) if entry.run_id is not None else 0,
        int(entry.requirement_id),
        _entry_key(entry),
    )


def _event_exists(db: Session, ledger_generation_id: int, idempotency_key: str) -> bool:
    return (
        db.query(models.ReservationEvent.id)
        .filter(
            models.ReservationEvent.ledger_generation_id == int(ledger_generation_id),
            models.ReservationEvent.idempotency_key == idempotency_key,
        )
        .first()
        is not None
    )


def _append_reservation_event(
    db: Session,
    allocation: CoverageAllocation,
    *,
    cycle_id: str,
) -> bool:
    reservation = allocation.reservation
    if reservation.id is None:
        return False
    # ReservationEvent remains historical evidence only.  Once the accepted
    # current scope has an R4 marker, this competing replenishment writer is
    # explicitly retired for that reservation.
    reject_legacy_supplier_receipt_writer(db, reservation)
    qty = _decimal(allocation.qty)
    if qty == 0:
        return False
    reservation_id = int(reservation.id)
    reservation_seeded = (
        db.query(models.ReservationEvent.id)
        .filter(
            models.ReservationEvent.ledger_generation_id == int(reservation.ledger_generation_id),
            models.ReservationEvent.reservation_id == reservation_id,
        )
        .first()
        is not None
    )
    key = (
        f"{'realize' if qty > 0 else 'unrealize'}:"
        f"{int(reservation.id)}:{int(allocation.fact.sle_id)}"
    )
    if _event_exists(db, int(reservation.ledger_generation_id), key):
        return False
    reserved_delta = (
        _decimal(reservation.reserved_qty)
        if (qty > 0 and not reservation_seeded)
        else Decimal("0")
    )
    return append_realization_event(
        db,
        reservation,
        realized_delta=qty,
        sle_id=int(allocation.fact.sle_id),
        fact_ref=str(allocation.fact.receipt_ref),
        fact_line_ref=f"{allocation.fact.receipt_line_no}#sle:{allocation.fact.sle_id}",
        match_rule="fifo",
        cycle_id=cycle_id,
        idempotency_key=key,
        event_at=allocation.fact.posting_at,
        reserved_delta=reserved_delta,
    )


def _buy_reservations_for_item(
    db: Session,
    ledger_generation_id: int,
    item_id: int,
) -> tuple[models.ReservationEntry, ...]:
    rows = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.ledger_generation_id == int(ledger_generation_id),
        models.ReservationEntry.item_id == int(item_id),
        models.ReservationEntry.planning_stock_pool == "default",
        models.ReservationEntry.realization_mode == "buy",
        models.ReservationEntry.lifecycle_status == "active",
    ).all()
    return tuple(sorted(rows, key=_buy_reservation_order))


def _exact_allocation_caps_by_order_line(
    db: Session,
    ledger_generation_id: int,
    item_ids: Iterable[int] | None = None,
    current_owner_ids: Iterable[int] | None = None,
) -> dict[tuple[int, str, str], dict[int, Decimal]]:
    """Return exact supplier-order caps for legacy or stable current owners.

    Historical callers retain the generation-scoped query.  The compact R4
    path passes stable current reservation IDs instead: generation is
    provenance there, not owner identity.  Export rows are grouped per source
    generation; identical repeated copies are deduplicated and conflicting
    copies fail closed rather than silently inflating an exact cap.
    """

    if current_owner_ids is not None:
        owner_ids = sorted({int(value) for value in current_owner_ids})
        if not owner_ids:
            return {}
        rows = (
            db.query(
                models.ReservationEntry.item_id,
                models.PurchaseExportObligationAllocation.supplier_order_ref,
                models.PurchaseExportObligationAllocation.supplier_order_line_no,
                models.PurchaseExportObligationAllocation.reservation_id,
                models.PurchaseExportObligationAllocation.ledger_generation_id,
                func.sum(models.PurchaseExportObligationAllocation.allocated_qty),
            )
            .join(
                models.ReservationEntry,
                models.ReservationEntry.id
                == models.PurchaseExportObligationAllocation.reservation_id,
            )
            .filter(
                models.PurchaseExportObligationAllocation.reservation_id.in_(owner_ids),
                models.ReservationEntry.id.in_(owner_ids),
                models.ReservationEntry.is_current.is_(True),
                models.ReservationEntry.planning_stock_pool == "default",
                models.ReservationEntry.realization_mode == "buy",
                models.ReservationEntry.lifecycle_status == "active",
            )
        )
        if item_ids is not None:
            rows = rows.filter(
                models.ReservationEntry.item_id.in_(
                    sorted({int(value) for value in item_ids})
                )
            )
        grouped = rows.group_by(
            models.ReservationEntry.item_id,
            models.PurchaseExportObligationAllocation.supplier_order_ref,
            models.PurchaseExportObligationAllocation.supplier_order_line_no,
            models.PurchaseExportObligationAllocation.reservation_id,
            models.PurchaseExportObligationAllocation.ledger_generation_id,
        ).all()
        values_by_key: dict[tuple[int, str, str, int], set[Decimal]] = {}
        for (
            item_id,
            supplier_order_ref,
            supplier_order_line_no,
            reservation_id,
            _source_generation_id,
            total,
        ) in grouped:
            key = (
                int(item_id),
                _text(supplier_order_ref),
                _text(supplier_order_line_no),
                int(reservation_id),
            )
            values_by_key.setdefault(key, set()).add(_decimal(total))
        caps: dict[tuple[int, str, str], dict[int, Decimal]] = {}
        for (item_id, order_ref, line_no, reservation_id), values in values_by_key.items():
            if len(values) != 1:
                raise SupplierReceiptEvidenceError(
                    "conflicting exact supplier-order caps across current-owner generations"
                )
            key = _order_line_key(item_id, order_ref, line_no)
            if key is None:
                continue
            caps.setdefault(key, {})[reservation_id] = next(iter(values))
        return caps

    query = (
        db.query(
            models.ReservationEntry.item_id,
            models.PurchaseExportObligationAllocation.supplier_order_ref,
            models.PurchaseExportObligationAllocation.supplier_order_line_no,
            models.PurchaseExportObligationAllocation.reservation_id,
            func.sum(models.PurchaseExportObligationAllocation.allocated_qty),
        )
        .join(
            models.ReservationEntry,
            models.ReservationEntry.id
            == models.PurchaseExportObligationAllocation.reservation_id,
        )
        .filter(
            models.PurchaseExportObligationAllocation.ledger_generation_id == int(ledger_generation_id),
            models.ReservationEntry.ledger_generation_id == int(ledger_generation_id),
            models.ReservationEntry.planning_stock_pool == "default",
            models.ReservationEntry.realization_mode == "buy",
            models.ReservationEntry.lifecycle_status == "active",
        )
    )
    if item_ids is not None:
        query = query.filter(models.ReservationEntry.item_id.in_(sorted({int(value) for value in item_ids})))
    rows = (
        query
        .group_by(
            models.ReservationEntry.item_id,
            models.PurchaseExportObligationAllocation.supplier_order_ref,
            models.PurchaseExportObligationAllocation.supplier_order_line_no,
            models.PurchaseExportObligationAllocation.reservation_id,
        )
        .all()
    )

    caps: dict[tuple[int, str, str], dict[int, Decimal]] = {}
    for item_id, supplier_order_ref, supplier_order_line_no, reservation_id, total in rows:
        key = _order_line_key(
            int(item_id),
            supplier_order_ref,
            supplier_order_line_no,
        )
        if key is None:
            continue
        caps.setdefault(key, {})[int(reservation_id)] = _decimal(total)
    return caps


def _validate_operations(rows: tuple[SupplierDocumentEvidence, ...]) -> None:
    transfers: dict[tuple[str, str, int, str], list[SupplierDocumentEvidence]] = {}
    for row in rows:
        operation = _operation_prefix(row)
        if operation in (RECEIPT_OPERATION, CORRECTION_OPERATION, SUPPLIER_RETURN_OPERATION):
            if operation == CORRECTION_OPERATION and not _text(row.correction_receipt_ref):
                raise SupplierReceiptEvidenceError(
                    f"correction {row.receipt_doc_ref}/{row.receipt_doc_line_no} "
                    "has no original receipt reference"
                )
            continue
        if operation == TRANSFER_OPERATION:
            key = (
                _text(row.receipt_doc_type),
                _text(row.receipt_doc_ref),
                int(row.item_id),
                _text(row.characteristic_ref),
            )
            transfers.setdefault(key, []).append(row)
            continue
    for key, pair in transfers.items():
        if (
            len(pair) != 2
            or sum((_decimal(row.signed_qty) for row in pair), Decimal("0")) != 0
            or abs(_decimal(pair[0].signed_qty)) != abs(_decimal(pair[1].signed_qty))
            or _text(pair[0].warehouse_ref1c) == _text(pair[1].warehouse_ref1c)
        ):
            raise SupplierReceiptEvidenceError(
                f"transfer {key[1]}/{key[2]} requires a balanced pair of rows"
            )


def allocate_supplier_receipts(
    facts: Iterable[ReceiptFact],
    reservations_by_item: dict[int, Iterable[models.ReservationEntry]],
    *,
    exact_allocation_caps: dict[tuple[int, str, str], dict[int, Decimal]] | None = None,
    history_mode: HistoryMode = "as_occurred",
    freeze_batch_by_reservation: Mapping[int, int] | None = None,
) -> tuple[tuple[CoverageAllocation, ...], Decimal]:
    """Pure deterministic allocator.

    ``freeze_batch_by_reservation`` is each owner's freeze boundary on the
    as-known axis (§49/§51); without it a reservation's own
    ``known_batch_id`` attribute is used (the R4 replay passes pure owners
    that carry it).

    Positive supplier receipts fill exact supplier-order-line matches first inside
    their export allocation cap, then FIFO by item. Returns unwind the same
    supplier-order-line first (newest-first), then global FIFO, then named
    original documents for explicit corrections.
    """
    ordered = sorted(
        tuple(facts),
        key=lambda row: (_history_time(row, history_mode), row.sle_id),
    )
    reservations = {
        item_id: tuple(
            sorted(
                (entry for entry in values if _entry_outstanding(entry) > 0),
                key=_buy_reservation_order,
            )
        )
        for item_id, values in reservations_by_item.items()
    }
    exact_caps = {
        key: dict(value) for key, value in (exact_allocation_caps or {}).items()
    }
    remaining: dict[int, Decimal] = {
        _entry_key(entry): _entry_outstanding(entry)
        for values in reservations.values()
        for entry in values
    }
    positive_by_receipt: dict[str, list[CoverageAllocation]] = {}
    active_by_order_line: dict[tuple[int, str, str], list[CoverageAllocation]] = {}
    active_by_item: dict[int, list[CoverageAllocation]] = {}
    active_qty: dict[int, Decimal] = {}
    result: list[CoverageAllocation] = []
    surplus = Decimal("0")
    for fact in ordered:
        qty = _decimal(fact.signed_qty)
        if qty > 0:
            item_id = int(fact.item_id)
            left = qty
            exact_key = _order_line_key(
                item_id,
                fact.supplier_order_ref,
                fact.supplier_order_line_no,
            )
            exact_caps_for_key: dict[int, Decimal] = {}
            if exact_key is not None:
                exact_caps_for_key = exact_caps.get(exact_key, {})
            item_reservations = tuple(
                entry
                for entry in reservations.get(item_id, ())
                if _text(getattr(entry, "planning_stock_pool", "default"))
                == _text(fact.planning_stock_pool)
                # Decisions §49/§51: a receipt known when the owner was frozen
                # is its frozen stock, never its replenishment.
                and not known_at_freeze(
                    fact.ingest_batch_id,
                    freeze_batch_by_reservation.get(int(_entry_key(entry)))
                    if freeze_batch_by_reservation is not None
                    else getattr(entry, "known_batch_id", None),
                )
            )

            for reservation in item_reservations:
                if left <= 0:
                    break
                key = _entry_key(reservation)
                outstanding = remaining.get(key, Decimal("0"))
                if not exact_caps_for_key:
                    continue
                exact_key_value = exact_caps_for_key.get(int(key), Decimal("0"))
                if exact_key_value <= 0:
                    continue
                exact_take = min(left, outstanding, exact_key_value)
                if exact_take <= 0:
                    continue
                exact_caps_for_key[int(key)] = exact_key_value - exact_take
                remaining[key] = outstanding - exact_take
                left -= exact_take
                allocation = CoverageAllocation(
                    fact=fact,
                    reservation=reservation,
                    qty=exact_take,
                    match_rule="pegged",
                    basis_fact=fact,
                )
                result.append(allocation)
                positive_by_receipt.setdefault(fact.receipt_ref, []).append(allocation)
                if exact_key is not None:
                    active_by_order_line.setdefault(exact_key, []).append(allocation)
                active_by_item.setdefault(item_id, []).append(allocation)
                active_qty[id(allocation)] = exact_take

            for reservation in item_reservations:
                if left <= 0:
                    break
                key = _entry_key(reservation)
                outstanding = remaining.get(key, Decimal("0"))
                if outstanding <= 0:
                    continue
                take = min(left, outstanding)
                if take <= 0:
                    continue
                allocation = CoverageAllocation(
                    fact=fact,
                    reservation=reservation,
                    qty=take,
                    match_rule="fifo",
                    basis_fact=fact,
                )
                result.append(allocation)
                positive_by_receipt.setdefault(fact.receipt_ref, []).append(allocation)
                if exact_key is not None:
                    active_by_order_line.setdefault(exact_key, []).append(allocation)
                active_by_item.setdefault(item_id, []).append(allocation)
                active_qty[id(allocation)] = take
                remaining[key] = outstanding - take
                left -= take
            if exact_key is not None and exact_key in exact_caps:
                exact_caps[exact_key] = exact_caps_for_key
            surplus += left
            continue

        left = -qty
        if left <= 0:
            continue
        exact_key = _order_line_key(
            int(fact.item_id),
            fact.supplier_order_ref,
            fact.supplier_order_line_no,
        )
        seen_ids: set[int] = set()
        source = (
            positive_by_receipt.get(_text(fact.correction_receipt_ref), [])
            if fact.correction_receipt_ref
            else []
        )
        unwind_by_reservation: dict[int, Decimal] = {}
        unwind_samples: dict[int, models.ReservationEntry] = {}
        unwind_basis: list[tuple[models.ReservationEntry, ReceiptFact, Decimal, str]] = []
        if fact.correction_receipt_ref:
            ordered_source = reversed(source)
        elif exact_key is not None:
            ordered_source = (
                list(reversed(active_by_order_line.get(exact_key, [])))
                + list(reversed(active_by_item.get(int(fact.item_id), [])))
            )
        else:
            ordered_source = reversed(active_by_item.get(int(fact.item_id), []))
        for original in ordered_source:
            if id(original) in seen_ids:
                continue
            seen_ids.add(id(original))
            available = active_qty.get(id(original), Decimal("0"))
            take = min(left, available)
            if take <= 0:
                continue
            reservation_key = _entry_key(original.reservation)
            unwind_by_reservation[reservation_key] = (
                unwind_by_reservation.get(reservation_key, Decimal("0")) + take
            )
            unwind_samples[reservation_key] = original.reservation
            unwind_basis.append(
                (
                    original.reservation,
                    original.basis_fact or original.fact,
                    take,
                    original.match_rule,
                )
            )
            active_qty[id(original)] = available - take
            left -= take
            if left == 0:
                break
        unwind_by_reservation_basis: dict[
            int, list[tuple[ReceiptFact, Decimal]]
        ] = {}
        unwind_match_rule: dict[int, str] = {}
        for reservation, basis, take, match_rule in unwind_basis:
            key = _entry_key(reservation)
            unwind_by_reservation_basis.setdefault(key, []).append((basis, take))
            unwind_match_rule[key] = match_rule
        for reservation_key, basis_events in unwind_by_reservation_basis.items():
            take = sum((qty for _basis, qty in basis_events), Decimal("0"))
            reservation = unwind_samples[reservation_key]
            result.append(CoverageAllocation(
                fact=fact,
                reservation=reservation,
                qty=-take,
                match_rule=unwind_match_rule[reservation_key],
                basis_fact=basis_events[0][0],
                basis_events=tuple(basis_events),
            ))

    return tuple(result), surplus


def replay_supplier_receipt_basis(
    facts: Iterable[ReceiptFact],
    reservations_by_item: dict[int, Iterable[models.ReservationEntry]],
    *,
    exact_allocation_caps: dict[tuple[int, str, str], dict[int, Decimal]] | None = None,
    history_mode: HistoryMode,
) -> SupplierReceiptReplayResult:
    """Replay the complete signed supplier stream into positive current basis.

    ``allocate_supplier_receipts`` remains the sole attribution algorithm.  A
    correction/return can unwind a prior positive allocation, but the current
    table stores only the resulting positive original-receipt basis.  Every
    input row is consumed; ``convergence_boundary`` is therefore a proof of
    processing the complete supplied source scope, not an arbitrary row limit.
    """

    rows = tuple(facts)
    if history_mode not in ("as_occurred", "as_known"):
        raise ValueError("history_mode must be as_occurred or as_known")
    if len({int(row.sle_id) for row in rows}) != len(rows):
        raise ValueError("supplier receipt facts require unique sle_id")
    active_reservations = {
        int(item_id): tuple(
            entry
            for entry in values
            if _text(getattr(entry, "lifecycle_status", "active")) == "active"
        )
        for item_id, values in reservations_by_item.items()
    }
    event_allocations, _event_surplus = allocate_supplier_receipts(
        rows,
        active_reservations,
        exact_allocation_caps=exact_allocation_caps,
        history_mode=history_mode,
    )
    reductions: dict[int, Decimal] = {}
    for row in event_allocations:
        if row.qty >= 0 or row.basis_fact is None:
            continue
        basis_events = row.basis_events or ((row.basis_fact, abs(_decimal(row.qty))),)
        for basis, quantity in basis_events:
            basis_id = int(basis.sle_id)
            reductions[basis_id] = reductions.get(basis_id, Decimal("0")) + _decimal(
                quantity
            )
    effective_facts: list[ReceiptFact] = []
    for row in rows:
        qty = _decimal(row.signed_qty)
        if qty <= 0:
            continue
        qty = max(Decimal("0"), qty - reductions.get(int(row.sle_id), Decimal("0")))
        if qty > 0:
            effective_facts.append(
                ReceiptFact(
                    **{
                        **row.__dict__,
                        "signed_qty": qty,
                    }
                )
            )
    final_allocations, surplus_qty = allocate_supplier_receipts(
        effective_facts,
        active_reservations,
        exact_allocation_caps=exact_allocation_caps,
        history_mode=history_mode,
    )
    merged: dict[tuple[int, int], CoverageAllocation] = {}
    for row in final_allocations:
        key = (int(row.fact.sle_id), _entry_key(row.reservation))
        current = merged.get(key)
        if current is None:
            merged[key] = row
        else:
            if current.match_rule == row.match_rule:
                merged_rule = current.match_rule
            else:
                merged_rule = "mixed"
            merged[key] = CoverageAllocation(
                fact=current.fact,
                reservation=current.reservation,
                qty=current.qty + row.qty,
                match_rule=merged_rule,
                basis_fact=current.basis_fact or current.fact,
            )
    # The positive replay is already the final basis; no negative row is
    # persisted and no arbitrary suffix is skipped.
    final = tuple(
        CoverageAllocation(
            fact=row.fact,
            reservation=row.reservation,
            qty=row.qty,
            match_rule=row.match_rule,
            basis_fact=row.fact,
        )
        for row in merged.values()
        if row.qty > 0
    )
    return SupplierReceiptReplayResult(
        allocations=final,
        surplus_qty=_decimal(surplus_qty),
        convergence_boundary=len(rows),
        processed_fact_ids=tuple(
            int(row.sle_id)
            for row in sorted(
                rows,
                key=lambda item: (_history_time(item, history_mode), item.sle_id),
            )
        ),
        unmatched_return_qty=(
            sum(
                (-_decimal(row.signed_qty) for row in rows if _decimal(row.signed_qty) < 0),
                Decimal("0"),
            )
            - sum(
                (-_decimal(row.qty) for row in event_allocations if _decimal(row.qty) < 0),
                Decimal("0"),
            )
        ),
    )


def _candidate_order_lines(
    db: Session,
    evidence: SupplierDocumentEvidence,
) -> list[models.SupplierOrderItem]:
    if _normalized_type(evidence.supplier_order_type) != SUPPLIER_ORDER_TYPE:
        return []
    order = db.query(models.SupplierOrder).filter(
        models.SupplierOrder.order_ref1c == _text(evidence.supplier_order_ref)
    ).one_or_none()
    if order is None:
        return []
    return db.query(models.SupplierOrderItem).filter(
        models.SupplierOrderItem.order_id == order.order_id,
        models.SupplierOrderItem.item_id_ref == evidence.item_id,
    ).all()


def normalize_supplier_receipt_evidence(
    db: Session,
    *,
    explicit_sles: Iterable[models.StockLedgerEntry],
    evidence: Iterable[SupplierDocumentEvidence],
) -> tuple[NormalizedSupplierReceiptFact, ...]:
    """Match typed supplier evidence to an explicit SLE set, read-only.

    This is the one evidence -> physical-row seam shared by the historical
    generation rebuild and bounded physical refresh.  ``explicit_sles`` is
    never expanded: no visible-generation query or lineage traversal occurs.
    One document line may legitimately represent several SLE rows when their
    aggregate signed quantity equals the normalized document quantity.
    """

    sle_rows = tuple(explicit_sles)
    by_identity: dict[tuple[str, str, str], list[models.StockLedgerEntry]] = {}
    for sle in sle_rows:
        by_identity.setdefault(
            (_text(sle.recorder_type), _text(sle.recorder_ref), _text(sle.line_no)),
            [],
        ).append(sle)

    evidence_by_identity: dict[tuple[str, str, str], SupplierDocumentEvidence] = {}
    for row in evidence:
        operation = _operation_prefix(row)
        if operation == TRANSFER_OPERATION:
            continue
        identity = (
            _text(row.receipt_doc_type),
            _text(row.receipt_doc_ref),
            _text(row.receipt_doc_line_no),
        )
        previous = evidence_by_identity.get(identity)
        if previous is not None and previous != row:
            raise SupplierReceiptEvidenceError(
                f"conflicting evidence for {row.receipt_doc_ref}/{row.receipt_doc_line_no}"
            )
        evidence_by_identity[identity] = row

    normalized: list[NormalizedSupplierReceiptFact] = []
    for identity, row in sorted(evidence_by_identity.items()):
        operation = _operation_prefix(row)
        matched_sles = tuple(
            sorted(by_identity.get(identity, ()), key=lambda sle: int(sle.id))
        )
        if not matched_sles:
            raise SupplierReceiptEvidenceError(
                f"evidence {row.receipt_doc_ref}/{row.receipt_doc_line_no} "
                "has no explicit physical Ledger row"
            )
        if any(
            int(sle.item_id) != int(row.item_id)
            or (
                _text(row.warehouse_ref1c)
                and _text(sle.warehouse_ref1c) != _text(row.warehouse_ref1c)
            )
            for sle in matched_sles
        ) or sum((_decimal(sle.qty) for sle in matched_sles), Decimal("0")) != _decimal(row.signed_qty):
            raise SupplierReceiptEvidenceError(
                "evidence assertions contradict explicit Ledger rows for "
                f"{row.receipt_doc_ref}/{row.receipt_doc_line_no}"
            )

        candidates = _candidate_order_lines(db, row)
        exact_candidate = (
            candidates[0]
            if len(candidates) == 1
            and candidates[0].line_number is not None
            and int(candidates[0].line_number) > 0
            else None
        )
        status = (
            "exact" if exact_candidate is not None
            else "ambiguous" if len(candidates) > 1
            else "unmatched"
        )
        canonical_line_no = (
            str(exact_candidate.line_number)
            if exact_candidate is not None else None
        )
        reason = None if status == "exact" else (
            "multiple exact supplier order lines"
            if status == "ambiguous"
            else "no exact typed supplier order line"
        )
        operation_rule = {
            RECEIPT_OPERATION: "supplier-receipt-exact-line",
            CORRECTION_OPERATION: "supplier-receipt-correction",
            SUPPLIER_RETURN_OPERATION: "supplier-return-exact-line",
        }[operation]
        # An ambiguous or unmatched raw order reference must never reach the
        # allocator as if it were an exact line.  The immutable evidence and
        # status remain available to provenance writers for diagnostics.
        supplier_order_ref = _text(row.supplier_order_ref) if status == "exact" else ""
        supplier_order_line_no = canonical_line_no if status == "exact" else "0"
        for sle in matched_sles:
            normalized.append(NormalizedSupplierReceiptFact(
                fact=ReceiptFact(
                    sle_id=int(sle.id),
                    posting_at=sle.posting_at,
                    ingest_batch_id=getattr(sle, "ingest_batch_id", None),
                    signed_qty=_decimal(sle.qty),
                    item_id=int(row.item_id),
                    supplier_order_ref=supplier_order_ref,
                    supplier_order_line_no=supplier_order_line_no,
                    receipt_ref=_text(row.receipt_doc_ref),
                    receipt_line_no=_text(row.receipt_doc_line_no),
                    correction_receipt_ref=_text(row.correction_receipt_ref) or None,
                    supplier_order_type=(
                        _normalized_type(row.supplier_order_type)
                        if status == "exact" else ""
                    ),
                ),
                evidence=row,
                operation=operation,
                match_status=status,
                canonical_line_no=canonical_line_no,
                match_rule=operation_rule,
                ambiguity_count=len(candidates) if status == "ambiguous" else 0,
                reason=reason,
            ))
    return tuple(normalized)


def _rebuild_supplier_receipt_coverage_unsafe(
    db: Session,
    *,
    ledger_generation_id: int,
    evidence: Iterable[SupplierDocumentEvidence],
    cycle_id: str,
    writer_mode: str = "historical",
) -> SupplierReceiptBuildResult:
    """Persist provenance and rebuild supplier coverage rows idempotently."""
    rows = tuple(evidence)
    _validate_operations(rows)
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise SupplierReceiptEvidenceError(
            f"Ledger generation {ledger_generation_id} does not exist"
        )
    if str(generation.status) != "building":
        raise SupplierReceiptEvidenceError(
            f"Ledger generation {ledger_generation_id} is immutable "
            f"in status {generation.status}"
        )
    if (
        generation.physical_import_batch is None
        or str(generation.physical_import_batch.status) != "completed"
    ):
        raise SupplierReceiptEvidenceError(
            "supplier receipt rebuild requires a completed physical boundary"
        )
    visible = visible_sles_for_generation(db, ledger_generation_id)
    normalized_rows = normalize_supplier_receipt_evidence(
        db,
        explicit_sles=visible,
        evidence=rows,
    )

    provenance_by_entry = {
        int(row.stock_ledger_entry_id): row
        for row in db.query(models.StockLedgerSupplierReceiptProvenance).filter_by(
            ledger_generation_id=ledger_generation_id
        ).all()
    }
    touched_provenance_entry_ids: set[int] = set()

    facts: list[ReceiptFact] = []
    provenance_count = 0
    exact_fact_count = len({
        (
            _text(normalized.evidence.receipt_doc_type),
            _text(normalized.evidence.receipt_doc_ref),
            _text(normalized.evidence.receipt_doc_line_no),
        )
        for normalized in normalized_rows
        if normalized.match_status == "exact"
    })
    for normalized in normalized_rows:
        row = normalized.evidence
        operation = normalized.operation
        matched_sle_id = int(normalized.fact.sle_id)
        built = build_supplier_receipt_provenance(
            ledger_generation_id=ledger_generation_id,
            stock_ledger_entry_id=matched_sle_id,
            receipt_doc_type=row.receipt_doc_type,
            receipt_doc_ref=row.receipt_doc_ref,
            receipt_doc_line_no=row.receipt_doc_line_no,
            operation_kind=_OPERATION_KINDS[operation],
            operation_key=row.operation_key,
            operation_name=row.operation_name,
            item_id=row.item_id,
            signed_qty=row.signed_qty,
            match_rule=normalized.match_rule,
            match_status=normalized.match_status,
            supplier_order_type=row.supplier_order_type,
            supplier_order_ref=row.supplier_order_ref,
            supplier_order_line_no=row.supplier_order_line_no,
            characteristic_ref=row.characteristic_ref,
            warehouse_ref1c=row.warehouse_ref1c,
            correction_receipt_ref=row.correction_receipt_ref,
            ambiguity_count=normalized.ambiguity_count,
            reason=normalized.reason,
        )
        evidence_payload = dict(built.evidence_payload)
        evidence_hash = built.evidence_hash
        touched_provenance_entry_ids.add(matched_sle_id)
        provenance = provenance_by_entry.get(matched_sle_id)
        if provenance is None:
            built.supplier_order_ref = normalized.fact.supplier_order_ref
            built.supplier_order_line_no = normalized.fact.supplier_order_line_no
            provenance = built
            db.add(provenance)
            provenance_by_entry[matched_sle_id] = provenance
        else:
            provenance.ledger_generation_id = ledger_generation_id
            provenance.stock_ledger_entry_id = matched_sle_id
            provenance.receipt_doc_type = _text(row.receipt_doc_type)
            provenance.receipt_doc_ref = _text(row.receipt_doc_ref)
            provenance.receipt_doc_line_no = _text(row.receipt_doc_line_no)
            provenance.supplier_order_ref = normalized.fact.supplier_order_ref
            provenance.supplier_order_line_no = normalized.fact.supplier_order_line_no
            provenance.operation_kind = _OPERATION_KINDS[operation]
            provenance.operation_key = evidence_payload["operation_key"]
            provenance.operation_name = evidence_payload["operation_name"]
            provenance.correction_receipt_ref = evidence_payload["correction_receipt_ref"]
            provenance.evidence_hash = evidence_hash
            provenance.evidence_payload = evidence_payload
            provenance.match_rule = normalized.match_rule
            provenance.match_status = normalized.match_status
            provenance.ambiguity_count = normalized.ambiguity_count
            provenance.reason = normalized.reason
        provenance_count += 1
        facts.append(normalized.fact)

    for stale_entry_id, stale_provenance in provenance_by_entry.items():
        if stale_entry_id in touched_provenance_entry_ids:
            continue
        # The sweep may only remove what this rebuild is authoritative for.
        # ``excluded_non_supplier`` rows are outside its input by construction
        # - ``rebuild_supplier_receipt_coverage_from_persisted_provenance``
        # filters them out before normalizing, and a non-supplier expense has
        # no supplier evidence to rebuild from - so they can never be
        # "touched" and were deleted as stale on every obligation refresh.
        # On the stand that silently dropped 341 of 2157 rows per refresh; the
        # candidate then no longer owned the record that its writer had looked
        # at those facts and ruled them out.  They belong to
        # ``generation_lifecycle._persist_non_supplier_receipt_rows``, which is
        # the only writer of that status.  A fact that is genuinely
        # reclassified as a supplier receipt arrives through the touch branch
        # above and is updated in place, not deleted here.
        if _text(stale_provenance.match_status) == "excluded_non_supplier":
            continue
        db.delete(stale_provenance)

    # Make the normalized, generation-scoped evidence durable inside the
    # current savepoint before FIFO is evaluated.  A later allocation failure
    # still rolls the savepoint back atomically, while the allocator can never
    # run ahead of its auditable provenance.
    db.flush()
    exact_caps = _exact_allocation_caps_by_order_line(
        db,
        ledger_generation_id=ledger_generation_id,
    )
    reservations_by_item = {
        item_id: _buy_reservations_for_item(
            db,
            ledger_generation_id=ledger_generation_id,
            item_id=item_id,
        )
        for item_id in {int(fact.item_id) for fact in facts}
    }
    # §49 holds on this path too: the same per-owner freeze cutoff the R4
    # replay and the consumption allocator read.
    from .current_replenishment import freeze_baselines_by_reservation

    allocations, surplus = allocate_supplier_receipts(
        facts,
        reservations_by_item,
        exact_allocation_caps=exact_caps,
        freeze_batch_by_reservation={
            reservation_id: boundary.batch_id
            for reservation_id, boundary in freeze_baselines_by_reservation(
                db,
                [entry for entries in reservations_by_item.values() for entry in entries],
            ).items()
        },
    )
    realized_keys: set[tuple[int, int]] = set()
    folded_reservations: set[int] = set()
    for allocation in allocations:
        if allocation.reservation.requirement_id is None:
            continue
        if allocation.qty != 0:
            realized_keys.add((
                int(allocation.fact.sle_id),
                int(allocation.reservation.requirement_id),
            ))
        reservation = allocation.reservation
        if reservation.id is not None:
            if writer_mode == "historical":
                if _append_reservation_event(
                    db,
                    allocation=allocation,
                    cycle_id=cycle_id,
                ):
                    folded_reservations.add(int(reservation.id))
                else:
                    event_key = (
                        f"{'realize' if allocation.qty > 0 else 'unrealize'}:"
                        f"{int(reservation.id)}:{int(allocation.fact.sle_id)}"
                    )
                    if _event_exists(db, int(reservation.ledger_generation_id), event_key):
                        folded_reservations.add(int(reservation.id))
            elif writer_mode != "current":
                raise ValueError(
                    "supplier receipt writer_mode must be historical or current"
                )
    for reservation_id in sorted(folded_reservations):
        fold_reservation_entry(db, reservation_id)
    db.flush()
    return SupplierReceiptBuildResult(
        provenance_count=provenance_count,
        exact_fact_count=exact_fact_count,
        allocation_count=len(realized_keys),
        surplus_qty=surplus,
    )


#: Every supplier-receipt provenance row carries the same evidence payload
#: keys, whoever wrote it.  Two writers with two shapes is how 26 rows ended
#: up unreadable by the rebuild that an obligation refresh runs.
SUPPLIER_PROVENANCE_PAYLOAD_KEYS = (
    "receipt_doc_type",
    "receipt_doc_ref",
    "receipt_doc_line_no",
    "operation_key",
    "operation_name",
    "supplier_order_type",
    "supplier_order_ref",
    "supplier_order_line_no",
    "item_id",
    "characteristic_ref",
    "warehouse_ref1c",
    "signed_qty",
    "correction_receipt_ref",
)


def build_supplier_receipt_provenance(
    *,
    ledger_generation_id: int,
    stock_ledger_entry_id: int,
    receipt_doc_type: object,
    receipt_doc_ref: object,
    receipt_doc_line_no: object,
    operation_kind: str,
    operation_key: object,
    operation_name: object,
    item_id: int,
    signed_qty: object,
    match_rule: str,
    match_status: str,
    supplier_order_type: object = "",
    supplier_order_ref: object = "",
    supplier_order_line_no: object = "",
    characteristic_ref: object = "",
    warehouse_ref1c: object = "",
    correction_receipt_ref: object = None,
    ambiguity_count: int = 0,
    reason: str | None = None,
) -> models.StockLedgerSupplierReceiptProvenance:
    """Build one provenance row: the columns and the payload, together.

    The typed fields are the relational store - the model gives each of them
    its own column - and the payload additionally carries the few facts that
    have no column (item, characteristic, warehouse, signed quantity and the
    supplier order type) so a rebuild can reconstruct the evidence without
    re-reading 1C.  Both stores are filled here, once, so a row cannot be
    half-written by whichever writer happened to create it.

    An ``exact`` row must name the supplier order document it matched: the
    rebuild re-derives exactness from that type, and a row without it would
    silently replay as ``unmatched`` and stop allocating to its BUY owner.
    """
    if (
        str(match_status) == "exact"
        and _normalized_type(supplier_order_type) != SUPPLIER_ORDER_TYPE
    ):
        raise SupplierReceiptEvidenceError(
            f"exact supplier receipt provenance for SLE {int(stock_ledger_entry_id)} "
            f"requires supplier order type {SUPPLIER_ORDER_TYPE}, got "
            f"{_text(supplier_order_type)!r}"
        )
    payload = {
        "receipt_doc_type": _text(receipt_doc_type)[:64],
        "receipt_doc_ref": _text(receipt_doc_ref)[:64],
        "receipt_doc_line_no": _text(receipt_doc_line_no)[:32],
        "operation_key": _text(operation_key)[:128],
        "operation_name": _text(operation_name)[:128],
        "supplier_order_type": _normalized_type(supplier_order_type)[:64],
        "supplier_order_ref": _text(supplier_order_ref)[:64],
        "supplier_order_line_no": _text(supplier_order_line_no)[:32],
        "item_id": int(item_id),
        "characteristic_ref": _text(characteristic_ref)[:64],
        "warehouse_ref1c": _text(warehouse_ref1c)[:64],
        "signed_qty": canonical_decimal(signed_qty),
        "correction_receipt_ref": _text(correction_receipt_ref)[:64] or None,
    }
    return models.StockLedgerSupplierReceiptProvenance(
        ledger_generation_id=int(ledger_generation_id),
        stock_ledger_entry_id=int(stock_ledger_entry_id),
        receipt_doc_type=_text(receipt_doc_type),
        receipt_doc_ref=_text(receipt_doc_ref),
        receipt_doc_line_no=_text(receipt_doc_line_no),
        supplier_order_ref=_text(supplier_order_ref) or None,
        supplier_order_line_no=_text(supplier_order_line_no) or None,
        operation_kind=str(operation_kind),
        operation_key=payload["operation_key"],
        operation_name=payload["operation_name"],
        correction_receipt_ref=payload["correction_receipt_ref"],
        evidence_hash=canonical_content_hash(payload),
        evidence_payload=payload,
        match_rule=str(match_rule),
        match_status=str(match_status),
        ambiguity_count=int(ambiguity_count),
        reason=reason,
    )


def rebuild_supplier_receipt_coverage(
    db: Session,
    *,
    ledger_generation_id: int,
    evidence: Iterable[SupplierDocumentEvidence],
    cycle_id: str,
    writer_mode: str = "historical",
) -> SupplierReceiptBuildResult:
    """Atomically replace one generation's supplier-derived read model.

    A savepoint is intentional: callers may catch a validation/persistence
    exception and continue their wider transaction.  In that case the last
    complete generation projection must remain intact.
    """
    rows = tuple(evidence)
    with db.begin_nested():
        return _rebuild_supplier_receipt_coverage_unsafe(
            db,
            ledger_generation_id=ledger_generation_id,
            evidence=rows,
            cycle_id=cycle_id,
            writer_mode=writer_mode,
        )


#: Generations whose pre-contract rows were already reported, so a stand
#: that has not been repaired yet logs once per generation, not per refresh.
_PRE_CONTRACT_LOGGED: set[int] = set()


def _payload_of(row: models.StockLedgerSupplierReceiptProvenance) -> dict:
    return dict(row.evidence_payload or {})


def _has_documented_identity(
    row: models.StockLedgerSupplierReceiptProvenance,
) -> bool:
    doc_type = _text(row.receipt_doc_type)
    ref = _text(row.receipt_doc_ref)
    return (
        bool(doc_type)
        and doc_type not in WRITER_MARKER_DOCUMENT_TYPES
        and bool(ref)
        and not ref.startswith("sle:")
        and bool(_text(row.receipt_doc_line_no))
    )


_PHYSICAL_PAYLOAD_KEYS = ("item_id", "signed_qty", "characteristic_ref", "warehouse_ref1c")


def _needs_physical_row(row: models.StockLedgerSupplierReceiptProvenance) -> bool:
    payload = _payload_of(row)
    return not _has_documented_identity(row) or any(
        payload.get(key) is None for key in _PHYSICAL_PAYLOAD_KEYS
    )


def _sles_by_id(
    db: Session, sle_ids: Iterable[int]
) -> dict[int, models.StockLedgerEntry]:
    ids = sorted({int(value) for value in sle_ids})
    result: dict[int, models.StockLedgerEntry] = {}
    for offset in range(0, len(ids), 1000):
        chunk = ids[offset:offset + 1000]
        for sle in db.query(models.StockLedgerEntry).filter(
            models.StockLedgerEntry.id.in_(chunk)
        ):
            result[int(sle.id)] = sle
    return result


def provenance_is_pre_contract(
    row: models.StockLedgerSupplierReceiptProvenance,
) -> bool:
    """Whether a row predates the one-row contract and needs normalising.

    Such a row names its writer instead of the 1C document or operation, lacks
    payload keys the contract declares, or is ``exact`` without the supplier
    order type that makes it exact on replay.  ``excluded_non_supplier`` rows
    are never replayed and are not judged here.
    """
    if _text(row.match_status) == "excluded_non_supplier":
        return False
    payload = _payload_of(row)
    return (
        not _has_documented_identity(row)
        or not resolves_to_documented_operation(
            _text(row.operation_key), _text(row.operation_name)
        )
        or any(key not in payload for key in SUPPLIER_PROVENANCE_PAYLOAD_KEYS)
        or any(payload.get(key) is None for key in _PHYSICAL_PAYLOAD_KEYS)
        or (
            _text(row.match_status) == "exact"
            and _normalized_type(payload.get("supplier_order_type"))
            != SUPPLIER_ORDER_TYPE
        )
    )


def resolve_persisted_supplier_evidence(
    row: models.StockLedgerSupplierReceiptProvenance,
    sle: models.StockLedgerEntry | None,
) -> tuple[SupplierDocumentEvidence, bool]:
    """Reconstruct one row's document evidence; the one formula for it.

    Used by the rebuild an obligation refresh runs and by the repair phase
    that normalises rows in place, so both read a pre-contract row the same
    way.  Returns the evidence and whether the row's own SLE had to supply
    part of it.

    Typed fields come from the columns - the relational store every writer
    fills - and the payload supplies what has no column.  A row written before
    the one-row contract is resolved deterministically from the physical fact
    it already references (``stock_ledger_entry_id``): the document identity
    (``recorder_type``/``recorder_ref``/``line_no``) and, where the payload
    lacks them, item, quantity, characteristic and warehouse.  The supplier
    order type of an ``exact`` row is the supplier order document: the
    normalizer only ever produced ``exact`` by matching one of its lines.
    Nothing is invented - a row whose SLE is gone fails closed.
    """
    payload = _payload_of(row)
    sle_id = int(row.stock_ledger_entry_id)
    from_sle = False

    def _physical() -> models.StockLedgerEntry:
        nonlocal from_sle
        if sle is None:
            raise SupplierReceiptEvidenceError(
                "persisted supplier receipt provenance is incomplete for "
                f"SLE {sle_id}: its physical Ledger row is missing"
            )
        from_sle = True
        return sle

    if _has_documented_identity(row):
        receipt_doc_type = _text(row.receipt_doc_type)
        receipt_doc_ref = _text(row.receipt_doc_ref)
        receipt_doc_line_no = _text(row.receipt_doc_line_no)
    else:
        physical = _physical()
        receipt_doc_type = _text(physical.recorder_type)
        stored_ref = _text(row.receipt_doc_ref)
        synthetic = not stored_ref or stored_ref.startswith("sle:")
        receipt_doc_ref = _text(physical.recorder_ref) if synthetic else stored_ref
        receipt_doc_line_no = (
            _text(physical.line_no)
            if synthetic or not _text(row.receipt_doc_line_no)
            else _text(row.receipt_doc_line_no)
        )

    # ``operation_kind`` is a column and is the typed classification the
    # writer already made, so it - not a marker string - decides which
    # documented operation a row replays as when the stored pair names a
    # writer ("26 rows with 'bounded_physical_refresh'").
    stored_key = _text(row.operation_key)
    stored_name = _text(row.operation_name)
    if resolves_to_documented_operation(stored_key, stored_name):
        operation_key, operation_name = stored_key, stored_name
    else:
        operation_key, operation_name = canonical_operation_for_kind(
            _text(row.operation_kind)
        )

    def _field(key: str, physical_attr: str) -> object:
        if payload.get(key) is not None:
            return payload[key]
        return getattr(_physical(), physical_attr)

    supplier_order_ref = (
        _text(row.supplier_order_ref) or _text(payload.get("supplier_order_ref"))
    )
    supplier_order_type = _normalized_type(payload.get("supplier_order_type"))
    if (
        not supplier_order_type
        and _text(row.match_status) == "exact"
        and supplier_order_ref
    ):
        supplier_order_type = SUPPLIER_ORDER_TYPE
    try:
        evidence = SupplierDocumentEvidence(
            receipt_doc_type=receipt_doc_type,
            receipt_doc_ref=receipt_doc_ref,
            receipt_doc_line_no=receipt_doc_line_no,
            operation_key=operation_key,
            operation_name=operation_name,
            supplier_order_type=supplier_order_type,
            supplier_order_ref=supplier_order_ref,
            supplier_order_line_no=_text(row.supplier_order_line_no)
            or _text(payload.get("supplier_order_line_no")),
            item_id=int(_field("item_id", "item_id")),
            characteristic_ref=_text(_field("characteristic_ref", "characteristic_ref")),
            warehouse_ref1c=_text(_field("warehouse_ref1c", "warehouse_ref1c")),
            signed_qty=_decimal(_field("signed_qty", "qty")),
            correction_receipt_ref=(
                _text(row.correction_receipt_ref)
                or _text(payload.get("correction_receipt_ref"))
                or None
            ),
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise SupplierReceiptEvidenceError(
            "persisted supplier receipt provenance is incomplete for "
            f"SLE {sle_id}"
        ) from exc
    return evidence, from_sle


def rebuild_supplier_receipt_coverage_from_persisted_provenance(
    db: Session,
    *,
    ledger_generation_id: int,
    cycle_id: str,
    writer_mode: str = "historical",
) -> SupplierReceiptBuildResult:
    """Replay BUY realization from the generation's immutable receipt evidence.

    Obligation refresh reuses the accepted physical prefix and clones its
    generation-scoped provenance.  Re-reading mutable 1C documents would break
    that boundary; reconstruct the normalized evidence from the persisted
    payload instead.
    """
    rows = (
        db.query(models.StockLedgerSupplierReceiptProvenance)
        .filter(
            models.StockLedgerSupplierReceiptProvenance.ledger_generation_id
            == int(ledger_generation_id),
            models.StockLedgerSupplierReceiptProvenance.match_status
            != "excluded_non_supplier",
        )
        .order_by(
            models.StockLedgerSupplierReceiptProvenance.receipt_doc_ref.asc(),
            models.StockLedgerSupplierReceiptProvenance.receipt_doc_line_no.asc(),
            models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id.asc(),
        )
        .all()
    )
    needs_sle = [
        int(row.stock_ledger_entry_id) for row in rows if _needs_physical_row(row)
    ]
    sles = _sles_by_id(db, needs_sle)
    evidence: list[SupplierDocumentEvidence] = []
    resolved_from_sle = 0
    for row in rows:
        resolved, from_sle = resolve_persisted_supplier_evidence(
            row, sles.get(int(row.stock_ledger_entry_id))
        )
        evidence.append(resolved)
        resolved_from_sle += int(from_sle)
    if resolved_from_sle and int(ledger_generation_id) not in _PRE_CONTRACT_LOGGED:
        _PRE_CONTRACT_LOGGED.add(int(ledger_generation_id))
        logger.warning(
            "supplier receipt rebuild of generation %s resolved %s pre-contract "
            "provenance rows from their own SLE; run "
            "`current_execution_migration --phase supplier-provenance-repair` "
            "to normalise them in place",
            int(ledger_generation_id),
            resolved_from_sle,
        )
    return rebuild_supplier_receipt_coverage(
        db,
        ledger_generation_id=int(ledger_generation_id),
        evidence=evidence,
        cycle_id=cycle_id,
        writer_mode=writer_mode,
    )

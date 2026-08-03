"""Supplier receipt provenance and coverage reshaping for one Ledger generation.

The caller supplies normalized 1C document evidence.  This module deliberately
does not read OData or legacy ``received_qty`` projections.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from .reservation import (
    append_realization_event,
    fold_reservation_entry,
    replenishment_remaining,
)

from .physical import canonical_content_hash, canonical_decimal
from .physical_visibility import visible_sles_for_generation


RECEIPT_OPERATION = "8d97069c"
CORRECTION_OPERATION = "8d96f940"
SUPPLIER_RETURN_OPERATION = "8d96f436"
TRANSFER_OPERATION = "8d970232"
SUPPLIER_ORDER_TYPE = "Document_ЗаказПоставщику"

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


@dataclass(frozen=True)
class CoverageAllocation:
    fact: ReceiptFact
    reservation: models.ReservationEntry
    qty: Decimal


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
) -> dict[tuple[int, str, str], dict[int, Decimal]]:
    rows = (
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
) -> tuple[tuple[CoverageAllocation, ...], Decimal]:
    """Pure deterministic allocator.

    Positive supplier receipts fill exact supplier-order-line matches first inside
    their export allocation cap, then FIFO by item. Returns unwind the same
    supplier-order-line first (newest-first), then global FIFO, then named
    original documents for explicit corrections.
    """
    ordered = sorted(facts, key=lambda row: (row.posting_at, row.sle_id))
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
            item_reservations = reservations.get(item_id, ())

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
                allocation = CoverageAllocation(fact=fact, reservation=reservation, qty=take)
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
            active_qty[id(original)] = available - take
            left -= take
            if left == 0:
                break
        for reservation_key, take in unwind_by_reservation.items():
            if take <= 0:
                continue
            result.append(CoverageAllocation(
                fact=fact,
                reservation=unwind_samples[reservation_key],
                qty=-take,
            ))

    return tuple(result), surplus


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


def _rebuild_supplier_receipt_coverage_unsafe(
    db: Session,
    *,
    ledger_generation_id: int,
    evidence: Iterable[SupplierDocumentEvidence],
    cycle_id: str,
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
    by_identity: dict[tuple[str, str, str], list[models.StockLedgerEntry]] = {}
    for sle in visible:
        by_identity.setdefault(
            (_text(sle.recorder_type), _text(sle.recorder_ref), _text(sle.line_no)),
            [],
        ).append(sle)

    provenance_by_entry = {
        int(row.stock_ledger_entry_id): row
        for row in db.query(models.StockLedgerSupplierReceiptProvenance).filter_by(
            ledger_generation_id=ledger_generation_id
        ).all()
    }
    touched_provenance_entry_ids: set[int] = set()

    facts: list[ReceiptFact] = []
    exact_fact_count = 0
    provenance_count = 0
    for row in rows:
        operation = _operation_prefix(row)
        if operation == TRANSFER_OPERATION:
            continue
        matched_sles = by_identity.get((
            _text(row.receipt_doc_type),
            _text(row.receipt_doc_ref),
            _text(row.receipt_doc_line_no),
        ), [])
        if not matched_sles:
            raise SupplierReceiptEvidenceError(
                f"evidence {row.receipt_doc_ref}/{row.receipt_doc_line_no} "
                "has no visible physical Ledger row"
            )
        if any(
            sle.item_id != row.item_id
            or _text(sle.warehouse_ref1c) != _text(row.warehouse_ref1c)
            for sle in matched_sles
        ) or sum((_decimal(sle.qty) for sle in matched_sles), Decimal("0")) != _decimal(row.signed_qty):
            raise SupplierReceiptEvidenceError(
                f"evidence assertions contradict Ledger for "
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
        status = "exact" if exact_candidate is not None else (
            "ambiguous" if len(candidates) > 1 else "unmatched"
        )
        if status == "exact":
            exact_fact_count += 1
        reason = None if status == "exact" else (
            "multiple exact supplier order lines" if status == "ambiguous"
            else "no exact typed supplier order line"
        )
        operation_rule = {
            RECEIPT_OPERATION: "supplier-receipt-exact-line",
            CORRECTION_OPERATION: "supplier-receipt-correction",
            SUPPLIER_RETURN_OPERATION: "supplier-return-exact-line",
        }[operation]
        evidence_payload = {
            "receipt_doc_type": _text(row.receipt_doc_type)[:64],
            "receipt_doc_ref": _text(row.receipt_doc_ref)[:64],
            "receipt_doc_line_no": _text(row.receipt_doc_line_no)[:32],
            "operation_key": _text(row.operation_key)[:128],
            "operation_name": _text(row.operation_name)[:128],
            "supplier_order_type": _normalized_type(row.supplier_order_type)[:64],
            "supplier_order_ref": _text(row.supplier_order_ref)[:64],
            "supplier_order_line_no": _text(row.supplier_order_line_no)[:32],
            "item_id": int(row.item_id),
            "characteristic_ref": _text(row.characteristic_ref)[:64],
            "warehouse_ref1c": _text(row.warehouse_ref1c)[:64],
            "signed_qty": canonical_decimal(row.signed_qty),
            "correction_receipt_ref": (
                _text(row.correction_receipt_ref)[:64] or None
            ),
        }
        evidence_hash = canonical_content_hash(evidence_payload)
        canonical_line_no = (
            str(exact_candidate.line_number)
            if exact_candidate is not None else None
        )
        for sle in matched_sles:
            matched_sle_id = int(sle.id)
            touched_provenance_entry_ids.add(matched_sle_id)
            provenance = provenance_by_entry.get(matched_sle_id)
            supplier_order_line_no = (
                canonical_line_no if status == "exact" else _text(row.supplier_order_line_no)
            )
            if provenance is None:
                provenance = models.StockLedgerSupplierReceiptProvenance(
                    ledger_generation_id=ledger_generation_id,
                    stock_ledger_entry_id=matched_sle_id,
                    receipt_doc_type=_text(row.receipt_doc_type),
                    receipt_doc_ref=_text(row.receipt_doc_ref),
                    receipt_doc_line_no=_text(row.receipt_doc_line_no),
                    supplier_order_ref=_text(row.supplier_order_ref),
                    supplier_order_line_no=supplier_order_line_no,
                    operation_kind=_OPERATION_KINDS[operation],
                    operation_key=evidence_payload["operation_key"],
                    operation_name=evidence_payload["operation_name"],
                    correction_receipt_ref=evidence_payload["correction_receipt_ref"],
                    evidence_hash=evidence_hash,
                    evidence_payload=evidence_payload,
                    match_rule=operation_rule,
                    match_status=status,
                    ambiguity_count=len(candidates) if status == "ambiguous" else 0,
                    reason=reason,
                )
                db.add(provenance)
                provenance_by_entry[matched_sle_id] = provenance
            else:
                provenance.ledger_generation_id = ledger_generation_id
                provenance.stock_ledger_entry_id = matched_sle_id
                provenance.receipt_doc_type = _text(row.receipt_doc_type)
                provenance.receipt_doc_ref = _text(row.receipt_doc_ref)
                provenance.receipt_doc_line_no = _text(row.receipt_doc_line_no)
                provenance.supplier_order_ref = _text(row.supplier_order_ref)
                provenance.supplier_order_line_no = supplier_order_line_no
                provenance.operation_kind = _OPERATION_KINDS[operation]
                provenance.operation_key = evidence_payload["operation_key"]
                provenance.operation_name = evidence_payload["operation_name"]
                provenance.correction_receipt_ref = evidence_payload["correction_receipt_ref"]
                provenance.evidence_hash = evidence_hash
                provenance.evidence_payload = evidence_payload
                provenance.match_rule = operation_rule
                provenance.match_status = status
                provenance.ambiguity_count = len(candidates) if status == "ambiguous" else 0
                provenance.reason = reason
            provenance_count += 1
            facts.append(ReceiptFact(
                sle_id=sle.id,
                posting_at=sle.posting_at,
                signed_qty=_decimal(sle.qty),
                item_id=int(row.item_id),
                supplier_order_ref=_text(row.supplier_order_ref),
                supplier_order_line_no=(
                    str(canonical_line_no) if status == "exact" else _text(row.supplier_order_line_no)
                ),
                receipt_ref=_text(row.receipt_doc_ref),
                receipt_line_no=_text(row.receipt_doc_line_no),
                correction_receipt_ref=_text(row.correction_receipt_ref) or None,
            ))

    for stale_entry_id, stale_provenance in provenance_by_entry.items():
        if stale_entry_id not in touched_provenance_entry_ids:
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
    allocations, surplus = allocate_supplier_receipts(
        facts,
        reservations_by_item,
        exact_allocation_caps=exact_caps,
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
    for reservation_id in sorted(folded_reservations):
        fold_reservation_entry(db, reservation_id)
    db.flush()
    return SupplierReceiptBuildResult(
        provenance_count=provenance_count,
        exact_fact_count=exact_fact_count,
        allocation_count=len(realized_keys),
        surplus_qty=surplus,
    )


def rebuild_supplier_receipt_coverage(
    db: Session,
    *,
    ledger_generation_id: int,
    evidence: Iterable[SupplierDocumentEvidence],
    cycle_id: str,
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
        )


def rebuild_supplier_receipt_coverage_from_persisted_provenance(
    db: Session,
    *,
    ledger_generation_id: int,
    cycle_id: str,
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
    evidence: list[SupplierDocumentEvidence] = []
    for row in rows:
        payload = dict(row.evidence_payload or {})
        try:
            evidence.append(SupplierDocumentEvidence(
                receipt_doc_type=str(payload["receipt_doc_type"]),
                receipt_doc_ref=str(payload["receipt_doc_ref"]),
                receipt_doc_line_no=str(payload["receipt_doc_line_no"]),
                operation_key=str(payload["operation_key"]),
                operation_name=str(payload["operation_name"]),
                supplier_order_type=str(payload.get("supplier_order_type") or ""),
                supplier_order_ref=str(payload.get("supplier_order_ref") or ""),
                supplier_order_line_no=str(payload.get("supplier_order_line_no") or ""),
                item_id=int(payload["item_id"]),
                characteristic_ref=str(payload.get("characteristic_ref") or ""),
                warehouse_ref1c=str(payload.get("warehouse_ref1c") or ""),
                signed_qty=_decimal(payload["signed_qty"]),
                correction_receipt_ref=(
                    str(payload["correction_receipt_ref"])
                    if payload.get("correction_receipt_ref")
                    else None
                ),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise SupplierReceiptEvidenceError(
                "persisted supplier receipt provenance is incomplete for "
                f"SLE {int(row.stock_ledger_entry_id)}"
            ) from exc
    return rebuild_supplier_receipt_coverage(
        db,
        ledger_generation_id=int(ledger_generation_id),
        evidence=evidence,
        cycle_id=cycle_id,
    )

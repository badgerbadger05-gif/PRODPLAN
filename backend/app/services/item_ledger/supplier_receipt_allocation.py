"""Supplier receipt provenance and coverage reshaping for one Ledger generation.

The caller supplies normalized 1C document evidence.  This module deliberately
does not read OData or legacy ``received_qty`` projections.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from app import models

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
    supplier_order_ref: str
    supplier_order_line_no: str
    receipt_ref: str
    receipt_line_no: str
    correction_receipt_ref: str | None = None


@dataclass(frozen=True)
class CoveragePin:
    freeze_allocation_id: int
    requirement_id: int
    bucket_id: int | None
    qty: Decimal
    due_at: datetime


@dataclass(frozen=True)
class CoverageAllocation:
    fact: ReceiptFact
    pin: CoveragePin
    qty: Decimal


@dataclass(frozen=True)
class SupplierReceiptBuildResult:
    provenance_count: int
    exact_fact_count: int
    allocation_count: int
    unplanned_qty: Decimal


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
    pins_by_order: dict[tuple[str, str], Iterable[CoveragePin]],
) -> tuple[tuple[CoverageAllocation, ...], Decimal]:
    """Pure deterministic allocator.

    Positive receipt facts fill frozen pins FIFO.  Corrections unwind only the
    named original receipt; supplier returns unwind the same order lineage.
    Negative allocations are audit rows and never mutate reservation
    realization.
    """
    ordered = sorted(facts, key=lambda row: (row.posting_at, row.sle_id))
    pins = {
        key: tuple(sorted(value, key=lambda pin: (
            pin.due_at, pin.requirement_id, pin.bucket_id or -1,
            pin.freeze_allocation_id,
        )))
        for key, value in pins_by_order.items()
    }
    remaining = {
        pin.freeze_allocation_id: pin.qty
        for values in pins.values() for pin in values
    }
    positive_by_receipt: dict[str, list[CoverageAllocation]] = {}
    active_by_order: dict[tuple[str, str], list[CoverageAllocation]] = {}
    active_qty: dict[int, Decimal] = {}
    result: list[CoverageAllocation] = []
    unplanned = Decimal("0")

    for fact in ordered:
        key = (fact.supplier_order_ref, fact.supplier_order_line_no)
        qty = fact.signed_qty
        if qty > 0:
            left = qty
            for pin in pins.get(key, ()):
                take = min(left, remaining[pin.freeze_allocation_id])
                if take <= 0:
                    continue
                allocation = CoverageAllocation(fact=fact, pin=pin, qty=take)
                result.append(allocation)
                positive_by_receipt.setdefault(fact.receipt_ref, []).append(allocation)
                active_by_order.setdefault(key, []).append(allocation)
                active_qty[id(allocation)] = take
                remaining[pin.freeze_allocation_id] -= take
                left -= take
                if left == 0:
                    break
            unplanned += left
            continue

        left = -qty
        source = (
            positive_by_receipt.get(_text(fact.correction_receipt_ref), [])
            if fact.correction_receipt_ref
            else active_by_order.get(key, [])
        )
        for original in reversed(source):
            available = active_qty[id(original)]
            take = min(left, available)
            if take <= 0:
                continue
            result.append(CoverageAllocation(fact=fact, pin=original.pin, qty=-take))
            active_qty[id(original)] -= take
            remaining[original.pin.freeze_allocation_id] += take
            left -= take
            if left == 0:
                break
        unplanned -= left
    return tuple(result), unplanned


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
        models.SupplierOrderItem.characteristic_ref1c
        == (_text(evidence.characteristic_ref) or None),
    ).all()


def _pins_for_order(
    db: Session,
    generation_id: int,
    order_ref: str,
    line_no: str,
) -> tuple[CoveragePin, ...]:
    exports = db.query(models.PurchaseExportLineAllocation).filter_by(
        ledger_generation_id=generation_id,
        supplier_order_ref=order_ref,
        supplier_order_line_no=line_no,
    ).all()
    allowed_requirements = {
        int(row.planned_purchase.source_mrp_requirement_id)
        for row in exports
        if row.planned_purchase is not None
        and row.planned_purchase.source_mrp_requirement_id is not None
    }
    export_caps: dict[int, Decimal] = {}
    for export in exports:
        purchase = export.planned_purchase
        if purchase is None or purchase.source_mrp_requirement_id is None:
            continue
        requirement_id = int(purchase.source_mrp_requirement_id)
        export_caps[requirement_id] = (
            export_caps.get(requirement_id, Decimal("0"))
            + _decimal(export.allocated_qty)
        )
    query = db.query(models.MrpFreezeAllocation).filter_by(
        source_type="supplier_order",
        source_ref=order_ref,
        source_line_ref=line_no,
    )
    if exports:
        if not allowed_requirements:
            return ()
        query = query.filter(
            models.MrpFreezeAllocation.requirement_id.in_(allowed_requirements)
        )
    rows = sorted(
        query.all(),
        key=lambda row: (
            row.requirement.period_to if row.requirement is not None else datetime.max.date(),
            row.run_id,
            row.requirement_id,
            row.id,
        ),
    )
    result: list[CoveragePin] = []
    for row in rows:
        requirement = row.requirement
        if requirement is None:
            continue
        run = db.get(models.PlanningRun, row.run_id)
        if (
            run is None
            or run.ledger_generation_id is None
            or int(run.ledger_generation_id) != int(generation_id)
            or run.active_freeze_version is None
            or int(row.freeze_version) != int(run.active_freeze_version)
        ):
            continue
        bucket = db.query(models.MrpRequirementBucket).filter_by(
            requirement_id=row.requirement_id
        ).order_by(models.MrpRequirementBucket.bucket_date, models.MrpRequirementBucket.id).first()
        due = datetime.combine(requirement.period_to, datetime.min.time())
        pin_qty = _decimal(row.alloc_qty)
        if exports:
            available_export = export_caps.get(row.requirement_id, Decimal("0"))
            pin_qty = min(pin_qty, available_export)
            export_caps[row.requirement_id] = available_export - pin_qty
        if pin_qty <= 0:
            continue
        result.append(CoveragePin(
            freeze_allocation_id=row.id,
            requirement_id=row.requirement_id,
            bucket_id=bucket.id if bucket else None,
            qty=pin_qty,
            due_at=due,
        ))
    return tuple(result)


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

    db.query(models.MrpExecutionAllocation).filter_by(
        ledger_generation_id=ledger_generation_id,
        fact_type="supplier_receipt",
        allocation_kind="coverage_realization",
    ).delete(synchronize_session=False)
    db.query(models.StockLedgerSupplierReceiptProvenance).filter_by(
        ledger_generation_id=ledger_generation_id
    ).delete(synchronize_session=False)

    facts: list[ReceiptFact] = []
    explicitly_unplanned = Decimal("0")
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
            or _text(sle.characteristic_ref) != _text(row.characteristic_ref)
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
            db.add(models.StockLedgerSupplierReceiptProvenance(
                ledger_generation_id=ledger_generation_id,
                stock_ledger_entry_id=sle.id,
                receipt_doc_type=_text(row.receipt_doc_type),
                receipt_doc_ref=_text(row.receipt_doc_ref),
                receipt_doc_line_no=_text(row.receipt_doc_line_no),
                supplier_order_ref=(
                    _text(row.supplier_order_ref) if status == "exact" else None
                ),
                supplier_order_line_no=(
                    canonical_line_no if status == "exact" else None
                ),
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
            ))
            provenance_count += 1
            if status == "exact":
                facts.append(ReceiptFact(
                    sle_id=sle.id,
                    posting_at=sle.posting_at,
                    signed_qty=_decimal(sle.qty),
                    supplier_order_ref=_text(row.supplier_order_ref),
                    supplier_order_line_no=str(canonical_line_no),
                    receipt_ref=_text(row.receipt_doc_ref),
                    receipt_line_no=_text(row.receipt_doc_line_no),
                    correction_receipt_ref=_text(row.correction_receipt_ref) or None,
                ))
            else:
                explicitly_unplanned += _decimal(sle.qty)

    # Make the normalized, generation-scoped evidence durable inside the
    # current savepoint before FIFO is evaluated.  A later allocation failure
    # still rolls the savepoint back atomically, while the allocator can never
    # run ahead of its auditable provenance.
    db.flush()
    keys = {(fact.supplier_order_ref, fact.supplier_order_line_no) for fact in facts}
    pins_by_order = {
        key: _pins_for_order(db, ledger_generation_id, key[0], key[1])
        for key in keys
    }
    allocations, unplanned = allocate_supplier_receipts(facts, pins_by_order)
    aggregated: dict[
        tuple[int, int, int | None, int], CoverageAllocation
    ] = {}
    aggregate_qty: dict[tuple[int, int, int | None, int], Decimal] = {}
    for allocation in allocations:
        key = (
            allocation.fact.sle_id,
            allocation.pin.requirement_id,
            allocation.pin.bucket_id,
            allocation.pin.freeze_allocation_id,
        )
        aggregated[key] = allocation
        aggregate_qty[key] = aggregate_qty.get(key, Decimal("0")) + allocation.qty
    for key, allocation in aggregated.items():
        qty = aggregate_qty[key]
        if qty == 0:
            continue
        db.add(models.MrpExecutionAllocation(
            ledger_generation_id=ledger_generation_id,
            cycle_id=cycle_id,
            requirement_id=allocation.pin.requirement_id,
            bucket_id=allocation.pin.bucket_id,
            fact_type="supplier_receipt",
            allocation_kind="coverage_realization",
            fact_ref=allocation.fact.receipt_ref,
            # One 1C document line may legitimately emit several physical
            # SLEs.  The read-ledger UNIQUE key predates stock_ledger_entry_id,
            # therefore keep both identities in its bounded line token.
            fact_line_ref=(
                f"{allocation.fact.receipt_line_no}#sle:{allocation.fact.sle_id}"
            ),
            fact_date=allocation.fact.posting_at,
            allocated_qty=qty,
            stock_ledger_entry_id=allocation.fact.sle_id,
            freeze_allocation_id=allocation.pin.freeze_allocation_id,
            origin_requirement_id=allocation.pin.requirement_id,
        ))
    db.flush()
    return SupplierReceiptBuildResult(
        provenance_count=provenance_count,
        exact_fact_count=len(facts),
        allocation_count=sum(1 for qty in aggregate_qty.values() if qty != 0),
        unplanned_qty=unplanned + explicitly_unplanned,
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

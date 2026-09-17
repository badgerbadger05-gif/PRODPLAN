"""Bounded supplier-document evidence for an incremental physical refresh.

This module is deliberately a read-only adapter.  It accepts the exact SLE
ids reported by the import delta, asks the canonical OData extractor to
reconcile those rows with their source documents, and returns typed
``ReceiptFact`` values for the bounded BUY writer.  It never discovers a
generation's visible history and never persists provenance or moves a
pointer; the caller that owns the eventual publication transaction performs
those operations.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from app import models

from .current_replenishment import (
    BoundedBuyReceiptDeltaManifest,
    CurrentReplenishmentError,
    DistributionScope,
    _normalise_bounded_buy_manifest,
    _normalise_bounded_buy_scopes,
)
from .physical_visibility import PhysicalVisibilityError, require_import_batch
from .supplier_receipt_allocation import (
    RECEIPT_OPERATION,
    ReceiptFact,
    SupplierReceiptEvidenceError,
    normalize_supplier_receipt_evidence,
)
from .supplier_receipt_odata import (
    SupplierEvidenceExtractionResult,
    extract_supplier_document_evidence,
)


_SUPPLIER_DOCUMENT_TYPES = frozenset({
    "Document_ПриходнаяНакладная",
    "Document_КорректировкаПоступления",
    "Document_РасходнаяНакладная",
})


def is_supplier_document_type(value: object) -> bool:
    """Return whether a recorder type is a canonical supplier document."""
    return _text(value) in _SUPPLIER_DOCUMENT_TYPES
_FORWARD_OPERATION = RECEIPT_OPERATION
_REJECTED_DELTA_MESSAGE = "complete affected-scope evidence required"


class BoundedSupplierEvidenceError(CurrentReplenishmentError):
    """The explicit physical delta cannot be proven safe for current BUY."""


@dataclass(frozen=True)
class BoundedSupplierEvidenceSummary:
    """Machine-readable evidence for the caller's refresh metrics."""

    manifest: BoundedBuyReceiptDeltaManifest
    input_delta_rows: int
    affected_scopes: tuple[DistributionScope, ...]
    fetched_document_count: int


def _text(value: object) -> str:
    return str(value or "").strip()


def _comparable_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _boundary(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
    target_cutoff: datetime,
) -> tuple[models.LedgerGeneration, models.LedgerGeneration, int, int]:
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    target = db.get(models.LedgerGeneration, int(target_generation_id))
    if parent is None or _text(parent.status) != "accepted":
        raise BoundedSupplierEvidenceError(
            "bounded supplier evidence requires an accepted parent generation"
        )
    if target is None or _text(target.status) != "building":
        raise BoundedSupplierEvidenceError(
            "bounded supplier evidence requires a BUILDING target generation"
        )
    if int(parent.id) == int(target.id):
        raise BoundedSupplierEvidenceError("bounded supplier target must differ from parent")
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or int(pointer.current_generation_id or -1) != int(parent.id):
        raise BoundedSupplierEvidenceError("bounded supplier parent is not current truth")
    if target.cutoff is None or parent.cutoff is None or target_cutoff is None:
        raise BoundedSupplierEvidenceError("bounded supplier cutoffs are required")
    if _comparable_datetime(target.cutoff) != _comparable_datetime(target_cutoff):
        raise BoundedSupplierEvidenceError("bounded supplier target cutoff mismatch")
    if _comparable_datetime(target.cutoff) < _comparable_datetime(parent.cutoff):
        raise BoundedSupplierEvidenceError("bounded supplier target cutoff precedes parent")
    if parent.physical_import_batch_id is None or target.physical_import_batch_id is None:
        raise BoundedSupplierEvidenceError("bounded supplier import boundaries are required")
    lower = int(parent.physical_import_batch_id)
    upper = int(target.physical_import_batch_id)
    if upper < lower:
        raise BoundedSupplierEvidenceError(
            "bounded supplier target import boundary precedes parent"
        )
    try:
        parent_batch = require_import_batch(db, lower)
        target_batch = require_import_batch(db, upper)
    except PhysicalVisibilityError as exc:
        raise BoundedSupplierEvidenceError(str(exc)) from exc
    if (
        _comparable_datetime(parent_batch.cutoff) != _comparable_datetime(parent.cutoff)
        or _comparable_datetime(target_batch.cutoff) != _comparable_datetime(target.cutoff)
    ):
        raise BoundedSupplierEvidenceError(
            "bounded supplier generation cutoff does not match import boundary"
        )
    return parent, target, lower, upper


def _scope_for_row(
    row: models.StockLedgerEntry,
    scopes: tuple[DistributionScope, ...],
) -> DistributionScope:
    candidates = tuple(
        scope
        for scope in scopes
        if int(row.item_id) == int(scope[0])
        and _text(row.characteristic_ref) == scope[1]
        and _text(row.organization_ref) == scope[2]
    )
    if len(candidates) != 1:
        raise BoundedSupplierEvidenceError(
            f"supplier SLE {int(row.id)} has ambiguous or foreign affected BUY scope"
        )
    return candidates[0]


def _load_changed_rows(
    db: Session,
    *,
    changed_sle_ids: Iterable[int | models.StockLedgerEntry],
    lower_batch_id: int,
    upper_batch_id: int,
    target_cutoff: datetime,
    parent_cutoff: datetime,
    scopes: tuple[DistributionScope, ...],
) -> tuple[models.StockLedgerEntry, ...]:
    ids: list[int] = []
    for raw in changed_sle_ids:
        value = getattr(raw, "id", raw)
        try:
            entry_id = int(value)
        except (TypeError, ValueError) as exc:
            raise BoundedSupplierEvidenceError("changed supplier SLE id is malformed") from exc
        if entry_id <= 0 or entry_id in ids:
            raise BoundedSupplierEvidenceError("changed supplier SLE ids must be unique")
        ids.append(entry_id)
    if not ids:
        return ()
    rows = tuple(
        db.query(models.StockLedgerEntry)
        .filter(models.StockLedgerEntry.id.in_(ids))
        .all()
    )
    by_id = {int(row.id): row for row in rows}
    if set(by_id) != set(ids):
        missing = sorted(set(ids) - set(by_id))
        raise BoundedSupplierEvidenceError(
            f"changed supplier SLE ids are missing: {missing}"
        )
    for row in rows:
        if not bool(row.active):
            raise BoundedSupplierEvidenceError(
                f"changed supplier SLE {int(row.id)} is not active"
            )
        batch_id = int(row.ingest_batch_id)
        if not lower_batch_id < batch_id <= upper_batch_id:
            raise BoundedSupplierEvidenceError(
                f"changed supplier SLE {int(row.id)} is outside target import boundary"
            )
        if row.posting_at is None or _comparable_datetime(row.posting_at) is None:
            raise BoundedSupplierEvidenceError(
                f"changed supplier SLE {int(row.id)} has no posting boundary"
            )
        posting_at = _comparable_datetime(row.posting_at)
        if posting_at <= _comparable_datetime(parent_cutoff):
            raise BoundedSupplierEvidenceError(
                f"{_REJECTED_DELTA_MESSAGE}: backdated supplier SLE {int(row.id)}"
            )
        if posting_at > _comparable_datetime(target_cutoff):
            raise BoundedSupplierEvidenceError(
                f"changed supplier SLE {int(row.id)} is after target cutoff"
            )
        _scope_for_row(row, scopes)
        if not is_supplier_document_type(row.recorder_type):
            raise BoundedSupplierEvidenceError(
                f"changed supplier SLE {int(row.id)} is not a supplier document"
            )
    return tuple(sorted(rows, key=lambda row: int(row.id)))


def _validate_manifest_shape(
    db: Session,
    *,
    manifest: BoundedBuyReceiptDeltaManifest,
    parent: models.LedgerGeneration,
    target: models.LedgerGeneration,
    scopes: tuple[DistributionScope, ...],
    target_cutoff: datetime,
) -> BoundedBuyReceiptDeltaManifest:
    try:
        normalized = _normalise_bounded_buy_manifest(manifest)
    except CurrentReplenishmentError as exc:
        raise BoundedSupplierEvidenceError(str(exc)) from exc
    if normalized.scope_receipt_facts or normalized.supersession_edge_ids or normalized.backdate_from:
        raise BoundedSupplierEvidenceError(
            f"{_REJECTED_DELTA_MESSAGE}: complete scope evidence is required"
        )
    if set(normalized.new_sle_ids) != {
        int(getattr(fact, "sle_id", -1)) for fact in normalized.receipt_facts
    }:
        raise BoundedSupplierEvidenceError(
            "supplier manifest new_sle_ids must exactly match typed receipt facts"
        )
    scopes_by_key: dict[tuple[int, str], list[DistributionScope]] = {}
    for scope in scopes:
        scopes_by_key.setdefault((scope[0], scope[3]), []).append(scope)
    for fact in normalized.receipt_facts:
        if not isinstance(fact, ReceiptFact):
            raise BoundedSupplierEvidenceError("supplier manifest contains untyped receipt evidence")
        row = db.get(models.StockLedgerEntry, int(fact.sle_id))
        if row is None:
            raise BoundedSupplierEvidenceError("supplier manifest references missing SLE")
        _scope_for_row(row, scopes)
        if int(row.ingest_batch_id) <= int(parent.physical_import_batch_id) or int(row.ingest_batch_id) > int(target.physical_import_batch_id):
            raise BoundedSupplierEvidenceError("supplier manifest SLE is outside target boundary")
        if _comparable_datetime(row.posting_at) <= _comparable_datetime(parent.cutoff) or _comparable_datetime(row.posting_at) > _comparable_datetime(target_cutoff):
            raise BoundedSupplierEvidenceError("supplier manifest SLE is outside forward cutoff")
        if int(fact.item_id) != int(row.item_id) or Decimal(str(fact.signed_qty)) != Decimal(str(row.qty)):
            raise BoundedSupplierEvidenceError("supplier manifest fact contradicts persisted SLE")
    return normalized


def validate_bounded_supplier_receipt_manifest(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
    target_cutoff: datetime,
    affected_scopes: Iterable[DistributionScope],
    manifest: BoundedBuyReceiptDeltaManifest,
) -> BoundedBuyReceiptDeltaManifest:
    """Validate a previously built manifest without any database writes."""

    parent, target, _lower, _upper = _boundary(
        db,
        parent_generation_id=parent_generation_id,
        target_generation_id=target_generation_id,
        target_cutoff=target_cutoff,
    )
    try:
        scopes = _normalise_bounded_buy_scopes(affected_scopes)
    except CurrentReplenishmentError as exc:
        raise BoundedSupplierEvidenceError(str(exc)) from exc
    return _validate_manifest_shape(
        db,
        manifest=manifest,
        parent=parent,
        target=target,
        scopes=scopes,
        target_cutoff=target_cutoff,
    )


def build_bounded_supplier_receipt_manifest(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
    target_cutoff: datetime,
    odata_client: object,
    changed_sle_ids: Iterable[int | models.StockLedgerEntry],
    affected_scopes: Iterable[DistributionScope],
) -> BoundedBuyReceiptDeltaManifest:
    """Build forward BUY receipt facts from an explicit physical delta only.

    ``extract_supplier_document_evidence`` is the sole source of document
    matching.  The explicit SLE list is the complete input boundary: an old
    receipt that is not in it is never read, inferred, or silently replayed.
    """

    # Materialise only the caller-provided delta ids.  An empty physical
    # delta is a valid no-op and does not need a network client at all.
    changed_ids = tuple(changed_sle_ids)
    parent, target, lower, upper = _boundary(
        db,
        parent_generation_id=parent_generation_id,
        target_generation_id=target_generation_id,
        target_cutoff=target_cutoff,
    )
    try:
        scopes = _normalise_bounded_buy_scopes(affected_scopes)
    except CurrentReplenishmentError as exc:
        raise BoundedSupplierEvidenceError(str(exc)) from exc
    rows = _load_changed_rows(
        db,
        changed_sle_ids=changed_ids,
        lower_batch_id=lower,
        upper_batch_id=upper,
        target_cutoff=target_cutoff,
        parent_cutoff=parent.cutoff,
        scopes=scopes,
    )
    if not rows:
        return BoundedBuyReceiptDeltaManifest()
    if odata_client is None:
        raise BoundedSupplierEvidenceError("supplier evidence requires an OData client")
    extraction: SupplierEvidenceExtractionResult = extract_supplier_document_evidence(
        db, odata_client, rows
    )
    if extraction.diagnostics:
        diagnostic = extraction.diagnostics[0]
        raise BoundedSupplierEvidenceError(
            f"supplier document evidence diagnostic {diagnostic.code}: {diagnostic.detail}"
        )
    excluded_ids = {
        int(entry.stock_ledger_entry_id)
        for entry in extraction.ignored_stock_ledger_entries
    }
    if excluded_ids:
        raise BoundedSupplierEvidenceError(
            f"supplier SLEs are excluded non-supplier documents: {sorted(excluded_ids)}"
        )
    try:
        normalized_rows = normalize_supplier_receipt_evidence(
            db,
            explicit_sles=rows,
            evidence=extraction.evidence,
        )
    except SupplierReceiptEvidenceError as exc:
        raise BoundedSupplierEvidenceError(str(exc)) from exc
    facts: list[ReceiptFact] = []
    seen_sle_ids: set[int] = set()
    for normalized in normalized_rows:
        if normalized.operation != _FORWARD_OPERATION or normalized.fact.signed_qty <= 0:
            raise BoundedSupplierEvidenceError(
                f"{_REJECTED_DELTA_MESSAGE}: supplier operation is not a forward receipt"
            )
        if normalized.match_status == "ambiguous":
            raise BoundedSupplierEvidenceError(
                f"ambiguous supplier-order evidence for SLE {normalized.fact.sle_id}"
            )
        row = db.get(models.StockLedgerEntry, int(normalized.fact.sle_id))
        if row is None:
            raise BoundedSupplierEvidenceError("supplier evidence references missing SLE")
        scope = _scope_for_row(row, scopes)
        fact = replace(normalized.fact, planning_stock_pool=scope[3])
        if fact.sle_id in seen_sle_ids:
            raise BoundedSupplierEvidenceError("supplier evidence maps one SLE more than once")
        seen_sle_ids.add(fact.sle_id)
        facts.append(fact)
    expected_ids = {int(row.id) for row in rows}
    if seen_sle_ids != expected_ids:
        missing = sorted(expected_ids - seen_sle_ids)
        raise BoundedSupplierEvidenceError(
            f"{_REJECTED_DELTA_MESSAGE}: no typed forward evidence for SLEs {missing}"
        )
    manifest = BoundedBuyReceiptDeltaManifest(
        new_sle_ids=tuple(sorted(seen_sle_ids)),
        receipt_facts=tuple(sorted(facts, key=lambda fact: int(fact.sle_id))),
    )
    return validate_bounded_supplier_receipt_manifest(
        db,
        parent_generation_id=parent_generation_id,
        target_generation_id=target_generation_id,
        target_cutoff=target_cutoff,
        affected_scopes=scopes,
        manifest=manifest,
    )


__all__ = [
    "BoundedSupplierEvidenceError",
    "BoundedSupplierEvidenceSummary",
    "build_bounded_supplier_receipt_manifest",
    "is_supplier_document_type",
    "validate_bounded_supplier_receipt_manifest",
]

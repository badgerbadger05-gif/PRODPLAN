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
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Session

from app import models

from .current_replenishment import (
    BoundedBuyReceiptDeltaManifest,
    CurrentReplenishmentError,
    DistributionScope,
    _normalise_bounded_buy_manifest,
    _normalise_bounded_buy_scopes,
)
from .physical_visibility import (
    PhysicalVisibilityError,
    require_import_batch,
    visible_sle_query,
    visible_sle_query_for_generation,
)
from .supplier_receipt_allocation import (
    CORRECTION_OPERATION,
    RECEIPT_OPERATION,
    SUPPLIER_RETURN_OPERATION,
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


def supplier_document_type_filter(column: Any) -> Any:
    """SQL form of :func:`is_supplier_document_type` over the same one set.

    A caller that has to count supplier rows instead of iterating them must
    not restate the document list in its own query.
    """
    return column.in_(sorted(_SUPPLIER_DOCUMENT_TYPES))


def lost_supplier_receipt_provenance_sle_ids(
    db: Session,
    *,
    ledger_generation_id: int,
    rows: Sequence[models.StockLedgerEntry] | None = None,
    contour: Mapping[str, str] | None = None,
    limit: int = 0,
) -> tuple[int, ...]:
    """Visible supplier facts this generation lost the typed evidence of.

    A fact counts as lost when some generation holds a provenance row for it
    and this one does not.  That is deliberately narrower than "has no row":
    a supplier document outside the planning contour is never typed by
    anybody and is not evidence loss, while a fact the system already typed
    once and then stopped owning is exactly the defect - the accepted pointer
    is the only generation the current replenishment reader looks at, so its
    missing row makes an accepted physical quantity uncountable.
    ``planning-truth-contract.md`` forbids that in as many words ("Provenance
    не может сделать принятое физическое количество неучитываемым"), and
    ``item_ledger.md`` lists "отказ учитывать принятый факт из-за provenance"
    among the outright prohibitions.

    A row in any status counts as owned, ``excluded_non_supplier`` included:
    the writer looked at the fact and typed it, which is what ownership means
    here.  ``limit`` bounds the diagnostic, never the verdict.

    ``contour`` restricts the verdict to the live planning contour.  A fact on
    a warehouse that has since left it is no longer a planning receipt at all,
    so its missing evidence cannot make a planning quantity uncountable and
    must not block publication for ever.

    ``rows`` restricts it to an explicit fact set.  A publication passes its
    own delta - CANON "Объём вычислений штатного физического refresh" makes
    that the unit of work, and history the refresh did not touch is not its
    verdict to give.  Maintenance passes ``rows=None`` and stays prefix-wide
    on purpose, because repairing history is exactly its job.  The repair
    phase passes the resolved live contour, so it repairs what the
    publication gate would refuse and nothing else.  The GC probe passes no
    contour, by design: it guards the *removal* of evidence, a warehouse
    outside today's contour may rejoin it, and deleting the only typed copy
    of such a fact is irreversible - so it is prefix-wide and contour-less.
    One function, two explicit arguments, so the callers cannot drift apart.
    """
    owned_here = exists().where(and_(
        models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id
        == models.StockLedgerEntry.id,
        models.StockLedgerSupplierReceiptProvenance.ledger_generation_id
        == int(ledger_generation_id),
    ))
    owned_elsewhere = exists().where(
        models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id
        == models.StockLedgerEntry.id
    )
    query = (
        visible_sle_query_for_generation(db, int(ledger_generation_id))
        .filter(supplier_document_type_filter(models.StockLedgerEntry.recorder_type))
        .filter(models.StockLedgerEntry.active.is_(True))
        .filter(models.StockLedgerEntry.qty != 0)
        .filter(~owned_here)
        .filter(owned_elsewhere)
        .order_by(None)
        .with_entities(models.StockLedgerEntry.id)
        .order_by(models.StockLedgerEntry.id.asc())
    )
    if contour is not None:
        warehouses = [
            str(key).strip()
            for key, value in dict(contour).items()
            if str(key or "").strip() and str(value or "").strip()
        ]
        if not warehouses:
            return ()
        query = query.filter(
            models.StockLedgerEntry.warehouse_ref1c.in_(sorted(warehouses))
        )
    if rows is not None:
        row_ids = sorted({int(row.id) for row in rows})
        if not row_ids:
            return ()
        query = query.filter(models.StockLedgerEntry.id.in_(row_ids))
    if limit and int(limit) > 0:
        query = query.limit(int(limit))
    return tuple(int(value) for (value,) in query.all())


def untyped_supplier_receipt_sle_ids(
    db: Session,
    *,
    ledger_generation_id: int,
    limit: int = 0,
) -> tuple[int, ...]:
    """Visible supplier facts no generation has ever typed.

    Distinct from :func:`lost_supplier_receipt_provenance_sle_ids`: this is
    not evidence loss.  A supplier document outside the planning contour is
    never typed by design, so these are reported, never repaired and never
    invented.  The one case where they are a verdict is a database in which
    *nothing* is typed at all, which no re-own can fix.
    """
    owned_anywhere = exists().where(
        models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id
        == models.StockLedgerEntry.id
    )
    query = (
        visible_sle_query_for_generation(db, int(ledger_generation_id))
        .filter(supplier_document_type_filter(models.StockLedgerEntry.recorder_type))
        .filter(models.StockLedgerEntry.active.is_(True))
        .filter(models.StockLedgerEntry.qty != 0)
        .filter(~owned_anywhere)
        .order_by(None)
        .with_entities(models.StockLedgerEntry.id)
        .order_by(models.StockLedgerEntry.id.asc())
    )
    if limit and int(limit) > 0:
        query = query.limit(int(limit))
    return tuple(int(value) for (value,) in query.all())


def untyped_supplier_receipt_rows_in_contour(
    db: Session,
    *,
    ledger_generation_id: int,
    planning_pool_by_warehouse: Mapping[str, str],
    limit: int = 0,
) -> tuple[models.StockLedgerEntry, ...]:
    """Candidate rows for the "typed nothing" verdict, not the verdict itself.

    Visible supplier receipts inside the caller's planning contour that this
    generation holds no provenance for.  Whether such a row is actually owed
    to a BUY order is decided by the one scope resolver in
    ``physical_refresh_current_publish``, which is also what the publisher
    uses to build the delta - asking the question twice in two places is how
    the two answers drift apart.

    The contour is the caller's resolved mapping, never re-derived here.
    """
    warehouses = [
        str(key).strip()
        for key, value in dict(planning_pool_by_warehouse or {}).items()
        if str(key or "").strip() and str(value or "").strip()
    ]
    if not warehouses:
        return ()
    owned_here = exists().where(and_(
        models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id
        == models.StockLedgerEntry.id,
        models.StockLedgerSupplierReceiptProvenance.ledger_generation_id
        == int(ledger_generation_id),
    ))
    query = (
        visible_sle_query_for_generation(db, int(ledger_generation_id))
        .filter(supplier_document_type_filter(models.StockLedgerEntry.recorder_type))
        .filter(models.StockLedgerEntry.movement_kind.in_(
            ("receipt", "supplier_receipt")
        ))
        .filter(models.StockLedgerEntry.active.is_(True))
        .filter(models.StockLedgerEntry.qty != 0)
        .filter(models.StockLedgerEntry.warehouse_ref1c.in_(sorted(warehouses)))
        .filter(~owned_here)
        .order_by(None)
        .order_by(models.StockLedgerEntry.id.asc())
    )
    if limit and int(limit) > 0:
        query = query.limit(int(limit))
    return tuple(query.all())


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
    """Place one physical row in the BUY scope the publisher resolved for it.

    The row is keyed through the canonical collapse
    (``mrp_freeze.pool_key_for``), the same one the reservations behind those
    scopes were frozen with.  Comparing raw columns here would reject every
    row the publisher had just accepted.
    """
    from app.services.mrp_freeze import distribution_scope_for

    key = distribution_scope_for(
        int(row.item_id),
        _text(row.characteristic_ref),
        _text(row.organization_ref),
        mode="buy",
    )
    candidates = tuple(scope for scope in scopes if tuple(scope) == key)
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
    backdate_from: datetime | None = None,
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
            # A backdated supplier fact is admissible only inside an explicitly
            # declared bounded replay window; the complete affected-scope
            # stream below is what makes its FIFO position provable.
            if backdate_from is None:
                raise BoundedSupplierEvidenceError(
                    f"{_REJECTED_DELTA_MESSAGE}: backdated supplier SLE {int(row.id)}"
                )
            if posting_at < _comparable_datetime(backdate_from):
                raise BoundedSupplierEvidenceError(
                    f"supplier SLE {int(row.id)} precedes the declared bounded "
                    "backdate boundary"
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
    bounded_replay: bool = False,
) -> BoundedBuyReceiptDeltaManifest:
    try:
        normalized = _normalise_bounded_buy_manifest(manifest)
    except CurrentReplenishmentError as exc:
        raise BoundedSupplierEvidenceError(str(exc)) from exc
    if not bounded_replay and (
        normalized.scope_receipt_facts
        or normalized.supersession_edge_ids
        or normalized.backdate_from
    ):
        raise BoundedSupplierEvidenceError(
            f"{_REJECTED_DELTA_MESSAGE}: complete scope evidence is required"
        )
    if bounded_replay and normalized.backdate_from is None:
        raise BoundedSupplierEvidenceError(
            "bounded supplier replay requires an explicit backdate boundary"
        )
    if bounded_replay and not normalized.scope_receipt_facts:
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
        lower_bound = (
            normalized.backdate_from if bounded_replay else parent.cutoff
        )
        too_old = (
            _comparable_datetime(row.posting_at) < _comparable_datetime(lower_bound)
            if bounded_replay
            else _comparable_datetime(row.posting_at) <= _comparable_datetime(lower_bound)
        )
        if too_old or _comparable_datetime(row.posting_at) > _comparable_datetime(target_cutoff):
            raise BoundedSupplierEvidenceError("supplier manifest SLE is outside forward cutoff")
        if int(fact.item_id) != int(row.item_id) or Decimal(str(fact.signed_qty)) != Decimal(str(row.qty)):
            raise BoundedSupplierEvidenceError("supplier manifest fact contradicts persisted SLE")
    return normalized


_SUPPLIER_OPERATION_KINDS = ("supplier_receipt", "correction", "supplier_return")


def _persisted_scope_evidence(
    db: Session,
    sle_ids: Sequence[int],
) -> dict[int, models.StockLedgerSupplierReceiptProvenance]:
    """Return one unambiguous typed provenance row per affected SLE."""

    if not sle_ids:
        return {}
    rows = (
        db.query(models.StockLedgerSupplierReceiptProvenance)
        .filter(
            models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id.in_(
                sorted(set(int(value) for value in sle_ids))
            ),
            models.StockLedgerSupplierReceiptProvenance.operation_kind.in_(
                _SUPPLIER_OPERATION_KINDS
            ),
        )
        .all()
    )
    resolved: dict[int, models.StockLedgerSupplierReceiptProvenance] = {}
    for row in rows:
        sle_id = int(row.stock_ledger_entry_id)
        previous = resolved.get(sle_id)
        if previous is None:
            resolved[sle_id] = row
            continue
        signature = (
            _text(row.receipt_doc_ref), _text(row.receipt_doc_line_no),
            _text(row.supplier_order_ref), _text(row.supplier_order_line_no),
            _text(row.operation_kind), _text(row.correction_receipt_ref),
        )
        previous_signature = (
            _text(previous.receipt_doc_ref), _text(previous.receipt_doc_line_no),
            _text(previous.supplier_order_ref), _text(previous.supplier_order_line_no),
            _text(previous.operation_kind), _text(previous.correction_receipt_ref),
        )
        if signature != previous_signature:
            raise BoundedSupplierEvidenceError(
                "persisted supplier evidence for one SLE is inconsistent across "
                f"generations (sle_id={sle_id})"
            )
    return resolved


def _bounded_scope_receipt_facts(
    db: Session,
    *,
    target: models.LedgerGeneration,
    target_cutoff: datetime,
    scopes: tuple[DistributionScope, ...],
    delta_facts_by_id: dict[int, ReceiptFact],
    planning_pool_by_warehouse: Mapping[str, str] | None,
) -> tuple[ReceiptFact, ...]:
    """Return the complete signed supplier stream of the affected BUY scopes.

    A correction or a backdated receipt changes the FIFO position of every
    later assignment inside the same distribution scope, so the allocator needs
    the whole signed stream of that scope — and only of that scope.  The query
    is shaped by the affected scopes and the target visibility boundary; it
    never reads another item, pool or the historical prefix at large.
    """

    predicates = [
        and_(
            models.StockLedgerEntry.item_id == int(scope[0]),
            models.StockLedgerEntry.characteristic_ref == scope[1],
            models.StockLedgerEntry.organization_ref == scope[2],
        )
        for scope in scopes
    ]
    rows = (
        visible_sle_query(
            db,
            physical_import_batch_id=int(target.physical_import_batch_id),
            cutoff=_comparable_datetime(target_cutoff),
        )
        .filter(
            or_(*predicates),
            models.StockLedgerEntry.recorder_type.in_(sorted(_SUPPLIER_DOCUMENT_TYPES)),
        )
        .all()
    )
    evidence_by_id = _persisted_scope_evidence(
        db, [int(row.id) for row in rows if int(row.id) not in delta_facts_by_id]
    )
    facts: list[ReceiptFact] = []
    for row in rows:
        scope = _scope_for_row(row, scopes)
        if planning_pool_by_warehouse is not None:
            pool = _text(planning_pool_by_warehouse.get(_text(row.warehouse_ref1c)))
            if pool != scope[3]:
                continue
        delta = delta_facts_by_id.get(int(row.id))
        if delta is not None:
            facts.append(delta)
            continue
        evidence = evidence_by_id.get(int(row.id))
        if evidence is None:
            raise BoundedSupplierEvidenceError(
                "bounded BUY scope evidence lacks persisted supplier provenance "
                f"for SLE {int(row.id)}; explicit maintenance replay is required"
            )
        if _text(evidence.match_status) == "excluded_non_supplier":
            continue
        if _text(evidence.match_status) == "ambiguous":
            raise BoundedSupplierEvidenceError(
                "bounded BUY scope evidence has ambiguous supplier-order "
                f"evidence for SLE {int(row.id)}"
            )
        facts.append(ReceiptFact(
            sle_id=int(row.id),
            posting_at=row.posting_at,
            known_at=getattr(row, "known_at", None),
            ingest_batch_id=getattr(row, "ingest_batch_id", None),
            signed_qty=Decimal(str(row.qty)),
            item_id=int(row.item_id),
            supplier_order_ref=_text(evidence.supplier_order_ref),
            supplier_order_line_no=_text(evidence.supplier_order_line_no),
            receipt_ref=_text(evidence.receipt_doc_ref),
            receipt_line_no=_text(evidence.receipt_doc_line_no),
            correction_receipt_ref=_text(evidence.correction_receipt_ref) or None,
            planning_stock_pool=scope[3],
        ))
    return tuple(sorted(facts, key=lambda fact: (int(fact.sle_id))))


def validate_bounded_supplier_receipt_manifest(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
    target_cutoff: datetime,
    affected_scopes: Iterable[DistributionScope],
    manifest: BoundedBuyReceiptDeltaManifest,
    bounded_replay: bool = False,
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
        bounded_replay=bounded_replay,
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
    backdate_from: datetime | None = None,
    supersession_edge_ids: Sequence[int] = (),
    planning_pool_by_warehouse: Mapping[str, str] | None = None,
) -> BoundedBuyReceiptDeltaManifest:
    """Build BUY receipt facts from an explicit physical delta only.

    ``extract_supplier_document_evidence`` is the sole source of document
    matching.  The explicit SLE list is the complete input boundary: an old
    receipt that is not in it is never read, inferred, or silently replayed.

    A declared ``backdate_from`` boundary or a supersession edge switches the
    adapter into the bounded replay mode required by CANON "Объём вычислений
    штатного физического refresh": the affected distribution scopes — and only
    they — additionally contribute their complete signed stream, so the
    canonical allocator can re-derive their FIFO from the earliest changed
    boundary without a full historical replay.
    """

    # Materialise only the caller-provided delta ids.  An empty physical
    # delta is a valid no-op and does not need a network client at all.
    changed_ids = tuple(changed_sle_ids)
    bounded_replay = backdate_from is not None or bool(tuple(supersession_edge_ids))
    parent, target, lower, upper = _boundary(
        db,
        parent_generation_id=parent_generation_id,
        target_generation_id=target_generation_id,
        target_cutoff=target_cutoff,
    )
    if bounded_replay and backdate_from is None:
        raise BoundedSupplierEvidenceError(
            "bounded supplier replay requires an explicit backdate boundary"
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
        backdate_from=backdate_from,
    )
    if not rows and not bounded_replay:
        return BoundedBuyReceiptDeltaManifest()
    facts: list[ReceiptFact] = []
    seen_sle_ids: set[int] = set()
    if rows:
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
        allowed_operations = (
            {RECEIPT_OPERATION, CORRECTION_OPERATION, SUPPLIER_RETURN_OPERATION}
            if bounded_replay else {_FORWARD_OPERATION}
        )
        for normalized in normalized_rows:
            if normalized.operation not in allowed_operations or (
                not bounded_replay and normalized.fact.signed_qty <= 0
            ):
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
    delta_facts = tuple(sorted(facts, key=lambda fact: int(fact.sle_id)))
    scope_facts: tuple[ReceiptFact, ...] = ()
    if bounded_replay:
        scope_facts = _bounded_scope_receipt_facts(
            db,
            target=target,
            target_cutoff=target_cutoff,
            scopes=scopes,
            delta_facts_by_id={int(fact.sle_id): fact for fact in delta_facts},
            planning_pool_by_warehouse=planning_pool_by_warehouse,
        )
        missing = sorted(seen_sle_ids - {int(fact.sle_id) for fact in scope_facts})
        if missing:
            raise BoundedSupplierEvidenceError(
                "bounded BUY scope evidence omits the typed delta facts "
                f"{missing}; explicit maintenance replay is required"
            )
    manifest = BoundedBuyReceiptDeltaManifest(
        new_sle_ids=tuple(sorted(seen_sle_ids)),
        receipt_facts=delta_facts,
        scope_receipt_facts=scope_facts,
        supersession_edge_ids=tuple(int(value) for value in supersession_edge_ids),
        backdate_from=backdate_from,
    )
    return validate_bounded_supplier_receipt_manifest(
        db,
        parent_generation_id=parent_generation_id,
        target_generation_id=target_generation_id,
        target_cutoff=target_cutoff,
        affected_scopes=scopes,
        manifest=manifest,
        bounded_replay=bounded_replay,
    )


__all__ = [
    "BoundedSupplierEvidenceError",
    "BoundedSupplierEvidenceSummary",
    "build_bounded_supplier_receipt_manifest",
    "is_supplier_document_type",
    "supplier_document_type_filter",
    "lost_supplier_receipt_provenance_sle_ids",
    "untyped_supplier_receipt_rows_in_contour",
    "untyped_supplier_receipt_sle_ids",
    "validate_bounded_supplier_receipt_manifest",
]

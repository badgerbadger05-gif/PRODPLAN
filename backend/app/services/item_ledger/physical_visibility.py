"""Historical visibility for shared, revisioned physical Ledger facts.

Visibility is defined by the import-batch boundary, never by the mutable
``StockLedgerEntry.active`` convenience flag.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Query, Session

from app import models


class PhysicalVisibilityError(ValueError):
    """The requested physical fact boundary is absent or non-deterministic."""


def require_import_batch(
    db: Session,
    physical_import_batch_id: int,
) -> models.PhysicalImportBatch:
    """Resolve an explicit import boundary and validate retained provenance."""
    batch = db.get(models.PhysicalImportBatch, int(physical_import_batch_id))
    if batch is None:
        raise PhysicalVisibilityError(
            f"physical import batch {physical_import_batch_id} does not exist"
        )
    if not str(batch.batch_key or "").strip():
        raise PhysicalVisibilityError("physical import batch has no deterministic batch_key")
    if not isinstance(batch.source_watermarks, dict):
        raise PhysicalVisibilityError("physical import batch has no source watermarks")
    if str(batch.status) != "completed":
        raise PhysicalVisibilityError(
            f"physical import batch {batch.id} is {batch.status}; completed required"
        )
    if not bool(batch.source_complete):
        raise PhysicalVisibilityError(
            f"physical import batch {batch.id} is incomplete; source_complete required"
        )
    if batch.expected_page_count is not None:
        pages = (
            db.query(models.PhysicalImportPage.page_no)
            .filter(models.PhysicalImportPage.import_batch_id == int(batch.id))
            .order_by(models.PhysicalImportPage.id.asc())
            .all()
        )
        numbers = [int(page_no) for (page_no,) in pages]
        expected = int(batch.expected_page_count)
        if numbers != list(range(1, expected + 1)):
            raise PhysicalVisibilityError(
                f"physical import batch {batch.id} has incomplete or reordered pages"
            )
    return batch


def visible_sle_query(
    db: Session,
    *,
    physical_import_batch_id: int,
    cutoff: datetime | None = None,
) -> Query:
    """Return SLE revisions visible at an explicit historical batch watermark.

    A revision is visible when it was imported no later than the watermark and
    no supersession of that revision had occurred by that boundary. A
    supersession with ``new_sle_id=NULL`` is therefore a tombstone. Later
    transitions do not alter earlier results.
    """
    batch = require_import_batch(db, physical_import_batch_id)
    boundary = int(batch.id)
    superseded_by_boundary = exists().where(
        models.StockLedgerFactSupersession.old_sle_id
        == models.StockLedgerEntry.id,
        models.StockLedgerFactSupersession.import_batch_id <= boundary,
    )
    query = db.query(models.StockLedgerEntry).filter(
        models.StockLedgerEntry.ingest_batch_id <= boundary,
        ~superseded_by_boundary,
    )
    if cutoff is not None:
        query = query.filter(models.StockLedgerEntry.posting_at <= cutoff)
    return query.order_by(
        models.StockLedgerEntry.posting_at.asc(),
        models.StockLedgerEntry.id.asc(),
    )


#: One revision of a document line as the freeze would have seen it:
#: ``(ingest_batch_id, first superseding batch or None, posting_at)``.
Revision = tuple


#: Session-scoped counters of the revision lookup (calls, identities looked
#: up, queries issued), read and reset by a publication for its metrics.
KNOWN_REVISIONS_METRICS_KEY = "known_revisions_lookup"


def take_known_revisions_metrics(db: Session) -> dict[str, int]:
    """Return and reset the revision-lookup counters of this session."""
    return dict(
        db.info.pop(KNOWN_REVISIONS_METRICS_KEY, None)
        or {"calls": 0, "identities": 0, "queries": 0}
    )


def known_revisions_by_sle(
    db: Session, rows: Any
) -> dict[int, tuple[Revision, ...]]:
    """Every revision of each fact's document line, for the freeze test (§53).

    A document re-posted in 1C is imported again as new SLE revisions.
    Whether the freeze knew a line is therefore a question about *all* its
    revisions: was one of them visible at the freeze batch - imported by it
    and not superseded or tombstoned by a batch not later than it - exactly
    as ``visible_sle_query`` builds the frozen stock.  The predicate
    (``historical_replay_core.known_at_freeze``) evaluates these intervals
    against each owner's own batch; a line unposted before the freeze and
    re-posted after it is not stock.

    Line continuity (decision §55): a line is the same line when the
    document (recorder) and the physical key (item, characteristic,
    organization, warehouse) match; the 1C line number does not take part,
    so a line inserted or deleted after the freeze does not turn the shifted
    lines into new facts.  Repeats of one key inside one document are paired
    by order: in every import batch of the document, the k-th line of that
    key (by line number) is the same line as the judged row's k-th.  The
    same line number on the same key is the fast path.  A row without a
    recorder is its own only revision.

    Known limitation of order matching (accepted, recorded for the owner):
    a repeat of a key inserted after the freeze BEFORE an existing repeat of
    the same key in the same document takes the old line's position - the
    new line's quantity is treated as stock at the freeze, and the old line,
    now in a later position, counts anew as replenishment.  Only repeats of
    one physical key inside one document are affected.

    One grouped query per recorder type and chunk of at most 1000 documents
    of that type (``recorder_type = :type AND recorder_ref IN (...)``, served
    by ``ix_stock_ledger_entry_recorder``), plus one per chunk of rows
    without a recorder; restricted to the documents of the rows passed in.
    """
    facts = [row for row in rows if row is not None]
    sle = models.StockLedgerEntry
    supersession = models.StockLedgerFactSupersession

    def _key(row_or_values) -> tuple:
        item, characteristic, organization, warehouse = row_or_values
        return (int(item), str(characteristic or ""), str(organization or ""), str(warehouse or ""))

    def _line_order(line_no: Any, sle_id: int) -> tuple:
        text = str(line_no or "").strip()
        return (int(text) if text.isdigit() else 10 ** 12, text, int(sle_id))

    def _revision_query():
        return (
            db.query(
                sle.id, sle.recorder_type, sle.recorder_ref, sle.line_no,
                sle.item_id, sle.characteristic_ref, sle.organization_ref,
                sle.warehouse_ref1c, sle.ingest_batch_id, sle.posting_at,
                func.min(supersession.import_batch_id),
            )
            .outerjoin(supersession, supersession.old_sle_id == sle.id)
            .group_by(sle.id)
        )

    documents_by_type: dict[str, set[str]] = {}
    for row in facts:
        if str(getattr(row, "recorder_ref", "") or "").strip():
            documents_by_type.setdefault(
                str(getattr(row, "recorder_type", "") or ""), set()
            ).add(str(row.recorder_ref))
    metrics = db.info.setdefault(
        KNOWN_REVISIONS_METRICS_KEY, {"calls": 0, "identities": 0, "queries": 0},
    )
    metrics["calls"] += 1
    metrics["identities"] += sum(len(refs) for refs in documents_by_type.values())
    # (recorder_type, recorder_ref, key) -> batch -> [(order, revision, line_no)]
    lines: dict[tuple, dict[int, list[tuple]]] = {}
    by_id: dict[int, Revision] = {}

    def _remember(values) -> None:
        (
            sle_id, recorder_type, recorder_ref, line_no, item_id, characteristic,
            organization, warehouse, batch_id, posting_at, superseded_batch,
        ) = values
        revision = (
            int(batch_id),
            int(superseded_batch) if superseded_batch is not None else None,
            posting_at,
        )
        by_id[int(sle_id)] = revision
        if str(recorder_ref or "").strip():
            document = (
                str(recorder_type or ""), str(recorder_ref),
                _key((item_id, characteristic, organization, warehouse)),
            )
            lines.setdefault(document, {}).setdefault(int(batch_id), []).append(
                (_line_order(line_no, sle_id), revision, str(line_no or "").strip(), int(sle_id))
            )

    for recorder_type, refs in sorted(documents_by_type.items()):
        # NULL and '' are the same (empty) recorder type, as in ``_remember``.
        type_filter = (
            sle.recorder_type == recorder_type
            if recorder_type
            else or_(sle.recorder_type.is_(None), sle.recorder_type == "")
        )
        documents = sorted(refs)
        for offset in range(0, len(documents), 1000):
            metrics["queries"] += 1
            for values in _revision_query().filter(
                type_filter, sle.recorder_ref.in_(documents[offset:offset + 1000])
            ):
                _remember(values)
    anonymous = sorted({
        int(row.id) for row in facts
        if not str(getattr(row, "recorder_ref", "") or "").strip()
    })
    for offset in range(0, len(anonymous), 1000):
        metrics["queries"] += 1
        for values in _revision_query().filter(sle.id.in_(anonymous[offset:offset + 1000])):
            by_id[int(values[0])] = (
                int(values[8]),
                int(values[10]) if values[10] is not None else None,
                values[9],
            )

    result: dict[int, tuple[Revision, ...]] = {}
    for row in facts:
        own = by_id.get(int(row.id), (int(row.ingest_batch_id), None, row.posting_at))
        document = (
            str(getattr(row, "recorder_type", "") or ""),
            str(getattr(row, "recorder_ref", "") or ""),
            _key((row.item_id, row.characteristic_ref, row.organization_ref, row.warehouse_ref1c)),
        )
        batches = lines.get(document) if document[1].strip() else None
        if not batches:
            result[int(row.id)] = (own,)
            continue
        own_batch = sorted(batches.get(int(row.ingest_batch_id), []))
        own_line = str(getattr(row, "line_no", "") or "").strip()
        position = next(
            (index for index, entry in enumerate(own_batch) if entry[3] == int(row.id)),
            None,
        )
        revisions = {own}
        for batch_lines in batches.values():
            ordered = sorted(batch_lines)
            same_number = [entry for entry in ordered if entry[2] == own_line]
            if len(same_number) == 1:
                revisions.add(same_number[0][1])
            elif position is not None and position < len(ordered):
                revisions.add(ordered[position][1])
        result[int(row.id)] = tuple(sorted(
            revisions, key=lambda value: (value[0], value[1] or 0),
        ))
    return result


def visible_sles(
    db: Session,
    *,
    physical_import_batch_id: int,
    cutoff: datetime | None = None,
) -> list[models.StockLedgerEntry]:
    return visible_sle_query(
        db,
        physical_import_batch_id=physical_import_batch_id,
        cutoff=cutoff,
    ).all()


def visible_sle_query_for_generation(
    db: Session,
    ledger_generation_id: int,
) -> Query:
    """Query form of the prefix named by one Ledger generation.

    Same single visibility rule as :func:`visible_sles_for_generation`; the
    query form exists so a caller that only needs a bounded count or a typed
    subset does not have to materialise the whole prefix, and never so that a
    second visibility rule can be written in SQL somewhere else.
    """
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise PhysicalVisibilityError(
            f"Ledger generation {ledger_generation_id} does not exist"
        )
    return visible_sle_query(
        db,
        physical_import_batch_id=int(generation.physical_import_batch_id),
        cutoff=generation.cutoff,
    )


def visible_sles_for_generation(
    db: Session,
    ledger_generation_id: int,
) -> list[models.StockLedgerEntry]:
    """Resolve the immutable physical prefix named by one Ledger generation."""
    return visible_sle_query_for_generation(db, int(ledger_generation_id)).all()


def import_batch_provenance(
    db: Session,
    physical_import_batch_id: int,
) -> dict[str, Any]:
    """Machine-readable deterministic import identity retained for audit."""
    batch = require_import_batch(db, physical_import_batch_id)
    return {
        "physical_import_batch_id": int(batch.id),
        "batch_key": str(batch.batch_key),
        "cutoff": batch.cutoff,
        "source_watermarks": dict(batch.source_watermarks),
    }

"""Historical visibility for shared, revisioned physical Ledger facts.

Visibility is defined by the import-batch boundary, never by the mutable
``StockLedgerEntry.active`` convenience flag.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import exists, func
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


def known_revisions_by_sle(
    db: Session, rows: Any
) -> dict[int, tuple[Revision, ...]]:
    """Every revision of each fact's document line, for the freeze test (§53).

    A document re-posted in 1C is imported again as new SLE revisions, and
    ``business_identity`` survives the re-post.  Whether the freeze knew the
    line is therefore a question about *all* its revisions: was one of them
    visible at the freeze batch - imported by it and not superseded or
    tombstoned by a batch not later than it - exactly as
    ``visible_sle_query`` builds the frozen stock.  The predicate
    (``historical_replay_core.known_at_freeze``) evaluates these intervals
    against each owner's own batch; a line unposted before the freeze and
    re-posted after it is not stock.

    Technical reading of §53 for line renumbering: ``business_identity`` is
    ``movement:<type>:<ref>:<line_no>`` and does not name the item, so after
    a line is inserted or deleted in 1C, line *k* may carry another item.
    Continuity is judged by the identity AND the physical key (item,
    characteristic, organization, warehouse): a revision of the same identity
    on a different key is a different line and does not lend its first
    import.  A row without an identity is its own only revision.

    One grouped query per chunk of at most 1000 identities (plus one per
    chunk of rows without an identity), restricted to the identities of the
    rows passed in.
    """
    facts = [row for row in rows if row is not None]

    def _key(identity, item_id, characteristic, organization, warehouse):
        return (
            str(identity), int(item_id), str(characteristic or ""),
            str(organization or ""), str(warehouse or ""),
        )

    identities = sorted({
        str(row.business_identity)
        for row in facts
        if str(getattr(row, "business_identity", "") or "").strip()
    })
    by_key: dict[tuple, list[Revision]] = {}
    by_id: dict[int, Revision] = {}
    sle = models.StockLedgerEntry
    supersession = models.StockLedgerFactSupersession

    def _revision_query():
        return (
            db.query(
                sle.id, sle.business_identity, sle.item_id, sle.characteristic_ref,
                sle.organization_ref, sle.warehouse_ref1c, sle.ingest_batch_id,
                sle.posting_at, func.min(supersession.import_batch_id),
            )
            .outerjoin(supersession, supersession.old_sle_id == sle.id)
            .group_by(sle.id)
        )

    for offset in range(0, len(identities), 1000):
        chunk = identities[offset:offset + 1000]
        for (
            sle_id, identity, item_id, characteristic, organization, warehouse,
            batch_id, posting_at, superseded_batch,
        ) in _revision_query().filter(sle.business_identity.in_(chunk)):
            revision = (
                int(batch_id),
                int(superseded_batch) if superseded_batch is not None else None,
                posting_at,
            )
            by_id[int(sle_id)] = revision
            by_key.setdefault(
                _key(identity, item_id, characteristic, organization, warehouse), []
            ).append(revision)
    anonymous = sorted({
        int(row.id) for row in facts
        if not str(getattr(row, "business_identity", "") or "").strip()
    })
    for offset in range(0, len(anonymous), 1000):
        for (
            sle_id, _identity, _item, _char, _org, _wh, batch_id, posting_at, superseded_batch,
        ) in _revision_query().filter(sle.id.in_(anonymous[offset:offset + 1000])):
            by_id[int(sle_id)] = (
                int(batch_id),
                int(superseded_batch) if superseded_batch is not None else None,
                posting_at,
            )
    result: dict[int, tuple[Revision, ...]] = {}
    for row in facts:
        identity = str(getattr(row, "business_identity", "") or "").strip()
        own = by_id.get(int(row.id), (int(row.ingest_batch_id), None, row.posting_at))
        if identity:
            revisions = by_key.get(
                _key(identity, row.item_id, row.characteristic_ref,
                     row.organization_ref, row.warehouse_ref1c),
                [],
            )
            result[int(row.id)] = tuple(sorted(
                set(revisions) | {own}, key=lambda value: (value[0], value[1] or 0),
            ))
        else:
            result[int(row.id)] = (own,)
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

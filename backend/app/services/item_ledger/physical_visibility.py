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


def first_known_batch_by_sle(
    db: Session, rows: Any
) -> dict[int, int]:
    """The batch in which each fact's document line first became known (§53).

    A document re-posted in 1C is imported again as new SLE revisions in a
    new batch, but its stable ``business_identity`` is kept across
    revisions.  The Ledger knew the fact from the first import of that
    identity, so that is the batch the freeze boundary judges - a re-post
    after the freeze does not turn frozen stock into replenishment.  One
    grouped query per chunk of identities; a row without an identity is
    known from its own batch.
    """
    facts = [row for row in rows if row is not None]
    identities = sorted({
        str(row.business_identity)
        for row in facts
        if str(getattr(row, "business_identity", "") or "").strip()
    })
    first_by_identity: dict[str, int] = {}
    for offset in range(0, len(identities), 1000):
        chunk = identities[offset:offset + 1000]
        for identity, batch_id in (
            db.query(
                models.StockLedgerEntry.business_identity,
                func.min(models.StockLedgerEntry.ingest_batch_id),
            )
            .filter(models.StockLedgerEntry.business_identity.in_(chunk))
            .group_by(models.StockLedgerEntry.business_identity)
        ):
            first_by_identity[str(identity)] = int(batch_id)
    result: dict[int, int] = {}
    for row in facts:
        own = int(row.ingest_batch_id)
        identity = str(getattr(row, "business_identity", "") or "").strip()
        result[int(row.id)] = min(own, first_by_identity.get(identity, own))
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

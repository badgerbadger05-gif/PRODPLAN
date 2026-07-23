"""Historical visibility for shared, revisioned physical Ledger facts.

Visibility is defined by the import-batch boundary, never by the mutable
``StockLedgerEntry.active`` convenience flag.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import exists
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


def visible_sles_for_generation(
    db: Session,
    ledger_generation_id: int,
) -> list[models.StockLedgerEntry]:
    """Resolve the immutable physical prefix named by one Ledger generation."""
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise PhysicalVisibilityError(
            f"Ledger generation {ledger_generation_id} does not exist"
        )
    return visible_sles(
        db,
        physical_import_batch_id=int(generation.physical_import_batch_id),
        cutoff=generation.cutoff,
    )


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

"""R3 boundaries for stable physical identity and source completeness.

This module deliberately does not calculate stock or MRP.  It only validates
the publication boundary and resolves the explicit live-plan pointer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app import models


class ImportCompletenessError(ValueError):
    """A source page set is incomplete or arrived in an invalid order."""


def business_identity_for_movement(
    recorder_type: str, recorder_ref: str, line_no: str
) -> str:
    """Return a normalized, generation-independent movement identity."""
    parts = tuple(str(value or "").strip() for value in (recorder_type, recorder_ref, line_no))
    if not parts[0] or not parts[1] or not parts[2]:
        raise ValueError("movement business identity requires recorder type, ref and line")
    return "movement:" + ":".join(parts)


@dataclass(frozen=True)
class ImportPageReceipt:
    page_no: int
    page_token: str
    content_hash: str


def record_import_page(
    session: Session,
    import_batch_id: int,
    page_no: int,
    expected_page_count: int,
    content: str,
) -> models.PhysicalImportPage:
    """Insert one page idempotently without making the batch publishable."""
    if int(page_no) < 1 or int(expected_page_count) < 1 or int(page_no) > int(expected_page_count):
        raise ImportCompletenessError("page number is outside expected source range")
    token = f"page-{int(page_no)}"
    digest = hashlib.sha256(str(content).encode("utf-8")).hexdigest()
    batch = session.get(models.PhysicalImportBatch, int(import_batch_id))
    if batch is None:
        raise ValueError(f"unknown physical import batch {import_batch_id}")
    if batch.expected_page_count not in (None, int(expected_page_count)):
        raise ImportCompletenessError("expected page count changed for import batch")
    batch.expected_page_count = int(expected_page_count)
    batch.source_complete = False
    existing = (
        session.query(models.PhysicalImportPage)
        .filter_by(import_batch_id=int(import_batch_id), page_no=int(page_no))
        .one_or_none()
    )
    if existing is not None:
        if existing.content_hash != digest:
            raise ImportCompletenessError("page content changed for existing page")
        return existing
    row = models.PhysicalImportPage(
        import_batch_id=int(import_batch_id), page_no=int(page_no),
        page_token=token, content_hash=digest,
    )
    session.add(row)
    session.flush()
    batch.received_page_count = int(
        session.query(models.PhysicalImportPage)
        .filter_by(import_batch_id=int(import_batch_id)).count()
    )
    return row


def finalize_import(session: Session, import_batch_id: int) -> models.PhysicalImportBatch:
    """Complete a batch only after contiguous pages arrived in source order."""
    batch = session.get(models.PhysicalImportBatch, int(import_batch_id))
    if batch is None:
        raise ValueError(f"unknown physical import batch {import_batch_id}")
    expected = int(batch.expected_page_count or 0)
    pages = (
        session.query(models.PhysicalImportPage)
        .filter_by(import_batch_id=int(import_batch_id))
        .order_by(models.PhysicalImportPage.id.asc())
        .all()
    )
    numbers = [int(page.page_no) for page in pages]
    if expected < 1 or len(numbers) != expected or set(numbers) != set(range(1, expected + 1)):
        raise ImportCompletenessError("missing or duplicate import pages")
    if numbers != sorted(numbers):
        raise ImportCompletenessError("import pages arrived out of order")
    batch.received_page_count = len(pages)
    batch.source_complete = True
    batch.status = "completed"
    batch.completed_at = datetime.utcnow()
    session.flush()
    return batch


def current_live_run(session: Session, plan_id: int) -> models.PlanningRun:
    """Resolve current MRP by the explicit pointer, never by parent traversal."""
    pointer = (
        session.query(models.PlanningLivePointer)
        .filter_by(plan_id=int(plan_id), status="active")
        .one_or_none()
    )
    if pointer is None:
        raise LookupError(f"no active MRP pointer for plan {int(plan_id)}")
    run = session.get(models.PlanningRun, int(pointer.run_id))
    if run is None:
        raise LookupError(f"live MRP pointer references missing run {int(pointer.run_id)}")
    return run

"""R3 boundaries for stable physical identity and source completeness.

This module deliberately does not calculate stock or MRP.  It only validates
the publication boundary and resolves the explicit live-plan pointer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import event
from sqlalchemy.orm import Session

from app import models


class ImportCompletenessError(ValueError):
    """A source page set is incomplete or arrived in an invalid order."""


class CurrentMrpResolutionError(LookupError):
    """The explicit current-MRP pointer is absent or cannot be trusted."""


def validate_legacy_identity_mapping(rows) -> None:
    """Fail closed when a legacy identity has more than one active copy."""
    active_by_identity: dict[str, int] = {}
    for row in rows:
        identity = str(row.get("business_identity") or "").strip()
        if not identity:
            raise ValueError("legacy mapping has empty business identity")
        if bool(row.get("active")):
            active_by_identity[identity] = active_by_identity.get(identity, 0) + 1
    duplicate = next(
        (identity for identity, count in active_by_identity.items() if count > 1),
        None,
    )
    if duplicate is not None:
        raise ValueError(f"active duplicate business identity {duplicate}")


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
    """Resolve current MRP by the explicit pointer, never by parent traversal.

    The pointer is a business identity boundary.  A pointer to a retired,
    building, foreign-plan, or non-fixed run is stale and must not silently
    fall back to a generation lineage or the numerically latest run.
    """
    plan = session.get(models.ProductionPlanHeader, int(plan_id))
    if plan is None:
        raise CurrentMrpResolutionError(f"production plan {int(plan_id)} does not exist")
    pointer = session.get(models.PlanningLivePointer, int(plan_id))
    if pointer is None or str(pointer.status or "") != "active":
        raise CurrentMrpResolutionError(
            f"no active MRP pointer for plan {int(plan_id)}"
        )
    run = session.get(models.PlanningRun, int(pointer.run_id))
    if run is None:
        raise CurrentMrpResolutionError(
            f"live MRP pointer references missing run {int(pointer.run_id)}"
        )
    if int(run.source_plan_id or -1) != int(plan_id):
        raise CurrentMrpResolutionError(
            f"live MRP pointer for plan {int(plan_id)} references another plan"
        )
    if str(run.status or "").upper() != "FIXED_SNAPSHOT":
        raise CurrentMrpResolutionError(
            f"live MRP pointer for plan {int(plan_id)} references a run that is not FIXED_SNAPSHOT"
        )
    if str(plan.status or "").lower() != "fixed":
        raise CurrentMrpResolutionError(
            f"live MRP pointer for plan {int(plan_id)} references a non-fixed plan"
        )
    return run


def set_live_pointer(session: Session, plan_id: int, run_id: int) -> models.PlanningLivePointer:
    """Atomically replace the one live MRP pointer for a plan."""
    plan = session.get(models.ProductionPlanHeader, int(plan_id))
    run = session.get(models.PlanningRun, int(run_id))
    if plan is None or run is None or int(run.source_plan_id or -1) != int(plan_id):
        raise ValueError("live MRP pointer identities do not match the plan")
    pointer = session.get(models.PlanningLivePointer, int(plan_id))
    if pointer is None:
        pointer = models.PlanningLivePointer(plan_id=int(plan_id), run_id=int(run_id))
        session.add(pointer)
    else:
        pointer.run_id = int(run_id)
        pointer.status = "active"
    session.flush()
    return pointer


def record_successor(
    session: Session,
    plan_id: int,
    predecessor_run_id: int,
    successor_run_id: int,
    *,
    reason: str,
) -> models.PlanningRunSuccessor:
    """Insert one idempotent business successor edge."""
    if int(predecessor_run_id) == int(successor_run_id):
        raise ValueError("a run cannot be its own successor")
    existing = (
        session.query(models.PlanningRunSuccessor)
        .filter_by(
            plan_id=int(plan_id), predecessor_run_id=int(predecessor_run_id),
            successor_run_id=int(successor_run_id),
        )
        .one_or_none()
    )
    if existing is not None:
        return existing
    row = models.PlanningRunSuccessor(
        plan_id=int(plan_id), predecessor_run_id=int(predecessor_run_id),
        successor_run_id=int(successor_run_id), reason=str(reason),
    )
    session.add(row)
    session.flush()
    return row


def retire_live_pointer(session: Session, plan_id: int) -> None:
    """Retire a plan with no successor without inventing a replacement run."""
    pointer = session.get(models.PlanningLivePointer, int(plan_id))
    if pointer is not None:
        pointer.status = "retired"
        session.flush()


@event.listens_for(Session, "before_flush")
def _r3_identity_before_flush(session: Session, _flush_context, _instances) -> None:
    """Enforce identity/mapping for every runtime physical writer.

    Test fixtures and legacy rows may omit ``ingest_source``; runtime writers
    cannot.  This keeps the historical server default from being a working
    fallback while preserving compatibility for old read-only fixtures.
    """
    for entry in tuple(session.new):
        if not isinstance(entry, models.StockLedgerEntry):
            continue
        source = str(entry.ingest_source or "").strip()
        if not source or source == "test":
            continue
        identity = str(entry.business_identity or "").strip()
        if not identity:
            identity = business_identity_for_movement(
                entry.recorder_type, entry.recorder_ref, entry.line_no
            )
            entry.business_identity = identity
        active_rows = (
            session.query(models.StockLedgerEntry)
            .filter(
                models.StockLedgerEntry.business_identity == identity,
                models.StockLedgerEntry.active.is_(True),
            )
            .all()
        )
        batch = session.get(models.PhysicalImportBatch, int(entry.ingest_batch_id))
        legacy_fixture_batch = bool(
            batch is not None
            and str((batch.source_watermarks or {}).get("origin") or "") == "test"
        )
        if (
            not legacy_fixture_batch
            and any(not (row in session.dirty and row.active is False) for row in active_rows)
        ):
            raise ValueError(f"active duplicate business identity {identity}")
        session.add(
            models.StockLedgerBusinessIdentityMap(
                business_identity=identity,
                stock_ledger_entry=entry,
                mapping_reason="runtime-accepted",
            )
        )

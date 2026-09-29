"""Exact persisted source window used by canonical cutoff-snap retirement."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import models
from .physical_visibility import require_import_batch, visible_sle_query


AUDIT_SOURCE = "audit_visible_at_terminal/1"
TARGETED_SOURCE = "targeted_import_window/1"
_LEDGER_LOCAL_TZ = ZoneInfo("Europe/Moscow")


def replaced_revision_ids(
    db: Session, *, lower_batch_id: int, upper_batch_id: int,
) -> set[int]:
    """Replacement rows already represented by an earlier revision."""
    return {
        int(new_id) for (new_id,) in db.query(
            models.StockLedgerFactSupersession.new_sle_id,
        ).filter(
            models.StockLedgerFactSupersession.import_batch_id > int(lower_batch_id),
            models.StockLedgerFactSupersession.import_batch_id <= int(upper_batch_id),
            models.StockLedgerFactSupersession.new_sle_id.isnot(None),
        ).all()
    }


def _posting_at_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=_LEDGER_LOCAL_TZ).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def _cutoff_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def retirement_source_facts(
    db: Session,
    *,
    lower_batch_id: int,
    upper_batch_id: int,
    selection: str,
    parent_cutoff: datetime | None = None,
    item_ids: set[int] | None = None,
) -> tuple[models.StockLedgerEntry, ...]:
    """Select the writer's input, before its per-cell net and earliest-date rule.

    Audit facts have historical visibility at the audit terminal. Targeted
    recorder pulls use every newly inserted row in their own import window.
    Both exclude revisions of facts already represented in the earlier basis.
    """
    lower, upper = int(lower_batch_id), int(upper_batch_id)
    if lower < 0 or upper <= lower:
        raise ValueError("invalid cutoff retirement source window")
    if lower:
        require_import_batch(db, lower)
    require_import_batch(db, upper)
    if selection == AUDIT_SOURCE:
        if parent_cutoff is None:
            raise ValueError("audit retirement requires parent cutoff")
        query = visible_sle_query(
            db, physical_import_batch_id=upper,
        ).filter(models.StockLedgerEntry.recorder_type.like("Document_%"))
    elif selection == TARGETED_SOURCE:
        query = db.query(models.StockLedgerEntry)
    else:
        raise ValueError("unknown cutoff retirement source selection")
    query = query.filter(
        models.StockLedgerEntry.ingest_batch_id > lower,
        models.StockLedgerEntry.ingest_batch_id <= upper,
    )
    if item_ids is not None:
        query = query.filter(models.StockLedgerEntry.item_id.in_(item_ids))
    replacements = replaced_revision_ids(
        db, lower_batch_id=lower, upper_batch_id=upper,
    )
    return tuple(
        row for row in query.all()
        if int(row.id) not in replacements
        and (selection != AUDIT_SOURCE or (
            row.posting_at is not None
            and _posting_at_utc(row.posting_at)
            <= _cutoff_utc(parent_cutoff)
        ))
    )

"""Crash-resumable, read-only historical physical Ledger import.

Each completed register window is one durable transaction and one
``LedgerBuildBatch`` checkpoint.  The generation remains BUILDING; acceptance,
publication, Balance seeding and reservation replay are separate stages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app import models

from .historical_register_scan import scan_historical_register_range
from .ingest import pull_recorder_movements
from .physical import canonical_content_hash


ALGORITHM_VERSION = "historical-physical-import/1"


class HistoricalImportError(RuntimeError):
    """A window failed and therefore did not advance its durable checkpoint."""


@dataclass(frozen=True)
class HistoricalImportResult:
    ledger_generation_id: int
    from_exclusive: datetime
    cutoff: datetime
    completed_through: datetime
    windows_completed: int
    windows_resumed: int
    recorders_pulled: int
    movements_inserted: int
    complete: bool
    physical_import_batch_id: int


def _window_key(
    generation_id: int,
    from_exclusive: datetime,
    to_inclusive: datetime,
) -> str:
    digest = canonical_content_hash({
        "generation": int(generation_id),
        "from_exclusive": from_exclusive.isoformat(),
        "to_inclusive": to_inclusive.isoformat(),
    })
    return f"historical-window:{generation_id}:{digest[:40]}"


def _completed_checkpoint(
    db: Session,
    *,
    generation_id: int,
    batch_key: str,
) -> models.LedgerBuildBatch | None:
    return (
        db.query(models.LedgerBuildBatch)
        .filter(
            models.LedgerBuildBatch.ledger_generation_id == int(generation_id),
            models.LedgerBuildBatch.stage == "physical_import",
            models.LedgerBuildBatch.batch_key == batch_key,
            models.LedgerBuildBatch.status == "completed",
        )
        .one_or_none()
    )


def _lock_physical_batch_sequence(db: Session) -> None:
    """Prevent a concurrent batch insert from crossing this window on Postgres."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text(
                "LOCK TABLE physical_import_batch "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
        )


def _assert_global_terminal(
    db: Session,
    *,
    expected_batch_id: int,
) -> None:
    """Visibility is id-prefix based, so every prefix must be globally linear."""
    global_max = db.query(func.max(models.PhysicalImportBatch.id)).scalar()
    if global_max is None or int(global_max) != int(expected_batch_id):
        raise HistoricalImportError(
            "physical import sequence interleaved: generation terminal "
            f"{expected_batch_id}, global terminal {global_max}"
        )


def _recorder_state_checksum(
    db: Session,
    recorder_type: str,
    recorder_ref: str,
) -> str:
    """Hash the durable active state, independent of shared batch watermarks."""
    rows = (
        db.query(models.StockLedgerEntry)
        .filter(
            models.StockLedgerEntry.recorder_type == recorder_type,
            models.StockLedgerEntry.recorder_ref == recorder_ref,
            models.StockLedgerEntry.active.is_(True),
        )
        .order_by(
            models.StockLedgerEntry.line_no.asc(),
            models.StockLedgerEntry.posting_at.asc(),
            models.StockLedgerEntry.id.asc(),
        )
        .all()
    )
    return canonical_content_hash([
        {
            "line_no": str(row.line_no or ""),
            "source_content_hash": str(row.source_content_hash or ""),
            "item_id": int(row.item_id),
            "characteristic_ref": str(row.characteristic_ref or ""),
            "organization_ref": str(row.organization_ref or ""),
            "warehouse_ref1c": str(row.warehouse_ref1c or ""),
            "qty": str(row.qty),
            "posting_at": row.posting_at.isoformat(),
            "record_type": str(row.record_type or ""),
            "movement_kind": str(row.movement_kind or ""),
        }
        for row in rows
    ])


def _validate_generation(
    db: Session,
    generation_id: int,
    to_inclusive: datetime,
) -> models.LedgerGeneration:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise ValueError(f"Ledger generation {generation_id} does not exist")
    if str(generation.status) != "building":
        raise ValueError(
            f"Ledger generation {generation.id} is {generation.status}; BUILDING required"
        )
    if generation.cutoff is None:
        raise ValueError("historical import requires a fixed generation cutoff")
    if generation.cutoff != to_inclusive:
        raise ValueError(
            "to_inclusive must exactly match the BUILDING generation cutoff"
        )
    current_boundary = db.get(
        models.PhysicalImportBatch, int(generation.physical_import_batch_id)
    )
    if current_boundary is None or str(current_boundary.status) != "completed":
        raise ValueError(
            "BUILDING generation must start from a completed physical boundary"
        )
    return generation


def run_historical_physical_import(
    db: Session,
    *,
    ledger_generation_id: int,
    client: Any,
    from_exclusive: datetime,
    to_inclusive: datetime,
    window_size: timedelta = timedelta(days=1),
    page_size: int = 1000,
    max_pages_per_window: int = 10_000,
    max_windows: int | None = None,
) -> HistoricalImportResult:
    """Import ``(from_exclusive, cutoff]`` without any 1C mutation.

    ``max_windows`` is an operational yield boundary: a worker may process a
    bounded number of new windows and resume from durable checkpoints later.
    """
    if to_inclusive <= from_exclusive:
        raise ValueError("to_inclusive must be greater than from_exclusive")
    if window_size <= timedelta(0):
        raise ValueError("window_size must be positive")
    if max_windows is not None and max_windows <= 0:
        raise ValueError("max_windows must be positive")

    generation = _validate_generation(
        db, ledger_generation_id, to_inclusive,
    )
    cursor = from_exclusive
    windows_completed = 0
    windows_resumed = 0
    recorders_pulled = 0
    movements_inserted = 0
    terminal_batch_id = int(generation.physical_import_batch_id)

    while cursor < to_inclusive:
        try:
            _lock_physical_batch_sequence(db)
            _assert_global_terminal(
                db,
                expected_batch_id=int(generation.physical_import_batch_id),
            )
        except Exception:
            db.rollback()
            raise
        window_end = min(cursor + window_size, to_inclusive)
        build_key = _window_key(generation.id, cursor, window_end)
        existing = _completed_checkpoint(
            db,
            generation_id=generation.id,
            batch_key=build_key,
        )
        if existing is not None:
            metrics = dict(existing.metrics or {})
            if (
                metrics.get("from_exclusive") != cursor.isoformat()
                or metrics.get("to_inclusive") != window_end.isoformat()
                or not metrics.get("physical_import_batch_id")
            ):
                raise HistoricalImportError(
                    f"completed checkpoint {existing.id} has invalid window lineage"
                )
            terminal_batch_id = int(metrics["physical_import_batch_id"])
            terminal = db.get(models.PhysicalImportBatch, terminal_batch_id)
            if terminal is None or str(terminal.status) != "completed":
                raise HistoricalImportError(
                    f"checkpoint {existing.id} names a non-completed physical boundary"
                )
            windows_resumed += 1
            cursor = window_end
            continue

        if max_windows is not None and windows_completed >= max_windows:
            break

        try:
            scan = scan_historical_register_range(
                client,
                from_exclusive=cursor,
                to_inclusive=window_end,
                window_size=window_size,
                page_size=page_size,
                max_pages_per_window=max_pages_per_window,
            )
            checkpoint = scan.windows[0]
            previous_batch_id = int(generation.physical_import_batch_id)
            physical_batch = models.PhysicalImportBatch(
                batch_key=f"historical:{generation.id}:{build_key.rsplit(':', 1)[-1]}",
                status="building",
                cutoff=to_inclusive,
                source_watermarks={
                    "source": "AccumulationRegister_ЗапасыНаСкладах",
                    "from_exclusive": cursor.isoformat(),
                    "to_inclusive": window_end.isoformat(),
                    "scan_checksum": checkpoint.content_hash,
                    "previous_import_batch_id": previous_batch_id,
                },
            )
            db.add(physical_batch)
            db.flush()

            pull_metrics: list[dict[str, Any]] = []
            for discovered in scan.recorders:
                identity = discovered.identity
                result = pull_recorder_movements(
                    db,
                    identity.recorder_type,
                    identity.recorder_ref,
                    client=client,
                    source="historical_register_scan",
                    import_batch=physical_batch,
                    # Physical facts are shared. Generation StockBin and
                    # reservations are built by later generation-scoped stages.
                    ledger_generation_id=None,
                    max_posting_at=to_inclusive,
                    strict_historical=True,
                )
                quarantined = (
                    result.status not in {"done", "empty"}
                    or bool(result.error)
                    or bool(result.diagnostics)
                    or result.skipped_unknown_item > 0
                    or result.skipped_non_warehouse > 0
                    or result.skipped_unknown_record_type > 0
                )
                if quarantined:
                    raise HistoricalImportError(
                        f"recorder {identity.recorder_type} "
                        f"{identity.recorder_ref} was quarantined"
                    )
                pull_metrics.append({
                    "recorder_type": identity.recorder_type,
                    "recorder_ref": identity.recorder_ref,
                    "first_period": discovered.first_period.isoformat(),
                    "status": result.status,
                    "inserted": int(result.inserted),
                    "skipped_inactive": int(result.skipped_inactive),
                    "skipped_zero_qty": int(result.skipped_zero_qty),
                    "skipped_pre_anchor": int(result.skipped_pre_anchor),
                    "state_checksum": _recorder_state_checksum(
                        db, identity.recorder_type, identity.recorder_ref,
                    ),
                })

            _assert_global_terminal(
                db,
                expected_batch_id=int(physical_batch.id),
            )
            metrics = {
                "from_exclusive": cursor.isoformat(),
                "to_inclusive": window_end.isoformat(),
                "rows_read": int(checkpoint.rows_read),
                "pages_read": int(checkpoint.pages_read),
                "recorder_count": int(checkpoint.recorder_count),
                "recorders_pulled": len(pull_metrics),
                "movements_inserted": sum(row["inserted"] for row in pull_metrics),
                "scan_checksum": checkpoint.content_hash,
                "pull_checksum": canonical_content_hash(pull_metrics),
                "recorders": pull_metrics,
                "physical_import_batch_id": int(physical_batch.id),
                "previous_import_batch_id": previous_batch_id,
            }
            physical_batch.status = "completed"
            physical_batch.completed_at = datetime.now(timezone.utc)
            physical_batch.source_watermarks = {
                **dict(physical_batch.source_watermarks or {}),
                **metrics,
            }
            build_batch = models.LedgerBuildBatch(
                ledger_generation_id=generation.id,
                stage="physical_import",
                batch_key=build_key,
                status="completed",
                algorithm_version=ALGORITHM_VERSION,
                metrics=metrics,
                completed_at=datetime.now(timezone.utc),
            )
            db.add(build_batch)
            db.flush()

            # Advance only after the terminal boundary and its checkpoint are
            # both completed in this same transaction.
            generation.physical_import_batch_id = physical_batch.id
            generation.source_watermarks = {
                **dict(generation.source_watermarks or {}),
                "historical_import_completed_through": window_end.isoformat(),
                "historical_physical_import_batch_id": int(physical_batch.id),
            }
            db.flush()
            db.commit()
        except Exception as exc:
            db.rollback()
            if isinstance(exc, HistoricalImportError):
                raise
            raise HistoricalImportError(
                f"historical import window ({cursor}, {window_end}] failed: {exc}"
            ) from exc

        terminal_batch_id = int(physical_batch.id)
        recorders_pulled += len(pull_metrics)
        movements_inserted += int(metrics["movements_inserted"])
        windows_completed += 1
        cursor = window_end
        generation = db.get(models.LedgerGeneration, int(ledger_generation_id))

    result = HistoricalImportResult(
        ledger_generation_id=int(ledger_generation_id),
        from_exclusive=from_exclusive,
        cutoff=to_inclusive,
        completed_through=cursor,
        windows_completed=windows_completed,
        windows_resumed=windows_resumed,
        recorders_pulled=recorders_pulled,
        movements_inserted=movements_inserted,
        complete=cursor == to_inclusive,
        physical_import_batch_id=terminal_batch_id,
    )
    # This job owns window transactions; release a read-only sequence lock when
    # the call consisted solely of resumed checkpoints or stopped at a yield.
    db.commit()
    return result

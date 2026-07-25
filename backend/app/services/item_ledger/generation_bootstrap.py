"""Explicit, fail-closed bootstrap for a historical Ledger generation.

This module only creates local lineage and orchestrates the existing read-only
historical importer.  It never accepts or publishes a generation and never
writes to 1C.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app import models

from .historical_import_orchestration import (
    HistoricalImportResult,
    run_historical_physical_import,
)
from .physical import guard_physical_batch_writer


ALGORITHM_VERSION = "ledger-generation-bootstrap/1"


class GenerationBootstrapError(RuntimeError):
    """The requested bootstrap cannot safely reuse the persisted lineage."""


@dataclass(frozen=True)
class GenerationBootstrapResult:
    ledger_generation_id: int
    generation_key: str
    physical_import_batch_id: int
    historical_from_exclusive: datetime
    replay_from: datetime
    cutoff: datetime
    created: bool


def _utc(value: datetime, field: str) -> datetime:
    # SQLite drops timezone metadata on round-trip; persisted naive values are
    # therefore interpreted as UTC. Public inputs are checked separately.
    if value is None:
        raise GenerationBootstrapError(f"{field} is missing")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _lock_lineage(db: Session) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("LOCK TABLE ledger_generation IN SHARE ROW EXCLUSIVE MODE"))
        db.execute(text("LOCK TABLE physical_import_batch IN SHARE ROW EXCLUSIVE MODE"))
        db.execute(text("LOCK TABLE ledger_build_batch IN SHARE ROW EXCLUSIVE MODE"))


def _assert_truth_pointer_is_quiescent(db: Session) -> None:
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or pointer.current_generation_id is None:
        return
    accepted = db.get(models.LedgerGeneration, int(pointer.current_generation_id))
    if accepted is None or str(accepted.status) != "accepted":
        raise GenerationBootstrapError(
            "planning truth pointer does not name an ACCEPTED generation"
        )
    active_stage = db.query(models.LedgerBuildBatch.id).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(accepted.id),
        models.LedgerBuildBatch.status == "building",
    ).first()
    if active_stage is not None:
        raise GenerationBootstrapError(
            "accepted generation has an active mutation checkpoint"
        )


def _lineage_values(generation: models.LedgerGeneration) -> tuple[datetime, datetime]:
    watermarks = dict(generation.source_watermarks or {})
    try:
        historical_from = datetime.fromisoformat(
            str(watermarks["historical_from_exclusive"])
        )
        replay_from = datetime.fromisoformat(str(watermarks["replay_from"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise GenerationBootstrapError(
            "existing generation has incomplete bootstrap watermarks"
        ) from exc
    return _utc(historical_from, "historical_from_exclusive"), _utc(
        replay_from, "replay_from"
    )


def create_historical_generation(
    db: Session,
    *,
    generation_key: str,
    historical_from_exclusive: datetime,
    replay_from: datetime,
    cutoff: datetime,
) -> GenerationBootstrapResult:
    """Create one BUILDING generation and its immutable starting boundary.

    Repeating the same request is idempotent.  A different BUILDING generation,
    partial global import boundary, or inconsistent truth pointer fails closed.
    """
    key = str(generation_key or "").strip()
    if not key:
        raise ValueError("generation_key is required")
    for field, value in (
        ("historical_from_exclusive", historical_from_exclusive),
        ("replay_from", replay_from),
        ("cutoff", cutoff),
    ):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
    historical_from = _utc(
        historical_from_exclusive, "historical_from_exclusive"
    )
    replay_at = _utc(replay_from, "replay_from")
    cutoff_at = _utc(cutoff, "cutoff")
    if not historical_from <= replay_at <= cutoff_at:
        raise ValueError(
            "expected historical_from_exclusive <= replay_from <= cutoff"
        )

    try:
        _lock_lineage(db)
        _assert_truth_pointer_is_quiescent(db)
        existing = db.query(models.LedgerGeneration).filter(
            models.LedgerGeneration.generation_key == key
        ).one_or_none()
        if existing is not None:
            existing_from, existing_replay = _lineage_values(existing)
            if (
                existing_from != historical_from
                or existing_replay != replay_at
                or _utc(existing.cutoff, "cutoff") != cutoff_at
                or str(existing.algorithm_version) != ALGORITHM_VERSION
                or str(existing.status) != "building"
            ):
                raise GenerationBootstrapError(
                    "generation_key already exists with different or non-BUILDING lineage"
                )
            boundary = db.get(
                models.PhysicalImportBatch,
                int(existing.physical_import_batch_id),
            )
            if boundary is None or str(boundary.status) != "completed":
                raise GenerationBootstrapError(
                    "existing generation points to an incomplete physical boundary"
                )
            incomplete_checkpoint = db.query(models.LedgerBuildBatch.id).filter(
                models.LedgerBuildBatch.ledger_generation_id == int(existing.id),
                models.LedgerBuildBatch.status != "completed",
            ).first()
            if incomplete_checkpoint is not None:
                raise GenerationBootstrapError(
                    "existing generation has an incomplete build checkpoint"
                )
            global_terminal = db.query(
                func.max(models.PhysicalImportBatch.id)
            ).scalar()
            if (
                global_terminal is None
                or int(global_terminal) != int(boundary.id)
            ):
                raise GenerationBootstrapError(
                    "physical import sequence interleaved after generation boundary"
                )
            db.commit()
            return GenerationBootstrapResult(
                ledger_generation_id=int(existing.id),
                generation_key=key,
                physical_import_batch_id=int(boundary.id),
                historical_from_exclusive=historical_from,
                replay_from=replay_at,
                cutoff=cutoff_at,
                created=False,
            )

        other_build = db.query(models.LedgerGeneration.id).filter(
            models.LedgerGeneration.status == "building"
        ).first()
        if other_build is not None:
            raise GenerationBootstrapError(
                f"BUILDING generation {int(other_build[0])} already exists"
            )
        incomplete_physical = db.query(models.PhysicalImportBatch.id).filter(
            models.PhysicalImportBatch.status == "building"
        ).first()
        if incomplete_physical is not None:
            raise GenerationBootstrapError(
                f"physical import batch {int(incomplete_physical[0])} is incomplete"
            )
        incomplete_stage = db.query(models.LedgerBuildBatch.id).filter(
            models.LedgerBuildBatch.status == "building"
        ).first()
        if incomplete_stage is not None:
            raise GenerationBootstrapError(
                f"Ledger build checkpoint {int(incomplete_stage[0])} is incomplete"
            )

        guard_physical_batch_writer(db)
        boundary = models.PhysicalImportBatch(
            batch_key=f"bootstrap:{key}",
            status="completed",
            cutoff=historical_from,
            source_watermarks={
                "source": "historical-bootstrap-boundary",
                "generation_key": key,
                "historical_from_exclusive": historical_from.isoformat(),
            },
            completed_at=datetime.now(timezone.utc),
        )
        db.add(boundary)
        db.flush()
        generation = models.LedgerGeneration(
            generation_key=key,
            status="building",
            cutoff=cutoff_at,
            source_watermarks={
                "historical_from_exclusive": historical_from.isoformat(),
                "replay_from": replay_at.isoformat(),
                "bootstrap_physical_import_batch_id": int(boundary.id),
            },
            capabilities={},
            physical_import_batch_id=int(boundary.id),
            algorithm_version=ALGORITHM_VERSION,
        )
        db.add(generation)
        db.flush()
        if int(db.query(func.max(models.PhysicalImportBatch.id)).scalar()) != int(
            boundary.id
        ):
            raise GenerationBootstrapError("physical import boundary interleaved")
        db.commit()
    except Exception:
        db.rollback()
        raise

    return GenerationBootstrapResult(
        ledger_generation_id=int(generation.id),
        generation_key=key,
        physical_import_batch_id=int(boundary.id),
        historical_from_exclusive=historical_from,
        replay_from=replay_at,
        cutoff=cutoff_at,
        created=True,
    )


def resume_historical_generation_import(
    db: Session,
    *,
    ledger_generation_id: int,
    client: Any,
    window_size: timedelta = timedelta(days=1),
    page_size: int = 1000,
    max_pages_per_window: int = 10_000,
    max_windows: int | None = None,
) -> HistoricalImportResult:
    """Resume only the exact persisted historical range of a BUILDING generation."""
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None or str(generation.status) != "building":
        raise GenerationBootstrapError("historical import requires a BUILDING generation")
    historical_from, _replay_from = _lineage_values(generation)
    if generation.cutoff is None:
        raise GenerationBootstrapError("BUILDING generation has no cutoff")
    _assert_truth_pointer_is_quiescent(db)
    return run_historical_physical_import(
        db,
        ledger_generation_id=int(generation.id),
        client=client,
        from_exclusive=historical_from,
        to_inclusive=_utc(generation.cutoff, "cutoff"),
        window_size=window_size,
        page_size=page_size,
        max_pages_per_window=max_pages_per_window,
        max_windows=max_windows,
    )


def historical_generation_status(
    db: Session, ledger_generation_id: int
) -> dict[str, Any]:
    """Read persisted bootstrap/checkpoint state without calculating facts."""
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise GenerationBootstrapError(
            f"LedgerGeneration {ledger_generation_id} not found"
        )
    historical_from, replay_from = _lineage_values(generation)
    checkpoints = (
        db.query(models.LedgerBuildBatch)
        .filter(
            models.LedgerBuildBatch.ledger_generation_id == int(generation.id),
            models.LedgerBuildBatch.stage == "physical_import",
        )
        .order_by(models.LedgerBuildBatch.id.asc())
        .all()
    )
    completed_through = (generation.source_watermarks or {}).get(
        "historical_import_completed_through"
    )
    return {
        "ledger_generation_id": int(generation.id),
        "generation_key": str(generation.generation_key),
        "status": str(generation.status),
        "historical_from_exclusive": historical_from.isoformat(),
        "replay_from": replay_from.isoformat(),
        "cutoff": _utc(generation.cutoff, "cutoff").isoformat(),
        "physical_import_batch_id": int(generation.physical_import_batch_id),
        "completed_through": completed_through,
        "physical_checkpoints": len(checkpoints),
        "completed_checkpoints": sum(
            1 for row in checkpoints if str(row.status) == "completed"
        ),
        "has_incomplete_checkpoint": any(
            str(row.status) != "completed" for row in checkpoints
        ),
    }

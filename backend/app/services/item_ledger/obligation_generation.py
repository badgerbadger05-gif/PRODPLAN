"""Create an unpublished generation which reuses accepted physical truth.

An obligation refresh deliberately forks only the immutable physical prefix.
It does not copy reservations, future supply, or any planning result.  The
caller is responsible for serialising this operation with any other generation
lifecycle work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app import models

from .generation_lifecycle import materialize_generation_stock_bins


ALGORITHM_VERSION = "ledger-obligation-generation/1"
REPLAY_VERSION = "ledger-obligation-replay/1"
GENERATION_KIND = "obligation_refresh"


class ObligationGenerationError(RuntimeError):
    """The requested obligation-generation lineage is not safe to create."""


@dataclass(frozen=True)
class ObligationGenerationResult:
    ledger_generation_id: int
    generation_key: str
    physical_import_batch_id: int
    cutoff: datetime
    created: bool


def _utc(value: datetime | None, field: str) -> datetime:
    if value is None:
        raise ObligationGenerationError(f"{field} is missing")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _expected_watermarks(parent_id: int) -> dict[str, Any]:
    return {
        "parent_generation_id": int(parent_id),
        "generation_kind": GENERATION_KIND,
    }


def _checkpoint_key(generation_key: str) -> str:
    return f"obligation-refresh:{generation_key}"


def _reused_metrics(
    parent: models.LedgerGeneration,
    physical: models.PhysicalImportBatch,
) -> dict[str, Any]:
    """Record the exact reused boundary rather than manufacturing an import."""
    return {
        "reused": True,
        "parent_generation_id": int(parent.id),
        "physical_import_batch_id": int(physical.id),
        "physical_batch_key": str(physical.batch_key),
        "physical_batch_metrics": dict(physical.source_watermarks or {}),
    }


def _require_current_accepted_parent(
    db: Session, parent_generation_id: int
) -> tuple[models.LedgerGeneration, models.PhysicalImportBatch]:
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    if parent is None or str(parent.status) != "accepted":
        raise ObligationGenerationError("parent generation must be ACCEPTED")
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or pointer.current_generation_id is None:
        raise ObligationGenerationError("planning truth pointer is not set")
    if int(pointer.current_generation_id) != int(parent.id):
        raise ObligationGenerationError(
            "parent generation is not the current planning truth pointer"
        )
    if parent.cutoff is None or parent.physical_import_batch_id is None:
        raise ObligationGenerationError("accepted parent has incomplete physical lineage")
    physical = db.get(
        models.PhysicalImportBatch, int(parent.physical_import_batch_id)
    )
    if physical is None or str(physical.status) != "completed":
        raise ObligationGenerationError("parent physical import batch is not completed")
    return parent, physical


def _exact_existing(
    db: Session,
    existing: models.LedgerGeneration,
    *,
    parent: models.LedgerGeneration,
    physical: models.PhysicalImportBatch,
    key: str,
) -> None:
    if (
        str(existing.status) != "building"
        or existing.physical_import_batch_id != physical.id
        or _utc(existing.cutoff, "existing cutoff") != _utc(parent.cutoff, "parent cutoff")
        or dict(existing.source_watermarks or {}) != _expected_watermarks(parent.id)
        or dict(existing.capabilities or {}) != {}
        or str(existing.algorithm_version) != ALGORITHM_VERSION
        or str(existing.replay_version) != REPLAY_VERSION
    ):
        raise ObligationGenerationError(
            "generation_key already exists with different or non-BUILDING obligation lineage"
        )
    checkpoints = db.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(existing.id),
        models.LedgerBuildBatch.stage == "physical_import",
    ).all()
    expected_metrics = _reused_metrics(parent, physical)
    if len(checkpoints) != 1:
        raise ObligationGenerationError("existing obligation generation lacks one physical checkpoint")
    checkpoint = checkpoints[0]
    if (
        str(checkpoint.status) != "completed"
        or str(checkpoint.batch_key) != _checkpoint_key(key)
        or str(checkpoint.algorithm_version) != ALGORITHM_VERSION
        or dict(checkpoint.metrics or {}) != expected_metrics
    ):
        raise ObligationGenerationError("existing obligation checkpoint conflicts")


def fork_obligation_generation(
    db: Session,
    parent_generation_id: int,
    generation_key: str,
) -> ObligationGenerationResult:
    """Fork a BUILDING obligation candidate from the current accepted prefix.

    This helper deliberately takes no PostgreSQL lock and owns no transaction:
    its lifecycle caller serialises the operation and atomically commits (or
    rolls back) this candidate together with the remaining planning snapshot.
    """
    key = str(generation_key or "").strip()
    if not key:
        raise ValueError("generation_key is required")
    if len(_checkpoint_key(key)) > 128:
        raise ValueError("generation_key is too long")

    parent, physical = _require_current_accepted_parent(db, parent_generation_id)
    existing = db.query(models.LedgerGeneration).filter(
        models.LedgerGeneration.generation_key == key
    ).one_or_none()
    if existing is not None:
        _exact_existing(db, existing, parent=parent, physical=physical, key=key)
        return ObligationGenerationResult(
            ledger_generation_id=int(existing.id),
            generation_key=key,
            physical_import_batch_id=int(physical.id),
            cutoff=_utc(parent.cutoff, "parent cutoff"),
            created=False,
        )

    candidate = models.LedgerGeneration(
        generation_key=key,
        status="building",
        cutoff=parent.cutoff,
        source_watermarks=_expected_watermarks(parent.id),
        capabilities={},
        physical_import_batch_id=int(physical.id),
        algorithm_version=ALGORITHM_VERSION,
        replay_version=REPLAY_VERSION,
    )
    db.add(candidate)
    db.flush()
    materialize_generation_stock_bins(db, int(candidate.id))
    db.add(models.LedgerBuildBatch(
        ledger_generation_id=int(candidate.id),
        stage="physical_import",
        batch_key=_checkpoint_key(key),
        status="completed",
        algorithm_version=ALGORITHM_VERSION,
        metrics=_reused_metrics(parent, physical),
        completed_at=datetime.now(timezone.utc),
    ))
    db.flush()

    return ObligationGenerationResult(
        ledger_generation_id=int(candidate.id),
        generation_key=key,
        physical_import_batch_id=int(physical.id),
        cutoff=_utc(parent.cutoff, "parent cutoff"),
        created=True,
    )

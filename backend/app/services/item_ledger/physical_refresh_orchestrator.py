"""Crash-resumable physical refresh followed by one atomic planning publish."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Lock
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.services.obligation_refresh_orchestrator import run_obligation_refresh

from .generation_lifecycle import accept_generation_build
from .historical_bootstrap_phase0 import (
    BalanceConvergenceResult,
    evaluate_physical_refresh_balance_convergence,
)
from .historical_import_orchestration import (
    HistoricalImportResult,
    run_historical_physical_import,
)
from .physical import PHYSICAL_SEQUENCE_LOCK_KEY, physical_sequence_lock_context
from .physical_refresh_import import (
    PhysicalRefreshImportResult,
    run_physical_recorder_audit,
)
from .physical_refresh_generation import fork_physical_refresh_generation


PHYSICAL_REFRESH_LOCK_KEY = PHYSICAL_SEQUENCE_LOCK_KEY
_sqlite_lock = Lock()


class PhysicalRefreshOrchestratorError(RuntimeError):
    """A complete fresh truth could not be proved and was not published."""


@dataclass(frozen=True)
class PhysicalRefreshOrchestrationResult:
    parent_generation_id: int
    physical_generation_id: int
    published_generation_id: int
    cutoff: datetime
    physical_import: HistoricalImportResult
    recorder_audit: PhysicalRefreshImportResult
    balance_convergence: BalanceConvergenceResult
    candidate_run_ids: tuple[int, ...]
    published: bool


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _obligation_key(key: str) -> str:
    digest = sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"physical-publish:{digest}"


def _acquire_lifecycle_lock(db: Session):
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return _sqlite_lock.acquire(blocking=False)
    connection = None
    try:
        # Keep the session-level advisory lock on a dedicated connection: the
        # work Session commits throughout the lifecycle and may otherwise
        # return its pooled connection, silently releasing the lock.
        connection = bind.connect()
        row = connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": PHYSICAL_REFRESH_LOCK_KEY},
        ).fetchone()
        connection.commit()
        if not row or not row[0]:
            connection.close()
            return False
        return connection
    except Exception:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        raise


def _release_lifecycle_lock(lock) -> None:
    if lock is False or lock is None:
        return
    if lock is True:
        if _sqlite_lock.locked():
            _sqlite_lock.release()
        return
    try:
        lock.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": PHYSICAL_REFRESH_LOCK_KEY},
        )
        lock.commit()
    except Exception:
        try:
            lock.rollback()
        except Exception:
            pass
    finally:
        try:
            lock.close()
        except Exception:
            pass


def _current_parent(db: Session) -> models.LedgerGeneration:
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or pointer.current_generation_id is None:
        raise PhysicalRefreshOrchestratorError(
            "planning truth pointer is unavailable"
        )
    parent = db.get(models.LedgerGeneration, int(pointer.current_generation_id))
    if parent is None or str(parent.status) != "accepted" or parent.cutoff is None:
        raise PhysicalRefreshOrchestratorError(
            "current planning truth is not an accepted generation"
        )
    return parent


def run_physical_refresh(
    db: Session,
    *,
    generation_key: str,
    target_cutoff: datetime,
    client: Any,
    balance_snapshot: Mapping[Any, Any],
    started_by: str = "auto-sync",
    window_size: timedelta = timedelta(days=1),
    max_windows: int | None = None,
    config_version_id: int | None = None,
    config_snapshot: Mapping[str, Any] | None = None,
    planning_pool_by_warehouse: Mapping[str, str] | None = None,
) -> PhysicalRefreshOrchestrationResult:
    """Advance physical truth and publish refreshed planning snapshots.

    Window imports are durable checkpoints.  The accepted pointer remains on
    the parent until physical validation, full replay, and the existing
    obligation-refresh publisher all succeed.
    """
    key = str(generation_key or "").strip()
    if not key:
        raise ValueError("generation_key is required")
    cutoff = _utc(target_cutoff, "target_cutoff")
    lifecycle_lock = _acquire_lifecycle_lock(db)
    if not lifecycle_lock:
        raise PhysicalRefreshOrchestratorError(
            "another physical refresh is running"
        )
    try:
        lock_context = physical_sequence_lock_context()
        lock_context.__enter__()
        parent = _current_parent(db)
        from_cutoff = _utc(parent.cutoff, "parent cutoff")
        fork = fork_physical_refresh_generation(
            db,
            int(parent.id),
            key,
            from_cutoff=from_cutoff,
            target_cutoff=cutoff,
        )
        recorder_audit = run_physical_recorder_audit(
            db,
            ledger_generation_id=int(fork.ledger_generation_id),
            parent_generation_id=int(parent.id),
            client=client,
        )
        physical_import = run_historical_physical_import(
            db,
            ledger_generation_id=int(fork.ledger_generation_id),
            client=client,
            from_exclusive=from_cutoff,
            to_inclusive=cutoff,
            window_size=window_size,
            max_windows=max_windows,
        )
        if not physical_import.complete:
            raise PhysicalRefreshOrchestratorError(
                "physical refresh yielded before reaching target cutoff"
            )

        convergence = evaluate_physical_refresh_balance_convergence(
            db,
            ledger_generation_id=int(fork.ledger_generation_id),
            balance_snapshot=balance_snapshot,
        )
        # Retain the diagnostic and completed import even when convergence is
        # false; neither operation moves the public planning-truth pointer.
        db.commit()
        if not convergence.valid:
            raise PhysicalRefreshOrchestratorError(
                f"Balance convergence failed: {convergence.mismatched} mismatches"
            )

        physical_generation = db.get(
            models.LedgerGeneration, int(fork.ledger_generation_id)
        )
        if physical_generation is None:
            raise PhysicalRefreshOrchestratorError(
                "physical refresh generation disappeared"
            )
        marks = dict(physical_generation.source_watermarks or {})
        try:
            replay_from = datetime.fromisoformat(str(marks["replay_from"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PhysicalRefreshOrchestratorError(
                "physical refresh lacks replay_from"
            ) from exc

        # This pointer transition and the following obligation refresh share
        # one outer transaction.  Other sessions therefore see either the old
        # complete truth or the final complete truth, never the intermediate.
        accept_generation_build(
            db,
            int(physical_generation.id),
            replay_from=replay_from,
            odata_client=client,
        )
        published = run_obligation_refresh(
            db,
            parent_generation_id=int(physical_generation.id),
            generation_key=_obligation_key(key),
            started_by=started_by,
            config_version_id=config_version_id,
            config_snapshot=dict(config_snapshot or {}),
            planning_pool_by_warehouse=dict(planning_pool_by_warehouse or {}),
        )
        db.commit()
        return PhysicalRefreshOrchestrationResult(
            parent_generation_id=int(parent.id),
            physical_generation_id=int(physical_generation.id),
            published_generation_id=int(published.target_generation_id),
            cutoff=cutoff,
            physical_import=physical_import,
            recorder_audit=recorder_audit,
            balance_convergence=convergence,
            candidate_run_ids=tuple(published.candidate_run_ids),
            published=bool(published.published),
        )
    except Exception:
        db.rollback()
        raise
    finally:
        if "lock_context" in locals():
            lock_context.__exit__(None, None, None)
        _release_lifecycle_lock(lifecycle_lock)

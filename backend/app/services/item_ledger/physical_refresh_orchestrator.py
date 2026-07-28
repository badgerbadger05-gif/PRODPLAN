"""Crash-resumable physical refresh followed by one atomic planning publish."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from typing import Any, Callable, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models

from ..mrp_result_snapshot import build_mrp_result_snapshot
from ..purchase_control_snapshot import (
    promote_candidate_snapshot as promote_purchase_journal_candidate,
)
from .generation_lifecycle import accept_generation_build
from .historical_bootstrap_phase0 import (
    BalanceConvergenceResult,
    evaluate_physical_refresh_balance_convergence,
)
from .historical_import_orchestration import (
    HistoricalImportResult,
    run_historical_physical_import,
)
from .opening_balance_reconcile import (
    OpeningBalanceReconcileResult,
    opening_boundary,
    reconcile_opening_balance,
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
    opening_reconcile: OpeningBalanceReconcileResult | None = None


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


def _reconcile_opening_balance(
    db: Session,
    *,
    ledger_generation_id: int,
    loader: Callable[[datetime], Mapping[Any, Any]] | None,
) -> OpeningBalanceReconcileResult | None:
    """Re-align the T0 prefix with 1C before the forward window import."""
    if loader is None:
        return None
    boundary = opening_boundary(db)
    if boundary is None:
        return None
    _batch, opening_at = boundary
    result = reconcile_opening_balance(
        db,
        ledger_generation_id=int(ledger_generation_id),
        opening_snapshot=loader(opening_at),
    )
    db.commit()
    return result


def _publish_refresh_read_snapshots(
    db: Session,
    *,
    generation: models.LedgerGeneration,
    fixed_run_ids: tuple[int, ...],
) -> None:
    """Finish the read surface of a just-accepted physical refresh.

    Publishes only what this generation owns: its own purchase-journal
    candidate, and MRP result snapshots for runs already bound to it.  Runs
    frozen against older truth stay unpublished here on purpose — rebinding
    them belongs to the obligation refresh, and quietly rebuilding them against
    fresh facts would contradict the frozen obligations they encode.

    Fails closed when the generation advertises a capability whose snapshot did
    not materialize: a silently missing snapshot reads to the operator as an
    outage of the whole screen, which is exactly how this gap survived.
    """
    capabilities = dict(generation.capabilities or {})
    accepted_at = generation.accepted_at or datetime.now(timezone.utc)

    promoted = promote_purchase_journal_candidate(
        db, generation=generation, accepted_at=accepted_at
    )
    if promoted is None and capabilities.get("purchase_control_journal"):
        raise PhysicalRefreshOrchestratorError(
            f"generation {generation.id} claims the purchase_control_journal "
            "capability but has no journal candidate to publish"
        )

    for run_id in fixed_run_ids:
        run = db.get(models.PlanningRun, int(run_id))
        if run is None:
            continue
        # A physical refresh does not rebind frozen runs — that is the
        # obligation refresh's job — so most fixed runs still belong to older
        # truth and have no snapshot to publish here. Only the ones this
        # generation actually owns are ours to rebuild.
        if (
            int(run.ledger_generation_id or 0) != int(generation.id)
            or run.ledger_cutoff != generation.cutoff
        ):
            continue
        try:
            build_mrp_result_snapshot(db, int(run_id))
        except ValueError as exc:
            raise PhysicalRefreshOrchestratorError(
                f"MRP result snapshot for run {run_id} could not be published: {exc}"
            ) from exc
    db.flush()


_MISMATCH_SAMPLE = 5


def _mismatch_digest(convergence: BalanceConvergenceResult) -> str:
    """Name the worst offenders so a failed refresh is actionable on sight.

    Without this the operator learns only a count and has to reconstruct the
    register diff by hand against a three-hour run.
    """
    mismatched = [delta for delta in convergence.deltas if not delta.matched]
    mismatched.sort(key=lambda delta: abs(Decimal(delta.delta_qty)), reverse=True)
    sample = ", ".join(
        f"item={delta.item_id} wh={delta.warehouse_ref1c} "
        f"ledger={delta.ledger_qty} 1c={delta.balance_qty} delta={delta.delta_qty}"
        for delta in mismatched[:_MISMATCH_SAMPLE]
    )
    surplus = len(mismatched) - _MISMATCH_SAMPLE
    if surplus > 0:
        sample = f"{sample}, +{surplus} more"
    return f"worst: {sample}" if sample else "no deltas retained"


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
    discovery_lookback: timedelta | None = None,
    opening_balance_loader: Callable[[datetime], Mapping[Any, Any]] | None = None,
    config_version_id: int | None = None,
    config_snapshot: Mapping[str, Any] | None = None,
    planning_pool_by_warehouse: Mapping[str, str] | None = None,
) -> PhysicalRefreshOrchestrationResult:
    """Advance physical truth and publish refreshed planning snapshots.

    Window imports are durable checkpoints.  The accepted pointer remains on
    the parent until physical validation, full replay, and the existing
    obligation-refresh publisher all succeed.

    ``opening_balance_loader`` is called with the anchor instant and must return
    1C's Balance as of it.  Without it the opening balance is left as seeded,
    which leaves documents backdated behind the anchor permanently unaccounted.
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
            discovery_lookback=discovery_lookback,
        )
        # Between the audit and the forward import: the audit requires the
        # generation to still sit on the parent boundary, and the forward import
        # starts from whatever boundary this leaves behind.
        opening_reconcile = _reconcile_opening_balance(
            db,
            ledger_generation_id=int(fork.ledger_generation_id),
            loader=opening_balance_loader,
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
                f" ({_mismatch_digest(convergence)})"
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

        # Physical refresh changes facts, not frozen plan obligations.
        # accept_generation_build rebuilds the generation-scoped reservation
        # fold and immutable read snapshots for the existing fixed runs.  A
        # second obligation refresh here used to re-explode every BOM and
        # overwrite frozen net quantities with today's stock basis.
        # The pointer is re-checked here, not only at the fork: the import
        # window runs for minutes and commits repeatedly, so another publisher
        # may have advanced truth in the meantime.  accept_generation_build
        # compares-and-sets under the pointer row lock.
        accept_generation_build(
            db,
            int(physical_generation.id),
            replay_from=replay_from,
            odata_client=client,
            expected_parent_id=int(parent.id),
        )
        fixed_run_ids = tuple(
            int(run_id)
            for (run_id,) in db.query(models.PlanningRun.run_id)
            .filter(models.PlanningRun.status == "FIXED_SNAPSHOT")
            .order_by(models.PlanningRun.run_id.asc())
            .all()
        )
        # Accepting a generation rebuilds the read snapshots that carry their own
        # accepted status, but not these two: the purchase journal is only ever
        # written as a candidate, and MRP result snapshots are per fixed run.
        # Both promotions live in the obligation refresh publisher, which this
        # path deliberately does not run, so without this every physical refresh
        # leaves the purchase and MRP screens unavailable.
        _publish_refresh_read_snapshots(
            db,
            generation=physical_generation,
            fixed_run_ids=fixed_run_ids,
        )
        db.commit()
        return PhysicalRefreshOrchestrationResult(
            parent_generation_id=int(parent.id),
            physical_generation_id=int(physical_generation.id),
            published_generation_id=int(physical_generation.id),
            cutoff=cutoff,
            physical_import=physical_import,
            recorder_audit=recorder_audit,
            balance_convergence=convergence,
            candidate_run_ids=fixed_run_ids,
            published=True,
            opening_reconcile=opening_reconcile,
        )
    except Exception:
        db.rollback()
        raise
    finally:
        if "lock_context" in locals():
            lock_context.__exit__(None, None, None)
        _release_lifecycle_lock(lifecycle_lock)

"""Crash-resumable physical refresh followed by one atomic planning publish."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app import models
from ..planning_pool_resolver import (
    effective_planning_pool_by_warehouse,
    validate_future_supply_destinations,
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
    ADJUSTMENT_RECORDER_TYPE,
    OpeningBalanceReconcileResult,
    opening_boundary,
    reconcile_opening_balance,
)
from .physical import (
    CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE,
    LedgerKey,
    PHYSICAL_SEQUENCE_LOCK_KEY,
    SEED_RECORDER_TYPE,
    canonical_content_hash,
    guard_physical_batch_writer,
    physical_sequence_lock_context,
    rebuild_running_balance,
)
from .physical_visibility import visible_sle_query
from .ingest import HistoricalPullBeyondCutoffError, pull_recorder_movements
from .physical_refresh_import import (
    PhysicalRefreshImportResult,
    run_physical_recorder_audit,
)
from .physical_refresh_generation import fork_physical_refresh_generation


PHYSICAL_REFRESH_LOCK_KEY = PHYSICAL_SEQUENCE_LOCK_KEY
_sqlite_lock = Lock()


class PhysicalRefreshOrchestratorError(RuntimeError):
    """A complete fresh truth could not be proved and was not published."""


class PhysicalRefreshBalanceConvergenceError(PhysicalRefreshOrchestratorError):
    """A persisted candidate no longer matches the 1C balance at its cutoff."""

    def __init__(self, ledger_generation_id: int, message: str):
        self.ledger_generation_id = int(ledger_generation_id)
        super().__init__(message)


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
    only_keys: set[tuple[int, str, str]] | None = None,
) -> OpeningBalanceReconcileResult | None:
    """Re-align selected T0 keys with 1C without replaying history."""
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
        only_keys=only_keys,
    )
    db.commit()
    return result


_MISMATCH_SAMPLE = 5
_TARGETED_REPAIR_PAGE_SIZE = 1000
_TARGETED_REPAIR_MAX_RECORDERS = 500


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


def _odata_local(value: datetime) -> str:
    return _utc(value, "OData datetime").astimezone(
        ZoneInfo("Europe/Moscow")
    ).replace(tzinfo=None, microsecond=0).isoformat()


def _normalized_ref(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.startswith("guid'") and raw.endswith("'"):
        raw = raw[5:-1]
    return raw.strip("{}").strip()


def _normalized_recorder_type(value: Any) -> str:
    raw = str(value or "").strip()
    prefix = "StandardODATA."
    return raw[len(prefix):] if raw.startswith(prefix) else raw


# A wholesale divergence means the physical import itself failed; snapping the
# entire inventory to Balance would silently paper over it. Refuse above this
# fraction of the compared cells instead. Normal backdated drift is a tiny
# fraction (hundreds of cells out of ~14k), so this never blocks the steady
# state — it only guards against masking a broken import.
_MAX_SNAP_FRACTION = 0.5


def _snap_balance_at_cutoff(
    db: Session,
    *,
    generation: models.LedgerGeneration,
    convergence: BalanceConvergenceResult,
) -> int:
    """Close the residual by snapping the ledger to 1C's Balance at the cutoff.

    1C lets documents be re-posted behind an already-frozen cutoff (backdating),
    so an append-only movement replay can never stay converged: chasing every
    changed recorder is unbounded and eventually exceeds any safety budget.
    1C's Balance register, by contrast, is always self-consistent and already
    reflects every backdated change. This writes one synthetic adjustment
    movement per mismatched cell (``delta = 1C balance - ledger``), dated at the
    generation cutoff, so the ledger's present balance matches 1C again. It is
    bounded by the number of mismatched cells, not by recorder fan-out, and
    converges even against 1C's own negative balances (they are mirrored).

    Planning-relevant flows (production output, supplier receipts, order-linked
    consumption) are still imported as real movements upstream; only the
    residual left by untracked or backdated adjustments is absorbed here. Each
    generation recomputes the residual from scratch, so a later re-import of a
    document this snap already absorbed is self-correcting (the next residual
    swings the other way and is re-snapped) — the level stays right.
    """
    bad = [delta for delta in convergence.deltas if not delta.matched]
    if not bad:
        return 0
    if convergence.compared and len(bad) > convergence.compared * _MAX_SNAP_FRACTION:
        raise PhysicalRefreshOrchestratorError(
            f"balance snap refused: {len(bad)} of {convergence.compared} cells "
            "diverge (>50%); the physical import likely failed and a snap would "
            "mask it"
        )

    cutoff = _utc(generation.cutoff, "generation cutoff")
    content_hash = canonical_content_hash(
        [
            [int(d.item_id), str(d.organization_ref), str(d.warehouse_ref1c), str(d.balance_qty)]
            for d in bad
        ]
    )
    guard_physical_batch_writer(db)
    batch = models.PhysicalImportBatch(
        batch_key=f"cutoff-balance-snap:{int(generation.id)}:{content_hash[:40]}",
        status="completed",
        cutoff=generation.cutoff,
        completed_at=datetime.now(timezone.utc),
        source_watermarks={
            "source": CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE,
            "generation_id": int(generation.id),
            "adjusted_keys": len(bad),
            "content_hash": content_hash,
            "previous_import_batch_id": int(generation.physical_import_batch_id),
        },
    )
    db.add(batch)
    db.flush()

    for delta in bad:
        key = LedgerKey(
            int(delta.item_id),
            "",
            str(delta.organization_ref or ""),
            str(delta.warehouse_ref1c or ""),
        )
        qty = Decimal(delta.balance_qty) - Decimal(delta.ledger_qty)
        recorder_ref = canonical_content_hash(
            {
                "generation_id": int(generation.id),
                "item_id": key.item_id,
                "organization_ref": key.organization_ref,
                "warehouse_ref1c": key.warehouse_ref1c,
            }
        )[:40]
        db.add(
            models.StockLedgerEntry(
                ingest_batch_id=int(batch.id),
                source_content_hash=recorder_ref,
                item_id=key.item_id,
                characteristic_ref="",
                organization_ref=key.organization_ref,
                warehouse_ref1c=key.warehouse_ref1c,
                qty=qty,
                posting_at=generation.cutoff,
                record_type="Receipt" if qty > 0 else "Expense",
                movement_kind=CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE,
                recorder_type=CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE,
                recorder_ref=recorder_ref,
                line_no="0",
                ingest_source=CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE,
            )
        )
        db.flush()
        rebuild_running_balance(db, key, ledger_generation_id=int(generation.id))

    generation.physical_import_batch_id = int(batch.id)
    generation.source_watermarks = {
        **dict(generation.source_watermarks or {}),
        "cutoff_balance_snap": {
            "adjusted_keys": len(bad),
            "cutoff": cutoff.isoformat(),
            "content_hash": content_hash,
        },
    }
    db.flush()
    return len(bad)


def _repair_mismatched_recorders(
    db: Session,
    *,
    generation: models.LedgerGeneration,
    client: Any,
    convergence: BalanceConvergenceResult,
) -> int:
    """Re-pull only recorders touching mismatched balance keys.

    This is the bounded fallback for a known 1C document that was re-posted
    behind the accepted cutoff without changing its line count.  It queries
    the flat register by the mismatched item, narrows rows by organisation and
    warehouse, and never scans or pulls unrelated recorder contents.
    """
    mismatch_keys = {
        (int(delta.item_id), str(delta.organization_ref), str(delta.warehouse_ref1c))
        for delta in convergence.deltas
        if not delta.matched
    }
    if not mismatch_keys:
        return 0

    item_rows = db.query(models.Item.item_id, models.Item.item_ref1c).filter(
        models.Item.item_id.in_({key[0] for key in mismatch_keys})
    ).all()
    ref_by_item = {
        int(item_id): _normalized_ref(item_ref)
        for item_id, item_ref in item_rows
        if _normalized_ref(item_ref)
    }
    opening = opening_boundary(db)
    if opening is None:
        raise PhysicalRefreshOrchestratorError(
            "targeted convergence repair requires opening boundary"
        )
    opening_at = _utc(opening[1], "opening boundary")
    cutoff = _utc(generation.cutoff, "generation cutoff")
    identities: set[tuple[str, str]] = set()

    # A cancelled/unposted recorder disappears from 1C's current register, so
    # the register-side lookup below cannot discover it.  Seed the repair set
    # with recorder identities already contributing to each mismatched Ledger
    # cell; re-pulling a vanished recorder produces the tombstone that removes
    # its obsolete movements.
    known_rows = visible_sle_query(
        db,
        physical_import_batch_id=int(generation.physical_import_batch_id),
        cutoff=generation.cutoff,
    ).filter(
        models.StockLedgerEntry.item_id.in_({key[0] for key in mismatch_keys}),
    ).all()
    synthetic_types = {SEED_RECORDER_TYPE, ADJUSTMENT_RECORDER_TYPE}
    for row in known_rows:
        key = (
            int(row.item_id),
            _normalized_ref(row.organization_ref),
            _normalized_ref(row.warehouse_ref1c),
        )
        recorder_type = _normalized_recorder_type(row.recorder_type)
        recorder_ref = _normalized_ref(row.recorder_ref)
        if (
            key in mismatch_keys
            and recorder_type not in synthetic_types
            and recorder_type
            and recorder_ref
        ):
            identities.add((recorder_type, recorder_ref))

    for item_id, item_ref in sorted(ref_by_item.items()):
        wanted = {
            (org, warehouse)
            for candidate_item, org, warehouse in mismatch_keys
            if candidate_item == item_id
        }
        offset = 0
        while True:
            params = {
                "$top": _TARGETED_REPAIR_PAGE_SIZE,
                "$skip": offset,
                "$filter": (
                    f"Period gt datetime'{_odata_local(opening_at)}' and "
                    f"Period le datetime'{_odata_local(cutoff)}' and "
                    f"Номенклатура_Key eq guid'{item_ref}'"
                ),
                "$select": (
                    "Period,Recorder,Recorder_Type,LineNumber,"
                    "Организация_Key,СтруктурнаяЕдиница_Key"
                ),
                "$orderby": "Period,Recorder_Type,Recorder,LineNumber",
            }
            response = client._make_request(
                "AccumulationRegister_ЗапасыНаСкладах_RecordType", params
            )
            rows = response.get("value") if isinstance(response, Mapping) else None
            if not isinstance(rows, list):
                raise PhysicalRefreshOrchestratorError(
                    "targeted convergence repair received malformed register page"
                )
            for row in rows:
                key = (
                    _normalized_ref(row.get("Организация_Key")),
                    _normalized_ref(row.get("СтруктурнаяЕдиница_Key")),
                )
                if key not in wanted:
                    continue
                identity = (
                    _normalized_recorder_type(row.get("Recorder_Type")),
                    _normalized_ref(row.get("Recorder")),
                )
                if identity[0] and identity[1]:
                    identities.add(identity)
            if len(identities) > _TARGETED_REPAIR_MAX_RECORDERS:
                raise PhysicalRefreshOrchestratorError(
                    "targeted convergence repair exceeded recorder limit"
                )
            if len(rows) < _TARGETED_REPAIR_PAGE_SIZE:
                break
            offset += len(rows)

    deferred: list[tuple[str, str]] = []
    repaired = 0
    for recorder_type, recorder_ref in sorted(identities):
        try:
            result = pull_recorder_movements(
                db,
                recorder_type,
                recorder_ref,
                client=client,
                source="physical_refresh_targeted_repair",
                ledger_generation_id=None,
                max_posting_at=generation.cutoff,
                strict_historical=True,
            )
        except HistoricalPullBeyondCutoffError:
            # The accepted prefix must keep the recorder revision that existed
            # at its cutoff.  A newer current RecordSet belongs to the next
            # refresh and cannot safely repair this immutable candidate.
            deferred.append((recorder_type, recorder_ref))
            continue
        if (
            result.status not in {"done", "empty"}
            or result.error
            or result.diagnostics
            or result.skipped_unknown_item
            or result.skipped_unknown_record_type
            or result.skipped_non_warehouse
        ):
            raise PhysicalRefreshOrchestratorError(
                f"targeted recorder repair failed: {recorder_type} {recorder_ref}"
            )
        repaired += 1

    terminal = db.query(func.max(models.PhysicalImportBatch.id)).scalar()
    if terminal is None:
        raise PhysicalRefreshOrchestratorError(
            "targeted convergence repair lost physical terminal"
        )
    generation.physical_import_batch_id = int(terminal)
    generation.source_watermarks = {
        **dict(generation.source_watermarks or {}),
        "targeted_convergence_repair": {
            "version": "1",
            "mismatched_keys": len(mismatch_keys),
            "recorder_count": repaired,
            "deferred_beyond_cutoff": [
                {"recorder_type": recorder_type, "recorder_ref": recorder_ref}
                for recorder_type, recorder_ref in deferred
            ],
            "physical_import_batch_id": int(terminal),
        },
    }
    db.commit()
    return repaired


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
    audit_all_known_recorders: bool = True,
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
        pool_mapping = effective_planning_pool_by_warehouse(
            db,
            planning_pool_by_warehouse,
        )
        validate_future_supply_destinations(
            db,
            ledger_generation_id=int(parent.id),
            mapping=pool_mapping,
        )
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
            audit_all_known_recorders=audit_all_known_recorders,
        )
        opening_reconcile = None
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
        if not convergence.valid:
            physical_generation = db.get(
                models.LedgerGeneration, int(fork.ledger_generation_id)
            )
            if physical_generation is None:
                raise PhysicalRefreshOrchestratorError(
                    "physical refresh generation disappeared before targeted repair"
                )
            snapped = _snap_balance_at_cutoff(
                db,
                generation=physical_generation,
                convergence=convergence,
            )
            if snapped:
                convergence = evaluate_physical_refresh_balance_convergence(
                    db,
                    ledger_generation_id=int(fork.ledger_generation_id),
                    balance_snapshot=balance_snapshot,
                )
        if not convergence.valid and opening_balance_loader is not None:
            remaining_keys = {
                (
                    int(delta.item_id),
                    str(delta.organization_ref),
                    str(delta.warehouse_ref1c),
                )
                for delta in convergence.deltas
                if not delta.matched
            }
            opening_reconcile = _reconcile_opening_balance(
                db,
                ledger_generation_id=int(fork.ledger_generation_id),
                loader=opening_balance_loader,
                only_keys=remaining_keys,
            )
            if opening_reconcile is not None and opening_reconcile.adjusted_keys:
                convergence = evaluate_physical_refresh_balance_convergence(
                    db,
                    ledger_generation_id=int(fork.ledger_generation_id),
                    balance_snapshot=balance_snapshot,
                )
        # Retain the diagnostic and completed import even when convergence is
        # false; neither operation moves the public planning-truth pointer.
        db.commit()
        if not convergence.valid:
            raise PhysicalRefreshBalanceConvergenceError(
                int(fork.ledger_generation_id),
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
            planning_pool_by_warehouse=pool_mapping,
        )
        fixed_run_ids = tuple(
            int(run_id)
            for (run_id,) in db.query(models.PlanningRun.run_id)
            .filter(models.PlanningRun.status == "FIXED_SNAPSHOT")
            .order_by(models.PlanningRun.run_id.asc())
            .all()
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

"""Crash-resumable physical refresh with bounded current-owner publication."""

from __future__ import annotations

import time
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
)
from .physical_visibility import visible_sle_query
from .ingest import HistoricalPullBeyondCutoffError, pull_recorder_movements
from .physical_refresh_import import (
    PhysicalRefreshImportResult,
    run_physical_recorder_audit,
)
from .r3_contract import business_identity_for_cutoff_balance_adjustment
from .supplier_future_supply import supplier_future_supply_delta
from .physical_refresh_generation import fork_physical_refresh_generation
from .physical_refresh_discard import discard_physical_refresh_candidate
from .output_repair_gate import assert_output_repair_allows
from app.services.mrp_freeze import MRP_LEDGER_LOCK_KEY
from app.services.planning_truth import record_pointer_verification
from .physical_refresh_current_publish import (
    ForwardPhysicalRefreshUnavailable,
    physical_refresh_last_failure_status,
    physical_refresh_phase_status,
    publish_forward_physical_refresh_current,
    repair_current_execution_scopes_from_pointer,
)


PHYSICAL_REFRESH_LOCK_KEY = PHYSICAL_SEQUENCE_LOCK_KEY
_sqlite_lock = Lock()


class PhysicalRefreshOrchestratorError(RuntimeError):
    """A complete fresh truth could not be proved and was not published."""


class PhysicalRefreshBalanceConvergenceError(PhysicalRefreshOrchestratorError):
    """A persisted candidate no longer matches the 1C balance at its cutoff."""

    def __init__(self, ledger_generation_id: int, message: str):
        self.ledger_generation_id = int(ledger_generation_id)
        super().__init__(message)


def _physical_refresh_evidence(
    physical_import: Any,
    recorder_audit: Any,
    *,
    database_ledger_rows: int,
) -> dict[str, Any]:
    """Return bounded delta evidence from durable import/audit checkpoints."""
    inserted_rows = int(getattr(physical_import, "movements_inserted", 0) or 0)
    changed_recorders = int(getattr(recorder_audit, "changed_recorders", 0) or 0)
    backdated_rows = int(getattr(recorder_audit, "backdated_recorders", 0) or 0)
    revised_rows = int(getattr(recorder_audit, "revised_recorders", 0) or 0)
    vanished_rows = int(getattr(recorder_audit, "vanished_recorders", 0) or 0)
    return {
        "input_delta_rows": max(
            inserted_rows,
            changed_recorders,
            backdated_rows + revised_rows + vanished_rows,
        ),
        "affected_scopes": (),
        "backdated": bool(backdated_rows),
        "database_ledger_rows": int(database_ledger_rows),
    }


def _physical_refresh_delta_rows(
    db: Session,
    *,
    parent: models.LedgerGeneration,
    target: models.LedgerGeneration,
    physical_import: Any,
    recorder_audit: Any,
) -> dict[str, Any]:
    """Load only rows named by new import batches and supersession edges.

    The batch interval is the durable import manifest.  Supersession edges add
    the old row for a revision/vanish without comparing the accepted prefix.
    Every query is therefore bounded by the refresh evidence rather than by
    the size of the historical Ledger.
    """
    parent_batch_id = int(parent.physical_import_batch_id or 0)
    terminal_ids = (
        int(getattr(physical_import, "physical_import_batch_id", 0) or 0),
        int(getattr(physical_import, "terminal_physical_import_batch_id", 0) or 0),
        int(getattr(recorder_audit, "terminal_physical_import_batch_id", 0) or 0),
        int(target.physical_import_batch_id or 0),
    )
    terminal_batch_id = max(terminal_ids)
    if terminal_batch_id <= parent_batch_id:
        return {
            "rows": (), "new_rows": (), "new_row_ids": (), "supersessions": (),
            "input_delta_rows": 0, "affected_scopes": (), "backdated": False,
            "backdate_from": None,
        }
    new_rows = tuple(db.query(models.StockLedgerEntry).filter(
        models.StockLedgerEntry.ingest_batch_id > parent_batch_id,
        models.StockLedgerEntry.ingest_batch_id <= terminal_batch_id,
    ).order_by(
        models.StockLedgerEntry.posting_at.asc(), models.StockLedgerEntry.id.asc(),
    ).all())
    supersessions = tuple(db.query(models.StockLedgerFactSupersession).filter(
        models.StockLedgerFactSupersession.import_batch_id > parent_batch_id,
        models.StockLedgerFactSupersession.import_batch_id <= terminal_batch_id,
    ).order_by(models.StockLedgerFactSupersession.id.asc()).all())
    changed_ids = {int(row.id) for row in new_rows}
    changed_ids.update(int(edge.old_sle_id) for edge in supersessions)
    changed_ids.update(
        int(edge.new_sle_id) for edge in supersessions if edge.new_sle_id is not None
    )
    rows = tuple(db.query(models.StockLedgerEntry).filter(
        models.StockLedgerEntry.id.in_(sorted(changed_ids))
    ).order_by(
        models.StockLedgerEntry.posting_at.asc(), models.StockLedgerEntry.id.asc(),
    ).all()) if changed_ids else ()
    if len(rows) != len(changed_ids):
        raise PhysicalRefreshOrchestratorError(
            "physical refresh evidence references a missing StockLedgerEntry"
        )
    item_ids = sorted({int(row.item_id) for row in rows})
    current = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.is_current.is_(True),
        models.ReservationEntry.lifecycle_status == "active",
        models.ReservationEntry.item_id.in_(item_ids) if item_ids else False,
    ).all()
    scopes: set[str] = set()
    for row in rows:
        mode = "make" if str(row.movement_kind or "") == "assembly_in" else "buy"
        matches = [entry for entry in current
                   if int(entry.item_id) == int(row.item_id)
                   and ("make" if str(entry.realization_mode or "") == "rework"
                        else str(entry.realization_mode or "")) == mode]
        if matches:
            scopes.update(
                ":".join((
                    str(int(entry.item_id)), str(entry.characteristic_ref or ""),
                    str(entry.organization_ref or ""), str(entry.planning_stock_pool or ""),
                    mode,
                )) for entry in matches
            )
        else:
            scopes.add(":".join((
                str(int(row.item_id)), str(row.characteristic_ref or ""),
                str(row.organization_ref or ""), str(row.warehouse_ref1c or ""), mode,
            )))
    parent_cutoff = _utc(parent.cutoff, "parent cutoff")
    rows_by_id = {int(row.id): row for row in rows}
    new_row_ids = {int(row.id) for row in new_rows}
    # The earliest changed boundary is the canonical start of a bounded scoped
    # replay (CANON "Объём вычислений штатного физического refresh", §37): the
    # oldest posting among newly imported backdated facts and among the facts a
    # supersession removed from the accepted basis.  It is kept on the raw
    # ``posting_at`` axis because every bounded consumer compares it against
    # persisted ``posting_at`` values, not against the generation cutoff.
    boundary_candidates = [
        row.posting_at
        for row in new_rows
        if _posting_at_utc(row.posting_at, "physical posting_at") <= parent_cutoff
    ]
    for edge in supersessions:
        old = rows_by_id.get(int(edge.old_sle_id))
        if old is None:
            raise PhysicalRefreshOrchestratorError(
                "physical refresh supersession references a missing superseded fact "
                f"(edge_id={int(edge.id)}, old_sle_id={int(edge.old_sle_id)})"
            )
        boundary_candidates.append(old.posting_at)
    backdate_from = (
        min(boundary_candidates, key=lambda value: _naive(value))
        if boundary_candidates else None
    )
    return {
        "rows": rows,
        "new_rows": new_rows,
        "new_row_ids": tuple(sorted(new_row_ids)),
        "supersessions": supersessions,
        "input_delta_rows": len(rows),
        "affected_scopes": tuple(sorted(scopes)),
        "backdated": any(
            _posting_at_utc(row.posting_at, "physical posting_at") <= parent_cutoff
            for row in rows
        ),
        "backdate_from": backdate_from,
    }


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
    # Operational proof that a physical tick used the bounded path.  These
    # fields are deliberately part of the result rather than log-only data so
    # the acceptance gate can measure replay cost on a production-sized dump.
    input_delta_rows: int = 0
    replayed_rows: int = 0
    affected_scopes: tuple[str, ...] = ()
    duration_ms: int = 0
    database_ledger_rows: int = 0
    phase_timings: tuple[tuple[str, int], ...] = ()
    # Current scopes a no-op refresh had to republish because a reference
    # writer had invalidated them since the last publication.
    repaired_scopes: tuple[str, ...] = ()
    # Cutoff up to which this tick proved the *current* pointer still true
    # without publishing a successor (§57).  ``None`` when nothing was proved
    # or the proof added no freshness.
    verified_cutoff: datetime | None = None


_LEDGER_LOCAL_TZ = ZoneInfo("Europe/Moscow")

def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _naive(value: datetime | None) -> datetime | None:
    """Drop only the adapter timezone marker for same-axis comparisons.

    ``posting_at`` boundaries travel to the bounded stock/replenishment writers
    unchanged; those modules compare them against persisted ``posting_at``
    columns, so the boundary must never be shifted onto another axis here.
    """
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _posting_at_utc(value: datetime, field: str) -> datetime:
    """Ledger ``posting_at`` is a naive Europe/Moscow timestamp (see ingest).

    Treating it as UTC shifts every fact by three hours: facts posted within
    three hours before the cutoff look like the future and genuinely
    backdated facts look current.  Localise naive values first.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=_LEDGER_LOCAL_TZ).astimezone(timezone.utc)
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
                business_identity=business_identity_for_cutoff_balance_adjustment(
                    recorder_ref,
                    "0",
                    item_id=key.item_id,
                    characteristic_ref=key.characteristic_ref,
                    organization_ref=key.organization_ref,
                    warehouse_ref1c=key.warehouse_ref1c,
                    snap_content_hash=content_hash,
                ),
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


def _bounded_custody_tail_sle_ids(
    db: Session,
    *,
    after_event_id: int,
    parent_generation_id: int | None = None,
    target_cutoff: datetime | None = None,
) -> tuple[int, ...]:
    """Validate and return the bounded physical identities behind an event tail.

    A refresh can crash after its import checkpoint commits but before current
    publication.  On the next launch those events are durable even though the
    accepted manifest still points at the earlier watermark.  Accept only
    events whose source SLE is either inside the accepted physical boundary or
    belongs to a completed import batch after that boundary.  This is a
    bounded recovery proof; it never discovers the historical event stream or
    ledger prefix.

    Local events, missing/inactive SLEs, incomplete batches, future postings,
    duplicate source identities, and malformed batch lineage fail closed.
    """
    rows = (
        db.query(
            models.ProductionMaterialCustodyEvent.id,
            models.ProductionMaterialCustodyEvent.source_sle_id,
            models.ProductionMaterialCustodyEvent.component_item_id,
        )
        .filter(models.ProductionMaterialCustodyEvent.id > int(after_event_id))
        .order_by(models.ProductionMaterialCustodyEvent.id.asc())
        .all()
    )
    source_ids: list[int] = []
    event_ids: list[int] = []
    event_components: dict[int, int] = {}
    event_by_source: dict[int, int] = {}
    for event_id, source_sle_id, component_item_id in rows:
        if source_sle_id is None:
            raise PhysicalRefreshOrchestratorError(
                "physical refresh custody tail contains a non-physical event "
                f"(event_id={int(event_id)})"
            )
        event_ids.append(int(event_id))
        source_ids.append(int(source_sle_id))
        event_components[int(event_id)] = int(component_item_id)
        event_by_source[int(source_sle_id)] = int(event_id)
    if len(set(source_ids)) != len(source_ids):
        raise PhysicalRefreshOrchestratorError(
            "physical refresh custody tail contains duplicate source SLEs "
            f"(events={event_ids}, source_sle_ids={source_ids})"
        )
    if not source_ids or parent_generation_id is None:
        return tuple(source_ids)

    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    if parent is None or parent.physical_import_batch_id is None:
        raise PhysicalRefreshOrchestratorError(
            "physical refresh custody tail cannot resolve parent import boundary"
        )
    parent_batch_id = int(parent.physical_import_batch_id)
    query = (
        db.query(
            models.StockLedgerEntry.id,
            models.StockLedgerEntry.ingest_batch_id,
            models.StockLedgerEntry.item_id,
            models.StockLedgerEntry.movement_kind,
            models.StockLedgerEntry.posting_at,
            models.StockLedgerEntry.active,
            models.PhysicalImportBatch.id,
            models.PhysicalImportBatch.status,
            models.PhysicalImportBatch.source_complete,
            models.PhysicalImportBatch.cutoff,
            models.PhysicalImportBatch.source_watermarks,
        )
        .join(
            models.PhysicalImportBatch,
            models.PhysicalImportBatch.id == models.StockLedgerEntry.ingest_batch_id,
        )
        .filter(models.StockLedgerEntry.id.in_(source_ids))
    )
    facts = query.all()
    by_id = {int(row[0]): row for row in facts}
    if set(by_id) != set(source_ids):
        missing = sorted(set(source_ids) - set(by_id))
        raise PhysicalRefreshOrchestratorError(
            "physical refresh custody tail references missing SLEs "
            f"(source_sle_ids={missing})"
        )
    cutoff = _utc(target_cutoff, "target cutoff") if target_cutoff is not None else None
    for source_id in source_ids:
        (
            _sle_id,
            ingest_batch_id,
            sle_item_id,
            movement_kind,
            posting_at,
            active,
            batch_id,
            batch_status,
            source_complete,
            batch_cutoff,
            marks,
        ) = by_id[int(source_id)]
        if not bool(active):
            raise PhysicalRefreshOrchestratorError(
                "physical refresh custody tail references inactive SLE "
                f"(source_sle_id={int(source_id)})"
            )
        event_id = event_by_source[int(source_id)]
        if int(sle_item_id) != int(event_components[event_id]):
            raise PhysicalRefreshOrchestratorError(
                "physical refresh custody tail has foreign source item "
                f"(event_id={event_id}, source_sle_id={int(source_id)}, "
                f"event_item_id={event_components[event_id]}, sle_item_id={int(sle_item_id)})"
            )
        if str(movement_kind or "") not in {"transfer_in", "transfer_out"}:
            raise PhysicalRefreshOrchestratorError(
                "physical refresh custody tail has unsupported source movement "
                f"(event_id={event_id}, source_sle_id={int(source_id)}, "
                f"movement_kind={str(movement_kind or '')!r})"
            )
        if str(batch_status or "") != "completed" or not bool(source_complete):
            raise PhysicalRefreshOrchestratorError(
                "physical refresh custody tail references incomplete import batch "
                f"(source_sle_id={int(source_id)}, batch_id={int(batch_id)})"
            )
        if cutoff is not None and _posting_at_utc(posting_at, "custody source posting_at") > _utc(cutoff, "target cutoff"):
            raise PhysicalRefreshOrchestratorError(
                "physical refresh custody tail contains a future source SLE "
                f"(source_sle_id={int(source_id)}, posting_at={posting_at!s})"
            )
        if batch_cutoff is not None and cutoff is not None:
            if _utc(batch_cutoff, "custody import cutoff") > cutoff:
                raise PhysicalRefreshOrchestratorError(
                    "physical refresh custody tail references a future import batch "
                    f"(source_sle_id={int(source_id)}, batch_id={int(batch_id)})"
                )
        if int(ingest_batch_id) <= parent_batch_id:
            continue
        batch_marks = dict(marks or {})
        previous = batch_marks.get("previous_import_batch_id")
        try:
            previous_id = int(previous)
        except (TypeError, ValueError):
            previous_id = -1
        # A post-parent batch is a recoverable retry delta only when its
        # persisted lineage identifies the preceding physical boundary.  This
        # excludes arbitrary completed batches from another source/process.
        if previous_id < parent_batch_id:
            raise PhysicalRefreshOrchestratorError(
                "physical refresh custody tail has foreign batch lineage "
                f"(source_sle_id={int(source_id)}, batch_id={int(batch_id)}, "
                f"parent_batch_id={parent_batch_id}, previous_batch_id={previous_id})"
            )
    return tuple(source_ids)


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
    discovery_lookback: timedelta | None = timedelta(0),
    audit_all_known_recorders: bool = False,
    opening_balance_loader: Callable[[datetime], Mapping[Any, Any]] | None = None,
    config_version_id: int | None = None,
    config_snapshot: Mapping[str, Any] | None = None,
    planning_pool_by_warehouse: Mapping[str, str] | None = None,
    database_ledger_rows: int | None = None,
) -> PhysicalRefreshOrchestrationResult:
    """Advance physical truth and publish refreshed planning snapshots.

    Window imports are durable checkpoints.  Routine refresh records only the
    bounded post-cutoff physical delta and publishes proven forward facts into
    compact current owners; equivalent imports keep the parent pointer.

    ``opening_balance_loader`` is called with the anchor instant and must return
    1C's Balance as of it.  Without it the opening balance is left as seeded,
    which leaves documents backdated behind the anchor permanently unaccounted.
    """
    started_monotonic = time.monotonic()
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
        # Serialise the durable repair gate check with repair-job creation.
        # Once the fork is flushed, its BUILDING physical candidate prevents a
        # repair from starting until this lifecycle finishes or is discarded.
        if db.get_bind().dialect.name == "postgresql":
            db.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": MRP_LEDGER_LOCK_KEY},
            )
        assert_output_repair_allows(
            db,
            operation="physical refresh",
            actor=started_by,
        )
        parent = _current_parent(db)
        parent_custody_manifest = db.get(
            models.ProductionMaterialCustodyProjectionManifest, int(parent.id)
        )
        custody_event_start = int(
            parent_custody_manifest.source_event_high_watermark_id
            if parent_custody_manifest is not None
            else 0
        )
        preexisting_custody_tail_sle_ids: tuple[int, ...] = ()
        if parent_custody_manifest is not None:
            # A previous process may have committed the import/custody event
            # tail before crashing.  Validate that bounded tail now and carry
            # it into the new current publication instead of rejecting a
            # recoverable retry at startup.
            preexisting_custody_tail_sle_ids = _bounded_custody_tail_sle_ids(
                db,
                after_event_id=custody_event_start,
                parent_generation_id=int(parent.id),
                target_cutoff=cutoff,
            )
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
            lightweight=True,
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

        physical_generation = db.get(
            models.LedgerGeneration, int(fork.ledger_generation_id)
        )
        if physical_generation is None:
            raise PhysicalRefreshOrchestratorError(
                "physical refresh generation disappeared before convergence"
            )

        # Resolve the bounded import manifest before convergence.  The
        # convergence gate folds the parent's compact current StockBin plus
        # these rows; it must not scan the accepted historical SLE prefix.
        delta = _physical_refresh_delta_rows(
            db,
            parent=parent,
            target=physical_generation,
            physical_import=physical_import,
            recorder_audit=recorder_audit,
        )

        def _bounded_convergence() -> BalanceConvergenceResult:
            return evaluate_physical_refresh_balance_convergence(
                db,
                ledger_generation_id=int(fork.ledger_generation_id),
                balance_snapshot=balance_snapshot,
                base_generation_id=int(parent.id),
                delta_rows=tuple(delta["rows"]),
                new_rows=tuple(delta["new_rows"]),
                supersession_edges=tuple(delta["supersessions"]),
            )

        convergence = _bounded_convergence()
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
                physical_generation = db.get(
                    models.LedgerGeneration, int(fork.ledger_generation_id)
                )
                delta = _physical_refresh_delta_rows(
                    db,
                    parent=parent,
                    target=physical_generation,
                    physical_import=physical_import,
                    recorder_audit=recorder_audit,
                )
                convergence = _bounded_convergence()
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
                physical_generation = db.get(
                    models.LedgerGeneration, int(fork.ledger_generation_id)
                )
                delta = _physical_refresh_delta_rows(
                    db,
                    parent=parent,
                    target=physical_generation,
                    physical_import=physical_import,
                    recorder_audit=recorder_audit,
                )
                convergence = _bounded_convergence()
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
        # The recorder audit and import checkpoints are the durable delta
        # manifest.  Do not derive a delta by comparing visible parent/target
        # prefixes: that turns every refresh into O(history) work and cannot
        # distinguish an ordinary tick from a correction without re-reading it.
        # A prod-scale row count is preflight evidence, not part of the timed
        # refresh transaction.  Import adapters may provide it from their
        # persisted checkpoint; absent that evidence the acceptance evaluator
        # fails closed rather than forcing a history scan here.
        # The production-scale row count is supplied by a separate preflight
        # or persisted adapter checkpoint.  Never count the historical Ledger
        # inside this timed refresh transaction.
        database_ledger_rows = int(
            database_ledger_rows
            if database_ledger_rows is not None
            else (getattr(physical_import, "database_ledger_rows", 0) or 0)
        )
        evidence = _physical_refresh_evidence(
            physical_import,
            recorder_audit,
            database_ledger_rows=database_ledger_rows,
        )
        input_delta_rows = max(
            int(evidence["input_delta_rows"]),
            int(delta["input_delta_rows"]),
        )
        custody_source_sle_ids = tuple(dict.fromkeys(
            preexisting_custody_tail_sle_ids
            + _bounded_custody_tail_sle_ids(
                db,
                after_event_id=custody_event_start,
                parent_generation_id=int(parent.id),
                target_cutoff=cutoff,
            )
        ))
        # Decision §25: a real change of the supply-relevant supplier-order
        # fields is accepted only by a new physical generation.  A tick with no
        # movement at all therefore still has to ask whether the 1C order
        # contour moved before it discards its candidate; §57 extends freshness
        # only when this refresh found no semantic delta of any kind.
        movement_delta = bool(input_delta_rows) or bool(custody_source_sle_ids)
        # Qualified here only when this tick moved no fact at all; with
        # movements the publication qualifies the contour itself, after it has
        # written its own receipt provenance.  The result is handed to the
        # publisher so the movement-free tick qualifies the mirror once.
        prepared_supplier_delta = (
            None
            if movement_delta
            else supplier_future_supply_delta(
                db,
                int(physical_generation.id),
                planning_pool_by_warehouse=pool_mapping,
            )
        )
        supplier_changed = movement_delta or bool(
            prepared_supplier_delta.changed_scopes
        )
        if not movement_delta and not supplier_changed:
            # Equivalent imports have no successor in R3.  Discard the
            # technical fork and keep the accepted pointer and every compact
            # current owner untouched; even a provenance-only UPDATE would
            # create needless WAL/audit churn.
            #
            # "Untouched" is only correct while the current scopes are still
            # ready.  Reference writers (specification import, calendar,
            # rates, resources, custody) invalidate their manifests and rely on
            # this worker to republish them; without the repair below an
            # overnight specification import left queue/readiness/drum/shelf
            # fail-closed until a real delta happened to arrive.
            try:
                repair = repair_current_execution_scopes_from_pointer(
                    db,
                    pointer_generation_id=int(parent.id),
                    payload_boundary_generation_id=int(physical_generation.id),
                    source_revision=int(
                        physical_generation.physical_import_batch_id or 0
                    ),
                )
            except Exception as exc:
                # The repair is caller-owned like the bounded publication:
                # roll it back whole, then release the technical candidate in
                # its own recovery transaction so the next tick is not blocked.
                db.rollback()
                reason = f"current execution scope repair failed: {exc}"
                try:
                    candidate = db.get(
                        models.LedgerGeneration, int(fork.ledger_generation_id)
                    )
                    if candidate is not None and str(candidate.status or "") == "building":
                        discard_physical_refresh_candidate(
                            db,
                            ledger_generation_id=int(candidate.id),
                            reason=reason,
                        )
                        db.commit()
                except Exception:
                    db.rollback()
                    raise
                raise PhysicalRefreshOrchestratorError(reason) from exc
            repaired_scopes = tuple(repair.repaired_scopes) if repair is not None else ()
            physical_generation.source_watermarks = {
                **dict(physical_generation.source_watermarks or {}),
                "physical_refresh_delta": {
                    "input_delta_rows": 0,
                    "replayed_rows": 0,
                    "affected_scopes": list(delta["affected_scopes"]),
                    "backdated": bool(delta["backdated"]),
                    "database_ledger_rows": database_ledger_rows,
                    "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
                    "repaired_scopes": list(repaired_scopes),
                },
            }
            discard_physical_refresh_candidate(
                db,
                ledger_generation_id=int(physical_generation.id),
                reason="no semantic physical delta",
            )
            # Decision §57.  This tick did read 1C up to ``cutoff`` and proved
            # the accepted pointer still converges on balances; the only thing
            # it did not find is a reason to create a successor.  Freshness is
            # the age of the last successful reconciliation, not of the last
            # publication, so record the proof on the pointer in the same
            # transaction as the discard.  Without it a weekend without
            # postings left every HTTP reader on 503 ``planning_truth_
            # unavailable`` until the first Monday document arrived.
            verified_cutoff = record_pointer_verification(
                db,
                verified_generation_id=int(parent.id),
                verified_cutoff=cutoff,
                balance_convergence_valid=bool(convergence.valid),
            )
            db.commit()
            fixed_run_ids = tuple(
                int(run_id)
                for (run_id,) in db.query(models.PlanningRun.run_id)
                .filter(models.PlanningRun.status == "FIXED_SNAPSHOT")
                .order_by(models.PlanningRun.run_id.asc())
                .all()
            )
            return PhysicalRefreshOrchestrationResult(
                parent_generation_id=int(parent.id),
                physical_generation_id=int(physical_generation.id),
                published_generation_id=int(parent.id),
                cutoff=cutoff,
                physical_import=physical_import,
                recorder_audit=recorder_audit,
                balance_convergence=convergence,
                candidate_run_ids=fixed_run_ids,
                published=False,
                opening_reconcile=opening_reconcile,
                input_delta_rows=0,
                replayed_rows=0,
                affected_scopes=(),
                duration_ms=int((time.monotonic() - started_monotonic) * 1000),
                database_ledger_rows=database_ledger_rows,
                repaired_scopes=repaired_scopes,
                verified_cutoff=verified_cutoff,
            )
        delta = _physical_refresh_delta_rows(
            db,
            parent=parent,
            target=physical_generation,
            physical_import=physical_import,
            recorder_audit=recorder_audit,
        )
        delta_rows = tuple(delta["rows"])
        window_rows = tuple(delta["new_rows"])
        window_row_ids = set(delta["new_row_ids"])
        basis_rows = tuple(
            row for row in delta_rows if int(row.id) not in window_row_ids
        )
        affected_scopes = tuple(delta["affected_scopes"])
        backdate_from = delta["backdate_from"]
        if len(delta_rows) != input_delta_rows:
            # The durable import/audit checkpoints name more changed facts than
            # the manifest can resolve.  A backdate or correction is handled by
            # the bounded scoped replay below; an unaccounted delta is not.
            reason = (
                "bounded physical delta manifest is incomplete; "
                "correction/backdate requires explicit maintenance"
            )
            physical_generation.source_watermarks = {
                **dict(physical_generation.source_watermarks or {}),
                "physical_refresh_delta": {
                    "input_delta_rows": input_delta_rows,
                    "replayed_rows": 0,
                    "affected_scopes": list(affected_scopes),
                    "backdated": bool(delta["backdated"]),
                    "database_ledger_rows": database_ledger_rows,
                    "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
                },
            }
            discard_physical_refresh_candidate(
                db,
                ledger_generation_id=int(physical_generation.id),
                reason=reason,
            )
            db.commit()
            raise PhysicalRefreshOrchestratorError(reason)

        try:
            current_publish = publish_forward_physical_refresh_current(
                db,
                target_generation_id=int(physical_generation.id),
                parent_generation_id=int(parent.id),
                delta_manifest={
                    "rows": window_rows,
                    "basis_rows": basis_rows,
                    "supersessions": tuple(delta["supersessions"]),
                    "backdate_from": backdate_from,
                    "custody_source_sle_ids": custody_source_sle_ids,
                    "supplier_future_supply_changed": supplier_changed,
                    "supplier_future_supply_delta": prepared_supplier_delta,
                },
                odata_client=client,
                source_revision=int(physical_generation.physical_import_batch_id),
                planning_pool_by_warehouse=pool_mapping,
                custody_source_sle_ids=custody_source_sle_ids,
            )
        except Exception as exc:
            # The import/convergence checkpoint is already durable, but current
            # publication is still caller-owned.  Roll it back first, then use
            # the checked discard boundary in a separate recovery transaction.
            db.rollback()
            reason = str(exc)
            try:
                candidate = db.get(
                    models.LedgerGeneration, int(fork.ledger_generation_id)
                )
                if candidate is not None and str(candidate.status or "") == "building":
                    discard_physical_refresh_candidate(
                        db,
                        ledger_generation_id=int(candidate.id),
                        reason=reason,
                    )
                    db.commit()
            except Exception:
                db.rollback()
                raise
            if isinstance(exc, ForwardPhysicalRefreshUnavailable):
                wrapped = PhysicalRefreshOrchestratorError(reason)
                phase_failure = getattr(exc, "physical_refresh_phase_status", None)
                if isinstance(phase_failure, dict):
                    setattr(wrapped, "physical_refresh_phase_status", phase_failure)
                raise wrapped from exc
            raise

        publish_duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        physical_generation.source_watermarks = {
            **dict(physical_generation.source_watermarks or {}),
            "physical_refresh_delta": {
                "input_delta_rows": input_delta_rows,
                "replayed_rows": int(current_publish.replayed_rows),
                "affected_scopes": list(current_publish.affected_scopes),
                "backdated": bool(delta["backdated"]),
                "backdate_from": (
                    _naive(backdate_from).isoformat()
                    if backdate_from is not None else None
                ),
                "database_ledger_rows": database_ledger_rows,
                "duration_ms": publish_duration_ms,
                "phase_timings": dict(getattr(current_publish, "phase_timings", ()) or ()),
            },
        }
        db.commit()
        fixed_run_ids = tuple(
            int(run_id)
            for (run_id,) in db.query(models.PlanningRun.run_id)
            .filter(models.PlanningRun.status == "FIXED_SNAPSHOT")
            .order_by(models.PlanningRun.run_id.asc())
            .all()
        )
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
            input_delta_rows=int(current_publish.input_delta_rows),
            replayed_rows=int(current_publish.replayed_rows),
            affected_scopes=tuple(current_publish.affected_scopes),
            duration_ms=publish_duration_ms,
            database_ledger_rows=database_ledger_rows,
            phase_timings=tuple(getattr(current_publish, "phase_timings", ()) or ()),
        )
    except Exception:
        db.rollback()
        raise
    finally:
        if "lock_context" in locals():
            lock_context.__exit__(None, None, None)
        _release_lifecycle_lock(lifecycle_lock)

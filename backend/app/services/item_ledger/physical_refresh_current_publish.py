"""Atomic bounded publication for forward physical-refresh deltas.

The normal refresh path deliberately has no generation-local execution model.
It applies the explicitly imported delta to the compact current owners, builds
all dependent DTOs while the candidate is still BUILDING, and only then marks
the candidate accepted and moves the planning-truth pointer.  The caller owns
the transaction; this module never commits.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock
import time
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy.orm import Session

from app import models
from app.services.item_ledger.assembly_output_persistence import (
    apply_bounded_assembly_output_plan_execution,
)
from app.services.item_ledger.current_execution import (
    build_compact_current_assembly_payload,
    publish_current_execution_scope,
    publish_current_purchase_control_from_payload,
    publish_current_production_control_from_payload,
    resolve_compact_queue_owner_ids,
)
from app.services.item_ledger.current_replenishment import (
    BoundedBuyReceiptDeltaManifest,
    apply_current_replenishment_for_bounded_buy_scopes,
    apply_current_replenishment_for_bounded_make_scopes,
)
from app.services.item_ledger.drum_schedule_persistence import (
    build_compact_current_drum_payload,
)
from app.services.item_ledger.physical_refresh_provenance import (
    apply_bounded_current_material_custody_events,
    handoff_current_physical_refresh_provenance,
)
from app.services.item_ledger.physical_refresh_stock_bin import (
    BoundedPhysicalDeltaManifest,
    apply_bounded_current_stock_bins,
)
from app.services.item_ledger.physical import CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE
from app.services.item_ledger.physical_refresh_supplier_evidence import (
    build_bounded_supplier_receipt_manifest,
    is_supplier_document_type,
)
from app.services.planning_truth import publish_generation
from app.services.item_ledger.shelf_projection_persistence import (
    build_compact_current_shelf_payload,
)
from app.services.production_control_journal_projection import (
    build_compact_current_production_control_payload,
)
from app.services.purchase_control_projection import (
    build_compact_current_purchase_control_payload,
)
from app.services.production_material_custody_projection import (
    lock_current_custody_marker,
)


class ForwardPhysicalRefreshUnavailable(RuntimeError):
    """The explicit delta is not safe for the bounded current publisher."""


_PHASE_STATUS_LOCK = RLock()
_PHASE_STATUS: dict[int, dict[str, Any]] = {}
_PHASE_FAILURE_STATUS: dict[int, dict[str, Any]] = {}
_LAST_PHASE_FAILURE: dict[str, Any] | None = None


class _PhaseTracker:
    """In-memory, transaction-independent progress for one refresh attempt.

    The refresh transaction must remain atomic, so persisting a status row at
    every phase would both add WAL and make rollback diagnostics misleading.
    This small registry is read-only to callers and is cleared in the public
    publisher's ``finally`` block.
    """

    def __init__(self, target_generation_id: int):
        self.target_generation_id = int(target_generation_id)
        self.started = 0.0
        self.phase_started = 0.0
        self.current_phase = "validation"
        self._phase_active = True
        self._timings: dict[str, int] = {}

    def start(self) -> None:
        self.started = time.monotonic()
        self.phase_started = self.started
        with _PHASE_STATUS_LOCK:
            _PHASE_TRACKERS[self.target_generation_id] = self
            # A retry for the same candidate starts a fresh diagnostic record.
            _PHASE_FAILURE_STATUS.pop(self.target_generation_id, None)
            _PHASE_STATUS[self.target_generation_id] = {
                "target_generation_id": self.target_generation_id,
                "current_phase": self.current_phase,
                "elapsed_ms": 0,
                "phase_elapsed_ms": 0,
                "phase_timings": {},
            }

    def failure(self, exc: BaseException) -> dict[str, Any]:
        """Freeze the last phase before the live registry is cleared.

        Publication errors are intentionally re-raised unchanged so the caller
        can perform its normal rollback/recovery.  The immutable snapshot is
        attached to that exception and retained separately for the operator
        status endpoint; it never participates in the business transaction.
        """
        now = time.monotonic()
        if self._phase_active and self.current_phase and self.phase_started:
            self._record(self.current_phase, now - self.phase_started)
        self._publish(now)
        snapshot = {
            "target_generation_id": self.target_generation_id,
            "current_phase": self.current_phase,
            "elapsed_ms": max(0, int((now - self.started) * 1000)),
            "phase_elapsed_ms": max(0, int((now - self.phase_started) * 1000)),
            "phase_timings": dict(self._timings),
            "failed": True,
            "error": str(exc)[:1000],
        }
        global _LAST_PHASE_FAILURE
        with _PHASE_STATUS_LOCK:
            _PHASE_FAILURE_STATUS[self.target_generation_id] = dict(snapshot)
            _LAST_PHASE_FAILURE = dict(snapshot)
        return snapshot

    def begin(self, name: str) -> None:
        now = time.monotonic()
        if self._phase_active and self.current_phase and self.current_phase != name and self.phase_started:
            self._record(self.current_phase, now - self.phase_started)
        self.current_phase = str(name)
        self.phase_started = now
        self._phase_active = True
        self._publish(now)

    def complete(self, name: str) -> None:
        now = time.monotonic()
        if self._phase_active and self.current_phase == str(name) and self.phase_started:
            self._record(str(name), now - self.phase_started)
            self._phase_active = False
        self._publish(now)

    def _record(self, name: str, elapsed: float) -> None:
        self._timings[str(name)] = max(0, int(elapsed * 1000))

    def _publish(self, now: float) -> None:
        with _PHASE_STATUS_LOCK:
            status = _PHASE_STATUS.get(self.target_generation_id)
            if status is None:
                return
            status.update({
                "current_phase": self.current_phase,
                "elapsed_ms": max(0, int((now - self.started) * 1000)),
                "phase_elapsed_ms": (
                    max(0, int((now - self.phase_started) * 1000))
                    if self._phase_active else 0
                ),
                "phase_timings": dict(self._timings),
            })

    def timings(self) -> tuple[tuple[str, int], ...]:
        return tuple((name, int(duration)) for name, duration in self._timings.items())

    def clear(self) -> None:
        with _PHASE_STATUS_LOCK:
            _PHASE_STATUS.pop(self.target_generation_id, None)
            _PHASE_TRACKERS.pop(self.target_generation_id, None)


def physical_refresh_phase_status(target_generation_id: int) -> dict[str, Any]:
    """Return live bounded-refresh phase progress, or an empty mapping.

    This is deliberately an in-memory diagnostic surface: it never reads or
    writes business tables and therefore cannot create a second publication
    source or break the caller-owned transaction boundary.
    """
    with _PHASE_STATUS_LOCK:
        status = _PHASE_STATUS.get(int(target_generation_id))
        if status is None:
            status = _PHASE_FAILURE_STATUS.get(int(target_generation_id))
        if status is None:
            return {}
        snapshot = dict(status)
        snapshot["phase_timings"] = dict(status.get("phase_timings") or {})
        now = time.monotonic()
        tracker = _PHASE_TRACKERS.get(int(target_generation_id))
        if tracker is not None:
            snapshot["elapsed_ms"] = max(0, int((now - tracker.started) * 1000))
            snapshot["phase_elapsed_ms"] = (
                max(0, int((now - tracker.phase_started) * 1000))
                if tracker._phase_active else 0
            )
        return snapshot


def physical_refresh_last_failure_status() -> dict[str, Any]:
    """Return the last failed phase snapshot for process-local diagnostics."""
    with _PHASE_STATUS_LOCK:
        if _LAST_PHASE_FAILURE is None:
            return {}
        snapshot = dict(_LAST_PHASE_FAILURE)
        snapshot["phase_timings"] = dict(snapshot.get("phase_timings") or {})
        return snapshot


def _clear_phase_failure(target_generation_id: int) -> None:
    global _LAST_PHASE_FAILURE
    with _PHASE_STATUS_LOCK:
        _PHASE_FAILURE_STATUS.pop(int(target_generation_id), None)
        if (
            _LAST_PHASE_FAILURE is not None
            and int(_LAST_PHASE_FAILURE.get("target_generation_id") or -1)
            == int(target_generation_id)
        ):
            _LAST_PHASE_FAILURE = None


_PHASE_TRACKERS: dict[int, _PhaseTracker] = {}


@dataclass(frozen=True)
class PhysicalRefreshCurrentPublishResult:
    target_generation_id: int
    parent_generation_id: int
    affected_scopes: tuple[str, ...]
    input_delta_rows: int
    replayed_rows: int
    queue_changed_rows: int
    readiness_changed_rows: int
    production_changed_rows: int
    purchase_changed_rows: int
    phase_timings: tuple[tuple[str, int], ...] = ()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _rows(delta_manifest: Mapping[str, Any]) -> tuple[Any, ...]:
    rows = tuple(delta_manifest.get("rows") or ())
    by_id: set[int] = set()
    for row in rows:
        try:
            row_id = int(row.id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ForwardPhysicalRefreshUnavailable("physical delta contains an untyped row") from exc
        if row_id <= 0 or row_id in by_id:
            raise ForwardPhysicalRefreshUnavailable("physical delta contains duplicate row ids")
        by_id.add(row_id)
    return rows


def _comparable(value: datetime) -> datetime:
    """Compare SQLite-naive and PostgreSQL-aware source timestamps uniformly."""
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _is_supplier_receipt(row: Any) -> bool:
    return (
        is_supplier_document_type(row.recorder_type)
        and _text(row.movement_kind) in {"receipt", "supplier_receipt"}
    )


def _is_cutoff_adjustment(row: Any) -> bool:
    return _text(row.movement_kind) == CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE


def _persisted_rows(
    db: Session,
    rows: Sequence[Any],
    *,
    parent: models.LedgerGeneration,
    target: models.LedgerGeneration,
) -> tuple[Any, ...]:
    ids = tuple(int(row.id) for row in rows)
    persisted = tuple(db.query(models.StockLedgerEntry).filter(
        models.StockLedgerEntry.id.in_(ids),
    ).all())
    by_id = {int(row.id): row for row in persisted}
    if set(by_id) != set(ids):
        raise ForwardPhysicalRefreshUnavailable("physical delta references a missing persisted SLE")
    lower = int(parent.physical_import_batch_id or 0)
    upper = int(target.physical_import_batch_id or 0)
    if lower <= 0 or upper <= lower:
        raise ForwardPhysicalRefreshUnavailable("physical refresh import boundary is invalid")
    for row in persisted:
        batch_id = int(row.ingest_batch_id or 0)
        posting_at = row.posting_at
        if not bool(row.active) or not posting_at:
            raise ForwardPhysicalRefreshUnavailable("physical delta row is inactive or has no posting boundary")
        if not lower < batch_id <= upper:
            raise ForwardPhysicalRefreshUnavailable("physical delta row is outside target import boundary")
        if _comparable(posting_at) <= _comparable(parent.cutoff):
            raise ForwardPhysicalRefreshUnavailable("incremental physical refresh supports forward facts only; backdate requires maintenance")
        if _comparable(posting_at) > _comparable(target.cutoff):
            raise ForwardPhysicalRefreshUnavailable("physical delta row is after target cutoff")
    return tuple(sorted(persisted, key=lambda row: int(row.id)))


def _assert_forward_only(
    rows: Sequence[Any],
    *,
    parent_cutoff: datetime,
    target_cutoff: datetime,
    supersessions: Sequence[Any],
) -> None:
    if supersessions:
        raise ForwardPhysicalRefreshUnavailable(
            "incremental physical refresh supports forward facts only; supersession requires maintenance"
        )
    # These are ordinary forward stock movements.  Only the first and
    # canonical supplier document types below enter an R4 allocation writer.
    supported = {
        "assembly_in", "assembly_out", "transfer_in", "transfer_out",
        "receipt", "expense",
    }
    for row in rows:
        posting_at = getattr(row, "posting_at", None)
        if posting_at is None or _comparable(posting_at) <= _comparable(parent_cutoff):
            raise ForwardPhysicalRefreshUnavailable(
                "incremental physical refresh supports forward facts only; backdate requires maintenance"
            )
        kind = _text(getattr(row, "movement_kind", ""))
        if kind == CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE:
            if not (
                _text(row.recorder_type) == CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE
                and _text(row.ingest_source) == CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE
                and _comparable(posting_at) == _comparable(target_cutoff)
            ):
                raise ForwardPhysicalRefreshUnavailable(
                    "cutoff balance adjustment does not match the canonical source"
                )
            continue
        if kind not in supported:
            raise ForwardPhysicalRefreshUnavailable(
                f"incremental physical refresh movement kind is unsupported: {kind or '<empty>'}"
            )


def _affected_keys(rows: Sequence[Any]) -> tuple[tuple[int, str, str, str], ...]:
    return tuple(sorted({
        (
            int(row.item_id),
            _text(row.characteristic_ref),
            _text(row.organization_ref),
            _text(row.warehouse_ref1c),
        )
        for row in rows
    }))


def _current_owner_rows(
    db: Session,
    rows: Sequence[Any],
    *,
    planning_pool_by_warehouse: Mapping[str, str],
) -> tuple[models.ReservationEntry, ...]:
    """Load only current owners relevant to this physical delta."""
    relevant_item_ids = {
        int(row.item_id)
        for row in rows
        if _text(row.movement_kind) == "assembly_in"
        or (
            _is_supplier_receipt(row)
            and _text(row.warehouse_ref1c)
            and _text(planning_pool_by_warehouse.get(_text(row.warehouse_ref1c)))
        )
    }
    if not relevant_item_ids:
        return ()
    return tuple(db.query(models.ReservationEntry).filter(
        models.ReservationEntry.item_id.in_(sorted(relevant_item_ids)),
        models.ReservationEntry.realization_mode.in_(("make", "buy", "rework")),
        models.ReservationEntry.lifecycle_status == "active",
        models.ReservationEntry.owner_kind == "current",
        models.ReservationEntry.is_current.is_(True),
    ).all())


def _current_scopes(
    rows: Sequence[Any],
    *,
    planning_pool_by_warehouse: Mapping[str, str],
    current_owners: Sequence[models.ReservationEntry],
) -> tuple[tuple[int, str, str, str, str], ...]:
    scopes: set[tuple[int, str, str, str, str]] = set()
    assembly_rows = tuple(
        row for row in rows if _text(row.movement_kind) == "assembly_in"
    )
    if assembly_rows:
        for row in assembly_rows:
            matching_owners = tuple(
                owner for owner in current_owners
                if int(owner.item_id) == int(row.item_id)
                and _text(owner.characteristic_ref) == _text(row.characteristic_ref)
                and _text(owner.organization_ref) == _text(row.organization_ref)
                and _text(owner.realization_mode) in {"make", "rework"}
            )
            # An assembly fact without a current MAKE owner is a valid
            # stock/output-only fact.  Do not invent a pool from the physical
            # warehouse and do not invoke the R4 writer for it.
            scopes.update(
                (
                    int(row.item_id), _text(row.characteristic_ref),
                    _text(row.organization_ref), _text(owner.planning_stock_pool),
                    "make",
                )
                for owner in matching_owners
                if _text(owner.planning_stock_pool)
            )

    # Supplier receipts enter BUY only when the physical warehouse is inside
    # the configured planning contour.  Unmapped warehouses are legitimate
    # stock-only receipts (for example surplus outside the planning contour).
    for row in rows:
        if not _is_supplier_receipt(row):
            continue
        warehouse = _text(row.warehouse_ref1c)
        pool = _text(planning_pool_by_warehouse.get(warehouse)) if warehouse else ""
        if not pool:
            continue
        if not any(
            int(owner.item_id) == int(row.item_id)
            and _text(owner.characteristic_ref) == _text(row.characteristic_ref)
            and _text(owner.organization_ref) == _text(row.organization_ref)
            and _text(owner.planning_stock_pool) == pool
            and _text(owner.realization_mode) == "buy"
            for owner in current_owners
        ):
            continue
        scopes.add((
            int(row.item_id), _text(row.characteristic_ref),
            _text(row.organization_ref), pool, "buy",
        ))
    return tuple(sorted(scopes))


def _mapped_supplier_rows(
    rows: Sequence[Any],
    *,
    planning_pool_by_warehouse: Mapping[str, str],
    current_owners: Sequence[models.ReservationEntry],
) -> tuple[Any, ...]:
    """Return only supplier receipts in the configured planning contour."""
    result = []
    for row in rows:
        if not _is_supplier_receipt(row):
            continue
        warehouse = _text(row.warehouse_ref1c)
        pool = _text(planning_pool_by_warehouse.get(warehouse)) if warehouse else ""
        if pool and any(
            int(owner.item_id) == int(row.item_id)
            and _text(owner.characteristic_ref) == _text(row.characteristic_ref)
            and _text(owner.organization_ref) == _text(row.organization_ref)
            and _text(owner.planning_stock_pool) == pool
            and _text(owner.realization_mode) == "buy"
            for owner in current_owners
        ):
            result.append(row)
    return tuple(result)


def _fixed_run_ids(db: Session) -> tuple[int, ...]:
    return tuple(int(value) for (value,) in db.query(models.PlanningRun.run_id).filter(
        models.PlanningRun.status == "FIXED_SNAPSHOT",
    ).order_by(models.PlanningRun.run_id.asc()).all())


def _member(value: Any, name: str, default: Any = ()) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _publish_assembly_current(
    db: Session,
    *,
    target_id: int,
    assembly_payload: Any,
    revision: str,
) -> tuple[int, int]:
    queue_rows = tuple(_member(assembly_payload, "queue_rows") or ())
    queue_result = publish_current_execution_scope(
        db,
        source_revision=revision,
        source_generation_id=target_id,
        scope_key="assembly:all-live-plans",
        rows=queue_rows,
        entity_kinds=("assembly_queue",),
        summary={"total_rows": len(queue_rows)},
    )
    readiness_rows = resolve_compact_queue_owner_ids(
        db, tuple(_member(assembly_payload, "readiness_rows") or ())
    )
    readiness_result = publish_current_execution_scope(
        db,
        source_revision=revision,
        source_generation_id=target_id,
        scope_key="assembly:all-live-plans",
        rows=readiness_rows,
        entity_kinds=("assembly_readiness",),
        summary=dict(_member(assembly_payload, "readiness_metrics", {}) or {}),
    )
    return int(queue_result.changed_rows), int(readiness_result.changed_rows)


def publish_forward_physical_refresh_current(
    db: Session,
    *,
    target_generation_id: int,
    parent_generation_id: int,
    delta_manifest: Mapping[str, Any],
    odata_client: object | None,
    source_revision: int | str,
    planning_pool_by_warehouse: Mapping[str, str],
    custody_source_sle_ids: Sequence[int] = (),
    phase_hook: Callable[[str], None] | None = None,
) -> PhysicalRefreshCurrentPublishResult:
    """Publish a bounded delta and expose live phase progress while running."""
    tracker = _PhaseTracker(int(target_generation_id))
    tracker.start()
    try:
        result = _publish_forward_physical_refresh_current(
            db,
            target_generation_id=target_generation_id,
            parent_generation_id=parent_generation_id,
            delta_manifest=delta_manifest,
            odata_client=odata_client,
            source_revision=source_revision,
            planning_pool_by_warehouse=planning_pool_by_warehouse,
            custody_source_sle_ids=custody_source_sle_ids,
            phase_hook=phase_hook,
            _phase_tracker=tracker,
        )
        _clear_phase_failure(int(target_generation_id))
        return replace(result, phase_timings=tracker.timings())
    except Exception as exc:
        failure = tracker.failure(exc)
        # Keep the original exception type/message for recovery policy while
        # making phase/timing diagnostics available to the orchestrator.
        try:
            setattr(exc, "physical_refresh_phase_status", failure)
        except Exception:
            pass
        raise
    finally:
        tracker.clear()


def _publish_forward_physical_refresh_current(
    db: Session,
    *,
    target_generation_id: int,
    parent_generation_id: int,
    delta_manifest: Mapping[str, Any],
    odata_client: object | None,
    source_revision: int | str,
    planning_pool_by_warehouse: Mapping[str, str],
    custody_source_sle_ids: Sequence[int] = (),
    phase_hook: Callable[[str], None] | None = None,
    _phase_tracker: _PhaseTracker | None = None,
) -> PhysicalRefreshCurrentPublishResult:
    """Publish one proven forward delta atomically into current owners.

    This function intentionally has no ``commit`` call.  Any error, including
    a late payload or pointer validation failure, is left to the caller's
    rollback boundary and therefore restores every current owner it touched.
    """
    # Custody writers allocate append-only event ids under this marker. Hold
    # it for the caller-owned publication transaction so a concurrent local
    # event cannot appear between the bounded fold and compact payload reads.
    lock_current_custody_marker(db)
    target = db.get(models.LedgerGeneration, int(target_generation_id))
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    pointer = db.get(models.PlanningTruthState, 1)
    if target is None or _text(target.status) != "building":
        raise ForwardPhysicalRefreshUnavailable("target generation must be BUILDING")
    if parent is None or _text(parent.status) != "accepted":
        raise ForwardPhysicalRefreshUnavailable("parent generation must be accepted")
    if pointer is None or int(pointer.current_generation_id or -1) != int(parent.id):
        raise ForwardPhysicalRefreshUnavailable("parent generation is not current truth")
    if (
        target.cutoff is None
        or parent.cutoff is None
        or _comparable(target.cutoff) < _comparable(parent.cutoff)
    ):
        raise ForwardPhysicalRefreshUnavailable("physical refresh generation boundary is invalid")

    def start_phase(name: str) -> None:
        if _phase_tracker is not None:
            _phase_tracker.begin(name)

    def phase(name: str) -> None:
        if _phase_tracker is not None:
            _phase_tracker.complete(name)
        if phase_hook is not None:
            phase_hook(name)

    rows = _rows(delta_manifest)
    custody_source_ids = tuple(
        int(value)
        for value in (
            tuple(custody_source_sle_ids)
            + tuple(delta_manifest.get("custody_source_sle_ids") or ())
        )
    )
    if not rows and not custody_source_ids:
        raise ForwardPhysicalRefreshUnavailable(
            "empty physical delta is a no-op; discard the candidate without publication"
        )
    supersessions = tuple(delta_manifest.get("supersessions") or ())
    rows = _persisted_rows(db, rows, parent=parent, target=target) if rows else ()
    if supersessions:
        raise ForwardPhysicalRefreshUnavailable(
            "incremental physical refresh supports forward facts only; supersession requires maintenance"
        )
    if rows:
        _assert_forward_only(
            rows,
            parent_cutoff=parent.cutoff,
            target_cutoff=target.cutoff,
            supersessions=supersessions,
        )
    if _phase_tracker is not None:
        _phase_tracker.complete("validation")
        _phase_tracker.begin("custody")
    # Transfer custody events can be discovered while ingesting an exact
    # recorder re-pull.  Fold only that explicit SLE-linked tail before any
    # compact payload reader runs; unrelated/local tails remain fail-closed.
    custody_event_rows = apply_bounded_current_material_custody_events(
        db,
        parent_generation_id=int(parent.id),
        target_generation_id=int(target.id),
        source_sle_ids=tuple(dict.fromkeys(
            tuple(int(row.id) for row in rows) + custody_source_ids
        )),
    )
    if _phase_tracker is not None:
        _phase_tracker.complete("custody")
        _phase_tracker.begin("scope")
    keys = _affected_keys(rows)
    current_owners = _current_owner_rows(
        db, rows, planning_pool_by_warehouse=planning_pool_by_warehouse,
    )
    scopes = _current_scopes(
        rows,
        planning_pool_by_warehouse=planning_pool_by_warehouse,
        current_owners=current_owners,
    )
    revision = int(source_revision)
    target_batch = int(target.physical_import_batch_id or 0)
    if target_batch <= 0:
        raise ForwardPhysicalRefreshUnavailable("target import boundary is missing")
    if _phase_tracker is not None:
        _phase_tracker.complete("scope")

    start_phase("stock")
    apply_bounded_current_stock_bins(
        db,
        target_generation_id=int(target.id),
        parent_generation_id=int(parent.id),
        affected_physical_keys=keys,
        delta_manifest=BoundedPhysicalDeltaManifest(
            new_sle_ids=tuple(int(row.id) for row in rows),
            supersession_edge_ids=(),
        ),
    )
    phase("stock")

    make_scopes = tuple(scope for scope in scopes if scope[4] == "make")
    buy_scopes = tuple(scope for scope in scopes if scope[4] == "buy")
    make_result = None
    start_phase("make")
    if make_scopes:
        make_result = apply_current_replenishment_for_bounded_make_scopes(
            db,
            target_generation_id=int(target.id),
            parent_generation_id=int(parent.id),
            target_cutoff=target.cutoff,
            affected_scopes=make_scopes,
            source_revision=revision,
        )
        phase("make")
    elif _phase_tracker is not None:
        _phase_tracker.complete("make")

    supplier_rows = _mapped_supplier_rows(
        rows,
        planning_pool_by_warehouse=planning_pool_by_warehouse,
        current_owners=current_owners,
    )
    supplier_ids = tuple(int(row.id) for row in supplier_rows)
    buy_manifest = BoundedBuyReceiptDeltaManifest()
    buy_result = None
    if supplier_ids:
        start_phase("supplier_manifest")
        if phase_hook is not None:
            phase_hook("supplier_manifest")
        buy_manifest = build_bounded_supplier_receipt_manifest(
            db,
            parent_generation_id=int(parent.id),
            target_generation_id=int(target.id),
            target_cutoff=target.cutoff,
            odata_client=odata_client,
            changed_sle_ids=supplier_ids,
            affected_scopes=buy_scopes,
        )
        if _phase_tracker is not None:
            _phase_tracker.complete("supplier_manifest")
        start_phase("buy")
        buy_result = apply_current_replenishment_for_bounded_buy_scopes(
            db,
            target_generation_id=int(target.id),
            parent_generation_id=int(parent.id),
            target_cutoff=target.cutoff,
            affected_scopes=buy_scopes,
            source_revision=revision,
            delta_manifest=buy_manifest,
        )
    else:
        start_phase("buy")
    phase("buy")

    start_phase("assembly_output")
    output = apply_bounded_assembly_output_plan_execution(
        db,
        target_generation_id=int(target.id),
        parent_generation_id=int(parent.id),
        affected_sle_ids=tuple(int(row.id) for row in rows if _text(row.movement_kind) == "assembly_in"),
        affected_physical_scopes=tuple((key[0], key[1], key[2]) for key in keys),
        earliest_posting_at=min((row.posting_at for row in rows), default=None),
        source_revision=str(revision),
    )
    phase("assembly_output")

    start_phase("assembly_payload")
    assembly_payload = build_compact_current_assembly_payload(
        db,
        target_generation_id=int(target.id),
        parent_generation_id=int(parent.id),
        affected_physical_keys=keys,
    )
    if _phase_tracker is not None:
        _phase_tracker.complete("assembly_payload")
    start_phase("drum_payload")
    drum_payload = build_compact_current_drum_payload(
        db,
        target_generation_id=int(target.id),
        parent_generation_id=int(parent.id),
        assembly_payload=assembly_payload,
    )
    if _phase_tracker is not None:
        _phase_tracker.complete("drum_payload")
    start_phase("shelf_payload")
    shelf_payload = build_compact_current_shelf_payload(
        db,
        target_generation_id=int(target.id),
        parent_generation_id=int(parent.id),
        drum_payload=drum_payload,
    )
    if _phase_tracker is not None:
        _phase_tracker.complete("shelf_payload")
    run_ids = _fixed_run_ids(db)
    start_phase("production_payload")
    production_payload = build_compact_current_production_control_payload(
        db,
        target_generation_id=int(target.id),
        parent_generation_id=int(parent.id),
        assembly_payload=assembly_payload,
        drum_payload=drum_payload,
        shelf_payload=shelf_payload,
        accepted_run_ids=run_ids,
        affected_item_ids=tuple(sorted({int(row.item_id) for row in rows})),
    )
    if _phase_tracker is not None:
        _phase_tracker.complete("production_payload")
    start_phase("purchase_payload")
    purchase_payload = build_compact_current_purchase_control_payload(
        db,
        target_generation_id=int(target.id),
        parent_generation_id=int(parent.id),
        accepted_run_ids=run_ids,
        # The current writer requires a complete payload.  Reuse unchanged
        # parent rows/cards and recompute only the stable BUY scopes touched
        # by this physical delta; an empty set is a true current-manifest
        # reuse and does not traverse purchase/custody history.
        affected_scopes=buy_scopes,
        reuse_parent_current=True,
    )
    if _phase_tracker is not None:
        _phase_tracker.complete("purchase_payload")
    phase("payloads")

    replayed_rows = (
        len(rows)
        + int(custody_event_rows)
        + int(getattr(make_result, "fact_rows", 0) or 0)
        + int(getattr(buy_result, "replayed_rows", 0) or 0)
        + int(getattr(output, "metrics", {}).get("replayed_fact_rows", 0) or 0)
    )

    start_phase("provenance")
    handoff_current_physical_refresh_provenance(
        db,
        parent_generation_id=int(parent.id),
        target_generation_id=int(target.id),
    )
    phase("provenance")

    start_phase("accepted")
    target.capabilities = {
        **dict(parent.capabilities or {}),
        **dict(target.capabilities or {}),
        "physical_ledger": True,
    }
    target.source_watermarks = {
        **dict(target.source_watermarks or {}),
        "physical_refresh_delta": {
            "input_delta_rows": len(rows),
            "replayed_rows": replayed_rows,
            "affected_scopes": [":".join(str(part) for part in scope) for scope in scopes],
        },
    }
    target.status = "accepted"
    target.accepted_at = target.accepted_at or datetime.now(timezone.utc)
    target.reason = None
    db.flush()
    phase("accepted")

    start_phase("queue")
    queue_changed, readiness_changed = _publish_assembly_current(
        db, target_id=int(target.id), assembly_payload=assembly_payload, revision=f"physical:g{target.id}:{revision}"
    )
    phase("queue")
    # Resolve compact dependent queue ids only after queue publication.
    start_phase("dependents")
    drum_rows = resolve_compact_queue_owner_ids(db, tuple(_member(drum_payload, "rows") or ()))
    publish_current_execution_scope(
        db,
        source_revision=f"physical:g{target.id}:{revision}",
        source_generation_id=int(target.id),
        scope_key="drum:all-live-plans",
        rows=drum_rows,
        entity_kinds=("drum_schedule", "drum_slot", "drum_gap", "drum_excluded"),
        summary=dict(_member(drum_payload, "metrics", {}) or {}),
    )
    publish_current_execution_scope(
        db,
        source_revision=f"physical:g{target.id}:{revision}",
        source_generation_id=int(target.id),
        scope_key="shelf:all-live-mrps",
        rows=tuple(_member(shelf_payload, "rows") or ()),
        entity_kinds=("shelf_projection",),
        summary=dict(_member(shelf_payload, "metrics", {}) or {}),
    )
    phase("dependents")
    start_phase("obligations")
    production_result = publish_current_production_control_from_payload(db, int(target.id), production_payload)
    purchase_result = publish_current_purchase_control_from_payload(db, int(target.id), purchase_payload)
    phase("obligations")

    # CAS pointer switch is deliberately the last business mutation.
    start_phase("pointer")
    publish_generation(db, target, expected_parent_id=int(parent.id))
    phase("pointer")
    return PhysicalRefreshCurrentPublishResult(
        target_generation_id=int(target.id),
        parent_generation_id=int(parent.id),
        affected_scopes=tuple(":".join(str(part) for part in scope) for scope in scopes),
        input_delta_rows=len(rows),
        replayed_rows=replayed_rows,
        queue_changed_rows=queue_changed,
        readiness_changed_rows=readiness_changed,
        production_changed_rows=int(production_result.changed_rows),
        purchase_changed_rows=int(purchase_result.changed_rows),
    )


__all__ = [
    "ForwardPhysicalRefreshUnavailable",
    "PhysicalRefreshCurrentPublishResult",
    "physical_refresh_last_failure_status",
    "physical_refresh_phase_status",
    "publish_forward_physical_refresh_current",
]

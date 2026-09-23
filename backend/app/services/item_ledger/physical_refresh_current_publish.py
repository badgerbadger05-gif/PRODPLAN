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
    assembly_queue_scope_summary,
    build_compact_current_assembly_payload,
    get_current_execution_scope,
    publish_current_execution_scope,
    publish_current_obligation_views_from_generation,
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
    lost_supplier_receipt_provenance_sle_ids,
    build_bounded_supplier_receipt_manifest,
    is_supplier_document_type,
)
from app.services.mrp_result_projection import build_mrp_result_current_payload
from app.services.planning_truth import publication_context, publish_generation
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


def _rows(delta_manifest: Mapping[str, Any], name: str = "rows") -> tuple[Any, ...]:
    rows = tuple(delta_manifest.get(name) or ())
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


def _backdate_boundary(delta_manifest: Mapping[str, Any]) -> datetime | None:
    """Read the declared earliest changed boundary of a bounded replay.

    A boundary is the operator-visible proof that this refresh knows how far
    back the correction reaches.  Without it a backdated fact or a supersession
    stays fail-closed: the bounded writers would otherwise fold a delta onto a
    basis that never contained the affected prefix.
    """
    raw = delta_manifest.get("backdate_from")
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise ForwardPhysicalRefreshUnavailable(
            "physical delta backdate boundary is malformed"
        ) from exc


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
    backdate_from: datetime | None = None,
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
            if backdate_from is None:
                raise ForwardPhysicalRefreshUnavailable("incremental physical refresh supports forward facts only; backdate requires maintenance")
            if _comparable(posting_at) < _comparable(backdate_from):
                raise ForwardPhysicalRefreshUnavailable(
                    f"physical delta row {int(row.id)} precedes the declared bounded "
                    "backdate boundary"
                )
        if _comparable(posting_at) > _comparable(target.cutoff):
            raise ForwardPhysicalRefreshUnavailable("physical delta row is after target cutoff")
    return tuple(sorted(persisted, key=lambda row: int(row.id)))


def _persisted_basis_rows(
    db: Session,
    rows: Sequence[Any],
    *,
    parent: models.LedgerGeneration,
    target: models.LedgerGeneration,
    supersessions: Sequence[Any],
) -> tuple[Any, ...]:
    """Resolve accepted facts that this refresh removes from the basis.

    Only a fact named as the ``old_sle_id`` of a declared supersession edge may
    enter the bounded scope derivation from outside the target import window.
    Anything else would silently widen the replay beyond the proven manifest.
    """
    if not rows:
        return ()
    if not supersessions:
        raise ForwardPhysicalRefreshUnavailable(
            "physical delta basis rows require declared supersession edges"
        )
    old_ids = {int(edge.old_sle_id) for edge in supersessions if edge.old_sle_id is not None}
    ids = tuple(int(row.id) for row in rows)
    foreign = sorted(set(ids) - old_ids)
    if foreign:
        raise ForwardPhysicalRefreshUnavailable(
            "physical delta basis row is not a declared superseded fact "
            f"(sle_ids={foreign})"
        )
    persisted = tuple(db.query(models.StockLedgerEntry).filter(
        models.StockLedgerEntry.id.in_(ids),
    ).all())
    if {int(row.id) for row in persisted} != set(ids):
        raise ForwardPhysicalRefreshUnavailable(
            "physical delta references a missing superseded SLE"
        )
    lower = int(parent.physical_import_batch_id or 0)
    for row in persisted:
        if row.posting_at is None:
            raise ForwardPhysicalRefreshUnavailable(
                "superseded fact has no posting boundary"
            )
        if int(row.ingest_batch_id or 0) > lower:
            raise ForwardPhysicalRefreshUnavailable(
                "superseded fact outside the parent basis must travel as a delta row"
            )
        if _comparable(row.posting_at) > _comparable(target.cutoff):
            raise ForwardPhysicalRefreshUnavailable(
                "superseded fact is after target cutoff"
            )
    return tuple(sorted(persisted, key=lambda row: int(row.id)))


# These are ordinary stock movements.  Only the canonical supplier document
# types enter an R4 allocation writer.
_SUPPORTED_MOVEMENT_KINDS = frozenset({
    "assembly_in", "assembly_out", "transfer_in", "transfer_out",
    "receipt", "expense",
})


def _assert_supported_delta(
    rows: Sequence[Any],
    *,
    parent_cutoff: datetime,
    target_cutoff: datetime,
    backdate_from: datetime | None,
) -> None:
    for row in rows:
        posting_at = getattr(row, "posting_at", None)
        if posting_at is None:
            raise ForwardPhysicalRefreshUnavailable(
                "physical delta row is inactive or has no posting boundary"
            )
        if _comparable(posting_at) <= _comparable(parent_cutoff):
            if backdate_from is None:
                raise ForwardPhysicalRefreshUnavailable(
                    "incremental physical refresh supports forward facts only; backdate requires maintenance"
                )
            if _comparable(posting_at) < _comparable(backdate_from):
                raise ForwardPhysicalRefreshUnavailable(
                    f"physical delta row {int(row.id)} precedes the declared bounded "
                    "backdate boundary"
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
        if kind not in _SUPPORTED_MOVEMENT_KINDS:
            raise ForwardPhysicalRefreshUnavailable(
                f"incremental physical refresh movement kind is unsupported: {kind or '<empty>'}"
            )


def _assert_supported_basis(rows: Sequence[Any]) -> None:
    """A removed accepted fact must still be a kind this refresh can scope."""
    for row in rows:
        kind = _text(getattr(row, "movement_kind", ""))
        if kind == CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE:
            continue
        if kind not in _SUPPORTED_MOVEMENT_KINDS:
            raise ForwardPhysicalRefreshUnavailable(
                "superseded fact movement kind is unsupported: "
                f"{kind or '<empty>'} (sle_id={int(row.id)})"
            )


def _assert_superseded_scopes_are_resolvable(
    db: Session,
    *,
    basis_rows: Sequence[Any],
    scopes: Sequence[tuple[int, str, str, str, str]],
) -> None:
    """Fail closed when a correction removes an allocation we cannot scope.

    A superseded fact that still carries a current replenishment assignment
    must fall inside a derived distribution scope, otherwise the bounded
    replay would leave an orphaned assignment behind while reporting success.
    """
    ids = [int(row.id) for row in basis_rows]
    if not ids:
        return
    known = set(scopes)
    rows = (
        db.query(
            models.ReservationConsumptionAllocation.sle_id,
            models.ReservationEntry.item_id,
            models.ReservationEntry.characteristic_ref,
            models.ReservationEntry.organization_ref,
            models.ReservationEntry.planning_stock_pool,
            models.ReservationEntry.realization_mode,
        )
        .join(
            models.ReservationEntry,
            models.ReservationEntry.id
            == models.ReservationConsumptionAllocation.reservation_id,
        )
        .filter(
            models.ReservationConsumptionAllocation.is_current.is_(True),
            models.ReservationConsumptionAllocation.sle_id.in_(sorted(ids)),
        )
        .all()
    )
    for sle_id, item_id, characteristic, organization, pool, mode in rows:
        realization = _text(mode)
        scope = (
            int(item_id), _text(characteristic), _text(organization), _text(pool),
            "make" if realization == "rework" else realization,
        )
        if scope not in known:
            raise ForwardPhysicalRefreshUnavailable(
                "supersession of allocated fact "
                f"{int(sle_id)} has no resolvable distribution scope "
                f"({':'.join(str(part) for part in scope)}); "
                "explicit maintenance replay is required"
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
            scopes.update(_make_scopes_for_assembly_row(row, current_owners))

    # Supplier receipts enter BUY only when the physical warehouse is inside
    # the configured planning contour.  Unmapped warehouses are legitimate
    # stock-only receipts (for example surplus outside the planning contour).
    for row in rows:
        scope = _buy_scope_for_receipt(
            row,
            planning_pool_by_warehouse=planning_pool_by_warehouse,
            current_owners=current_owners,
        )
        if scope is not None:
            scopes.add(scope)
    return tuple(sorted(scopes))


def _canonical_scope(value: Any, mode: str) -> tuple[int, str, str, str, str]:
    """The distribution scope of a fact or an owner, through the one pool key.

    ``mrp_freeze.distribution_scope_for`` is that key; every frozen
    reservation column was written through it, so a physical row has to be
    keyed the same way before the two can be compared.  Comparing raw columns
    was a second rule and it never matched live data.
    """
    from app.services.mrp_freeze import distribution_scope_for

    return distribution_scope_for(
        int(value.item_id),
        _text(getattr(value, "characteristic_ref", "")),
        _text(getattr(value, "organization_ref", "")),
        mode=mode,
    )


def _owner_scopes_for(
    row: Any,
    current_owners: Sequence[models.ReservationEntry],
    modes: set[str],
    *,
    scope_mode: str,
) -> tuple[tuple[int, str, str, str, str], ...]:
    """Scopes of the current owners of this row's item, in the given modes.

    ``modes`` selects which owners this kind of fact can realize; the scope
    itself is emitted in ``scope_mode``.  Canon §18 says an accepted
    ``assembly_in`` closes a ``rework`` reserve as well as a ``make`` one, and
    it is the same physical fact either way - so rework owners join the MAKE
    scope and the allocator settles them addressed-then-FIFO with the make
    ones, instead of a second scope replaying the same fact a second time.
    """
    return tuple({
        _canonical_scope(owner, scope_mode)
        for owner in current_owners
        if int(owner.item_id) == int(row.item_id)
        and _text(owner.realization_mode) in modes
    })


def _single_scope_or_fail(
    row: Any,
    scopes: tuple[tuple[int, str, str, str, str], ...],
) -> tuple[int, str, str, str, str] | None:
    """One scope, or fail closed with the one ambiguity verdict."""
    if not scopes:
        return None
    if len(scopes) > 1:
        raise ForwardPhysicalRefreshUnavailable(
            f"facts for item {int(row.item_id)} have ambiguous distribution pools"
        )
    return scopes[0]


def _make_scopes_for_assembly_row(
    row: Any,
    current_owners: Sequence[models.ReservationEntry],
) -> tuple[tuple[int, str, str, str, str], ...]:
    """The MAKE/rework scopes one ``assembly_in`` fact realizes, if any.

    An assembly fact without a current owner in those modes is a valid
    stock/output-only fact: no pool is invented from the physical warehouse
    and the R4 writer is not invoked for it.

    Both sides are keyed through the canonical collapse, so a fact with a real
    characteristic and the 1C organization still reaches the owner that was
    frozen with the collapsed key.  ``rework`` owners are realized in this
    same MAKE scope (canon §18), so one physical output is replayed once and
    settled across make and rework reserves by the canonical allocator.

    At most one scope, and a second one is the single ambiguity verdict this
    module gives.
    """
    scope = _single_scope_or_fail(
        row,
        _owner_scopes_for(
            row, current_owners, {"make", "rework"}, scope_mode="make",
        ),
    )
    return () if scope is None else (scope,)


def _buy_scope_for_receipt(
    row: Any,
    *,
    planning_pool_by_warehouse: Mapping[str, str],
    current_owners: Sequence[models.ReservationEntry],
) -> tuple[int, str, str, str, str] | None:
    """The BUY distribution scope one supplier receipt belongs to, if any.

    Two rules, and only one of them looks at the physical row.  The warehouse
    decides *whether* the receipt is planning-relevant at all: outside the
    configured contour it is a legitimate stock-only receipt.  The scope
    itself is the canonical pool key of the item's current BUY owner - the
    same key the reservation was frozen with, and the same one the
    generation-wide writer resolves
    (``current_replenishment.apply_current_replenishment_for_accepted_generation``,
    which attaches receipts to reservations by item and states the reason: a
    physical receipt carries no pool identity).
    """
    if not _is_supplier_receipt(row):
        return None
    warehouse = _text(row.warehouse_ref1c)
    # Only membership is used, never the mapped value: today every contour
    # warehouse maps to the one canonical pool (``pool_key_for``), so the
    # value would be redundant and comparing it would be a second rule.  It
    # becomes meaningful again when multi-pool support widens that function.
    if not warehouse or not _text(planning_pool_by_warehouse.get(warehouse)):
        return None
    return _single_scope_or_fail(
        row, _owner_scopes_for(row, current_owners, {"buy"}, scope_mode="buy"),
    )


def _typed_supplier_sle_ids(
    db: Session,
    target_generation_id: int,
    rows: Sequence[Any],
) -> set[int]:
    """Which of these delta rows the target already owns typed evidence for."""
    ids = sorted({int(row.id) for row in rows})
    if not ids:
        return set()
    return {
        int(value)
        for (value,) in db.query(
            models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id
        ).filter(
            models.StockLedgerSupplierReceiptProvenance.ledger_generation_id
            == int(target_generation_id),
            models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id.in_(ids),
        )
    }


def _untyped_buy_owned_receipt_ids(
    rows: Sequence[Any],
    *,
    current_owners: Sequence[models.ReservationEntry],
    planning_pool_by_warehouse: Mapping[str, str],
    typed_sle_ids: set[int],
) -> tuple[int, ...]:
    """Delta receipts owed to a current BUY order that nothing has typed.

    Bounded to this refresh's own delta, like the MAKE gate beside it.  CANON
    "Объём вычислений штатного физического refresh" makes the delta the unit
    of work, and scanning the whole visible prefix also judged history the
    refresh is not responsible for: a receipt that predates its item's BUY
    owner was covered at freeze time (``covered_from_stock_at_freeze``), not
    by replenishment, and would have blocked every future refresh for ever.
    The historical count stays visible - the migration postflight reports it
    as ``pre_deploy_backlog`` - but it is information, not a verdict.

    Uses the same resolver as the delta itself, so the gate cannot disagree
    with the publisher about which receipts belong to a BUY scope.
    """
    return tuple(
        int(row.id) for row in rows
        if int(row.id) not in typed_sle_ids
        and _buy_scope_for_receipt(
            row,
            planning_pool_by_warehouse=planning_pool_by_warehouse,
            current_owners=current_owners,
        ) is not None
    )


def _mapped_supplier_rows(
    rows: Sequence[Any],
    *,
    planning_pool_by_warehouse: Mapping[str, str],
    current_owners: Sequence[models.ReservationEntry],
) -> tuple[Any, ...]:
    """Return only supplier receipts that resolve to a current BUY scope."""
    return tuple(
        row for row in rows
        if _buy_scope_for_receipt(
            row,
            planning_pool_by_warehouse=planning_pool_by_warehouse,
            current_owners=current_owners,
        ) is not None
    )


def _fixed_run_ids(db: Session) -> tuple[int, ...]:
    return tuple(int(value) for (value,) in db.query(models.PlanningRun.run_id).filter(
        models.PlanningRun.status == "FIXED_SNAPSHOT",
    ).order_by(models.PlanningRun.run_id.asc()).all())


def build_period_plan_execution_current_payloads(
    db: Session,
    *,
    generation_id: int,
    run_ids: Sequence[int],
) -> Mapping[str, Any]:
    """Build the canonical period-execution current payloads for one generation.

    ``period_plan_service`` imports the ledger publishers, so this module-level
    seam keeps the canonical builder reachable (and patchable in tests) without
    creating an import cycle.
    """
    from app.services.period_plan_service import (
        build_period_plan_execution_current_payloads_for_generation,
    )

    return build_period_plan_execution_current_payloads_for_generation(
        db,
        int(generation_id),
        run_ids=tuple(int(value) for value in run_ids),
    )


def _build_obligation_view_payloads(
    db: Session,
    *,
    generation_id: int,
    run_ids: Sequence[int],
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Build the MRP and period current payloads with the canonical builders.

    The bounded refresh advances the accepted pointer, and the current readers
    of ``mrp_result``/``period_plan_execution`` require their manifest to match
    that pointer exactly.  Republishing them from the same canonical builders
    used by the full accept path is therefore part of every publication, not an
    optional extra.

    Those builders run inside the publication context (§40), so the age gate
    does not apply to them: the physical refresh is the only mechanism that
    makes the pointer fresh, and refusing to build on an old pointer is a
    self-deadlock.  Coherence with the exact pointer, its capabilities and any
    operator invalidation are still enforced; only the clock is ignored, and
    only inside a publication.
    """
    mrp_payloads = {
        str(run_id): build_mrp_result_current_payload(db, int(run_id))
        for run_id in sorted({int(value) for value in run_ids})
    }
    period_payloads = build_period_plan_execution_current_payloads(
        db, generation_id=int(generation_id), run_ids=run_ids,
    )
    return mrp_payloads, period_payloads


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
        # The scope has one summary contract, owned by
        # ``assembly_queue_scope_summary``.  Writing a partial one here
        # overwrote ``total_queue_qty`` on every bounded refresh and the
        # reader - which requires it - 503'd until the next full accept.
        summary=assembly_queue_scope_summary(queue_rows),
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
        # §40: this publication is what makes the pointer fresh, so nothing
        # inside it may be gated on the pointer's age.
        with publication_context():
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
    backdate_from = _backdate_boundary(delta_manifest)
    if supersessions and backdate_from is None:
        raise ForwardPhysicalRefreshUnavailable(
            "incremental physical refresh supports forward facts only; supersession requires maintenance"
        )
    rows = (
        _persisted_rows(
            db, rows, parent=parent, target=target, backdate_from=backdate_from,
        )
        if rows else ()
    )
    basis_rows = _persisted_basis_rows(
        db,
        _rows(delta_manifest, "basis_rows"),
        parent=parent,
        target=target,
        supersessions=supersessions,
    )
    if rows:
        _assert_supported_delta(
            rows,
            parent_cutoff=parent.cutoff,
            target_cutoff=target.cutoff,
            backdate_from=backdate_from,
        )
    _assert_supported_basis(basis_rows)
    # The bounded replay scope must cover both the facts this refresh adds and
    # the accepted facts it removes; folding only the former would leave the
    # removed fact's key and assignments behind.
    scoped_rows = tuple(rows) + tuple(basis_rows)
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
    keys = _affected_keys(scoped_rows)
    current_owners = _current_owner_rows(
        db, scoped_rows, planning_pool_by_warehouse=planning_pool_by_warehouse,
    )
    scopes = _current_scopes(
        scoped_rows,
        planning_pool_by_warehouse=planning_pool_by_warehouse,
        current_owners=current_owners,
    )
    _assert_superseded_scopes_are_resolvable(
        db, basis_rows=basis_rows, scopes=scopes,
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
            supersession_edge_ids=tuple(int(edge.id) for edge in supersessions),
            backdate_from=backdate_from,
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
            # R4 markers carry the publishing generation, not the import
            # batch (``current_replenishment.GENERATION_REVISION``).
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
    superseded_supplier_ids = tuple(
        int(row.id) for row in basis_rows if _is_supplier_receipt(row)
    )
    buy_manifest = BoundedBuyReceiptDeltaManifest()
    buy_result = None
    if buy_scopes and (supplier_ids or superseded_supplier_ids):
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
            backdate_from=backdate_from,
            supersession_edge_ids=tuple(int(edge.id) for edge in supersessions),
            planning_pool_by_warehouse=planning_pool_by_warehouse,
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
        affected_sle_ids=tuple(
            int(row.id) for row in rows if _text(row.movement_kind) == "assembly_in"
        ),
        affected_physical_scopes=tuple((key[0], key[1], key[2]) for key in keys),
        # A backdated or superseded output moves the earliest boundary back so
        # the canonical document netting still sees the complete document.
        earliest_posting_at=min(
            (row.posting_at for row in scoped_rows), default=None,
        ),
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
        affected_item_ids=tuple(sorted({int(row.item_id) for row in scoped_rows})),
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
    # Build the obligation view payloads while the pointer still names the
    # parent: their canonical builders read the current manifests, which are
    # only coherent before this publication starts advancing them.
    start_phase("obligation_view_payload")
    mrp_payloads, period_payloads = _build_obligation_view_payloads(
        db, generation_id=int(target.id), run_ids=run_ids,
    )
    if _phase_tracker is not None:
        _phase_tracker.complete("obligation_view_payload")
    phase("payloads")

    replayed_rows = (
        len(scoped_rows)
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
            "backdate_from": (
                _comparable(backdate_from).isoformat()
                if backdate_from is not None else None
            ),
            "superseded_facts": len(basis_rows),
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
    # One canonical obligation-view publisher owns all four manifests.  A
    # bounded refresh that advanced only production/purchase left mrp_result
    # and period_plan_execution pinned to the previous generation, and every
    # reader of those two scopes fails closed against the advanced pointer.
    obligation_results = publish_current_obligation_views_from_generation(
        db,
        int(target.id),
        purchase_payload=purchase_payload,
        production_payload=production_payload,
        mrp_payloads=mrp_payloads,
        period_payloads=period_payloads,
        period_run_ids=run_ids,
    )
    production_result = obligation_results["production_control_journal"]
    purchase_result = obligation_results["purchase_control_journal"]
    phase("obligations")

    # The generation about to become the pointer must own the complete typed
    # evidence of its own visible prefix.  The current replenishment reader
    # looks only at the pointer, so an uncovered supplier fact is an accepted
    # physical quantity nobody can count - fail closed here rather than
    # publish coverage that silently drops to zero.
    uncovered = lost_supplier_receipt_provenance_sle_ids(
        db,
        ledger_generation_id=int(target.id),
        rows=rows,
        contour=planning_pool_by_warehouse,
        limit=9,
    )
    if uncovered:
        raise ForwardPhysicalRefreshUnavailable(
            f"generation {int(target.id)} does not own supplier receipt "
            f"provenance for its visible supplier facts; first uncovered "
            f"sle_ids={list(uncovered[:8])}"
        )
    # A supplier receipt inside the planning contour for an item that has an
    # active current BUY owner is a receipt against that order.  Untyped, it
    # is counted by nobody: the purchase journal never sees it arrive.  This
    # is the second half of the same gate - the first half catches evidence
    # the system lost, this one catches evidence it never wrote.
    unallocatable = _untyped_buy_owned_receipt_ids(
        rows,
        current_owners=current_owners,
        planning_pool_by_warehouse=planning_pool_by_warehouse,
        typed_sle_ids=_typed_supplier_sle_ids(db, int(target.id), rows),
    )
    if unallocatable:
        raise ForwardPhysicalRefreshUnavailable(
            f"generation {int(target.id)} leaves {len(unallocatable)} supplier "
            "receipts untyped although their items have an active current BUY "
            f"owner; first sle_ids={list(unallocatable[:8])}"
        )
    # The MAKE mirror.  An accepted assembly output is a receipt into stock and
    # it extinguishes demand; a delta output whose item has a live MAKE owner
    # but which this refresh never placed in a MAKE scope was never offered to
    # the writer at all, so its demand silently stays open.  Scoped to the
    # delta because that is what this refresh is responsible for, and because
    # "produced no allocation" is not by itself a defect - a surplus output
    # beyond the open requirement legitimately allocates nothing.
    # With both sides keyed through the canonical collapse, a fact whose item
    # has a live MAKE owner always resolves to a scope - so this can only
    # fire when the publisher and the writer disagree, i.e. when the scope
    # plumbing between them drops a fact.  That is what it guards now, and it
    # is the regression that produced 492 unrealized outputs on the stand.
    published_make_scopes = set(make_scopes)
    unscoped = tuple(
        int(row.id) for row in rows
        if _text(row.movement_kind) == "assembly_in"
        and _text(row.warehouse_ref1c)
        and _text(planning_pool_by_warehouse.get(_text(row.warehouse_ref1c)))
        and _make_scopes_for_assembly_row(row, current_owners)
        and not (
            set(_make_scopes_for_assembly_row(row, current_owners))
            & published_make_scopes
        )
    )
    if unscoped:
        raise ForwardPhysicalRefreshUnavailable(
            f"generation {int(target.id)} leaves {len(unscoped)} assembly "
            "outputs outside every MAKE scope although their items have an "
            f"active current MAKE owner; first sle_ids={list(unscoped[:8])}"
        )

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


# The complete set of compact current scopes owned by one accepted pointer.
CURRENT_EXECUTION_SCOPE_KEYS: tuple[tuple[str, str], ...] = (
    ("assembly_queue", "assembly:all-live-plans"),
    ("assembly_readiness", "assembly:all-live-plans"),
    ("drum_schedule", "drum:all-live-plans"),
    ("drum_slot", "drum:all-live-plans"),
    ("drum_gap", "drum:all-live-plans"),
    ("drum_excluded", "drum:all-live-plans"),
    ("shelf_projection", "shelf:all-live-mrps"),
    ("production_control_journal", "production:all-live-orders"),
    ("purchase_control_journal", "purchase:all-live-plans"),
    ("mrp_result", "mrp:all-live-plans"),
    ("period_plan_execution", "period-plan:all-live-plans"),
)


@dataclass(frozen=True)
class CurrentExecutionScopeRepairResult:
    pointer_generation_id: int
    repaired_scopes: tuple[str, ...]
    changed_rows: int
    closed_rows: int


def current_execution_scopes_needing_repair(
    db: Session,
    *,
    pointer_generation_id: int,
) -> tuple[str, ...]:
    """Name every current scope a reader would reject for the accepted pointer.

    Reference writers (specification import, calendar, rates, resources,
    custody) deliberately invalidate their manifests and rely on the worker to
    republish.  A refresh that publishes nothing therefore has to answer this
    question itself, otherwise an invalidated scope stays fail-closed until the
    next semantic physical delta happens to arrive.
    """
    stale: list[str] = []
    for entity_kind, scope_key in CURRENT_EXECUTION_SCOPE_KEYS:
        manifest = get_current_execution_scope(
            db, entity_kind=entity_kind, scope_key=scope_key,
        )
        if manifest is None:
            # Never published in this contour; a repair cannot invent the
            # missing publication history, so leave it fail-closed.
            continue
        if (
            not bool(manifest.result_ready)
            or int(manifest.source_generation_id or 0) != int(pointer_generation_id)
        ):
            stale.append(f"{entity_kind}:{scope_key}")
    return tuple(stale)


def repair_current_execution_scopes_from_pointer(
    db: Session,
    *,
    pointer_generation_id: int,
    payload_boundary_generation_id: int,
    source_revision: int | str,
) -> CurrentExecutionScopeRepairResult | None:
    """Republish stale/not-ready current scopes from the accepted pointer.

    This is the no-op refresh counterpart of the bounded publisher: the exact
    same compact payload builders and the same canonical publishers, run
    against the accepted pointer generation instead of a new one.  Nothing is
    accepted, no generation is created and the pointer is not moved, so an
    unchanged result is a true no-op for rows and audit.

    ``payload_boundary_generation_id`` is the technical BUILDING candidate the
    refresh already forked.  It contributes only the ``as_of`` cutoff that
    readiness, drum and shelf evaluate against; it is never published and the
    caller discards it as usual.  The legacy per-generation publisher must not
    be used here: a bounded candidate has no staged queue/readiness/drum/shelf
    rows, so that reader would publish an empty scope and close every current
    row.
    """
    with publication_context():
        return _repair_current_execution_scopes_from_pointer(
            db,
            pointer_generation_id=int(pointer_generation_id),
            payload_boundary_generation_id=int(payload_boundary_generation_id),
            source_revision=source_revision,
        )


def _repair_current_execution_scopes_from_pointer(
    db: Session,
    *,
    pointer_generation_id: int,
    payload_boundary_generation_id: int,
    source_revision: int | str,
) -> CurrentExecutionScopeRepairResult | None:
    pointer = db.get(models.PlanningTruthState, 1)
    generation = db.get(models.LedgerGeneration, int(pointer_generation_id))
    if pointer is None or int(pointer.current_generation_id or -1) != int(pointer_generation_id):
        raise ForwardPhysicalRefreshUnavailable(
            "current execution repair requires the accepted truth pointer"
        )
    if generation is None or _text(generation.status) != "accepted":
        raise ForwardPhysicalRefreshUnavailable(
            "current execution repair requires an accepted pointer generation"
        )
    stale = current_execution_scopes_needing_repair(
        db, pointer_generation_id=int(generation.id),
    )
    if not stale:
        return None
    boundary = db.get(models.LedgerGeneration, int(payload_boundary_generation_id))
    if boundary is None or _text(boundary.status) != "building":
        raise ForwardPhysicalRefreshUnavailable(
            "current execution repair requires a BUILDING payload boundary"
        )
    if (
        boundary.cutoff is None
        or generation.cutoff is None
        or _comparable(boundary.cutoff) < _comparable(generation.cutoff)
    ):
        raise ForwardPhysicalRefreshUnavailable(
            "current execution repair boundary is invalid"
        )
    if int(boundary.id) == int(generation.id):
        raise ForwardPhysicalRefreshUnavailable(
            "current execution repair boundary must differ from the pointer"
        )

    lock_current_custody_marker(db)
    run_ids = _fixed_run_ids(db)
    assembly_payload = build_compact_current_assembly_payload(
        db,
        target_generation_id=int(boundary.id),
        parent_generation_id=int(generation.id),
        affected_physical_keys=(),
    )
    drum_payload = build_compact_current_drum_payload(
        db,
        target_generation_id=int(boundary.id),
        parent_generation_id=int(generation.id),
        assembly_payload=assembly_payload,
    )
    shelf_payload = build_compact_current_shelf_payload(
        db,
        target_generation_id=int(boundary.id),
        parent_generation_id=int(generation.id),
        drum_payload=drum_payload,
    )
    production_payload = build_compact_current_production_control_payload(
        db,
        target_generation_id=int(boundary.id),
        parent_generation_id=int(generation.id),
        assembly_payload=assembly_payload,
        drum_payload=drum_payload,
        shelf_payload=shelf_payload,
        accepted_run_ids=run_ids,
        affected_item_ids=(),
    )
    purchase_payload = build_compact_current_purchase_control_payload(
        db,
        target_generation_id=int(boundary.id),
        parent_generation_id=int(generation.id),
        accepted_run_ids=run_ids,
        affected_scopes=(),
        reuse_parent_current=True,
    )
    mrp_payloads, period_payloads = _build_obligation_view_payloads(
        db, generation_id=int(generation.id), run_ids=run_ids,
    )

    revision = f"repair:g{int(generation.id)}:{source_revision}"
    changed = 0
    closed = 0
    queue_changed, readiness_changed = _publish_assembly_current(
        db,
        target_id=int(generation.id),
        assembly_payload=assembly_payload,
        revision=revision,
    )
    changed += int(queue_changed) + int(readiness_changed)
    drum_rows = resolve_compact_queue_owner_ids(
        db, tuple(_member(drum_payload, "rows") or ())
    )
    drum_result = publish_current_execution_scope(
        db,
        source_revision=revision,
        source_generation_id=int(generation.id),
        scope_key="drum:all-live-plans",
        rows=drum_rows,
        entity_kinds=("drum_schedule", "drum_slot", "drum_gap", "drum_excluded"),
        summary=dict(_member(drum_payload, "metrics", {}) or {}),
    )
    shelf_result = publish_current_execution_scope(
        db,
        source_revision=revision,
        source_generation_id=int(generation.id),
        scope_key="shelf:all-live-mrps",
        rows=tuple(_member(shelf_payload, "rows") or ()),
        entity_kinds=("shelf_projection",),
        summary=dict(_member(shelf_payload, "metrics", {}) or {}),
    )
    obligation_results = publish_current_obligation_views_from_generation(
        db,
        int(generation.id),
        purchase_payload=purchase_payload,
        production_payload=production_payload,
        mrp_payloads=mrp_payloads,
        period_payloads=period_payloads,
        period_run_ids=run_ids,
    )
    for result in (drum_result, shelf_result, *obligation_results.values()):
        changed += int(result.changed_rows)
        closed += int(result.closed_rows)
    return CurrentExecutionScopeRepairResult(
        pointer_generation_id=int(generation.id),
        repaired_scopes=stale,
        changed_rows=changed,
        closed_rows=closed,
    )


__all__ = [
    "CURRENT_EXECUTION_SCOPE_KEYS",
    "CurrentExecutionScopeRepairResult",
    "ForwardPhysicalRefreshUnavailable",
    "PhysicalRefreshCurrentPublishResult",
    "current_execution_scopes_needing_repair",
    "physical_refresh_last_failure_status",
    "physical_refresh_phase_status",
    "publish_forward_physical_refresh_current",
    "repair_current_execution_scopes_from_pointer",
]

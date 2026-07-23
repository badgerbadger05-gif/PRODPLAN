"""Fixed-MRP execution ledger — the cycle (increment 3, ``run_ledger_cycle``).

This module owns the ledger CYCLE that rebuilds, from immutable freeze tables
plus the current fact mirror, the explainable ``mrp_execution_allocation`` rows
and the derived ``MrpRequirement.executed_qty`` cache. It replaces the phase-2
per-item FIFO recompute (whose double-count and period-filter bugs it fixes).

Design (blueprint v2 §1/§4/§5/§6/§12)
-------------------------------------
* **Canonical scope** (``_ledger_scope``) — the last FIXED_SNAPSHOT run per
  source_plan_id (NO ``period_to >= today`` filter — an overdue plan does not
  silently close; fixes runs 13/14 invisibility) PLUS CLOSED runs that still
  carry an open requirement.
* **verify_frozen_supply** — recomputes realized/evaporated for every active
  freeze allocation FROM SCRATCH (Δ of the physical line versus its frozen
  fact). Evaporation (a terminal source that never delivered) is recorded into
  ``mrp_drift_event`` (kind='evaporation'); it never materialises drift here
  (that is increment 4 — ``drift_adjustment_qty`` stays 0).
* **_build_execution_allocations** — the Δ-from-baseline budget (the phase-2
  double-count fix): only new facts BEYOND the frozen/realised position enter
  ``executed_qty``. Realising a frozen supplier/WIP allocation is
  ``coverage_realization`` and does NOT count as execution. The classifier
  (§5) splits facts into direct (linked to a requirement) and pool
  (bucket-FIFO, oldest plan first) allocations.
* **executed_qty** — a derived cache: Σ of a requirement's ``kind='execution'``
  allocation rows.

Idempotency
-----------
The inputs are the immutable freeze tables and the (overwrite-snapshot) fact
mirror. Every cycle DELETEs its scope and re-INSERTs; no step reads its own
output; all iterations are totally ordered. Two cycles over unchanged facts
therefore yield an identical allocation payload and identical ``executed_qty``
(only ``cycle_id`` / ``calculated_at`` / PKs differ). The advisory lock shared
with the freeze serialises the maintenance operation on PostgreSQL.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from ..models import (
    MrpDriftEvent,
    MrpExecutionAllocation,
    MrpFreezeAllocation,
    MrpFreezeBaseline,
    MrpFreezeComponent,
    MrpRequirement,
    MrpRequirementBucket,
    PlannedPurchase,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    StockLedgerEntry,
    SupplierOrder,
    SupplierOrderItem,
    SyncLink,
    PhysicalImportBatch,
    LedgerGeneration,
)
from .mrp_freeze import MRP_LEDGER_LOCK_KEY, PoolKey, pool_key_for
from .mrp_stock_helpers import effective_stock_by_item_all
from .period_plan_service import _to_float
from .supplier_order_status import (
    state_counts_in_mrp as _supplier_order_counts_in_mrp,
    state_is_terminal as _supplier_order_is_terminal,
)

EPS = 1e-9
_CANCELLED = "cancelled"
FIXED_SNAPSHOT_STATUS = "FIXED_SNAPSHOT"
CLOSED_STATUS = "CLOSED"
DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"
PURCHASE_ORDER_ENTITY = "Document_ЗаказПоставщику"

# Drift maturity window (v2 §7.1). A shortfall/surplus pool must persist across
# ≥2 cycles AND for at least this many hours before it materialises. Overridable
# via MRP_DRIFT_MATURITY_HOURS (tests / validation set 0 for an immediate top-up).
# Inc6 (design §11а): under STOCK_SOURCE=bin the window is REMOVED — paired
# production + write-off arrive on ONE registrar (inc0), so a shortfall is no
# longer a timing artefact and matures immediately. The legacy path keeps W.
DRIFT_MATURITY_WINDOW_HOURS = 48.0

# Inc6 (design §11а / §2.1): SLE movement kinds that consume the pool contour —
# the actual component consumption read from ledger-1 under STOCK_SOURCE=bin,
# REPLACING the frozen-norm model ``Σ(Δproduced_parent × frozen_norm)``. An
# adjustment/reconcile SLE is deliberately EXCLUDED: it is the out-of-band
# residual, already folded into on_hand (actual_stock), so it surfaces as drift.
_DRIFT_CONSUMPTION_KINDS = ("assembly_out", "writeoff", "transfer_out")


def _drift_maturity_window_hours() -> float:
    raw = os.environ.get("MRP_DRIFT_MATURITY_HOURS")
    if raw is None or str(raw).strip() == "":
        return DRIFT_MATURITY_WINDOW_HOURS
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DRIFT_MATURITY_WINDOW_HOURS


def _coerce_dt(value: Any) -> Optional[datetime]:
    """Best-effort parse of a stored timestamp into a tz-aware datetime (UTC)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

# fact_type by physical source class.
_PRODUCTION_CLASS = "production"
_SUPPLIER_CLASS = "supplier"


def _min_produced(quantity: Any, produced: Any) -> float:
    """Executed output of one production line, clamped to its own quantity."""
    qty = _to_float(quantity)
    done = max(0.0, _to_float(produced))
    return min(qty, done)


def _order_ref(order_ref1c: Any, order_id: Any) -> str:
    ref = str(order_ref1c or "").strip()
    if ref:
        return ref
    return f"local:{int(order_id)}" if order_id is not None else "local"


# ---------------------------------------------------------------------------
# §1 — canonical scope
# ---------------------------------------------------------------------------
@dataclass
class LedgerScope:
    run_ids: List[int]
    runs_by_id: Dict[int, PlanningRun]
    version_by_run: Dict[int, int]
    open_reqs: List[MrpRequirement]
    reqs_by_id: Dict[int, MrpRequirement]
    open_req_ids: Set[int]
    pool_by_req: Dict[int, PoolKey]
    reqs_by_pool: Dict[PoolKey, List[MrpRequirement]]
    pool_items: Set[int]
    buckets_by_req: Dict[int, List[MrpRequirementBucket]]
    anchor_by_pool: Dict[PoolKey, Tuple[float, float, float]]
    freeze_allocs: List[MrpFreezeAllocation]


def _scope_run_ids(db: Session) -> List[int]:
    """Last FIXED_SNAPSHOT run per source_plan_id (NO period_to filter — an
    overdue snapshot stays in scope; fixes runs 13/14) PLUS every CLOSED run
    that still carries an open requirement."""
    fixed_rows = (
        db.query(PlanningRun.source_plan_id, func.max(PlanningRun.run_id))
        .filter(PlanningRun.status == FIXED_SNAPSHOT_STATUS)
        .filter(PlanningRun.source_plan_id.isnot(None))
        .group_by(PlanningRun.source_plan_id)
        .all()
    )
    run_ids: Set[int] = {int(rid) for _plan_id, rid in fixed_rows if rid is not None}

    # CLOSED runs that still carry an open requirement (empty today; the branch
    # is written now so a future closure increment needs no scope change).
    closed_rows = (
        db.query(PlanningRun.run_id)
        .join(MrpRequirement, MrpRequirement.run_id == PlanningRun.run_id)
        .filter(PlanningRun.status == CLOSED_STATUS)
        .filter(MrpRequirement.status == "open")
        .distinct()
        .all()
    )
    run_ids.update(int(rid) for (rid,) in closed_rows if rid is not None)
    return sorted(run_ids)


def _ledger_scope(db: Session) -> LedgerScope:
    run_ids = _scope_run_ids(db)
    if not run_ids:
        return LedgerScope(
            run_ids=[], runs_by_id={}, version_by_run={}, open_reqs=[], reqs_by_id={},
            open_req_ids=set(), pool_by_req={}, reqs_by_pool={}, pool_items=set(),
            buckets_by_req={}, anchor_by_pool={}, freeze_allocs=[],
        )

    runs_by_id = {
        int(r.run_id): r
        for r in db.query(PlanningRun).filter(PlanningRun.run_id.in_(run_ids)).all()
    }
    version_by_run = {
        rid: int(run.active_freeze_version)
        for rid, run in runs_by_id.items()
        if run.active_freeze_version is not None
    }

    open_reqs = (
        db.query(MrpRequirement)
        .filter(MrpRequirement.run_id.in_(run_ids))
        .filter(MrpRequirement.status == "open")
        .order_by(MrpRequirement.id.asc())
        .all()
    )
    reqs_by_id = {int(r.id): r for r in open_reqs}
    open_req_ids = set(reqs_by_id.keys())

    pool_by_req: Dict[int, PoolKey] = {}
    reqs_by_pool: Dict[PoolKey, List[MrpRequirement]] = {}
    pool_items: Set[int] = set()
    for req in open_reqs:
        pk = pool_key_for(int(req.item_id))
        pool_by_req[int(req.id)] = pk
        reqs_by_pool.setdefault(pk, []).append(req)
        pool_items.add(int(req.item_id))

    buckets_by_req: Dict[int, List[MrpRequirementBucket]] = {}
    if open_req_ids:
        for bucket in (
            db.query(MrpRequirementBucket)
            .filter(MrpRequirementBucket.requirement_id.in_(open_req_ids))
            .order_by(
                MrpRequirementBucket.bucket_date.asc(),
                MrpRequirementBucket.id.asc(),
            )
            .all()
        ):
            buckets_by_req.setdefault(int(bucket.requirement_id), []).append(bucket)

    # Anchor: baseline row of the pool with max(frozen_at, run_id) among the
    # scope runs at their active freeze version. No baseline → (0, 0) fallback =
    # full-history Δ (phase-2 parity).
    anchor_by_pool: Dict[PoolKey, Tuple[float, float, float]] = {}
    if pool_items:
        best_by_pool: Dict[PoolKey, Tuple[Any, int]] = {}
        for row in (
            db.query(MrpFreezeBaseline)
            .filter(MrpFreezeBaseline.run_id.in_(run_ids))
            .filter(MrpFreezeBaseline.item_id.in_(pool_items))
            .all()
        ):
            if version_by_run.get(int(row.run_id)) != int(row.freeze_version):
                continue
            pk = pool_key_for(int(row.item_id))
            rank = (row.frozen_at or datetime.min, int(row.run_id))
            prev = best_by_pool.get(pk)
            if prev is None or rank > prev[0]:
                best_by_pool[pk] = (rank, None)  # placeholder
                anchor_by_pool[pk] = (
                    _to_float(row.stock_qty),
                    _to_float(row.produced_total),
                    _to_float(row.received_total),
                )

    freeze_allocs: List[MrpFreezeAllocation] = []
    if open_req_ids:
        for alloc in (
            db.query(MrpFreezeAllocation)
            .filter(MrpFreezeAllocation.run_id.in_(run_ids))
            .filter(MrpFreezeAllocation.requirement_id.in_(open_req_ids))
            .order_by(MrpFreezeAllocation.id.asc())
            .all()
        ):
            if version_by_run.get(int(alloc.run_id)) != int(alloc.freeze_version):
                continue
            freeze_allocs.append(alloc)

    return LedgerScope(
        run_ids=run_ids,
        runs_by_id=runs_by_id,
        version_by_run=version_by_run,
        open_reqs=open_reqs,
        reqs_by_id=reqs_by_id,
        open_req_ids=open_req_ids,
        pool_by_req=pool_by_req,
        reqs_by_pool=reqs_by_pool,
        pool_items=pool_items,
        buckets_by_req=buckets_by_req,
        anchor_by_pool=anchor_by_pool,
        freeze_allocs=freeze_allocs,
    )


# ---------------------------------------------------------------------------
# §3 — verify_frozen_supply
# ---------------------------------------------------------------------------
@dataclass
class FactCandidate:
    """A production/receipt fact competing for a requirement's net."""

    pool: PoolKey
    fact_class: str            # production | supplier
    fact_type: str             # linked_production | unlinked_production | supplier_receipt
    fact_ref: str
    fact_line_ref: str
    fact_date: Optional[datetime]
    qty: float
    owner_req_id: Optional[int] = None      # set for direct (linked) candidates
    origin_req_id: Optional[int] = None      # set for surplus redirected to the pool


@dataclass
class VerifyResult:
    realized_by_alloc_id: Dict[int, float]
    evaporated_by_alloc_id: Dict[int, float]
    realized_by_pool: Dict[PoolKey, Dict[str, float]]
    surplus_candidates: List[FactCandidate]
    allocated_line_keys: Set[Tuple[str, str, str]]


def _freeze_queue_key(alloc: MrpFreezeAllocation, run: Optional[PlanningRun]) -> tuple:
    period_from = (run.period_from if run else None) or date.min
    period_to = (run.period_to if run else None) or date.max
    return (period_from, period_to, int(alloc.run_id), int(alloc.requirement_id), int(alloc.id))


def verify_frozen_supply(db: Session, scope: LedgerScope, cycle_id: str) -> VerifyResult:
    """Recompute realized/evaporated for every active freeze allocation from
    scratch and rebuild the ``kind='evaporation'`` drift events."""
    realized_by_alloc_id: Dict[int, float] = {}
    evaporated_by_alloc_id: Dict[int, float] = {}
    realized_by_pool: Dict[PoolKey, Dict[str, float]] = {}
    surplus_candidates: List[FactCandidate] = []
    allocated_line_keys: Set[Tuple[str, str, str]] = set()

    # Zero every active allocation first (recompute-from-scratch semantics).
    for alloc in scope.freeze_allocs:
        realized_by_alloc_id[int(alloc.id)] = 0.0
        evaporated_by_alloc_id[int(alloc.id)] = 0.0

    # Group by physical line (excluding stock — stock disappearance is drift/§4).
    groups: Dict[Tuple[str, str, str], List[MrpFreezeAllocation]] = {}
    for alloc in scope.freeze_allocs:
        stype = str(alloc.source_type or "")
        if stype == "stock":
            continue
        key = (stype, str(alloc.source_ref or ""), str(alloc.source_line_ref or ""))
        groups.setdefault(key, []).append(alloc)
        allocated_line_keys.add(key)

    # Batch-load the physical lines referenced by the groups.
    wip_pids = [
        int(lref) for (stype, _ref, lref) in groups if stype == "wip_order" and str(lref).isdigit()
    ]
    sup_pks = [
        int(lref) for (stype, _ref, lref) in groups if stype == "supplier_order" and str(lref).isdigit()
    ]
    wip_rows: Dict[int, Any] = {}
    if wip_pids:
        for pp, order, state in (
            db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState)
            .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
            .outerjoin(
                ProductionOrderLineState,
                ProductionOrderLineState.product_id == ProductionProduct.product_id,
            )
            .filter(ProductionProduct.product_id.in_(wip_pids))
            .all()
        ):
            wip_rows[int(pp.product_id)] = (pp, order, state)
    sup_rows: Dict[int, Any] = {}
    if sup_pks:
        for soi, order in (
            db.query(SupplierOrderItem, SupplierOrder)
            .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
            .filter(SupplierOrderItem.item_id.in_(sup_pks))
            .all()
        ):
            sup_rows[int(soi.item_id)] = (soi, order)

    for key, allocs in groups.items():
        stype, source_ref, source_line_ref = key
        pk = pool_key_for(int(allocs[0].item_id))
        fact_at_freeze = _to_float(allocs[0].fact_at_freeze)

        delta_line = 0.0
        terminal = False
        fact_class = _PRODUCTION_CLASS
        fact_type = "unlinked_production"
        fact_date: Optional[datetime] = None

        if stype == "wip_order":
            fact_class = _PRODUCTION_CLASS
            fact_type = "unlinked_production"
            row = wip_rows.get(int(source_line_ref)) if str(source_line_ref).isdigit() else None
            if row is None:
                terminal = True
            else:
                pp, order, state = row
                quantity_now = _to_float(pp.quantity)
                produced_now = max(0.0, _to_float(pp.produced_qty))
                produced_at_freeze_est = max(0.0, quantity_now - fact_at_freeze)
                delta_line = max(0.0, produced_now - produced_at_freeze_est)
                fact_date = order.order_date
                state_key = str(order.order_state_key or "").strip().lower()
                line_status = str((state.status if state else "") or "").strip().lower()
                terminal = (
                    state_key == DONE_STATE_KEY
                    or bool(order.deletion_mark)
                    or line_status == _CANCELLED
                )
        elif stype == "supplier_order":
            fact_class = _SUPPLIER_CLASS
            fact_type = "supplier_receipt"
            row = sup_rows.get(int(source_line_ref)) if str(source_line_ref).isdigit() else None
            if row is None:
                terminal = True
            else:
                soi, order = row
                quantity_now = _to_float(soi.quantity)
                received_now = max(0.0, _to_float(soi.received_qty))
                received_at_freeze_est = max(0.0, quantity_now - fact_at_freeze)
                delta_line = max(0.0, received_now - received_at_freeze_est)
                delta_line = min(delta_line, fact_at_freeze)
                fact_date = soi.delivery_date or order.order_date
                terminal = (
                    _supplier_order_is_terminal(order.order_state_name)
                    or bool(order.deletion_mark)
                )
        else:
            continue

        # Distribute delta_line across the group's allocations in freeze-queue order.
        ordered = sorted(
            allocs, key=lambda a: _freeze_queue_key(a, scope.runs_by_id.get(int(a.run_id)))
        )
        remaining = delta_line
        realized_sum = 0.0
        for alloc in ordered:
            alloc_qty = _to_float(alloc.alloc_qty)
            realized_i = min(alloc_qty, max(0.0, remaining))
            if realized_i < 0.0:
                realized_i = 0.0
            realized_by_alloc_id[int(alloc.id)] = realized_i
            remaining = max(0.0, remaining - realized_i)
            realized_sum += realized_i
            pool_bucket = realized_by_pool.setdefault(pk, {_PRODUCTION_CLASS: 0.0, _SUPPLIER_CLASS: 0.0})
            pool_bucket[fact_class] += realized_i
            if terminal:
                evaporated_by_alloc_id[int(alloc.id)] = max(0.0, alloc_qty - realized_i)

        surplus_line = max(0.0, delta_line - realized_sum)
        if surplus_line > EPS:
            surplus_candidates.append(
                FactCandidate(
                    pool=pk,
                    fact_class=fact_class,
                    fact_type=fact_type,
                    fact_ref=source_ref,
                    fact_line_ref=source_line_ref,
                    fact_date=fact_date,
                    qty=surplus_line,
                )
            )

    # Persist realized/evaporated onto the allocations.
    for alloc in scope.freeze_allocs:
        alloc.realized_qty = round(realized_by_alloc_id.get(int(alloc.id), 0.0), 3)
        alloc.evaporated_qty = round(evaporated_by_alloc_id.get(int(alloc.id), 0.0), 3)

    # Inc4 (PURE SHADOW, dual-write §2.6): the old table (MrpFreezeAllocation)
    # stays the read source; ALSO mirror the freshly-computed realized/evaporated
    # onto the reservation_coverage frozen pins. Wrapped so a shadow failure
    # never changes verify_frozen_supply's legacy result.
    try:
        from .item_ledger.reservation_ledger import mirror_verify_realized

        mirror_verify_realized(db, scope.freeze_allocs)
    except Exception:  # noqa: BLE001 — shadow must never break verify
        logging.getLogger(__name__).exception(
            "Inc4 frozen-pin verify mirror failed; continuing (verify unaffected)"
        )

    _rebuild_evaporation_events(db, scope, cycle_id, evaporated_by_alloc_id)

    return VerifyResult(
        realized_by_alloc_id=realized_by_alloc_id,
        evaporated_by_alloc_id=evaporated_by_alloc_id,
        realized_by_pool=realized_by_pool,
        surplus_candidates=surplus_candidates,
        allocated_line_keys=allocated_line_keys,
    )


def _rebuild_evaporation_events(
    db: Session,
    scope: LedgerScope,
    cycle_id: str,
    evaporated_by_alloc_id: Dict[int, float],
) -> int:
    """Rebuild ``mrp_drift_event`` kind='evaporation' rows for the scope's
    requirements (DELETE by req scope + INSERT per evaporated allocation).
    first_seen_cycle_id is carried from the replaced row of the same
    (req, source-triple); provenance only, never part of a computation."""
    if not scope.open_req_ids:
        return 0
    alloc_by_id = {int(a.id): a for a in scope.freeze_allocs}

    # Capture prior first_seen by (requirement_id, source_type, source_ref,
    # source_line_ref) before deleting.
    prior_first_seen: Dict[Tuple[int, str, str, str], str] = {}
    for ev in (
        db.query(MrpDriftEvent)
        .filter(MrpDriftEvent.kind == "evaporation")
        .filter(MrpDriftEvent.requirement_id.in_(scope.open_req_ids))
        .all()
    ):
        details = ev.details or {}
        key = (
            int(ev.requirement_id) if ev.requirement_id is not None else -1,
            str(details.get("source_type", "")),
            str(details.get("source_ref", "")),
            str(details.get("source_line_ref", "")),
        )
        prior_first_seen[key] = ev.first_seen_cycle_id or ev.cycle_id or cycle_id

    db.query(MrpDriftEvent).filter(
        MrpDriftEvent.kind == "evaporation"
    ).filter(
        MrpDriftEvent.requirement_id.in_(scope.open_req_ids)
    ).delete(synchronize_session=False)

    count = 0
    for alloc_id, evaporated in sorted(evaporated_by_alloc_id.items()):
        if evaporated <= EPS:
            continue
        alloc = alloc_by_id.get(int(alloc_id))
        if alloc is None:
            continue
        pk = pool_key_for(int(alloc.item_id))
        key = (
            int(alloc.requirement_id),
            str(alloc.source_type or ""),
            str(alloc.source_ref or ""),
            str(alloc.source_line_ref or ""),
        )
        first_seen = prior_first_seen.get(key, cycle_id)
        db.add(
            MrpDriftEvent(
                cycle_id=cycle_id,
                item_id=int(alloc.item_id),
                characteristic_ref=pk.characteristic_ref,
                organization_ref=pk.organization_ref,
                planning_stock_pool=pk.planning_stock_pool,
                kind="evaporation",
                drift_qty=round(float(evaporated), 3),
                matured=True,
                first_seen_cycle_id=first_seen,
                requirement_id=int(alloc.requirement_id),
                details={
                    "source_type": str(alloc.source_type or ""),
                    "source_ref": str(alloc.source_ref or ""),
                    "source_line_ref": str(alloc.source_line_ref or ""),
                    "freeze_allocation_id": int(alloc.id),
                    "alloc_qty": _to_float(alloc.alloc_qty),
                    "realized_qty": round(
                        float(alloc.realized_qty) if alloc.realized_qty is not None else 0.0, 3
                    ),
                },
            )
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# §4/§5 — classifier + allocation builder
# ---------------------------------------------------------------------------
def _fact_type_for_coverage(source_type: str) -> str:
    if source_type == "supplier_order":
        return "supplier_receipt"
    return "unlinked_production"


@dataclass
class _Slot:
    req_id: int
    bucket_id: Optional[int]
    bucket_date: Optional[date]
    net_qty: float
    sort_key: tuple


def _effective_net(
    req: MrpRequirement, eff_net_by_req: Optional[Dict[int, float]] = None
) -> float:
    """Closure / bucket-cap demand target. DEFAULT (legacy) = frozen net + drift.

    Inc6 (design §11б): under STOCK_SOURCE=bin the caller passes ``eff_net_by_req``
    (built once per cycle from the reservation ledger's ``uncovered`` + supplier
    term — :func:`item_ledger.reservation_ledger.effective_net_bin`). Legacy stays
    byte-identical: with ``eff_net_by_req`` None the formula is unchanged."""
    if eff_net_by_req is not None:
        val = eff_net_by_req.get(int(req.id))
        if val is not None:
            return val
    return max(_to_float(req.net_required_qty) + _to_float(req.drift_adjustment_qty), 0.0)


def _build_execution_allocations(
    db: Session,
    scope: LedgerScope,
    verify: VerifyResult,
    cycle_id: str,
    produced_now: Dict[int, float],
    received_now: Dict[int, float],
    ledger_generation_id: int,
    eff_net_by_req: Optional[Dict[int, float]] = None,
) -> Tuple[List[MrpExecutionAllocation], Dict[int, float], int, int]:
    """Rebuild ``mrp_execution_allocation`` for the scope. Returns
    (row objects, executed_by_req, execution_row_count, coverage_row_count).

    Inc6: ``eff_net_by_req`` (bin only) overrides the frozen-net demand target so
    the bucket caps / room clamps track the reservation-ledger effective_net."""
    now = datetime.now(timezone.utc)
    rows: List[MrpExecutionAllocation] = []
    exec_by_req: Dict[int, float] = {int(rid): 0.0 for rid in scope.open_req_ids}
    exec_in_bucket: Dict[Tuple[int, Optional[int]], float] = {}
    coverage_count = 0
    execution_count = 0

    # 5.0 — DELETE existing execution allocations for the open-req scope.
    if scope.open_req_ids:
        db.query(MrpExecutionAllocation).filter(
            MrpExecutionAllocation.ledger_generation_id == ledger_generation_id,
            MrpExecutionAllocation.requirement_id.in_(scope.open_req_ids)
        ).delete(synchronize_session=False)

    if not scope.open_req_ids:
        return rows, exec_by_req, 0, 0

    # 5.1 — Δ-from-baseline budget per pool. ``produced_now`` / ``received_now``
    # are computed ONCE at cycle level (mirroring _write_freeze_baseline's exact
    # filters: deletion_mark=false, NO status/source filter) and shared with
    # compute_stock_drift so the two steps see the identical fact aggregate.
    budget_remaining: Dict[PoolKey, Dict[str, float]] = {}
    for pk, reqs in scope.reqs_by_pool.items():
        item_id = int(reqs[0].item_id)
        anchor = scope.anchor_by_pool.get(pk, (0.0, 0.0, 0.0))
        _stock0, produced_total, received_total = anchor
        realized = verify.realized_by_pool.get(pk, {})
        delta_prod = max(0.0, produced_now.get(item_id, 0.0) - produced_total)
        delta_recv = max(0.0, received_now.get(item_id, 0.0) - received_total)
        budget_remaining[pk] = {
            _PRODUCTION_CLASS: max(0.0, delta_prod - realized.get(_PRODUCTION_CLASS, 0.0)),
            _SUPPLIER_CLASS: max(0.0, delta_recv - realized.get(_SUPPLIER_CLASS, 0.0)),
        }

    # 5.2 — coverage_realization rows (realising a frozen allocation; NOT executed).
    alloc_by_id = {int(a.id): a for a in scope.freeze_allocs}
    for alloc_id in sorted(verify.realized_by_alloc_id):
        realized_i = verify.realized_by_alloc_id[alloc_id]
        if realized_i <= EPS:
            continue
        alloc = alloc_by_id.get(int(alloc_id))
        if alloc is None:
            continue
        rows.append(
            MrpExecutionAllocation(
                ledger_generation_id=ledger_generation_id,
                cycle_id=cycle_id,
                requirement_id=int(alloc.requirement_id),
                bucket_id=None,
                fact_type=_fact_type_for_coverage(str(alloc.source_type or "")),
                allocation_kind="coverage_realization",
                fact_ref=str(alloc.source_ref or ""),
                fact_line_ref=str(alloc.source_line_ref or ""),
                fact_date=None,
                allocated_qty=round(float(realized_i), 3),
                freeze_allocation_id=int(alloc.id),
                origin_requirement_id=None,
                calculated_at=now,
            )
        )
        coverage_count += 1

    # ----- gather direct + pool candidates from the fact mirror -----
    direct_candidates, pool_candidates = _classify_facts(db, scope, verify)

    # helper: build the per-req bucket slots (real buckets or one virtual slot).
    def _slots_for_req(req_id: int) -> List[_Slot]:
        req = scope.reqs_by_id[req_id]
        run = scope.runs_by_id.get(int(req.run_id))
        pf = (run.period_from if run else None) or date.min
        ptv = (run.period_to if run else None) or date.max
        buckets = scope.buckets_by_req.get(req_id, [])
        slots: List[_Slot] = []
        if buckets:
            for b in buckets:
                bdate = b.bucket_date or ptv
                slots.append(
                    _Slot(
                        req_id=req_id,
                        bucket_id=int(b.id),
                        bucket_date=bdate,
                        net_qty=_to_float(b.net_qty),
                        sort_key=(bdate, pf, ptv, int(req.run_id), req_id, int(b.id)),
                    )
                )
            # Bucket-cap extension (§4): the frozen bucket nets sum to net, so a
            # positive drift_adjustment must widen a bucket cap or the drift
            # top-up could never execute to effective_net. Give the whole
            # positive adjustment to the earliest (first) bucket. Σ caps then
            # equals effective_net. A negative adjustment is handled by the
            # room_req clamp in _place (effective_net − executed), never by
            # cutting bucket caps.
            adj = max(_to_float(req.drift_adjustment_qty), 0.0)
            if adj > EPS and slots:
                slots[0].net_qty += adj
        else:
            bdate = ptv
            slots.append(
                _Slot(
                    req_id=req_id,
                    bucket_id=None,
                    bucket_date=bdate,
                    net_qty=_effective_net(req, eff_net_by_req),
                    sort_key=(bdate, pf, ptv, int(req.run_id), req_id, -1),
                )
            )
        return slots

    def _place(
        slots: List[_Slot],
        qty: float,
        cand: FactCandidate,
        pk: PoolKey,
    ) -> float:
        """Greedily place ``qty`` into ``slots`` (already ordered), capped by
        each bucket's net, the req's effective net, and the class budget.
        Returns the amount actually placed (execution rows appended)."""
        nonlocal execution_count
        placed_total = 0.0
        for slot in slots:
            if qty <= EPS:
                break
            req = scope.reqs_by_id[slot.req_id]
            room_bucket = slot.net_qty - exec_in_bucket.get((slot.req_id, slot.bucket_id), 0.0)
            room_req = _effective_net(req, eff_net_by_req) - exec_by_req.get(slot.req_id, 0.0)
            room_budget = budget_remaining[pk][cand.fact_class]
            take = min(qty, room_bucket, room_req, room_budget)
            if take <= EPS:
                continue
            rows.append(
                MrpExecutionAllocation(
                    ledger_generation_id=ledger_generation_id,
                    cycle_id=cycle_id,
                    requirement_id=int(slot.req_id),
                    bucket_id=slot.bucket_id,
                    fact_type=cand.fact_type,
                    allocation_kind="execution",
                    fact_ref=cand.fact_ref,
                    fact_line_ref=cand.fact_line_ref,
                    fact_date=cand.fact_date,
                    allocated_qty=round(float(take), 3),
                    freeze_allocation_id=None,
                    origin_requirement_id=cand.origin_req_id,
                    calculated_at=now,
                )
            )
            execution_count += 1
            exec_in_bucket[(slot.req_id, slot.bucket_id)] = (
                exec_in_bucket.get((slot.req_id, slot.bucket_id), 0.0) + take
            )
            exec_by_req[slot.req_id] = exec_by_req.get(slot.req_id, 0.0) + take
            budget_remaining[pk][cand.fact_class] -= take
            qty -= take
            placed_total += take
        return placed_total

    # 5.3 — direct (linked) phase.
    surplus_from_direct: List[FactCandidate] = []
    direct_sorted = sorted(
        direct_candidates,
        key=lambda c: (c.fact_date or datetime.min, c.fact_ref, c.fact_line_ref, c.owner_req_id or -1),
    )
    for cand in direct_sorted:
        req_id = int(cand.owner_req_id)
        if req_id not in scope.reqs_by_id:
            continue
        pk = scope.pool_by_req[req_id]
        take = min(cand.qty, budget_remaining[pk][cand.fact_class])
        if take <= EPS:
            continue
        placed = _place(_slots_for_req(req_id), take, cand, pk)
        overflow = take - placed
        if overflow > EPS:
            surplus_from_direct.append(
                FactCandidate(
                    pool=pk,
                    fact_class=cand.fact_class,
                    fact_type=cand.fact_type,
                    fact_ref=cand.fact_ref,
                    fact_line_ref=cand.fact_line_ref,
                    fact_date=cand.fact_date,
                    qty=overflow,
                    origin_req_id=req_id,
                )
            )

    # 5.4 — pool phase (global bucket-FIFO, oldest plan first).
    all_pool_candidates = list(pool_candidates) + list(verify.surplus_candidates) + surplus_from_direct
    cands_by_pool: Dict[PoolKey, List[FactCandidate]] = {}
    for cand in all_pool_candidates:
        cands_by_pool.setdefault(cand.pool, []).append(cand)

    for pk, cands in cands_by_pool.items():
        reqs = scope.reqs_by_pool.get(pk)
        if not reqs:
            continue
        # Global buckets of the pool, oldest-plan-first at the BUCKET level.
        slots: List[_Slot] = []
        for req in reqs:
            slots.extend(_slots_for_req(int(req.id)))
        slots.sort(key=lambda s: s.sort_key)
        # Deterministic fact order.
        cands.sort(key=lambda c: (c.fact_date or datetime.min, c.fact_ref, c.fact_line_ref))
        remaining_by_cand = [c.qty for c in cands]
        for slot in slots:
            req = scope.reqs_by_id[slot.req_id]
            for idx, cand in enumerate(cands):
                cand_rem = remaining_by_cand[idx]
                if cand_rem <= EPS:
                    continue
                room_bucket = slot.net_qty - exec_in_bucket.get((slot.req_id, slot.bucket_id), 0.0)
                room_req = _effective_net(req, eff_net_by_req) - exec_by_req.get(slot.req_id, 0.0)
                room_budget = budget_remaining[pk][cand.fact_class]
                take = min(cand_rem, room_bucket, room_req, room_budget)
                if take <= EPS:
                    continue
                rows.append(
                    MrpExecutionAllocation(
                        ledger_generation_id=ledger_generation_id,
                        cycle_id=cycle_id,
                        requirement_id=int(slot.req_id),
                        bucket_id=slot.bucket_id,
                        fact_type=cand.fact_type,
                        allocation_kind="execution",
                        fact_ref=cand.fact_ref,
                        fact_line_ref=cand.fact_line_ref,
                        fact_date=cand.fact_date,
                        allocated_qty=round(float(take), 3),
                        freeze_allocation_id=None,
                        origin_requirement_id=cand.origin_req_id,
                        calculated_at=now,
                    )
                )
                execution_count += 1
                exec_in_bucket[(slot.req_id, slot.bucket_id)] = (
                    exec_in_bucket.get((slot.req_id, slot.bucket_id), 0.0) + take
                )
                exec_by_req[slot.req_id] = exec_by_req.get(slot.req_id, 0.0) + take
                budget_remaining[pk][cand.fact_class] -= take
                remaining_by_cand[idx] = cand_rem - take

    for row in rows:
        db.add(row)

    return rows, exec_by_req, execution_count, coverage_count


def _classify_facts(
    db: Session,
    scope: LedgerScope,
    verify: VerifyResult,
) -> Tuple[List[FactCandidate], List[FactCandidate]]:
    """Classify the current production/receipt fact mirror into direct (linked)
    and pool candidates. Allocated physical lines are skipped — verify already
    produced their coverage (realized) and pool surplus."""
    direct: List[FactCandidate] = []
    pool: List[FactCandidate] = []
    if not scope.pool_items:
        return direct, pool

    # ----- production lines -----
    for pp, order, state in (
        db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .filter(ProductionProduct.item_id.in_(scope.pool_items))
        .filter(ProductionOrder.deletion_mark.is_(False))
        .order_by(ProductionProduct.product_id.asc())
        .all()
    ):
        line_status = str((state.status if state else "") or "").strip().lower()
        if line_status == _CANCELLED:
            continue
        item_id = int(pp.item_id)
        pk = pool_key_for(item_id)
        ref = _order_ref(order.order_ref1c, order.order_id)
        line_ref = str(int(pp.product_id))
        if ("wip_order", ref, line_ref) in verify.allocated_line_keys:
            continue  # handled by verify (coverage + surplus)
        claim = _min_produced(pp.quantity, pp.produced_qty)
        if claim <= EPS:
            continue
        source = str(order.source or "").strip().lower()
        src_req_id = int(pp.source_mrp_requirement_id) if pp.source_mrp_requirement_id is not None else None
        fact_date = order.order_date
        if src_req_id is not None and src_req_id in scope.open_req_ids:
            direct.append(
                FactCandidate(
                    pool=pk, fact_class=_PRODUCTION_CLASS, fact_type="linked_production",
                    fact_ref=ref, fact_line_ref=line_ref, fact_date=fact_date,
                    qty=claim, owner_req_id=src_req_id,
                )
            )
        elif src_req_id is not None:
            # owner-req carried/closed → carry stub (increment 6): unlinked pool.
            pool.append(
                FactCandidate(
                    pool=pk, fact_class=_PRODUCTION_CLASS, fact_type="unlinked_production",
                    fact_ref=ref, fact_line_ref=line_ref, fact_date=fact_date, qty=claim,
                )
            )
        elif source == "1c":
            pool.append(
                FactCandidate(
                    pool=pk, fact_class=_PRODUCTION_CLASS, fact_type="unlinked_production",
                    fact_ref=ref, fact_line_ref=line_ref, fact_date=fact_date, qty=claim,
                )
            )
        # source == 'mrp' without a link → ignore (surfaces as drift).

    # ----- receipt lines -----
    own_by_ref = _own_exported_supplier_owner(db, scope)
    for soi, order in (
        db.query(SupplierOrderItem, SupplierOrder)
        .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
        .filter(SupplierOrderItem.item_id_ref.in_(scope.pool_items))
        .filter(SupplierOrder.deletion_mark.is_(False))
        .order_by(SupplierOrderItem.item_id.asc())
        .all()
    ):
        if not _supplier_order_counts_in_mrp(order.order_state_name):
            continue
        item_id = int(soi.item_id_ref)
        pk = pool_key_for(item_id)
        ref = _order_ref(order.order_ref1c, order.order_id)
        line_ref = str(int(soi.item_id))
        if ("supplier_order", ref, line_ref) in verify.allocated_line_keys:
            continue
        received = max(0.0, _to_float(soi.received_qty))
        if received <= EPS:
            continue
        fact_date = soi.delivery_date or order.order_date
        owner_req_id = None
        order_ref1c = str(order.order_ref1c or "").strip()
        if order_ref1c and order_ref1c in own_by_ref:
            owner_req_id = own_by_ref[order_ref1c].get(item_id)
        if owner_req_id is not None:
            direct.append(
                FactCandidate(
                    pool=pk, fact_class=_SUPPLIER_CLASS, fact_type="supplier_receipt",
                    fact_ref=ref, fact_line_ref=line_ref, fact_date=fact_date,
                    qty=received, owner_req_id=owner_req_id,
                )
            )
        else:
            pool.append(
                FactCandidate(
                    pool=pk, fact_class=_SUPPLIER_CLASS, fact_type="supplier_receipt",
                    fact_ref=ref, fact_line_ref=line_ref, fact_date=fact_date, qty=received,
                )
            )
    return direct, pool


def _own_exported_supplier_owner(
    db: Session, scope: LedgerScope
) -> Dict[str, Dict[int, int]]:
    """Map ``order_ref1c → {item_id: owner_requirement_id}`` for supplier orders
    exported from a scope PlannedPurchase (SyncLink success). Owner =
    PlannedPurchase.source_mrp_requirement_id if open, else fallback to the open
    req of (PlannedPurchase.run_id, item_id)."""
    links = (
        db.query(SyncLink.source_id, SyncLink.target_ref_key)
        .filter(SyncLink.source_system == "PRODPLAN")
        .filter(SyncLink.source_doctype == "planned_purchase")
        .filter(SyncLink.target_entity == PURCHASE_ORDER_ENTITY)
        .filter(SyncLink.status == "success")
        .filter(SyncLink.target_ref_key.isnot(None))
        .all()
    )
    if not links:
        return {}
    source_ids = [int(sid) for sid, _ref in links]
    pp_by_id = {
        int(pp.purchase_id): pp
        for pp in db.query(PlannedPurchase).filter(PlannedPurchase.purchase_id.in_(source_ids)).all()
    }
    # (run_id, item_id) → open req id, for the fallback.
    open_req_by_run_item: Dict[Tuple[int, int], int] = {
        (int(req.run_id), int(req.item_id)): int(req.id) for req in scope.open_reqs
    }
    result: Dict[str, Dict[int, int]] = {}
    for source_id, ref_key in links:
        ref = str(ref_key or "").strip()
        if not ref:
            continue
        pp = pp_by_id.get(int(source_id))
        if pp is None:
            continue
        owner_req_id: Optional[int] = None
        if pp.source_mrp_requirement_id is not None and int(pp.source_mrp_requirement_id) in scope.open_req_ids:
            owner_req_id = int(pp.source_mrp_requirement_id)
        else:
            owner_req_id = open_req_by_run_item.get((int(pp.run_id), int(pp.item_id)))
        if owner_req_id is None:
            continue
        result.setdefault(ref, {})[int(pp.item_id)] = owner_req_id
    return result


# ---------------------------------------------------------------------------
# §6 — executed_qty aggregate
# ---------------------------------------------------------------------------
def _aggregate_executed_qty(scope: LedgerScope, exec_by_req: Dict[int, float]) -> Tuple[int, float]:
    """Write ``executed_qty`` (derived cache) onto every open requirement.
    Requirements without execution rows are zeroed. Returns
    (items_touched, total_executed)."""
    total_executed = 0.0
    touched_items: Set[int] = set()
    for req in scope.open_reqs:
        value = round(exec_by_req.get(int(req.id), 0.0), 3)
        req.executed_qty = value
        total_executed += value
        if value > EPS:
            touched_items.add(int(req.item_id))
    return len(touched_items), round(total_executed, 6)


# ---------------------------------------------------------------------------
# §4 (increment 4) — stock drift: reconcile reality against the frozen position
# ---------------------------------------------------------------------------
@dataclass
class DriftResult:
    adjust_by_req: Dict[int, float]
    matured_shortfall_total: float
    matured_surplus_total: float
    evap_adjust_total: float
    unattributed_total: float
    events_written: int
    pending_pools: int


def _produced_received_now(
    db: Session, item_ids: Set[int]
) -> Tuple[Dict[int, float], Dict[int, float]]:
    """Cumulative produced / received per item, aggregated with the SAME filters
    as ``mrp_freeze._write_freeze_baseline`` (deletion_mark=false only, no status
    filter) so the Δ against the frozen baseline is not skewed."""
    if not item_ids:
        return {}, {}
    ids = [int(i) for i in item_ids]
    produced_now: Dict[int, float] = {
        int(iid): _to_float(qty)
        for iid, qty in (
            db.query(ProductionProduct.item_id, func.sum(ProductionProduct.produced_qty))
            .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
            .filter(ProductionProduct.item_id.in_(ids))
            .filter(ProductionOrder.deletion_mark.is_(False))
            .group_by(ProductionProduct.item_id)
            .all()
        )
    }
    received_now: Dict[int, float] = {
        int(iid): _to_float(qty)
        for iid, qty in (
            db.query(SupplierOrderItem.item_id_ref, func.sum(SupplierOrderItem.received_qty))
            .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
            .filter(SupplierOrderItem.item_id_ref.in_(ids))
            .filter(SupplierOrder.deletion_mark.is_(False))
            .group_by(SupplierOrderItem.item_id_ref)
            .all()
        )
    }
    return produced_now, received_now


def _frozen_component_rows(db: Session, scope: LedgerScope) -> List[MrpFreezeComponent]:
    """Frozen BOM norm rows for the scope at each run's active freeze version,
    restricted to components that are pool items (the drift subjects)."""
    if not scope.pool_items or not scope.version_by_run:
        return []
    rows: List[MrpFreezeComponent] = []
    for row in (
        db.query(MrpFreezeComponent)
        .filter(MrpFreezeComponent.run_id.in_(scope.run_ids))
        .filter(MrpFreezeComponent.component_item_id.in_(scope.pool_items))
        .all()
    ):
        if scope.version_by_run.get(int(row.run_id)) != int(row.freeze_version):
            continue
        rows.append(row)
    return rows


def _drift_parent_items(db: Session, scope: LedgerScope) -> Set[int]:
    """Parent items that consume a pool item under the frozen norms — their
    produced-Δ drives the expected consumption of their components."""
    return {int(r.parent_item_id) for r in _frozen_component_rows(db, scope)}


def _drift_anchor_by_item(
    db: Session, scope: LedgerScope, item_ids: Set[int]
) -> Dict[int, Tuple[float, float, float, int, Optional[datetime]]]:
    """Per-item frozen anchor (stock S0, produced_total, received_total, donor
    run_id, frozen_at) = the baseline of the pool with max(frozen_at, run_id)
    among the scope runs at their active version. Same selection pattern as the
    ledger scope's pool anchor, extended to parent items (level-0 / closed
    included). ``frozen_at`` (Inc6) bounds the SLE consumption window under bin."""
    if not item_ids:
        return {}
    best: Dict[int, Tuple[Tuple[Any, int], Tuple[float, float, float, int, Optional[datetime]]]] = {}
    for row in (
        db.query(MrpFreezeBaseline)
        .filter(MrpFreezeBaseline.run_id.in_(scope.run_ids))
        .filter(MrpFreezeBaseline.item_id.in_([int(i) for i in item_ids]))
        .all()
    ):
        if scope.version_by_run.get(int(row.run_id)) != int(row.freeze_version):
            continue
        iid = int(row.item_id)
        rank = (row.frozen_at or datetime.min, int(row.run_id))
        prev = best.get(iid)
        if prev is None or rank > prev[0]:
            best[iid] = (
                rank,
                (
                    _to_float(row.stock_qty),
                    _to_float(row.produced_total),
                    _to_float(row.received_total),
                    int(row.run_id),
                    _coerce_dt(row.frozen_at),
                ),
            )
    return {iid: payload for iid, (_rank, payload) in best.items()}


def _actual_consumption_by_item_from_sle(
    db: Session,
    item_ids: Set[int],
    frozen_at_by_item: Dict[int, Optional[datetime]],
) -> Dict[int, float]:
    """Inc6 (design §11а) — actual pool consumption READ from ledger-1 (SLE).

    Σ of ``|qty|`` over active SLE rows with ``qty < 0`` and
    ``movement_kind ∈ {assembly_out, writeoff, transfer_out}`` since the freeze
    anchor (``posting_at >= frozen_at``), per item. This REPLACES the frozen-norm
    consumption model under STOCK_SOURCE=bin — one registrar carries both the
    parent Receipt and the component Expense (inc0), so consumption is a fact, not
    an inference. Adjustment/reconcile SLE are excluded (out-of-band residual)."""
    if not item_ids:
        return {}
    ids = [int(i) for i in item_ids]
    result: Dict[int, float] = {}
    skipped_no_anchor: Set[int] = set()
    for iid, qty, posting_at in (
        db.query(
            StockLedgerEntry.item_id,
            StockLedgerEntry.qty,
            StockLedgerEntry.posting_at,
        )
        .filter(StockLedgerEntry.item_id.in_(ids))
        .filter(StockLedgerEntry.active.is_(True))
        .filter(StockLedgerEntry.qty < 0)
        .filter(StockLedgerEntry.movement_kind.in_(_DRIFT_CONSUMPTION_KINDS))
        .all()
    ):
        item_id = int(iid)
        fa = frozen_at_by_item.get(item_id)
        # Robustness (finding #2): NEVER sum the whole SLE history without a lower
        # bound. A missing/NULL freeze anchor for an anchored item would otherwise
        # fold in every issue-kind row ever posted (an unbounded, wrong drift). If
        # the anchor is absent, skip the item (consumption = 0) with a diagnostic.
        if fa is None:
            if item_id not in skipped_no_anchor:
                skipped_no_anchor.add(item_id)
                logging.getLogger(__name__).warning(
                    "drift: skipping SLE consumption for item %s — no freeze "
                    "anchor (frozen_at is NULL); refusing unbounded history sum",
                    item_id,
                )
            continue
        pat = _coerce_dt(posting_at)
        if pat is not None and pat < fa:
            continue
        result[item_id] = result.get(item_id, 0.0) + (-_to_float(qty))
    return result


def _req_queue_key(req: MrpRequirement, run: Optional[PlanningRun]) -> tuple:
    period_from = (run.period_from if run else None) or date.min
    period_to = (run.period_to if run else None) or date.max
    return (period_from, period_to, int(req.run_id), int(req.id))


def compute_stock_drift(
    db: Session,
    scope: LedgerScope,
    verify: VerifyResult,
    produced_now: Dict[int, float],
    received_now: Dict[int, float],
    cycle_id: str,
) -> DriftResult:
    """Recompute per-pool stock drift from scratch (v2 §5/§7.1).

    drift is a PURE function of the immutable baseline (S0 / produced_total /
    received_total), the frozen BOM norms (``mrp_freeze_component``, NEVER the
    current ``SpecComponent``), the current mirrored facts (produced/received/
    stock) and executed_qty. It never reads its own prior value; the only
    inter-cycle state is the debounce chain carried in the shortfall/surplus
    ``mrp_drift_event`` rows.

    Returns the per-req ``drift_adjustment`` to materialise (matured shortfall +
    immediate evaporation − matured surplus).
    """
    from .item_ledger.config import use_bin_stock

    now = datetime.now(timezone.utc)
    window_hours = _drift_maturity_window_hours()
    use_bin = use_bin_stock()

    # --- evaporation adjustment (req-scoped, applied immediately, no W) ---
    # Single-channel evaporation (corrected Finding D). Each coverage source
    # resurfaces its evaporation through EXACTLY ONE channel:
    #   * ``supplier_order`` pins are ``own_open_coverage`` in the sizer (via
    #     ``own_exported_outstanding``) and are NOT netted into ``net_required``,
    #     so their evaporation resurfaces ONLY as own_open_coverage dropping — it
    #     must NOT ALSO inflate the drift/effective_net. Folding it here as well
    #     over-orders by the dead pin's alloc. EXCLUDED from the drift term.
    #   * ``wip_order`` pins WERE netted into ``net_required`` at freeze and are
    #     NOT in own_open_coverage, so their evaporation must resurface via the
    #     drift/effective_net term — KEPT here.
    # Under bin the whole term is REMOVED: a dead supplier pin already raises the
    # reservation ledger's ``uncovered``/``pin_live`` (which feed effective_net
    # under §11б), and WIP evaporation likewise surfaces via ``uncovered`` there.
    evap_by_req: Dict[int, float] = {}
    if not use_bin:
        for alloc in scope.freeze_allocs:
            if str(alloc.source_type or "") == "supplier_order":
                continue
            ev = verify.evaporated_by_alloc_id.get(int(alloc.id), 0.0)
            if ev > EPS:
                rid = int(alloc.requirement_id)
                evap_by_req[rid] = evap_by_req.get(rid, 0.0) + ev

    adjust_by_req: Dict[int, float] = {
        rid: round(v, 6) for rid, v in evap_by_req.items()
    }
    evap_adjust_total = round(sum(evap_by_req.values()), 6)

    if not scope.reqs_by_pool:
        return DriftResult(
            adjust_by_req=adjust_by_req,
            matured_shortfall_total=0.0,
            matured_surplus_total=0.0,
            evap_adjust_total=evap_adjust_total,
            unattributed_total=0.0,
            events_written=0,
            pending_pools=0,
        )

    # --- frozen norms + anchors ---
    comp_rows = _frozen_component_rows(db, scope)
    parent_items = {int(r.parent_item_id) for r in comp_rows}
    norm_by_key: Dict[Tuple[int, int, int], float] = {}
    created_by_key: Dict[Tuple[int, int, int], datetime] = {}
    parents_by_component: Dict[int, Set[int]] = {}
    for r in comp_rows:
        key = (int(r.run_id), int(r.parent_item_id), int(r.component_item_id))
        norm_by_key[key] = norm_by_key.get(key, 0.0) + _to_float(r.norm_qty_per_unit)
        created = _coerce_dt(r.created_at) or datetime.min.replace(tzinfo=timezone.utc)
        if key not in created_by_key or created > created_by_key[key]:
            created_by_key[key] = created
        parents_by_component.setdefault(int(r.component_item_id), set()).add(int(r.parent_item_id))

    anchor_by_item = _drift_anchor_by_item(db, scope, set(scope.pool_items) | parent_items)

    def _frozen_norm(parent: int, component: int, donor_run: int) -> Optional[float]:
        key = (int(donor_run), int(parent), int(component))
        if key in norm_by_key:
            return norm_by_key[key]
        candidates = [
            (created_by_key[(r, parent, component)], r)
            for r in scope.run_ids
            if (r, parent, component) in norm_by_key
        ]
        if not candidates:
            return None
        best_run = max(candidates)[1]
        return norm_by_key[(best_run, parent, component)]

    actual_stock_by_item = effective_stock_by_item_all(db)

    # Inc6 (design §11а) — under bin, actual consumption is READ from ledger-1
    # (Σ issue-kind SLE since the freeze anchor), REPLACING the norm model. The
    # frozen norms (mrp_freeze_component) STAY the BOM-explosion/netting source;
    # they simply no longer feed drift.
    actual_consumption_by_item: Dict[int, float] = {}
    if use_bin:
        frozen_at_by_item = {
            iid: payload[4] for iid, payload in anchor_by_item.items()
        }
        actual_consumption_by_item = _actual_consumption_by_item_from_sle(
            db, set(scope.pool_items), frozen_at_by_item
        )

    # --- debounce chain: snapshot prior first_seen, then rebuild globally ---
    prior_first_seen: Dict[Tuple[int, str, str, str, str], Tuple[str, Optional[datetime]]] = {}
    for ev in (
        db.query(MrpDriftEvent)
        .filter(MrpDriftEvent.kind.in_(("shortfall", "surplus")))
        .all()
    ):
        details = ev.details or {}
        key = (
            int(ev.item_id),
            str(ev.characteristic_ref or ""),
            str(ev.organization_ref or ""),
            str(ev.planning_stock_pool or ""),
            str(ev.kind or ""),
        )
        seen_at = _coerce_dt(details.get("first_seen_at")) or _coerce_dt(ev.created_at)
        prior_first_seen[key] = (ev.first_seen_cycle_id or ev.cycle_id or cycle_id, seen_at)

    db.query(MrpDriftEvent).filter(
        MrpDriftEvent.kind.in_(("shortfall", "surplus"))
    ).delete(synchronize_session=False)

    matured_shortfall_total = 0.0
    matured_surplus_total = 0.0
    unattributed_total = 0.0
    events_written = 0
    pending_pools = 0

    for pk, reqs in scope.reqs_by_pool.items():
        item_id = int(reqs[0].item_id)
        anchor = anchor_by_item.get(item_id)
        if anchor is None:
            continue  # pool without an anchor is out of drift (§5)
        bom_level_min = min(int(req.bom_level or 0) for req in reqs)
        if bom_level_min == 0:
            continue  # finished-goods shipping is not drift (§5, conservative)

        s0, produced_total, received_total, _donor, _frozen_at = anchor
        delta_produced_in = max(0.0, produced_now.get(item_id, 0.0) - produced_total)
        delta_received = max(0.0, received_now.get(item_id, 0.0) - received_total)

        expected_consumption = 0.0
        parents_used: List[Dict[str, Any]] = []
        if use_bin:
            # Inc6 (§11а): consumption is the SLE fact, not the norm inference.
            expected_consumption = actual_consumption_by_item.get(item_id, 0.0)
            parents_used.append({"source": "sle", "actual_consumption": round(expected_consumption, 3)})
        for parent in sorted(parents_by_component.get(item_id, set())) if not use_bin else ():
            p_anchor = anchor_by_item.get(parent)
            if p_anchor is None:
                # Parent without an anchor contributes 0 (a (0,0) fallback would
                # imply a false, huge consumption). Diagnostic only.
                parents_used.append({"parent_item_id": parent, "no_anchor": True})
                continue
            norm = _frozen_norm(parent, item_id, int(p_anchor[3]))
            if norm is None or norm <= EPS:
                continue
            delta_p = max(0.0, produced_now.get(parent, 0.0) - p_anchor[1])
            expected_consumption += delta_p * norm
            parents_used.append(
                {"parent_item_id": parent, "delta_produced": round(delta_p, 3), "norm": round(norm, 6)}
            )

        expected_stock = s0 + delta_produced_in + delta_received - expected_consumption
        actual_stock = actual_stock_by_item.get(item_id, 0.0)
        drift = actual_stock - expected_stock

        if abs(drift) <= EPS:
            continue

        kind = "shortfall" if drift < 0 else "surplus"
        chain_key = (
            item_id,
            pk.characteristic_ref,
            pk.organization_ref,
            pk.planning_stock_pool,
            kind,
        )
        prior = prior_first_seen.get(chain_key)
        if prior is not None:
            first_seen_cycle_id, first_seen_at = prior
            if first_seen_at is None:
                first_seen_at = now
        else:
            first_seen_cycle_id, first_seen_at = cycle_id, now
        # Inc6 (design §11а): under bin the W=48h maturity window + debounce are
        # REMOVED — a shortfall (the out-of-band residual) matures immediately.
        matured = use_bin or (
            first_seen_cycle_id != cycle_id
            and (now - first_seen_at).total_seconds() >= window_hours * 3600.0
        )

        reqs_sorted = sorted(
            reqs, key=lambda r: _req_queue_key(r, scope.runs_by_id.get(int(r.run_id)))
        )
        initial_by_req = {
            int(r.id): max(_to_float(r.initial_snapshot_stock), 0.0) for r in reqs_sorted
        }
        per_req_shares: Dict[int, float] = {}

        if drift < 0:
            total_short = -drift
            sum_initial = sum(initial_by_req.values())
            shortfall_pool = min(total_short, sum_initial)
            unattributed_total += max(0.0, total_short - shortfall_pool)
            remaining = shortfall_pool
            for r in reqs_sorted:
                share = min(remaining, initial_by_req[int(r.id)])
                if share > EPS:
                    per_req_shares[int(r.id)] = round(share, 6)
                remaining = max(0.0, remaining - share)
                if matured and share > EPS:
                    adjust_by_req[int(r.id)] = round(
                        adjust_by_req.get(int(r.id), 0.0) + share, 6
                    )
                    matured_shortfall_total += share
        else:
            remaining = drift
            for r in reqs_sorted:
                rid = int(r.id)
                net = _to_float(r.net_required_qty)
                executed = _to_float(r.executed_qty)
                evap_share = evap_by_req.get(rid, 0.0)
                open_deficit = max(net + evap_share - executed, 0.0)
                take = min(remaining, open_deficit)
                if take > EPS:
                    per_req_shares[rid] = round(-take, 6)
                remaining = max(0.0, remaining - take)
                if matured and take > EPS:
                    adjust_by_req[rid] = round(adjust_by_req.get(rid, 0.0) - take, 6)
                    matured_surplus_total += take

        if not matured:
            pending_pools += 1

        db.add(
            MrpDriftEvent(
                cycle_id=cycle_id,
                item_id=item_id,
                characteristic_ref=pk.characteristic_ref,
                organization_ref=pk.organization_ref,
                planning_stock_pool=pk.planning_stock_pool,
                kind=kind,
                drift_qty=round(abs(drift), 3),
                expected_stock=round(expected_stock, 3),
                actual_stock=round(actual_stock, 3),
                matured=bool(matured),
                first_seen_cycle_id=first_seen_cycle_id,
                requirement_id=None,
                details={
                    "per_req_shares": per_req_shares,
                    "unattributed": round(max(0.0, -drift) - sum(
                        v for v in per_req_shares.values() if v > 0
                    ), 6) if drift < 0 else 0.0,
                    "parents": parents_used,
                    "first_seen_at": (first_seen_at or now).isoformat(),
                },
            )
        )
        events_written += 1

    return DriftResult(
        adjust_by_req={rid: round(v, 6) for rid, v in adjust_by_req.items()},
        matured_shortfall_total=round(matured_shortfall_total, 6),
        matured_surplus_total=round(matured_surplus_total, 6),
        evap_adjust_total=evap_adjust_total,
        unattributed_total=round(unattributed_total, 6),
        events_written=events_written,
        pending_pools=pending_pools,
    )


def _materialize_drift_adjustment(scope: LedgerScope, drift: DriftResult) -> int:
    """Write ``drift_adjustment_qty`` onto every open requirement from scratch
    (missing → 0). This is the ONLY writer of the column besides the freeze
    (which zeroes it). It never reads its own prior value (G5)."""
    touched = 0
    for req in scope.open_reqs:
        value = round(drift.adjust_by_req.get(int(req.id), 0.0), 3)
        req.drift_adjustment_qty = value
        if abs(value) > EPS:
            touched += 1
    return touched


# ---------------------------------------------------------------------------
# §5 (increment 5) — plan closure by execution
# ---------------------------------------------------------------------------
def apply_requirement_closure(
    db: Session, scope: LedgerScope, eff_net_by_req: Optional[Dict[int, float]] = None
) -> Dict[str, Any]:
    """Auto-close every open requirement whose REAL deficit has been executed.

    Runs in the ledger cycle right AFTER ``_materialize_drift_adjustment``, so
    both ``executed_qty`` and ``drift_adjustment_qty`` are already persistent on
    the open req. Closure rule per requirement (owner ruling 21.07 — close ONLY
    on a real, executed deficit)::

        effective_net = max(net_required_qty + drift_adjustment_qty, 0)
        close ⟺ effective_net > EPS AND executed_qty + EPS >= effective_net

    A requirement closed here had a REAL deficit met by realized production /
    receipt, so an evaporation cannot resurface it — it is safe to leave the
    scope. A net=0 requirement (``effective_net <= EPS``) is covered by frozen
    STOCK, not by execution — that is NOT "executed", so it is NOT closed here.
    It stays open-but-zero (in scope, visible to drift / evaporation, ordering
    nothing); it is swept only when the whole run closes (§2). Closing it early
    would drop it from scope and hide a deficit that an evaporated supply later
    resurfaces (no auto-reopen this increment) — the project's cardinal bug.
    A short deficit (труба 6092, june runs 13/14) stays open; no date closes it.
    Reopening is the manual endpoint only (blueprint §7.3 pending_reopen →
    operator).
    """
    now = datetime.now(timezone.utc)
    closed = 0
    for req in scope.open_reqs:
        effective_net = _effective_net(req, eff_net_by_req)
        if effective_net > EPS and _to_float(req.executed_qty) + EPS >= effective_net:
            req.status = "closed"
            req.closed_at = now
            closed += 1
    db.flush()
    return {"requirements_closed": closed}


def apply_run_closure(
    db: Session,
    scope: LedgerScope,
    eff_net_by_req: Optional[Dict[int, float]] = None,
    cycle_id: str = "",
) -> List[int]:
    """Auto-close every FIXED_SNAPSHOT run that has NO OPEN DEFICIT left.

    Runs right after :func:`apply_requirement_closure` (its closures are already
    flushed). A run closes ONLY by execution — never by an overdue period. For
    each scope run still at ``FIXED_SNAPSHOT`` that carries at least one
    requirement (evidence): count the open reqs whose real deficit is still
    positive (``net_required_qty + drift_adjustment_qty - executed_qty > EPS``;
    correct even when net+drift ≤ 0, then the term is ≤ -executed ≤ 0 and not
    counted). If NONE remain, the plan is done and its frozen stock is realized —
    batch-close the remaining open reqs (this sweeps the open-but-zero net=0
    tail), then stamp ``status='CLOSED'``, ``finished_at=now``. A closed run
    drops out of ``_scope_run_ids`` next cycle (no open req) → out of freeze (its
    stock is released on refreeze) and out of the sizing loop.
    """
    now = datetime.now(timezone.utc)
    closed_run_ids: List[int] = []
    for rid in scope.run_ids:
        run = scope.runs_by_id.get(rid)
        if run is None or str(run.status or "") != FIXED_SNAPSHOT_STATUS:
            continue
        total = (
            db.query(func.count(MrpRequirement.id))
            .filter(MrpRequirement.run_id == rid)
            .scalar()
        ) or 0
        if total <= 0:
            continue
        if eff_net_by_req is not None:
            # Inc6 (bin): the effective_net source is the reservation ledger, not
            # net+drift — count the open deficit in Python over the same target.
            open_reqs_of_run = (
                db.query(MrpRequirement)
                .filter(MrpRequirement.run_id == rid)
                .filter(MrpRequirement.status == "open")
                .all()
            )
            open_deficit = sum(
                1
                for r in open_reqs_of_run
                if _effective_net(r, eff_net_by_req) - _to_float(r.executed_qty) > EPS
            )
        else:
            open_deficit = (
                db.query(func.count(MrpRequirement.id))
                .filter(MrpRequirement.run_id == rid)
                .filter(MrpRequirement.status == "open")
                .filter(
                    (
                        func.coalesce(MrpRequirement.net_required_qty, 0.0)
                        + func.coalesce(MrpRequirement.drift_adjustment_qty, 0.0)
                        - func.coalesce(MrpRequirement.executed_qty, 0.0)
                    )
                    > EPS
                )
                .scalar()
            ) or 0
        if open_deficit == 0:
            # Sweep the remaining open-but-zero (net=0) tail, then close the run.
            (
                db.query(MrpRequirement)
                .filter(MrpRequirement.run_id == rid)
                .filter(MrpRequirement.status == "open")
                .update(
                    {"status": "closed", "closed_at": now},
                    synchronize_session=False,
                )
            )
            run.status = CLOSED_STATUS
            run.finished_at = now
            closed_run_ids.append(rid)
    db.flush()
    # Д3а (design §6.2, решение №15): a closed run releases its reservations —
    # otherwise they stay active forever, inflating reserved_soft and starving
    # available/coverage of the surviving plans. Wrapped: a reservation-ledger
    # failure must not undo the closure itself; the run_reservation_shadow
    # sweep (release_closed_run_reservations) self-heals on the next cycle.
    if closed_run_ids:
        try:
            from .item_ledger.reservation_ledger import release_run_reservations

            release_run_reservations(db, closed_run_ids, cycle_id=cycle_id)
        except Exception:  # noqa: BLE001 — closure must stand even if release fails
            logging.getLogger(__name__).exception(
                "release_run_reservations failed for closed runs %s "
                "(ghost reserves will be swept next cycle)",
                closed_run_ids,
            )
    return closed_run_ids


# ---------------------------------------------------------------------------
# §2 — the cycle
# ---------------------------------------------------------------------------
def _ensure_legacy_diagnostic_generation(db: Session) -> int:
    """Create/reuse an isolated BUILDING context for explicit legacy tests."""
    batch = (
        db.query(PhysicalImportBatch)
        .filter(PhysicalImportBatch.batch_key == "legacy-ledger-diagnostic")
        .one_or_none()
    )
    if batch is None:
        batch = PhysicalImportBatch(
            batch_key="legacy-ledger-diagnostic",
            status="completed",
            cutoff=datetime.now(timezone.utc),
            source_watermarks={},
        )
        db.add(batch)
        db.flush()
    generation = (
        db.query(LedgerGeneration)
        .filter(LedgerGeneration.generation_key == "legacy-ledger-diagnostic")
        .one_or_none()
    )
    if generation is None:
        generation = LedgerGeneration(
            generation_key="legacy-ledger-diagnostic",
            status="building",
            cutoff=batch.cutoff,
            physical_import_batch_id=batch.id,
            algorithm_version="legacy-diagnostic/1",
            replay_version="legacy-diagnostic/1",
            source_watermarks={},
            capabilities={},
        )
        db.add(generation)
        db.flush()
    return int(generation.id)


def _run_legacy_ledger_cycle_diagnostic(db: Session) -> Dict[str, Any]:
    """Legacy generation-unaware rebuild retained only for diagnostics/tests.

    Does NOT commit — the caller owns the transaction (matching how
    reconciliation persists / rolls back). Serialised against the freeze via the
    shared advisory lock on PostgreSQL (SQLite is a single-writer no-op).
    """
    ledger_generation_id = _ensure_legacy_diagnostic_generation(db)
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MRP_LEDGER_LOCK_KEY})

    scope = _ledger_scope(db)
    cycle_id = f"led-{datetime.now(timezone.utc).isoformat()}"

    if not scope.open_req_ids:
        return {
            "cycle_id": cycle_id,
            "runs": scope.run_ids,
            "items_touched": 0,
            "total_executed": 0.0,
            "execution_rows": 0,
            "coverage_rows": 0,
            "realized_total": 0.0,
            "evaporated_total": 0.0,
            "evaporation_events": 0,
        }

    # produced/received facts are aggregated ONCE for the whole cycle over the
    # pool items plus the drift parent items, and shared by the execution-budget
    # step and compute_stock_drift so both see the identical Δ-from-baseline.
    parent_items = _drift_parent_items(db, scope)
    drift_item_ids: Set[int] = set(scope.pool_items) | parent_items
    produced_now, received_now = _produced_received_now(db, drift_item_ids)

    from .item_ledger.config import use_bin_stock

    use_bin = use_bin_stock()

    verify = verify_frozen_supply(db, scope, cycle_id)

    # Inc6 (design §11б): under bin the reservation ledger is LOAD-BEARING — it is
    # materialized/redistributed EARLY (before execution allocation + closure) so
    # ``effective_net`` can derive from its ``uncovered`` + supplier term for the
    # whole cycle. Under legacy it stays PURE SHADOW at the end (byte-identical).
    eff_net_by_req: Optional[Dict[int, float]] = None
    if use_bin:
        from .item_ledger.reservation_ledger import (
            effective_net_bin,
            run_reservation_shadow,
        )

        run_reservation_shadow(db, scope, cycle_id)
        eff_net_by_req = {}
        for req in scope.open_reqs:
            val = effective_net_bin(db, req)
            eff_net_by_req[int(req.id)] = (
                val
                if val is not None
                else max(_to_float(req.net_required_qty) + _to_float(req.drift_adjustment_qty), 0.0)
            )

    _rows, exec_by_req, execution_rows, coverage_rows = _build_execution_allocations(
        db, scope, verify, cycle_id, produced_now, received_now,
        ledger_generation_id, eff_net_by_req,
    )
    items_touched, total_executed = _aggregate_executed_qty(scope, exec_by_req)

    # §4 (increment 4): drift AFTER executed aggregation (a surplus cap needs
    # executed; shortfall caps = frozen initial_snapshot_stock). Then materialise
    # drift_adjustment_qty (the reconcile sizer reads it next cycle / this run).
    # Under bin the drift is shrunk (SLE consumption, no W, no evaporation term).
    drift = compute_stock_drift(db, scope, verify, produced_now, received_now, cycle_id)
    _materialize_drift_adjustment(scope, drift)

    # §5 (increment 5): plan closure by execution. Closure runs AFTER drift so
    # effective_net accounts for drift_adjustment (an evaporated supply can
    # re-open / keep-open a requirement). Requirements close first, then a run
    # closes once none of its requirements remain open.
    requirement_closure = apply_requirement_closure(db, scope, eff_net_by_req)
    runs_closed = apply_run_closure(db, scope, eff_net_by_req, cycle_id=cycle_id)

    # Inc4 (PURE SHADOW, ADDITIVE) — LEGACY ONLY: materialize the reservation
    # ledger from the same scope + the live SLE substrate. Wrapped so any failure
    # logs and never breaks the cycle (no reader consults it under legacy). Under
    # bin it already ran early (above) as the load-bearing effective_net source.
    # NB (Inc6в, DEFERRED): MrpFreezeAllocation → SQL-VIEW cleanup stays for Inc7;
    # the Inc4 dual-write persists and MrpFreezeAllocation remains the read source.
    if not use_bin:
        try:
            from .item_ledger.reservation_ledger import run_reservation_shadow

            run_reservation_shadow(db, scope, cycle_id)
        except Exception:  # noqa: BLE001 — shadow must never break the ledger cycle
            logging.getLogger(__name__).exception(
                "Inc4 reservation shadow block failed; continuing (ledger cycle unaffected)"
            )

    realized_total = round(sum(verify.realized_by_alloc_id.values()), 6)
    evaporated_total = round(sum(verify.evaporated_by_alloc_id.values()), 6)
    evaporation_events = sum(1 for v in verify.evaporated_by_alloc_id.values() if v > EPS)

    return {
        "cycle_id": cycle_id,
        "runs": scope.run_ids,
        "items_touched": items_touched,
        "total_executed": total_executed,
        "execution_rows": execution_rows,
        "coverage_rows": coverage_rows,
        "realized_total": realized_total,
        "evaporated_total": evaporated_total,
        "evaporation_events": evaporation_events,
        "drift_events": drift.events_written,
        "drift_pending_pools": drift.pending_pools,
        "drift_matured_shortfall": drift.matured_shortfall_total,
        "drift_matured_surplus": drift.matured_surplus_total,
        "drift_evap_adjust": drift.evap_adjust_total,
        "drift_unattributed": drift.unattributed_total,
        "requirements_closed": requirement_closure["requirements_closed"],
        "runs_closed": runs_closed,
    }


def run_ledger_cycle(
    db: Session,
    *,
    diagnostic_legacy: bool = False,
) -> Dict[str, Any]:
    """Public containment boundary.

    Production callers may not create generation-less execution allocations.
    The old algorithm is available only through an explicit diagnostic opt-in
    while generation-scoped replay replaces it.
    """
    if not diagnostic_legacy:
        raise NotImplementedError(
            "generation-unaware run_ledger_cycle is retired; "
            "use generation-scoped historical replay"
        )
    return _run_legacy_ledger_cycle_diagnostic(db)


def populate_executed_qty(
    db: Session,
    run_ids: Optional[List[int]] = None,
    *,
    diagnostic_legacy: bool = False,
) -> Dict[str, Any]:
    """Retired phase-2 entry point (v2 §4). A partial recompute is no longer
    supported — the ledger cycle rebuilds the whole canonical scope. Kept as a
    loud shim so any forgotten caller fails fast; ``run_ids=None`` (the
    reconciliation tail's call) delegates to :func:`run_ledger_cycle`."""
    if run_ids is not None:
        raise ValueError(
            "partial ledger recompute retired v2 §4; use run_ledger_cycle"
        )
    return run_ledger_cycle(db, diagnostic_legacy=diagnostic_legacy)

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
    MrpRequirement,
    MrpRequirementBucket,
    PlannedPurchase,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SupplierOrder,
    SupplierOrderItem,
    SyncLink,
)
from .mrp_freeze import MRP_LEDGER_LOCK_KEY, PoolKey, pool_key_for
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


def _effective_net(req: MrpRequirement) -> float:
    return max(_to_float(req.net_required_qty) + _to_float(req.drift_adjustment_qty), 0.0)


def _build_execution_allocations(
    db: Session,
    scope: LedgerScope,
    verify: VerifyResult,
    cycle_id: str,
) -> Tuple[List[MrpExecutionAllocation], Dict[int, float], int, int]:
    """Rebuild ``mrp_execution_allocation`` for the scope. Returns
    (row objects, executed_by_req, execution_row_count, coverage_row_count)."""
    now = datetime.now(timezone.utc)
    rows: List[MrpExecutionAllocation] = []
    exec_by_req: Dict[int, float] = {int(rid): 0.0 for rid in scope.open_req_ids}
    exec_in_bucket: Dict[Tuple[int, Optional[int]], float] = {}
    coverage_count = 0
    execution_count = 0

    # 5.0 — DELETE existing execution allocations for the open-req scope.
    if scope.open_req_ids:
        db.query(MrpExecutionAllocation).filter(
            MrpExecutionAllocation.requirement_id.in_(scope.open_req_ids)
        ).delete(synchronize_session=False)

    if not scope.open_req_ids:
        return rows, exec_by_req, 0, 0

    # 5.1 — Δ-from-baseline budget per pool (mirrors _write_freeze_baseline's
    # exact filters: join order, deletion_mark=false, NO status/source filter).
    produced_now: Dict[int, float] = {
        int(iid): _to_float(qty)
        for iid, qty in (
            db.query(ProductionProduct.item_id, func.sum(ProductionProduct.produced_qty))
            .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
            .filter(ProductionProduct.item_id.in_(scope.pool_items))
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
            .filter(SupplierOrderItem.item_id_ref.in_(scope.pool_items))
            .filter(SupplierOrder.deletion_mark.is_(False))
            .group_by(SupplierOrderItem.item_id_ref)
            .all()
        )
    }
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
        else:
            bdate = ptv
            slots.append(
                _Slot(
                    req_id=req_id,
                    bucket_id=None,
                    bucket_date=bdate,
                    net_qty=_effective_net(req),
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
            room_req = _effective_net(req) - exec_by_req.get(slot.req_id, 0.0)
            room_budget = budget_remaining[pk][cand.fact_class]
            take = min(qty, room_bucket, room_req, room_budget)
            if take <= EPS:
                continue
            rows.append(
                MrpExecutionAllocation(
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
                room_req = _effective_net(req) - exec_by_req.get(slot.req_id, 0.0)
                room_budget = budget_remaining[pk][cand.fact_class]
                take = min(cand_rem, room_bucket, room_req, room_budget)
                if take <= EPS:
                    continue
                rows.append(
                    MrpExecutionAllocation(
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
# §2 — the cycle
# ---------------------------------------------------------------------------
def run_ledger_cycle(db: Session) -> Dict[str, Any]:
    """Rebuild the execution ledger for the canonical scope (increment 3).

    Does NOT commit — the caller owns the transaction (matching how
    reconciliation persists / rolls back). Serialised against the freeze via the
    shared advisory lock on PostgreSQL (SQLite is a single-writer no-op).
    """
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

    verify = verify_frozen_supply(db, scope, cycle_id)
    _rows, exec_by_req, execution_rows, coverage_rows = _build_execution_allocations(
        db, scope, verify, cycle_id
    )
    items_touched, total_executed = _aggregate_executed_qty(scope, exec_by_req)

    # [increment-4 slot] compute_stock_drift / drift materialisation.
    # [increment-5 slot] requirement closure / released routing.

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
    }


def populate_executed_qty(
    db: Session,
    run_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Retired phase-2 entry point (v2 §4). A partial recompute is no longer
    supported — the ledger cycle rebuilds the whole canonical scope. Kept as a
    loud shim so any forgotten caller fails fast; ``run_ids=None`` (the
    reconciliation tail's call) delegates to :func:`run_ledger_cycle`."""
    if run_ids is not None:
        raise ValueError(
            "partial ledger recompute retired v2 §4; use run_ledger_cycle"
        )
    return run_ledger_cycle(db)

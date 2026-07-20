"""Freeze v2 orchestrator — ``refreeze_active_snapshots`` and its machinery.

Root fix for the fixed-MRP execution-ledger rebuild (increment 2). A single
"freeze" re-derives every open FIXED_SNAPSHOT plan's net demand against ONE
queue-wide, consume-once supply pool (effective stock + open WIP + open supplier
supply), so a physical unit is credited to at most one plan. The frozen position
is persisted immutably into the ledger v2 tables (``mrp_freeze_baseline`` /
``mrp_freeze_allocation`` / ``mrp_freeze_component``) at a new version; prior
versions are never mutated.

Key invariants (see the increment-2 spec):

* Pools are built ONCE and mutated in place; runs are processed earliest-need
  first so the plan that needs a unit soonest claims it.
* Self-exclusion: a run's OWN materialised production and OWN already-exported
  supplier orders must NOT count as coverage of the very net they execute
  (else a refreeze zeroes its own net and self-destructs).
* MrpRequirement ids survive a refreeze (the ``(run_id, item_id)`` upsert), so
  production orders stay linked.
* Pool columns are always written as ``''`` / ``'default'`` — never NULL.
* One commit per operation; ``dry_run`` rolls the whole queue back.

The heavy per-run body (``_freeze_one_run``) and the run-header preparation
(``_prepare_include_run``) live in :mod:`period_plan_service`; this module owns
the pool construction, the pool key, the freeze-table writers and the queue
orchestration. The module cycle (mrp_freeze → period_plan_service) is one-way:
period_plan_service imports back only locally, inside function bodies.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from ..models import (
    Item,
    MrpFreezeAllocation,
    MrpFreezeBaseline,
    MrpFreezeComponent,
    MrpRequirement,
    PlannedPurchase,
    PlanningRun,
    ProductionOrder,
    ProductionPlanHeader,
    ProductionProduct,
    Specification,
    SupplierOrder,
    SupplierOrderItem,
    SyncLink,
)
from .mrp_stock_helpers import (
    WipSupplyLine,
    active_wip_supply_by_item,
    effective_stock_by_item_all,
)
from .replenishment import (
    REPLENISHMENT_FLOW_PURCHASE,
    classify_replenishment_flow,
)

__all__ = [
    "PoolKey",
    "pool_key_for",
    "WipSupplyLine",
    "FreezeSharedPools",
    "ItemFreezeTrace",
    "FreezeTrace",
    "build_shared_pools",
    "refreeze_active_snapshots",
]

EPS = 1e-9
FIXED_SNAPSHOT_STATUS = "FIXED_SNAPSHOT"
PURCHASE_ORDER_ENTITY = "Document_ЗаказПоставщику"

DEFAULT_STOCK_POOL = "default"
EMPTY_REF = ""

# Shared with the future run_ledger_cycle so the two never interleave. SQLite is
# a no-op (single-writer); PG serialises the maintenance operation.
MRP_LEDGER_LOCK_KEY = 0x4D52504C444752  # "MRPLDGR"


# ---------------------------------------------------------------------------
# §1 — pool key (pragmatic single-pool today; the ONLY place to widen later)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PoolKey:
    item_id: int
    characteristic_ref: str = EMPTY_REF
    organization_ref: str = EMPTY_REF
    planning_stock_pool: str = DEFAULT_STOCK_POOL


def pool_key_for(
    item_id: int,
    characteristic_ref: Optional[str] = None,
    organization_ref: Optional[str] = None,
) -> PoolKey:
    """Normalise any item into today's single pool ``('', '', 'default')``.

    The characteristic/organization arguments are accepted (so callers already
    thread them) but collapsed — multi-pool support is a future change confined
    to THIS function. Every baseline / allocation / component / requirement pool
    column is written through here, always ``''`` / ``'default'`` (never NULL).
    """
    return PoolKey(int(item_id), EMPTY_REF, EMPTY_REF, DEFAULT_STOCK_POOL)


# ---------------------------------------------------------------------------
# §2 — shared structures
# ---------------------------------------------------------------------------
@dataclass
class FreezeSharedPools:
    """Queue-wide, consume-once supply ledgers (semantically 1:1 with Step A's
    SharedPools, but WIP is an identity-carrying ETA list and stock keeps its
    frozen S0). Built ONCE and mutated in place across the run queue.

    * ``stock`` — ``{item_id: remaining effective stock}`` (consume-once).
    * ``stock_initial`` — ``{item_id: S0}`` frozen at build time (immutable).
    * ``wip`` — ``{item_id: [WipSupplyLine]}`` (``.remaining`` mutated).
    * ``supplier`` — ``{item_id: [row]}`` (``remaining_qty`` mutated; the SAME
      row objects are shared by every run).
    """

    stock: Dict[int, float]
    stock_initial: Dict[int, float]
    wip: Dict[int, List[WipSupplyLine]]
    supplier: Dict[int, List[Dict[str, Any]]]


@dataclass
class ItemFreezeTrace:
    """Per-item record of what covered its net during one run's freeze."""

    stock_alloc: float = 0.0
    wip_allocs: List[Tuple[WipSupplyLine, float]] = field(default_factory=list)
    supplier_allocs: List[Tuple[Dict[str, Any], float]] = field(default_factory=list)


@dataclass
class FreezeTrace:
    """Per-run trace consumed by the freeze-table writers."""

    by_item: Dict[int, ItemFreezeTrace] = field(
        default_factory=lambda: defaultdict(ItemFreezeTrace)
    )
    # (parent_item_id, component_item_id, spec_id, norm_per_unit)
    component_norms: List[Tuple[int, int, int, float]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# §3 — self-exclusion sets (built BEFORE the pools)
# ---------------------------------------------------------------------------
def _own_wip_product_ids(db: Session, active_run_ids: Iterable[int]) -> Set[int]:
    """Product ids of a run's OWN materialised production — orders that EXECUTE
    the active snapshots' net, so they must not double as coverage of it.

    Own = ``ProductionProduct.source_mrp_requirement_id`` in an active run's
    requirements, OR ``ProductionOrder.source == 'mrp'`` with ``source_run_id``
    in the active set.
    """
    run_ids = [int(r) for r in active_run_ids]
    if not run_ids:
        return set()
    result: Set[int] = set()

    req_ids = [
        int(rid)
        for (rid,) in db.query(MrpRequirement.id)
        .filter(MrpRequirement.run_id.in_(run_ids))
        .all()
    ]
    if req_ids:
        for (pid,) in (
            db.query(ProductionProduct.product_id)
            .filter(ProductionProduct.source_mrp_requirement_id.in_(req_ids))
            .all()
        ):
            result.add(int(pid))

    for (pid,) in (
        db.query(ProductionProduct.product_id)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .filter(ProductionOrder.source == "mrp")
        .filter(ProductionOrder.source_run_id.in_(run_ids))
        .all()
    ):
        result.add(int(pid))
    return result


def _own_supplier_order_ids(db: Session, active_run_ids: Iterable[int]) -> Set[int]:
    """SupplierOrder ids exported from a run's OWN PlannedPurchase — their supply
    is the run's own coverage (kept as ``own_exported_left`` in _freeze_one_run),
    so they must not ALSO be credited via the shared supplier pool.

    A successful ``planned_purchase → Document_ЗаказПоставщику`` sync-link is own
    when its source PlannedPurchase's run is active. If the source PlannedPurchase
    row is gone (deleted), the order is excluded by default (§15.1: prefer a small
    over-order to a phantom double-credit; the run_id debt is tracked upstream).
    """
    active_set = {int(r) for r in active_run_ids}
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
        return set()

    source_ids = [int(sid) for sid, _ref in links]
    pp_run_by_id = {
        int(pid): int(rid)
        for pid, rid in (
            db.query(PlannedPurchase.purchase_id, PlannedPurchase.run_id)
            .filter(PlannedPurchase.purchase_id.in_(source_ids))
            .all()
        )
    }
    own_refs: Set[str] = set()
    for source_id, ref_key in links:
        ref = str(ref_key or "").strip()
        if not ref:
            continue
        run_id = pp_run_by_id.get(int(source_id))
        if run_id is None or run_id in active_set:
            own_refs.add(ref)
    if not own_refs:
        return set()

    return {
        int(oid)
        for (oid,) in db.query(SupplierOrder.order_id)
        .filter(SupplierOrder.order_ref1c.in_(own_refs))
        .all()
    }


# ---------------------------------------------------------------------------
# §3 — pool construction (ONCE for the whole queue)
# ---------------------------------------------------------------------------
def build_shared_pools(
    db: Session,
    active_run_ids: Iterable[int],
    *,
    exclude_wip_product_ids: Optional[Iterable[int]] = None,
    exclude_supplier_order_ids: Optional[Iterable[int]] = None,
) -> FreezeSharedPools:
    """Build the three consume-once pools once for the active-run queue.

    Stock is ``effective_stock_by_item_all`` (the existing pool contour: ignored
    warehouses excluded); ``stock_initial`` snapshots S0. WIP is the identity
    loader with own product ids excluded. Supplier supply is loaded for every
    item that has any open supplier line (a safe superset — consumption only
    touches purchase-flow items a run actually needs), with the queue-wide cutoff
    ``max(period_to)`` and own orders excluded; phasing is re-applied per bucket
    downstream.
    """
    run_ids = [int(r) for r in active_run_ids]
    stock = effective_stock_by_item_all(db)
    stock_initial = dict(stock)
    wip = active_wip_supply_by_item(db, exclude_product_ids=exclude_wip_product_ids)

    supplier: Dict[int, List[Dict[str, Any]]] = {}
    if run_ids:
        # Import here to avoid a top-level cycle with period_plan_service.
        from .period_plan_service import _load_purchase_supplier_remaining

        supplier_item_ids = [
            int(iid)
            for (iid,) in db.query(SupplierOrderItem.item_id_ref).distinct().all()
            if iid is not None
        ]
        period_tos = [
            pt
            for (pt,) in db.query(PlanningRun.period_to)
            .filter(PlanningRun.run_id.in_(run_ids))
            .all()
            if pt is not None
        ]
        req_tos = [
            pt
            for (pt,) in db.query(MrpRequirement.period_to)
            .filter(MrpRequirement.run_id.in_(run_ids))
            .all()
            if pt is not None
        ]
        candidates = period_tos + req_tos
        max_period_to = max(candidates) if candidates else date.today()
        if supplier_item_ids:
            supplier = _load_purchase_supplier_remaining(
                db,
                supplier_item_ids,
                max_period_to,
                exclude_order_ids=exclude_supplier_order_ids,
            )
    return FreezeSharedPools(
        stock=stock, stock_initial=stock_initial, wip=wip, supplier=supplier
    )


# ---------------------------------------------------------------------------
# §7 — freeze-table writers (per-run, per-version, frozen_at=now)
# ---------------------------------------------------------------------------
def _write_freeze_baseline(
    db: Session,
    run: PlanningRun,
    new_version: int,
    item_ids: List[int],
    stock_initial: Dict[int, float],
    now: datetime,
) -> int:
    """Frozen supply position per pool for every item with gross > 0.

    ``stock_qty`` = S0 (equal across every run of the pool = pool anchor v2 §6).
    ``produced_total`` / ``received_total`` = cumulative facts (kept for later
    Δ), stamped identically for every run. ``unit_coef`` = 1.0 (§15.2 risk).
    """
    if not item_ids:
        return 0
    produced_by_item = {
        int(iid): _to_float(qty)
        for iid, qty in (
            db.query(ProductionProduct.item_id, func.sum(ProductionProduct.produced_qty))
            .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
            .filter(ProductionProduct.item_id.in_(item_ids))
            .filter(ProductionOrder.deletion_mark.is_(False))
            .group_by(ProductionProduct.item_id)
            .all()
        )
    }
    received_by_item = {
        int(iid): _to_float(qty)
        for iid, qty in (
            db.query(SupplierOrderItem.item_id_ref, func.sum(SupplierOrderItem.received_qty))
            .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
            .filter(SupplierOrderItem.item_id_ref.in_(item_ids))
            .filter(SupplierOrder.deletion_mark.is_(False))
            .group_by(SupplierOrderItem.item_id_ref)
            .all()
        )
    }
    count = 0
    for iid in item_ids:
        pk = pool_key_for(int(iid))
        db.add(
            MrpFreezeBaseline(
                run_id=int(run.run_id),
                freeze_version=int(new_version),
                item_id=int(iid),
                characteristic_ref=pk.characteristic_ref,
                organization_ref=pk.organization_ref,
                planning_stock_pool=pk.planning_stock_pool,
                frozen_at=now,
                stock_qty=_to_float(stock_initial.get(int(iid), 0.0)),
                produced_total=produced_by_item.get(int(iid), 0.0),
                received_total=received_by_item.get(int(iid), 0.0),
                unit_coef=1.0,
            )
        )
        count += 1
    return count


def _write_freeze_allocation(
    db: Session,
    run: PlanningRun,
    new_version: int,
    trace: FreezeTrace,
    req_by_item: Dict[int, MrpRequirement],
    stock_initial: Dict[int, float],
    now: datetime,
) -> int:
    """Coverage-carrying frozen allocations from the per-run trace.

    Invariants: Σ stock-alloc per item ≤ stock_initial; per-req
    initial_snapshot_stock == Σ its stock allocation. WIP is aggregated per
    product line, supplier per supplier-order line.
    """
    count = 0
    for iid, itrace in trace.by_item.items():
        req = req_by_item.get(int(iid))
        if req is None:
            continue
        pk = pool_key_for(int(iid))
        rid = int(req.id)

        if itrace.stock_alloc > EPS:
            db.add(
                MrpFreezeAllocation(
                    run_id=int(run.run_id),
                    freeze_version=int(new_version),
                    requirement_id=rid,
                    item_id=int(iid),
                    characteristic_ref=pk.characteristic_ref,
                    organization_ref=pk.organization_ref,
                    planning_stock_pool=pk.planning_stock_pool,
                    source_type="stock",
                    source_ref="default",
                    source_line_ref="",
                    alloc_qty=float(itrace.stock_alloc),
                    fact_at_freeze=_to_float(stock_initial.get(int(iid), 0.0)),
                    realized_qty=0.0,
                    evaporated_qty=0.0,
                    created_at=now,
                )
            )
            count += 1

        wip_agg: Dict[int, Dict[str, Any]] = {}
        for line, used in itrace.wip_allocs:
            if used <= EPS:
                continue
            entry = wip_agg.setdefault(
                int(line.product_id),
                {
                    "order_ref1c": line.order_ref1c,
                    "order_id": int(line.order_id),
                    "used": 0.0,
                    "fact": float(line.fact_at_freeze),
                },
            )
            entry["used"] += float(used)
        for product_id, entry in sorted(wip_agg.items()):
            ref = entry["order_ref1c"] or f"local:{entry['order_id']}"
            db.add(
                MrpFreezeAllocation(
                    run_id=int(run.run_id),
                    freeze_version=int(new_version),
                    requirement_id=rid,
                    item_id=int(iid),
                    characteristic_ref=pk.characteristic_ref,
                    organization_ref=pk.organization_ref,
                    planning_stock_pool=pk.planning_stock_pool,
                    source_type="wip_order",
                    source_ref=str(ref),
                    source_line_ref=str(product_id),
                    alloc_qty=float(entry["used"]),
                    fact_at_freeze=float(entry["fact"]),
                    realized_qty=0.0,
                    evaporated_qty=0.0,
                    created_at=now,
                )
            )
            count += 1

        sup_agg: Dict[Any, Dict[str, Any]] = {}
        for row, used in itrace.supplier_allocs:
            if used <= EPS:
                continue
            line_id = row.get("line_id")
            key = int(line_id) if line_id is not None else id(row)
            entry = sup_agg.setdefault(
                key,
                {
                    "order_ref1c": row.get("order_ref1c"),
                    "order_id": row.get("order_id"),
                    "used": 0.0,
                    "fact": _to_float(row.get("fact_at_freeze", 0.0)),
                    "line_id": line_id,
                },
            )
            entry["used"] += float(used)
        for _key, entry in sorted(sup_agg.items(), key=lambda kv: str(kv[0])):
            order_id = entry["order_id"]
            ref = entry["order_ref1c"] or (
                f"local:{order_id}" if order_id is not None else "local"
            )
            line_ref = entry["line_id"]
            db.add(
                MrpFreezeAllocation(
                    run_id=int(run.run_id),
                    freeze_version=int(new_version),
                    requirement_id=rid,
                    item_id=int(iid),
                    characteristic_ref=pk.characteristic_ref,
                    organization_ref=pk.organization_ref,
                    planning_stock_pool=pk.planning_stock_pool,
                    source_type="supplier_order",
                    source_ref=str(ref),
                    source_line_ref=str(line_ref if line_ref is not None else _key),
                    alloc_qty=float(entry["used"]),
                    fact_at_freeze=float(entry["fact"]),
                    realized_qty=0.0,
                    evaporated_qty=0.0,
                    created_at=now,
                )
            )
            count += 1
    return count


def _write_freeze_component(
    db: Session,
    run: PlanningRun,
    new_version: int,
    trace: FreezeTrace,
    now: datetime,
) -> int:
    """Frozen BOM norms from the trace, aggregated per (parent, component, spec).

    Coverage: every parent with gross > 0 and a default spec (including
    stock-covered parents whose explosion was skipped).
    """
    if not trace.component_norms:
        return 0
    agg: Dict[Tuple[int, int, int], float] = {}
    spec_ids: Set[int] = set()
    for parent, component, spec_id, norm in trace.component_norms:
        key = (int(parent), int(component), int(spec_id))
        agg[key] = agg.get(key, 0.0) + float(norm)
        spec_ids.add(int(spec_id))

    spec_ref_by_id = {
        int(sid): (str(ref) if ref else None)
        for sid, ref in (
            db.query(Specification.spec_id, Specification.spec_ref1c)
            .filter(Specification.spec_id.in_(list(spec_ids)))
            .all()
        )
    }
    count = 0
    for (parent, component, spec_id), norm in sorted(agg.items()):
        ppk = pool_key_for(parent)
        cpk = pool_key_for(component)
        spec_ref = spec_ref_by_id.get(int(spec_id)) or str(spec_id)
        db.add(
            MrpFreezeComponent(
                run_id=int(run.run_id),
                freeze_version=int(new_version),
                parent_item_id=int(parent),
                parent_characteristic_ref=ppk.characteristic_ref,
                parent_organization_ref=ppk.organization_ref,
                parent_planning_stock_pool=ppk.planning_stock_pool,
                component_item_id=int(component),
                component_characteristic_ref=cpk.characteristic_ref,
                component_organization_ref=cpk.organization_ref,
                component_planning_stock_pool=cpk.planning_stock_pool,
                spec_ref=str(spec_ref),
                spec_version=None,
                norm_qty_per_unit=float(norm),
                unit_coef=1.0,
                created_at=now,
            )
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# §6 — orchestrator
# ---------------------------------------------------------------------------
def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _next_freeze_version(db: Session, run: PlanningRun) -> int:
    """``max(max(baseline.version for run), run.active_freeze_version) + 1`` — a
    refreeze ALWAYS writes a fresh version; prior versions are never mutated."""
    max_baseline = (
        db.query(func.max(MrpFreezeBaseline.freeze_version))
        .filter(MrpFreezeBaseline.run_id == int(run.run_id))
        .scalar()
    )
    base = max(int(max_baseline or 0), int(run.active_freeze_version or 0))
    return base + 1


def refreeze_active_snapshots(
    db: Session,
    *,
    include_plan_id: Optional[int] = None,
    dry_run: bool = False,
    started_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-freeze every open FIXED_SNAPSHOT run against one shared pool (v2 §6).

    The queue is processed earliest-need-first, consuming stock / WIP / supplier
    supply once. When ``include_plan_id`` is set (the create-snapshot entry
    point) that plan's run header is created/refreshed and added to the queue;
    other runs' headers are untouched. One commit per operation; ``dry_run``
    rolls the whole queue back after building the report.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MRP_LEDGER_LOCK_KEY})

    now = datetime.now(timezone.utc)
    from .period_plan_service import _freeze_one_run, _prepare_include_run

    active_run_ids: List[int] = [int(r) for r in _latest_active_snapshot_run_ids(db)]
    include_run_id: Optional[int] = None
    if include_plan_id is not None:
        include_run = _prepare_include_run(db, int(include_plan_id), started_by, now)
        include_run_id = int(include_run.run_id)
        if include_run_id not in active_run_ids:
            active_run_ids.append(include_run_id)

    active_run_ids = sorted({int(r) for r in active_run_ids})
    frozen_at = now.isoformat()
    if not active_run_ids:
        return {
            "status": "ok",
            "dry_run": bool(dry_run),
            "frozen_at": frozen_at,
            "order": [],
            "results": [],
            "totals": {},
        }

    runs = {
        int(r.run_id): r
        for r in db.query(PlanningRun).filter(PlanningRun.run_id.in_(active_run_ids)).all()
    }
    plan_ids = {
        int(r.source_plan_id) for r in runs.values() if r.source_plan_id is not None
    }
    plans = (
        {
            int(p.id): p
            for p in db.query(ProductionPlanHeader)
            .filter(ProductionPlanHeader.id.in_(plan_ids))
            .all()
        }
        if plan_ids
        else {}
    )

    ordered_ids = sorted(
        active_run_ids,
        key=lambda rid: (
            runs[rid].period_from or date.min,
            runs[rid].period_to or date.max,
            int(rid),
        ),
    )

    # Exclusion sets are built over the FULL active set BEFORE the pools.
    own_wip = _own_wip_product_ids(db, active_run_ids)
    own_sup = _own_supplier_order_ids(db, active_run_ids)
    pools = build_shared_pools(
        db,
        active_run_ids,
        exclude_wip_product_ids=own_wip,
        exclude_supplier_order_ids=own_sup,
    )

    results: List[Dict[str, Any]] = []
    for rid in ordered_ids:
        run = runs[rid]
        plan = (
            plans.get(int(run.source_plan_id)) if run.source_plan_id is not None else None
        )
        if plan is None:
            continue
        new_version = _next_freeze_version(db, run)
        trace = FreezeTrace()
        res = _freeze_one_run(
            db,
            run,
            plan,
            shared_pools=pools,
            trace=trace,
            now=now,
            new_version=new_version,
            is_include=(rid == include_run_id),
        )
        results.append(res)

    if dry_run:
        db.rollback()
    else:
        db.commit()

    totals = {
        "runs": len(results),
        "requirement_count": sum(int(r.get("requirement_count", 0)) for r in results),
        "production_count": sum(int(r.get("production_count", 0)) for r in results),
        "purchase_count": sum(int(r.get("purchase_count", 0)) for r in results),
        "rework_count": sum(int(r.get("rework_count", 0)) for r in results),
        "baseline_rows": sum(int(r.get("baseline_rows", 0)) for r in results),
        "allocation_rows": sum(int(r.get("allocation_rows", 0)) for r in results),
        "component_rows": sum(int(r.get("component_rows", 0)) for r in results),
    }
    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "frozen_at": frozen_at,
        "order": [int(r) for r in ordered_ids],
        "results": results,
        "totals": totals,
    }


def _latest_active_snapshot_run_ids(db: Session) -> List[int]:
    """Latest FIXED_SNAPSHOT run per open plan. Delegates to the reconciliation
    helper (single source of truth); imported locally to avoid a module cycle."""
    from .mrp_reconciliation import _latest_active_snapshot_run_ids as _impl

    return list(_impl(db))

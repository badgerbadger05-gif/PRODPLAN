"""Freeze v2 orchestrator — ``freeze_candidate_snapshots`` and its machinery.

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

The heavy per-run body (``_freeze_one_run``) lives in
:mod:`period_plan_service`; this module owns the pool construction, the pool
key, the freeze-table writers and the queue orchestration. The module cycle
(mrp_freeze → period_plan_service) is one-way: period_plan_service imports back
only locally, inside function bodies.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    DefaultSpecification,
    LedgerGeneration,
    LedgerFutureSupply,
    MrpFreezeAllocation,
    MrpFreezeBaseline,
    MrpFreezeComponent,
    MrpFreezeComponentCumulative,
    MrpRequirement,
    PlanningRun,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionProduct,
    ReservationConsumptionAllocation,
    ReservationEntry,
    SpecComponent,
    Specification,
    StockBin,
    StockLedgerEntry,
)
from .mrp_stock_helpers import (
    WipSupplyLine,
    apply_planning_warehouse_scope,
    planning_stock_by_item,
    planning_warehouse_scope,
)
from .planning_truth import (
    CAPABILITY_EXECUTION_ALLOCATIONS,
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_RESERVATION_REPLAY,
    PlanningTruthUnavailable,
    require_accepted_truth,
)
from .bom_specification_resolver import BomSpecificationResolver
from .planning_pool_resolver import DEFAULT_STOCK_POOL

__all__ = [
    "PoolKey",
    "pool_key_for",
    "WipSupplyLine",
    "FreezeSharedPools",
    "ItemFreezeTrace",
    "FreezeTrace",
    "build_shared_pools",
    "freeze_candidate_snapshots",
]

EPS = 1e-9
FIXED_SNAPSHOT_STATUS = "FIXED_SNAPSHOT"

EMPTY_REF = ""

# Shared by freeze and generation publication so they never interleave. SQLite is
# a no-op (single-writer); PG serialises the maintenance operation.
MRP_LEDGER_LOCK_KEY = 0x4D52504C444752  # "MRPLDGR"
MRP_REQUIRED_CAPABILITIES = (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_RESERVATION_REPLAY,
    CAPABILITY_EXECUTION_ALLOCATIONS,
)


def _assert_full_pool_qualifiers_default(
    *, row: Any, kind: str, context: str
) -> None:
    item_id = row.item_id if hasattr(row, "item_id") else None
    if (
        str(getattr(row, "characteristic_ref", "") or EMPTY_REF) != EMPTY_REF
        or str(getattr(row, "organization_ref", "") or EMPTY_REF) != EMPTY_REF
        or str(getattr(row, "planning_stock_pool", "") or DEFAULT_STOCK_POOL)
        != DEFAULT_STOCK_POOL
    ):
        suffix = f" run {int(row.run_id)}" if getattr(row, "run_id", None) is not None else ""
        suffix_item = f" item {int(item_id)}" if item_id else ""
        raise LedgerPoolUnavailable(
            f"ledger_pool_unavailable: {kind} {context} has pool qualifier unavailable{suffix}{suffix_item}"
        )


class LedgerPoolUnavailable(RuntimeError):
    """MRP cannot safely construct a Ledger-native coverage pool."""

    code = "ledger_pool_unavailable"


# ---------------------------------------------------------------------------
#  — pool key (pragmatic single-pool today; the ONLY place to widen later)
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
#  — shared structures
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
    baseline_at: datetime | None = None
    physical_import_batch_id: int | None = None


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
    root_item_ids: Set[int] = field(default_factory=set)
    # (parent_item_id, component_item_id, spec_id, norm_per_unit)
    component_norms: List[Tuple[int, int, int, float]] = field(default_factory=list)


def _raise_unavailable_if_characteristics_present(
    rows: list[Any],
    *,
    context: str,
) -> None:
    if rows:
        raise LedgerPoolUnavailable(
            "ledger_pool_unavailable: physical pool qualifier unavailable "
            f"in {context}"
        )


# ---------------------------------------------------------------------------
#  — pool construction (ONCE for the whole queue)
# ---------------------------------------------------------------------------
def build_shared_pools(
    db: Session,
    active_run_ids: Iterable[int],
    *,
    ledger_generation_id: int,
    relevant_item_ids: Optional[Iterable[int]] = None,
    stock_baseline_at: datetime | None = None,
) -> FreezeSharedPools:
    """Build consume-once pools from one exact *candidate* generation only.

    ``StockBin`` is the copied immutable physical prefix.  WIP and supplier
    availability is read exclusively from qualified ``LedgerFutureSupply``
    rows captured for this candidate.  In particular, this function must not
    look at ``ProductionProduct.remaining_qty`` or supplier-order counters:
    those are source-system projections, never planning facts.
    """
    relevant_ids = {int(i) for i in (relevant_item_ids or ())}
    generation = db.get(LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise LedgerPoolUnavailable(
            f"ledger_pool_unavailable: generation {ledger_generation_id} is missing"
        )
    if stock_baseline_at is not None:
        from .item_ledger.physical_visibility import visible_sle_query

        baseline_scope = planning_warehouse_scope(db)
        baseline_query = visible_sle_query(
            db,
            physical_import_batch_id=int(generation.physical_import_batch_id),
            cutoff=stock_baseline_at,
        ).filter(func.abs(func.coalesce(StockLedgerEntry.qty, 0)) > EPS).with_entities(
            StockLedgerEntry.id, StockLedgerEntry.item_id, StockLedgerEntry.characteristic_ref
        )
        baseline_query = apply_planning_warehouse_scope(
            baseline_query,
            baseline_scope,
            warehouse_column=StockLedgerEntry.warehouse_ref1c,
            organization_column=StockLedgerEntry.organization_ref,
        )
        if relevant_ids:
            baseline_query = baseline_query.filter(StockLedgerEntry.item_id.in_(relevant_ids))
        _raise_unavailable_if_characteristics_present(
            [
                row
                for row in baseline_query.all()
                if str(getattr(row, "characteristic_ref", "") or "").strip()
            ],
            context="historical physical rows",
        )
        _assert_baseline_within_physical_history(
            db,
            physical_import_batch_id=int(generation.physical_import_batch_id),
            baseline_at=stock_baseline_at,
        )
    else:
        current_scope = planning_warehouse_scope(db)
        current_query = (
            db.query(StockBin.id, StockBin.item_id, StockBin.characteristic_ref)
            .filter(StockBin.ledger_generation_id == int(ledger_generation_id))
            .filter(func.abs(func.coalesce(StockBin.on_hand, 0)) > EPS)
        )
        current_query = apply_planning_warehouse_scope(
            current_query,
            current_scope,
            warehouse_column=StockBin.warehouse_ref1c,
            organization_column=StockBin.organization_ref,
        )
        if relevant_ids:
            current_query = current_query.filter(StockBin.item_id.in_(relevant_ids))
        _raise_unavailable_if_characteristics_present(
            [
                row
                for row in current_query.all()
                if str(getattr(row, "characteristic_ref", "") or "").strip()
            ],
            context="current physical stock",
        )
    stock = (
        _ledger_stock_by_item_at(
            db,
            physical_import_batch_id=int(generation.physical_import_batch_id),
            cutoff=stock_baseline_at,
        )
        if stock_baseline_at is not None
        else _ledger_stock_by_item_all(db, int(ledger_generation_id))
    )
    stock_initial = dict(stock)
    future_rows = (
        db.query(LedgerFutureSupply)
        .filter(LedgerFutureSupply.ledger_generation_id == int(ledger_generation_id))
        .filter(LedgerFutureSupply.evidence_status == "exact")
        .filter(LedgerFutureSupply.open_qty_at_cutoff > EPS)
        .order_by(
            LedgerFutureSupply.item_id.asc(),
            LedgerFutureSupply.eta_date.asc(),
            LedgerFutureSupply.id.asc(),
        )
        .all()
    )
    wip: Dict[int, List[WipSupplyLine]] = defaultdict(list)
    supplier: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in future_rows:
        item_id = int(row.item_id)
        if relevant_ids and item_id not in relevant_ids:
            continue
        # A line without stable source identities cannot be an allocation
        # source.  The capture adapter should mark it rejected; fail closed if
        # malformed data bypassed that boundary.
        if not str(row.source_ref or "").strip() or not str(row.source_line_ref or "").strip():
            raise LedgerPoolUnavailable(
                "ledger_pool_unavailable: exact future supply lacks source identity"
            )
        if not str(row.planning_stock_pool or "").strip() or not str(row.destination_warehouse_ref1c or "").strip():
            raise LedgerPoolUnavailable(
                "ledger_pool_unavailable: exact future supply lacks pool or destination warehouse"
            )
        if str(row.planning_stock_pool) != DEFAULT_STOCK_POOL:
            raise LedgerPoolUnavailable(
                "ledger_pool_unavailable: MRP cannot mix non-default future-supply pools"
            )
        eta = row.eta_date
        if row.supply_kind == "wip_order":
            try:
                local_id = int(row.source_local_id or row.id)
            except (TypeError, ValueError):
                local_id = int(row.id)
            wip[item_id].append(WipSupplyLine(
                eta=eta,
                remaining=_to_float(row.open_qty_at_cutoff),
                fact_at_freeze=_to_float(row.open_qty_at_cutoff),
                order_id=local_id,
                order_ref1c=str(row.source_ref),
                product_id=int(row.id),
                source_line_ref=str(row.source_line_ref),
            ))
        elif row.supply_kind == "supplier_order":
            if eta is None:
                raise LedgerPoolUnavailable(
                    "ledger_pool_unavailable: exact supplier future supply lacks ETA"
                )
            supplier[item_id].append({
                "order_ref1c": str(row.source_ref),
                "source_ref": str(row.source_ref),
                "source_line_ref": str(row.source_line_ref),
                "order_id": None,
                "line_id": int(row.id),
                "delivery_date": eta or date.min,
                "remaining_qty": _to_float(row.open_qty_at_cutoff),
                "fact_at_freeze": _to_float(row.open_qty_at_cutoff),
            })
        else:
            raise LedgerPoolUnavailable(
                f"ledger_pool_unavailable: unknown future supply kind {row.supply_kind!r}"
            )
    for lines in wip.values():
        lines.sort(key=lambda line: (line.eta or date.min, line.order_ref1c or "", line.product_id))
    for lines in supplier.values():
        lines.sort(key=lambda line: (line["delivery_date"], line["order_ref1c"], line["line_id"]))
    return FreezeSharedPools(
        stock=stock,
        stock_initial=stock_initial,
        wip=dict(wip),
        supplier=dict(supplier),
        baseline_at=stock_baseline_at,
        physical_import_batch_id=int(generation.physical_import_batch_id),
    )


def _as_utc(value: datetime) -> datetime:
    """Normalise DB/test datetimes for physical-history boundary comparisons."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _assert_baseline_within_physical_history(
    db: Session,
    *,
    physical_import_batch_id: int,
    baseline_at: datetime,
) -> None:
    """Reject a stock baseline outside the imported physical-history prefix.

    An empty aggregate before the opening boundary is not a factual zero.  It is
    unavailable history and must stop MRP before immutable requirements are
    written.
    """
    from .item_ledger.opening_balance_reconcile import opening_boundary

    boundary = opening_boundary(db)
    if boundary is None:
        raise LedgerPoolUnavailable(
            "ledger_pool_unavailable: physical history has no opening boundary "
            "for the requested stock baseline"
        )
    opening_batch, opening_at = boundary
    if int(opening_batch.id) > int(physical_import_batch_id):
        raise LedgerPoolUnavailable(
            "ledger_pool_unavailable: physical opening boundary is outside "
            f"import prefix {physical_import_batch_id}"
        )
    if _as_utc(baseline_at) < _as_utc(opening_at):
        raise LedgerPoolUnavailable(
            "ledger_pool_unavailable: stock baseline "
            f"{_as_utc(baseline_at).isoformat()} precedes physical opening "
            f"{_as_utc(opening_at).isoformat()}"
        )


def _ledger_stock_by_item_all(db: Session, ledger_generation_id: int) -> Dict[int, float]:
    """Read planning stock from one exact accepted generation, with no fallback."""
    return planning_stock_by_item(db, int(ledger_generation_id))


def _ledger_stock_by_item_at(
    db: Session,
    *,
    physical_import_batch_id: int,
    cutoff: datetime,
) -> Dict[int, float]:
    """Signed physical stock at one immutable business-time boundary."""
    from .item_ledger.physical_visibility import visible_sle_query

    scope = planning_warehouse_scope(db)
    query = visible_sle_query(
        db,
        physical_import_batch_id=int(physical_import_batch_id),
        cutoff=cutoff,
    ).order_by(None).with_entities(
        StockLedgerEntry.item_id,
        func.sum(StockLedgerEntry.qty),
    )
    query = apply_planning_warehouse_scope(
        query,
        scope,
        warehouse_column=StockLedgerEntry.warehouse_ref1c,
        organization_column=StockLedgerEntry.organization_ref,
    )
    return {
        int(item_id): _to_float(qty)
        for item_id, qty in query.group_by(StockLedgerEntry.item_id).all()
    }


# ---------------------------------------------------------------------------
#  — freeze-table writers (per-run, per-version, frozen_at=now)
# ---------------------------------------------------------------------------
def _write_freeze_baseline(
    db: Session,
    run: PlanningRun,
    new_version: int,
    item_ids: List[int],
    stock_initial: Dict[int, float],
    now: datetime,
    *,
    baseline_at: datetime | None,
    physical_import_batch_id: int | None,
) -> int:
    """Frozen supply position per pool for every item with gross > 0.

    ``stock_qty`` = S0 (equal across every run of the pool = pool anchor v2 ).
    ``produced_total`` / ``received_total`` = cumulative facts (kept for later
    Δ), stamped identically for every run. ``unit_coef`` = 1.0 ( risk).
    """
    if not item_ids:
        return 0
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
                baseline_at=baseline_at,
                physical_import_batch_id=physical_import_batch_id,
                stock_qty=_to_float(stock_initial.get(int(iid), 0.0)),
                # Deprecated diagnostic columns. They must never be populated
                # from ProductionProduct/SupplierOrderItem legacy counters.
                produced_total=0.0,
                received_total=0.0,
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

        wip_agg: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for line, used in itrace.wip_allocs:
            if used <= EPS:
                continue
            source_ref = str(line.order_ref1c or "").strip()
            source_line_ref = str(line.source_line_ref or "").strip()
            if not source_ref or not source_line_ref:
                raise LedgerPoolUnavailable(
                    "ledger_pool_unavailable: WIP allocation lacks stable source identity"
                )
            entry = wip_agg.setdefault(
                (source_ref, source_line_ref),
                {
                    "source_ref": source_ref,
                    "source_line_ref": source_line_ref,
                    "used": 0.0,
                    "fact": float(line.fact_at_freeze),
                },
            )
            entry["used"] += float(used)
        for _identity, entry in sorted(wip_agg.items()):
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
                    source_ref=entry["source_ref"],
                    source_line_ref=entry["source_line_ref"],
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
            source_ref = str(row.get("source_ref") or row.get("order_ref1c") or "").strip()
            source_line_ref = str(row.get("source_line_ref") or "").strip()
            if not source_ref or not source_line_ref:
                raise LedgerPoolUnavailable(
                    "ledger_pool_unavailable: supplier allocation lacks stable source identity"
                )
            key = (source_ref, source_line_ref)
            entry = sup_agg.setdefault(
                key,
                {
                    "source_ref": source_ref,
                    "source_line_ref": source_line_ref,
                    "used": 0.0,
                    "fact": _to_float(row.get("fact_at_freeze", 0.0)),
                },
            )
            entry["used"] += float(used)
        for _key, entry in sorted(sup_agg.items(), key=lambda kv: str(kv[0])):
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
                    source_ref=entry["source_ref"],
                    source_line_ref=entry["source_line_ref"],
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


def _write_freeze_component_cumulative(
    db: Session,
    run: PlanningRun,
    new_version: int,
    trace: FreezeTrace,
) -> int:
    """Write cumulative root->component BOM norms for one freeze run/version."""
    if not trace.component_norms or not trace.root_item_ids:
        return 0

    # Trace data is immediate parent->child norms from the BOM explosion.
    # Build it into an adjacency list before propagating to cumulative values.
    zero = Decimal("0")
    immediate_norm_by_edge: Dict[Tuple[int, int], Decimal] = {}
    graph: Dict[int, list[tuple[int, Decimal]]] = defaultdict(list)
    for parent, component, _spec_id, norm in trace.component_norms:
        decimal_norm = Decimal(str(norm))
        if decimal_norm <= zero:
            continue
        parent_id = int(parent)
        child_id = int(component)
        key = (parent_id, child_id)
        immediate_norm_by_edge[key] = immediate_norm_by_edge.get(key, zero) + decimal_norm

    for (parent_id, component_id), norm in sorted(immediate_norm_by_edge.items()):
        graph[parent_id].append((component_id, norm))

    for parent_id, edges in graph.items():
        graph[parent_id] = sorted(edges, key=lambda item: item[0])

    cumulative_norm_by_root_component: Dict[Tuple[int, int], Decimal] = {}

    for root_id in sorted(trace.root_item_ids):
        stack: list[tuple[int, Decimal, set[int]]] = [
            (int(root_id), Decimal("1"), {int(root_id)})
        ]
        while stack:
            parent_id, path_factor, ancestors = stack.pop()
            for component_id, parent_norm in graph.get(parent_id, []):
                if component_id in ancestors:
                    continue
                cumulative_norm = path_factor * parent_norm
                if cumulative_norm <= zero:
                    continue
                key = (int(root_id), int(component_id))
                cumulative_norm_by_root_component[key] = (
                    cumulative_norm_by_root_component.get(key, zero) + cumulative_norm
                )
                next_ancestors = set(ancestors)
                next_ancestors.add(int(component_id))
                stack.append((int(component_id), cumulative_norm, next_ancestors))

    if not cumulative_norm_by_root_component:
        return 0

    count = 0
    for (root_id, component_id), norm in sorted(cumulative_norm_by_root_component.items()):
        if norm <= zero:
            continue
        db.add(
            MrpFreezeComponentCumulative(
                run_id=int(run.run_id),
                freeze_version=int(new_version),
                root_item_id=int(root_id),
                component_item_id=int(component_id),
                cumulative_norm_qty_per_root_unit=norm,
            )
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
#  — orchestrator
# ---------------------------------------------------------------------------
def _to_float(value: Any) -> float:
    """Frozen-quantity coercion — fail closed, never a silent zero.

    This runs on the WRITE path of the frozen obligation (baselines, allocations,
    retained claims). Swallowing a malformed quantity into ``0.0`` produced an
    immutable, permanently wrong freeze with no trace of the failure.
    """
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise LedgerPoolUnavailable(
            f"frozen quantity is not numeric: {value!r}"
        ) from exc


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
    """Retired destructive entry point.

    Recalculating published snapshots used to rewrite fixed runs and append
    reservations into the accepted Ledger generation.  That is no longer a
    valid operation.  The caller must first fork an obligation generation and
    build explicit ``BUILDING_SNAPSHOT`` run headers, then call
    :func:`freeze_candidate_snapshots`.
    """
    raise LedgerPoolUnavailable(
        "refreeze_active_snapshots is retired; freeze explicit BUILDING_SNAPSHOT candidates"
    )


def freeze_candidate_snapshots(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
    candidate_run_ids: Iterable[int],
) -> Dict[str, Any]:
    """Materialize fresh MRP rows only into an obligation-refresh candidate.

    Parents remain published and byte-for-byte untouched until the separate
    publish transaction supersedes them.  This function owns no implicit
    candidate discovery and performs no commit on failure; the refresh worker
    supplies the exact parent/target/candidate manifest.
    """
    parent_id = int(parent_generation_id)
    target_id = int(target_generation_id)
    requested_ids = sorted({int(value) for value in candidate_run_ids})
    if not requested_ids:
        raise LedgerPoolUnavailable("candidate freeze requires candidate run ids")
    try:
        readiness = require_accepted_truth(
            db, consumer="mrp_freeze.candidate", required_capabilities=MRP_REQUIRED_CAPABILITIES
        )
    except PlanningTruthUnavailable as exc:
        reason = getattr(exc.readiness, "reason", None) or str(exc)
        raise LedgerPoolUnavailable(
            "candidate freeze parent is not the current accepted Ledger generation: "
            f"{reason}"
        ) from exc
    if int(readiness.ledger_generation or 0) != parent_id:
        raise LedgerPoolUnavailable("candidate freeze parent is not the current accepted Ledger generation")

    parent = db.get(LedgerGeneration, parent_id)
    target = db.get(LedgerGeneration, target_id)
    if parent is None or str(parent.status) != "accepted":
        raise LedgerPoolUnavailable("candidate freeze parent is not the current accepted Ledger generation")
    if target is None or str(target.status) != "building":
        raise LedgerPoolUnavailable("candidate freeze target must be a BUILDING Ledger generation")
    marks = dict(target.source_watermarks or {})
    if marks.get("generation_kind") != "obligation_refresh" or marks.get("parent_generation_id") != parent_id:
        raise LedgerPoolUnavailable("candidate freeze target does not descend from accepted generation")
    if target.cutoff != parent.cutoff or target.physical_import_batch_id != parent.physical_import_batch_id:
        raise LedgerPoolUnavailable("candidate freeze target does not share immutable physical prefix")

    # A build is a closed set.  Do this validation before deriving anything so
    # a worker cannot select only the convenient candidates from a sealed
    # manifest batch for a candidate freeze build (or slip an unsealed
    # candidate into it).
    from .obligation_refresh_manifest import MANIFEST_HASH_KEY, MANIFEST_KEY

    watermarks = dict(target.source_watermarks or {})
    manifest = watermarks.get(MANIFEST_KEY)
    manifest_hash = watermarks.get(MANIFEST_HASH_KEY)
    if not isinstance(manifest, Mapping) or not isinstance(manifest_hash, str):
        raise LedgerPoolUnavailable("candidate freeze target lacks a sealed obligation_refresh_manifest")
    canonical_manifest = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if sha256(canonical_manifest.encode("utf-8")).hexdigest() != manifest_hash:
        raise LedgerPoolUnavailable("candidate freeze obligation_refresh_manifest hash conflicts")
    entries = manifest.get("entries")
    add_request = manifest.get("add_request")
    if not isinstance(entries, list) or not isinstance(add_request, Mapping):
        raise LedgerPoolUnavailable("candidate freeze obligation_refresh_manifest is malformed")
    if not isinstance(add_request.get("config_snapshot"), Mapping):
        raise LedgerPoolUnavailable("candidate freeze add config is malformed")

    manifest_entries: Dict[int, Mapping[str, Any]] = {}
    retained_entries: List[Mapping[str, Any]] = []
    manifest_plan_ids: Set[int] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise LedgerPoolUnavailable("candidate freeze obligation_refresh_manifest entry is malformed")
        try:
            action = str(entry["action"])
            plan_id = int(entry["plan_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerPoolUnavailable("candidate freeze obligation_refresh_manifest entry identity is malformed") from exc
        if action in {"retain", "retire"}:
            if (
                plan_id <= 0
                or plan_id in manifest_plan_ids
                or entry.get("candidate_run_id") is not None
                or entry.get("parent_run_id") is None
            ):
                raise LedgerPoolUnavailable(f"candidate freeze {action} entry is malformed")
            # A retired parent is being closed by this very build: its frozen
            # claims are released, so it must NOT hold stock or future supply
            # away from the candidates.  Only ``retain`` keeps seniority below.
            if action == "retain":
                retained_entries.append(entry)
            manifest_plan_ids.add(plan_id)
            continue
        if action != "add":
            raise LedgerPoolUnavailable("candidate freeze obligation_refresh_manifest contains unsupported action")
        if plan_id <= 0:
            raise LedgerPoolUnavailable("candidate freeze obligation_refresh_manifest contains unsupported action")
        try:
            candidate_id = int(entry["candidate_run_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerPoolUnavailable("candidate freeze candidate identity is malformed") from exc
        if candidate_id in manifest_entries or candidate_id <= 0 or plan_id in manifest_plan_ids:
            raise LedgerPoolUnavailable("candidate freeze obligation_refresh_manifest has duplicate candidate or plan")
        manifest_entries[candidate_id] = entry
        manifest_plan_ids.add(plan_id)
    if set(manifest_entries) != set(requested_ids):
        raise LedgerPoolUnavailable("candidate freeze ids differ from sealed obligation_refresh_manifest")

    all_target_candidate_ids = {
        int(run_id) for (run_id,) in db.query(PlanningRun.run_id).filter(
            PlanningRun.ledger_generation_id == target_id,
            PlanningRun.status == "BUILDING_SNAPSHOT",
        ).all()
    }
    if all_target_candidate_ids != set(manifest_entries):
        raise LedgerPoolUnavailable("candidate freeze target has missing or extra sealed candidates")

    runs = {
        int(run.run_id): run
        for run in db.query(PlanningRun).filter(PlanningRun.run_id.in_(requested_ids)).all()
    }
    if set(runs) != set(requested_ids):
        raise LedgerPoolUnavailable("candidate freeze manifest names missing PlanningRun rows")
    for run in runs.values():
        if str(run.status) != "BUILDING_SNAPSHOT" or int(run.ledger_generation_id or 0) != target_id:
            raise LedgerPoolUnavailable("candidate freeze runs must be BUILDING_SNAPSHOT rows on target")
        entry = manifest_entries[int(run.run_id)]
        if int(run.source_plan_id or -1) != int(entry["plan_id"]):
            raise LedgerPoolUnavailable("candidate freeze source plan conflicts with sealed manifest")
        if entry.get("parent_run_id") is not None or run.prior_run_id is not None:
            raise LedgerPoolUnavailable("candidate freeze add candidate must not have a parent run")
        plan = db.get(ProductionPlanHeader, int(run.source_plan_id))
        if plan is None or str(plan.status) != "fixed":
            raise LedgerPoolUnavailable("candidate freeze add plan must be fixed")
        if run.period_from != plan.period_from or run.period_to != plan.period_to:
            raise LedgerPoolUnavailable("candidate freeze add candidate period conflicts with fixed plan")
        if (
            run.horizon_days != add_request.get("horizon_days")
            or run.config_version_id != add_request.get("config_version_id")
            or run.config_snapshot != add_request["config_snapshot"]
        ):
            raise LedgerPoolUnavailable("candidate freeze add candidate config conflicts with manifest")
        if db.query(ProductionPlanLine.id).filter(
            ProductionPlanLine.plan_id == int(plan.id),
            ProductionPlanLine.locked_by_run_id.isnot(None),
        ).first() is not None:
            raise LedgerPoolUnavailable("candidate freeze add plan lines must be unlocked")
        # Rebuild is deliberately only for an empty candidate.  Retrying a
        # partial candidate risks preserving stale derived rows.
        existing_requirements = (
            db.query(MrpRequirement)
            .filter(MrpRequirement.run_id == int(run.run_id))
            .all()
        )
        for req in existing_requirements:
            _assert_full_pool_qualifiers_default(
                row=req, kind="candidate", context="MrpRequirement",
            )
        if existing_requirements:
            raise LedgerPoolUnavailable("candidate freeze run already has derived requirements")

    plans = {
        int(plan.id): plan
        for plan in db.query(ProductionPlanHeader)
        .filter(ProductionPlanHeader.id.in_([int(run.source_plan_id) for run in runs.values()]))
        .all()
    }
    if len(plans) != len(runs):
        raise LedgerPoolUnavailable("candidate freeze source plan is missing")

    relevant_item_ids = _relevant_item_ids_for_plans(db, set(plans))
    # Frozen obligation stock is the signed physical Ledger balance immediately
    # before the earliest plan business period starts.  Receipts on/after that
    # boundary are execution facts, never retroactive netting.
    baseline_day = min(plan.period_from for plan in plans.values())
    stock_baseline_at = (
        datetime.combine(baseline_day, time.min) - timedelta(microseconds=1)
    )
    pools = build_shared_pools(
        db, requested_ids, ledger_generation_id=target_id,
        relevant_item_ids=relevant_item_ids,
        stock_baseline_at=stock_baseline_at,
    )
    retained_run_ids = [
        int(entry["parent_run_id"])
        for entry in retained_entries
    ]
    if retained_run_ids:
        # Seniority order for FIFO attribution below: oldest plan first,
        # the same queue key the execution ledger uses everywhere.
        retained_run_ids = [
            int(row.run_id)
            for row in db.query(PlanningRun)
            .filter(PlanningRun.run_id.in_(retained_run_ids))
            .order_by(
                PlanningRun.period_from.asc(),
                PlanningRun.period_to.asc(),
                PlanningRun.run_id.asc(),
            )
            .all()
        ]
        retained_runs = {
            int(row.run_id): int(row.active_freeze_version or 0)
            for row in db.query(PlanningRun).filter(
                PlanningRun.run_id.in_(retained_run_ids)
            ).all()
        }
        retained_reservations = [
            row
            for row in db.query(ReservationEntry).filter(
                ReservationEntry.run_id.in_(retained_run_ids),
                ReservationEntry.ledger_generation_id == target_id,
                ReservationEntry.lifecycle_status == "active",
            ).all()
        ]
        if retained_reservations:
            allocated = {
                int(row.reservation_id): _to_float(row.allocated_qty)
                for row in db.query(
                    ReservationConsumptionAllocation.reservation_id,
                    func.coalesce(
                        func.sum(ReservationConsumptionAllocation.allocated_qty), 0,
                    ).label("allocated_qty"),
                )
                .filter(
                    ReservationConsumptionAllocation.ledger_generation_id == target_id,
                    ReservationConsumptionAllocation.reservation_id.in_(
                        [int(row.id) for row in retained_reservations]
                    ),
                )
                .group_by(ReservationConsumptionAllocation.reservation_id)
                .all()
            }
            retained_stock_by_key: Dict[Tuple[int, str, str, str], float] = defaultdict(float)
            for reservation in retained_reservations:
                # Only the immutable active freeze version may reserve the pool.
                if int(reservation.freeze_version or 0) != retained_runs.get(
                    int(reservation.run_id), 0
                ):
                    continue
                _assert_full_pool_qualifiers_default(
                    row=reservation, kind="retained", context="ReservationEntry",
                )
                reserved_qty = _to_float(reservation.reserved_qty)
                attributed_qty = allocated.get(int(reservation.id), 0.0)
                senior_hold_qty = max(reserved_qty - attributed_qty, 0.0)
                if senior_hold_qty <= EPS:
                    continue
                key = (
                    int(reservation.item_id),
                    str(reservation.characteristic_ref or EMPTY_REF),
                    str(reservation.organization_ref or EMPTY_REF),
                    str(reservation.planning_stock_pool or DEFAULT_STOCK_POOL),
                )
                retained_stock_by_key[key] += senior_hold_qty
            for (item_id, characteristic_ref, organization_ref, planning_stock_pool), hold_qty in (
                retained_stock_by_key.items()
            ):
                available_qty = pools.stock.get(item_id, 0.0)
                pools.stock[item_id] = max(available_qty - hold_qty, 0.0)
        retained_future = db.query(MrpFreezeAllocation).filter(
            MrpFreezeAllocation.run_id.in_(retained_run_ids),
            MrpFreezeAllocation.source_type.in_(("wip_order", "supplier_order")),
            MrpFreezeAllocation.item_id.in_(relevant_item_ids),
        ).all()
        # Oldest plan first: receipts against a shared supply line are
        # attributed to the senior retained claim before anything is left for
        # the new candidate (decisions-log / FIFO discipline).
        retained_order = {
            int(run_id): index for index, run_id in enumerate(retained_run_ids)
        }
        retained_future.sort(key=lambda row: (
            retained_order.get(int(row.run_id), len(retained_order)), int(row.id)
        ))
        for allocation in retained_future:
            if int(allocation.freeze_version) != retained_runs.get(int(allocation.run_id), 0):
                continue
            # MrpFreezeAllocation.realized_qty/evaporated_qty are dead columns
            # (their writer retired with the legacy execution engine): the live
            # measure of "how much of this frozen claim is still outstanding"
            # is the supply line's own ledger remainder (ordered − received).
            # A claim can therefore never exceed the line remainder — whatever
            # was received has already realized the senior claim first.
            retained_qty = _to_float(allocation.alloc_qty)
            if retained_qty <= EPS:
                continue
            item_id = int(allocation.item_id)
            if str(allocation.planning_stock_pool or DEFAULT_STOCK_POOL) != DEFAULT_STOCK_POOL:
                raise LedgerPoolUnavailable(
                    "ledger_pool_unavailable: retained allocation uses unsupported stock pool"
                )
            source_ref = str(allocation.source_ref or "").strip()
            source_line_ref = str(allocation.source_line_ref or "").strip()
            if not source_ref or not source_line_ref:
                raise LedgerPoolUnavailable(
                    "ledger_pool_unavailable: retained future allocation lacks stable source identity"
                )
            if allocation.source_type == "wip_order":
                matches = [
                    line for line in pools.wip.get(item_id, [])
                    if str(line.order_ref1c or "") == source_ref
                    and str(line.source_line_ref or "") == source_line_ref
                ]
                remaining_key = "remaining"
            else:
                matches = [
                    line for line in pools.supplier.get(item_id, [])
                    if str(line.get("source_ref") or line.get("order_ref1c") or "") == source_ref
                    and str(line.get("source_line_ref") or "") == source_line_ref
                ]
                remaining_key = "remaining_qty"
            if len(matches) > 1:
                raise LedgerPoolUnavailable(
                    "ledger_pool_unavailable: retained future allocation source "
                    f"{allocation.source_type}:{source_ref}:{source_line_ref} "
                    f"matched {len(matches)} target rows"
                )
            if not matches:
                # The supply line is fully received/closed: the retained claim
                # is fully realized and there is nothing left to grant the new
                # candidate from it — no exclusion needed, no double count
                # possible (an absent line grants nothing to anyone).
                continue
            line = matches[0]
            available = (
                _to_float(getattr(line, remaining_key))
                if allocation.source_type == "wip_order"
                else _to_float(line[remaining_key])
            )
            # Clip instead of raise: alloc − received > remainder simply means
            # part of the claim already arrived (FIFO-attributed to the senior
            # retained plan); the outstanding claim is capped by the remainder.
            retained_qty = min(retained_qty, available)
            if retained_qty <= EPS:
                continue
            new_remaining = max(available - retained_qty, 0.0)
            if allocation.source_type == "wip_order":
                line.remaining = new_remaining
            else:
                line[remaining_key] = new_remaining
    from .period_plan_service import _freeze_one_run
    now = datetime.now(timezone.utc)
    # Every date this freeze clamps to is the generation cutoff, never the wall
    # clock: rebuilding the same generation tomorrow must yield the same rows.
    cutoff_date = target.cutoff.date() if isinstance(target.cutoff, datetime) else target.cutoff
    if not isinstance(cutoff_date, date):
        raise LedgerPoolUnavailable("candidate freeze target has no usable cutoff")
    results: List[Dict[str, Any]] = []
    for run_id in sorted(requested_ids, key=lambda rid: (runs[rid].period_from or date.min, runs[rid].period_to or date.max, rid)):
        run = runs[run_id]
        run.ledger_cutoff = target.cutoff
        trace = FreezeTrace()
        result = _freeze_one_run(
            db, run, plans[int(run.source_plan_id)], shared_pools=pools,
            trace=trace, now=now, new_version=1, cutoff_date=cutoff_date,
            is_include=True, manage_plan_locks=False,
        )
        results.append(result)

    # Reservation stock coverage is derived from the frozen allocation rows.
    # Test and worker sessions may disable autoflush, so make the allocation
    # checkpoint visible before materializing ReservationEntry.
    db.flush()
    from .item_ledger.reservation_ledger import materialize_reservations_for_freeze
    materialize_reservations_for_freeze(
        db, requested_ids, ledger_generation_id=target_id,
    )
    try:
        readiness = require_accepted_truth(
            db, "candidate freeze", required_capabilities=MRP_REQUIRED_CAPABILITIES
        )
    except PlanningTruthUnavailable as exc:
        raise LedgerPoolUnavailable("accepted Ledger pointer changed during candidate freeze") from exc
    if int(readiness.generation_id or 0) != parent_id:
        raise LedgerPoolUnavailable("accepted Ledger pointer changed during candidate freeze")
    # The refresh coordinator owns the outer atomic transaction: this executor
    # deliberately exposes its writes with flush but never commits or rolls
    # back, including when a later publish validation fails.
    db.flush()
    return {
        "status": "ok", "ledger_generation_id": target_id,
        "parent_generation_id": parent_id, "order": requested_ids, "results": results,
    }


def _relevant_item_ids_for_plans(db: Session, plan_ids: Set[int]) -> Set[int]:
    """Conservative BOM closure used only for pre-write pool validation."""
    if not plan_ids:
        return set()
    relevant = {
        int(item_id)
        for (item_id,) in db.query(ProductionPlanLine.item_id)
        .filter(
            ProductionPlanLine.plan_id.in_(plan_ids),
            ProductionPlanLine.qty > 0,
        )
        .distinct()
        .all()
    }
    descendants = BomSpecificationResolver(db).descendant_ids_by_root(relevant)
    return set().union(*descendants.values()) if descendants else relevant

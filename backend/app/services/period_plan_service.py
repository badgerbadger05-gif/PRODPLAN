from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

if TYPE_CHECKING:  # pragma: no cover - typing only; runtime import is mrp_freeze→here
    from .mrp_freeze import FreezeSharedPools, FreezeTrace

from ..models import (
    DefaultSpecification,
    Item,
    LedgerGeneration,
    MrpRequirement,
    MrpRequirementBucket,
    Operation,
    PlannedOrder,
    PlannedOrderStage,
    PlannedPurchase,
    PlannedRework,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionResource,
    ResourceProductionKind,
    PlanningTruthState,
    SpecComponent,
    SpecOperation,
    Specification,
    SyncLink,
    SupplierOrder,
    SupplierOrderItem,
)
from .planning_service import (
    DEFAULT_PLANNING_CONFIG,
    get_active_planning_config,
)
from .supplier_order_status import (
    normalize_state as _normalize_supplier_order_state_name,
    state_counts_in_mrp as _supplier_order_counts_in_mrp,
)
from .capacity_scheduler import CapacityScheduler
from .mrp_stock_helpers import (
    active_wip_eta_by_item as _active_wip_eta_by_item,
    consume_wip_at_or_before as _consume_wip_at_or_before,
    consume_wip_detailed as _consume_wip_detailed,
    effective_stock_by_item_all as _effective_stock_by_item_all,
)
from .replenishment import (
    REPLENISHMENT_FLOW_PRODUCTION,
    REPLENISHMENT_FLOW_PURCHASE,
    REPLENISHMENT_FLOW_REWORK,
    classify_replenishment_flow,
)


PLAN_STATUSES = {"draft", "fixed", "archived"}

# Matches planning_service.DONE_STATE_KEY — 1C state for completed production orders.
_DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"
_DIRECT_1C_PRODUCTION_HORIZON = date(2026, 5, 1)
_SUPPLIER_ORDER_DONE_STATES = {"принят на склад"}


def _parse_date(value: Any, field: str = "date") -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception as exc:
        raise ValueError(f"Invalid {field}") from exc


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _date_to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10] if value else None


def _forecast_payload(
    forecast_date: Optional[date],
    due_date: Optional[date],
    *,
    reason: str = "capacity",
) -> Dict[str, Any]:
    if not forecast_date or not due_date:
        return {"forecast_date": _date_to_iso(forecast_date), "forecast_shift_days": None, "forecast_reason": None}
    shift = (forecast_date - due_date).days
    if shift > 0:
        reason_text = "смещение по мощностям" if reason == "capacity" else reason
    elif shift < 0:
        reason_text = "раньше плановой даты"
    else:
        reason_text = "в срок"
    return {
        "forecast_date": forecast_date.isoformat(),
        "forecast_shift_days": shift,
        "forecast_reason": reason_text,
    }


def _fridays_between(start: date, finish: date) -> List[date]:
    if finish < start:
        return []
    days_to_friday = (4 - start.weekday()) % 7
    current = start + timedelta(days=days_to_friday)
    out: List[date] = []
    while current <= finish:
        out.append(current)
        current += timedelta(days=7)
    if not out:
        out.append(finish)
    return out


def _serialize_plan(
    plan: ProductionPlanHeader,
    *,
    line_count: Optional[int] = None,
    total_qty: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "id": int(plan.id),
        "name": str(plan.name or ""),
        "period_from": plan.period_from.isoformat() if plan.period_from else None,
        "period_to": plan.period_to.isoformat() if plan.period_to else None,
        "status": str(plan.status or "draft"),
        "comment": str(plan.comment or "") if plan.comment else None,
        "created_by": str(plan.created_by or "") if plan.created_by else None,
        "fixed_by": str(plan.fixed_by or "") if plan.fixed_by else None,
        "fixed_at": plan.fixed_at.isoformat() if plan.fixed_at else None,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
        "line_count": int(line_count or 0) if line_count is not None else None,
        "total_qty": float(total_qty or 0.0) if total_qty is not None else None,
    }


def _is_supplier_order_done(order: Optional[SupplierOrder]) -> bool:
    if order is None or bool(getattr(order, "deletion_mark", False)):
        return False
    state_name = _normalize_supplier_order_state_name(getattr(order, "order_state_name", None))
    return state_name in _SUPPLIER_ORDER_DONE_STATES


def _get_plan(db: Session, plan_id: int) -> ProductionPlanHeader:
    plan = db.query(ProductionPlanHeader).filter(ProductionPlanHeader.id == int(plan_id)).first()
    if not plan:
        raise ValueError("План не найден")
    return plan


def _assert_plan_editable(plan: ProductionPlanHeader) -> None:
    if str(plan.status or "").lower() != "draft":
        raise ValueError("План зафиксирован и недоступен для редактирования")


def list_period_plans(
    db: Session,
    *,
    limit: int = 100,
    offset: int = 0,
    status: Optional[str] = None,
    period_from: Any = None,
    period_to: Any = None,
    created_by: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
) -> Dict[str, Any]:
    q = db.query(ProductionPlanHeader)
    if status:
        q = q.filter(ProductionPlanHeader.status == str(status).strip().lower())
    if period_from:
        q = q.filter(ProductionPlanHeader.period_to >= _parse_date(period_from, "period_from"))
    if period_to:
        q = q.filter(ProductionPlanHeader.period_from <= _parse_date(period_to, "period_to"))
    if created_by:
        q = q.filter(ProductionPlanHeader.created_by.ilike(f"%{str(created_by).strip()}%"))
    total = q.count()
    sort_cols = {
        "name": ProductionPlanHeader.name,
        "status": ProductionPlanHeader.status,
        "period_from": ProductionPlanHeader.period_from,
        "period_to": ProductionPlanHeader.period_to,
        "fixed_at": ProductionPlanHeader.fixed_at,
        "created_at": ProductionPlanHeader.created_at,
    }
    sort_col = sort_cols.get((sort_by or "period_from").lower(), ProductionPlanHeader.period_from)
    sort_expr = sort_col.asc() if (sort_dir or "desc").lower() == "asc" else sort_col.desc()
    plans = (
        q.order_by(sort_expr, ProductionPlanHeader.id.desc())
        .offset(max(0, int(offset or 0)))
        .limit(max(1, min(int(limit or 100), 500)))
        .all()
    )
    plan_ids = [int(plan.id) for plan in plans]
    line_stats: Dict[int, Dict[str, float]] = {}
    if plan_ids:
        stats_rows = (
            db.query(
                ProductionPlanLine.plan_id,
                func.count(ProductionPlanLine.id).label("line_count"),
                func.coalesce(func.sum(ProductionPlanLine.qty), 0.0).label("total_qty"),
            )
            .filter(ProductionPlanLine.plan_id.in_(plan_ids))
            .group_by(ProductionPlanLine.plan_id)
            .all()
        )
        line_stats = {
            int(row.plan_id): {"line_count": int(row.line_count or 0), "total_qty": _to_float(row.total_qty)}
            for row in stats_rows
        }
    return {
        "rows": [
            _serialize_plan(
                plan,
                line_count=int(line_stats.get(int(plan.id), {}).get("line_count", 0)),
                total_qty=float(line_stats.get(int(plan.id), {}).get("total_qty", 0.0)),
            )
            for plan in plans
        ],
        "total": int(total),
    }


def create_period_plan(
    db: Session,
    *,
    name: str,
    period_from: Any,
    period_to: Any,
    created_by: Optional[str] = None,
    comment: Optional[str] = None,
) -> Dict[str, Any]:
    start = _parse_date(period_from, "period_from")
    finish = _parse_date(period_to, "period_to")
    if finish < start:
        raise ValueError("Дата окончания периода не может быть раньше даты начала")
    title = str(name or "").strip()
    if not title:
        raise ValueError("Название плана обязательно")

    # Auto-generate comment from period_from if caller did not provide one.
    # Format: "МАЙ 2026" / "МАЙ–ИЮНЬ 2026" for multi-month ranges.
    effective_comment = comment
    if not effective_comment:
        _RU_MONTHS = [
            "ЯНВАРЬ", "ФЕВРАЛЬ", "МАРТ", "АПРЕЛЬ", "МАЙ", "ИЮНЬ",
            "ИЮЛЬ", "АВГУСТ", "СЕНТЯБРЬ", "ОКТЯБРЬ", "НОЯБРЬ", "ДЕКАБРЬ",
        ]
        if start.year == finish.year and start.month == finish.month:
            effective_comment = f"{_RU_MONTHS[start.month - 1]} {start.year}"
        elif start.year == finish.year:
            effective_comment = f"{_RU_MONTHS[start.month - 1]}–{_RU_MONTHS[finish.month - 1]} {start.year}"
        else:
            effective_comment = f"{_RU_MONTHS[start.month - 1]} {start.year} – {_RU_MONTHS[finish.month - 1]} {finish.year}"

    plan = ProductionPlanHeader(
        name=title,
        period_from=start,
        period_to=finish,
        status="draft",
        created_by=created_by,
        comment=effective_comment,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return _serialize_plan(plan)


def get_period_plan(db: Session, plan_id: int) -> Dict[str, Any]:
    return _serialize_plan(_get_plan(db, plan_id))


def fix_period_plan(db: Session, plan_id: int, *, fixed_by: Optional[str] = None) -> Dict[str, Any]:
    plan = _get_plan(db, plan_id)
    if plan.status == "archived":
        raise ValueError("Архивный план нельзя фиксировать")
    if plan.status != "fixed":
        plan.status = "fixed"
        plan.fixed_by = fixed_by
        plan.fixed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(plan)
    return _serialize_plan(plan)


def archive_period_plan(db: Session, plan_id: int) -> Dict[str, Any]:
    plan = _get_plan(db, plan_id)
    plan.status = "archived"
    db.commit()
    db.refresh(plan)
    return _serialize_plan(plan)


def unarchive_period_plan(db: Session, plan_id: int) -> Dict[str, Any]:
    """Restore plan from archive. Returns to 'fixed' if previously fixed (has fixed_at), else 'draft'."""
    plan = _get_plan(db, plan_id)
    if plan.status != "archived":
        raise ValueError("Только архивный план можно вернуть из архива")
    plan.status = "fixed" if plan.fixed_at else "draft"
    db.commit()
    db.refresh(plan)
    return _serialize_plan(plan)


def update_period_plan_header(
    db: Session,
    plan_id: int,
    *,
    name: Optional[str] = None,
    period_from: Any = None,
    period_to: Any = None,
    comment: Any = None,
) -> Dict[str, Any]:
    """Update editable header fields. Only allowed for draft plans."""
    plan = _get_plan(db, plan_id)
    _assert_plan_editable(plan)
    if name is not None:
        title = str(name).strip()
        if not title:
            raise ValueError("Название плана обязательно")
        plan.name = title
    new_from = _parse_date(period_from, "period_from") if period_from is not None else plan.period_from
    new_to = _parse_date(period_to, "period_to") if period_to is not None else plan.period_to
    if new_to < new_from:
        raise ValueError("Дата окончания периода не может быть раньше даты начала")
    plan.period_from = new_from
    plan.period_to = new_to
    if comment is not None:
        plan.comment = str(comment) if str(comment).strip() else None
    db.commit()
    db.refresh(plan)
    return _serialize_plan(plan)


def list_mrp_runs_for_plan(db: Session, plan_id: int, *, limit: int = 50) -> Dict[str, Any]:
    """Return only published snapshots from the exact current Ledger truth."""
    _get_plan(db, plan_id)  # validates existence
    truth = db.get(PlanningTruthState, 1)
    if truth is None or truth.current_generation_id is None:
        raise ValueError("Current accepted Ledger truth is unavailable")
    generation = db.get(LedgerGeneration, int(truth.current_generation_id))
    if generation is None or str(generation.status) != "accepted":
        raise ValueError("Current accepted Ledger truth is unavailable")
    runs = (
        db.query(PlanningRun)
        .filter(
            PlanningRun.source_plan_id == int(plan_id),
            PlanningRun.ledger_generation_id == int(truth.current_generation_id),
            PlanningRun.status == "FIXED_SNAPSHOT",
        )
        .order_by(PlanningRun.run_id.desc())
        .limit(max(1, min(int(limit or 50), 200)))
        .all()
    )
    rows = []
    for r in runs:
        rows.append({
            "run_id": int(r.run_id),
            "status": str(r.status or ""),
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "started_by": r.started_by,
            "horizon_days": int(r.horizon_days) if r.horizon_days is not None else None,
            "period_from": r.period_from.isoformat() if r.period_from else None,
            "period_to": r.period_to.isoformat() if r.period_to else None,
            "fixed_at": r.fixed_at.isoformat() if r.fixed_at else None,
        })
    return {"rows": rows, "total": len(rows)}


def delete_period_plan(db: Session, plan_id: int) -> Dict[str, Any]:
    """Delete a plan only when it has no snapshot in current published truth."""
    plan = _get_plan(db, plan_id)
    fixed_runs = (
        db.query(PlanningRun)
        .filter(
            PlanningRun.source_plan_id == plan_id,
            PlanningRun.status.in_(("FIXED_SNAPSHOT", "BUILDING_SNAPSHOT")),
        )
        .count()
    )
    if fixed_runs > 0:
        raise ValueError(
            f"Нельзя удалить план: по нему есть {fixed_runs} зафиксированных расчётов MRP"
        )
    name = plan.name
    db.delete(plan)
    db.commit()
    return {"status": "deleted", "id": plan_id, "name": name}


def material_availability_positions(
    db: Session,
    item_ids: Optional[Iterable[int]] = None,
) -> Dict[int, Dict[str, float]]:
    """Inc5 (design §2.5 / §11): expose the ledger pool projection
    (``on_hand`` / ``incoming`` / ``reserved_soft`` / ``available`` /
    ``projected`` / ``uncovered``) per item for the period-plan
    material-availability readers, behind the ``STOCK_SOURCE=bin`` flag.

    Additive and read-only: under the default legacy flag this returns ``{}`` and
    nothing in the planning pipeline consults it — the netting path
    (``_explode_bom_net_first``) is NOT rewired onto the reservation ledger here
    (that is Inc6). Only the stock ON-HAND source of the netting is flipped, via
    ``effective_stock_by_item_all``.
    """
    from .item_ledger.config import use_bin_stock

    if not use_bin_stock():
        return {}
    from .item_ledger import item_ledger_position

    ids = list(item_ids) if item_ids is not None else None
    return item_ledger_position(db, ids)


def _explode_bom_net_first(
    db: Session,
    plan_demands: Dict[int, Dict[date, float]],
    shared_pools: Optional["FreezeSharedPools"] = None,
    trace: Optional["FreezeTrace"] = None,
) -> Tuple[Dict[int, Dict[date, float]], Dict[int, Dict[date, float]], Dict[int, int]]:
    """Net-first multi-level BOM explosion with WIP netting and lead-time shifting.

    ``shared_pools`` — when provided (freeze v2), the effective-stock and WIP
    pools are NOT re-read here: they are *aliased* from the queue-wide, mutable
    ``FreezeSharedPools`` so a physical unit consumed by one run is invisible to
    the next. ``trace`` (freeze v2) records, per item, how much stock / which WIP
    lines / (later) which supplier lines covered its net, plus the BOM norms —
    written in place, only when both ``shared_pools`` and ``trace`` are set.

    ``shared_pools=None`` (every non-freeze caller and the byte-for-byte legacy
    path) re-reads both pools fresh and mutates nothing shared — behaviour is
    identical to before these params existed.

    For each plan item (level 0), explode the BOM tree level by level.
    At each level:
      1. Accumulate gross demand for each item.
      2. Net gross demand against available stock + WIP (chronologically per bucket).
      3. Explode the demand that on-hand STOCK does not cover (after-stock, not
         after-WIP) to the item's components, shifting each child's need-date
         back by its buffer_days. WIP nets the parent's own NEW orders but must
         not suppress this explosion: an open parent order still consumes its
         components, so their demand has to keep propagating down the tree.

    Returns:
        gross_map  — {item_id: {bucket_date: gross_qty}}
        net_map    — {item_id: {bucket_date: net_qty}}  (after stock + WIP)
        bom_level_map — {item_id: minimum_bom_level}  (0 = plan item, 1 = component, …)

    Cycle safety: each item is exploded as a parent at most once (``exploded_parents``
    set). Convergent BOMs (same sub-assembly under multiple parents) are handled
    correctly because all parents at the same depth contribute to ``next_demand``
    before the next iteration starts.
    """
    # --- Pre-load BOM data in bulk (avoid N+1 per item) ---
    # Effective stock with ignored warehouses (e.g., brak isolator) excluded;
    # using Item.stock_qty directly would let MRP "see" stock that production
    # control then refuses as a material-issue source.
    #
    # Freeze v2: alias the queue-wide consume-once stock ledger instead of
    # re-reading, so an earlier run's consumption persists to this one.
    if shared_pools is not None:
        stock_by_item: Dict[int, float] = shared_pools.stock
    else:
        stock_by_item = _effective_stock_by_item_all(db)

    # A top-level production item represents the approved release programme
    # (finished goods), unlike a top-level purchased item which is still a
    # replenishment request and may be netted against stock.
    root_production_item_ids = {
        int(item_id)
        for item_id, replenishment_method in (
            db.query(Item.item_id, Item.replenishment_method)
            .filter(Item.item_id.in_(list(plan_demands)))
            .all()
        )
        if classify_replenishment_flow(replenishment_method) == REPLENISHMENT_FLOW_PRODUCTION
    }

    default_spec_map: Dict[int, int] = {
        int(ds.item_id): int(ds.spec_id)
        for ds in db.query(DefaultSpecification).all()
    }

    components_by_spec: Dict[int, List[SpecComponent]] = {}
    for comp in db.query(SpecComponent).all():
        components_by_spec.setdefault(int(comp.spec_id), []).append(comp)

    # --- WIP: remaining qty from active (non-done, non-deleted) production
    # orders, keyed by planned_finish_date so the netting respects when the
    # WIP is actually expected to be physically available. A WIP order that
    # finishes in September does NOT cover a July demand bucket; previously
    # we collapsed all WIP into one timeless pool, which over-credited early
    # buckets and under-planned production.
    # Freeze v2: alias the queue-wide WIP pool (identity-carrying, self-excluded)
    # so its greedy consumption persists across runs; else read fresh per call.
    if shared_pools is not None:
        wip_eta_by_item: Dict[int, list] = shared_pools.wip
    else:
        try:
            wip_eta_by_item = _active_wip_eta_by_item(db)
        except Exception:
            wip_eta_by_item = {}

    # --- Buffer-days lookup: item → default spec → production_kind → resource.buffer_days ---
    all_spec_ids: set = set(default_spec_map.values())
    if all_spec_ids:
        specs = db.query(Specification).filter(Specification.spec_id.in_(all_spec_ids)).all()
    else:
        specs = []
    spec_by_id: Dict[int, Any] = {int(s.spec_id): s for s in specs}

    kind_ids: set = {int(s.production_kind_id) for s in specs if getattr(s, "production_kind_id", None)}
    resource_kind_by_kind: Dict[int, list] = {}
    if kind_ids:
        for rk in (
            db.query(ResourceProductionKind)
            .filter(ResourceProductionKind.production_kind_id.in_(kind_ids))
            .all()
        ):
            resource_kind_by_kind.setdefault(int(rk.production_kind_id), []).append(rk)

    resource_ids: set = {int(rk.resource_id) for lst in resource_kind_by_kind.values() for rk in lst}
    res_by_id: Dict[int, Any] = {}
    if resource_ids:
        resources = db.query(ProductionResource).filter(ProductionResource.resource_id.in_(resource_ids)).all()
        res_by_id = {int(r.resource_id): r for r in resources}

    buffer_days_cache: Dict[int, int] = {}
    today = date.today()

    def clamp_to_today(value: date) -> date:
        return today if value < today else value

    def resolve_buffer_days(item_id: int) -> int:
        if item_id in buffer_days_cache:
            return buffer_days_cache[item_id]
        spec_id = default_spec_map.get(item_id)
        buffer_val = 0
        if spec_id:
            spec = spec_by_id.get(int(spec_id))
            if spec and getattr(spec, "production_kind_id", None):
                for rk in resource_kind_by_kind.get(int(spec.production_kind_id), []):
                    res = res_by_id.get(int(rk.resource_id))
                    if res and getattr(res, "buffer_days", None):
                        try:
                            buffer_raw = float(res.buffer_days or 0.0)
                        except Exception:
                            buffer_raw = 0.0
                        if buffer_raw > 0:
                            buffer_val = int(buffer_raw)
                            break
        buffer_days_cache[item_id] = max(0, buffer_val)
        return buffer_days_cache[item_id]

    # --- BFS state ---
    gross_map: Dict[int, Dict[date, float]] = {}
    net_map: Dict[int, Dict[date, float]] = {}
    bom_level_map: Dict[int, int] = {}
    # Stock pool (immediate, no ETA) — consumed before WIP for each bucket.
    # Freeze v2 aliases the shared ledgers (NOT copies): run N's consumption
    # must be visible to run N+1. The None path keeps private copies.
    if shared_pools is not None:
        avail_stock: Dict[int, float] = shared_pools.stock
        avail_wip: Dict[int, list] = shared_pools.wip
    else:
        avail_stock = dict(stock_by_item)
        # WIP pool with per-item ETA list; mutated as buckets are netted so the
        # same WIP line can't cover two different demand buckets.
        avail_wip = {
            int(iid): list(entries) for iid, entries in wip_eta_by_item.items()
        }
    # Prevent cycles: track items already exploded as parents
    exploded_parents: set = set()

    # Level 0: demand from plan lines
    demand_map: Dict[int, Dict[date, float]] = {
        int(iid): dict(buckets) for iid, buckets in plan_demands.items()
    }

    MAX_BOM_DEPTH = 20
    for depth in range(MAX_BOM_DEPTH):
        if not demand_map:
            break

        next_demand: Dict[int, Dict[date, float]] = {}

        for iid, buckets in sorted(demand_map.items()):
            if not buckets:
                continue

            iid = int(iid)

            # Track minimum BOM level at which this item appears
            if iid not in bom_level_map:
                bom_level_map[iid] = depth
            else:
                bom_level_map[iid] = min(bom_level_map[iid], depth)

            # Accumulate gross demand
            if iid not in gross_map:
                gross_map[iid] = {}
            for bucket_date, qty in buckets.items():
                gross_map[iid][bucket_date] = gross_map[iid].get(bucket_date, 0.0) + float(qty)

            # Level-0 production entries are the approved release programme.
            # They must always become production assignments in full: existing
            # finished-goods stock and earlier/open production orders are not
            # a substitute for the quantity explicitly scheduled in this
            # period plan. Purchased root entries still net normally.
            #
            # Components, in contrast, are netted chronologically: first
            # against the immediate stock pool, then against WIP whose ETA is
            # at or before the bucket.
            # WIP entries with eta > bucket_date can't cover that bucket but
            # may cover a later one, so the per-item WIP list is mutated in
            # place across the bucket loop.
            #
            # Two residuals are tracked, because on-hand stock and open WIP are
            # NOT interchangeable for the purpose of component demand:
            #   * after-stock (`explode_buckets`) — gross minus on-hand stock.
            #     This is everything that still has to be MADE, so it drives the
            #     dependent demand exploded to children. An open parent order
            #     (WIP) does not put the parent's components on hand — producing
            #     that order still consumes them, so they must still be planned.
            #     Only physical stock of the parent legitimately stops the
            #     explosion (its components were consumed historically).
            #   * after-stock-and-WIP (`net_buckets`) — used to size NEW orders
            #     for THIS item (net_map / net_required_qty / reconciliation
            #     top-ups). An open order already covers this, so no duplicate
            #     parent order is created.
            stock_left = avail_stock.get(iid, 0.0)
            stock_before = stock_left  # freeze v2: how much stock this item ate
            wip_list = avail_wip.setdefault(iid, [])
            net_buckets: List[Tuple[date, float]] = []      # after stock + WIP → orders
            explode_buckets: List[Tuple[date, float]] = []  # after stock only → children

            for bucket_date, bucket_qty in sorted(buckets.items()):
                q = float(bucket_qty or 0.0)
                if q <= 1e-9:
                    continue
                if depth == 0 and iid in root_production_item_ids:
                    # The plan is a release obligation, not a sales-demand
                    # forecast.  Do not let stock/WIP from a previous period
                    # shrink the top-level production task.
                    net_buckets.append((bucket_date, q))
                    explode_buckets.append((bucket_date, q))
                    continue
                # 1) Consume free stock first (always available).
                if stock_left >= q:
                    stock_left -= q
                    continue
                after_stock = q - stock_left
                stock_left = 0.0
                # Whatever on-hand stock can't cover still has to be produced,
                # so it propagates to components regardless of existing WIP.
                explode_buckets.append((bucket_date, after_stock))
                # 2) Consume WIP whose ETA <= bucket_date to size NEW orders only.
                if shared_pools is not None:
                    after_wip, wip_used = _consume_wip_detailed(wip_list, bucket_date, after_stock)
                    if trace is not None and wip_used:
                        trace.by_item[iid].wip_allocs.extend(wip_used)
                else:
                    after_wip = _consume_wip_at_or_before(wip_list, bucket_date, after_stock)
                if after_wip <= 1e-9:
                    continue
                net_buckets.append((bucket_date, after_wip))

            avail_stock[iid] = stock_left
            if shared_pools is not None and trace is not None:
                consumed_stock = stock_before - stock_left
                if consumed_stock > 1e-12:
                    trace.by_item[iid].stock_alloc += consumed_stock

            # Accumulate net demand (after stock + WIP) — sizes orders for THIS item.
            if net_buckets:
                if iid not in net_map:
                    net_map[iid] = {}
                for bucket_date, net_q in net_buckets:
                    net_map[iid][bucket_date] = net_map[iid].get(bucket_date, 0.0) + float(net_q)

            # Explode demand that on-hand stock does NOT cover (after-stock,
            # NOT after-stock-and-WIP): an open parent order still needs its
            # components produced, so WIP must not suppress the explosion or
            # lower BOM levels silently stay in deficit with no orders.
            if not explode_buckets or iid in exploded_parents:
                continue  # Nothing to propagate, or cycle guard
            exploded_parents.add(iid)

            spec_id = default_spec_map.get(iid)
            if not spec_id:
                continue  # Leaf item (purchased material or item without BOM)

            comps = components_by_spec.get(int(spec_id), [])
            if not comps:
                continue

            for bucket_date, exp_q in explode_buckets:
                for comp in comps:
                    try:
                        child_id = int(comp.item_id)
                        per_unit = float(comp.quantity or 0.0)
                    except Exception:
                        continue
                    if per_unit <= 1e-12 or exp_q <= 1e-9:
                        continue
                    child_qty = exp_q * per_unit
                    # Classical MRP lead-time offset: shift the child's
                    # need_date back by the PARENT's production time
                    # (`resolve_buffer_days(iid)`), so the components are
                    # required by the moment the parent's production starts.
                    # The child's OWN buffer applies one level deeper, when
                    # the child is itself exploded into its components —
                    # the BFS accumulates the buffer chain correctly.
                    # Earlier this used `resolve_buffer_days(child_id)`,
                    # which shifted by the wrong link and effectively lost
                    # the parent's lead time at every level (over 3 levels
                    # with buffers 7/5/3 it produced a 12-day error).
                    buf = resolve_buffer_days(iid)
                    child_date = (bucket_date - timedelta(days=buf)) if buf > 0 else bucket_date
                    child_date = clamp_to_today(child_date)
                    if child_id not in next_demand:
                        next_demand[child_id] = {}
                    next_demand[child_id][child_date] = (
                        next_demand[child_id].get(child_date, 0.0) + child_qty
                    )

        demand_map = next_demand

    # Freeze v2: record the frozen BOM norms for EVERY parent that carries gross
    # demand and has a default spec — including stock-covered parents whose
    # explosion was skipped (empty explode_buckets). The writer aggregates dups.
    if shared_pools is not None and trace is not None:
        for iid, gross_buckets in gross_map.items():
            if sum(float(q) for q in gross_buckets.values()) <= 1e-9:
                continue
            spec_id = default_spec_map.get(int(iid))
            if not spec_id:
                continue
            for comp in components_by_spec.get(int(spec_id), []):
                try:
                    child_id = int(comp.item_id)
                    per_unit = float(comp.quantity or 0.0)
                except Exception:
                    continue
                trace.component_norms.append((int(iid), child_id, int(spec_id), per_unit))

    return gross_map, net_map, bom_level_map


def _load_purchase_supplier_remaining(
    db: Session,
    item_ids: List[int],
    period_to: date,
    *,
    exclude_order_ids: Optional[Iterable[int]] = None,
) -> Dict[int, List[Dict[str, Any]]]:
    """
    Batch-load open supplier-order lines for the given purchased item IDs where
    delivery_date <= period_to.  Results are sorted by delivery_date ascending so
    they can be consumed greedily (earliest supply covers earliest demand).

    Filtering rules mirror planning_service._get_active_supplier_remaining_by_item_date:
    - Deleted supplier orders are skipped (deletion_mark=True).
    - Учитываются только фазы «в пути» / «на складе» (state_counts_in_mrp);
      «Нет товара» (Новый заказ / В закупку / Бухгалтерия) и терминальные — пропускаются.
    - Lines without a delivery_date are skipped.
    - Lines with remaining_qty <= 0 are skipped.

    Each row additionally carries identity — ``order_id`` / ``order_ref1c`` /
    ``line_id`` (SupplierOrderItem PK) / ``line_number`` / ``fact_at_freeze`` —
    for the freeze allocation writer. Existing callers read only ``delivery_date``
    / ``remaining_qty``; the extra keys are inert. ``exclude_order_ids`` drops a
    run's OWN already-exported supplier orders (self-exclusion). Both the extra
    keys and the deterministic ``(delivery_date, order_id, line_id)`` tie-break
    are additive — with ``exclude_order_ids=None`` and no same-date ties the
    result is the prior behaviour.
    """
    if not item_ids:
        return {}
    exclude = {int(o) for o in (exclude_order_ids or [])}

    try:
        rows = (
            db.query(
                SupplierOrderItem.item_id_ref,
                SupplierOrderItem.delivery_date,
                SupplierOrder.order_state_key,
                SupplierOrder.order_state_name,
                SupplierOrderItem.remaining_qty,
                SupplierOrder.order_id,
                SupplierOrder.order_ref1c,
                SupplierOrderItem.item_id,
                SupplierOrderItem.line_number,
            )
            .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
            .filter(SupplierOrderItem.item_id_ref.in_(item_ids))
            .filter(SupplierOrder.deletion_mark.is_(False))
            .filter(SupplierOrderItem.delivery_date.isnot(None))
            .filter(SupplierOrderItem.delivery_date < period_to + timedelta(days=1))
            .filter(func.coalesce(SupplierOrderItem.remaining_qty, 0.0) > 0)
            .order_by(
                SupplierOrderItem.delivery_date.asc(),
                SupplierOrder.order_id.asc(),
                SupplierOrderItem.item_id.asc(),
            )
            .all()
        )
    except Exception:
        rows = []

    result: Dict[int, List[Dict[str, Any]]] = {}
    for (
        iid,
        delivery_dt,
        state_key,
        state_name,
        qty,
        order_id,
        order_ref1c,
        line_id,
        line_number,
    ) in rows:
        try:
            if not _supplier_order_counts_in_mrp(state_name):
                continue
            if order_id is not None and int(order_id) in exclude:
                continue
            item_id = int(iid)
            delivery_date = (
                delivery_dt.date() if isinstance(delivery_dt, datetime) else delivery_dt
            )
            remaining = float(qty or 0.0)
        except Exception:
            continue
        if remaining <= 1e-12:
            continue
        result.setdefault(item_id, []).append(
            {
                "delivery_date": delivery_date,
                "remaining_qty": remaining,
                "order_id": int(order_id) if order_id is not None else None,
                "order_ref1c": (str(order_ref1c) if order_ref1c else None),
                "line_id": int(line_id) if line_id is not None else None,
                "line_number": int(line_number) if line_number is not None else None,
                "fact_at_freeze": remaining,
            }
        )
    return result


def create_mrp_snapshot_from_period_plan(
    db: Session,
    plan_id: int,
    *,
    generation_key: str,
    started_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish this fixed plan through the atomic Ledger obligation refresh.

    Transaction ownership deliberately remains with the caller.  This service
    neither commits nor rolls back, so a failed refresh cannot expose a partial
    candidate generation.
    """
    key = str(generation_key or "").strip()
    if not key:
        raise ValueError("generation_key is required")
    plan = _get_plan(db, int(plan_id))
    if plan.status != "fixed":
        raise ValueError("MRP-снимок можно создать только из зафиксированного плана")
    if not db.query(ProductionPlanLine.id).filter(
        ProductionPlanLine.plan_id == int(plan.id),
        ProductionPlanLine.qty > 0,
    ).first():
        raise ValueError("В плане нет положительной потребности для MRP")
    truth = db.get(PlanningTruthState, 1)
    if truth is None or truth.current_generation_id is None:
        raise ValueError("Current accepted Ledger truth is unavailable")
    parent = db.get(LedgerGeneration, int(truth.current_generation_id))
    if parent is None or str(parent.status) != "accepted":
        raise ValueError("Current accepted Ledger truth is unavailable")
    try:
        cfg_id, cfg = get_active_planning_config(db)
    except Exception:
        cfg_id, cfg = None, dict(DEFAULT_PLANNING_CONFIG)

    from .obligation_refresh_orchestrator import run_obligation_refresh
    report = run_obligation_refresh(
        db,
        parent_generation_id=int(parent.id),
        generation_key=key,
        add_plan_ids=(int(plan.id),),
        started_by=started_by or "api",
        horizon_days=max(1, (plan.period_to - plan.period_from).days + 1),
        config_version_id=cfg_id,
        config_snapshot=dict(cfg),
    )
    run = db.query(PlanningRun).filter(
        PlanningRun.run_id.in_(report.candidate_run_ids),
        PlanningRun.source_plan_id == int(plan.id),
        PlanningRun.ledger_generation_id == int(report.target_generation_id),
        PlanningRun.status == "FIXED_SNAPSHOT",
    ).one_or_none()
    if run is None:
        raise ValueError("MRP-снимок не создан для плана")
    run_id = int(run.run_id)
    return {
        "status": "ok",
        "generation_key": key,
        "ledger_generation_id": int(report.target_generation_id),
        "run_id": run_id,
        "plan_id": int(plan.id),
        "published": bool(report.published),
        # Compatibility counters are reads of the just-published immutable
        # rows.  They do not trigger another planning calculation.
        "requirement_count": db.query(MrpRequirement).filter_by(run_id=run_id).count(),
        "bucket_count": db.query(MrpRequirementBucket).filter_by(run_id=run_id).count(),
        "production_count": db.query(PlannedOrder).filter_by(run_id=run_id).count(),
        "stage_count": db.query(PlannedOrderStage).filter_by(run_id=run_id).count(),
        "purchase_count": db.query(PlannedPurchase).filter_by(run_id=run_id).count(),
        "rework_count": db.query(PlannedRework).filter_by(run_id=run_id).count(),
        "freeze_version": int(run.active_freeze_version or 0),
    }


def _prepare_include_run(
    db: Session,
    plan_id: int,
    started_by: Optional[str],
    now: datetime,
) -> PlanningRun:
    """Validate the include plan and get-or-create/refresh ONLY its run header
    (v2 §6.2). Every other run's header is left untouched by a refreeze.
    Validation errors carry the same texts as the legacy snapshot entry point
    and fire before any pool is built or row written.
    """
    plan = _get_plan(db, plan_id)
    if plan.status != "fixed":
        raise ValueError("MRP-снимок можно создать только из зафиксированного плана")
    has_line = (
        db.query(ProductionPlanLine.id)
        .filter(ProductionPlanLine.plan_id == int(plan.id))
        .filter(ProductionPlanLine.qty > 0)
        .first()
    )
    if not has_line:
        raise ValueError("В плане нет положительной потребности для MRP")

    try:
        cfg_id, cfg = get_active_planning_config(db)
    except Exception:
        cfg_id, cfg = 0, dict(DEFAULT_PLANNING_CONFIG)

    snapshot = dict(cfg or {})
    snapshot["planning_horizon_days"] = max(1, (plan.period_to - plan.period_from).days + 1)
    snapshot["source_plan"] = {
        "id": int(plan.id),
        "name": str(plan.name or ""),
        "period_from": plan.period_from.isoformat(),
        "period_to": plan.period_to.isoformat(),
    }

    run = _latest_fixed_run_for_plan(db, int(plan.id))
    if run is None:
        run = PlanningRun(
            status="FIXED_SNAPSHOT",
            started_by=started_by or "api",
            horizon_days=int(snapshot["planning_horizon_days"]),
            pinned=True,
            source_plan_id=int(plan.id),
            period_from=plan.period_from,
            period_to=plan.period_to,
            fixed_at=now,
            config_version_id=cfg_id,
            config_snapshot=snapshot,
            warnings=[],
            kpi={},
            started_at=now,
            finished_at=now,
        )
        db.add(run)
    else:
        run.started_by = started_by or run.started_by or "api"
        run.horizon_days = int(snapshot["planning_horizon_days"])
        run.pinned = True
        run.period_from = plan.period_from
        run.period_to = plan.period_to
        run.fixed_at = now
        run.config_version_id = cfg_id
        run.config_snapshot = snapshot
        run.warnings = []
        run.kpi = {}
        run.started_at = now
        run.finished_at = now
    db.flush()
    return run


def _freeze_one_run(
    db: Session,
    run: PlanningRun,
    plan: ProductionPlanHeader,
    *,
    shared_pools: "FreezeSharedPools",
    trace: "FreezeTrace",
    now: datetime,
    new_version: int,
    is_include: bool = True,
    manage_plan_locks: bool = True,
) -> Dict[str, Any]:
    """Freeze ONE active run against the shared queue-wide pool (v2 §5/§6.5).

    The legacy single-snapshot body, extended so the BOM explosion consumes the
    shared pools once (``shared_pools``/``trace``); requirements are stamped with
    the new freeze version, pool key, zeroed drift and ``initial_snapshot_stock``;
    own already-exported PlannedPurchase survive the rebuild as self-coverage;
    and the freeze baseline/allocation/component tables are written. Requirement
    ids are preserved through the ``(run_id,item_id)`` upsert. No commit here —
    the orchestrator owns the transaction.
    """
    from .mrp_freeze import (
        pool_key_for,
        _write_freeze_baseline,
        _write_freeze_allocation,
        _write_freeze_component,
    )

    lines = (
        db.query(ProductionPlanLine)
        .filter(ProductionPlanLine.plan_id == int(plan.id))
        .filter(ProductionPlanLine.qty > 0)
        .order_by(ProductionPlanLine.item_id.asc(), ProductionPlanLine.bucket_date.asc())
        .all()
    )

    # A plan keeps one fixed run. Rebuilding it retains MrpRequirement ids so
    # already created production orders stay linked to this plan and visible
    # in its execution journal; only derived bucket/proposal rows are rebuilt.
    existing_req_by_item: Dict[int, MrpRequirement] = {
        int(req.item_id): req
        for req in db.query(MrpRequirement).filter(MrpRequirement.run_id == int(run.run_id)).all()
    }
    # Own already-exported PlannedPurchase survive the rebuild (v2 §5): their 1C
    # supplier order is self-excluded from the pool, so the exported qty is this
    # run's own coverage — consume it before booking any fresh purchase, and do
    # not delete it. Unexported local recommendations are rebuilt as before.
    if existing_req_by_item:
        exported_purchase_ids = {
            int(source_id)
            for (source_id,) in (
                db.query(SyncLink.source_id)
                .join(PlannedPurchase, PlannedPurchase.purchase_id == SyncLink.source_id)
                .filter(PlannedPurchase.run_id == int(run.run_id))
                .filter(SyncLink.source_system == "PRODPLAN")
                .filter(SyncLink.source_doctype == "planned_purchase")
                .filter(SyncLink.target_entity == "Document_ЗаказПоставщику")
                .filter(SyncLink.status == "success")
                .filter(SyncLink.target_ref_key.isnot(None))
                .all()
            )
        }
        db.query(PlannedOrderStage).filter(PlannedOrderStage.run_id == int(run.run_id)).delete(synchronize_session=False)
        db.query(PlannedOrder).filter(PlannedOrder.run_id == int(run.run_id)).delete(synchronize_session=False)
        purchase_delete = db.query(PlannedPurchase).filter(PlannedPurchase.run_id == int(run.run_id))
        if exported_purchase_ids:
            purchase_delete = purchase_delete.filter(PlannedPurchase.purchase_id.notin_(exported_purchase_ids))
        purchase_delete.delete(synchronize_session=False)
        db.query(PlannedRework).filter(PlannedRework.run_id == int(run.run_id)).delete(synchronize_session=False)
        db.query(MrpRequirementBucket).filter(MrpRequirementBucket.run_id == int(run.run_id)).delete(synchronize_session=False)

    # --- Collect plan-level (level 0) demand and lock plan lines ---
    buckets_by_item: Dict[int, Dict[date, float]] = {}
    for line in lines:
        item_id = int(line.item_id)
        line_qty = _to_float(line.qty)
        if line_qty <= 0:
            continue
        buckets_by_item.setdefault(item_id, {})
        buckets_by_item[item_id][line.bucket_date] = (
            buckets_by_item[item_id].get(line.bucket_date, 0.0) + line_qty
        )
        # Candidate snapshots are not published state.  Their source-plan
        # locks are transferred only by the atomic publisher, never while a
        # BUILDING_SNAPSHOT is being calculated.
        if manage_plan_locks:
            line.locked_by_run_id = int(run.run_id)

    # --- Multi-level net-first BOM explosion + stock netting ---
    # gross_map[item_id][bucket_date] = gross qty (before stock)
    # net_map[item_id][bucket_date]   = net qty  (after stock)
    # bom_level_map[item_id]          = 0 for plan items, 1+ for components
    gross_map, net_map, bom_level_map = _explode_bom_net_first(
        db, buckets_by_item, shared_pools, trace
    )

    # --- Persist MrpRequirement + MrpRequirementBucket for every item with demand ---
    req_count = 0
    bucket_count = 0
    req_by_item: Dict[int, MrpRequirement] = {}
    seen_requirement_item_ids: Set[int] = set()
    for item_id, gross_buckets in sorted(gross_map.items()):
        total_gross = sum(float(q) for q in gross_buckets.values())
        if total_gross <= 1e-9:
            continue

        net_buckets = net_map.get(item_id, {})
        total_net = sum(float(q) for q in net_buckets.values()) if net_buckets else 0.0
        bom_lvl = bom_level_map.get(item_id, 0)

        pk = pool_key_for(int(item_id))
        item_trace = trace.by_item.get(int(item_id))
        initial_stock = float(item_trace.stock_alloc) if item_trace else 0.0
        req = existing_req_by_item.get(int(item_id))
        if req is None:
            req = MrpRequirement(
                run_id=int(run.run_id),
                item_id=int(item_id),
                total_required_qty=total_gross,
                net_required_qty=total_net,
                covered_qty=0.0,
                remaining_qty=total_net,
                period_from=plan.period_from,
                period_to=plan.period_to,
                bom_level=bom_lvl,
            )
            db.add(req)
        else:
            req.total_required_qty = total_gross
            req.net_required_qty = total_net
            req.covered_qty = 0.0
            req.remaining_qty = total_net
            req.period_from = plan.period_from
            req.period_to = plan.period_to
            req.bom_level = bom_lvl
        # Freeze v2 stamps: version, zeroed drift, pool key, frozen stock alloc.
        req.freeze_version = int(new_version)
        req.drift_adjustment_qty = 0.0
        req.characteristic_ref = pk.characteristic_ref
        req.organization_ref = pk.organization_ref
        req.planning_stock_pool = pk.planning_stock_pool
        req.initial_snapshot_stock = initial_stock
        db.flush()
        req_by_item[item_id] = req
        seen_requirement_item_ids.add(int(item_id))
        req_count += 1

        # Store per-bucket gross and net quantities for traceability
        all_bucket_dates = sorted(set(gross_buckets) | set(net_buckets))
        for bucket_date in all_bucket_dates:
            gross_qty = float(gross_buckets.get(bucket_date, 0.0))
            net_qty = float(net_buckets.get(bucket_date, 0.0)) if net_buckets else 0.0
            if gross_qty <= 1e-9 and net_qty <= 1e-9:
                continue
            db.add(
                MrpRequirementBucket(
                    requirement_id=int(req.id),
                    run_id=int(run.run_id),
                    item_id=int(item_id),
                    bucket_date=bucket_date,
                    gross_qty=gross_qty,
                    net_qty=net_qty,
                )
            )
            bucket_count += 1

    for item_id, req in existing_req_by_item.items():
        if item_id in seen_requirement_item_ids:
            continue
        pk = pool_key_for(int(item_id))
        req.total_required_qty = 0.0
        req.net_required_qty = 0.0
        req.covered_qty = 0.0
        req.remaining_qty = 0.0
        req.period_from = plan.period_from
        req.period_to = plan.period_to
        # Dropped item: still re-stamp the freeze version (initial stock = 0).
        req.freeze_version = int(new_version)
        req.drift_adjustment_qty = 0.0
        req.characteristic_ref = pk.characteristic_ref
        req.organization_ref = pk.organization_ref
        req.planning_stock_pool = pk.planning_stock_pool
        req.initial_snapshot_stock = 0.0

    # --- Allocate PlannedOrder / PlannedPurchase / PlannedRework by replenishment flow ---
    allocatable_item_ids = [
        iid for iid, buckets in net_map.items()
        if any(float(q) > 1e-9 for q in buckets.values())
    ]
    purchase_count = 0
    rework_count = 0
    production_count = 0
    stage_count = 0
    frozen_schedule_warnings: List[Dict[str, Any]] = []
    created_production_orders: List[PlannedOrder] = []
    if allocatable_item_ids:
        items_by_id: Dict[int, Item] = {
            r.item_id: r
            for r in db.query(Item).filter(Item.item_id.in_(allocatable_item_ids)).all()
        }
        # Batch-load default spec_id per item (needed for PlannedRework.spec_id)
        spec_id_by_item: Dict[int, int] = {
            int(ds.item_id): int(ds.spec_id)
            for ds in db.query(DefaultSpecification.item_id, DefaultSpecification.spec_id)
            .filter(DefaultSpecification.item_id.in_(allocatable_item_ids))
            .all()
        }

        # Freeze v2: the supplier pool is the queue-wide, consume-once ledger
        # (built once with max-cutoff and own orders self-excluded). Alias it —
        # NOT a copy — so this run's greedy consumption persists to the next run.
        # Per-bucket phasing (delivery_date <= bucket_date) is preserved below.
        supplier_work: Dict[int, List[Dict[str, Any]]] = shared_pools.supplier

        for iid in allocatable_item_ids:
            item = items_by_id.get(iid)
            if not item:
                continue
            flow = classify_replenishment_flow(item.replenishment_method)
            lead_time = int(item.replenishment_time or 0)
            alloc_total_qty = 0.0

            if flow == REPLENISHMENT_FLOW_PURCHASE:
                req_id = int(req_by_item[iid].id) if iid in req_by_item else None
                sup_rows = supplier_work.get(iid, [])  # sorted by delivery_date asc

                for bucket_date, net_qty in sorted(net_map[iid].items()):
                    net_qty = float(net_qty)
                    if net_qty <= 1e-9:
                        continue

                    # Consume supplier orders arriving no later than this bucket's need_date.
                    # Earlier supply covers earlier demand (chronological greedy match).
                    remaining_need = net_qty
                    for sup_row in sup_rows:
                        if remaining_need <= 1e-12:
                            break
                        if sup_row["delivery_date"] > bucket_date:
                            break  # list is sorted; no earlier-arriving supply after this
                        avail = float(sup_row.get("remaining_qty", 0.0) or 0.0)
                        if avail <= 1e-12:
                            continue
                        used = min(avail, remaining_need)
                        sup_row["remaining_qty"] = max(avail - used, 0.0)
                        remaining_need = max(remaining_need - used, 0.0)
                        trace.by_item[int(iid)].supplier_allocs.append((sup_row, used))

                    # Full bucket demand is considered covered (by supplier or planned purchase).
                    alloc_total_qty += net_qty

                    if remaining_need > 1e-9:
                        # Only create a PlannedPurchase for the portion not covered by supplier.
                        need_date = bucket_date
                        order_date = max(date.today(), need_date - timedelta(days=lead_time))
                        db.add(PlannedPurchase(
                            run_id=int(run.run_id),
                            item_id=int(iid),
                            requested_qty=net_qty,      # original bucket net demand
                            planned_qty=remaining_need,  # after supplier netting
                            qty=remaining_need,
                            need_date=need_date,
                            order_date=order_date,
                            lead_time_days=lead_time,
                            bucket_date=need_date,
                            supplier_ref1c=getattr(item, "supplier_ref1c", None),
                            source_mrp_requirement_id=req_id,
                            ledger_generation_id=int(run.ledger_generation_id),
                        ))
                        purchase_count += 1

            elif flow == REPLENISHMENT_FLOW_REWORK:
                spec_id = spec_id_by_item.get(iid)
                for bucket_date, net_qty in sorted(net_map[iid].items()):
                    net_qty = float(net_qty)
                    if net_qty <= 1e-9:
                        continue
                    need_date = bucket_date
                    order_date = max(date.today(), need_date - timedelta(days=lead_time))
                    db.add(PlannedRework(
                        run_id=int(run.run_id),
                        item_id=int(iid),
                        spec_id=spec_id,
                        requested_qty=net_qty,
                        planned_qty=net_qty,
                        qty=net_qty,
                        need_date=need_date,
                        order_date=order_date,
                        lead_time_days=lead_time,
                        bucket_date=need_date,
                        component_blocked=False,
                        component_partial=False,
                        ledger_generation_id=int(run.ledger_generation_id),
                    ))
                    rework_count += 1
                    alloc_total_qty += net_qty

            else:
                req_id = int(req_by_item[iid].id) if iid in req_by_item else None
                for bucket_date, net_qty in sorted(net_map[iid].items()):
                    net_qty = float(net_qty)
                    if net_qty <= 1e-9:
                        continue
                    order = PlannedOrder(
                        run_id=int(run.run_id),
                        item_id=int(iid),
                        requested_qty=net_qty,
                        planned_qty=net_qty,
                        qty=net_qty,
                        need_date=bucket_date,
                        start_date=bucket_date,
                        finish_date=bucket_date,
                        bucket_date=bucket_date,
                        demand_ref=f"mrp_requirement:{req_id}" if req_id else None,
                        demand_date=bucket_date,
                        ledger_generation_id=int(run.ledger_generation_id),
                    )
                    db.add(order)
                    created_production_orders.append(order)
                    production_count += 1
                    alloc_total_qty += net_qty

            # Mark the requirement covered by the allocation created above.
            #
            # Only purchase and rework flows close the requirement at snapshot
            # time, because their PlannedPurchase / PlannedRework rows ARE the
            # downstream orders that will be issued to 1C. PlannedOrder for a
            # production-flow item is just an MRP proposal — covered_qty is
            # incremented when a ProductionProduct is materialized via
            # create_production_orders_from_mrp_requirements. Otherwise the
            # very first materialization always skips with
            # "remaining_qty=0 (уже покрыто)".
            if flow in (REPLENISHMENT_FLOW_PURCHASE, REPLENISHMENT_FLOW_REWORK):
                req = req_by_item.get(iid)
                if req and alloc_total_qty > 0:
                    total_net = _to_float(req.net_required_qty)
                    req.covered_qty = min(alloc_total_qty, total_net)
                    req.remaining_qty = max(0.0, total_net - alloc_total_qty)

        if created_production_orders:
            db.flush()
            produced_item_ids = sorted({int(order.item_id) for order in created_production_orders})
            produced_spec_id_by_item: Dict[int, int] = {
                int(ds.item_id): int(ds.spec_id)
                for ds in db.query(DefaultSpecification.item_id, DefaultSpecification.spec_id)
                .filter(DefaultSpecification.item_id.in_(produced_item_ids))
                .all()
            }
            spec_ids = sorted(set(produced_spec_id_by_item.values()))
            stage_norms_by_spec: Dict[int, List[Tuple[int, float]]] = {}
            resource_id_by_spec: Dict[int, int] = {}
            if spec_ids:
                for row in (
                    db.query(Specification.spec_id, ResourceProductionKind.resource_id)
                    .join(
                        ResourceProductionKind,
                        ResourceProductionKind.production_kind_id == Specification.production_kind_id,
                    )
                    .filter(Specification.spec_id.in_(spec_ids))
                    .order_by(ResourceProductionKind.id.asc())
                    .all()
                ):
                    try:
                        resource_id_by_spec.setdefault(int(row.spec_id), int(row.resource_id))
                    except Exception:
                        continue
                stage_rows = (
                    db.query(
                        SpecOperation.spec_id.label("spec_id"),
                        SpecOperation.stage_id.label("stage_id"),
                        func.sum(func.coalesce(SpecOperation.time_norm, Operation.time_norm, 0)).label("hours"),
                    )
                    .join(Operation, SpecOperation.operation_id == Operation.operation_id)
                    .filter(SpecOperation.spec_id.in_(spec_ids))
                    .filter(SpecOperation.stage_id.isnot(None))
                    .group_by(SpecOperation.spec_id, SpecOperation.stage_id)
                    .all()
                )
                for row in stage_rows:
                    try:
                        sid = int(row.stage_id)
                        hours = float(row.hours or 0.0)
                        if sid > 0 and hours > 1e-12:
                            stage_norms_by_spec.setdefault(int(row.spec_id), []).append((sid, hours))
                    except Exception:
                        continue
                missing_stage_spec_ids = [
                    int(spec_id)
                    for spec_id in spec_ids
                    if int(spec_id) not in stage_norms_by_spec
                ]
                if missing_stage_spec_ids:
                    component_stage_rows = (
                        db.query(
                            SpecComponent.spec_id.label("spec_id"),
                            SpecComponent.stage_id.label("stage_id"),
                        )
                        .filter(SpecComponent.spec_id.in_(missing_stage_spec_ids))
                        .filter(SpecComponent.stage_id.isnot(None))
                        .group_by(SpecComponent.spec_id, SpecComponent.stage_id)
                        .all()
                    )
                    for row in component_stage_rows:
                        try:
                            sid = int(row.stage_id)
                            if sid > 0:
                                stage_norms_by_spec.setdefault(int(row.spec_id), []).append((sid, 0.0))
                        except Exception:
                            continue

            for order in created_production_orders:
                spec_id = produced_spec_id_by_item.get(int(order.item_id))
                if not spec_id:
                    continue
                qty = _to_float(order.qty)
                if qty <= 1e-12:
                    continue
                for stage_id, hours_per_unit in stage_norms_by_spec.get(int(spec_id), []):
                    db.add(PlannedOrderStage(
                        run_id=int(run.run_id),
                        order_id=int(order.order_id),
                        stage_id=int(stage_id),
                        area_id=resource_id_by_spec.get(int(spec_id)),
                        bucket_date=order.bucket_date,
                        hours=float(hours_per_unit) * qty,
                    ))
                    stage_count += 1

            db.flush()
            scheduler = CapacityScheduler(db, run.config_snapshot)
            schedule_warnings: List[Dict[str, Any]] = []
            for order in sorted(created_production_orders, key=lambda o: (o.need_date, int(o.order_id))):
                stages = (
                    db.query(PlannedOrderStage)
                    .filter(PlannedOrderStage.order_id == int(order.order_id))
                    .all()
                )
                if not stages:
                    continue
                stage_hours = {int(stage.stage_id): _to_float(stage.hours) for stage in stages}
                stage_areas = {
                    int(stage.stage_id): (int(stage.area_id) if stage.area_id is not None else None)
                    for stage in stages
                }
                schedule_result, warnings = scheduler.schedule_backward(
                    int(order.item_id),
                    _to_float(order.qty),
                    order.need_date,
                    stage_hours,
                    stage_areas_by_stage=stage_areas,
                )
                for warning in warnings:
                    warning["run_id"] = int(run.run_id)
                    warning["order_id"] = int(order.order_id)
                schedule_warnings.extend(warnings)

                start_dt = schedule_result.get("order_start_date")
                finish_dt = schedule_result.get("order_finish_date")
                if isinstance(start_dt, datetime):
                    order.start_date = start_dt.date()
                elif isinstance(start_dt, date):
                    order.start_date = start_dt
                if isinstance(finish_dt, datetime):
                    order.finish_date = finish_dt.date()
                elif isinstance(finish_dt, date):
                    order.finish_date = finish_dt

            frozen_schedule_warnings = schedule_warnings
            # Only the include run records scheduler warnings on run.warnings
            # (legacy single-snapshot behaviour). Other runs report them in the
            # refreeze result only, never mutating their own header (v2 §6.5d).
            if schedule_warnings and is_include:
                run.warnings = list(run.warnings or []) + schedule_warnings

    # --- Freeze v2 ledger writers (per-run, per-version, frozen_at=now) ---
    frozen_item_ids = sorted(
        int(iid)
        for iid, gross_buckets in gross_map.items()
        if sum(float(q) for q in gross_buckets.values()) > 1e-9
    )
    baseline_rows = _write_freeze_baseline(
        db, run, new_version, frozen_item_ids, shared_pools.stock_initial, now
    )
    allocation_rows = _write_freeze_allocation(
        db, run, new_version, trace, req_by_item, shared_pools.stock_initial, now
    )
    component_rows = _write_freeze_component(db, run, new_version, trace, now)
    run.active_freeze_version = int(new_version)

    # No commit here — the orchestrator owns the queue transaction.
    return {
        "status": "ok",
        "run_id": int(run.run_id),
        "plan_id": int(plan.id),
        "freeze_version": int(new_version),
        "requirement_count": int(req_count),
        "bucket_count": int(bucket_count),
        "production_count": int(production_count),
        "stage_count": int(stage_count),
        "purchase_count": int(purchase_count),
        "rework_count": int(rework_count),
        "baseline_rows": int(baseline_rows),
        "allocation_rows": int(allocation_rows),
        "component_rows": int(component_rows),
        "schedule_warnings": len(frozen_schedule_warnings),
    }


def delete_period_plan_item(db: Session, plan_id: int, item_id: int) -> Dict[str, Any]:
    """Delete all lines for an item across all buckets of the plan. Locked lines block deletion."""
    plan = _get_plan(db, plan_id)
    _assert_plan_editable(plan)
    locked = (
        db.query(ProductionPlanLine)
        .filter(
            ProductionPlanLine.plan_id == int(plan.id),
            ProductionPlanLine.item_id == int(item_id),
            ProductionPlanLine.locked_by_run_id.isnot(None),
        )
        .count()
    )
    if locked > 0:
        raise ValueError("Нельзя удалить номенклатуру: есть строки, зафиксированные MRP-прогоном")
    deleted = (
        db.query(ProductionPlanLine)
        .filter(
            ProductionPlanLine.plan_id == int(plan.id),
            ProductionPlanLine.item_id == int(item_id),
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"status": "ok", "plan_id": int(plan.id), "item_id": int(item_id), "deleted": int(deleted)}


def add_item_to_period_plan(db: Session, plan_id: int, item_id: int) -> Dict[str, Any]:
    plan = _get_plan(db, plan_id)
    _assert_plan_editable(plan)
    item = db.query(Item).filter(Item.item_id == int(item_id)).first()
    if not item:
        raise ValueError("Номенклатура не найдена")
    first_bucket = _fridays_between(plan.period_from, plan.period_to)[0]
    line = (
        db.query(ProductionPlanLine)
        .filter(
            ProductionPlanLine.plan_id == int(plan.id),
            ProductionPlanLine.item_id == int(item_id),
            ProductionPlanLine.bucket_date == first_bucket,
        )
        .first()
    )
    if not line:
        db.add(ProductionPlanLine(plan_id=int(plan.id), item_id=int(item_id), bucket_date=first_bucket, qty=0))
        db.commit()
    return {"status": "ok", "plan_id": int(plan.id), "item_id": int(item_id)}


def bulk_upsert_period_plan_lines(db: Session, plan_id: int, entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    plan = _get_plan(db, plan_id)
    _assert_plan_editable(plan)
    saved = 0
    for entry in entries or []:
        item_id = int(entry.get("item_id"))
        bucket_date = _parse_date(entry.get("bucket_date") or entry.get("date"), "bucket_date")
        qty = Decimal(str(float(entry.get("qty") or 0.0)))
        if bucket_date < plan.period_from or bucket_date > plan.period_to:
            raise ValueError("Дата ячейки вне периода плана")
        line = (
            db.query(ProductionPlanLine)
            .filter(
                ProductionPlanLine.plan_id == int(plan.id),
                ProductionPlanLine.item_id == item_id,
                ProductionPlanLine.bucket_date == bucket_date,
            )
            .first()
        )
        if line:
            if line.locked_by_run_id is not None:
                raise ValueError("Строка уже использована в MRP и недоступна для редактирования")
            line.qty = qty
        else:
            db.add(ProductionPlanLine(plan_id=int(plan.id), item_id=item_id, bucket_date=bucket_date, qty=qty))
        saved += 1
    db.commit()
    return {"status": "ok", "saved": int(saved)}


def lock_period_plan_lines(db: Session, plan_id: int, run_id: int, line_ids: Optional[Iterable[int]] = None) -> int:
    plan = _get_plan(db, plan_id)
    q = db.query(ProductionPlanLine).filter(ProductionPlanLine.plan_id == int(plan.id))
    if line_ids is not None:
        ids = [int(x) for x in line_ids]
        q = q.filter(ProductionPlanLine.id.in_(ids))
    count = q.update({"locked_by_run_id": int(run_id)}, synchronize_session=False)
    db.commit()
    return int(count or 0)


def _latest_fixed_run_for_plan(db: Session, plan_id: int) -> Optional[PlanningRun]:
    return (
        db.query(PlanningRun)
        .filter(
            PlanningRun.source_plan_id == int(plan_id),
            PlanningRun.status == "FIXED_SNAPSHOT",
        )
        .order_by(PlanningRun.run_id.desc())
        .first()
    )


def _bom_descendants_by_item(db: Session, item_ids: Iterable[int]) -> Dict[int, Set[int]]:
    roots = sorted({int(i) for i in item_ids})
    if not roots:
        return {}

    spec_by_item: Dict[int, int] = {
        int(row.item_id): int(row.spec_id)
        for row in db.query(DefaultSpecification.item_id, DefaultSpecification.spec_id)
        .filter(DefaultSpecification.item_id.in_(roots))
        .all()
    }
    result: Dict[int, Set[int]] = {root: {root} for root in roots}

    def visit(root_id: int, item_id: int, seen: Set[int]) -> None:
        spec_id = spec_by_item.get(int(item_id))
        if not spec_id or spec_id in seen:
            return
        seen.add(spec_id)
        components = (
            db.query(SpecComponent.item_id)
            .filter(SpecComponent.spec_id == int(spec_id))
            .all()
        )
        for row in components:
            child_id = int(row.item_id)
            result[root_id].add(child_id)
            if child_id not in spec_by_item:
                ds = db.query(DefaultSpecification.spec_id).filter(DefaultSpecification.item_id == child_id).first()
                if ds:
                    spec_by_item[child_id] = int(ds.spec_id)
            visit(root_id, child_id, seen)

    for root in roots:
        visit(root, root, set())
    return result


def _plan_matrix_forecasts(
    db: Session,
    plan_id: int,
    item_ids: Iterable[int],
    bucket_keys: Iterable[str],
) -> Dict[Tuple[int, str], Dict[str, Any]]:
    run = _latest_fixed_run_for_plan(db, plan_id)
    if not run:
        return {}
    ids = sorted({int(i) for i in item_ids})
    if not ids:
        return {}
    buckets = [_parse_date(key, "bucket_date") for key in bucket_keys]
    descendants = _bom_descendants_by_item(db, ids)
    all_relevant_item_ids = sorted(set().union(*descendants.values())) if descendants else ids
    result: Dict[Tuple[int, str], Dict[str, Any]] = {}
    rows = (
        db.query(PlannedOrder)
        .filter(PlannedOrder.run_id == int(run.run_id), PlannedOrder.item_id.in_(all_relevant_item_ids))
        .all()
    )
    for order in rows:
        forecast = order.finish_date or order.start_date or order.need_date
        order_need = order.bucket_date or order.need_date
        if not order_need or not forecast:
            continue
        order_item_id = int(order.item_id)
        for root_id, related_item_ids in descendants.items():
            if order_item_id not in related_item_ids:
                continue
            for plan_bucket in buckets:
                if order_need > plan_bucket:
                    continue
                payload = _forecast_payload(forecast, plan_bucket)
                if (payload.get("forecast_shift_days") or 0) <= 0:
                    continue
                key = (int(root_id), plan_bucket.isoformat())
                prev = result.get(key)
                if not prev or (payload.get("forecast_shift_days") or 0) > (prev.get("forecast_shift_days") or 0):
                    result[key] = payload
    return result


def get_period_plan_matrix(db: Session, plan_id: int) -> Dict[str, Any]:
    plan = _get_plan(db, plan_id)
    buckets = _fridays_between(plan.period_from, plan.period_to)
    bucket_keys = [dt.isoformat() for dt in buckets]

    rows = (
        db.query(
            Item.item_id,
            Item.item_code,
            Item.item_name,
            Item.item_article,
            ProductionPlanLine.bucket_date,
            ProductionPlanLine.qty,
            ProductionPlanLine.locked_by_run_id,
        )
        .join(Item, Item.item_id == ProductionPlanLine.item_id)
        .filter(ProductionPlanLine.plan_id == int(plan.id))
        .order_by(Item.item_name.asc(), Item.item_code.asc(), ProductionPlanLine.bucket_date.asc())
        .all()
    )

    forecast_by_cell = _plan_matrix_forecasts(db, int(plan.id), [int(row.item_id) for row in rows], bucket_keys)

    by_item: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        item_id = int(row.item_id)
        rec = by_item.setdefault(
            item_id,
            {
                "item_id": item_id,
                "item_code": str(row.item_code or ""),
                "item_name": str(row.item_name or ""),
                "item_article": str(row.item_article or "") if row.item_article else None,
                "total_qty": 0.0,
                "buckets": {key: 0.0 for key in bucket_keys},
                "locked_buckets": {},
                "bucket_forecasts": {},
            },
        )
        key = row.bucket_date.isoformat()
        q = _to_float(row.qty)
        rec["buckets"][key] = q
        rec["total_qty"] = _to_float(rec["total_qty"]) + q
        if row.locked_by_run_id is not None:
            rec["locked_buckets"][key] = int(row.locked_by_run_id)
        forecast = forecast_by_cell.get((item_id, key))
        if forecast:
            rec["bucket_forecasts"][key] = forecast

    return {
        "plan": _serialize_plan(plan),
        "buckets": bucket_keys,
        "rows": list(by_item.values()),
        "total": len(by_item),
    }


def _execution_snapshot_key(
    *,
    plan_id: int,
    run_id: int,
    root_item_id: Optional[int],
    bom_level: Optional[int],
    flow: Optional[str],
) -> str:
    return (
        f"plan={int(plan_id)};run={int(run_id)};"
        f"root={int(root_item_id) if root_item_id is not None else '*'};"
        f"level={int(bom_level) if bom_level is not None else '*'};"
        f"flow={flow or '*'}"
    )


def _resolve_execution_run(db: Session, plan: ProductionPlanHeader, run_id: Optional[int]) -> PlanningRun:
    if run_id is not None:
        run = db.query(PlanningRun).filter(PlanningRun.run_id == int(run_id)).first()
        if not run or int(run.source_plan_id or -1) != int(plan.id):
            raise ValueError("Run not found for this plan")
        return run
    run = (
        db.query(PlanningRun)
        .filter(
            PlanningRun.source_plan_id == int(plan.id),
            PlanningRun.status == "FIXED_SNAPSHOT",
        )
        .order_by(PlanningRun.run_id.desc())
        .first()
    )
    if not run:
        raise ValueError("No FIXED_SNAPSHOT run found for this plan")
    return run


def _execution_unavailable_payload(
    db: Session,
    *,
    plan: ProductionPlanHeader,
    run: PlanningRun,
    root_item_id: Optional[int],
    bom_level: Optional[int],
    flow: Optional[str],
    truth_state: Any,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    reqs_with_items = (
        db.query(MrpRequirement, Item)
        .join(Item, Item.item_id == MrpRequirement.item_id)
        .filter(MrpRequirement.run_id == int(run.run_id))
        .order_by(MrpRequirement.bom_level.asc(), Item.item_name.asc())
        .all()
    )
    if root_item_id is not None:
        related_ids = _bom_descendants_by_item(db, [int(root_item_id)]).get(
            int(root_item_id), {int(root_item_id)}
        )
        reqs_with_items = [
            (req, item) for req, item in reqs_with_items if int(req.item_id) in related_ids
        ]
    rows: List[Dict[str, Any]] = []
    for req, item in reqs_with_items:
        item_flow = classify_replenishment_flow(getattr(item, "replenishment_method", None))
        if bom_level is not None and int(req.bom_level or 0) != bom_level:
            continue
        if flow is not None and item_flow != flow:
            continue
        rows.append({
            "req_id": int(req.id),
            "item_id": int(req.item_id),
            "item_code": str(item.item_code or ""),
            "item_article": str(item.item_article or "") if item.item_article else None,
            "item_name": str(item.item_name or ""),
            "flow": item_flow,
            "bom_level": int(req.bom_level or 0),
            "gross_qty": _to_float(req.total_required_qty),
            "net_qty": _to_float(req.net_required_qty),
            "completed_qty": None,
            "covered_qty": None,
            "remaining_qty": None,
            "progress_base_qty": None,
            "coverage_pct": None,
            "status": "execution_unavailable",
            "work_items": [],
        })
    state_value = lambda name: (
        truth_state.get(name) if isinstance(truth_state, dict) else getattr(truth_state, name, None)
    )
    cutoff = state_value("cutoff")
    cutoff_value = cutoff.isoformat() if hasattr(cutoff, "isoformat") else cutoff
    truth_status = "unavailable" if reason else (state_value("status") or "unavailable")
    generation = state_value("generation_id")
    return {
        "plan": _serialize_plan(plan),
        "run_id": int(run.run_id),
        "truth_status": truth_status,
        "ledger_generation": generation,
        "cutoff": cutoff_value,
        "truth_reason": reason or state_value("reason") or "Execution snapshot is not published",
        "rows": rows,
        "summary": {
            "truth_status": truth_status,
            "total_items": len(rows),
            "execution_completed_qty": None,
            "execution_base_qty": None,
            "execution_pct": None,
            "execution_by_flow": None,
        },
    }


def get_period_plan_execution_journal(
    db: Session,
    plan_id: int,
    *,
    run_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    bom_level: Optional[int] = None,
    flow: Optional[str] = None,
) -> Dict[str, Any]:
    """Read the immutable execution snapshot. Never computes or publishes."""
    from .planning_truth import (
        CAPABILITY_EXECUTION_ALLOCATIONS,
        CAPABILITY_PHYSICAL_LEDGER,
        CAPABILITY_PLANNING_SNAPSHOTS,
        CAPABILITY_RESERVATION_REPLAY,
        PlanningTruthUnavailable,
        get_latest_read_snapshot,
        get_truth_state,
    )

    plan = _get_plan(db, plan_id)
    run = _resolve_execution_run(db, plan, run_id)
    snapshot_key = _execution_snapshot_key(
        plan_id=plan.id, run_id=run.run_id, root_item_id=root_item_id,
        bom_level=bom_level, flow=flow,
    )
    capabilities = (
        CAPABILITY_PHYSICAL_LEDGER,
        CAPABILITY_RESERVATION_REPLAY,
        CAPABILITY_EXECUTION_ALLOCATIONS,
        CAPABILITY_PLANNING_SNAPSHOTS,
    )
    try:
        snapshot = get_latest_read_snapshot(
            db,
            consumer="period_plan_execution",
            snapshot_key=snapshot_key,
            required_capabilities=capabilities,
        )
    except PlanningTruthUnavailable as exc:
        return _execution_unavailable_payload(
            db, plan=plan, run=run, root_item_id=root_item_id,
            bom_level=bom_level, flow=flow, truth_state=exc.state,
        )
    if snapshot is None:
        return _execution_unavailable_payload(
            db, plan=plan, run=run, root_item_id=root_item_id,
            bom_level=bom_level, flow=flow, truth_state=get_truth_state(db),
            reason="Execution snapshot is missing for the accepted Ledger generation",
        )
    return dict(snapshot.payload)


def _compute_legacy_period_plan_execution_journal(
    db: Session,
    plan_id: int,
    *,
    run_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    bom_level: Optional[int] = None,
    flow: Optional[str] = None,
) -> Dict[str, Any]:
    plan = _get_plan(db, plan_id)

    if run_id is not None:
        run = db.query(PlanningRun).filter(PlanningRun.run_id == int(run_id)).first()
        if not run or int(run.source_plan_id or -1) != int(plan.id):
            raise ValueError("Run not found for this plan")
    else:
        run = (
            db.query(PlanningRun)
            .filter(
                PlanningRun.source_plan_id == int(plan.id),
                PlanningRun.status == "FIXED_SNAPSHOT",
            )
            .order_by(PlanningRun.run_id.desc())
            .first()
        )
        if not run:
            raise ValueError("No FIXED_SNAPSHOT run found for this plan")

    reqs_with_items = (
        db.query(MrpRequirement, Item)
        .join(Item, Item.item_id == MrpRequirement.item_id)
        .filter(MrpRequirement.run_id == int(run.run_id))
        .order_by(MrpRequirement.bom_level.asc(), Item.item_name.asc())
        .all()
    )
    if root_item_id is not None:
        related_ids = _bom_descendants_by_item(db, [int(root_item_id)]).get(int(root_item_id), {int(root_item_id)})
        reqs_with_items = [(req, item) for req, item in reqs_with_items if int(req.item_id) in related_ids]

    if not reqs_with_items:
        from .planning_truth import get_truth_state

        empty_truth = get_truth_state(db)
        execution_available = bool(empty_truth.ready)
        return {
            "plan": _serialize_plan(plan),
            "run_id": int(run.run_id),
            "truth_status": empty_truth.status,
            "truth_generation_id": empty_truth.generation_id,
            "ledger_generation": empty_truth.generation_id,
            "truth_cutoff": empty_truth.cutoff.isoformat() if empty_truth.cutoff else None,
            "cutoff": empty_truth.cutoff.isoformat() if empty_truth.cutoff else None,
            "truth_reason": empty_truth.reason,
            "rows": [],
            "summary": {
                "truth_status": empty_truth.status,
                "total_items": 0,
                "fully_covered": 0,
                "partially_covered": 0,
                "not_covered": 0,
                "net_zero": 0,
                "execution_completed_qty": 0.0 if execution_available else None,
                "execution_base_qty": 0.0 if execution_available else None,
                "execution_pct": 100.0 if execution_available else None,
                "execution_by_flow": {} if execution_available else None,
            },
        }

    req_ids = [int(req.id) for req, _ in reqs_with_items]
    item_ids = [int(req.item_id) for req, _ in reqs_with_items]
    req_by_id = {int(req.id): req for req, _ in reqs_with_items}

    # Execution is a fact, and accepted Item Ledger truth is its only source.
    # Do this before touching any legacy order aggregates: an empty/unaccepted
    # ledger must never be presented as apparently precise execution.
    from .planning_truth import (
        CAPABILITY_EXECUTION_ALLOCATIONS,
        CAPABILITY_PHYSICAL_LEDGER,
        CAPABILITY_RESERVATION_REPLAY,
        PlanningTruthUnavailable,
        require_accepted_truth,
    )

    try:
        truth_state = require_accepted_truth(
            db,
            consumer="period_plan_execution",
            required_capabilities=(
                CAPABILITY_PHYSICAL_LEDGER,
                CAPABILITY_RESERVATION_REPLAY,
                CAPABILITY_EXECUTION_ALLOCATIONS,
            ),
        )
    except PlanningTruthUnavailable as exc:
        truth_state = exc.state

        def _truth_value(name: str) -> Any:
            if isinstance(truth_state, dict):
                return truth_state.get(name)
            return getattr(truth_state, name, None)

        unavailable_rows: List[Dict[str, Any]] = []
        for req, item in reqs_with_items:
            item_flow = classify_replenishment_flow(getattr(item, "replenishment_method", None))
            if bom_level is not None and int(req.bom_level or 0) != bom_level:
                continue
            if flow is not None and item_flow != flow:
                continue
            unavailable_rows.append({
                "req_id": int(req.id),
                "item_id": int(req.item_id),
                "item_code": str(item.item_code or ""),
                "item_article": str(item.item_article or "") if item.item_article else None,
                "item_name": str(item.item_name or ""),
                "flow": item_flow,
                "bom_level": int(req.bom_level or 0),
                "gross_qty": _to_float(req.total_required_qty),
                "net_qty": _to_float(req.net_required_qty),
                "completed_qty": None,
                "covered_qty": None,
                "remaining_qty": None,
                "progress_base_qty": None,
                "coverage_pct": None,
                "status": "execution_unavailable",
                "work_items": [],
            })
        return {
            "plan": _serialize_plan(plan),
            "run_id": int(run.run_id),
            "truth_status": _truth_value("status") or "unavailable",
            "truth_generation_id": _truth_value("generation_id"),
            "ledger_generation": _truth_value("generation_id"),
            "truth_cutoff": (
                _truth_value("cutoff").isoformat()
                if hasattr(_truth_value("cutoff"), "isoformat")
                else _truth_value("cutoff")
            ),
            "cutoff": (
                _truth_value("cutoff").isoformat()
                if hasattr(_truth_value("cutoff"), "isoformat")
                else _truth_value("cutoff")
            ),
            "truth_reason": _truth_value("reason"),
            "rows": unavailable_rows,
            "summary": {
                "truth_status": _truth_value("status") or "unavailable",
                "total_items": len(unavailable_rows),
                "execution_completed_qty": None,
                "execution_base_qty": None,
                "execution_pct": None,
                "execution_by_flow": None,
            },
        }

    # Production: actual production orders linked via source_mrp_requirement_id.
    prod_rows = (
        db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .filter(ProductionProduct.source_mrp_requirement_id.in_(req_ids))
        .all()
    )
    prods_by_req_id: Dict[int, List[Dict[str, Any]]] = {}
    prod_ordered_by_req_id: Dict[int, float] = {}
    prod_done_by_req_id: Dict[int, float] = {}
    for pp, po, state in prod_rows:
        if state and str(state.status or "").lower() in {"cancelled"}:
            continue
        req_id = int(pp.source_mrp_requirement_id)
        qty_value = _to_float(pp.quantity)
        remaining_value = _to_float(pp.remaining_qty)
        done_value = min(qty_value, max(0.0, _to_float(getattr(pp, "produced_qty", 0.0))))
        is_one_c_opened = bool(po.order_ref1c)
        if is_one_c_opened:
            prod_ordered_by_req_id[req_id] = prod_ordered_by_req_id.get(req_id, 0.0) + qty_value
            prod_done_by_req_id[req_id] = prod_done_by_req_id.get(req_id, 0.0) + done_value
        req_due = req_by_id.get(req_id).period_to if req_by_id.get(req_id) else None
        planned_finish = state.planned_finish_date if state and state.planned_finish_date else None
        forecast = _forecast_payload(planned_finish, req_due or planned_finish)
        prods_by_req_id.setdefault(req_id, []).append({
            "type": "production_order",
            "product_id": int(pp.product_id),
            "order_id": int(po.order_id),
            "order_number": str(po.order_number or ""),
            "order_ref1c": str(po.order_ref1c or "") if po.order_ref1c else None,
            "order_source": str(po.source or "1c"),
            "one_c_opened": is_one_c_opened,
            "opened_at": state.opened_at.isoformat() if state and state.opened_at else None,
            "order_state": str(po.order_state_name or po.order_state_key or ""),
            "qty": qty_value,
            "completed_qty": done_value,
            "remaining_qty": remaining_value,
            **forecast,
        })

    # Production: direct 1C orders are not linked to a specific MRP
    # requirement.  Allocate them FIFO between period plans, otherwise the
    # same output is counted in every plan that contains the item and newer
    # plans can look completed while older ones remain open.
    direct_prods_by_req_id: Dict[int, List[Dict[str, Any]]] = {}
    direct_prod_ordered_by_req_id: Dict[int, float] = {}
    direct_prod_done_by_req_id: Dict[int, float] = {}
    direct_items_by_item: Dict[int, List[Dict[str, Any]]] = {}
    direct_query = (
        db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .filter(ProductionProduct.item_id.in_(item_ids))
        .filter(ProductionProduct.source_mrp_requirement_id.is_(None))
        .filter(ProductionOrder.source == "1c")
        .filter(ProductionOrder.order_ref1c.isnot(None))
        .filter(ProductionOrder.deletion_mark.is_(False))
    )
    # A direct 1C order belongs only to the period in which it was opened.
    # It must never close the execution of a future release programme.
    direct_from = plan.period_from or _DIRECT_1C_PRODUCTION_HORIZON
    direct_query = direct_query.filter(
        ProductionOrder.order_date >= datetime.combine(direct_from, datetime.min.time())
    )
    if plan.period_to:
        direct_query = direct_query.filter(
            ProductionOrder.order_date < datetime.combine(plan.period_to + timedelta(days=1), datetime.min.time())
        )
    direct_prod_rows = direct_query.all()
    for pp, po, state in direct_prod_rows:
        if state and str(state.status or "").lower() in {"cancelled"}:
            continue
        item_id = int(pp.item_id)
        qty_value = _to_float(pp.quantity)
        remaining_value = _to_float(pp.remaining_qty)
        produced_value = max(0.0, _to_float(getattr(pp, "produced_qty", 0.0)))
        is_done_state = str(po.order_state_key or "").lower() == _DONE_STATE_KEY
        done_value = min(qty_value, produced_value)
        if is_done_state and done_value <= 1e-9:
            done_value = qty_value
        planned_finish = state.planned_finish_date if state and state.planned_finish_date else None
        direct_items_by_item.setdefault(item_id, []).append({
            "type": "production_order",
            "product_id": int(pp.product_id),
            "order_id": int(po.order_id),
            "order_number": str(po.order_number or ""),
            "order_ref1c": str(po.order_ref1c or "") if po.order_ref1c else None,
            "order_source": str(po.source or "1c"),
            "one_c_opened": True,
            "opened_at": state.opened_at.isoformat() if state and state.opened_at else None,
            "order_state": str(po.order_state_name or po.order_state_key or ""),
            "qty": qty_value,
            "completed_qty": done_value,
            "remaining_qty": remaining_value,
            **_forecast_payload(planned_finish, plan.period_to or planned_finish),
        })

    # Each journal is self-contained.  Period filtering above already makes a
    # direct order unique to its period, so allocating it to older plans would
    # incorrectly let a past programme consume current execution.
    fifo_requirements: Dict[int, List[Tuple[MrpRequirement, ProductionPlanHeader]]] = {}
    for candidate_req, _item in reqs_with_items:
        fifo_requirements.setdefault(int(candidate_req.item_id), []).append((candidate_req, plan))

    for item_id, direct_items in direct_items_by_item.items():
        demands = sorted(
            fifo_requirements.get(item_id, []),
            key=lambda entry: (
                entry[1].period_from,
                entry[1].period_to,
                int(entry[1].id),
                int(entry[0].bom_level or 0),
                int(entry[0].id),
            ),
        )
        for metric, linked_by_req, destination in (
            ("qty", prod_ordered_by_req_id, direct_prod_ordered_by_req_id),
            ("completed_qty", prod_done_by_req_id, direct_prod_done_by_req_id),
        ):
            available = sum(_to_float(direct_item[metric]) for direct_item in direct_items)
            for demand, _ in demands:
                req_id = int(demand.id)
                capacity = max(0.0, _to_float(demand.net_required_qty) - linked_by_req.get(req_id, 0.0))
                allocated = min(available, capacity)
                if allocated > 1e-9:
                    destination[req_id] = destination.get(req_id, 0.0) + allocated
                    available -= allocated
                if available <= 1e-9:
                    break

        # Keep the order details visible on the plan that received the FIFO
        # allocation. Quantities in the displayed work item are clipped to the
        # amount allocated to that requirement.
        for demand, _ in demands:
            req_id = int(demand.id)
            ordered_left = direct_prod_ordered_by_req_id.get(req_id, 0.0)
            completed_left = direct_prod_done_by_req_id.get(req_id, 0.0)
            if ordered_left <= 1e-9 and completed_left <= 1e-9:
                continue
            for direct_item in direct_items:
                item_qty = _to_float(direct_item["qty"])
                item_done = _to_float(direct_item["completed_qty"])
                allocated_qty = min(ordered_left, item_qty)
                allocated_done = min(completed_left, item_done, allocated_qty)
                if allocated_qty <= 1e-9 and allocated_done <= 1e-9:
                    continue
                allocated_item = dict(direct_item)
                allocated_item["qty"] = allocated_qty
                allocated_item["completed_qty"] = allocated_done
                allocated_item["remaining_qty"] = max(0.0, allocated_qty - allocated_done)
                direct_prods_by_req_id.setdefault(req_id, []).append(allocated_item)
                ordered_left -= allocated_qty
                completed_left -= allocated_done

    # Production: planned MRP tasks. They are the live work queue before real
    # 1C production orders are created, so the execution journal must show them.
    demand_refs = [f"mrp_requirement:{req_id}" for req_id in req_ids]
    planned_orders_by_req_id: Dict[int, List[Dict[str, Any]]] = {}
    planned_ordered_by_req_id: Dict[int, float] = {}
    if demand_refs:
        for po in (
            db.query(PlannedOrder)
            .filter(PlannedOrder.run_id == int(run.run_id), PlannedOrder.demand_ref.in_(demand_refs))
            .all()
        ):
            raw_ref = str(po.demand_ref or "")
            try:
                req_id = int(raw_ref.split(":", 1)[1])
            except Exception:
                continue
            qty_value = _to_float(po.qty)
            planned_ordered_by_req_id[req_id] = planned_ordered_by_req_id.get(req_id, 0.0) + qty_value
            forecast_date = po.finish_date or po.start_date or po.need_date
            planned_orders_by_req_id.setdefault(req_id, []).append({
                "type": "planned_order",
                "order_id": int(po.order_id),
                "qty": qty_value,
                "completed_qty": 0.0,
                "remaining_qty": qty_value,
                "need_date": po.need_date.isoformat() if po.need_date else None,
                **_forecast_payload(forecast_date, po.need_date),
            })

    # Purchase: PlannedPurchase linked via source_mrp_requirement_id (precise),
    # with a fallback to run_id + item_id for rows created before migration 08.
    purchases_by_req_id: Dict[int, List[Dict[str, Any]]] = {}
    purchases_by_item_fallback: Dict[int, List[Dict[str, Any]]] = {}
    purchase_ordered_by_req_id: Dict[int, float] = {}
    purchase_ordered_by_item_fallback: Dict[int, float] = {}
    purchase_done_by_req_id: Dict[int, float] = {}
    purchase_done_by_item_fallback: Dict[int, float] = {}
    planned_purchase_rows = (
        db.query(PlannedPurchase)
        .filter(PlannedPurchase.run_id == int(run.run_id), PlannedPurchase.item_id.in_(item_ids))
        .all()
    )
    purchase_ids = [int(pp.purchase_id) for pp in planned_purchase_rows]
    purchase_links_by_id: Dict[int, SyncLink] = {}
    if purchase_ids:
        for link in (
            db.query(SyncLink)
            .filter(
                SyncLink.source_system == "PRODPLAN",
                SyncLink.source_doctype == "planned_purchase",
                SyncLink.source_id.in_(purchase_ids),
                SyncLink.target_entity == "Document_ЗаказПоставщику",
                SyncLink.status == "success",
                SyncLink.target_ref_key.isnot(None),
            )
            .all()
        ):
            purchase_links_by_id[int(link.source_id)] = link
    supplier_refs = sorted({
        str(link.target_ref_key).strip()
        for link in purchase_links_by_id.values()
        if str(link.target_ref_key or "").strip()
    })
    supplier_orders_by_ref: Dict[str, SupplierOrder] = {}
    if supplier_refs:
        for order in (
            db.query(SupplierOrder)
            .filter(SupplierOrder.order_ref1c.in_(supplier_refs))
            .all()
        ):
            supplier_orders_by_ref[str(order.order_ref1c or "").strip()] = order

    for pp in planned_purchase_rows:
        qty_value = _to_float(pp.qty)
        purchase_id = int(pp.purchase_id)
        link = purchase_links_by_id.get(purchase_id)
        supplier_ref = str(getattr(link, "target_ref_key", "") or "").strip() if link else ""
        supplier_order = supplier_orders_by_ref.get(supplier_ref) if supplier_ref else None
        is_ordered = bool(supplier_ref)
        is_done = _is_supplier_order_done(supplier_order)
        ordered_value = qty_value if is_ordered else 0.0
        done_value = qty_value if is_done else 0.0
        entry = {
            "type": "planned_purchase",
            "purchase_id": purchase_id,
            "qty": qty_value,
            "completed_qty": done_value,
            "remaining_qty": max(0.0, qty_value - done_value),
            "need_date": pp.need_date.isoformat() if pp.need_date else None,
            "order_date": pp.order_date.isoformat() if pp.order_date else None,
            "lead_time_days": int(pp.lead_time_days or 0),
            "order_ref1c": supplier_ref or None,
            "order_number": str(getattr(supplier_order, "order_number", "") or "") if supplier_order else None,
            "order_state": str(getattr(supplier_order, "order_state_name", "") or getattr(supplier_order, "order_state_key", "") or "") if supplier_order else None,
            "one_c_opened": is_ordered,
            **_forecast_payload(pp.need_date, pp.need_date, reason="purchase"),
        }
        if pp.source_mrp_requirement_id is not None:
            req_id = int(pp.source_mrp_requirement_id)
            purchases_by_req_id.setdefault(req_id, []).append(entry)
            purchase_ordered_by_req_id[req_id] = purchase_ordered_by_req_id.get(req_id, 0.0) + ordered_value
            purchase_done_by_req_id[req_id] = purchase_done_by_req_id.get(req_id, 0.0) + done_value
        else:
            item_id = int(pp.item_id)
            purchases_by_item_fallback.setdefault(item_id, []).append(entry)
            purchase_ordered_by_item_fallback[item_id] = purchase_ordered_by_item_fallback.get(item_id, 0.0) + ordered_value
            purchase_done_by_item_fallback[item_id] = purchase_done_by_item_fallback.get(item_id, 0.0) + done_value

    # Rework: PlannedRework by run_id + item_id
    reworks_by_item: Dict[int, List[Dict[str, Any]]] = {}
    rework_ordered_by_item: Dict[int, float] = {}
    for rw in (
        db.query(PlannedRework)
        .filter(PlannedRework.run_id == int(run.run_id), PlannedRework.item_id.in_(item_ids))
        .all()
    ):
        item_id = int(rw.item_id)
        qty_value = _to_float(rw.qty)
        rework_ordered_by_item[item_id] = rework_ordered_by_item.get(item_id, 0.0) + qty_value
        reworks_by_item.setdefault(int(rw.item_id), []).append({
            "type": "planned_rework",
            "rework_id": int(rw.rework_id),
            "qty": qty_value,
            "completed_qty": 0.0,
            "remaining_qty": qty_value,
            "need_date": rw.need_date.isoformat() if rw.need_date else None,
            "order_date": rw.order_date.isoformat() if rw.order_date else None,
            "lead_time_days": int(rw.lead_time_days or 0),
            **_forecast_payload(rw.need_date, rw.need_date, reason="rework"),
        })

    rows: List[Dict[str, Any]] = []
    summary = {
        "total_items": 0,
        "fully_covered": 0,
        "partially_covered": 0,
        "not_covered": 0,
        "net_zero": 0,
        "execution_completed_qty": 0.0,
        "execution_base_qty": 0.0,
        "execution_pct": 100.0,
        "execution_by_flow": {},
    }
    execution_by_flow: Dict[str, Dict[str, float]] = {}

    for req, item in reqs_with_items:
        item_flow = classify_replenishment_flow(getattr(item, "replenishment_method", None))

        if bom_level is not None and int(req.bom_level or 0) != bom_level:
            continue
        if flow is not None and item_flow != flow:
            continue

        req_id = int(req.id)
        item_id = int(req.item_id)
        gross_qty = _to_float(req.total_required_qty)
        net_qty = _to_float(req.net_required_qty)
        stock_qty = max(0.0, gross_qty - net_qty)

        if item_flow == REPLENISHMENT_FLOW_PRODUCTION:
            actual_items = prods_by_req_id.get(req_id, [])
            direct_items = direct_prods_by_req_id.get(req_id, [])
            planned_items = planned_orders_by_req_id.get(req_id, [])
            work_items = actual_items + direct_items or planned_items
            # "В заказах" is the quantity placed into real production orders.
            # Planned MRP tasks remain visible in work_items and in "К запуску",
            # but they are not actual orders yet.
            ordered_qty = prod_ordered_by_req_id.get(req_id, 0.0) + direct_prod_ordered_by_req_id.get(req_id, 0.0)
            completed_qty = prod_done_by_req_id.get(req_id, 0.0) + direct_prod_done_by_req_id.get(req_id, 0.0)
        elif item_flow == REPLENISHMENT_FLOW_PURCHASE:
            work_items = purchases_by_req_id.get(req_id, []) or purchases_by_item_fallback.get(item_id, [])
            ordered_qty = purchase_ordered_by_req_id.get(req_id, purchase_ordered_by_item_fallback.get(item_id, 0.0))
            completed_qty = purchase_done_by_req_id.get(req_id, purchase_done_by_item_fallback.get(item_id, 0.0))
        else:
            work_items = reworks_by_item.get(item_id, [])
            ordered_qty = rework_ordered_by_item.get(item_id, 0.0)
            completed_qty = 0.0

        progress_base_qty = net_qty if net_qty > 1e-9 else ordered_qty
        completed_qty = min(max(0.0, completed_qty), progress_base_qty) if progress_base_qty > 1e-9 else 0.0
        remaining_qty = max(0.0, progress_base_qty - completed_qty)
        unassigned_qty = max(0.0, net_qty - ordered_qty)
        progress_pct = round(completed_qty / progress_base_qty * 100.0, 1) if progress_base_qty > 1e-9 else 100.0
        forecast_dates: List[date] = []
        for wi in work_items:
            raw_forecast = wi.get("forecast_date") or wi.get("need_date")
            if raw_forecast:
                try:
                    forecast_dates.append(_parse_date(raw_forecast, "forecast_date"))
                except Exception:
                    pass
        row_forecast = max(forecast_dates) if forecast_dates else None
        row_due = req.period_to if req.period_to else None
        row_forecast_payload = _forecast_payload(row_forecast, row_due)

        need_dates: List[date] = []
        for wi in work_items:
            raw_need = wi.get("need_date")
            if raw_need:
                try:
                    need_dates.append(_parse_date(raw_need, "need_date"))
                except Exception:
                    pass
        row_need_date = min(need_dates) if need_dates else row_due

        if net_qty < 1e-9:
            row_status = "net_zero"
        elif remaining_qty < 1e-9:
            row_status = "covered"
        elif completed_qty > 1e-9:
            row_status = "partial"
        elif ordered_qty > 1e-9:
            row_status = "ordered"
        else:
            row_status = "none"

        rows.append({
            "req_id": req_id,
            "item_id": item_id,
            "item_code": str(item.item_code or ""),
            "item_article": str(item.item_article or "") if item.item_article else None,
            "item_name": str(item.item_name or ""),
            "flow": item_flow,
            "bom_level": int(req.bom_level or 0),
            "gross_qty": gross_qty,
            "stock_qty": stock_qty,
            "net_qty": net_qty,
            "ordered_qty": ordered_qty,
            "completed_qty": completed_qty,
            "covered_qty": completed_qty,
            "remaining_qty": remaining_qty,
            "unassigned_qty": unassigned_qty,
            "progress_base_qty": progress_base_qty,
            "coverage_pct": progress_pct,
            "need_date": row_need_date.isoformat() if row_need_date else None,
            "status": row_status,
            **row_forecast_payload,
            "work_items": work_items,
        })

        summary["total_items"] += 1
        summary["execution_completed_qty"] += completed_qty
        summary["execution_base_qty"] += progress_base_qty
        flow_summary = execution_by_flow.setdefault(
            item_flow,
            {"completed_qty": 0.0, "base_qty": 0.0, "execution_pct": 100.0},
        )
        flow_summary["completed_qty"] += completed_qty
        flow_summary["base_qty"] += progress_base_qty
        if net_qty < 1e-9:
            summary["net_zero"] += 1
        elif remaining_qty < 1e-9:
            summary["fully_covered"] += 1
        elif completed_qty > 1e-9:
            summary["partially_covered"] += 1
        else:
            summary["not_covered"] += 1

    execution_base_qty = _to_float(summary["execution_base_qty"])
    summary["execution_pct"] = (
        round(_to_float(summary["execution_completed_qty"]) / execution_base_qty * 100.0, 1)
        if execution_base_qty > 1e-9
        else 100.0
    )
    for flow_summary in execution_by_flow.values():
        base_qty = _to_float(flow_summary["base_qty"])
        flow_summary["execution_pct"] = (
            round(_to_float(flow_summary["completed_qty"]) / base_qty * 100.0, 1)
            if base_qty > 1e-9
            else 100.0
        )
    summary["execution_by_flow"] = execution_by_flow
    summary["truth_status"] = truth_state.status

    return {
        "plan": _serialize_plan(plan),
        "run_id": int(run.run_id),
        "truth_status": truth_state.status,
        "truth_generation_id": truth_state.generation_id,
        "ledger_generation": truth_state.generation_id,
        "truth_cutoff": truth_state.cutoff.isoformat() if truth_state.cutoff else None,
        "cutoff": truth_state.cutoff.isoformat() if truth_state.cutoff else None,
        "truth_reason": truth_state.reason,
        "rows": rows,
        "summary": summary,
    }


def build_period_plan_execution_snapshot(
    db: Session,
    plan_id: int,
    *,
    run_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    bom_level: Optional[int] = None,
    flow: Optional[str] = None,
) -> Dict[str, Any]:
    """Reserved entry point for the future Ledger-allocation builder.

    The retained legacy diagnostic reads produced/received order aggregates.
    Those values are not accepted Item Ledger facts and must never be published
    under an accepted Ledger generation, regardless of declared capabilities.
    """
    raise NotImplementedError(
        "period-plan execution snapshot publication is blocked until a "
        "generation-scoped Item Ledger allocation builder is implemented"
    )

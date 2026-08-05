from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import func, text
from sqlalchemy.orm import Session

if TYPE_CHECKING:  # pragma: no cover - typing only; runtime import is mrp_freeze→here
    from .mrp_freeze import FreezeSharedPools, FreezeTrace

from ..models import (
    DefaultSpecification,
    Item,
    LedgerGeneration,
    MrpRequirement,
    MrpRequirementBucket,
    MrpRunRoot,
    Operation,
    PlannedOrder,
    PlannedOrderStage,
    PlannedPurchase,
    PlannedRework,
    ClosedPlanSnapshot,
    PlanningRun,
    PlanningReadSnapshot,
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
    ReservationEntry,
    ReservationEvent,
    ReplenishmentWorkItem,
)
from .planning_service import (
    DEFAULT_PLANNING_CONFIG,
    get_active_planning_config,
)
from .mrp_stock_helpers import (
    active_wip_eta_by_item as _active_wip_eta_by_item,
    consume_wip_at_or_before as _consume_wip_at_or_before,
    consume_wip_detailed as _consume_wip_detailed,
    effective_stock_by_item_all as _effective_stock_by_item_all,
)
from .planning_run_candidate import _resolve_parent_generation_id
from .forecast import forecast_payload as _forecast_payload
from .item_ledger.reservation import (
    replenishment_execution_pct,
    replenishment_execution_status,
    replenishment_remaining,
)
from .replenishment import (
    REPLENISHMENT_FLOW_PRODUCTION,
    REPLENISHMENT_FLOW_PURCHASE,
    REPLENISHMENT_FLOW_REWORK,
    classify_replenishment_flow,
)
from .warnings import make_warning
from .bom_specification_resolver import (
    BomSpecificationResolutionError,
    BomSpecificationResolver,
)


def _rounded_replenishment_pct(required_qty: Any, received_qty: Any) -> float | None:
    """Presentation precision only; the reservation core owns the formula."""
    value = replenishment_execution_pct(required_qty, received_qty)
    return None if value is None else round(float(value), 1)


# Matches planning_service.DONE_STATE_KEY — 1C state for completed production orders.
_DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"
_CLOSE_REFRESH_KEY_PREFIX = "close-fixed-run"
_FIX_REFRESH_KEY_PREFIX = "fix-period-plan"

# Canonical `PlannedOrder.demand_ref` spelling for a frozen MRP requirement.
# The writer below and `production_control_journal` (the other consumer of this
# link) both use it; `_LEGACY_DEMAND_REF_PREFIX` is accepted on read only, so
# rows written by older builds keep resolving to their requirement.
_DEMAND_REF_PREFIX = "mrp_requirement:"
_LEGACY_DEMAND_REF_PREFIX = "req:"


def _demand_ref(requirement_id: int) -> str:
    return f"{_DEMAND_REF_PREFIX}{int(requirement_id)}"


def _demand_ref_lookup(requirement_ids: Iterable[int]) -> Dict[str, int]:
    """Every accepted spelling of a requirement's demand_ref → requirement id."""
    lookup: Dict[str, int] = {}
    for requirement_id in requirement_ids:
        rid = int(requirement_id)
        lookup[f"{_DEMAND_REF_PREFIX}{rid}"] = rid
        lookup[f"{_LEGACY_DEMAND_REF_PREFIX}{rid}"] = rid
    return lookup


def _close_refresh_generation_key(*, run_id: int, parent_generation_id: int) -> str:
    return f"{_CLOSE_REFRESH_KEY_PREFIX}:{int(run_id)}:{int(parent_generation_id)}"


def _fix_refresh_generation_key(*, plan_id: int, parent_generation_id: int) -> str:
    """Server-owned refresh key for «Зафиксировать».

    Deterministic in ``(plan, parent generation)`` exactly like the close key, so
    a retry of the same fixation against the same accepted truth reuses one
    generation instead of forking a second one.  Callers never have to invent a
    key; the UI must not be able to pin one.
    """
    return f"{_FIX_REFRESH_KEY_PREFIX}:{int(plan_id)}:{int(parent_generation_id)}"


def _lock_mrp_ledger(db: Session) -> None:
    """Take the Ledger publication lock that ``run_obligation_refresh`` uses.

    The orchestrator acquires ``MRP_LEDGER_LOCK_KEY`` only once it is already
    committed to forking a generation.  Everything this module checks *before*
    that call — «does this plan already own a snapshot in the current accepted
    truth?» — used to run unserialised, so two concurrent fixations could both
    see "no snapshot", both fork, and leave the plan permanently poisoned with
    two current FIXED_SNAPSHOT runs.  Taking the *same* key here closes that
    TOCTOU window; PostgreSQL advisory locks are re-entrant inside one
    transaction, so the orchestrator's later acquisition is a no-op.

    SQLite (tests) has no advisory locks and a single writer, so this is a
    documented no-op — the same platform guard the orchestrator uses.
    """
    try:
        dialect = db.get_bind().dialect.name
    except Exception:  # pragma: no cover - unbound session in unit tests
        return
    if dialect != "postgresql":
        return
    from .mrp_freeze import MRP_LEDGER_LOCK_KEY

    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MRP_LEDGER_LOCK_KEY})


def _current_accepted_generation(db: Session) -> LedgerGeneration:
    """Resolve the one accepted Ledger generation every plan write descends from."""
    truth = db.get(PlanningTruthState, 1)
    if truth is None or truth.current_generation_id is None:
        raise ValueError("Current accepted Ledger truth is unavailable")
    parent = db.get(LedgerGeneration, int(truth.current_generation_id))
    if parent is None or str(parent.status) != "accepted":
        raise ValueError("Current accepted Ledger truth is unavailable")
    return parent


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
    persisted_output_by_plan: Dict[int, Dict[str, Any]] = {}
    execution_by_plan: Dict[int, Dict[str, Any]] = {}
    if plan_ids:
        stats_rows = (
            db.query(
                ProductionPlanLine.plan_id,
                func.count(ProductionPlanLine.id).label("line_count"),
                func.coalesce(func.sum(ProductionPlanLine.qty), 0.0).label("total_qty"),
                func.count(ProductionPlanLine.remaining_output_qty).label("initialized_count"),
                func.coalesce(func.sum(ProductionPlanLine.accepted_output_qty), 0.0).label(
                    "accepted_output_qty"
                ),
            )
            .filter(ProductionPlanLine.plan_id.in_(plan_ids))
            .group_by(ProductionPlanLine.plan_id)
            .all()
        )
        line_stats = {
            int(row.plan_id): {"line_count": int(row.line_count or 0), "total_qty": _to_float(row.total_qty)}
            for row in stats_rows
        }
        for row in stats_rows:
            line_count = int(row.line_count or 0)
            initialized = line_count > 0 and int(row.initialized_count or 0) == line_count
            if not initialized:
                continue
            planned = _to_float(row.total_qty)
            accepted = min(max(_to_float(row.accepted_output_qty), 0.0), planned)
            execution_pct = _rounded_replenishment_pct(planned, accepted)
            persisted_output_by_plan[int(row.plan_id)] = {
                "execution_pct": execution_pct,
                "execution_partial": False,
                "execution_progress_status": replenishment_execution_status(
                    planned,
                    accepted,
                    partial_truth=False,
                ),
                "execution_completed_qty": accepted,
                "execution_base_qty": planned,
            }
        truth_generation_id = (
            db.query(PlanningTruthState.current_generation_id)
            .filter(PlanningTruthState.id == 1)
            .scalar()
        )
        if truth_generation_id is not None:
            snapshots = (
                db.query(PlanningReadSnapshot)
                .filter(
                    PlanningReadSnapshot.consumer == "period_plan_execution",
                    PlanningReadSnapshot.ledger_generation_id
                    == int(truth_generation_id),
                    PlanningReadSnapshot.truth_status == "accepted",
                )
                .order_by(
                    PlanningReadSnapshot.published_at.desc(),
                    PlanningReadSnapshot.id.desc(),
                )
                .all()
            )
            wanted = set(plan_ids)
            for snapshot in snapshots:
                payload = dict(snapshot.payload or {})
                payload_plan = dict(payload.get("plan") or {})
                try:
                    payload_plan_id = int(payload_plan.get("id"))
                except (TypeError, ValueError):
                    continue
                if payload_plan_id not in wanted or payload_plan_id in execution_by_plan:
                    continue
                summary = dict(payload.get("summary") or {})
                execution_by_plan[payload_plan_id] = {
                    "execution_pct": (
                        summary.get("execution_pct")
                        if summary.get("execution_pct") is not None
                        else summary.get("execution_confirmed_pct")
                    ),
                    "execution_partial": bool(
                        summary.get("execution_partial", False)
                    ),
                    "execution_progress_status": replenishment_execution_status(
                        summary.get("execution_base_qty") or 0,
                        summary.get("execution_completed_qty") or 0,
                        partial_truth=bool(summary.get("execution_partial", False)),
                    ),
                    "execution_completed_qty": summary.get("execution_completed_qty"),
                    "execution_base_qty": summary.get("execution_base_qty"),
                    "execution_by_flow": summary.get("execution_by_flow") or {},
                    "execution_status": str(payload.get("truth_status") or ""),
                    "execution_reason": payload.get("truth_reason"),
                    "execution_generation_id": int(truth_generation_id),
                }
    return {
        "rows": [
            {
                **_serialize_plan(
                    plan,
                    line_count=int(line_stats.get(int(plan.id), {}).get("line_count", 0)),
                    total_qty=float(line_stats.get(int(plan.id), {}).get("total_qty", 0.0)),
                ),
                **{
                    **execution_by_plan.get(
                        int(plan.id),
                        {
                        "execution_pct": None,
                        "execution_partial": False,
                        "execution_progress_status": "unavailable",
                        "execution_completed_qty": None,
                        "execution_base_qty": None,
                        "execution_by_flow": {},
                        "execution_status": "unavailable",
                        "execution_reason": (
                            "Execution snapshot is missing for the accepted Ledger generation"
                            if truth_generation_id is not None
                            else "Accepted Ledger generation is unavailable"
                        ),
                        "execution_generation_id": (
                            int(truth_generation_id)
                            if truth_generation_id is not None
                            else None
                        ),
                        },
                    ),
                    # Plan execution is the persisted root-output counter.  A
                    # replacement MRP starts its own 0/N execution without
                    # resetting the immutable plan's accumulated X/total.
                    **persisted_output_by_plan.get(int(plan.id), {}),
                },
            }
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
    """The single atomic «Зафиксировать» action (``period_plan_target.md`` §Фиксация).

    Fixation is one operation, not two buttons: it validates a non-empty plan,
    freezes the release rows, explodes the BOM once and publishes the consistent
    single-generation Ledger snapshot (full reservations, availability coverage,
    replenishment need, make/buy routing, assembly queue) through
    ``run_obligation_refresh``, and only then marks the plan ``fixed``.

    Fail closed.  The status flip and the snapshot share one transaction: if the
    snapshot cannot be published the whole thing is rolled back and the plan
    stays ``draft`` and editable.  A plan can never end up ``fixed`` — immutable,
    with no way back to draft — while carrying no MRP snapshot.

    Idempotent for the recovery case: a plan that is already ``fixed`` and
    already owns a snapshot in the current accepted truth is returned unchanged
    (``mrp.immutable = True``); a plan left ``fixed`` without a snapshot by the
    old two-step flow gets its missing snapshot published here.
    """
    plan = _get_plan(db, plan_id)
    if plan.status == "closed":
        raise ValueError("Закрытый план нельзя фиксировать")
    # Serialise with the Ledger publication lock BEFORE any check that decides
    # whether a snapshot has to be created (see ``_lock_mrp_ledger``).
    _lock_mrp_ledger(db)
    has_release = (
        db.query(ProductionPlanLine.id)
        .filter(
            ProductionPlanLine.plan_id == int(plan.id),
            ProductionPlanLine.qty > 0,
        )
        .first()
    )
    if not has_release:
        raise ValueError("Нельзя зафиксировать пустой план: нет положительного выпуска")

    try:
        if plan.status != "fixed":
            plan.status = "fixed"
            plan.fixed_by = fixed_by
            plan.fixed_at = datetime.now(timezone.utc)
            # Flush, do not commit: the snapshot publisher requires a fixed plan
            # inside this same transaction, and a failure must undo the flip.
            db.flush()
        snapshot = create_mrp_snapshot_for_plan(
            db, int(plan.id), started_by=fixed_by or "api",
        )
        # Serialise while the transaction is still open: after the commit every
        # attribute is expired, and the published state is exactly what we are
        # about to commit.
        payload = _serialize_plan(plan)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {**payload, "mrp": snapshot}


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


def _explode_bom_net_first(
    db: Session,
    plan_demands: Dict[int, Dict[date, float]],
    shared_pools: Optional["FreezeSharedPools"] = None,
    trace: Optional["FreezeTrace"] = None,
    *,
    need_date_floor: date,
) -> Tuple[
    Dict[int, Dict[date, float]],
    Dict[int, Dict[date, float]],
    Dict[int, int],
    List[Dict[str, Any]],
]:
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

    ``need_date_floor`` is the earliest date a need may be dated to. It is the
    *generation cutoff*, never ``date.today()``: a rebuild of the same Ledger
    generation on another day must produce the same numbers (planning-truth
    invariants 4-5).

    Returns:
        gross_map  — {item_id: {bucket_date: gross_qty}}
        net_map    — {item_id: {bucket_date: net_qty}}  (after stock + WIP)
        bom_level_map — {item_id: minimum_bom_level}  (0 = plan item, 1 = component, …)
        warnings   — structured records of BOM edges that could not be exploded

    Cycle safety is PER PATH, not global: an item is re-exploded whenever new
    demand reaches it at a deeper level, and only an edge back into the item's
    own ancestors is refused. A single global "already exploded" set silently
    truncated convergent BOMs — with ``A→B→C`` and ``A→D→B→C`` the second arrival
    of ``B`` (at depth 2) got its gross/net but its children were never
    re-exploded, so ``C`` permanently lost the ``D`` branch's demand. Ancestors
    only ever grow, so the walk still terminates on a genuinely cyclic BOM.
    """
    # --- Pre-load BOM data in bulk (avoid N+1 per item) ---
    # Effective stock with ignored warehouses (e.g., brak isolator) excluded;
    # The Item model has no physical quantity. Stock comes only from accepted,
    # generation-scoped warehouse bins.
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
    if trace is not None:
        trace.root_item_ids = set(root_production_item_ids)

    spec_resolver = BomSpecificationResolver(db)
    default_spec_map: Dict[int, int] = {
        item_id: spec_id
        for item_id in sorted(int(value) for value in plan_demands)
        if (spec_id := spec_resolver.default_spec_id(item_id)) is not None
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
        # Fail closed: a WIP read that blows up used to be swallowed into an
        # empty pool, and an empty WIP pool inflates net demand for the whole
        # frozen — permanently, because the freeze is never recomputed.
        wip_eta_by_item = _active_wip_eta_by_item(db)

    # --- Buffer-days lookup: item → default spec → production_kind → resource.buffer_days ---
    # Pinned specs are selected by ref only when their edge is reached, but
    # their production-kind metadata must already be available for lead-time
    # shifting once selected.
    specs = db.query(Specification).all()
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

    buffer_days_cache: Dict[Tuple[int, int | None], int] = {}

    def clamp_to_floor(value: date) -> date:
        return need_date_floor if value < need_date_floor else value

    def resolve_buffer_days(item_id: int, spec_id: int | None) -> int:
        cache_key = (int(item_id), int(spec_id) if spec_id is not None else None)
        if cache_key in buffer_days_cache:
            return buffer_days_cache[cache_key]
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
        buffer_days_cache[cache_key] = max(0, buffer_val)
        return buffer_days_cache[cache_key]

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
    # A queued branch carries both the selected specification and its own
    # ancestry.  An item may be reached in the same BFS level through two
    # explicitly pinned specifications; those are two legitimate material
    # structures, not an ambiguity.  Keeping the ancestry on the branch also
    # makes the cycle guard genuinely per-path (rather than accidentally
    # combining ancestors from convergent paths).
    #
    # The first two fields are the logical demand-branch key requested by the
    # BOM contract: (item_id, spec_id).  The frozen ancestor set is only the
    # traversal context for that branch.
    DemandBranch = Tuple[int, int | None, frozenset[int]]
    explosion_warnings: List[Dict[str, Any]] = []
    reported_cycle_edges: Set[Tuple[int, int]] = set()
    reported_negative_stock_items: Set[int] = set()

    # Level 0: demand from plan lines
    demand_map: Dict[DemandBranch, Dict[date, float]] = {
        (int(iid), default_spec_map.get(int(iid)), frozenset()): dict(buckets)
        for iid, buckets in plan_demands.items()
    }
    traced_spec_scopes: Set[Tuple[int, int]] = set()

    MAX_BOM_DEPTH = 20
    for depth in range(MAX_BOM_DEPTH):
        if not demand_map:
            break

        next_demand: Dict[DemandBranch, Dict[date, float]] = {}

        for (iid, spec_id, own_ancestors), buckets in sorted(
            demand_map.items(),
            key=lambda row: (
                row[0][0],
                -1 if row[0][1] is None else row[0][1],
                tuple(sorted(row[0][2])),
            ),
        ):
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
            stock_left = float(avail_stock.get(iid, 0.0) or 0.0)
            if stock_left < 0.0:
                # A negative physical balance is evidence of an inventory
                # discrepancy, not anti-stock that may be added to a new
                # obligation.  Preserve the physical S0 in the freeze baseline
                # (shared_pools.stock_initial), but expose zero units to the
                # consume-once allocation pool and make the discrepancy
                # independently visible on the frozen run.
                if iid not in reported_negative_stock_items:
                    explosion_warnings.append(
                        {
                            "code": "NEGATIVE_PHYSICAL_STOCK_UNAVAILABLE",
                            "item_id": iid,
                            "physical_stock_qty": stock_left,
                            "allocatable_stock_qty": 0.0,
                        }
                    )
                    reported_negative_stock_items.add(iid)
                stock_left = 0.0
                avail_stock[iid] = 0.0
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

            if not spec_id:
                continue  # Leaf item (purchased material or item without BOM)

            comps = components_by_spec.get(int(spec_id), [])
            if not comps:
                continue
            if (
                shared_pools is not None
                and trace is not None
                and (iid, int(spec_id)) not in traced_spec_scopes
            ):
                traced_spec_scopes.add((iid, int(spec_id)))
                for component in comps:
                    trace.component_norms.append(
                        (
                            int(iid),
                            int(component.item_id),
                            int(spec_id),
                            float(component.quantity or 0.0),
                        )
                    )

            # Explode demand that on-hand stock does NOT cover (after-stock,
            # NOT after-stock-and-WIP): an open parent order still needs its
            # components produced, so WIP must not suppress the explosion or
            # lower BOM levels silently stay in deficit with no orders.
            if not explode_buckets:
                continue  # Nothing to propagate

            child_ancestors = own_ancestors | {iid}
            for bucket_date, exp_q in explode_buckets:
                for comp in comps:
                    try:
                        child_id = int(comp.item_id)
                        per_unit = float(comp.quantity or 0.0)
                    except Exception:
                        continue
                    if per_unit <= 1e-12 or exp_q <= 1e-9:
                        continue
                    if child_id in child_ancestors:
                        # A genuinely cyclic BOM. The demand on this edge cannot
                        # be planned, so say so instead of absorbing it.
                        if (iid, child_id) not in reported_cycle_edges:
                            reported_cycle_edges.add((iid, child_id))
                            explosion_warnings.append(make_warning(
                                "BOM_CYCLE_EDGE_SKIPPED",
                                msg=(
                                    "BOM-цикл: компонент уже является предком "
                                    "своего узла, ветка не разворачивается"
                                ),
                                parent_item_id=int(iid),
                                child_item_id=int(child_id),
                                bom_level=int(depth),
                            ))
                        continue
                    child_qty = exp_q * per_unit
                    child_spec_id = spec_resolver.child_spec_id(comp)
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
                    buf = resolve_buffer_days(iid, spec_id)
                    child_date = (bucket_date - timedelta(days=buf)) if buf > 0 else bucket_date
                    child_date = clamp_to_floor(child_date)
                    child_branch: DemandBranch = (
                        child_id,
                        child_spec_id,
                        frozenset(child_ancestors),
                    )
                    next_demand.setdefault(child_branch, {})[child_date] = (
                        next_demand.setdefault(child_branch, {}).get(child_date, 0.0)
                        + child_qty
                    )

        demand_map = next_demand

    if any(
        float(qty or 0.0) > 1e-9
        for buckets in demand_map.values()
        for qty in buckets.values()
    ):
        # Fail closed: the residual demand belongs to real BOM levels that would
        # never be planned. Silently dropping it is exactly the "лишиться
        # невыполненной потребности" the canon forbids.
        raise ValueError(
            "Развёртка BOM превысила предел вложенности "
            f"({MAX_BOM_DEPTH}): потребность нижних уровней осталась "
            "неразвёрнутой для номенклатур "
            f"{sorted({item_id for item_id, _spec_id, _ancestors in demand_map})[:10]}"
        )

    return gross_map, net_map, bom_level_map, explosion_warnings


def create_mrp_snapshot_from_period_plan(
    db: Session,
    plan_id: int,
    *,
    generation_key: str,
    started_by: Optional[str] = None,
    allow_stale_parent: bool = False,
) -> Dict[str, Any]:
    """Publish this fixed plan through the atomic Ledger obligation refresh.

    Strict entry point: ``generation_key`` must be pinned by the caller.  The
    canonical UI path is :func:`fix_period_plan`; use
    :func:`create_mrp_snapshot_for_plan` when the key should be server-owned.

    Transaction ownership deliberately remains with the caller.  This service
    neither commits nor rolls back, so a failed refresh cannot expose a partial
    candidate generation.
    """
    key = str(generation_key or "").strip()
    if not key:
        raise ValueError("generation_key is required")
    # The existing-snapshot probe below and the fork it guards must be one
    # atomic decision, so the publication lock is taken here rather than deep
    # inside ``run_obligation_refresh``.
    _lock_mrp_ledger(db)
    plan = _get_plan(db, int(plan_id))
    if plan.status != "fixed":
        raise ValueError("MRP-снимок можно создать только из зафиксированного плана")
    if not db.query(ProductionPlanLine.id).filter(
        ProductionPlanLine.plan_id == int(plan.id),
        ProductionPlanLine.qty > 0,
    ).first():
        raise ValueError("В плане нет положительной потребности для MRP")
    parent = _current_accepted_generation(db)
    try:
        cfg_id, cfg = get_active_planning_config(db)
    except Exception:
        cfg_id, cfg = None, dict(DEFAULT_PLANNING_CONFIG)
    current_run: PlanningRun | None = None
    for current in (
        db.query(PlanningRun)
        .filter(
            PlanningRun.source_plan_id == int(plan.id),
            PlanningRun.status == "FIXED_SNAPSHOT",
        )
        .all()
    ):
        resolved_parent_generation_id = _resolve_parent_generation_id(db, current)
        if resolved_parent_generation_id == int(parent.id):
            if current_run is not None:
                raise ValueError(
                    "План имеет несколько текущих зафиксированных MRP-снимков"
                )
            current_run = current
    if current_run is not None:
        # A fixed plan is an immutable obligation.  Re-opening its snapshot must
        # never fork a generation, re-explode the BOM or net against today's
        # stock.  Fact refreshes rebuild generation-scoped reservations and
        # execution snapshots around this same run.
        return {
            "status": "ok",
            "generation_key": key,
            "ledger_generation_id": int(parent.id),
            "run_id": int(current_run.run_id),
            "published": False,
            "immutable": True,
        }

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
        allow_stale_parent=bool(allow_stale_parent),
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


def create_mrp_snapshot_for_plan(
    db: Session,
    plan_id: int,
    *,
    generation_key: Optional[str] = None,
    started_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish the plan's MRP snapshot with a server-owned refresh key.

    The refresh key is infrastructure, not user input: when the caller does not
    pin one it is derived deterministically from ``(plan, current accepted
    generation)``, exactly like the close path.  This is what makes the HTTP
    contract keyless — the client cannot invent, collide with or replay a
    generation key.

    Used by the atomic :func:`fix_period_plan` and by the compatibility
    ``POST /period-plans/{id}/mrp-snapshot`` recovery route.  Repeating it for a
    plan that already owns a snapshot in the current truth is idempotent: the
    existing run is returned and nothing is forked.

    Transaction ownership stays with the caller (no commit / no rollback here).
    """
    # Resolve the key under the publication lock: the parent generation it is
    # derived from must be the same one the refresh will fork.
    _lock_mrp_ledger(db)
    key = str(generation_key or "").strip()
    if not key:
        parent = _current_accepted_generation(db)
        key = _fix_refresh_generation_key(
            plan_id=int(plan_id), parent_generation_id=int(parent.id),
        )
    return create_mrp_snapshot_from_period_plan(
        db, int(plan_id), generation_key=key, started_by=started_by,
    )


def _has_mrp_result_snapshot(db: Session, run_id: int, generation_id: Optional[int]) -> bool:
    # Lazy: ``mrp_result_snapshot`` owns both the consumer name and the key
    # spelling, and importing it at module scope would close an import cycle.
    from .mrp_result_snapshot import CONSUMER as MRP_RESULT_CONSUMER, _snapshot_key

    query = db.query(PlanningReadSnapshot.id).filter(
        PlanningReadSnapshot.consumer == MRP_RESULT_CONSUMER,
        PlanningReadSnapshot.snapshot_key == _snapshot_key(int(run_id)),
    )
    if generation_id is not None:
        query = query.filter(
            PlanningReadSnapshot.ledger_generation_id == int(generation_id)
        )
    return query.first() is not None


def repair_duplicate_plan_snapshots(
    db: Session,
    plan_id: int,
    *,
    repaired_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Admin repair for a plan poisoned with more than one FIXED_SNAPSHOT run.

    Before the publication lock was taken in :func:`_lock_mrp_ledger`, two
    concurrent fixations of the same plan could both observe «no snapshot yet»,
    both fork a generation and both publish.  The plan is then permanently
    unusable: :func:`create_mrp_snapshot_from_period_plan` refuses it with «План
    имеет несколько текущих зафиксированных MRP-снимков», and the
    ``uq_planning_run_fixed_snapshot_source_plan`` migration cannot even be
    applied to such a database.  The race itself is closed; this is the cleanup
    for rows it already produced.

    The surviving run is chosen deterministically, never by wall-clock luck:
    a published MRP result snapshot in the current accepted generation wins over
    one in any generation, which wins over one with no snapshot at all, and the
    highest ``run_id`` breaks every remaining tie.  Losers become
    ``SUPERSEDED`` — the same status the obligation-refresh publisher gives a
    parent run it replaced; no new lifecycle state is invented.  Plan lines
    still locked by a loser are re-pointed at the survivor so the plan keeps one
    coherent lock owner.

    Transaction ownership stays with the caller (no commit / no rollback here).
    """
    _lock_mrp_ledger(db)
    plan = _get_plan(db, int(plan_id))
    parent = _current_accepted_generation(db)
    runs = (
        db.query(PlanningRun)
        .filter(
            PlanningRun.source_plan_id == int(plan.id),
            PlanningRun.status == "FIXED_SNAPSHOT",
        )
        .order_by(PlanningRun.run_id.asc())
        .all()
    )
    current_run_ids = {
        int(run.run_id)
        for run in runs
        if _resolve_parent_generation_id(
            db, run, current_generation_id=int(parent.id)
        ) == int(parent.id)
    }
    if len(runs) <= 1:
        return {
            "status": "ok",
            "plan_id": int(plan.id),
            "repaired": False,
            "survivor_run_id": int(runs[0].run_id) if runs else None,
            "superseded_run_ids": [],
            "current_generation_id": int(parent.id),
        }

    # Prefer a run that is bound to the current truth at all: a FIXED_SNAPSHOT
    # left behind by an older generation must never outrank a live one.
    candidates = [run for run in runs if int(run.run_id) in current_run_ids] or runs

    def _rank(run: PlanningRun) -> tuple[int, int, int]:
        return (
            int(_has_mrp_result_snapshot(db, int(run.run_id), int(parent.id))),
            int(_has_mrp_result_snapshot(db, int(run.run_id), None)),
            int(run.run_id),
        )

    survivor = max(candidates, key=_rank)
    superseded: List[int] = []
    for run in runs:
        if int(run.run_id) == int(survivor.run_id):
            continue
        run.status = "SUPERSEDED"
        run.pinned = False
        superseded.append(int(run.run_id))
    db.query(ProductionPlanLine).filter(
        ProductionPlanLine.plan_id == int(plan.id),
        ProductionPlanLine.locked_by_run_id.in_(superseded),
    ).update({"locked_by_run_id": int(survivor.run_id)}, synchronize_session=False)
    db.flush()
    return {
        "status": "ok",
        "plan_id": int(plan.id),
        "repaired": True,
        "survivor_run_id": int(survivor.run_id),
        "superseded_run_ids": superseded,
        "current_generation_id": int(parent.id),
        "repaired_by": str(repaired_by) if repaired_by else None,
    }


def _freeze_one_run(
    db: Session,
    run: PlanningRun,
    plan: ProductionPlanHeader,
    *,
    shared_pools: "FreezeSharedPools",
    trace: "FreezeTrace",
    now: datetime,
    new_version: int,
    cutoff_date: date,
    is_include: bool = True,
    manage_plan_locks: bool = True,
) -> Dict[str, Any]:
    """Freeze ONE active run against the shared queue-wide pool (v2 /).

    The legacy single-snapshot body, extended so the BOM explosion consumes the
    shared pools once (``shared_pools``/``trace``); requirements are stamped with
    the new freeze version, pool key, zeroed drift and ``initial_snapshot_stock``;
    own already-exported PlannedPurchase survive the rebuild as self-coverage;
    and the freeze baseline/allocation/component tables are written. Requirement
    ids are preserved through the ``(run_id,item_id)`` upsert. No commit here —
    the orchestrator owns the transaction.

    ``cutoff_date`` is the generation cutoff and the ONLY "now" this freeze may
    use. Wall-clock ``date.today()`` made a rebuild of the same generation on a
    later day produce different need/order dates, breaking planning-truth
    invariants 4-5 (one cutoff for all projections, idempotent reprocessing).
    """
    from .mrp_freeze import (
        LedgerPoolUnavailable,
        pool_key_for,
        _write_freeze_baseline,
        _write_freeze_allocation,
        _write_freeze_component,
        _write_freeze_component_cumulative,
    )

    lines = (
        db.query(ProductionPlanLine)
        .filter(ProductionPlanLine.plan_id == int(plan.id))
        .filter(ProductionPlanLine.qty > 0)
        .order_by(ProductionPlanLine.item_id.asc(), ProductionPlanLine.bucket_date.asc())
        .all()
    )

    run_roots = {
        int(root.plan_line_id): root
        for root in db.query(MrpRunRoot)
        .filter(MrpRunRoot.run_id == int(run.run_id))
        .all()
    }
    if not run_roots:
        # First fixation: the run receives the immutable plan matrix. A
        # specification rebase pre-creates roots from the saved plan remainder
        # and therefore never enters this branch.
        for line in lines:
            planned = max(Decimal(str(line.qty or 0)), Decimal("0"))
            if line.remaining_output_qty is None:
                line.accepted_output_qty = max(
                    Decimal(str(line.accepted_output_qty or 0)), Decimal("0")
                )
                line.remaining_output_qty = max(
                    planned - Decimal(str(line.accepted_output_qty or 0)), Decimal("0")
                )
            root = MrpRunRoot(
                run_id=int(run.run_id),
                plan_line_id=int(line.id),
                planned_qty=planned,
                accepted_qty=Decimal("0"),
                remaining_qty=planned,
            )
            db.add(root)
            run_roots[int(line.id)] = root
        db.flush()

    # Candidate fixation is add-only.  An existing requirement means this run
    # was already derived, and rebuilding it would mutate frozen obligations
    # and could delete purchase proposals without confirmed 1C read-back.
    existing_req_by_item: Dict[int, MrpRequirement] = {
        int(req.item_id): req
        for req in db.query(MrpRequirement).filter(MrpRequirement.run_id == int(run.run_id)).all()
    }
    if existing_req_by_item:
        raise LedgerPoolUnavailable(
            "candidate freeze is add-only; run already has derived requirements"
        )

    # --- Collect plan-level (level 0) demand and lock plan lines ---
    buckets_by_item: Dict[int, Dict[date, float]] = {}
    for line in lines:
        root = run_roots.get(int(line.id))
        if root is None:
            continue
        item_id = int(line.item_id)
        line_qty = _to_float(root.planned_qty)
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
    gross_map, net_map, bom_level_map, explosion_warnings = _explode_bom_net_first(
        db, buckets_by_item, shared_pools, trace, need_date_floor=cutoff_date
    )
    if explosion_warnings:
        # `planning_run_candidate` seeds `warnings` as an empty dict; every
        # reader treats the column as a list. Normalise before appending.
        existing_warnings = run.warnings
        run.warnings = (
            list(existing_warnings) if isinstance(existing_warnings, list) else []
        ) + explosion_warnings

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
        if total_net > total_gross + 1e-9:
            raise LedgerPoolUnavailable(
                "ledger_pool_unavailable: net requirement exceeds gross "
                f"for run={int(run.run_id)}, item={int(item_id)}: "
                f"net={total_net}, gross={total_gross}"
            )
        bom_lvl = bom_level_map.get(item_id, 0)

        pk = pool_key_for(int(item_id))
        req = existing_req_by_item.get(int(item_id))
        if req is None:
            req = MrpRequirement(
                run_id=int(run.run_id),
                item_id=int(item_id),
                total_required_qty=total_gross,
                net_required_qty=total_net,
                period_from=plan.period_from,
                period_to=plan.period_to,
                bom_level=bom_lvl,
            )
            db.add(req)
        else:
            req.total_required_qty = total_gross
            req.net_required_qty = total_net
            req.period_from = plan.period_from
            req.period_to = plan.period_to
            req.bom_level = bom_lvl
        # Freeze v2 stamps: version, zeroed drift, pool key, frozen stock alloc.
        req.freeze_version = int(new_version)
        req.characteristic_ref = pk.characteristic_ref
        req.organization_ref = pk.organization_ref
        req.planning_stock_pool = pk.planning_stock_pool
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
        req.period_from = plan.period_from
        req.period_to = plan.period_to
        # Dropped item: still re-stamp the freeze version (initial stock = 0).
        req.freeze_version = int(new_version)
        req.characteristic_ref = pk.characteristic_ref
        req.organization_ref = pk.organization_ref
        req.planning_stock_pool = pk.planning_stock_pool

    # --- Allocate PlannedOrder / PlannedPurchase / PlannedRework by replenishment flow ---
    allocatable_item_ids = [
        iid for iid, buckets in net_map.items()
        if any(float(q) > 1e-9 for q in buckets.values())
    ]
    purchase_count = 0
    rework_count = 0
    production_count = 0
    stage_count = 0
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
                        order_date = max(cutoff_date, need_date - timedelta(days=lead_time))
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

            elif flow == REPLENISHMENT_FLOW_PRODUCTION:
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
                        demand_ref=_demand_ref(req_id) if req_id else None,
                        demand_date=bucket_date,
                        ledger_generation_id=int(run.ledger_generation_id),
                    )
                    db.add(order)
                    created_production_orders.append(order)
                    production_count += 1
                    alloc_total_qty += net_qty
            elif flow == REPLENISHMENT_FLOW_REWORK:
                # Preserve the frozen MRP requirement, but do not invent a
                # production, purchase, or legacy rework executor.  The
                # reservation materializer records it as unavailable and an
                # accepted assembly receipt may still close it.
                continue
            else:
                raise ValueError(
                    "Unsupported replenishment flow for period planning "
                    f"(item={int(iid)}, method={item.replenishment_method!r}, flow={flow})"
                )

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

    # --- Freeze v2 ledger writers (per-run, per-version, frozen_at=now) ---
    frozen_item_ids = sorted(
        int(iid)
        for iid, gross_buckets in gross_map.items()
        if sum(float(q) for q in gross_buckets.values()) > 1e-9
    )
    baseline_rows = _write_freeze_baseline(
        db,
        run,
        new_version,
        frozen_item_ids,
        shared_pools.stock_initial,
        now,
        baseline_at=shared_pools.baseline_at,
        physical_import_batch_id=shared_pools.physical_import_batch_id,
    )
    allocation_rows = _write_freeze_allocation(
        db, run, new_version, trace, req_by_item, shared_pools.stock_initial, now
    )
    component_rows = _write_freeze_component(db, run, new_version, trace, now)
    component_rows += _write_freeze_component_cumulative(db, run, new_version, trace)
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
        "schedule_warnings": 0,
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


def _read_period_plan_execution_payload_for_run(
    db: Session,
    *,
    plan: ProductionPlanHeader,
    run: PlanningRun,
    generation_id: Optional[int] = None,
) -> Dict[str, Any]:
    payload_generation_id = (
        int(generation_id) if generation_id is not None else int(run.ledger_generation_id)
        if run.ledger_generation_id is not None else None
    )
    if payload_generation_id is None:
        raise ValueError(f"run_id={int(run.run_id)}: execution snapshot generation is unknown")
    snapshot_key = _execution_snapshot_key(
        plan_id=plan.id,
        run_id=run.run_id,
        root_item_id=None,
        bom_level=None,
        flow=None,
    )
    snapshot = (
        db.query(PlanningReadSnapshot)
        .filter(
            PlanningReadSnapshot.consumer == "period_plan_execution",
            PlanningReadSnapshot.snapshot_key == snapshot_key,
            PlanningReadSnapshot.ledger_generation_id == payload_generation_id,
        )
        .one_or_none()
    )
    if snapshot is None:
        raise ValueError(
            f"run_id={int(run.run_id)}: execution snapshot is missing for the run"
        )
    return dict(snapshot.payload)


def _latest_closed_plan_snapshot(
    db: Session,
    *,
    plan_id: int,
    run_id: int,
) -> ClosedPlanSnapshot | None:
    return (
        db.query(ClosedPlanSnapshot)
        .filter(
            ClosedPlanSnapshot.plan_id == int(plan_id),
            ClosedPlanSnapshot.run_id == int(run_id),
        )
        .one_or_none()
    )


def close_fixed_plan(db: Session, run_id: int, *, dry_run: bool = False) -> Dict[str, Any]:
    """Canonical explicit close for a fixed planning run.

    Accepts only ``FIXED_SNAPSHOT`` → ``CLOSED`` and releases active run
    reservations so they are removed from active queue usage.
    This operation is close-only: no requirement status changes, no pruning and
    no recalculation. No reopen path exists by design.

    ``dry_run`` is a *full-fidelity* preview, not a cheap one: it executes the
    real ``run_obligation_refresh`` (generation fork + freeze + publish) and only
    then rolls the transaction back.  That is what makes it trustworthy — a
    dry run that succeeds proves the real close will succeed — but it costs a
    complete refresh and holds the global Ledger publication advisory lock for
    its whole duration.  A cheap preview would need the orchestrator to expose a
    validate-only mode; short of that, do NOT call ``dry_run`` on a hot path or
    from polling UI.  ``published_generation_id`` in a dry-run result names the
    forked generation that was rolled back, so it must not be persisted or shown
    as a real generation id.
    """
    run = (
        db.query(PlanningRun)
        .filter(PlanningRun.run_id == int(run_id))
        .with_for_update()
        .one_or_none()
    )
    if run is None:
        raise ValueError(f"run_id={run_id}: прогон не найден")
    if run.source_plan_id is None:
        raise ValueError(f"run_id={run_id}: run is not bound to a production plan")
    if str(run.status or "") not in {"FIXED_SNAPSHOT", "CLOSED"}:
        raise ValueError(
            f"run_id={run_id}: нельзя close "
            f"(status={run.status}, ожидался FIXED_SNAPSHOT)"
        )
    truth = db.get(PlanningTruthState, 1)
    if truth is None or truth.current_generation_id is None:
        raise ValueError("Current accepted planning truth is unavailable")
    parent_generation_id = int(truth.current_generation_id)
    parent_generation = db.get(LedgerGeneration, parent_generation_id)
    if parent_generation is None or str(parent_generation.status) != "accepted":
        raise ValueError("Current accepted planning truth is unavailable")
    plan = db.get(ProductionPlanHeader, int(run.source_plan_id))
    if plan is None:
        raise ValueError(f"run_id={run_id}: bound plan not found")

    existing_closed_snapshot = _latest_closed_plan_snapshot(
        db, plan_id=plan.id, run_id=run.run_id
    )

    if str(run.status or "") == "CLOSED":
        if existing_closed_snapshot is None:
            raise ValueError("closed plan snapshot is missing for this run")
        active_reservations = db.query(ReservationEntry.id).filter(
            ReservationEntry.run_id == int(run.run_id),
            ReservationEntry.ledger_generation_id == parent_generation_id,
            ReservationEntry.lifecycle_status == "active",
        ).count()
        if active_reservations:
            raise ValueError(
                "закрытый прогон всё ещё присутствует в текущем planning truth"
            )
        current_payload = _read_period_plan_execution_payload_for_run(
            db,
            plan=plan,
            run=run,
            generation_id=int(existing_closed_snapshot.ledger_generation_id),
        )
        if dict(existing_closed_snapshot.payload or {}) != current_payload:
            raise ValueError("closed plan snapshot payload mismatch for this run")
        if dry_run:
            db.rollback()
            return {
                "status": "already_closed",
                "run_id": int(run.run_id),
                "dry_run": bool(dry_run),
                "requirements_closed": 0,
                "reservations_released": 0,
                "purchases_pruned": [],
                "published_generation_id": int(existing_closed_snapshot.ledger_generation_id),
            }
        return {
            "status": "already_closed",
            "run_id": int(run.run_id),
            "dry_run": bool(dry_run),
            "requirements_closed": 0,
            "reservations_released": 0,
            "purchases_pruned": [],
            "published_generation_id": int(existing_closed_snapshot.ledger_generation_id),
        }

    resolved_parent_generation_id = _resolve_parent_generation_id(
        db, run, current_generation_id=parent_generation_id,
    )
    if resolved_parent_generation_id != parent_generation_id:
        raise ValueError(f"run_id={run_id}: not bound to the current accepted planning truth")

    payload_generation_id = (
        int(run.ledger_generation_id) if run.ledger_generation_id is not None else parent_generation_id
    )
    execution_payload = _read_period_plan_execution_payload_for_run(
        db,
        plan=plan,
        run=run,
        generation_id=payload_generation_id,
    )
    if existing_closed_snapshot is not None:
        # The run is still FIXED_SNAPSHOT here (the CLOSED branch returned
        # above), so this row is the residue of a closure whose worker died
        # *after* recording the snapshot and before the publication became
        # truth.  Answering «already_closed» would be a silent lie: the plan is
        # still open in the current planning truth.  Verify the recorded payload
        # is still the one this run produces and then resume the refresh; the
        # existing row is reused rather than duplicated.
        if dict(existing_closed_snapshot.payload or {}) != execution_payload:
            raise ValueError("closed plan snapshot payload mismatch for this run")

    execution_generation = db.get(LedgerGeneration, payload_generation_id)
    if execution_generation is None:
        raise ValueError("run has no execution generation lineage")
    if execution_generation.cutoff is None:
        raise ValueError("run execution cutoff is unavailable")

    if not dry_run and existing_closed_snapshot is None:
        db.add(ClosedPlanSnapshot(
            plan_id=plan.id,
            run_id=run.run_id,
            ledger_generation_id=payload_generation_id,
            cutoff=execution_generation.cutoff,
            payload=execution_payload,
            closed_at=datetime.now(timezone.utc),
        ))

    from .obligation_refresh_orchestrator import run_obligation_refresh

    generation_key = _close_refresh_generation_key(
        run_id=int(run.run_id),
        parent_generation_id=parent_generation_id,
    )
    try:
        report = run_obligation_refresh(
            db,
            parent_generation_id=parent_generation_id,
            generation_key=generation_key,
            add_plan_ids=(),
            retire_plan_ids=(int(plan.id),),
            started_by=f"close_fixed_plan:{int(run.run_id)}",
        )
        if dry_run:
            db.rollback()
            return {
                "status": "closed",
                "run_id": int(run_id),
                "dry_run": bool(dry_run),
                "requirements_closed": 0,
                "reservations_released": 0,
                "purchases_pruned": [],
                "published_generation_id": int(report.target_generation_id),
                "published": bool(report.published),
            }
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(run)
    db.refresh(plan)
    if str(run.status) != "CLOSED" or str(plan.status) != "closed":
        raise ValueError("публикация закрытия не завершила lifecycle плана")
    return {
        "status": "closed",
        "run_id": int(run_id),
        "dry_run": bool(dry_run),
        "requirements_closed": 0,
        "reservations_released": 0,
        "purchases_pruned": [],
        "published_generation_id": int(report.target_generation_id),
        "published": bool(report.published),
    }


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
    return BomSpecificationResolver(db).descendant_ids_by_root(roots)


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
    include_forecasts = str(plan.status).lower() == "draft"

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

    forecast_by_cell = (
        _plan_matrix_forecasts(db, int(plan.id), [int(row.item_id) for row in rows], bucket_keys)
        if include_forecasts else {}
    )
    bucket_totals: Dict[str, float] = {key: 0.0 for key in bucket_keys}

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
        bucket_totals[key] += q

    grand_total = 0.0
    for bucket_key in bucket_keys:
        grand_total += bucket_totals[bucket_key]

    return {
        "plan": _serialize_plan(plan),
        "buckets": bucket_keys,
        "rows": list(by_item.values()),
        "bucket_totals": bucket_totals,
        "grand_total": grand_total,
        "total_qty": grand_total,
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
    # Canonical snapshot is keyed by plan+run only; request-level filters are
    # applied by the read path so one immutable snapshot serves all variants.
    return f"plan={int(plan_id)};run={int(run_id)}"


def _resolve_execution_run(db: Session, plan: ProductionPlanHeader, run_id: Optional[int]) -> PlanningRun:
    if run_id is not None:
        run = db.query(PlanningRun).filter(PlanningRun.run_id == int(run_id)).first()
        if (
            not run
            or int(run.source_plan_id or -1) != int(plan.id)
            or str(run.status or "") not in {"FIXED_SNAPSHOT", "CLOSED"}
        ):
            raise ValueError("Run not found for this plan")
        return run
    run = (
        db.query(PlanningRun)
        .filter(
            PlanningRun.source_plan_id == int(plan.id),
            PlanningRun.status.in_(("FIXED_SNAPSHOT", "CLOSED")),
        )
        .order_by(PlanningRun.run_id.desc())
        .first()
    )
    if not run:
        raise ValueError("No FIXED_SNAPSHOT run found for this plan")
    return run


def _attach_run_output_summary(
    db: Session,
    payload: Dict[str, Any],
    run: PlanningRun,
) -> Dict[str, Any]:
    """Overlay the persisted root-output counter for exactly one MRP run."""
    planned, accepted = (
        db.query(
            func.coalesce(func.sum(MrpRunRoot.planned_qty), 0),
            func.coalesce(func.sum(MrpRunRoot.accepted_qty), 0),
        )
        .filter(MrpRunRoot.run_id == int(run.run_id))
        .one()
    )
    planned_qty = max(_to_float(planned), 0.0)
    accepted_qty = min(max(_to_float(accepted), 0.0), planned_qty)
    result = dict(payload)
    summary = dict(result.get("summary") or {})
    summary.update({
        "root_output_completed_qty": accepted_qty,
        "root_output_base_qty": planned_qty,
        "root_output_pct": _rounded_replenishment_pct(planned_qty, accepted_qty),
        "execution_completed_qty": accepted_qty,
        "execution_base_qty": planned_qty,
        "execution_pct": _rounded_replenishment_pct(planned_qty, accepted_qty),
        "execution_partial": False,
    })
    result["summary"] = summary
    return result


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
    # Fail closed without consulting mutable requirements, items, or BOM.
    # A public GET may expose only the persisted execution snapshot; when it is
    # absent there are deliberately no quantitative rows to filter or page.
    rows: List[Dict[str, Any]] = []
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
            "total_items": 0,
            "execution_completed_qty": None,
            "execution_base_qty": None,
            "execution_pct": None,
            "execution_by_flow": None,
        },
    }


def _generation_truth_status(generation: Optional[LedgerGeneration]) -> str:
    """Truth status of a generation, read from the generation itself.

    A candidate is ``building`` until its own publish transaction flips it, and
    the persisted read-snapshot payload is sealed byte-for-byte at build time —
    so the payload keeps the published spelling. This helper exists for the
    build metadata, which must never claim ``accepted`` for a generation that
    was never accepted.
    """
    if generation is None:
        return "unavailable"
    status = str(getattr(generation, "status", "") or "")
    if status == "accepted":
        return "accepted"
    if status == "building":
        return "building"
    return "unavailable"


def _iso_datetime(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value) if value else None


def _filter_execution_rows(
    _db: Session,
    rows: List[Dict[str, Any]],
    *,
    root_item_id: Optional[int],
    bom_level: Optional[int],
    flow: Optional[str],
) -> List[Dict[str, Any]]:
    if root_item_id is None and bom_level is None and flow is None:
        return rows
    filtered: List[Dict[str, Any]] = []
    for row in rows:
        root_members = {
            int(value)
            for value in (row.get("root_item_ids") or [])
        }
        if (
            root_item_id is not None
            and int(root_item_id) not in root_members
            and int(row.get("item_id", 0)) != int(root_item_id)
        ):
            continue
        if bom_level is not None and int(row.get("bom_level", 0)) != int(bom_level):
            continue
        if flow is not None and row.get("flow") != flow:
            continue
        filtered.append(dict(row))
    return filtered


_EXECUTION_STATUS_LABELS = {
    "execution_unavailable": "Исполнение недоступно",
    "covered": "Закрыто",
    "partial": "Частично",
    "ordered": "Оформлено",
    "none": "Не оформлено",
    "net_zero": "Покрыто складом",
}
_EXECUTION_INCOMPLETE_STATUSES = {"partial", "ordered", "none"}
_EXECUTION_SORT_FIELDS = {
    "bom_level",
    "item_article",
    "item_code",
    "item_name",
    "flow",
    "gross_qty",
    "net_qty",
    "ordered_qty",
    "completed_qty",
    "remaining_qty",
    "need_date",
    "coverage_pct",
    "status",
}


def _query_execution_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    status: Optional[str],
    include_net_zero: bool,
    sort_by: str,
    sort_dir: str,
    limit: int,
    offset: int,
) -> tuple[List[Dict[str, Any]], int, List[Dict[str, Any]]]:
    status_filter = str(status or "").strip().lower()
    allowed_statuses = set(_EXECUTION_STATUS_LABELS) | {"incomplete"}
    if status_filter and status_filter not in allowed_statuses:
        raise ValueError(f"Unsupported execution journal status: {status_filter}")
    sort_field = str(sort_by or "bom_level").strip().lower()
    if sort_field not in _EXECUTION_SORT_FIELDS:
        raise ValueError(f"Unsupported execution journal sort field: {sort_field}")
    direction = str(sort_dir or "asc").strip().lower()
    if direction not in {"asc", "desc"}:
        raise ValueError(f"Unsupported execution journal sort direction: {direction}")
    page_limit = max(1, min(int(limit or 100), 500))
    page_offset = max(0, int(offset or 0))

    prepared: List[Dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        row_status = str(row.get("status") or "execution_unavailable")
        row["status"] = row_status
        row["status_label"] = _EXECUTION_STATUS_LABELS.get(
            row_status, row_status
        )
        if not include_net_zero and row_status == "net_zero":
            continue
        if status_filter == "incomplete" and row_status not in _EXECUTION_INCOMPLETE_STATUSES:
            continue
        if status_filter and status_filter != "incomplete" and row_status != status_filter:
            continue
        prepared.append(row)

    def sort_value(row: Dict[str, Any]) -> tuple[bool, Any]:
        if sort_field == "status":
            value: Any = str(row.get("status_label") or "").casefold()
        else:
            value = row.get(sort_field)
            if isinstance(value, str):
                value = value.casefold()
        return value is None, value

    prepared.sort(
        key=lambda row: (
            sort_value(row),
            int(row.get("bom_level") or 0),
            int(row.get("item_id") or 0),
            int(row.get("req_id") or 0),
        ),
        reverse=direction == "desc",
    )
    total = len(prepared)
    return prepared[page_offset:page_offset + page_limit], total, prepared


def _finalize_execution_payload(
    db: Session,
    payload: Dict[str, Any],
    *,
    root_item_id: Optional[int],
    bom_level: Optional[int],
    flow: Optional[str],
    status: Optional[str],
    include_net_zero: bool,
    sort_by: str,
    sort_dir: str,
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    result = dict(payload)
    facet_rows = _filter_execution_rows(
        db,
        list(result.get("rows") or []),
        root_item_id=root_item_id,
        bom_level=None,
        flow=flow,
    )
    result["facets"] = {
        "bom_levels": sorted({
            int(row.get("bom_level") or 0) for row in facet_rows
        }),
    }
    filtered = _filter_execution_rows(
        db,
        facet_rows,
        root_item_id=None,
        bom_level=bom_level,
        flow=None,
    )
    filtered = _attach_execution_information_links(filtered)
    page_rows, total, summary_rows = _query_execution_rows(
        filtered,
        status=status,
        include_net_zero=include_net_zero,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )
    summary_payload = dict(result.get("summary") or {})
    if str(result.get("truth_status") or "") == "accepted":
        summary_payload.update(_execution_row_summary(summary_rows))
        if "root_output_base_qty" in summary_payload:
            summary_payload.update({
                "execution_completed_qty": summary_payload["root_output_completed_qty"],
                "execution_base_qty": summary_payload["root_output_base_qty"],
                "execution_pct": summary_payload["root_output_pct"],
                "execution_partial": False,
            })
            summary_payload.pop("root_output_completed_qty", None)
            summary_payload.pop("root_output_base_qty", None)
            summary_payload.pop("root_output_pct", None)
    else:
        # Unknown execution stays unknown. Filtering may change the row count,
        # but must never turn unavailable quantities into numeric zeroes.
        summary_payload["total_items"] = total
    result.update({
        "rows": page_rows,
        "total": total,
        "limit": max(1, min(int(limit or 100), 500)),
        "offset": max(0, int(offset or 0)),
        "summary": summary_payload,
    })
    return result


def _execution_row_summary(
    rows: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    total_items = 0
    execution_completed_qty = 0.0
    execution_base_qty = 0.0
    execution_total_base_qty = 0.0
    execution_by_flow: Dict[str, Dict[str, float]] = {}
    execution_has_unavailable = False
    fully_covered = 0
    partially_covered = 0
    not_covered = 0
    net_zero = 0
    for row in rows:
        total_items += 1
        execution_available = bool(row.get("execution_available", True))
        completed_qty = _to_float(row.get("completed_qty"))
        base_qty = _to_float(row.get("progress_base_qty", row.get("net_qty")))
        item_flow = str(row.get("flow") or "")
        execution_total_base_qty += base_qty
        if execution_available:
            execution_completed_qty += completed_qty
            execution_base_qty += base_qty
        flow_summary = execution_by_flow.setdefault(
            item_flow,
            {
                "completed_qty": 0.0,
                "base_qty": 0.0,
                "total_base_qty": 0.0,
                "purchase_covered_qty": 0.0,
                "purchase_to_order_qty": 0.0,
                "available": True,
            },
        )
        flow_summary["total_base_qty"] += base_qty
        flow_summary["purchase_covered_qty"] += _to_float(
            row.get("purchase_covered_qty")
        )
        flow_summary["purchase_to_order_qty"] += _to_float(
            row.get("purchase_to_order_qty")
        )
        if execution_available:
            flow_summary["completed_qty"] += completed_qty
            flow_summary["base_qty"] += base_qty
        else:
            execution_has_unavailable = True
            flow_summary["available"] = False
        status = str(row.get("status") or "")
        if status == "net_zero":
            net_zero += 1
        elif status == "covered":
            fully_covered += 1
        elif status == "partial":
            partially_covered += 1
        else:
            not_covered += 1
    # A percentage of nothing is not 100% — it is undefined. The canonical
    # `replenishment_execution_pct` returns None for a zero base; the plan-level
    # aggregates must not invent a full bar for an empty or fully stock-covered
    # selection (the UI renders null as «недоступно»).
    execution_confirmed_pct = _rounded_replenishment_pct(
        execution_total_base_qty,
        execution_completed_qty,
    )
    execution_pct = (
        None
        if execution_has_unavailable
        else (
            _rounded_replenishment_pct(execution_base_qty, execution_completed_qty)
        )
    )
    for details in execution_by_flow.values():
        if not bool(details.get("available", True)):
            total_base_qty = _to_float(details.get("total_base_qty"))
            details["confirmed_pct"] = _rounded_replenishment_pct(
                total_base_qty,
                _to_float(details.get("completed_qty")),
            )
            details["execution_pct"] = None
            continue
        base_qty = _to_float(details.get("base_qty"))
        details["execution_pct"] = _rounded_replenishment_pct(
            base_qty,
            _to_float(details.get("completed_qty")),
        )
        total_base_qty = _to_float(details.get("total_base_qty"))
        details["covered_pct"] = _rounded_replenishment_pct(
            total_base_qty,
            _to_float(details.get("purchase_covered_qty")),
        )
        details["to_order_pct"] = _rounded_replenishment_pct(
            total_base_qty,
            _to_float(details.get("purchase_to_order_qty")),
        )
    return {
        "truth_status": "accepted",
        "total_items": total_items,
        "execution_completed_qty": execution_completed_qty,
        "execution_base_qty": execution_total_base_qty,
        "execution_available_base_qty": execution_base_qty,
        "execution_pct": execution_pct,
        "execution_confirmed_pct": execution_confirmed_pct,
        "execution_partial": execution_has_unavailable,
        "execution_by_flow": execution_by_flow,
        "fully_covered": fully_covered,
        "partially_covered": partially_covered,
        "not_covered": not_covered,
        "net_zero": net_zero,
    }


def _attach_execution_information_links(
    rows: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for row in rows:
        row = dict(row)
        if not isinstance(row.get("information_links"), dict):
            row_item_id = int(row.get("item_id") or 0)
            row["information_links"] = {
                "reservation_events": [
                    {
                        "reservation_id": int(reservation_id),
                        "url": f"/api/v1/item-ledger/{row_item_id}/reservations/{int(reservation_id)}/events",
                    }
                    for reservation_id in sorted(set(int(rid) for rid in row.get("reservation_ids", []) if rid is not None))
                ],
            }
        execution_events = row.get("execution_events") or []
        if isinstance(execution_events, list):
            resolved_events = []
            row_item_id = int(row.get("item_id") or 0)
            for event in execution_events:
                event = dict(event)
                if (
                    isinstance(event, dict)
                    and int(event.get("reservation_id") or 0)
                    and "reservation_events_url" not in event
                ):
                    event["reservation_events_url"] = (
                        f"/api/v1/item-ledger/{row_item_id}/reservations/{int(event.get('reservation_id'))}/events"
                    )
                resolved_events.append(event)
            row["execution_events"] = resolved_events
        row["ledger_links"] = {
            "item_id": int(row.get("item_id") or 0),
            "reservation_ids": sorted({
                int(reservation_id)
                for reservation_id in row.get("reservation_ids", [])
                if reservation_id is not None
            }),
            "events": [
                {
                    "event_id": int(event.get("event_id")),
                    "reservation_id": int(event.get("reservation_id")),
                    "sle_id": event.get("stock_ledger_entry_id"),
                    "fact_ref": event.get("fact_ref"),
                    "fact_line_ref": event.get("fact_line_ref"),
                    "match_rule": event.get("match_rule"),
                }
                for event in row.get("execution_events", [])
                if event.get("event_id") is not None
                and event.get("reservation_id") is not None
                and str(event.get("event_kind") or "") == "realize"
            ],
        }
        enriched.append(row)
    return enriched


def _execution_obligation_links(
    db: Session,
    run: PlanningRun,
    *,
    requirement_ids: List[int],
    requirement_id_by_item: Dict[int, int],
) -> tuple[Dict[int, List[Dict[str, Any]]], Dict[int, float]]:
    """Capture navigational obligation links without consulting legacy facts."""
    links_by_requirement: Dict[int, List[Dict[str, Any]]] = {}
    ordered_by_requirement: Dict[int, float] = {}
    if not requirement_ids:
        return links_by_requirement, ordered_by_requirement

    actual_production_requirements: Set[int] = set()
    for product, order, state in (
        db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .filter(ProductionProduct.source_mrp_requirement_id.in_(requirement_ids))
        .all()
    ):
        requirement_id = int(product.source_mrp_requirement_id)
        if state is not None and str(state.status or "").casefold() == "cancelled":
            continue
        qty = _to_float(product.quantity)
        opened = bool(order.order_ref1c)
        actual_production_requirements.add(requirement_id)
        if opened:
            ordered_by_requirement[requirement_id] = (
                ordered_by_requirement.get(requirement_id, 0.0) + qty
            )
        links_by_requirement.setdefault(requirement_id, []).append({
            "type": "production_order",
            "product_id": int(product.product_id),
            "order_id": int(order.order_id),
            "order_number": str(order.order_number or ""),
            "one_c_opened": opened,
            "qty": qty,
            "completed_qty": None,
            "remaining_qty": None,
            "opened_at": _iso_datetime(state.opened_at) if state is not None else None,
            "order_state": str(order.order_state_name or order.order_state_key or ""),
        })

    # The writer stamps `mrp_requirement:<id>`; reading `req:<id>` matched
    # nothing, so every make requirement silently reported ordered_qty=0 and a
    # full unassigned_qty.  Read both spellings, write only the canonical one.
    requirement_id_by_demand_ref = _demand_ref_lookup(requirement_ids)
    for planned in (
        db.query(PlannedOrder)
        .filter(
            PlannedOrder.run_id == int(run.run_id),
            PlannedOrder.demand_ref.in_(sorted(requirement_id_by_demand_ref)),
        )
        .all()
    ):
        requirement_id = requirement_id_by_demand_ref.get(str(planned.demand_ref or ""))
        if requirement_id is None:
            continue
        if requirement_id in actual_production_requirements:
            continue
        qty = _to_float(planned.qty)
        links_by_requirement.setdefault(requirement_id, []).append({
            "type": "planned_order",
            "order_id": int(planned.order_id),
            "qty": qty,
            "completed_qty": None,
            "remaining_qty": None,
            "need_date": _iso_datetime(planned.need_date),
            **_forecast_payload(
                planned.finish_date or planned.start_date or planned.need_date,
                planned.need_date,
            ),
        })

    purchases = (
        db.query(PlannedPurchase)
        .filter(
            PlannedPurchase.run_id == int(run.run_id),
            PlannedPurchase.item_id.in_(list(requirement_id_by_item)),
        )
        .all()
    )
    purchase_ids = [int(row.purchase_id) for row in purchases]
    sync_by_purchase: Dict[int, SyncLink] = {}
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
            sync_by_purchase[int(link.source_id)] = link
    supplier_refs = {
        str(link.target_ref_key or "").strip()
        for link in sync_by_purchase.values()
        if str(link.target_ref_key or "").strip()
    }
    supplier_by_ref = {
        str(order.order_ref1c or "").strip(): order
        for order in (
            db.query(SupplierOrder)
            .filter(SupplierOrder.order_ref1c.in_(supplier_refs))
            .all()
            if supplier_refs
            else []
        )
    }
    for purchase in purchases:
        requirement_id = (
            int(purchase.source_mrp_requirement_id)
            if purchase.source_mrp_requirement_id is not None
            else requirement_id_by_item.get(int(purchase.item_id))
        )
        if requirement_id is None:
            continue
        link = sync_by_purchase.get(int(purchase.purchase_id))
        supplier_ref = str(getattr(link, "target_ref_key", "") or "").strip()
        supplier_order = supplier_by_ref.get(supplier_ref)
        opened = bool(supplier_ref)
        qty = _to_float(purchase.qty)
        if opened:
            ordered_by_requirement[requirement_id] = (
                ordered_by_requirement.get(requirement_id, 0.0) + qty
            )
        links_by_requirement.setdefault(requirement_id, []).append({
            "type": "planned_purchase",
            "purchase_id": int(purchase.purchase_id),
            "qty": qty,
            "completed_qty": None,
            "remaining_qty": None,
            "need_date": _iso_datetime(purchase.need_date),
            "order_date": _iso_datetime(purchase.order_date),
            "lead_time_days": int(purchase.lead_time_days or 0),
            "order_ref1c": supplier_ref or None,
            "order_number": (
                str(supplier_order.order_number or "") if supplier_order else None
            ),
            "order_state": (
                str(
                    supplier_order.order_state_name
                    or supplier_order.order_state_key
                    or ""
                )
                if supplier_order
                else None
            ),
            "one_c_opened": opened,
            **_forecast_payload(purchase.need_date, purchase.need_date, reason="purchase"),
        })

    for rework in (
        db.query(PlannedRework)
        .filter(
            PlannedRework.run_id == int(run.run_id),
            PlannedRework.item_id.in_(list(requirement_id_by_item)),
        )
        .all()
    ):
        requirement_id = requirement_id_by_item.get(int(rework.item_id))
        if requirement_id is None:
            continue
        links_by_requirement.setdefault(requirement_id, []).append({
            "type": "planned_rework",
            "rework_id": int(rework.rework_id),
            "qty": _to_float(rework.qty),
            "completed_qty": None,
            "remaining_qty": None,
            "need_date": _iso_datetime(rework.need_date),
            "order_date": _iso_datetime(rework.order_date),
            "lead_time_days": int(rework.lead_time_days or 0),
            **_forecast_payload(rework.need_date, rework.need_date, reason="rework"),
        })

    return links_by_requirement, ordered_by_requirement


def _build_execution_snapshot_rows(
    db: Session,
    run: PlanningRun,
    *,
    requirement_ids: Iterable[int],
    items_by_requirement: Dict[int, Dict[str, Any]],
    generation_id: int,
    root_item_ids_by_item: Dict[int, List[int]],
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    req_ids = sorted({int(value) for value in requirement_ids})
    req_rows: List[Dict[str, Any]] = []
    if not req_ids:
        return req_rows, {
            "total_items": 0,
            "truth_status": _generation_truth_status(
                db.get(LedgerGeneration, int(generation_id))
            ),
        }

    item_ids = [int(req.get("item_id")) for req in items_by_requirement.values() if req.get("item_id") is not None]
    if item_ids:
        item_flow_by_id = {
            int(item.item_id): classify_replenishment_flow(getattr(item, "replenishment_method", None))
            for item in db.query(Item.item_id, Item.replenishment_method).filter(Item.item_id.in_(item_ids)).all()
        }
    else:
        item_flow_by_id = {}
    requirement_id_by_item = {
        int(payload["item_id"]): int(requirement_id)
        for requirement_id, payload in items_by_requirement.items()
    }
    work_items_by_requirement, ordered_by_requirement = _execution_obligation_links(
        db,
        run,
        requirement_ids=req_ids,
        requirement_id_by_item=requirement_id_by_item,
    )

    reservation_rows = (
        db.query(
            ReservationEntry.id,
                ReservationEntry.requirement_id,
                ReservationEntry.realization_mode,
                ReservationEntry.reserved_qty,
                ReservationEntry.replenishment_required_qty,
                ReservationEntry.replenishment_received_qty,
        )
        .filter(
            ReservationEntry.ledger_generation_id == int(generation_id),
            ReservationEntry.requirement_id.in_(req_ids),
        )
        .all()
    )
    buy_entries = (
        db.query(ReplenishmentWorkItem, ReservationEntry, Item)
        .join(
            ReservationEntry,
            ReservationEntry.id == ReplenishmentWorkItem.reservation_id,
        )
        .join(Item, Item.item_id == ReplenishmentWorkItem.item_id)
        .filter(
            ReplenishmentWorkItem.ledger_generation_id == int(generation_id),
            ReplenishmentWorkItem.replenishment_method == "buy",
            ReservationEntry.lifecycle_status == "active",
        )
        .all()
    )
    from .purchase_control_snapshot import open_supplier_coverage_by_reservation

    open_purchase_by_reservation, _open_purchase_slices = (
        open_supplier_coverage_by_reservation(
            db,
            int(generation_id),
            buy_entries,
        )
    )
    reservation_ids_by_req: Dict[int, List[int]] = {}
    realized_by_req_mode: Dict[tuple[int, str], float] = {}
    purchase_coverage_by_req: Dict[int, tuple[float, float]] = {}
    reservation_ids: List[int] = []
    for (
        row_id,
        req_id,
        realization_mode,
        _reserved_qty,
        replenishment_required_qty,
        replenishment_received_qty,
    ) in reservation_rows:
        rid = int(req_id)
        reservation_ids_by_req.setdefault(rid, []).append(int(row_id))
        reservation_ids.append(int(row_id))
        mode_key = (rid, str(realization_mode or ""))
        realized_by_req_mode[mode_key] = (
            realized_by_req_mode.get(mode_key, 0.0)
            + _to_float(replenishment_received_qty)
        )
        if str(realization_mode or "") == "buy":
            covered, to_order = purchase_coverage_by_req.get(rid, (0.0, 0.0))
            received = _to_float(replenishment_received_qty)
            remaining_after_receipts = float(
                replenishment_remaining(
                    replenishment_required_qty,
                    replenishment_received_qty,
                )
            )
            open_order_covered = min(
                open_purchase_by_reservation.get(int(row_id), 0.0),
                remaining_after_receipts,
            )
            purchase_coverage_by_req[rid] = (
                covered + received + open_order_covered,
                to_order + max(remaining_after_receipts - open_order_covered, 0.0),
            )

    events_by_requirement: Dict[int, List[Dict[str, Any]]] = {}
    if reservation_ids:
        for event, req_id in (
            db.query(ReservationEvent, ReservationEntry.requirement_id.label("requirement_id"))
            .join(ReservationEntry, ReservationEvent.reservation_id == ReservationEntry.id)
            .filter(ReservationEvent.ledger_generation_id == int(generation_id))
            .filter(ReservationEvent.reservation_id.in_(reservation_ids))
            .all()
        ):
            event_payload = {
                "event_id": int(event.id),
                "reservation_id": int(event.reservation_id),
                "reservation_event_kind": str(event.event_kind or ""),
                "realized_delta": _to_float(event.realized_delta),
                "reserved_delta": _to_float(event.reserved_delta),
                "fact_ref": str(event.fact_ref or ""),
                "fact_line_ref": str(event.fact_line_ref or ""),
                "match_rule": str(event.match_rule or ""),
                "event_at": _iso_datetime(event.event_at),
                "event_kind": str(event.event_kind or ""),
                "stock_ledger_entry_id": int(event.sle_id) if event.sle_id is not None else None,
            }
            events_by_requirement.setdefault(int(req_id), []).append(event_payload)

    for req_id in req_ids:
        req = items_by_requirement.get(int(req_id))
        if not req:
            continue
        item_id = int(req.get("item_id"))
        reservations = sorted(set(reservation_ids_by_req.get(req_id, [])))
        events = sorted(
            events_by_requirement.get(req_id, []),
            key=lambda payload: (
                str(payload.get("event_at") or ""),
                int(payload.get("event_id") or 0),
            ),
        )
        for event in events:
            event["reservation_events_url"] = f"/api/v1/item-ledger/{item_id}/reservations/{int(event.get('reservation_id') or 0)}/events"
        reservation_row = next(
            (
                row for row in reservation_rows
                if int(row[1]) == int(req_id)
            ),
            None,
        )
        flow = item_flow_by_id.get(item_id)
        # The reservation ledger of THIS generation is the only source of
        # execution for a requirement. When it holds no row, the fact is
        # unknown — reporting a zero (and netting it against the frozen
        # requirement as a legacy fallback) fabricates progress. Fail closed to
        # `unavailable`, exactly like the truth-level payload does.
        execution_available = (
            reservation_row is not None
            and flow in (REPLENISHMENT_FLOW_PURCHASE, REPLENISHMENT_FLOW_PRODUCTION)
        )
        execution_unavailable_reason = (
            None
            if execution_available
            else (
                "Поток replenishment этого требования не поддерживается"
                if flow not in (REPLENISHMENT_FLOW_PURCHASE, REPLENISHMENT_FLOW_PRODUCTION)
                else (
                    "Резерв этой потребности отсутствует в поколении "
                    f"{int(generation_id)}: факт выполнения неизвестен"
                )
            )
        )
        progress_base_qty = (
            _to_float(reservation_row[4])
            if reservation_row is not None
            else _to_float(req["net_required_qty"])
        )
        realization_mode = (
            "buy"
            if flow == REPLENISHMENT_FLOW_PURCHASE
            else "make"
        )
        completed_qty = round(
            realized_by_req_mode.get((int(req_id), realization_mode), 0.0),
            10,
        )
        execution_allocations = [
            {
                "event_id": int(event.get("event_id")),
                "reservation_id": int(event.get("reservation_id")),
                "stock_ledger_entry_id": event.get("stock_ledger_entry_id"),
                "qty": _to_float(event.get("realized_delta")),
                "fact_ref": event.get("fact_ref"),
                "fact_line_ref": event.get("fact_line_ref"),
                "match_rule": event.get("match_rule"),
                "event_at": event.get("event_at"),
            }
            for event in events
            if event.get("stock_ledger_entry_id") is not None
            and abs(_to_float(event.get("realized_delta"))) > 1e-9
        ]
        if not execution_available:
            req_rows.append({
                "req_id": int(req_id),
                "item_id": item_id,
                "item_code": str(req.get("item_code") or ""),
                "item_article": str(req.get("item_article") or "") if req.get("item_article") else None,
                "item_name": str(req.get("item_name") or ""),
                "flow": flow,
                "bom_level": int(req.get("bom_level") or 0),
                "gross_qty": _to_float(req.get("gross_required_qty")),
                "net_qty": _to_float(req.get("net_required_qty")),
                "progress_base_qty": progress_base_qty,
                "completed_qty": None,
                "execution_available": False,
                "execution_unavailable_reason": execution_unavailable_reason,
                "execution_source": None,
                "remaining_qty": None,
                "coverage_pct": None,
                "ordered_qty": ordered_by_requirement.get(req_id, 0.0),
                "purchase_covered_qty": 0.0,
                "purchase_to_order_qty": 0.0,
                "unassigned_qty": max(
                    0.0,
                    _to_float(req.get("net_required_qty"))
                    - ordered_by_requirement.get(req_id, 0.0),
                ),
                "root_item_ids": root_item_ids_by_item.get(item_id, []),
                "information_links": {"reservation_events": []},
                "status": "execution_unavailable",
                "reservation_ids": [],
                "execution_events": events,
                "execution_allocations": execution_allocations,
                "work_items": work_items_by_requirement.get(req_id, []),
            })
            continue
        if progress_base_qty > 1e-9:
            if completed_qty < -1e-9 or completed_qty > progress_base_qty + 1e-9:
                raise ValueError(
                    f"reservation fold exceeds frozen requirement {req_id}: "
                    f"realized={completed_qty}, frozen={progress_base_qty}"
                )
            if completed_qty < 1e-9 and reservations:
                status = "ordered"
            elif completed_qty < 1e-9:
                status = "none"
            elif completed_qty + 1e-9 >= progress_base_qty:
                status = "covered"
            else:
                status = "partial"
        else:
            if abs(completed_qty) > 1e-9:
                raise ValueError(
                    f"net-zero requirement {req_id} has realized quantity "
                    f"{completed_qty}"
                )
            status = "net_zero"
            completed_qty = 0.0
        remaining_qty = max(0.0, progress_base_qty - completed_qty)
        coverage_pct = _rounded_replenishment_pct(progress_base_qty, completed_qty)
        ordered_qty = ordered_by_requirement.get(req_id, 0.0)
        purchase_covered_qty, purchase_to_order_qty = (
            purchase_coverage_by_req.get(req_id, (0.0, 0.0))
            if flow == REPLENISHMENT_FLOW_PURCHASE
            else (0.0, 0.0)
        )
        req_rows.append({
            "req_id": int(req_id),
            "item_id": item_id,
            "item_code": str(req.get("item_code") or ""),
            "item_article": str(req.get("item_article") or "") if req.get("item_article") else None,
            "item_name": str(req.get("item_name") or ""),
            "flow": item_flow_by_id.get(item_id),
            "bom_level": int(req.get("bom_level") or 0),
            "gross_qty": _to_float(req.get("gross_required_qty")),
            "net_qty": _to_float(req.get("net_required_qty")),
            "progress_base_qty": progress_base_qty,
            "completed_qty": completed_qty,
            "execution_available": execution_available,
            "execution_unavailable_reason": execution_unavailable_reason,
            "execution_source": (
                "supplier_receipt_coverage"
                if flow == REPLENISHMENT_FLOW_PURCHASE
                else (
                    "reservation_realization"
                    if flow == REPLENISHMENT_FLOW_PRODUCTION
                    else None
                )
            ),
            "remaining_qty": remaining_qty,
            "coverage_pct": coverage_pct,
            "ordered_qty": ordered_qty,
            "purchase_covered_qty": purchase_covered_qty,
            "purchase_to_order_qty": purchase_to_order_qty,
            "unassigned_qty": max(
                0.0, _to_float(req.get("net_required_qty")) - ordered_qty
            ),
            "root_item_ids": root_item_ids_by_item.get(item_id, []),
            "information_links": {
                "reservation_events": [
                    {
                        "reservation_id": reservation_id,
                        "url": f"/api/v1/item-ledger/{item_id}/reservations/{reservation_id}/events",
                    }
                    for reservation_id in reservations
                ],
            },
            "status": status,
            "reservation_ids": reservations,
            "execution_events": events,
            # Real FIFO assignments of physical facts onto this requirement's
            # reservations, not a placeholder: every reservation event that
            # names a stock ledger entry is one allocation.
            "execution_allocations": execution_allocations,
            "work_items": work_items_by_requirement.get(req_id, []),
        })

    req_rows.sort(key=lambda row: (int(row.get("bom_level") or 0), int(row.get("item_id") or 0), str(row.get("item_code") or "")))
    generation = db.get(LedgerGeneration, int(generation_id))
    return req_rows, {
        "truth_status": _generation_truth_status(generation),
        "reservation_rows": len(reservation_rows),
        "allocation_rows": sum(
            len(row.get("execution_allocations") or []) for row in req_rows
        ),
        "execution_by_requirement": _execution_row_summary(req_rows),
    }


def get_period_plan_execution_journal(
    db: Session,
    plan_id: int,
    *,
    run_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    bom_level: Optional[int] = None,
    flow: Optional[str] = None,
    status: Optional[str] = None,
    include_net_zero: bool = True,
    sort_by: str = "bom_level",
    sort_dir: str = "asc",
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """Read the immutable execution snapshot. Never computes or publishes."""
    from .planning_truth import get_truth_state
    from .planning_truth import (
        CAPABILITY_EXECUTION_ALLOCATIONS,
        CAPABILITY_PHYSICAL_LEDGER,
        CAPABILITY_PLANNING_SNAPSHOTS,
        CAPABILITY_RESERVATION_REPLAY,
        PlanningTruthUnavailable,
        get_latest_read_snapshot,
    )

    plan = _get_plan(db, plan_id)
    run = _resolve_execution_run(db, plan, run_id)

    if str(run.status or "") == "CLOSED":
        closed_snapshot = (
            db.query(ClosedPlanSnapshot)
            .filter(
                ClosedPlanSnapshot.plan_id == int(plan.id),
                ClosedPlanSnapshot.run_id == int(run.run_id),
            )
            .one_or_none()
        )
        if closed_snapshot is None:
            return _finalize_execution_payload(db, _execution_unavailable_payload(
                db,
                plan=plan,
                run=run,
                root_item_id=None,
                bom_level=None,
                flow=None,
                truth_state=get_truth_state(db),
                reason="Execution snapshot is missing for the closed plan",
            ), root_item_id=root_item_id, bom_level=bom_level, flow=flow,
                status=status, include_net_zero=include_net_zero,
                sort_by=sort_by, sort_dir=sort_dir, limit=limit, offset=offset)
        payload = dict(closed_snapshot.payload or {})
    else:
        snapshot_key = _execution_snapshot_key(
            plan_id=plan.id,
            run_id=run.run_id,
            root_item_id=root_item_id,
            bom_level=bom_level,
            flow=flow,
        )
        capabilities = (
            CAPABILITY_PHYSICAL_LEDGER,
            CAPABILITY_RESERVATION_REPLAY,
            CAPABILITY_EXECUTION_ALLOCATIONS,
            "supplier_receipt_coverage",
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
            return _finalize_execution_payload(db, _execution_unavailable_payload(
                db,
                plan=plan,
                run=run,
                root_item_id=None,
                bom_level=None,
                flow=None,
                truth_state=exc.state,
            ), root_item_id=root_item_id, bom_level=bom_level, flow=flow,
                status=status, include_net_zero=include_net_zero,
                sort_by=sort_by, sort_dir=sort_dir, limit=limit, offset=offset)
        if snapshot is None:
            return _finalize_execution_payload(db, _execution_unavailable_payload(
                db, plan=plan, run=run, root_item_id=None,
                bom_level=None, flow=None, truth_state=get_truth_state(db),
                reason="Execution snapshot is missing for the accepted Ledger generation",
            ), root_item_id=root_item_id, bom_level=bom_level, flow=flow,
                status=status, include_net_zero=include_net_zero,
                sort_by=sort_by, sort_dir=sort_dir, limit=limit, offset=offset)
        payload = dict(snapshot.payload)

    payload = _attach_run_output_summary(db, payload, run)

    return _finalize_execution_payload(
        db,
        payload,
        root_item_id=root_item_id,
        bom_level=bom_level,
        flow=flow,
        status=status,
        include_net_zero=include_net_zero,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )


def build_period_plan_execution_snapshot(
    db: Session,
    plan_id: int,
    *,
    run_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    bom_level: Optional[int] = None,
    flow: Optional[str] = None,
    generation_id: Optional[int] = None,
    persist: bool = False,
) -> Dict[str, Any]:
    """Build one immutable Ledger-native plan/run execution snapshot."""
    from .planning_truth import get_truth_state

    plan = _get_plan(db, plan_id)
    run = _resolve_execution_run(db, plan, run_id)
    reqs_with_items = (
        db.query(MrpRequirement, Item)
        .join(Item, Item.item_id == MrpRequirement.item_id)
        .filter(MrpRequirement.run_id == int(run.run_id))
        .order_by(MrpRequirement.bom_level.asc(), Item.item_name.asc())
        .all()
    )
    run_requirements = [int(req.id) for req, _item in reqs_with_items]
    items_by_req: Dict[int, Dict[str, Any]] = {}
    for req, item in reqs_with_items:
        items_by_req[int(req.id)] = {
            "item_id": int(req.item_id),
            "item_code": str(item.item_code or ""),
            "item_name": str(item.item_name or ""),
            "item_article": str(item.item_article or "") if item.item_article else None,
            "gross_required_qty": _to_float(req.total_required_qty),
            "net_required_qty": _to_float(req.net_required_qty),
            "bom_level": int(req.bom_level or 0),
        }

    if generation_id is None:
        if run.ledger_generation_id is not None:
            generation_id = int(run.ledger_generation_id)
        else:
            truth = get_truth_state(db)
            if truth.generation_id is None:
                raise ValueError("execution snapshot requires a Ledger generation")
            generation_id = int(truth.generation_id)
    if not isinstance(generation_id, int) or generation_id <= 0:
        raise ValueError("execution snapshot requires generation_id")

    generation = db.get(LedgerGeneration, generation_id)
    if generation is None:
        raise ValueError("execution snapshot references missing LedgerGeneration")
    if generation.cutoff is None:
        raise ValueError("execution snapshot requires generation cutoff")
    if str(generation.status or "") not in {"building", "accepted"}:
        raise ValueError("execution snapshot requires building or accepted generation")

    root_item_ids = sorted({
        int(item_id)
        for (item_id,) in (
            db.query(ProductionPlanLine.item_id)
            .filter(ProductionPlanLine.plan_id == int(plan.id))
            .distinct()
            .all()
        )
    })
    descendants_by_root = _bom_descendants_by_item(db, root_item_ids)
    roots_by_item: Dict[int, List[int]] = {}
    for root_id, descendant_ids in descendants_by_root.items():
        for item_id in descendant_ids:
            roots_by_item.setdefault(int(item_id), []).append(int(root_id))
    for item_id in list(roots_by_item):
        roots_by_item[item_id] = sorted(set(roots_by_item[item_id]))

    run_root_accepted = _to_float(
        db.query(func.coalesce(func.sum(MrpRunRoot.accepted_qty), 0))
        .filter(MrpRunRoot.run_id == int(run.run_id))
        .scalar()
    )
    if run.prior_run_id is not None and run_root_accepted <= 1e-9:
        # A replacement MRP has no execution history of its own at birth.  Its
        # requirements remain visible in the MRP result, while this journal
        # intentionally stays empty until the first accepted root output.
        rows = []
    else:
        rows, _meta = _build_execution_snapshot_rows(
            db,
            run,
            requirement_ids=run_requirements,
            items_by_requirement=items_by_req,
            generation_id=generation_id,
            root_item_ids_by_item=roots_by_item,
        )
    rows = _filter_execution_rows(
        db,
        rows,
        root_item_id=root_item_id,
        bom_level=bom_level,
        flow=flow,
    )
    rows = _attach_execution_information_links(rows)
    summary = _execution_row_summary(rows)
    payload = _attach_run_output_summary(db, {
        "plan": _serialize_plan(plan),
        "run_id": int(run.run_id),
        # Candidate snapshots are unreachable until the surrounding transaction
        # atomically accepts and publishes their generation.
        "truth_status": "accepted",
        "ledger_generation": int(generation_id),
        "cutoff": generation.cutoff.isoformat(),
        "truth_generation_id": generation_id,
        "truth_cutoff": generation.cutoff.isoformat(),
        "truth_reason": None,
        "rows": rows,
        "summary": summary,
    }, run)
    snapshot_key = _execution_snapshot_key(
        plan_id=plan.id,
        run_id=run.run_id,
        root_item_id=None,
        bom_level=None,
        flow=None,
    )
    if not persist:
        return payload

    existing = (
        db.query(PlanningReadSnapshot)
        .filter(
            PlanningReadSnapshot.consumer == "period_plan_execution",
            PlanningReadSnapshot.snapshot_key == snapshot_key,
            PlanningReadSnapshot.ledger_generation_id == int(generation_id),
        )
        .one_or_none()
    )
    if existing is not None:
        if (
            existing.cutoff != generation.cutoff
            or str(existing.truth_status) != "accepted"
            or dict(existing.payload or {}) != payload
            or existing.reason is not None
        ):
            raise ValueError(
                f"execution snapshot {snapshot_key} conflicts with sealed candidate"
            )
        return dict(existing.payload)
    db.add(PlanningReadSnapshot(
        consumer="period_plan_execution",
        snapshot_key=snapshot_key,
        ledger_generation_id=int(generation_id),
        cutoff=generation.cutoff,
        truth_status="accepted",
        payload=payload,
        reason=None,
        published_at=datetime.now(timezone.utc),
    ))
    db.flush()
    return payload


def build_period_plan_execution_snapshots_for_generation(
    db: Session,
    generation_id: int,
) -> Dict[str, Any]:
    """Build every fixed plan/run snapshot belonging to one candidate."""
    generation = db.get(LedgerGeneration, int(generation_id))
    if generation is None:
        raise ValueError("execution snapshot generation does not exist")
    reservation_run_ids = {
        int(run_id)
        for (run_id,) in (
            db.query(ReservationEntry.run_id)
            .filter(
                ReservationEntry.ledger_generation_id == int(generation_id),
                ReservationEntry.run_id.isnot(None),
            )
            .distinct()
            .all()
        )
    }
    # A candidate run with zero reservations (e.g. a plan whose demand is fully
    # stock-covered) still owns a journal: derive runs from the generation's own
    # FIXED_SNAPSHOT lineage too, not only from reservation back-references.
    generation_run_ids = {
        int(run_id)
        for (run_id,) in (
            db.query(PlanningRun.run_id)
            .filter(
                PlanningRun.ledger_generation_id == int(generation_id),
                PlanningRun.status == "FIXED_SNAPSHOT",
                PlanningRun.source_plan_id.isnot(None),
            )
            .all()
        )
    }
    run_ids = sorted(reservation_run_ids | generation_run_ids)
    runs = (
        db.query(PlanningRun)
        .filter(PlanningRun.run_id.in_(run_ids))
        .order_by(PlanningRun.run_id.asc())
        .all()
        if run_ids
        else []
    )
    if len(runs) != len(run_ids):
        raise ValueError("execution snapshot run lineage is incomplete")
    snapshots: List[Dict[str, int]] = []
    for run in runs:
        if (
            str(run.status or "") != "FIXED_SNAPSHOT"
            or run.source_plan_id is None
        ):
            raise ValueError(
                f"execution snapshot run {run.run_id} lacks fixed period-plan lineage"
            )
        build_period_plan_execution_snapshot(
            db,
            int(run.source_plan_id),
            run_id=int(run.run_id),
            generation_id=int(generation_id),
            persist=True,
        )
        snapshots.append({
            "plan_id": int(run.source_plan_id),
            "run_id": int(run.run_id),
        })
    return {
        "ledger_generation_id": int(generation_id),
        "snapshots": len(snapshots),
        "plan_runs": snapshots,
    }

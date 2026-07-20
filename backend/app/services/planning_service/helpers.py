from __future__ import annotations

from datetime import datetime, date, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Set, DefaultDict, Callable

from sqlalchemy.orm import Session, load_only
from sqlalchemy import func, and_, asc, desc
from collections import defaultdict
import json
import re
import math
import logging
from ...models import (
    PlanningConfigVersion,
    PlanningRun,
    PlannedOrder,
    PlannedOrderStage,
    PlannedPurchase,
    PlannedRework,
    CapacityLoad,
    PeggingLink,
    Item,
    Unit,
    ProductionPlanHeader,
    DefaultSpecification,
    SpecComponent,
    ProductionPlanEntry,
    ProductionResource,
    ResourceStage,
    ProductionStage,
    SpecOperation,
    Operation,
    ProductionKind,
    ResourceProductionKind,
    Specification,
    ProductionOrder,
    ProductionProduct,
    SupplierOrder,
    SupplierOrderItem,
    Supplier,
    ItemCategory,
)
from ...models import RootProduct
from ..stage_logic import determine_parent_stage_and_norm, pick_area_for_stage
from ..order_quantity_calculator import OrderQuantityCalculator
from ..priority_manager import PriorityManager
from ..capacity_scheduler import CapacityScheduler
from ..mrp_stock_helpers import (
    active_wip_eta_by_item as _active_wip_eta_by_item,
    consume_wip_at_or_before as _consume_wip_at_or_before,
    effective_stock_by_item_all as _effective_stock_by_item_all,
)
from ..pegging_builder import PeggingBuilder
from ..replenishment import (
    REPLENISHMENT_FLOW_PURCHASE,
    REPLENISHMENT_FLOW_REWORK,
    classify_replenishment_flow,
)
from ..warnings import make_warning, log_warning
from ..supplier_order_status import (
    STATE_TO_PHASE,
    NETTING_PHASES,
    state_counts_in_mrp as _supplier_order_counts_in_mrp,
)


from .constants import DONE_STATE_KEY, _REF1C_RE

def _unit_display_from_parts(
    guid: Optional[str],
    short_name: Optional[str],
    unit_name: Optional[str],
    unit_code: Optional[str],
) -> str:
    value = str(short_name or unit_name or unit_code or "").strip()
    if value:
        return value
    raw_guid = str(guid or "").strip()
    if raw_guid and not _REF1C_RE.match(raw_guid):
        return raw_guid
    return "шт."


def _bom_descendant_ids_for_roots(db: Session, root_item_ids: List[int]) -> Set[int]:
    roots = sorted({int(i) for i in root_item_ids if i is not None})
    if not roots:
        return set()

    spec_by_item: Dict[int, int] = {
        int(row.item_id): int(row.spec_id)
        for row in db.query(DefaultSpecification.item_id, DefaultSpecification.spec_id)
        .filter(DefaultSpecification.item_id.in_(roots))
        .all()
    }
    result: Set[int] = set(roots)

    def visit(item_id: int, seen_specs: Set[int]) -> None:
        spec_id = spec_by_item.get(int(item_id))
        if not spec_id or spec_id in seen_specs:
            return
        next_seen = set(seen_specs)
        next_seen.add(int(spec_id))
        for row in db.query(SpecComponent.item_id).filter(SpecComponent.spec_id == int(spec_id)).all():
            child_id = int(row.item_id)
            result.add(child_id)
            if child_id not in spec_by_item:
                ds = db.query(DefaultSpecification.spec_id).filter(DefaultSpecification.item_id == child_id).first()
                if ds:
                    spec_by_item[child_id] = int(ds.spec_id)
            visit(child_id, next_seen)

    for root_id in roots:
        visit(root_id, set())
    return result


def _load_stage_area_context(db: Session) -> Tuple[Dict[int, str], Dict[int, int], Dict[int, str]]:
    """Return stage names plus the best production resource for each stage."""
    stage_name_by_id: Dict[int, str] = {}
    try:
        for stage in db.query(ProductionStage).all():
            sid = getattr(stage, "stage_id", None)
            if sid is not None:
                stage_name_by_id[int(sid)] = str(getattr(stage, "stage_name", "") or "")
    except Exception:
        stage_name_by_id = {}

    resources: List[ProductionResource] = []
    stages_by_resource: Dict[int, Set[int]] = {}
    try:
        resources = db.query(ProductionResource).all()
        for row in db.query(ResourceStage).all():
            rid = getattr(row, "resource_id", None)
            sid = getattr(row, "stage_id", None)
            if rid is None or sid is None:
                continue
            stages_by_resource.setdefault(int(rid), set()).add(int(sid))
    except Exception:
        resources = []
        stages_by_resource = {}

    area_id_by_stage: Dict[int, int] = {}
    for stage_id in stage_name_by_id:
        area_id = pick_area_for_stage(resources, stages_by_resource, stage_name_by_id, int(stage_id))
        if area_id is not None:
            area_id_by_stage[int(stage_id)] = int(area_id)

    area_name_by_id: Dict[int, str] = {}
    for resource in resources:
        rid = getattr(resource, "resource_id", None)
        if rid is None:
            continue
        area_name_by_id[int(rid)] = str(getattr(resource, "resource_name", "") or "")

    return stage_name_by_id, area_id_by_stage, area_name_by_id


def _load_purchase_area_map(db: Session, item_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Map a purchased component to the stage/resource where it is consumed."""
    unique_item_ids = sorted({int(item_id) for item_id in item_ids if item_id is not None})
    if not unique_item_ids:
        return {}

    stage_name_by_id, area_id_by_stage, area_name_by_id = _load_stage_area_context(db)
    component_stage: Dict[int, int] = {}
    try:
        rows = (
            db.query(SpecComponent.item_id, SpecComponent.stage_id)
            .filter(SpecComponent.item_id.in_(unique_item_ids))
            .filter(SpecComponent.stage_id.isnot(None))
            .all()
        )
    except Exception:
        rows = []

    for row in rows:
        item_id_val = getattr(row, "item_id", row[0] if isinstance(row, (tuple, list)) and len(row) > 0 else None)
        stage_id_val = getattr(row, "stage_id", row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else None)
        if item_id_val is None or stage_id_val is None:
            continue
        iid = int(item_id_val)
        if iid in component_stage:
            continue
        component_stage[iid] = int(stage_id_val)

    result: Dict[int, Dict[str, Any]] = {}
    for iid, sid in component_stage.items():
        area_id = area_id_by_stage.get(int(sid))
        area_name = area_name_by_id.get(int(area_id), "") if area_id is not None else ""
        result[int(iid)] = {
            "main_area_id": int(area_id) if area_id is not None else None,
            "main_area_name": area_name or stage_name_by_id.get(int(sid)) or None,
            "main_stage_id": int(sid),
            "main_stage_name": stage_name_by_id.get(int(sid)) or None,
        }
    return result


def _load_production_area_map(db: Session, item_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Map a produced item to its main stage/resource from the default spec."""
    unique_item_ids = sorted({int(item_id) for item_id in item_ids if item_id is not None})
    if not unique_item_ids:
        return {}

    stage_name_by_id, area_id_by_stage, area_name_by_id = _load_stage_area_context(db)
    item_to_spec: Dict[int, int] = {}
    try:
        defaults = (
            db.query(DefaultSpecification.item_id, DefaultSpecification.spec_id)
            .filter(DefaultSpecification.item_id.in_(unique_item_ids))
            .all()
        )
    except Exception:
        defaults = []

    spec_ids: Set[int] = set()
    for row in defaults or []:
        item_id_val = getattr(row, "item_id", row[0] if isinstance(row, (tuple, list)) and len(row) > 0 else None)
        spec_id_val = getattr(row, "spec_id", row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else None)
        if item_id_val is None or spec_id_val is None:
            continue
        item_to_spec[int(item_id_val)] = int(spec_id_val)
        spec_ids.add(int(spec_id_val))

    if not spec_ids:
        return {}

    stage_hours_by_spec: Dict[int, Dict[int, float]] = defaultdict(dict)
    try:
        op_rows = (
            db.query(
                SpecOperation.spec_id,
                SpecOperation.stage_id,
                func.sum(func.coalesce(SpecOperation.time_norm, Operation.time_norm, 0.0)).label("hours"),
            )
            .outerjoin(Operation, SpecOperation.operation_id == Operation.operation_id)
            .filter(SpecOperation.spec_id.in_(list(spec_ids)))
            .filter(SpecOperation.stage_id.isnot(None))
            .group_by(SpecOperation.spec_id, SpecOperation.stage_id)
            .all()
        )
    except Exception:
        op_rows = []

    for row in op_rows or []:
        spec_id_val = getattr(row, "spec_id", row[0] if isinstance(row, (tuple, list)) and len(row) > 0 else None)
        stage_id_val = getattr(row, "stage_id", row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else None)
        hours_val = getattr(row, "hours", row[2] if isinstance(row, (tuple, list)) and len(row) > 2 else 0.0)
        if spec_id_val is None or stage_id_val is None:
            continue
        stage_hours_by_spec[int(spec_id_val)][int(stage_id_val)] = float(hours_val or 0.0)

    if not stage_hours_by_spec:
        try:
            comp_rows = (
                db.query(SpecComponent.spec_id, SpecComponent.stage_id)
                .filter(SpecComponent.spec_id.in_(list(spec_ids)))
                .filter(SpecComponent.stage_id.isnot(None))
                .all()
            )
        except Exception:
            comp_rows = []
        for row in comp_rows or []:
            spec_id_val = getattr(row, "spec_id", row[0] if isinstance(row, (tuple, list)) and len(row) > 0 else None)
            stage_id_val = getattr(row, "stage_id", row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else None)
            if spec_id_val is None or stage_id_val is None:
                continue
            stage_hours_by_spec[int(spec_id_val)].setdefault(int(stage_id_val), 0.0)

    result: Dict[int, Dict[str, Any]] = {}
    for item_id_val, spec_id_val in item_to_spec.items():
        stage_hours = stage_hours_by_spec.get(int(spec_id_val), {})
        if not stage_hours:
            continue
        sid = max(stage_hours.items(), key=lambda item: float(item[1] or 0.0))[0]
        area_id = area_id_by_stage.get(int(sid))
        area_name = area_name_by_id.get(int(area_id), "") if area_id is not None else ""
        result[int(item_id_val)] = {
            "main_area_id": int(area_id) if area_id is not None else None,
            "main_area_name": area_name or stage_name_by_id.get(int(sid)) or None,
            "main_stage_id": int(sid),
            "main_stage_name": stage_name_by_id.get(int(sid)) or None,
        }
    return result


def _production_supply_qty_expr():
    """Quantity still expected from an open production line.

    A completed 1C order is historical execution, not future supply. Its
    output becomes MRP coverage only through the synced warehouse balance.
    """
    return func.coalesce(ProductionProduct.remaining_qty, 0.0)


def _ensure_dict(raw: Any) -> Dict[str, Any]:
    """
    Try to robustly convert various JSON/Mapping representations to a plain dict.
    Gracefully handles:
      - dict-like objects
      - JSON string
      - objects implementing .items()
      - list of (key, value) pairs
    Returns {} if conversion is not possible.
    """
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return dict(parsed)
            else:
                return {}
        except Exception:
            return {}
    # Mapping protocol
    try:
        # This can still raise if raw is an iterable of single values (e.g. list of strings)
        return dict(raw or {})
    except Exception:
        pass
    # Try items()
    try:
        return dict(getattr(raw, "items")())
    except Exception:
        pass
    # Try sequence of pairs
    try:
        return {k: v for (k, v) in list(raw)}
    except Exception:
        pass
    return {}


def _load_turning_blank_priority_map(db: Session, run_id: int) -> Dict[Tuple[int, str], Dict[str, Any]]:
    try:
        run = db.query(PlanningRun).filter(PlanningRun.run_id == int(run_id)).first()
        warnings = list(getattr(run, "warnings", None) or []) if run else []
    except Exception:
        warnings = []
    result: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for warning in warnings:
        if not isinstance(warning, dict) or warning.get("code") != "TURNING_BLANK_PRIORITY":
            continue
        try:
            item_id = int(warning.get("item_id"))
            need_date = str(warning.get("need_date") or "")
        except Exception:
            continue
        if not need_date:
            continue
        result[(item_id, need_date)] = warning
    return result


def _turning_blank_badge(priority_map: Dict[Tuple[int, str], Dict[str, Any]], item_id: int, need_date: Any) -> Optional[str]:
    date_key = need_date.isoformat() if isinstance(need_date, (datetime, date)) else str(need_date or "")
    if (int(item_id), date_key) in priority_map:
        return "Заготовка под токарный участок"
    return None


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursive dict merge: override values take precedence."""
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override if override is not None else base
    result = dict(base)
    for k, v in (override or {}).items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _to_date(val: Any) -> date:
    """Robustly convert string/datetime to date object"""
    if isinstance(val, date):
        return val
    if isinstance(val, datetime):
        return val.date()
    if not isinstance(val, str):
        raise TypeError(f"Cannot convert {type(val)} to date")
    return datetime.fromisoformat(val.replace("Z", "+00:00")).date()


def _load_item_category_meta(db: Session, item_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    unique_ids = sorted({int(iid) for iid in item_ids if iid is not None})
    if not unique_ids:
        return {}

    try:
        rows = (
            db.query(
                Item.item_id,
                ItemCategory.category_id,
                ItemCategory.category_code,
                ItemCategory.category_name,
                ItemCategory.category_ref1c,
            )
            .outerjoin(ItemCategory, Item.category_id == ItemCategory.category_id)
            .filter(Item.item_id.in_(unique_ids))
            .all()
        )
    except Exception:
        rows = []

    result: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        try:
            item_id_val, category_id_val, category_code_val, category_name_val, category_ref1c_val = row
        except Exception:
            continue
        has_linked_category = category_id_val is not None
        resolved_group_name = (category_name_val or "").strip()
        if not resolved_group_name and has_linked_category:
            resolved_group_name = "Без названия группы"
        if not resolved_group_name:
            resolved_group_name = "Без товарной группы"

        result[int(item_id_val)] = {
            "group_id": int(category_id_val) if category_id_val is not None else None,
            "group_name": resolved_group_name,
            "group_ref1c": category_ref1c_val,
        }
    return result


def _read_last_stock_sync_at() -> Optional[str]:
    from pathlib import Path
    p = Path("config") / "last_sync_time.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text("utf-8") or "{}")
        if not isinstance(data, dict):
            return None
        val = str(data.get("last_sync") or "").strip()
        return val or None
    except Exception:
        return None


def _get_active_production_remaining_by_item(db: Session) -> Dict[int, float]:
    """
    Aggregate remaining qty from ALL active production orders by produced item,
    regardless of source. Internal MRP-originated orders (source='mrp', created
    via /v1/production-control/orders/from-mrp) and 1C-synced ones (source='1c')
    are both counted here, because both represent already-committed production
    that should reduce the net requirement of subsequent MRP runs (plan rule:
    "эти заказы учитываются в следующих MRP-расчетах как активное ожидаемое
    производство").

    Active filter:
    - deletion_mark == false
    - completed 1C orders are excluded. Their factual output must be present
      in synced warehouse stock before it can cover new MRP demand.
    - remaining_qty > 0.
    """
    try:
        supply_qty = _production_supply_qty_expr()
        rows = (
            db.query(
                ProductionProduct.item_id,
                func.sum(supply_qty).label("remaining_qty"),
            )
            .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
            .filter(ProductionOrder.deletion_mark.is_(False))
            .filter(func.lower(func.coalesce(ProductionOrder.order_state_key, "")) != DONE_STATE_KEY)
            .filter(supply_qty > 0)
            .group_by(ProductionProduct.item_id)
            .all()
        )
    except Exception:
        rows = []

    result: Dict[int, float] = {}
    for iid, qty in rows:
        try:
            result[int(iid)] = float(qty or 0.0)
        except Exception:
            continue
    return result


_get_active_1c_remaining_by_item = _get_active_production_remaining_by_item


def _get_active_supplier_remaining_by_item_date(db: Session) -> Dict[int, List[Tuple[date, float]]]:
    """
    Aggregate open supplier-order quantities by item and expected delivery date.

    Business rule (см. supplier_order_status):
    - deleted orders are ignored;
    - учитываются только фазы «в пути» / «на складе» (state_counts_in_mrp);
      группа «Нет товара» (Новый заказ / В закупку / Бухгалтерия) и терминальные
      (Отменён / Завершён) — игнорируются;
    - rows without delivery date are skipped for automatic date-sensitive coverage.
    """
    try:
        rows = (
            db.query(
                SupplierOrderItem.item_id_ref,
                SupplierOrderItem.delivery_date,
                SupplierOrder.order_state_key,
                SupplierOrder.order_state_name,
                SupplierOrderItem.remaining_qty,
            )
            .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
            .filter(SupplierOrder.deletion_mark.is_(False))
            .filter(SupplierOrderItem.delivery_date.isnot(None))
            .filter(func.coalesce(SupplierOrderItem.remaining_qty, 0.0) > 0)
            .order_by(SupplierOrderItem.delivery_date.asc())
            .all()
        )
    except Exception:
        rows = []

    result: Dict[int, List[Tuple[date, float]]] = defaultdict(list)
    for iid, delivery_dt, state_key, state_name, qty in rows:
        try:
            if not _supplier_order_counts_in_mrp(state_name):
                continue
            item_id = int(iid)
            delivery_date = delivery_dt.date() if isinstance(delivery_dt, datetime) else delivery_dt
            if not isinstance(delivery_date, date):
                delivery_date = _to_date(str(delivery_dt))
            remaining_qty = float(qty or 0.0)
        except Exception:
            continue
        if remaining_qty <= 1e-12:
            continue
        result[item_id].append((delivery_date, remaining_qty))
    return dict(result)


def _load_late_supplier_order_coverage(db: Session, item_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """
    Load active supplier orders that can cover demand, but arrive after the need date.
    This is diagnostic only: late orders do not reduce MRP purchase quantity.
    """
    ids = sorted({int(iid) for iid in item_ids if iid is not None})
    if not ids:
        return {}

    try:
        rows = (
            db.query(
                SupplierOrderItem.item_id_ref,
                SupplierOrderItem.delivery_date,
                SupplierOrderItem.remaining_qty,
                SupplierOrder.order_number,
                SupplierOrder.order_state_key,
                SupplierOrder.order_state_name,
            )
            .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
            .filter(SupplierOrderItem.item_id_ref.in_(ids))
            .filter(SupplierOrder.deletion_mark.is_(False))
            .filter(SupplierOrderItem.delivery_date.isnot(None))
            .filter(func.coalesce(SupplierOrderItem.remaining_qty, 0.0) > 0)
            .order_by(SupplierOrderItem.delivery_date.asc(), SupplierOrder.order_number.asc())
            .all()
        )
    except Exception:
        rows = []

    result: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for iid, delivery_dt, qty, order_number, state_key, state_name in rows:
        try:
            if not _supplier_order_counts_in_mrp(state_name):
                continue
            item_id = int(iid)
            delivery_date = delivery_dt.date() if isinstance(delivery_dt, datetime) else delivery_dt
            if not isinstance(delivery_date, date):
                delivery_date = _to_date(str(delivery_dt))
            remaining_qty = float(qty or 0.0)
        except Exception:
            continue
        if remaining_qty <= 1e-12:
            continue
        result[item_id].append(
            {
                "delivery_date": delivery_date,
                "remaining_qty": remaining_qty,
                "order_number": str(order_number or "").strip(),
            }
        )
    return dict(result)


def _late_supplier_order_badge(
    late_supplier_rows: Dict[int, List[Dict[str, Any]]],
    item_id: int,
    need_date: Any,
    qty: Any,
) -> Optional[str]:
    if not need_date:
        return None
    try:
        need_dt = need_date.date() if isinstance(need_date, datetime) else need_date
        if not isinstance(need_dt, date):
            need_dt = _to_date(str(need_date))
        required_qty = float(qty or 0.0)
    except Exception:
        return None
    if required_qty <= 1e-12:
        return None

    total_late = 0.0
    first_delivery: Optional[date] = None
    order_numbers: List[str] = []
    for row in late_supplier_rows.get(int(item_id), []) or []:
        delivery_date = row.get("delivery_date")
        if not isinstance(delivery_date, date) or delivery_date <= need_dt:
            continue
        if first_delivery is None:
            first_delivery = delivery_date
        total_late += float(row.get("remaining_qty", 0.0) or 0.0)
        order_number = str(row.get("order_number") or "").strip()
        if order_number and order_number not in order_numbers:
            order_numbers.append(order_number)
        if total_late + 1e-9 >= required_qty:
            break

    if first_delivery is None or total_late <= 1e-12:
        return None

    delay_days = max(0, int((first_delivery - need_dt).days))
    order_suffix = f" ({', '.join(order_numbers[:2])})" if order_numbers else ""
    if total_late + 1e-9 >= required_qty:
        return f"Покрыто заказом, но опоздание {delay_days} дн.{order_suffix}"
    return f"Частично покрыто заказом, но опоздание {delay_days} дн.{order_suffix}"


def _merge_badges(*badges: Optional[str]) -> Optional[str]:
    parts: List[str] = []
    for badge in badges:
        text = str(badge or "").strip()
        if text and text not in parts:
            parts.append(text)
    return "; ".join(parts) if parts else None


def _build_component_reservations_from_active_1c(
    db: Session,
    default_spec_map: Dict[int, int],
    components_loader: Callable[[int], List[SpecComponent]],
    max_depth: int,
) -> Tuple[Dict[int, float], List[Dict[str, Any]]]:
    """
    Build recursive component reservation map from active 1C orders.

    For each open 1C order line with remaining qty > 0:
      reserve(component) += remaining_qty * qty_per_unit
    Completed 1C orders reserve no components; they are historical execution.
    with recursive BOM explosion and cycle protection.
    """
    warnings: List[Dict[str, Any]] = []
    reserved_by_component: DefaultDict[int, float] = defaultdict(float)

    try:
        supply_qty = _production_supply_qty_expr()
        seed_rows = (
            db.query(
                ProductionProduct.item_id,
                func.sum(supply_qty).label("remaining_qty"),
            )
            .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
            .filter(ProductionOrder.deletion_mark.is_(False))
            .filter(func.lower(func.coalesce(ProductionOrder.order_state_key, "")) != DONE_STATE_KEY)
            .filter(supply_qty > 0)
            .group_by(ProductionProduct.item_id)
            .all()
        )
    except Exception:
        seed_rows = []

    seen_cycle_edges: Set[Tuple[int, int]] = set()

    def explode(parent_item_id: int, qty: float, depth: int, path: Set[int]) -> None:
        if qty <= 1e-12:
            return
        if depth > int(max_depth):
            return

        spec_id = default_spec_map.get(int(parent_item_id))
        if not spec_id:
            return

        comps = components_loader(int(spec_id)) or []
        new_path = set(path)
        new_path.add(int(parent_item_id))

        for comp in comps:
            try:
                child_id = int(getattr(comp, "item_id"))
                per_unit = float(getattr(comp, "quantity", 0.0) or 0.0)
            except Exception:
                continue
            if per_unit <= 1e-12:
                continue

            # Cycle protection: do not reserve cycle edge and do not recurse into it.
            if child_id in new_path:
                cycle_edge = (int(parent_item_id), int(child_id))
                if cycle_edge not in seen_cycle_edges:
                    seen_cycle_edges.add(cycle_edge)
                    warnings.append(
                        make_warning(
                            "ACTIVE_1C_BOM_CYCLE_SKIPPED",
                            "Cycle detected during recursive reservation of components from active 1C orders",
                            parent_item_id=int(parent_item_id),
                            child_item_id=int(child_id),
                            depth=int(depth),
                        )
                    )
                continue

            child_qty = float(qty) * float(per_unit)
            if child_qty <= 1e-12:
                continue

            # Anti-duplicates policy:
            # identical component from different roots/paths should be summed;
            # only cyclic re-entrance is skipped above.
            reserved_by_component[int(child_id)] += child_qty

            explode(int(child_id), float(child_qty), depth + 1, new_path)

    for iid, rem_qty in seed_rows:
        try:
            parent_id = int(iid)
            q = float(rem_qty or 0.0)
        except Exception:
            continue
        if q <= 1e-12:
            continue
        explode(parent_id, q, depth=1, path=set())

    return dict(reserved_by_component), warnings


def _generate_shortage_report_v2(db: Session, run_id: int) -> Dict[str, Any]:
    """
    Placeholder function kept for test monkeypatching.
    Real implementation lives elsewhere; tests replace this via monkeypatch.setattr.
    """
    raise NotImplementedError("Use monkeypatch to stub _generate_shortage_report_v2 in tests")

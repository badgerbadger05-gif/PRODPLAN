"""Phase-2 static supermarket-position preview and materialization."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from ...models import (
    DbrCategorySupplyRisk,
    DbrDrumSchedule,
    DbrSettings,
    DbrSupermarketPosition,
    Item,
    ItemCategory,
)
from ..replenishment import (
    REPLENISHMENT_FLOW_PRODUCTION,
    REPLENISHMENT_FLOW_PURCHASE,
    REPLENISHMENT_FLOW_REWORK,
    classify_replenishment_flow,
)
from . import adapters, classify as classify_mod
from .generation import DbrProjectionUnavailable, require_generation
from .core.drum import kit as kit_mod
from .core.feeder import adu, demand_explosion, zones

ROUTE_MACHINING = "machining"
ROUTE_WELDING = "welding"
ROUTE_PAINTING = "painting"
_REBUILD_LOCK = 0x444252504F534954


def _route_class(route_text: str, *, is_w2: bool, is_w3: bool) -> str:
    if is_w2:
        return ROUTE_MACHINING
    if is_w3 or any(token in route_text for token in ("окрас", "покрас", "порош")):
        return ROUTE_PAINTING
    if "свар" in route_text:
        return ROUTE_WELDING
    return ROUTE_MACHINING


def _batch_days(route_text: str, *, is_w3: bool, settings) -> float:
    if is_w3:
        return float(settings.batch_days_paint_black or 0)
    if "токар" in route_text or "фрез" in route_text:
        return float(settings.batch_days_turning or 0)
    if "гиб" in route_text:
        return float(settings.batch_days_bending or 0)
    if "свар" in route_text:
        return float(settings.batch_days_welding or 0)
    return float(settings.batch_days_turning or 0)


def _resolve_schedule(
    db: Session, schedule_id: Optional[int], ledger_generation_id: int
) -> DbrDrumSchedule:
    query = db.query(DbrDrumSchedule).filter(
        DbrDrumSchedule.ledger_generation_id == ledger_generation_id
    )
    if schedule_id is not None:
        schedule = query.filter(DbrDrumSchedule.id == schedule_id).one_or_none()
    else:
        schedule = query.filter(DbrDrumSchedule.status == "active").one_or_none()
    if schedule is None:
        raise LookupError("drum schedule not found")
    return schedule


def _daily_rates(schedule: DbrDrumSchedule, id_to_code: dict[int, str]) -> dict[str, float]:
    qty: dict[str, float] = defaultdict(float)
    workdays = set()
    for slot in schedule.slots:
        code = id_to_code.get(int(slot.item_id))
        if code:
            qty[code] += float(slot.qty or 0)
            workdays.add(slot.slot_date)
    return adu.daily_rates_from_plan(qty, len(workdays))


def _assert_acyclic(roots, components_of, node_for) -> None:
    done: set[str] = set()

    def walk(item: str, stack: tuple[str, ...]) -> None:
        if item in stack:
            raise ValueError("цикл BOM: " + " -> ".join((*stack, item)))
        if item in done:
            return
        next_stack = (*stack, item)
        for child, _qty in components_of(item):
            node = node_for(child)
            if node.kind == demand_explosion.SKIP:
                continue
            if node.kind == demand_explosion.RECURSE or node.explode_through:
                walk(child, next_stack)
        done.add(item)

    for root in roots:
        walk(root, ())


def preview_positions(
    db: Session,
    schedule_id: Optional[int] = None,
    *,
    ledger_generation_id: int,
    diagnostic_legacy: bool = False,
) -> dict[str, Any]:
    """Calculate static positions without writing any database row."""
    if not diagnostic_legacy:
        raise DbrProjectionUnavailable(
            "Ledger-native DBR position preview is not implemented; "
            "legacy stock calculators cannot represent accepted truth"
        )
    schedule = _resolve_schedule(db, schedule_id, ledger_generation_id)
    settings = db.get(DbrSettings, 1)
    if settings is None:
        raise ValueError("настройки DBR не созданы")
    id_to_code, code_to_id = adapters.item_code_maps(db)
    daily_rates = _daily_rates(schedule, id_to_code)
    classify, classifier_notes = classify_mod.build_classifier(db, settings)
    components_of = adapters.build_components_provider(db)
    items = {row.item_code: row for row in db.query(Item).all()}
    route_text_by_item = adapters.item_route_text_map(db)

    node_cache: dict[str, demand_explosion.Node] = {}

    def node_for(code: str) -> demand_explosion.Node:
        if code in node_cache:
            return node_cache[code]
        decision, warehouse = classify(code)
        if decision == kit_mod.FASTENER:
            node = demand_explosion.Node(demand_explosion.SKIP)
        elif decision == kit_mod.RECURSE:
            node = demand_explosion.Node(demand_explosion.RECURSE)
        else:
            item = items.get(code)
            # Сквозной разворот — только для собственного производства:
            # закупка и переработка (давальческая) — глухие границы, голая
            # деталь под покрытой в ADU-спрос не попадает (под сигнал).
            flow = classify_replenishment_flow(item.replenishment_method) if item else REPLENISHMENT_FLOW_PRODUCTION
            make = flow == REPLENISHMENT_FLOW_PRODUCTION
            node = demand_explosion.Node(
                demand_explosion.BOUNDARY,
                warehouse,
                explode_through=make and bool(list(components_of(code))),
            )
        node_cache[code] = node
        return node

    _assert_acyclic(sorted(daily_rates), components_of, node_for)
    memo: dict[str, list[adu.KitLine]] = {}
    kits = {
        sku: demand_explosion.explode_kit(
            sku, components_of, node_for, memo=memo
        )
        for sku in sorted(daily_rates)
    }
    empty = [sku for sku, lines in kits.items() if not lines]
    if empty:
        raise ValueError("пустой BOM/кит для SKU: " + ", ".join(empty))
    adu_rows = adu.build_adu(kits, daily_rates)

    category_names = {
        row.category_id: row.category_name for row in db.query(ItemCategory).all()
    }
    risks = {
        row.item_group: row for row in db.query(DbrCategorySupplyRisk).all()
    }
    warnings = list(classifier_notes)
    positions = []
    calculated_at = datetime.now()
    for (code, boundary_wh), row in sorted(adu_rows.items()):
        item = items.get(code)
        if item is None:
            raise ValueError(f"номенклатура {code} не найдена")
        flow = classify_replenishment_flow(item.replenishment_method)
        purchase = flow == REPLENISHMENT_FLOW_PURCHASE
        processing = flow == REPLENISHMENT_FLOW_REWORK
        category = category_names.get(item.category_id)
        risk = risks.get(category)
        warehouse = boundary_wh
        risk_pct = float(risk.supply_risk_pct or 0) if risk else 0.0
        quality: list[str] = []
        k_var = 0.25 if row.commonality >= 2 else 0.5
        optimal = float(item.optimal_batch or 0)

        if processing:
            # Питатель №3 (давальческая переработка): RT покрывает всю цепочку
            # (мехцех → ожидание рейса → кругорейс → приёмка), квант партии —
            # ADU × рейс-интервал (optimal_batch у этих позиций не бывает).
            # Карточному сроку 1С не доверяем — мусор (дока §6, находка 1).
            rt = float(settings.rt_processing_days or 0)
            batch = float(settings.processing_trip_interval_days or 0)
            computed = zones.compute_purchase_zones(
                row.adu, rt, batch, k_var=k_var, supply_risk_pct=risk_pct
            )
            route_class = None
            supply_type = "processing"
        elif purchase:
            if risk and risk.receipt_warehouse_ref1c:
                warehouse = risk.receipt_warehouse_ref1c
            rt = float(item.replenishment_time or 0)
            batch = float(settings.batch_days_turning or 0)
            computed = zones.compute_purchase_zones(
                row.adu, rt, batch, k_var=k_var, supply_risk_pct=risk_pct
            )
            route_class = None
            supply_type = "purchase"
        else:
            route_text = route_text_by_item.get(code, "")
            is_w2 = warehouse == settings.w2_warehouse_ref1c
            is_w3 = warehouse == settings.w3_warehouse_ref1c
            route_class = _route_class(route_text, is_w2=is_w2, is_w3=is_w3)
            if route_class == ROUTE_PAINTING:
                rt = float(settings.rt_painting_days or 0)
            elif route_class == ROUTE_WELDING:
                rt = float(settings.rt_welding_days or 0)
            else:
                rt = float(settings.rt_machining_days or 0)
            if not route_text and not is_w2 and not is_w3:
                quality.append("route_class_defaulted_no_operation_route_data")
            batch = _batch_days(route_text, is_w3=is_w3, settings=settings)
            computed = zones.compute_zones(
                row.adu,
                rt,
                batch,
                optimal_batch=optimal,
                k_var=k_var,
                supply_risk_pct=risk_pct,
            )
            supply_type = "manufacture"
        if not warehouse:
            raise ValueError(f"не разрешён обязательный склад позиции {code}")
        warnings.extend(f"{code}: {message}" for message in quality)
        positions.append(
            {
                "item_id": item.item_id,
                "item_code": code,
                "item_name": item.item_name,
                "warehouse_ref1c": warehouse,
                "supply_type": supply_type,
                "mode": "shelf" if zones.has_shelf(row.adu, rt, float(settings.shelf_threshold_qty or 0)) else "under_schedule",
                "adu": row.adu,
                "commonality": row.commonality,
                "route_class": route_class,
                "rt_days": rt,
                "rt_source": "chain" if processing else ("lead_time" if purchase else "class"),
                "batch_days": batch,
                "q_batch": computed.green,
                "k_var": k_var,
                "supply_risk_pct": risk_pct,
                "red_qty": computed.red,
                "yellow_qty": computed.yellow,
                "green_qty": computed.green,
                "target_qty": computed.target,
                "data_quality": quality,
                "calculation_snapshot": {
                    "schedule_id": schedule.id,
                    "schedule_daily_rates": daily_rates,
                    "adu": row.adu,
                    "commonality": row.commonality,
                    "rt_days": rt,
                    "batch_days": batch,
                    "k_var": k_var,
                    "supply_risk_pct": risk_pct,
                    "shelf_threshold_qty": float(settings.shelf_threshold_qty or 0),
                },
                "calculated_at": calculated_at,
            }
        )
    return {
        "schedule_id": schedule.id,
        "daily_rates": daily_rates,
        "positions": positions,
        "warnings": sorted(set(warnings)),
    }


def rebuild_positions(
    db: Session,
    schedule_id: Optional[int] = None,
    expected_schedule_id: Optional[int] = None,
    *,
    ledger_generation_id: int | None,
    diagnostic_legacy: bool = False,
) -> dict[str, Any]:
    generation_id = require_generation(
        db, ledger_generation_id, consumer="dbr_positions_rebuild"
    )
    if not diagnostic_legacy:
        raise DbrProjectionUnavailable(
            "Ledger-native DBR position builder is not implemented; "
            "legacy stock calculators cannot be stamped as accepted truth"
        )
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _REBUILD_LOCK})
    preview = preview_positions(
        db,
        schedule_id,
        ledger_generation_id=generation_id,
        diagnostic_legacy=True,
    )
    if expected_schedule_id is not None and preview["schedule_id"] != expected_schedule_id:
        raise ValueError(
            f"активный график изменился: ожидался {expected_schedule_id}, получен {preview['schedule_id']}"
        )
    existing = {
        (row.item_id, row.warehouse_ref1c): row
        for row in db.query(DbrSupermarketPosition)
        .filter(DbrSupermarketPosition.ledger_generation_id == generation_id)
        .with_for_update()
        .all()
    }
    seen = set()
    created = updated = 0
    fields = (
        "supply_type", "mode", "adu", "commonality", "route_class", "rt_days",
        "rt_source", "batch_days", "q_batch", "k_var", "red_qty", "yellow_qty",
        "green_qty", "target_qty", "data_quality", "supply_risk_pct",
        "calculation_snapshot", "calculated_at",
    )
    for data in preview["positions"]:
        key = (data["item_id"], data["warehouse_ref1c"])
        seen.add(key)
        obj = existing.get(key)
        if obj is None:
            obj = DbrSupermarketPosition(
                ledger_generation_id=generation_id,
                item_id=key[0],
                warehouse_ref1c=key[1],
            )
            db.add(obj)
            created += 1
        else:
            updated += 1
        for field in fields:
            setattr(obj, field, data[field])
        obj.source_schedule_id = preview["schedule_id"]
        obj.is_active = True
        obj.is_stale = False
    deactivated = 0
    for key, obj in existing.items():
        if key not in seen and obj.is_active:
            obj.is_active = False
            obj.is_stale = True
            deactivated += 1
    db.flush()
    return {
        **preview,
        "created": created,
        "updated": updated,
        "deactivated": deactivated,
    }


def list_positions(db: Session, active_only: bool = False):
    query = db.query(DbrSupermarketPosition)
    if active_only:
        query = query.filter(DbrSupermarketPosition.is_active.is_(True))
    return query.order_by(DbrSupermarketPosition.item_id, DbrSupermarketPosition.warehouse_ref1c).all()


def position_out(position: DbrSupermarketPosition, live: Optional[dict] = None) -> dict[str, Any]:
    data = {
        column.name: getattr(position, column.name)
        for column in DbrSupermarketPosition.__table__.columns
    }
    data["item_code"] = position.item.item_code
    data["item_name"] = position.item.item_name
    if live is not None:
        data["live_nfp"] = live
    return data


def query_position_views(
    db: Session,
    *,
    include_live_nfp: bool = False,
    active: Optional[bool] = None,
    active_only: bool = False,
    mode: Optional[str] = None,
    supply: Optional[str] = None,
    warehouse: Optional[str] = None,
    zone: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
) -> list[dict[str, Any]]:
    from . import feeder_nfp_service

    query = db.query(DbrSupermarketPosition).join(Item)
    effective_active = True if active_only and active is None else active
    if effective_active is not None:
        query = query.filter(DbrSupermarketPosition.is_active.is_(effective_active))
    if mode:
        query = query.filter(DbrSupermarketPosition.mode == mode)
    if supply:
        query = query.filter(DbrSupermarketPosition.supply_type == supply)
    if warehouse:
        query = query.filter(DbrSupermarketPosition.warehouse_ref1c == warehouse)
    if search:
        pattern = f"%{search.strip()}%"
        query = query.filter(
            or_(Item.item_code.ilike(pattern), Item.item_name.ilike(pattern))
        )
    positions = query.order_by(
        DbrSupermarketPosition.item_id,
        DbrSupermarketPosition.warehouse_ref1c,
    ).all()
    live_by_id = (
        feeder_nfp_service.live_nfp_rows(db, positions)
        if include_live_nfp or zone
        else {}
    )
    if zone:
        requested_zone = zone.strip().casefold()
        positions = [
            row
            for row in positions
            if str(live_by_id[int(row.id)]["zone"]).strip().casefold()
            == requested_zone
        ]
    positions = positions[offset : offset + limit]
    return [
        position_out(row, live_by_id.get(int(row.id)) if include_live_nfp else None)
        for row in positions
    ]


def get_position_view(
    db: Session, position_id: int, *, include_live_nfp: bool = True
) -> Optional[dict[str, Any]]:
    from . import feeder_nfp_service

    position = db.get(DbrSupermarketPosition, position_id)
    if position is None:
        return None
    live = (
        feeder_nfp_service.live_nfp_rows(db, [position])[position_id]
        if include_live_nfp
        else None
    )
    return position_out(position, live)

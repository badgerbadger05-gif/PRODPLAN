"""Advisory replenishment signal projection and idempotent materialization."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from ...models import (
    DbrDrumSchedule,
    DbrDrumSlot,
    DbrFeederSignal,
    DbrSupermarketPosition,
    DbrSettings,
    ItemWarehouseStock,
    Item,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SupplierOrder,
    SupplierOrderItem,
)
from ..production_control_reservations import load_reservation_state
from . import adapters, classify as classify_mod
from .core.drum.kit import build_kit
from . import feeder_material_service, feeder_nfp_service
from .core.feeder import signal_identity, zones

_REFRESH_LOCK = 0x4442525349474E4C

# PRODPLAN-specific signal lifecycle state (not part of the prodflow core port
# in core/feeder/signal_identity.py, which stays a verbatim port).
DIAGNOSTIC = "Diagnostic"


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


def _active_schedule(db: Session) -> Optional[DbrDrumSchedule]:
    return (
        db.query(DbrDrumSchedule)
        .filter(DbrDrumSchedule.status == "active")
        .one_or_none()
    )


def _under_schedule_rows(
    db: Session, schedule: Optional[DbrDrumSchedule], *, today: Optional[date] = None,
) -> list[dict[str, Any]]:
    """Chronological netting for under-schedule boundaries of future slots.

    Position membership is authoritative.  ``KitLine.under_schedule`` is only
    classifier provenance and deliberately is not used as the membership gate.
    """
    if schedule is None:
        return []
    today = today or datetime.now().date()
    positions = (
        db.query(DbrSupermarketPosition)
        .filter(
            DbrSupermarketPosition.is_active.is_(True),
            DbrSupermarketPosition.mode == "under_schedule",
        )
        .all()
    )
    if not positions:
        return []
    item_rows = db.query(Item.item_id, Item.item_code).all()
    id_to_code = {int(item_id): code for item_id, code in item_rows}
    position_by_key = {
        (id_to_code.get(int(p.item_id), ""), _norm(p.warehouse_ref1c)): p
        for p in positions
    }
    slots = (
        db.query(DbrDrumSlot)
        .filter(
            DbrDrumSlot.schedule_id == schedule.id,
            DbrDrumSlot.release_status == "pending",
            DbrDrumSlot.slot_date >= today,
        )
        .order_by(DbrDrumSlot.slot_date, DbrDrumSlot.position, DbrDrumSlot.id)
        .all()
    )
    settings = db.get(DbrSettings, 1)
    if settings is None:
        raise ValueError("настройки DBR не созданы")
    classify, classifier_notes = classify_mod.build_classifier(db, settings)
    components_of = adapters.build_components_provider(db)
    kits: dict[str, list] = {}
    demand_rows: list[tuple[DbrDrumSlot, DbrSupermarketPosition, float]] = []
    for slot in slots:
        sku = id_to_code.get(int(slot.item_id))
        if not sku:
            continue
        if sku not in kits:
            kits[sku] = build_kit(sku, components_of, classify)
        aggregated: dict[int, float] = defaultdict(float)
        for line in kits[sku]:
            # Membership, not line.under_schedule, defines this signal family.
            position = position_by_key.get((line.item, _norm(line.source_warehouse)))
            if position is not None:
                aggregated[int(position.id)] += float(line.qty_per_unit) * float(slot.qty or 0)
        by_id = {int(p.id): p for p in positions}
        for position_id, demand in sorted(aggregated.items()):
            demand_rows.append((slot, by_id[position_id], demand))

    item_ids = {int(p.item_id) for p in positions}
    pool: dict[tuple[int, str], float] = defaultdict(float)
    for item_id, warehouse, qty in db.query(
        ItemWarehouseStock.item_id, ItemWarehouseStock.warehouse_ref1c, ItemWarehouseStock.qty
    ).filter(ItemWarehouseStock.item_id.in_(item_ids)).all():
        pool[(int(item_id), _norm(warehouse))] += float(qty or 0)
    reservation_state = load_reservation_state(db, item_ids=item_ids)
    reservations: dict[tuple[int, str], float] = defaultdict(float)
    for (warehouse, item_id), qty in reservation_state.by_warehouse_item.items():
        reservations[(int(item_id), _norm(warehouse))] += float(qty or 0)
    for key, qty in reservations.items():
        pool[key] -= qty

    inbound: dict[tuple[int, str], list[tuple[date, float]]] = defaultdict(list)
    incomplete_by_key: dict[tuple[int, str], list[str]] = defaultdict(list)
    prod_rows = db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState).join(
        ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id
    ).outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id).filter(
        ProductionProduct.item_id.in_(item_ids), ProductionProduct.remaining_qty > 0,
        ProductionOrder.deletion_mark.is_(False),
    ).all()
    for line, _order, state in prod_rows:
        key = (int(line.item_id), _norm(line.destination_warehouse_ref1c))
        if not key[1]:
            for p in positions:
                if int(p.item_id) == key[0]:
                    incomplete_by_key[(key[0], _norm(p.warehouse_ref1c))].append("production_inbound_destination_missing")
        elif state is None or state.planned_finish_date is None:
            incomplete_by_key[key].append("production_inbound_eta_missing")
        else:
            inbound[key].append((state.planned_finish_date, float(line.remaining_qty or 0)))
    supplier_rows = db.query(SupplierOrderItem, SupplierOrder).join(
        SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id
    ).filter(
        SupplierOrderItem.item_id_ref.in_(item_ids), SupplierOrderItem.remaining_qty > 0,
        SupplierOrder.deletion_mark.is_(False),
    ).all()
    for line, _order in supplier_rows:
        key = (int(line.item_id_ref), _norm(line.destination_warehouse_ref1c))
        if not key[1]:
            for p in positions:
                if int(p.item_id) == key[0]:
                    incomplete_by_key[(key[0], _norm(p.warehouse_ref1c))].append("supplier_inbound_destination_missing")
        elif line.delivery_date is None:
            incomplete_by_key[key].append("supplier_inbound_eta_missing")
        else:
            inbound[key].append((line.delivery_date.date(), float(line.remaining_qty or 0)))
    for arrivals in inbound.values():
        arrivals.sort(key=lambda row: row[0])
    inbound_cursor: dict[tuple[int, str], int] = defaultdict(int)

    output = []
    for slot, position, demand in demand_rows:
        key = (int(position.item_id), _norm(position.warehouse_ref1c))
        arrivals = inbound.get(key, [])
        cursor = inbound_cursor[key]
        while cursor < len(arrivals) and arrivals[cursor][0] <= slot.slot_date:
            pool[key] += arrivals[cursor][1]
            cursor += 1
        inbound_cursor[key] = cursor
        available_before = pool[key]
        shortage = max(demand - available_before, 0.0)
        generated = zones.round_up(shortage, float(position.q_batch or 1)) if shortage else 0.0
        quality = sorted(set(classifier_notes + incomplete_by_key.get(key, [])))
        # Only an actionable recommendation may cover later demand.  A
        # diagnostic batch is hypothetical and must not mask later shortages.
        pool[key] += (generated if not quality else 0.0) - demand
        rt_days = float(position.rt_days or 0)
        need_date = slot.slot_date - timedelta(days=int(round(rt_days)))
        slack = (need_date - today).days
        priority = min(2.0, 1.0 - slack / rt_days) if rt_days > 0 else (2.0 if slack <= 0 else 0.0)
        item_code = id_to_code.get(int(position.item_id), "")
        output.append({
            "position_id": int(position.id), "item_id": int(position.item_id),
            "item_code": item_code, "warehouse_ref1c": position.warehouse_ref1c,
            "signal_type": "Под график", "slot_id": int(slot.id),
            "required_date": slot.slot_date, "need_date": need_date,
            "raw_demand_qty": demand, "raw_shortage_qty": shortage,
            "suggested_qty": generated if not quality else 0.0,
            "calculated_batch_qty": generated,
            "priority": priority,
            "is_complete": not quality, "data_quality": quality,
            "dedup_key": signal_identity.build_dedup_key(
                "Под график", drum_slot=str(slot.id), item=item_code,
                warehouse=position.warehouse_ref1c,
            ),
            "action": "open" if shortage > 0 else "none",
            "available_before": available_before,
        })
    return output


def _kit_shortages(db: Session, schedule: Optional[DbrDrumSchedule]) -> dict[tuple[str, str], float]:
    """Aggregate the gate's already-qualified shortages without re-evaluating it."""
    result: dict[tuple[str, str], float] = defaultdict(float)
    if schedule is None:
        return result
    slots = (
        db.query(DbrDrumSlot)
        .filter(
            DbrDrumSlot.schedule_id == schedule.id,
            DbrDrumSlot.release_status == "pending",
            DbrDrumSlot.shortage_json.isnot(None),
        )
        .all()
    )
    for slot in slots:
        for row in slot.shortage_json or []:
            item = str(row.get("item") or "").strip()
            warehouse = _norm(row.get("warehouse"))
            if not item or not warehouse:
                continue
            deficit = max(float(row.get("required") or 0) - float(row.get("available") or 0), 0.0)
            result[(item, warehouse)] += deficit
    return result


def preview_signals(db: Session) -> dict[str, Any]:
    schedule = _active_schedule(db)
    positions = (
        db.query(DbrSupermarketPosition)
        .filter(
            DbrSupermarketPosition.is_active.is_(True),
            DbrSupermarketPosition.mode == "shelf",
        )
        .order_by(DbrSupermarketPosition.id)
        .all()
    )
    live = feeder_nfp_service.live_nfp_rows(db, positions)
    item_codes = {
        int(item_id): code
        for item_id, code in db.query(Item.item_id, Item.item_code)
        .filter(Item.item_id.in_({int(p.item_id) for p in positions}))
        .all()
    } if positions else {}
    shortages = _kit_shortages(db, schedule)
    existing = {
        row.supermarket_position_id: row
        for row in db.query(DbrFeederSignal)
        .filter(DbrFeederSignal.signal_type == "Пополнение").all()
    }
    rows = []
    for position in positions:
        nfp = live[int(position.id)]
        item_code = item_codes.get(int(position.item_id), "")
        shortage = shortages.get((item_code, _norm(position.warehouse_ref1c)), 0.0)
        kit_force = shortage > 0
        actionable = nfp["is_complete"] and (nfp["zone"] != zones.GREEN or kit_force)
        position_zones = zones.Zones(
            red=float(position.red_qty or 0),
            yellow=float(position.yellow_qty or 0),
            green=float(position.green_qty or 0),
        )
        batch_qty = float(position.q_batch or 1)
        base_qty = zones.replenishment_qty(
            float(nfp["nfp"]), position_zones, batch_qty
        )
        suggested = (
            zones.round_up(max(base_qty, shortage), batch_qty)
            if actionable
            else 0.0
        )
        current = existing.get(int(position.id))
        if actionable:
            action = "update" if current and current.status == signal_identity.OPEN else "open"
        elif current and current.status == signal_identity.OPEN:
            action = "cancel"
        else:
            action = "none"
        rows.append({
            "signal_type": "Пополнение",
            "position_id": int(position.id),
            "item_id": int(position.item_id),
            "item_code": item_code,
            "warehouse_ref1c": position.warehouse_ref1c,
            "dedup_key": signal_identity.build_dedup_key(
                "Пополнение",
                supermarket_position=str(position.id),
                item=item_code,
                warehouse=position.warehouse_ref1c,
            ),
            "zone": nfp["zone"],
            "priority": float(nfp["penetration"]),
            "nfp": float(nfp["nfp"]),
            "target_qty": float(position.target_qty or 0),
            "kit_force": kit_force,
            "kit_shortage_qty": shortage,
            "suggested_qty": suggested,
            "is_complete": bool(nfp["is_complete"]),
            "missing_reasons": list(nfp["missing_reasons"]),
            "action": action,
        })
    schedule_rows = _under_schedule_rows(db, schedule)
    existing_schedule = {
        row.dedup_key: row
        for row in db.query(DbrFeederSignal)
        .filter(DbrFeederSignal.signal_type == "Под график").all()
    }
    for row in schedule_rows:
        current = existing_schedule.get(row["dedup_key"])
        if row["raw_shortage_qty"] > 0 and not row["is_complete"]:
            row["action"] = "diagnostic"
        elif row["suggested_qty"] > 0:
            row["action"] = "update" if current and current.status == signal_identity.OPEN else "open"
        elif current and current.status in (signal_identity.OPEN, DIAGNOSTIC):
            row["action"] = "cancel"
        else:
            row["action"] = "none"
    rows.extend(schedule_rows)
    return {
        "schedule_id": int(schedule.id) if schedule else None,
        "positions": len(positions),
        "under_schedule_demands": len(schedule_rows),
        "actionable": sum(row["action"] in ("open", "update") for row in rows),
        "diagnostic": sum(row["action"] == "diagnostic" for row in rows),
        "rows": rows,
    }


def refresh_signals(db: Session, expected_schedule_id: Optional[int] = None) -> dict[str, Any]:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _REFRESH_LOCK})
    preview = preview_signals(db)
    if expected_schedule_id is not None and preview["schedule_id"] != expected_schedule_id:
        raise ValueError(
            f"активный график изменился: ожидался {expected_schedule_id}, получен {preview['schedule_id']}"
        )
    current = {
        row.dedup_key: row
        for row in db.query(DbrFeederSignal).with_for_update().all()
    }
    now = datetime.now()
    created = updated = reopened = cancelled = diagnostic = 0
    seen: set[str] = set()
    for data in preview["rows"]:
        position_id = data["position_id"]
        dedup_key = data["dedup_key"]
        seen.add(dedup_key)
        signal = current.get(dedup_key)
        # A signal that has already been materialized into a 1С order (Фаза 3)
        # is owned by materialize_service/feedback_service — the advisory refresh
        # must not revert its lifecycle status back to Open/Cancelled.
        if signal is not None and signal.status in (
            signal_identity.ORDER_CREATED,
            signal_identity.IN_WORK,
            signal_identity.DONE,
        ):
            continue
        actionable = data["action"] in ("open", "update")
        diagnostic_action = data["action"] == "diagnostic"
        desired = actionable or diagnostic_action
        if signal is None and not desired:
            continue
        if signal is None:
            signal = DbrFeederSignal(
                dedup_key=dedup_key,
                supermarket_position_id=position_id,
                item_id=data["item_id"],
                warehouse_ref1c=data["warehouse_ref1c"],
            )
            db.add(signal)
            created += 1
        elif actionable and signal.status in (signal_identity.CANCELLED, DIAGNOSTIC):
            reopened += 1
        elif actionable:
            updated += 1
        if actionable:
            signal.status = signal_identity.OPEN
            signal.cancelled_at = None
            signal.suggested_qty = data["suggested_qty"]
        elif diagnostic_action:
            signal.status = DIAGNOSTIC
            signal.cancelled_at = None
            signal.suggested_qty = 0
            diagnostic += 1
        else:
            if signal.status in (signal_identity.OPEN, DIAGNOSTIC):
                cancelled += 1
            signal.status = signal_identity.CANCELLED
            signal.cancelled_at = now
            signal.suggested_qty = 0
        signal.zone = data.get("zone")
        signal.priority = data["priority"]
        signal.signal_type = data["signal_type"]
        signal.nfp_snapshot = data.get("nfp")
        signal.target_qty_snapshot = data.get("target_qty")
        signal.kit_force = data.get("kit_force", False)
        signal.kit_shortage_qty = data.get("kit_shortage_qty", 0)
        signal.source_schedule_id = preview["schedule_id"]
        signal.drum_slot_id = data.get("slot_id")
        signal.need_date = data.get("need_date")
        signal.required_date = data.get("required_date")
        signal.raw_demand_qty = data.get("raw_demand_qty")
        signal.raw_shortage_qty = data.get("raw_shortage_qty")
        signal.calculated_batch_qty = data.get("calculated_batch_qty")
        signal.data_quality = data.get("data_quality", [])
        signal.is_incomplete = not data.get("is_complete", True)
        signal.reason_json = {
            "is_complete": data.get("is_complete", True),
            "missing_reasons": data.get("missing_reasons", data.get("data_quality", [])),
            "generator": "chronological_under_schedule" if data["signal_type"] == "Под график" else "bulk_live_nfp",
            "available_before": data.get("available_before"),
        }
        signal.refreshed_at = now

    # Signals whose source position/slot ceased to be active must not remain live.
    # Chain signals are owned by feeder_chain_service and never swept here.
    for dedup_key, signal in current.items():
        if signal.signal_type == "Цепочка":
            continue
        if dedup_key not in seen and signal.status in (signal_identity.OPEN, DIAGNOSTIC):
            signal.status = signal_identity.CANCELLED
            signal.suggested_qty = 0
            signal.cancelled_at = now
            signal.refreshed_at = now
            reason = "slot_or_position_not_active" if signal.signal_type == "Под график" else "position_not_active_shelf"
            signal.reason_json = {"missing_reasons": [reason], "generator": "chronological_under_schedule" if signal.signal_type == "Под график" else "bulk_live_nfp"}
            cancelled += 1
    db.flush()
    return {
        **preview, "created": created, "updated": updated, "reopened": reopened,
        "cancelled": cancelled, "diagnostic_persisted": diagnostic,
    }


def signal_out(signal: DbrFeederSignal) -> dict[str, Any]:
    return {
        "id": signal.id,
        "dedup_key": signal.dedup_key,
        "signal_type": signal.signal_type,
        "position_id": signal.supermarket_position_id,
        "item_id": signal.item_id,
        "item_code": signal.item.item_code if signal.item else None,
        "item_name": signal.item.item_name if signal.item else None,
        "warehouse_ref1c": signal.warehouse_ref1c,
        "status": signal.status,
        "suggested_qty": float(signal.suggested_qty or 0),
        "priority": float(signal.priority or 0),
        "zone": signal.zone,
        "nfp_snapshot": float(signal.nfp_snapshot) if signal.nfp_snapshot is not None else None,
        "target_qty_snapshot": float(signal.target_qty_snapshot) if signal.target_qty_snapshot is not None else None,
        "kit_force": signal.kit_force,
        "kit_shortage_qty": float(signal.kit_shortage_qty or 0),
        "parent_signal_id": signal.parent_signal_id,
        "chain_depth": int(signal.chain_depth or 0),
        "source_schedule_id": signal.source_schedule_id,
        "drum_slot_id": signal.drum_slot_id,
        "need_date": signal.need_date,
        "required_date": signal.required_date,
        "raw_demand_qty": float(signal.raw_demand_qty) if signal.raw_demand_qty is not None else None,
        "raw_shortage_qty": float(signal.raw_shortage_qty) if signal.raw_shortage_qty is not None else None,
        "calculated_batch_qty": float(signal.calculated_batch_qty) if signal.calculated_batch_qty is not None else None,
        "data_quality": signal.data_quality,
        "is_incomplete": signal.is_incomplete,
        "reason_json": signal.reason_json,
        "refreshed_at": signal.refreshed_at,
        "cancelled_at": signal.cancelled_at,
    }


_MATERIAL_KEYS = ("material_status", "kit_cls", "can_launch", "deficit_lines", "root_items")


def _material_annotations(db: Session) -> dict[int, dict[str, Any]]:
    """Material readiness per signal id, degrading to {} if DBR is unconfigured."""
    try:
        signals = feeder_material_service.live_queue(db)
        return feeder_material_service.annotate_queue(db, signals, with_roots=True)["annotations"]
    except Exception:
        return {}


def list_signals(
    db: Session, *, status: Optional[str] = None, zone: Optional[str] = None,
    search: Optional[str] = None, signal_type: Optional[str] = None,
    limit: int = 1000, offset: int = 0, include_material: bool = True,
) -> list[dict[str, Any]]:
    query = db.query(DbrFeederSignal).join(Item)
    if status:
        query = query.filter(DbrFeederSignal.status == status)
    if signal_type:
        query = query.filter(DbrFeederSignal.signal_type == signal_type)
    if zone:
        query = query.filter(func.lower(DbrFeederSignal.zone) == zone.strip().lower())
    if search:
        pattern = f"%{search.strip()}%"
        query = query.filter(or_(Item.item_code.ilike(pattern), Item.item_name.ilike(pattern)))
    rows = query.order_by(
        DbrFeederSignal.kit_force.desc(),
        DbrFeederSignal.priority.desc(),
        DbrFeederSignal.id,
    ).offset(offset).limit(limit).all()
    out = [signal_out(row) for row in rows]
    if include_material:
        annotations = _material_annotations(db)
        for row in out:
            note = annotations.get(row["id"], {})
            row["material_status"] = note.get("material_status")
            row["kit_cls"] = note.get("kit_cls")
            row["can_launch"] = bool(note.get("can_launch", False))
            row["deficit_lines"] = note.get("deficit_lines", [])
            row["root_items"] = note.get("root_items", [])
    return out


def get_signal(db: Session, signal_id: int) -> Optional[dict[str, Any]]:
    signal = db.get(DbrFeederSignal, signal_id)
    return signal_out(signal) if signal else None

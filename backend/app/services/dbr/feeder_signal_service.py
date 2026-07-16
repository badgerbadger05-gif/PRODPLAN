"""Advisory replenishment signal projection and idempotent materialization."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from ...models import (
    DbrDrumSchedule,
    DbrDrumSlot,
    DbrFeederSignal,
    DbrSupermarketPosition,
    Item,
)
from . import feeder_nfp_service
from .core.feeder import signal_identity, zones

_REFRESH_LOCK = 0x4442525349474E4C


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


def _active_schedule(db: Session) -> Optional[DbrDrumSchedule]:
    return (
        db.query(DbrDrumSchedule)
        .filter(DbrDrumSchedule.status == "active")
        .one_or_none()
    )


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
        for row in db.query(DbrFeederSignal).all()
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
    return {
        "schedule_id": int(schedule.id) if schedule else None,
        "positions": len(positions),
        "actionable": sum(row["action"] in ("open", "update") for row in rows),
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
        row.supermarket_position_id: row
        for row in db.query(DbrFeederSignal).with_for_update().all()
    }
    now = datetime.now()
    created = updated = reopened = cancelled = 0
    seen: set[int] = set()
    for data in preview["rows"]:
        position_id = data["position_id"]
        seen.add(position_id)
        signal = current.get(position_id)
        actionable = data["action"] in ("open", "update")
        if signal is None and not actionable:
            continue
        if signal is None:
            signal = DbrFeederSignal(
                dedup_key=data["dedup_key"],
                supermarket_position_id=position_id,
                item_id=data["item_id"],
                warehouse_ref1c=data["warehouse_ref1c"],
            )
            db.add(signal)
            created += 1
        elif actionable and signal.status == signal_identity.CANCELLED:
            reopened += 1
        elif actionable:
            updated += 1
        if actionable:
            signal.status = signal_identity.OPEN
            signal.cancelled_at = None
            signal.suggested_qty = data["suggested_qty"]
        else:
            if signal.status == signal_identity.OPEN:
                cancelled += 1
            signal.status = signal_identity.CANCELLED
            signal.cancelled_at = now
            signal.suggested_qty = 0
        signal.zone = data["zone"]
        signal.priority = data["priority"]
        signal.nfp_snapshot = data["nfp"]
        signal.target_qty_snapshot = data["target_qty"]
        signal.kit_force = data["kit_force"]
        signal.kit_shortage_qty = data["kit_shortage_qty"]
        signal.source_schedule_id = preview["schedule_id"]
        signal.reason_json = {
            "is_complete": data["is_complete"],
            "missing_reasons": data["missing_reasons"],
            "generator": "bulk_live_nfp",
        }
        signal.refreshed_at = now

    # A signal whose position ceased to be an active shelf must not remain live.
    for position_id, signal in current.items():
        if position_id not in seen and signal.status == signal_identity.OPEN:
            signal.status = signal_identity.CANCELLED
            signal.suggested_qty = 0
            signal.cancelled_at = now
            signal.refreshed_at = now
            signal.reason_json = {"missing_reasons": ["position_not_active_shelf"], "generator": "bulk_live_nfp"}
            cancelled += 1
    db.flush()
    return {**preview, "created": created, "updated": updated, "reopened": reopened, "cancelled": cancelled}


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
        "source_schedule_id": signal.source_schedule_id,
        "reason_json": signal.reason_json,
        "refreshed_at": signal.refreshed_at,
        "cancelled_at": signal.cancelled_at,
    }


def list_signals(
    db: Session, *, status: Optional[str] = None, zone: Optional[str] = None,
    search: Optional[str] = None, limit: int = 1000, offset: int = 0,
) -> list[dict[str, Any]]:
    query = db.query(DbrFeederSignal).join(Item)
    if status:
        query = query.filter(DbrFeederSignal.status == status)
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
    return [signal_out(row) for row in rows]


def get_signal(db: Session, signal_id: int) -> Optional[dict[str, Any]]:
    signal = db.get(DbrFeederSignal, signal_id)
    return signal_out(signal) if signal else None

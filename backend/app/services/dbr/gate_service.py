"""DBR kit-completeness gate (Frappe-free adapter, техдизайн Фазы 1 §4–§5).

Builds slot kits via the core kit walk + classifier, snapshots stock and open
replenishment in one transaction, runs kit_gate.evaluate() and writes
kit_status / shortage_json back onto pending slots in the gate horizon.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models import DbrDrumSchedule
from . import adapters
from . import classify as classify_mod
from . import settings_service
from .core.drum import kit_gate
from .core.drum.kit import build_kit

_STATUS_BUCKET = {kit_gate.GREEN: "green", kit_gate.YELLOW: "yellow", kit_gate.RED: "red"}

_GATE_LOCK = 0x44425247415445


def _resolve_schedule(db: Session, schedule_id: Optional[int]) -> Optional[DbrDrumSchedule]:
    if schedule_id is not None:
        return db.get(DbrDrumSchedule, schedule_id)
    return db.query(DbrDrumSchedule).filter(DbrDrumSchedule.status == "active").first()


def refresh_gate(db: Session, schedule_id: Optional[int] = None, today: Optional[date] = None) -> dict[str, Any]:
    """Recompute the kit gate for pending slots in the near horizon.

    Uses the active schedule when schedule_id is omitted. Returns counts plus
    the deduplicated classifier notes (disputable items).
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _GATE_LOCK})
    empty = {"updated": 0, "green": 0, "yellow": 0, "red": 0, "notes": []}
    schedule = _resolve_schedule(db, schedule_id)
    if schedule is None:
        return empty

    settings = settings_service.get_or_create_settings(db)
    today = today or date.today()
    horizon = adapters.horizon_workdays(db, today, settings.gate_horizon_workdays)

    slots = [
        s for s in schedule.slots if s.slot_date in horizon and (s.release_status or "pending") == "pending"
    ]
    slots.sort(key=lambda s: (s.slot_date, s.position, s.id))
    if not slots:
        return empty

    id_to_code, code_to_id = adapters.item_code_maps(db)
    get_components = adapters.build_components_provider(db)
    classify, notes = classify_mod.build_classifier(db, settings)

    graded: list = []
    gate_slots: list[kit_gate.GateSlot] = []
    kits: dict[str, list] = {}
    needed: set[tuple[str, str]] = set()
    for slot in slots:
        code = id_to_code.get(int(slot.item_id))
        if code is None:
            notes.append(f"slot {slot.id}: item_id {slot.item_id} не найден в номенклатуре")
            continue
        if code not in kits:
            kits[code] = build_kit(code, get_components, classify)
        for line in kits[code]:
            needed.add((line.item, line.source_warehouse))
        gate_slots.append(kit_gate.GateSlot(date=slot.slot_date, sku=code, qty=int(slot.qty)))
        graded.append(slot)

    if not gate_slots:
        return {**empty, "notes": sorted(set(notes))}

    stock = adapters.stock_snapshot_by_code(db, needed, code_to_id)
    inbound = adapters.open_inbound(db, needed, code_to_id, id_to_code)
    verdicts = kit_gate.evaluate(gate_slots, kits, stock, inbound=inbound)

    counter = {"updated": 0, "green": 0, "yellow": 0, "red": 0}
    for slot, verdict in zip(graded, verdicts, strict=True):
        status = _STATUS_BUCKET[verdict.status]
        counter[status] += 1
        shortage = (
            [
                {
                    "item": s.item,
                    "required": s.required,
                    "available": s.available,
                    "warehouse": s.warehouse,
                }
                for s in verdict.shortages
            ]
            or None
        )
        changed = False
        if (slot.kit_status or "") != status:
            slot.kit_status = status
            changed = True
        if (slot.shortage_json or None) != shortage:
            slot.shortage_json = shortage
            changed = True
        if changed:
            counter["updated"] += 1
    db.flush()
    counter["notes"] = sorted(set(notes))
    return counter

"""DBR slot operations — manual move and roll-forward.

move_slot is a local correction over the auto-leveled schedule (validated for
workday / period / past / load limit). roll_forward wraps core/rollforward.
Release/materialization (slot → 1С production order) lives in
services/dbr/materialize_service.py (Фаза 3).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy.orm import Session

from ...models import DbrAssemblyRate, DbrDrumSchedule, DbrDrumSlot
from . import adapters
from . import settings_service
from .core.drum import rollforward
from .core.drum.program_input import frozen_window

PENDING = "pending"
RELEASED = "released"
COMPLETED = "completed"

_LOAD_EPS = 1e-9


def _resolve_schedule(db: Session, schedule_id: Optional[int]) -> Optional[DbrDrumSchedule]:
    if schedule_id is not None:
        return db.get(DbrDrumSchedule, schedule_id)
    return db.query(DbrDrumSchedule).filter(DbrDrumSchedule.status == "active").first()


def move_slot(
    db: Session,
    slot_id: int,
    new_date: date,
    new_resource_id: Optional[int] = None,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Move a slot to another workday (and optionally another resource).

    Validations: slot must be pending, schedule draft/active, target is a
    workday inside the period, not in the past, not inside the frozen window,
    and the target day's load on the resource stays ≤ 1.0.
    """
    slot = db.get(DbrDrumSlot, slot_id)
    if slot is None:
        raise LookupError("slot not found")
    if slot.release_status == COMPLETED:
        raise ValueError("плитка закрыта выпуском — переносить нечего")
    if slot.release_status == RELEASED:
        raise ValueError("слот релизнут в производство — переносить нельзя")

    schedule = db.get(DbrDrumSchedule, slot.schedule_id)
    if schedule is None:
        raise LookupError("schedule not found")
    if schedule.status not in ("draft", "active"):
        raise ValueError(f"график в статусе «{schedule.status}» не редактируется")

    target_resource = new_resource_id if new_resource_id is not None else slot.resource_id
    old_date = slot.slot_date
    if new_date == old_date and target_resource == slot.resource_id:
        return {"ok": True, "moved": False}

    if target_resource != slot.resource_id:
        assigned = (
            db.query(DbrAssemblyRate.id)
            .filter(
                DbrAssemblyRate.item_id == slot.item_id,
                DbrAssemblyRate.resource_id == target_resource,
            )
            .first()
        )
        if assigned is None:
            raise ValueError(
                "изделие слота не назначено на выбранный производственный ресурс"
            )

    if not (schedule.period_from <= new_date <= schedule.period_to):
        raise ValueError(f"дата {new_date} вне периода графика {schedule.period_from}—{schedule.period_to}")

    today = today or date.today()
    if new_date < today:
        raise ValueError("переносить слот в прошлое нельзя")

    workday_dates, _fallback = adapters.load_workdays(db, schedule.period_from, schedule.period_to)
    workdays = set(workday_dates)
    if new_date not in workdays:
        raise ValueError(f"{new_date} — нерабочий день по производственному календарю")

    settings = settings_service.get_or_create_settings(db)
    frozen = frozen_window(workdays, today, settings.frozen_days)
    if old_date in frozen or new_date in frozen:
        raise ValueError(
            f"замороженная зона ({settings.frozen_days} раб. дн. от сегодня): перенос запрещён"
        )

    # Day-load check on the target resource/day.
    takts, _name_to_rid = adapters.load_sku_takts(db)
    id_to_code, _code_to_id = adapters.item_code_maps(db)
    code = id_to_code.get(int(slot.item_id))
    moved_takt = takts.get(code, ("", 0.0))[1] if code else 0.0
    if moved_takt >= 1:
        day_load = float(slot.qty or 0) / moved_takt
        others = (
            db.query(DbrDrumSlot)
            .filter(
                DbrDrumSlot.schedule_id == slot.schedule_id,
                DbrDrumSlot.resource_id == target_resource,
                DbrDrumSlot.slot_date == new_date,
                DbrDrumSlot.id != slot.id,
            )
            .all()
        )
        for other in others:
            other_code = id_to_code.get(int(other.item_id))
            other_takt = takts.get(other_code, ("", 0.0))[1] if other_code else 0.0
            if other_takt >= 1:
                day_load += float(other.qty or 0) / other_takt
        if day_load > 1.0 + _LOAD_EPS:
            raise ValueError(
                f"не хватает мощности: нагрузка на {new_date} станет {round(day_load * 100)}% (лимит 100%)"
            )

    slot.slot_date = new_date
    slot.resource_id = target_resource
    # Date/resource changed → prior gate verdict is stale.
    slot.kit_status = "unknown"
    slot.shortage_json = None
    db.flush()
    return {"ok": True, "moved": True, "from": str(old_date), "to": str(new_date)}


def roll_forward(db: Session, schedule_id: Optional[int] = None, today: Optional[date] = None) -> dict[str, Any]:
    """Move unfinished past-due pending slots to the right (core/rollforward).

    Released slots stay put (their order owns the dates); planned_date never
    changes. Slots already produced-out are closed (completed).
    """
    schedule = _resolve_schedule(db, schedule_id)
    if schedule is None:
        return {"moved": 0, "closed": 0, "overloaded": 0, "no_schedule": True}

    today = today or date.today()
    if today > schedule.period_to:
        return {"moved": 0, "closed": 0, "overloaded": 0, "horizon_exhausted": True}

    closed = 0
    for slot in schedule.slots:
        if slot.release_status != COMPLETED and rollforward.is_closed(int(slot.qty or 0), float(slot.produced_qty or 0)):
            slot.release_status = COMPLETED
            closed += 1

    workday_dates, _fallback = adapters.load_workdays(db, today, schedule.period_to)
    workdays = [d for d in workday_dates if d >= today]
    if not workdays:
        return {"moved": 0, "closed": closed, "overloaded": 0, "no_workdays": True}

    takts, _name_to_rid = adapters.load_sku_takts(db)
    id_to_code, _code_to_id = adapters.item_code_maps(db)
    resource_names = adapters.resource_name_map(db)

    overdue: list[rollforward.OverdueSlot] = []
    load_by_day: dict[tuple[str, date], float] = {}
    slot_by_name: dict[str, DbrDrumSlot] = {}
    for slot in schedule.slots:
        if rollforward.is_closed(int(slot.qty or 0), float(slot.produced_qty or 0)):
            continue
        rest = rollforward.remaining_qty(int(slot.qty or 0), float(slot.produced_qty or 0))
        if not rest:
            continue
        code = id_to_code.get(int(slot.item_id)) or ""
        ws = resource_names.get(int(slot.resource_id)) or ""
        movable = slot.slot_date < today and slot.release_status == PENDING
        if movable:
            slot_by_name[str(slot.id)] = slot
            overdue.append(
                rollforward.OverdueSlot(
                    name=str(slot.id),
                    planned_date=slot.planned_date or slot.slot_date,
                    workstation=ws,
                    item=code,
                    remaining_qty=rest,
                )
            )
        elif slot.slot_date >= today:
            takt = takts.get(code, ("", 0.0))[1]
            key = (ws, slot.slot_date)
            load_by_day[key] = load_by_day.get(key, 0.0) + rollforward.slot_load(rest, takt)

    if not overdue:
        return {"moved": 0, "closed": closed, "overloaded": 0}

    moves = rollforward.plan_rollforward(overdue, workdays, takts, load_by_day)
    moved = overloaded = 0
    for move in moves:
        slot = slot_by_name.get(move.name)
        if slot is None:
            continue
        slot.slot_date = move.to_date
        slot.kit_status = "unknown"
        slot.shortage_json = None
        moved += 1
        if move.overloaded:
            overloaded += 1
    db.flush()
    return {"moved": moved, "closed": closed, "overloaded": overloaded}

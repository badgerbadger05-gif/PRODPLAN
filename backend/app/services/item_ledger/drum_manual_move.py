"""Validated operator correction of one persisted canonical drum tile."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app import models
from app.services import planning_truth
from app.services.work_calendar_service import is_workday


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def move_drum_slot(
    db: Session,
    slot_id: int,
    new_date: date,
    *,
    new_resource_id: int | None = None,
    moved_by: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Move a current accepted tile without changing queue priority or readiness.

    The move is an explicit operator correction of calendar position.  It may
    not cross an assembly resource because point-of-use readiness is bound to
    that resource's warehouse.  Capacity and the readiness date remain hard
    gates.
    """
    truth = planning_truth.require_accepted_truth(
        db,
        "drum_manual_move",
        required_capabilities=(
            planning_truth.CAPABILITY_PHYSICAL_LEDGER,
            planning_truth.CAPABILITY_ASSEMBLY_QUEUE,
            planning_truth.CAPABILITY_DRUM_SCHEDULE,
        ),
    )
    slot = db.get(models.DrumSlot, int(slot_id))
    if slot is None:
        raise LookupError("плитка барабана не найдена")
    schedule = db.get(models.DrumSchedule, int(slot.drum_schedule_id))
    if schedule is None or int(schedule.ledger_generation_id) != int(truth.generation_id):
        raise ValueError("перемещать можно только плитки текущего принятого барабана")

    target_resource = int(new_resource_id or slot.resource_id)
    if target_resource != int(slot.resource_id):
        raise ValueError(
            "плитка привязана к участку и его складу потребления; "
            "между участками её переносить нельзя"
        )
    if not (schedule.schedule_from <= new_date <= schedule.schedule_to):
        raise ValueError(
            f"дата {new_date.isoformat()} вне горизонта барабана "
            f"{schedule.schedule_from.isoformat()}—{schedule.schedule_to.isoformat()}"
        )
    if new_date < (today or date.today()):
        raise ValueError("переносить плитку в прошлое нельзя")
    if not is_workday(db, new_date):
        raise ValueError(f"{new_date.isoformat()} — нерабочий день")
    if slot.readiness_date is not None and new_date < slot.readiness_date:
        raise ValueError(
            "плитка ещё не обеспечена: ближайшая дата готовности "
            f"{slot.readiness_date.isoformat()}"
        )

    old_date = slot.slot_date
    if old_date == new_date:
        return {
            "ok": True,
            "moved": False,
            "slot_id": int(slot.id),
            "from_date": old_date.isoformat(),
            "to_date": new_date.isoformat(),
            "resource_id": target_resource,
        }

    rate = (
        db.query(models.AssemblyRate)
        .filter(
            models.AssemblyRate.item_id == int(slot.item_id),
            models.AssemblyRate.resource_id == target_resource,
        )
        .one_or_none()
    )
    resource = db.get(models.ProductionResource, target_resource)
    if rate is None or resource is None or _d(rate.qty_per_capacity) <= 0:
        raise ValueError("для плитки не настроен однозначный положительный такт участка")
    capacity = _d(resource.capacity)
    if capacity <= 0:
        raise ValueError("для участка не настроена положительная мощность")

    resource_slots = (
        db.query(models.DrumSlot)
        .filter(
            models.DrumSlot.drum_schedule_id == int(schedule.id),
            models.DrumSlot.resource_id == target_resource,
        )
        .order_by(
            models.DrumSlot.slot_date,
            models.DrumSlot.slot_ordinal,
            models.DrumSlot.id,
        )
        .all()
    )
    item_ids = {int(row.item_id) for row in resource_slots}
    rate_rows = (
        db.query(models.AssemblyRate)
        .filter(
            models.AssemblyRate.resource_id == target_resource,
            models.AssemblyRate.item_id.in_(sorted(item_ids)),
        )
        .all()
    )
    rate_by_item = {int(row.item_id): _d(row.qty_per_capacity) for row in rate_rows}
    if any(rate_by_item.get(item_id, Decimal("0")) <= 0 for item_id in item_ids):
        raise ValueError("не удалось проверить загрузку участка: отсутствует такт изделия")
    # A drop means insertion into the resource timeline, not stacking the tile
    # on top of an already full day.  Repack the target day and its tail in the
    # existing order around the tile pinned to its requested date, carrying
    # overflow to later workdays and using the vacancy left at the source.
    # Earlier unaffected days and every domain quantity remain untouched.
    repack_from = min(old_date, new_date)
    tail = [
        row for row in resource_slots
        if int(row.id) != int(slot.id) and row.slot_date >= repack_from
    ]
    ordered_tail = [slot, *tail]
    placements: dict[int, date] = {int(slot.id): new_date}
    current = repack_from

    def next_workday(candidate: date) -> date:
        day = candidate
        while day <= schedule.schedule_to and not is_workday(db, day):
            day += timedelta(days=1)
        return day

    slot_load = _d(slot.slot_qty) / rate_by_item[int(slot.item_id)]
    if slot_load > capacity + Decimal("0.0000001"):
        raise ValueError(
            f"плитка #{int(slot.id)} целиком не помещается в дневную мощность участка"
        )
    used_by_day: dict[date, Decimal] = {new_date: slot_load}

    for row in tail:
        rate_value = rate_by_item[int(row.item_id)]
        load = _d(row.slot_qty) / rate_value
        if load > capacity + Decimal("0.0000001"):
            raise ValueError(
                f"плитка #{int(row.id)} целиком не помещается в дневную мощность участка"
            )
        earliest = max(repack_from, row.readiness_date or repack_from)
        if current < earliest:
            current = earliest
        current = next_workday(current)
        while (
            current <= schedule.schedule_to
            and used_by_day.get(current, Decimal("0")) + load
            > capacity + Decimal("0.0000001")
        ):
            current = next_workday(current + timedelta(days=1))
        if current > schedule.schedule_to:
            raise ValueError("вставка сдвигает плитки за горизонт барабана")
        placements[int(row.id)] = current
        used_by_day[current] = used_by_day.get(current, Decimal("0")) + load

    stamp = datetime.now(timezone.utc)
    actor = str(moved_by or "operator").strip()[:100] or "operator"
    for row in ordered_tail:
        placed = placements[int(row.id)]
        if row.slot_date == placed:
            continue
        if row.auto_slot_date is None:
            row.auto_slot_date = row.slot_date
        if row.auto_resource_id is None:
            row.auto_resource_id = int(row.resource_id)
        row.slot_date = placed
        row.manual_moved_at = stamp
        row.manual_moved_by = actor
    db.commit()
    return {
        "ok": True,
        "moved": True,
        "slot_id": int(slot.id),
        "from_date": old_date.isoformat(),
        "to_date": new_date.isoformat(),
        "resource_id": target_resource,
        "manual_moved_at": slot.manual_moved_at.isoformat(),
        "manual_moved_by": slot.manual_moved_by,
    }

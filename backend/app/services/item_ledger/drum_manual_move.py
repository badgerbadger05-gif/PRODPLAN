"""Validated operator correction of one persisted canonical drum tile."""

from __future__ import annotations

from datetime import date, datetime, timezone
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

    target_slots = (
        db.query(models.DrumSlot)
        .filter(
            models.DrumSlot.drum_schedule_id == int(schedule.id),
            models.DrumSlot.resource_id == target_resource,
            models.DrumSlot.slot_date == new_date,
            models.DrumSlot.id != int(slot.id),
        )
        .all()
    )
    item_ids = {int(row.item_id) for row in target_slots} | {int(slot.item_id)}
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
    target_load = _d(slot.slot_qty) / rate_by_item[int(slot.item_id)]
    for other in target_slots:
        target_load += _d(other.slot_qty) / rate_by_item[int(other.item_id)]
    if target_load > capacity + Decimal("0.0000001"):
        percent = (target_load / capacity * Decimal("100")).quantize(Decimal("1"))
        raise ValueError(
            f"не хватает мощности: загрузка участка на {new_date.isoformat()} "
            f"станет {percent}%"
        )

    if slot.auto_slot_date is None:
        slot.auto_slot_date = old_date
    if slot.auto_resource_id is None:
        slot.auto_resource_id = int(slot.resource_id)
    slot.slot_date = new_date
    slot.manual_moved_at = datetime.now(timezone.utc)
    slot.manual_moved_by = str(moved_by or "operator").strip()[:100] or "operator"
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

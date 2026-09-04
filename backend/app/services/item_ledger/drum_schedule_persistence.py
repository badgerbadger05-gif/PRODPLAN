"""Canonical persisted assembly queue and deterministic drum schedule."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any

from sqlalchemy.orm import Session

from app import models
from app.services.work_calendar_service import is_workday

from .assembly_queue_snapshot import materialize_assembly_queue_lines
from .assembly_readiness_persistence import materialize_assembly_readiness
from .drum_scheduler import AssemblyRateProfile, QueueLine, build_drum_plan


STAGE = "drum_schedule"
ALGORITHM_VERSION = "drum-schedule/4-readiness-curve"


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


_READINESS_RANK = {"now": 0, "transfer": 1, "kitting": 2, "committed": 3, "launch": 4}


def _action_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        json.dumps(row.get(name), sort_keys=True, default=str)
        for name in (
            "action_kind", "item_id", "available_date", "confidence", "source_key",
            "source_warehouse_ref1c", "destination_warehouse_ref1c", "resource_id", "path",
        )
    )


def _slot_readiness_payload(
    readiness: models.AssemblyReadiness,
    phase: str,
    slot_qty: Decimal,
) -> tuple[date | None, list[dict[str, Any]], list[dict[str, Any]]]:
    curve = list(readiness.readiness_curve or [])
    phase_rank = _READINESS_RANK.get(str(phase), 99)
    tile_curve = [
        {
            "horizon": str(point.get("horizon") or ""),
            "cumulative_qty": str(slot_qty if _READINESS_RANK.get(str(point.get("horizon")), 99) >= phase_rank else Decimal("0")),
            "available_date": point.get("available_date"),
        }
        for point in curve
    ]
    if phase_rank >= 99:
        return None, tile_curve, []
    current_index = next(
        (index for index, point in enumerate(curve) if str(point.get("horizon")) == phase),
        None,
    )
    if current_index is None:
        return None, tile_curve, []
    current = curve[current_index]
    previous = curve[current_index - 1] if current_index > 0 else {"cumulative_qty": "0", "actions": []}
    increment = max(_d(current.get("cumulative_qty")) - _d(previous.get("cumulative_qty")), Decimal("0"))
    if increment <= 0:
        return (
            date.fromisoformat(str(current["available_date"])) if current.get("available_date") else None,
            tile_curve,
            [],
        )
    previous_qty = {_action_key(row): _d(row.get("qty")) for row in list(previous.get("actions") or [])}
    ratio = slot_qty / increment
    actions: list[dict[str, Any]] = []
    for row in list(current.get("actions") or []):
        delta = max(_d(row.get("qty")) - previous_qty.get(_action_key(row), Decimal("0")), Decimal("0"))
        if delta <= 0:
            continue
        payload = dict(row)
        payload["qty"] = str((delta * ratio).quantize(Decimal("0.001")))
        actions.append(payload)
    return (
        date.fromisoformat(str(current["available_date"])) if current.get("available_date") else None,
        tile_curve,
        actions,
    )


def _rates_and_capacity(
    db: Session, queue_rows: list[models.AssemblyQueueLine]
) -> tuple[
    dict[int, tuple[AssemblyRateProfile, ...]],
    dict[int, Decimal],
    int,
    dict[int, int],
]:
    item_ids = sorted({int(row.item_id) for row in queue_rows})
    rate_rows = (
        db.query(models.AssemblyRate)
        .filter(models.AssemblyRate.item_id.in_(item_ids))
        .order_by(models.AssemblyRate.item_id, models.AssemblyRate.resource_id)
        .all()
        if item_ids
        else []
    )
    rates: dict[int, list[AssemblyRateProfile]] = {}
    for row in rate_rows:
        rates.setdefault(int(row.item_id), []).append(
            AssemblyRateProfile(
                resource_id=int(row.resource_id),
                qty_per_capacity=_d(row.qty_per_capacity),
            )
        )
    normalized = {item_id: tuple(values) for item_id, values in rates.items()}
    for item_id in item_ids:
        profiles = normalized.get(item_id, ())
        # A missing rate means that the queue row is outside the drum contour.
        # It remains in AssemblyQueueLine and can still be closed by an accepted
        # physical output.  Ambiguous or invalid configured rates remain a hard
        # data error: silently choosing between two resources would be unsafe.
        if len(profiles) > 1:
            raise ValueError(f"ambiguous assembly rate for item {item_id}")
        if profiles and _d(profiles[0].qty_per_capacity) <= 0:
            raise ValueError(f"invalid assembly rate for item {item_id}")

    resource_ids = sorted(
        {profile.resource_id for values in normalized.values() for profile in values}
    )
    resources = (
        db.query(models.ProductionResource)
        .filter(models.ProductionResource.resource_id.in_(resource_ids))
        .all()
        if resource_ids
        else []
    )
    capacity = {int(row.resource_id): _d(row.capacity) for row in resources}
    if set(resource_ids) != set(capacity):
        raise ValueError("assembly rate references missing production resource")
    # Every resource keeps its own planning range; the schedule window is the
    # widest of them, but a resource never spills demand past its own horizon.
    horizon_by_resource = {
        int(row.resource_id): max(int(row.planning_range or 0), 1) for row in resources
    }
    horizon = max(list(horizon_by_resource.values()) or [1])
    return normalized, capacity, horizon, horizon_by_resource


def _plan(
    db: Session,
    generation: models.LedgerGeneration,
    queue_rows: list[models.AssemblyQueueLine],
):
    rates, capacity, horizon, horizon_by_resource = _rates_and_capacity(db, queue_rows)
    readiness_rows = (
        db.query(models.AssemblyReadiness)
        .filter(models.AssemblyReadiness.ledger_generation_id == int(generation.id))
        .all()
    )
    readiness_by_line = {
        int(row.assembly_queue_line_id): row for row in readiness_rows
    }
    if set(readiness_by_line) != {int(row.id) for row in queue_rows}:
        raise ValueError("assembly readiness does not cover the open assembly queue")
    scheduled_rows = [row for row in queue_rows if int(row.item_id) in rates]
    excluded_rows = [row for row in queue_rows if int(row.item_id) not in rates]
    schedule_from = generation.cutoff.date()
    schedule_to = schedule_from + timedelta(days=horizon - 1)
    resource_horizon_end = {
        resource_id: schedule_from + timedelta(days=days - 1)
        for resource_id, days in horizon_by_resource.items()
    }
    calendar: dict[date, bool] = {}
    cursor = schedule_from
    while cursor <= schedule_to:
        calendar[cursor] = is_workday(db, cursor)
        cursor += timedelta(days=1)
    plan = build_drum_plan(
        tuple(
            QueueLine(
                queue_line_id=int(row.id),
                plan_id=int(row.plan_id),
                plan_line_id=int(row.plan_line_id),
                item_id=int(row.item_id),
                sort_key=row.sort_key,
                planned_output_qty=_d(row.planned_output_qty),
                accepted_plan_output_qty=_d(row.accepted_plan_output_qty),
                original_priority=tuple(row.original_priority or ()),
                ready_qty=_d(readiness_by_line[int(row.id)].ready_qty),
                readiness_status=str(readiness_by_line[int(row.id)].status),
                readiness_curve=tuple(
                    (
                        str(point.get("horizon") or ""),
                        _d(point.get("cumulative_qty")),
                        date.fromisoformat(str(point["available_date"]))
                        if point.get("available_date") else None,
                    )
                    for point in list(readiness_by_line[int(row.id)].readiness_curve or [])
                ),
            )
            for row in scheduled_rows
        ),
        rates,
        calendar,
        schedule_from=schedule_from,
        schedule_to=schedule_to,
        resource_capacity_by_id=capacity,
        resource_horizon_end_by_id=resource_horizon_end,
    )
    excluded_open_qty = sum(
        (_d(row.assembly_remaining_qty) for row in excluded_rows), Decimal("0")
    )
    return type(plan)(
        schedule_from=plan.schedule_from,
        schedule_to=plan.schedule_to,
        slots=plan.slots,
        gaps=plan.gaps,
        queue_signature=plan.queue_signature,
        slot_signature=plan.slot_signature,
        gap_signature=plan.gap_signature,
        metrics={
            **dict(plan.metrics),
            "queue_lines": len(queue_rows),
            "excluded_lines": len(excluded_rows),
            "excluded_open_qty": str(excluded_open_qty),
            "excluded_item_ids": sorted({int(row.item_id) for row in excluded_rows}),
        },
    )


def _validate_persisted_checkpoint(
    db: Session,
    schedule: models.DrumSchedule,
    batch: models.LedgerBuildBatch,
) -> None:
    slot_count = db.query(models.DrumSlot).filter(
        models.DrumSlot.drum_schedule_id == int(schedule.id)
    ).count()
    gap_count = db.query(models.DrumCapacityGap).filter(
        models.DrumCapacityGap.drum_schedule_id == int(schedule.id)
    ).count()
    if (
        schedule.status != "completed"
        or batch.status != "completed"
        or schedule.algorithm_version != ALGORITHM_VERSION
        or batch.algorithm_version != ALGORITHM_VERSION
        or slot_count != int(schedule.slot_row_count)
        or gap_count != int(schedule.gap_row_count)
        or dict(batch.metrics or {}) != dict(schedule.metrics or {})
        or _d(schedule.total_open_qty)
        != _d(schedule.total_slot_qty) + _d(schedule.total_gap_qty)
    ):
        raise ValueError("persisted drum checkpoint is incomplete or inconsistent")


def materialize_drum_schedule(
    db: Session, ledger_generation_id: int
) -> dict[str, Any]:
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise ValueError(f"LedgerGeneration {ledger_generation_id} not found")
    if generation.status != "building":
        raise ValueError("drum schedule requires a BUILDING generation")
    if generation.cutoff is None:
        raise ValueError("drum schedule requires generation cutoff")

    existing = (
        db.query(models.DrumSchedule)
        .filter(models.DrumSchedule.ledger_generation_id == int(generation.id))
        .one_or_none()
    )
    batch_key = f"g{int(generation.id)}:{STAGE}:{ALGORITHM_VERSION}"
    batch = (
        db.query(models.LedgerBuildBatch)
        .filter(
            models.LedgerBuildBatch.ledger_generation_id == int(generation.id),
            models.LedgerBuildBatch.stage == STAGE,
            models.LedgerBuildBatch.batch_key == batch_key,
        )
        .one_or_none()
    )
    if existing is not None or batch is not None:
        if existing is None or batch is None:
            raise ValueError("partial drum checkpoint exists")
        _validate_persisted_checkpoint(db, existing, batch)
        return {
            "ledger_generation_id": int(generation.id),
            "schedule_id": int(existing.id),
            "batch_id": int(batch.id),
            **dict(existing.metrics or {}),
        }

    queue_rows = [
        row
        for row in materialize_assembly_queue_lines(db, int(generation.id))
        if _d(row.assembly_remaining_qty) > 0
    ]
    materialize_assembly_readiness(db, int(generation.id))
    plan = _plan(db, generation, queue_rows)

    schedule = models.DrumSchedule(
        ledger_generation_id=int(generation.id),
        status="completed",
        algorithm_version=ALGORITHM_VERSION,
        schedule_from=plan.schedule_from,
        schedule_to=plan.schedule_to,
        queue_signature=plan.queue_signature,
        slot_signature=plan.slot_signature,
        gap_signature=plan.gap_signature,
        slot_row_count=len(plan.slots),
        gap_row_count=len(plan.gaps),
        total_open_qty=_d(plan.metrics["total_open_qty"]),
        total_slot_qty=_d(plan.metrics["total_slot_qty"]),
        total_gap_qty=_d(plan.metrics["total_gap_qty"]),
        metrics=dict(plan.metrics),
    )
    db.add(schedule)
    db.flush()
    readiness_by_line = {
        int(row.assembly_queue_line_id): row
        for row in db.query(models.AssemblyReadiness)
        .filter(models.AssemblyReadiness.ledger_generation_id == int(generation.id))
        .all()
    }
    for slot in plan.slots:
        readiness = readiness_by_line[int(slot.queue_line_id)]
        readiness_date, tile_curve, tile_actions = _slot_readiness_payload(
            readiness, slot.readiness_phase, slot.slot_qty
        )
        db.add(
            models.DrumSlot(
                drum_schedule_id=int(schedule.id),
                assembly_queue_line_id=int(slot.queue_line_id),
                plan_id=int(slot.plan_id),
                plan_line_id=int(slot.plan_line_id),
                item_id=int(slot.item_id),
                resource_id=int(slot.resource_id),
                slot_date=slot.slot_date,
                auto_slot_date=slot.slot_date,
                auto_resource_id=int(slot.resource_id),
                slot_qty=slot.slot_qty,
                planned_output_qty=slot.planned_output_qty,
                slot_ordinal=int(slot.slot_ordinal),
                original_priority=list(slot.original_priority),
                readiness_phase=slot.readiness_phase,
                readiness_date=readiness_date,
                readiness_curve=tile_curve,
                action_manifest=tile_actions,
                unavailable_reasons=list(readiness.unavailable_reasons or []),
                blocking_manifest=(
                    list(readiness.blocking_manifest or [])
                    if slot.readiness_phase in {"blocked", "unavailable"}
                    else []
                ),
            )
        )
    for gap in plan.gaps:
        db.add(
            models.DrumCapacityGap(
                drum_schedule_id=int(schedule.id),
                assembly_queue_line_id=int(gap.queue_line_id),
                plan_id=int(gap.plan_id),
                plan_line_id=int(gap.plan_line_id),
                item_id=int(gap.item_id),
                resource_id=int(gap.resource_id),
                gap_date=gap.gap_date,
                required_qty=gap.required_qty,
                available_capacity=gap.available_capacity,
                gap_qty=gap.gap_qty,
                original_priority=list(gap.original_priority),
                readiness_phase=gap.readiness_phase,
            )
        )
    batch = models.LedgerBuildBatch(
        ledger_generation_id=int(generation.id),
        stage=STAGE,
        batch_key=batch_key,
        status="completed",
        algorithm_version=ALGORITHM_VERSION,
        metrics=dict(plan.metrics),
        completed_at=datetime.now(timezone.utc),
    )
    db.add(batch)
    db.flush()
    return {
        "ledger_generation_id": int(generation.id),
        "schedule_id": int(schedule.id),
        "batch_id": int(batch.id),
        **dict(plan.metrics),
    }

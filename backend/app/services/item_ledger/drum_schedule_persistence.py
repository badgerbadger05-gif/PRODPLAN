"""Canonical persisted assembly queue and deterministic drum schedule."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app import models
from app.services.work_calendar_service import is_workday

from .drum_scheduler import AssemblyRateProfile, QueueLine, build_drum_plan
from .live_plan_scope import live_plan_run_ids


STAGE = "drum_schedule"
ALGORITHM_VERSION = "drum-schedule/1"


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def _priority(period_from: date, period_to: date, plan_id: int, line_id: int) -> list[Any]:
    return [period_from.isoformat(), period_to.isoformat(), int(plan_id), int(line_id)]


def _sort_key(period_from: date, period_to: date, plan_id: int, line_id: int) -> str:
    return (
        f"{period_from.isoformat()}|{period_to.isoformat()}|"
        f"{int(plan_id):010d}|{int(line_id):010d}"
    )


def _source_rows(db: Session, generation_id: int) -> list[dict[str, Any]]:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise ValueError(f"LedgerGeneration {generation_id} not found")
    run_ids = live_plan_run_ids(db, generation)
    allocations = (
        db.query(
            models.AssemblyOutputAllocation.plan_line_id.label("plan_line_id"),
            func.sum(models.AssemblyOutputAllocation.allocated_qty).label("accepted_qty"),
        )
        .filter(
            models.AssemblyOutputAllocation.ledger_generation_id == int(generation_id)
        )
        .group_by(models.AssemblyOutputAllocation.plan_line_id)
        .subquery()
    )
    rows = (
        db.query(
            models.ProductionPlanLine,
            models.ProductionPlanHeader,
            models.PlanningRun,
            func.coalesce(allocations.c.accepted_qty, 0),
        )
        .join(
            models.ProductionPlanHeader,
            models.ProductionPlanHeader.id == models.ProductionPlanLine.plan_id,
        )
        .join(
            models.PlanningRun,
            and_(
                models.PlanningRun.source_plan_id == models.ProductionPlanHeader.id,
                models.PlanningRun.run_id.in_(run_ids),
            ),
        )
        .outerjoin(
            allocations,
            allocations.c.plan_line_id == models.ProductionPlanLine.id,
        )
        .filter(
            models.ProductionPlanHeader.status == "fixed",
            models.ProductionPlanLine.qty > 0,
        )
        .order_by(
            models.PlanningRun.period_from.asc(),
            models.PlanningRun.period_to.asc(),
            models.ProductionPlanHeader.id.asc(),
            models.ProductionPlanLine.id.asc(),
        )
        .all()
    )
    result: list[dict[str, Any]] = []
    for line, plan, run, accepted in rows:
        planned = _d(line.qty)
        accepted_qty = _d(accepted)
        remaining = max(planned - accepted_qty, Decimal("0"))
        if remaining == 0:
            continue
        period_from = run.period_from or plan.period_from
        period_to = run.period_to or plan.period_to
        result.append(
            {
                "planning_run_id": int(run.run_id),
                "plan_id": int(plan.id),
                "plan_line_id": int(line.id),
                "item_id": int(line.item_id),
                "bucket_date": line.bucket_date,
                "period_from": period_from,
                "period_to": period_to,
                "planned_output_qty": planned,
                "accepted_plan_output_qty": accepted_qty,
                "assembly_remaining_qty": remaining,
                "original_priority": _priority(
                    period_from, period_to, int(plan.id), int(line.id)
                ),
                "sort_key": _sort_key(
                    period_from, period_to, int(plan.id), int(line.id)
                ),
            }
        )
    return result


def _assert_queue_matches(
    source: list[dict[str, Any]], persisted: list[models.AssemblyQueueLine]
) -> None:
    by_line = {int(row.plan_line_id): row for row in persisted}
    if set(by_line) != {int(row["plan_line_id"]) for row in source}:
        raise ValueError("persisted assembly queue differs from fixed plan")
    for expected in source:
        actual = by_line[int(expected["plan_line_id"])]
        for field in (
            "planning_run_id",
            "plan_id",
            "item_id",
            "bucket_date",
            "period_from",
            "period_to",
            "sort_key",
        ):
            if getattr(actual, field) != expected[field]:
                raise ValueError("persisted assembly queue differs from fixed plan")
        for field in (
            "planned_output_qty",
            "accepted_plan_output_qty",
            "assembly_remaining_qty",
        ):
            if _d(getattr(actual, field)) != _d(expected[field]):
                raise ValueError("persisted assembly queue differs from fixed plan")


def _persist_queue(
    db: Session, generation_id: int, source: list[dict[str, Any]]
) -> list[models.AssemblyQueueLine]:
    existing = (
        db.query(models.AssemblyQueueLine)
        .filter(models.AssemblyQueueLine.ledger_generation_id == int(generation_id))
        .order_by(models.AssemblyQueueLine.sort_key.asc())
        .all()
    )
    if existing:
        _assert_queue_matches(source, existing)
        return existing
    rows = [
        models.AssemblyQueueLine(
            ledger_generation_id=int(generation_id),
            line_status="open",
            **payload,
        )
        for payload in source
    ]
    db.add_all(rows)
    db.flush()
    return rows


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
        if len(normalized.get(item_id, ())) != 1:
            reason = "missing" if item_id not in normalized else "ambiguous"
            raise ValueError(f"{reason} assembly rate for item {item_id}")

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
    return build_drum_plan(
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
            )
            for row in queue_rows
        ),
        rates,
        calendar,
        schedule_from=schedule_from,
        schedule_to=schedule_to,
        resource_capacity_by_id=capacity,
        resource_horizon_end_by_id=resource_horizon_end,
    )


def _assert_schedule_matches(
    db: Session, schedule: models.DrumSchedule, plan
) -> None:
    slots = (
        db.query(models.DrumSlot)
        .filter(models.DrumSlot.drum_schedule_id == int(schedule.id))
        .all()
    )
    gaps = (
        db.query(models.DrumCapacityGap)
        .filter(models.DrumCapacityGap.drum_schedule_id == int(schedule.id))
        .all()
    )
    if (
        schedule.queue_signature != plan.queue_signature
        or schedule.slot_signature != plan.slot_signature
        or schedule.gap_signature != plan.gap_signature
        or len(slots) != len(plan.slots)
        or len(gaps) != len(plan.gaps)
        or _d(schedule.total_open_qty) != _d(plan.metrics["total_open_qty"])
        or _d(schedule.total_slot_qty) != _d(plan.metrics["total_slot_qty"])
        or _d(schedule.total_gap_qty) != _d(plan.metrics["total_gap_qty"])
    ):
        raise ValueError("persisted drum schedule drifted from canonical input")


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

    source = _source_rows(db, int(generation.id))
    queue_rows = _persist_queue(db, int(generation.id), source)
    plan = _plan(db, generation, queue_rows)
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
        _assert_schedule_matches(db, existing, plan)
        return {
            "ledger_generation_id": int(generation.id),
            "schedule_id": int(existing.id),
            "batch_id": int(batch.id),
            **dict(existing.metrics or {}),
        }

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
    for slot in plan.slots:
        db.add(
            models.DrumSlot(
                drum_schedule_id=int(schedule.id),
                assembly_queue_line_id=int(slot.queue_line_id),
                plan_id=int(slot.plan_id),
                plan_line_id=int(slot.plan_line_id),
                item_id=int(slot.item_id),
                resource_id=int(slot.resource_id),
                slot_date=slot.slot_date,
                slot_qty=slot.slot_qty,
                planned_output_qty=slot.planned_output_qty,
                slot_ordinal=int(slot.slot_ordinal),
                original_priority=list(slot.original_priority),
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

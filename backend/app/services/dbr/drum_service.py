"""DBR drum schedule — build / activate / extend (Frappe-free adapter).

Wraps the leveling core: production program → program_input → leveling.level →
slots + capacity gaps persisted into dbr_drum_* in one transaction. Semantics
mirror prodflow drum/schedule_service but read PRODPLAN tables via adapters.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models import (
    DbrDrumCapacityGap,
    DbrDrumSchedule,
    DbrDrumScheduleProgram,
    DbrDrumSlot,
    DbrProductionProgram,
)
from . import adapters
from . import settings_service
from .core.drum.leveling import level
from .core.drum.program_input import (
    ProgramRow,
    build_volumes,
    months_in_period,
    workdays_by_month,
)

DRAFT = "draft"
ACTIVE = "active"
SUPERSEDED = "superseded"
CANCELLED = "cancelled"

_ACTIVATE_ADVISORY_LOCK = 0x4442524143544956

_SNAPSHOT_FIELDS = (
    "frozen_days",
    "gate_horizon_workdays",
    "shelf_threshold_qty",
    "feeder_chain_enabled",
    "w2_warehouse_ref1c",
    "w3_warehouse_ref1c",
    "w4_warehouse_ref1c",
    "fastener_categories",
)


def _config_snapshot(settings) -> dict[str, Any]:
    snap: dict[str, Any] = {}
    for field in _SNAPSHOT_FIELDS:
        value = getattr(settings, field, None)
        # Decimals -> float so the JSON column round-trips cleanly.
        if hasattr(value, "__float__") and not isinstance(value, (bool, int)):
            value = float(value)
        snap[field] = value
    return snap


def _level_program(db: Session, program: DbrProductionProgram):
    """Level one program to (LevelingResult, name→resource_id, code→item_id, carried, fallback)."""
    takts, name_to_rid = adapters.load_sku_takts(db)
    if not takts:
        raise ValueError("«Настройки сборки» (dbr_assembly_rate) пусты — выравнивать нечем")
    id_to_code, code_to_id = adapters.item_code_maps(db)

    rows: list[ProgramRow] = []
    missing: list[str] = []
    for item in program.items:
        code = id_to_code.get(int(item.item_id))
        if code is None or item.qty is None or float(item.qty) <= 0:
            continue
        if code not in takts:
            missing.append(code)
            continue
        rows.append(
            ProgramRow(item_code=code, qty=float(item.qty), program_date=item.program_date, family=code)
        )
    if missing:
        raise ValueError(
            "Изделия не назначены на участок в «Настройках сборки»: " + ", ".join(sorted(set(missing)))
        )

    out_of_period = [r for r in rows if not (program.from_date <= r.program_date <= program.to_date)]
    if out_of_period:
        listed = ", ".join(
            f"{r.item_code} ({r.program_date})"
            for r in sorted(out_of_period, key=lambda r: (r.program_date, r.item_code))
        )
        raise ValueError(f"Строки программы вне периода {program.from_date}—{program.to_date}: {listed}")

    volumes = build_volumes(rows)
    months = months_in_period(program.from_date, program.to_date)
    workday_dates, fallback = adapters.load_workdays(db, program.from_date, program.to_date)
    workdays = workdays_by_month(months, workday_dates)

    result = level(volumes, workdays, takts)
    carried = [{"item_code": sku, "qty": qty} for (_group, sku), qty in sorted(result.carried_over.items())]
    return result, name_to_rid, code_to_id, carried, fallback


def _append_result(db: Session, schedule: DbrDrumSchedule, result, program_id, name_to_rid, code_to_id) -> int:
    # Continue day positions after any existing slots (extend appends).
    pos_by_day: dict[Any, int] = {}
    for existing in schedule.slots:
        pos_by_day[existing.slot_date] = max(pos_by_day.get(existing.slot_date, -1), int(existing.position))

    added = 0
    for slot in result.slots:
        rid = name_to_rid.get(slot.workstation)
        iid = code_to_id.get(slot.sku)
        if rid is None or iid is None:
            continue
        pos = pos_by_day.get(slot.date, -1) + 1
        pos_by_day[slot.date] = pos
        # Append through the relationship so schedule.slots stays fresh in-session
        # (extend idempotency + positions read it right after appending).
        schedule.slots.append(
            DbrDrumSlot(
                slot_date=slot.date,
                planned_date=slot.date,
                resource_id=rid,
                item_id=iid,
                qty=slot.qty,
                produced_qty=0,
                kit_status="unknown",
                release_status="pending",
                source_program_id=program_id,
                position=pos,
            )
        )
        added += 1

    for gap in result.gaps:
        schedule.capacity_gaps.append(
            DbrDrumCapacityGap(
                gap_date=gap.date,
                resource_id=name_to_rid.get(gap.workstation),
                item_id=code_to_id.get(gap.family),
                required_qty=gap.required_qty,
                takt_qty=gap.takt_qty,
                gap_qty=gap.gap_qty,
            )
        )
    return added


def build_schedule(db: Session, program_id: int) -> tuple[DbrDrumSchedule, dict[str, Any]]:
    """Build a Draft drum schedule from an approved program.

    Returns (schedule, meta) where meta carries carried_over + calendar
    fallback flag (transient, not persisted — mirrors prodflow's _carried_over).
    """
    program = db.get(DbrProductionProgram, program_id)
    if program is None:
        raise LookupError("program not found")
    if program.status != "approved":
        raise ValueError("барабан строится только из утверждённой программы (статус «approved»)")
    if not program.items:
        raise ValueError("в программе нет строк")

    result, name_to_rid, code_to_id, carried, fallback = _level_program(db, program)
    settings = settings_service.get_or_create_settings(db)

    config_snapshot = _config_snapshot(settings)
    config_snapshot["calendar_fallback"] = fallback
    schedule = DbrDrumSchedule(
        period_from=program.from_date,
        period_to=program.to_date,
        source_program_id=program.id,
        status=DRAFT,
        config_snapshot=config_snapshot,
    )
    db.add(schedule)
    db.flush()
    db.add(DbrDrumScheduleProgram(schedule_id=schedule.id, program_id=program.id))
    added = _append_result(db, schedule, result, program.id, name_to_rid, code_to_id)
    db.flush()
    return schedule, {"slots_added": added, "carried_over": carried, "calendar_fallback": fallback}


def activate(db: Session, schedule_id: int) -> DbrDrumSchedule:
    """Make a schedule Active; any other Active schedule becomes Superseded."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _ACTIVATE_ADVISORY_LOCK},
        )
    schedule = (
        db.query(DbrDrumSchedule)
        .filter(DbrDrumSchedule.id == schedule_id)
        .with_for_update()
        .one_or_none()
    )
    if schedule is None:
        raise LookupError("schedule not found")
    if schedule.status in (SUPERSEDED, CANCELLED):
        raise ValueError(f"график в статусе «{schedule.status}» активировать нельзя")
    for other in (
        db.query(DbrDrumSchedule)
        .filter(DbrDrumSchedule.status == ACTIVE, DbrDrumSchedule.id != schedule.id)
        .with_for_update()
        .all()
    ):
        other.status = SUPERSEDED
    schedule.status = ACTIVE
    db.flush()
    return schedule


def extend(db: Session, schedule_id: int, program_id: int) -> tuple[DbrDrumSchedule, dict[str, Any]]:
    """Extend a rolling drum with the next month's approved program (variant A).

    Idempotent on source_program_id: re-extending with the same program adds
    nothing (extended=False).
    """
    schedule = (
        db.query(DbrDrumSchedule)
        .filter(DbrDrumSchedule.id == schedule_id)
        .with_for_update()
        .one_or_none()
    )
    if schedule is None:
        raise LookupError("schedule not found")
    if schedule.status not in (DRAFT, ACTIVE):
        raise ValueError(f"график в статусе «{schedule.status}» не продлевается")

    program = db.get(DbrProductionProgram, program_id)
    if program is None:
        raise LookupError("program not found")
    if program.status != "approved":
        raise ValueError("продлевать можно только утверждённой программой")
    if not program.items:
        raise ValueError("в программе нет строк")

    covered = (
        db.query(DbrDrumScheduleProgram.id)
        .filter(
            DbrDrumScheduleProgram.schedule_id == schedule.id,
            DbrDrumScheduleProgram.program_id == program_id,
        )
        .first()
    )
    # Compatibility for schedules extended before the coverage-marker table
    # existed: their slots still carry source_program_id.
    covered_by_legacy_slot = any(
        slot.source_program_id == program_id for slot in schedule.slots
    )
    if covered is not None or covered_by_legacy_slot:
        return schedule, {"extended": False, "reason": "already_covered", "slots_added": 0, "carried_over": []}

    result, name_to_rid, code_to_id, carried, fallback = _level_program(db, program)
    config_snapshot = dict(schedule.config_snapshot or {})
    config_snapshot["calendar_fallback"] = bool(
        config_snapshot.get("calendar_fallback") or fallback
    )
    schedule.config_snapshot = config_snapshot
    db.add(DbrDrumScheduleProgram(schedule_id=schedule.id, program_id=program.id))
    db.flush()
    added = _append_result(db, schedule, result, program.id, name_to_rid, code_to_id)
    if program.to_date > schedule.period_to:
        schedule.period_to = program.to_date
    db.flush()
    return schedule, {
        "extended": True,
        "slots_added": added,
        "carried_over": carried,
        "calendar_fallback": fallback,
        "period_to": schedule.period_to,
    }

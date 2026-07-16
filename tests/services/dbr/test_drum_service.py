"""Tests for DBR drum build / activate / extend (drum_service.py)."""

from datetime import date
from decimal import Decimal

import pytest

from app.models import (
    DbrAssemblyRate,
    DbrDrumSchedule,
    DbrDrumSlot,
    Item,
    ProductionResource,
    WorkCalendarDay,
)
from app.services.dbr import drum_service, program_service


def _setup_assembly(db, code="SLED", qty_per_capacity=10, capacity=1):
    res = ProductionResource(resource_name="Сборка снегоходов", capacity=capacity)
    item = Item(item_code=code, item_name=code)
    db.add_all([res, item])
    db.flush()
    db.add(DbrAssemblyRate(resource_id=res.resource_id, item_id=item.item_id, qty_per_capacity=qty_per_capacity))
    db.flush()
    return res, item


def _workweek(db, start=date(2026, 8, 3), days=5):
    # Mon..Fri workdays (explicit calendar avoids fallback ambiguity).
    for i in range(days):
        db.add(WorkCalendarDay(date=date(start.year, start.month, start.day + i), is_workday=True))
    db.flush()


def _approved_program(db, item, qty, day=date(2026, 8, 3)):
    program = program_service.create_program(
        db,
        from_date=date(2026, 8, 1),
        to_date=date(2026, 8, 31),
        items=[{"item_id": item.item_id, "program_date": day, "qty": Decimal(qty)}],
    )
    db.flush()
    program_service.approve_program(db, program.id)
    return program


def test_build_schedule_creates_slots(db_session):
    db = db_session
    res, item = _setup_assembly(db)
    _workweek(db)
    program = _approved_program(db, item, 20)

    schedule, meta = drum_service.build_schedule(db, program.id)
    db.commit()

    assert schedule.status == "draft"
    slots = db.query(DbrDrumSlot).filter(DbrDrumSlot.schedule_id == schedule.id).all()
    assert slots, "expected drum slots"
    assert sum(int(s.qty) for s in slots) == 20
    assert all(s.resource_id == res.resource_id for s in slots)
    assert all(s.release_status == "pending" and s.kit_status == "unknown" for s in slots)
    assert schedule.config_snapshot is not None
    assert meta["slots_added"] == len(slots)


def test_build_requires_approved_program(db_session):
    db = db_session
    res, item = _setup_assembly(db)
    _workweek(db)
    program = program_service.create_program(
        db,
        from_date=date(2026, 8, 1),
        to_date=date(2026, 8, 31),
        items=[{"item_id": item.item_id, "program_date": date(2026, 8, 3), "qty": Decimal("5")}],
    )
    db.flush()
    with pytest.raises(ValueError):
        drum_service.build_schedule(db, program.id)


def test_build_rejects_unassigned_item(db_session):
    db = db_session
    res, item = _setup_assembly(db)
    _workweek(db)
    other = Item(item_code="NO-TAKT", item_name="No takt")
    db.add(other)
    db.flush()
    program = program_service.create_program(
        db,
        from_date=date(2026, 8, 1),
        to_date=date(2026, 8, 31),
        items=[{"item_id": other.item_id, "program_date": date(2026, 8, 3), "qty": Decimal("5")}],
    )
    db.flush()
    program_service.approve_program(db, program.id)
    with pytest.raises(ValueError, match="не назначены"):
        drum_service.build_schedule(db, program.id)


def test_activate_supersedes_previous(db_session):
    db = db_session
    res, item = _setup_assembly(db)
    _workweek(db)
    p1 = _approved_program(db, item, 10)
    s1, _ = drum_service.build_schedule(db, p1.id)
    drum_service.activate(db, s1.id)
    db.flush()

    p2 = _approved_program(db, item, 10)
    s2, _ = drum_service.build_schedule(db, p2.id)
    drum_service.activate(db, s2.id)
    db.commit()

    assert db.get(DbrDrumSchedule, s1.id).status == "superseded"
    assert db.get(DbrDrumSchedule, s2.id).status == "active"


def test_extend_is_idempotent(db_session):
    db = db_session
    res, item = _setup_assembly(db)
    _workweek(db)
    p1 = _approved_program(db, item, 10)
    schedule, _ = drum_service.build_schedule(db, p1.id)
    drum_service.activate(db, schedule.id)
    db.flush()

    p2 = _approved_program(db, item, 6)
    _, meta1 = drum_service.extend(db, schedule.id, p2.id)
    assert meta1["extended"] is True and meta1["slots_added"] > 0
    n_after = db.query(DbrDrumSlot).filter(DbrDrumSlot.schedule_id == schedule.id).count()

    _, meta2 = drum_service.extend(db, schedule.id, p2.id)
    assert meta2["extended"] is False
    assert db.query(DbrDrumSlot).filter(DbrDrumSlot.schedule_id == schedule.id).count() == n_after

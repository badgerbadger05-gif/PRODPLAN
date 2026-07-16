"""Tests for DBR drum build / activate / extend (drum_service.py)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import (
    DbrAssemblyRate,
    DbrDrumCapacityGap,
    DbrDrumSchedule,
    DbrDrumScheduleProgram,
    DbrDrumSlot,
    DbrProductionProgram,
    Item,
    ProductionResource,
    WorkCalendarDay,
)
from app.database import Base
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

    p2 = _approved_program(db, item, 300)
    _, meta1 = drum_service.extend(db, schedule.id, p2.id)
    assert meta1["extended"] is True and meta1["slots_added"] > 0
    n_after = db.query(DbrDrumSlot).filter(DbrDrumSlot.schedule_id == schedule.id).count()
    gaps_after = (
        db.query(DbrDrumCapacityGap)
        .filter(DbrDrumCapacityGap.schedule_id == schedule.id)
        .count()
    )
    assert gaps_after > 0

    _, meta2 = drum_service.extend(db, schedule.id, p2.id)
    assert meta2["extended"] is False
    assert db.query(DbrDrumSlot).filter(DbrDrumSlot.schedule_id == schedule.id).count() == n_after
    assert (
        db.query(DbrDrumCapacityGap)
        .filter(DbrDrumCapacityGap.schedule_id == schedule.id)
        .count()
        == gaps_after
    )
    assert (
        db.query(DbrDrumScheduleProgram)
        .filter_by(schedule_id=schedule.id, program_id=p2.id)
        .count()
        == 1
    )


def test_database_rejects_two_active_schedules_across_independent_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'active-invariant.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    first = session_factory()
    second = session_factory()
    try:
        first.add(
            DbrDrumSchedule(
                period_from=date(2026, 8, 1),
                period_to=date(2026, 8, 31),
                status="active",
            )
        )
        first.commit()
        second.add(
            DbrDrumSchedule(
                period_from=date(2026, 9, 1),
                period_to=date(2026, 9, 30),
                status="active",
            )
        )
        with pytest.raises(IntegrityError):
            second.commit()
        second.rollback()
        assert first.query(DbrDrumSchedule).filter_by(status="active").count() == 1
    finally:
        first.close()
        second.close()
        engine.dispose()


def test_database_rejects_duplicate_schedule_program_marker(db_session):
    program = DbrProductionProgram(
        from_date=date(2026, 8, 1),
        to_date=date(2026, 8, 31),
        status="approved",
    )
    schedule = DbrDrumSchedule(
        period_from=program.from_date,
        period_to=program.to_date,
        status="draft",
    )
    db_session.add_all([program, schedule])
    db_session.flush()
    db_session.add(
        DbrDrumScheduleProgram(schedule_id=schedule.id, program_id=program.id)
    )
    db_session.flush()
    db_session.add(
        DbrDrumScheduleProgram(schedule_id=schedule.id, program_id=program.id)
    )

    with pytest.raises(IntegrityError):
        db_session.flush()

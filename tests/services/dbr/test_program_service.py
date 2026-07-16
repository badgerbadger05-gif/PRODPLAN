"""Tests for DBR production program CRUD + approve (program_service.py)."""

from datetime import date
from decimal import Decimal

import pytest

from app.models import DbrProductionProgram, Item
from app.services.dbr import program_service


def _item(db, code="SLED"):
    it = Item(item_code=code, item_name=code)
    db.add(it)
    db.flush()
    return it


def test_create_and_get_program(db_session):
    db = db_session
    it = _item(db)
    program = program_service.create_program(
        db,
        from_date=date(2026, 8, 1),
        to_date=date(2026, 8, 31),
        title="Август",
        items=[{"item_id": it.item_id, "program_date": date(2026, 8, 3), "qty": Decimal("10")}],
    )
    db.commit()
    fetched = program_service.get_program(db, program.id)
    assert fetched.status == "draft"
    assert len(fetched.items) == 1
    assert float(fetched.items[0].qty) == 10.0


def test_create_rejects_inverted_period(db_session):
    with pytest.raises(ValueError):
        program_service.create_program(db_session, from_date=date(2026, 8, 31), to_date=date(2026, 8, 1))


def test_update_replaces_items_only_in_draft(db_session):
    db = db_session
    it = _item(db)
    program = program_service.create_program(
        db,
        from_date=date(2026, 8, 1),
        to_date=date(2026, 8, 31),
        items=[{"item_id": it.item_id, "program_date": date(2026, 8, 3), "qty": Decimal("5")}],
    )
    db.flush()
    program_service.update_program(
        db,
        program.id,
        {"title": "new", "items": [{"item_id": it.item_id, "program_date": date(2026, 8, 4), "qty": Decimal("9")}]},
    )
    db.flush()
    assert program.title == "new"
    assert len(program.items) == 1
    assert float(program.items[0].qty) == 9.0


def test_approve_requires_items(db_session):
    db = db_session
    program = program_service.create_program(db, from_date=date(2026, 8, 1), to_date=date(2026, 8, 31))
    db.flush()
    with pytest.raises(ValueError):
        program_service.approve_program(db, program.id)


def test_approve_and_lock_editing(db_session):
    db = db_session
    it = _item(db)
    program = program_service.create_program(
        db,
        from_date=date(2026, 8, 1),
        to_date=date(2026, 8, 31),
        items=[{"item_id": it.item_id, "program_date": date(2026, 8, 3), "qty": Decimal("5")}],
    )
    db.flush()
    program_service.approve_program(db, program.id)
    assert db.get(DbrProductionProgram, program.id).status == "approved"
    with pytest.raises(ValueError):
        program_service.update_program(db, program.id, {"title": "x"})


def test_approve_is_idempotent(db_session):
    db = db_session
    it = _item(db)
    program = program_service.create_program(
        db,
        from_date=date(2026, 8, 1),
        to_date=date(2026, 8, 31),
        items=[{"item_id": it.item_id, "program_date": date(2026, 8, 3), "qty": Decimal("5")}],
    )
    db.flush()
    program_service.approve_program(db, program.id)
    program_service.approve_program(db, program.id)  # no raise
    assert program.status == "approved"

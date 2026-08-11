"""Schema contract for canonical reservation-consumption allocation table."""

import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, inspect

from app import models
from app.database import Base


def test_reservation_consumption_allocation_metadata_contract():
    table = models.ReservationConsumptionAllocation.__table__
    assert table.name == "reservation_consumption_allocation"

    nullable = {column.name for column in table.columns if column.nullable}
    assert "sle_id" not in nullable
    assert "item_id" not in nullable
    assert "reservation_id" not in nullable
    assert "ledger_generation_id" not in nullable
    assert "requirement_id" not in nullable
    assert "allocated_qty" not in nullable
    assert "match_rule" not in nullable
    assert "idempotency_key" not in nullable

    assert not table.c.ledger_generation_id.nullable
    assert not table.c.reservation_id.nullable
    assert not table.c.requirement_id.nullable
    assert not table.c.allocated_qty.nullable
    assert not table.c.match_rule.nullable
    assert not table.c.idempotency_key.nullable
    assert not table.c.sle_id.nullable
    assert not table.c.item_id.nullable

    foreign_keys = {
        (foreign_key.parent.name, foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in table.foreign_keys
    }
    assert ("ledger_generation_id", "ledger_generation.id", "RESTRICT") in foreign_keys
    assert ("reservation_id", "reservation_entry.id", "RESTRICT") in foreign_keys
    assert ("sle_id", "stock_ledger_entry.id", "RESTRICT") in foreign_keys
    assert ("requirement_id", "mrp_requirement.id", "RESTRICT") in foreign_keys
    assert ("item_id", "items.item_id", "RESTRICT") in foreign_keys

    unique = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("ledger_generation_id", "idempotency_key") in unique
    assert ("ledger_generation_id", "sle_id", "reservation_id") in unique

    checks = {
        constraint.name
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert {
        "ck_reservation_consumption_allocation_match_rule",
        "ck_reservation_consumption_allocation_qty_positive",
    } <= checks

    indexes = {index.name for index in table.indexes}
    assert {
        "ix_reservation_consumption_allocation_generation",
        "ix_reservation_consumption_allocation_reservation",
        "ix_reservation_consumption_allocation_requirement",
        "ix_reservation_consumption_allocation_sle",
    } <= indexes


def test_reservation_consumption_allocation_sqlite_schema_has_constraints():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    checks = {row["name"] for row in inspector.get_check_constraints("reservation_consumption_allocation")}
    assert "ck_reservation_consumption_allocation_match_rule" in checks
    assert "ck_reservation_consumption_allocation_qty_positive" in checks


def test_reservation_consumption_allocation_migration_follows_schema_head():
    path = (
        Path(__file__).resolve().parents[2]
        / "backend/alembic/versions/20260731_09_add_reservation_consumption_allocation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "reservation_consumption_allocation_migration", path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.revision == "20260731_09"
    assert module.down_revision == "20260731_08"

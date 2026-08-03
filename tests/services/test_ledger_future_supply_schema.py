"""Schema contract for immutable generation-scoped future supply."""

import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, inspect

from app import models
from app.database import Base


def test_ledger_future_supply_metadata_contract():
    table = models.LedgerFutureSupply.__table__
    assert table.name == "ledger_future_supply"

    nullable_identity = {column.name for column in table.columns if column.nullable}
    assert {"source_ref", "source_line_ref", "source_local_id", "eta_date", "source_updated_at", "reason"} <= nullable_identity
    assert {"source_requirement_id"} <= nullable_identity
    assert not table.c.ledger_generation_id.nullable
    assert not table.c.capture_batch_id.nullable
    assert not table.c.capture_cutoff.nullable

    foreign_keys = {
        (foreign_key.parent.name, foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in table.foreign_keys
    }
    assert ("ledger_generation_id", "ledger_generation.id", "RESTRICT") in foreign_keys
    assert ("capture_batch_id", "ledger_build_batch.id", "RESTRICT") in foreign_keys
    assert ("item_id", "items.item_id", None) in foreign_keys
    assert ("source_requirement_id", "mrp_requirement.id", "RESTRICT") in foreign_keys

    unique = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("ledger_generation_id", "supply_kind", "source_ref", "source_line_ref") in unique

    checks = {constraint.name for constraint in table.constraints if constraint.__class__.__name__ == "CheckConstraint"}
    assert {
        "ck_ledger_future_supply_kind",
        "ck_ledger_future_supply_evidence_status",
        "ck_ledger_future_supply_quantities_nonnegative",
        "ck_ledger_future_supply_capture_cutoff",
    } <= checks

    indexes = {index.name for index in table.indexes}
    assert {
        "ix_ledger_future_supply_generation_kind_item_eta",
        "ix_ledger_future_supply_generation_item_eta",
        "ix_ledger_future_supply_source_requirement_id",
    } <= indexes


def test_ledger_future_supply_sqlite_schema_has_constraints():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    checks = {row["name"] for row in inspector.get_check_constraints("ledger_future_supply")}
    assert "ck_ledger_future_supply_quantities_nonnegative" in checks
    assert "ck_ledger_future_supply_evidence_status" in checks


def test_ledger_future_supply_migration_follows_shared_head():
    path = (
        Path(__file__).resolve().parents[2]
        / "backend/alembic/versions/20260731_07_add_source_requirement_to_ledger_future_supply.py"
    )
    spec = importlib.util.spec_from_file_location("ledger_future_supply_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.revision == "20260731_07"
    assert module.down_revision == "20260731_06"

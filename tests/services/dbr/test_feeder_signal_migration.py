from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.database import Base
from app.models import DbrFeederSignal, DbrSettings


def test_orm_schema_has_signal_constraints_and_safe_settings_default():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    columns = {row["name"]: row for row in inspect(engine).get_columns("dbr_feeder_signal")}
    unique = {tuple(row["column_names"]) for row in inspect(engine).get_unique_constraints("dbr_feeder_signal")}
    assert ("dedup_key",) in unique
    assert columns["kit_force"]["nullable"] is False
    assert columns["priority"]["nullable"] is False
    assert columns["drum_slot_id"]["nullable"] is True
    assert columns["need_date"]["nullable"] is True
    assert columns["required_date"]["nullable"] is True
    assert columns["raw_demand_qty"]["nullable"] is True
    assert columns["raw_shortage_qty"]["nullable"] is True
    assert columns["data_quality"]["nullable"] is False
    assert columns["is_incomplete"]["nullable"] is False
    assert columns["calculated_batch_qty"]["nullable"] is True
    type_checks = " ".join(
        str(row.get("sqltext", ""))
        for row in inspect(engine).get_check_constraints("dbr_feeder_signal")
        if row.get("name") == "ck_dbr_feeder_signal_type"
    )
    assert "Пополнение" in type_checks and "Под график" in type_checks
    status_checks = " ".join(
        str(row.get("sqltext", ""))
        for row in inspect(engine).get_check_constraints("dbr_feeder_signal")
        if row.get("name") == "ck_dbr_feeder_signal_status"
    )
    assert "Diagnostic" in status_checks

    session = Session(engine)
    settings = DbrSettings(id=1)
    session.add(settings)
    session.commit()
    assert settings.feeder_chain_enabled is False


def test_default_migration_never_rewrites_existing_settings_rows():
    migration = Path("backend/alembic/versions/20260717_04_add_dbr_feeder_signals.py").read_text()
    normalized = migration.upper()
    assert "ALTER_COLUMN" in normalized
    assert "UPDATE DBR_SETTINGS" not in normalized
    assert 'SERVER_DEFAULT=SA.FALSE()' in normalized


def test_diagnostic_migration_neutralizes_existing_incomplete_open_rows():
    migration = Path(
        "backend/alembic/versions/20260717_06_diagnostic_feeder_signals.py"
    ).read_text()
    normalized = " ".join(migration.split())
    assert "calculated_batch_qty = COALESCE(calculated_batch_qty, suggested_qty)" in normalized
    assert "suggested_qty = 0, status = 'Diagnostic'" in normalized
    assert "is_incomplete = true AND status = 'Open'" in normalized

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

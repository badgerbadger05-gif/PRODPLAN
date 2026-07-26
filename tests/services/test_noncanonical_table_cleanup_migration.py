from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION = (
    Path(__file__).parents[2]
    / "backend/alembic/versions/20260726_04_drop_noncanonical_planning_tables.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("cleanup_noncanonical_tables", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_drops_only_retired_tables(monkeypatch) -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table("items", metadata, sa.Column("id", sa.Integer, primary_key=True))
    migration = _load_migration()
    for table_name in migration.TABLES_IN_DROP_ORDER:
        sa.Table(table_name, metadata, sa.Column("id", sa.Integer, primary_key=True))
    metadata.create_all(engine)

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()
        remaining = set(sa.inspect(connection).get_table_names())

    assert "items" in remaining
    assert not (set(migration.TABLES_IN_DROP_ORDER) & remaining)


def test_downgrade_requires_database_backup() -> None:
    migration = _load_migration()
    with pytest.raises(RuntimeError, match="cannot be downgraded"):
        migration.downgrade()

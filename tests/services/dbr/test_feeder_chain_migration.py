"""Task C: diagnostic (06) and chain (07) migrations must upgrade on SQLite too.

These migrations swap named CHECK constraints and (07) relax a NOT NULL column —
operations Postgres does with ALTER but SQLite cannot. The fix routes them through
batch_alter_table on non-Postgres. Here we actually run the upgrades against an
in-memory SQLite database seeded with the post-05 schema and assert they apply.
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


_VERSIONS = Path("backend/alembic/versions")

_PRE06_DDL = """
CREATE TABLE dbr_feeder_signal (
    id INTEGER NOT NULL PRIMARY KEY,
    dedup_key VARCHAR(66) NOT NULL,
    signal_type VARCHAR(30) NOT NULL DEFAULT 'Пополнение',
    supermarket_position_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    warehouse_ref1c VARCHAR(36) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'Open',
    suggested_qty NUMERIC(16, 3) NOT NULL DEFAULT 0,
    is_incomplete BOOLEAN NOT NULL DEFAULT 0,
    CONSTRAINT ck_dbr_feeder_signal_status CHECK (status IN ('Open', 'Cancelled')),
    CONSTRAINT ck_dbr_feeder_signal_type CHECK (signal_type IN ('Пополнение', 'Под график'))
)
"""


def _load(filename):
    path = _VERSIONS / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(module, connection):
    ctx = MigrationContext.configure(connection)
    module.op = Operations(ctx)
    module.upgrade()


def test_migrations_06_and_07_upgrade_on_sqlite():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(_PRE06_DDL)

        _run_upgrade(_load("20260717_06_diagnostic_feeder_signals.py"), connection)
        columns = {c["name"] for c in sa.inspect(connection).get_columns("dbr_feeder_signal")}
        assert "calculated_batch_qty" in columns
        # Diagnostic status was rejected before, must be accepted now.
        connection.exec_driver_sql(
            "INSERT INTO dbr_feeder_signal "
            "(dedup_key, signal_type, supermarket_position_id, item_id, warehouse_ref1c, status, suggested_qty) "
            "VALUES ('S:x', 'Под график', 1, 1, 'W', 'Diagnostic', 0)"
        )

        _run_upgrade(_load("20260717_07_add_feeder_chain_signals.py"), connection)
        columns = {
            c["name"]: c for c in sa.inspect(connection).get_columns("dbr_feeder_signal")
        }
        assert "parent_signal_id" in columns and "chain_depth" in columns
        assert columns["supermarket_position_id"]["nullable"] is True
        # A chain signal has no shelf position and a widened type.
        connection.exec_driver_sql(
            "INSERT INTO dbr_feeder_signal "
            "(dedup_key, signal_type, supermarket_position_id, item_id, warehouse_ref1c, status, suggested_qty, chain_depth) "
            "VALUES ('C:x', 'Цепочка', NULL, 1, 'W', 'Open', 5, 1)"
        )
        rows = connection.exec_driver_sql(
            "SELECT signal_type FROM dbr_feeder_signal WHERE dedup_key = 'C:x'"
        ).fetchall()
        assert rows == [("Цепочка",)]

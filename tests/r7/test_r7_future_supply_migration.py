"""Focused tests for the R7 future-supply identity backfill."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text


def _migration():
    path = (
        Path(__file__).parents[2]
        / "backend/alembic/versions/20260910_10_r7_stable_future_supply.py"
    )
    spec = spec_from_file_location("r7_stable_future_supply", path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_postgresql_backfill_is_validation_plus_one_set_based_update():
    migration = _migration()

    class Result:
        def first(self):
            return None

    class Bind:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self):
            self.calls = []

        def execute(self, statement, params=None):
            self.calls.append((str(statement), params))
            return Result()

    bind = Bind()
    migration._backfill_postgresql(bind, 937)

    assert len(bind.calls) == 2
    validation_sql, validation_params = bind.calls[0]
    update_sql, update_params = bind.calls[1]
    assert validation_params == update_params == {"pointer_id": 937}
    assert "GROUP BY business_identity" in validation_sql
    assert "ledger_generation_id = :pointer_id" in validation_sql
    assert "UPDATE ledger_future_supply AS lfs" in update_sql
    assert "FROM identities" in update_sql
    assert ".mappings().all" not in validation_sql + update_sql


def test_postgresql_backfill_stops_before_update_on_current_collision():
    migration = _migration()

    class Result:
        def first(self):
            return ("collision", 12, "supplier_order:ORDER:1:")

    class Bind:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self):
            self.calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            return Result()

    bind = Bind()
    with pytest.raises(RuntimeError, match="active identity collision"):
        migration._backfill_postgresql(bind, 937)
    assert bind.calls == 1


def test_compatibility_backfill_keeps_identity_formula_and_current_pointer():
    migration = _migration()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE ledger_future_supply ("
            "id INTEGER PRIMARY KEY, ledger_generation_id INTEGER NOT NULL, "
            "supply_kind VARCHAR(32), source_ref VARCHAR(64), "
            "source_line_ref VARCHAR(64), source_local_id VARCHAR(128), "
            "source_content_hash VARCHAR(64), evidence_status VARCHAR(16), "
            "current_identity VARCHAR(256), is_current BOOLEAN)"
        )
        conn.execute(
            text(
                "INSERT INTO ledger_future_supply "
                "(id,ledger_generation_id,supply_kind,source_ref,source_line_ref,"
                "source_local_id,source_content_hash,evidence_status) VALUES "
                "(1,10,'supplier_order',' ORDER ',' 1 ',NULL,'x','exact'),"
                "(2,10,'wip_order',NULL,NULL,NULL,' hash ','rejected'),"
                "(3,11,'supplier_order','ORDER','1',NULL,'y','exact')"
            )
        )
        conn.exec_driver_sql(
            "CREATE TABLE ledger_generation (id INTEGER PRIMARY KEY, status VARCHAR(16))"
        )
        conn.execute(text(
            "INSERT INTO ledger_generation(id,status) VALUES (10,'accepted'),(11,'building')"
        ))

        migration._backfill_compat(conn, 10)
        rows = conn.execute(
            text(
                "SELECT id,current_identity,is_current FROM ledger_future_supply "
                "ORDER BY id"
            )
        ).all()

    assert rows == [
        (1, "supplier_order:ORDER:1:", True),
        (2, "wip_order:rejected:hash", True),
        (3, "supplier_order:ORDER:1:", False),
    ]

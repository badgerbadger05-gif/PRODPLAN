"""Shape tests for bounded R7 current-owner migration."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


def _migration():
    path = (
        Path(__file__).parents[2]
        / "backend/alembic/versions/20260910_11_r7_future_supply_current_owner.py"
    )
    spec = spec_from_file_location("r7_current_owner", path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, *, scalar=None, first=None):
        self._scalar = scalar
        self._first = first

    def scalar(self):
        return self._scalar

    def first(self):
        return self._first


class _Bind:
    def __init__(self, *, pointer=937, validation=None):
        self.pointer = pointer
        self.validation = validation
        self.calls = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append(sql)
        if "SELECT current_generation_id" in sql:
            return _Result(scalar=self.pointer)
        if "GROUP BY lfs.current_identity" in sql:
            return _Result(first=self.validation)
        return _Result()


def test_backfill_scopes_insert_to_accepted_pointer_and_prunes_only_after_insert():
    migration = _migration()
    bind = _Bind()

    migration._backfill_current_owner(bind)

    insert_index = next(i for i, sql in enumerate(bind.calls) if "INSERT INTO ledger_future_supply_current" in sql)
    delete_index = next(i for i, sql in enumerate(bind.calls) if "DELETE FROM ledger_future_supply" in sql)
    assert insert_index < delete_index
    assert "pts.current_generation_id" in bind.calls[insert_index]
    assert "lg.status = 'accepted'" in bind.calls[insert_index]
    assert "lg.status <> 'building'" in bind.calls[delete_index]
    assert ".mappings().all" not in "\n".join(bind.calls)


def test_backfill_fails_closed_before_insert_or_prune_on_collision():
    migration = _migration()
    bind = _Bind(validation=("supplier_order:X:1", 2, 20))

    with pytest.raises(RuntimeError, match="empty/too-long/colliding"):
        migration._backfill_current_owner(bind)

    assert not any("INSERT INTO ledger_future_supply_current" in sql for sql in bind.calls)
    assert not any("DELETE FROM ledger_future_supply" in sql for sql in bind.calls)


def test_backfill_without_truth_pointer_preserves_legacy_rows():
    migration = _migration()
    bind = _Bind(pointer=None)

    migration._backfill_current_owner(bind)

    assert any("UPDATE ledger_future_supply" in sql for sql in bind.calls)
    assert not any("INSERT INTO ledger_future_supply_current" in sql for sql in bind.calls)
    assert not any("DELETE FROM ledger_future_supply" in sql for sql in bind.calls)

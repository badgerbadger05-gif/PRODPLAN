"""RED contract for the final removal of the legacy planning-read tables."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app import models


REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS = REPO_ROOT / "backend" / "alembic" / "versions"
LEGACY_TABLES = {
    "planning_read_snapshot",
    "planning_read_row",
    "planning_read_root_member",
}
LIVE_SCOPES = {
    "production_control_journal": "production:all-live-orders",
    "purchase_control_journal": "purchase:all-live-plans",
    "mrp_result": "mrp:all-live-plans",
    "period_plan_execution": "period-plan:all-live-plans",
}


def _migration_path() -> Path:
    matches = sorted(VERSIONS.glob("20260911_03_*.py"))
    assert len(matches) == 1, (
        "R10 final PlanningRead drop migration is missing or ambiguous; "
        "expected exactly one 20260911_03_*.py"
    )
    return matches[0]


def _migration():
    path = _migration_path()
    spec = importlib.util.spec_from_file_location("r10_planning_read_drop", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(module, connection, name: str = "upgrade"):
    context = MigrationContext.configure(connection)
    module.op = Operations(context)
    return getattr(module, name)()


def _legacy_schema(connection, *, external_fk: bool = False) -> None:
    connection.execute(sa.text(
        "CREATE TABLE ledger_generation ("
        "id INTEGER PRIMARY KEY, status VARCHAR(32) NOT NULL, cutoff DATETIME)"
    ))
    connection.execute(sa.text(
        "CREATE TABLE planning_truth_state ("
        "id INTEGER PRIMARY KEY, current_generation_id INTEGER)"
    ))
    connection.execute(sa.text(
        "CREATE TABLE current_execution_scope ("
        "id INTEGER PRIMARY KEY, entity_kind VARCHAR(128) NOT NULL, "
        "scope_key VARCHAR(256) NOT NULL, source_generation_id INTEGER, "
        "source_revision VARCHAR(256), result_ready BOOLEAN, summary JSON)"
    ))
    connection.execute(sa.text(
        "CREATE TABLE planning_read_snapshot ("
        "id INTEGER PRIMARY KEY, consumer VARCHAR(128) NOT NULL, "
        "snapshot_key VARCHAR(256) NOT NULL, ledger_generation_id INTEGER NOT NULL, "
        "truth_status VARCHAR(16) NOT NULL, payload JSON NOT NULL)"
    ))
    connection.execute(sa.text(
        "CREATE TABLE planning_read_row ("
        "id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, row_key VARCHAR(256) NOT NULL, "
        "payload JSON NOT NULL, FOREIGN KEY(snapshot_id) REFERENCES planning_read_snapshot(id))"
    ))
    connection.execute(sa.text(
        "CREATE TABLE planning_read_root_member ("
        "id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, row_id INTEGER NOT NULL, "
        "root_key VARCHAR(256) NOT NULL, "
        "FOREIGN KEY(snapshot_id) REFERENCES planning_read_snapshot(id), "
        "FOREIGN KEY(row_id) REFERENCES planning_read_row(id))"
    ))
    if external_fk:
        connection.execute(sa.text(
            "CREATE TABLE external_legacy_reference ("
            "id INTEGER PRIMARY KEY, snapshot_id INTEGER, "
            "FOREIGN KEY(snapshot_id) REFERENCES planning_read_snapshot(id))"
        ))


def _accepted_current_truth(connection, *, status: str = "accepted") -> None:
    connection.execute(
        sa.text("INSERT INTO ledger_generation(id,status,cutoff) VALUES (1,:status,:cutoff)"),
        {"status": status, "cutoff": "2026-09-11 00:00:00"},
    )
    connection.execute(
        sa.text("INSERT INTO planning_truth_state(id,current_generation_id) VALUES (1,1)")
    )
    for index, (kind, scope_key) in enumerate(LIVE_SCOPES.items(), start=1):
        connection.execute(
            sa.text(
                "INSERT INTO current_execution_scope "
                "(id,entity_kind,scope_key,source_generation_id,source_revision,result_ready,summary) "
                "VALUES (:id,:kind,:scope,1,:revision,1,:summary)"
            ),
            {
                "id": index,
                "kind": kind,
                "scope": scope_key,
                "revision": f"accepted:g1:{kind}",
                "summary": "{}",
            },
        )


def test_final_drop_revision_and_models_are_explicitly_current_only():
    assert not LEGACY_TABLES & set(models.Base.metadata.tables)
    assert not any(
        hasattr(models, name)
        for name in ("PlanningReadSnapshot", "PlanningReadRow", "PlanningReadRootMember")
    )
    migration = _migration()
    assert migration.revision == "20260911_03"
    assert migration.down_revision == "20260911_02"


def test_empty_database_drop_is_idempotent_and_downgrade_is_irreversible():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _run(migration, connection)
        _run(migration, connection)
        with pytest.raises(RuntimeError, match="irreversible|backup|restore"):
            _run(migration, connection, "downgrade")
    engine.dispose()


def test_legacy_data_without_exact_current_truth_fails_before_any_drop():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _legacy_schema(connection)
        connection.execute(sa.text(
            "INSERT INTO planning_read_snapshot "
            "(id,consumer,snapshot_key,ledger_generation_id,truth_status,payload) "
            "VALUES (1,'mrp_result','legacy',1,'accepted','{}')"
        ))
        with pytest.raises(RuntimeError, match="current|truth|scope|ready"):
            _run(migration, connection)
        assert LEGACY_TABLES <= set(sa.inspect(connection).get_table_names())
    engine.dispose()


def test_building_generation_blocks_drop_before_tables_change():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _legacy_schema(connection)
        _accepted_current_truth(connection, status="building")
        connection.execute(sa.text(
            "INSERT INTO planning_read_snapshot "
            "(id,consumer,snapshot_key,ledger_generation_id,truth_status,payload) "
            "VALUES (1,'mrp_result','legacy',1,'building','{}')"
        ))
        with pytest.raises(RuntimeError, match="BUILDING|building|accepted"):
            _run(migration, connection)
        assert LEGACY_TABLES <= set(sa.inspect(connection).get_table_names())
    engine.dispose()


def test_exact_ready_current_truth_allows_valid_empty_legacy_drop():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _legacy_schema(connection)
        _accepted_current_truth(connection)
        connection.execute(sa.text(
            "INSERT INTO planning_read_snapshot "
            "(id,consumer,snapshot_key,ledger_generation_id,truth_status,payload) "
            "VALUES (1,'mrp_result','legacy-empty',1,'accepted','{}')"
        ))
        _run(migration, connection)
        assert not LEGACY_TABLES & set(sa.inspect(connection).get_table_names())
    engine.dispose()


def test_unknown_inbound_foreign_key_fails_closed_before_partial_drop():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _legacy_schema(connection, external_fk=True)
        with pytest.raises(RuntimeError, match="foreign|inbound|external|unknown"):
            _run(migration, connection)
        assert LEGACY_TABLES <= set(sa.inspect(connection).get_table_names())
    engine.dispose()


def test_postgres_path_declares_exclusive_nowait_protection_before_drop():
    source = _migration_path().read_text(encoding="utf-8")
    upper = source.upper()
    assert "ACCESS EXCLUSIVE" in upper
    assert "NOWAIT" in upper or "LOCK" in upper

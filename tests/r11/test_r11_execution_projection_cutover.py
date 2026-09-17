"""Focused contracts for bounded execution-projection retention."""

from pathlib import Path
import importlib.util
from types import SimpleNamespace

import pytest

from sqlalchemy import create_engine, text


REPO = Path(__file__).resolve().parents[2]
SERVICE = REPO / "backend/app/services/item_ledger/execution_projection_retention.py"
MIGRATION = REPO / "backend/alembic/versions/20260914_02_execution_projection_cutover.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("r12_cutover", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_migration_is_after_reservation_bootstrap_and_is_fk_safe_set_based():
    migration = _load_migration()
    assert migration.revision == "20260914_02"
    assert migration.down_revision == "20260914_01"
    tables = [statement.split("DELETE FROM", 1)[1].split()[0]
              for statement in migration._RETENTION_STATEMENTS]
    assert tables == [
        "drum_capacity_gap",
        "drum_slot",
        "assembly_readiness",
        "drum_schedule",
        "assembly_queue_line",
        "shelf_projection",
        "assembly_output_allocation",
        "assembly_output_fact_decision",
        "stock_ledger_supplier_receipt_provenance",
        "replenishment_work_item",
    ]
    source = MIGRATION.read_text(encoding="utf-8")
    assert "current_execution_scope" in source
    assert "lg.status = 'building'" in source
    assert "DELETE FROM ledger_generation" not in source
    assert "UPDATE reservation_entry" not in source
    assert "UPDATE reservation_event" not in source
    assert "_assert_backup_ready" in source
    assert "prodplan.execution_projection_backup_ready" in source
    assert migration._REQUIRED_SCOPES == (
        ("assembly_queue", "assembly:all-live-plans"),
        ("assembly_readiness", "assembly:all-live-plans"),
        ("drum_schedule", "drum:all-live-plans"),
        ("drum_slot", "drum:all-live-plans"),
        ("drum_gap", "drum:all-live-plans"),
        ("drum_excluded", "drum:all-live-plans"),
        ("shelf_projection", "shelf:all-live-mrps"),
    )


def test_retention_sql_keeps_exact_current_and_active_building_rows():
    # This is an in-memory SQL shape test, not a production database test.  It
    # exercises the same correlated predicates on SQLite and catches accidental
    # broad DELETEs while remaining cheap and deterministic.
    from app.services.item_ledger.execution_projection_retention import (
        _RETENTION_STATEMENTS,
    )

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE ledger_generation (id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
        conn.exec_driver_sql("CREATE TABLE assembly_queue_line (id INTEGER PRIMARY KEY, ledger_generation_id INTEGER)")
        conn.exec_driver_sql("CREATE TABLE assembly_readiness (id INTEGER PRIMARY KEY, ledger_generation_id INTEGER)")
        conn.exec_driver_sql("CREATE TABLE drum_schedule (id INTEGER PRIMARY KEY, ledger_generation_id INTEGER)")
        conn.exec_driver_sql("CREATE TABLE drum_slot (id INTEGER PRIMARY KEY, drum_schedule_id INTEGER, assembly_queue_line_id INTEGER)")
        conn.exec_driver_sql("CREATE TABLE drum_capacity_gap (id INTEGER PRIMARY KEY, drum_schedule_id INTEGER, assembly_queue_line_id INTEGER)")
        for table in (
            "assembly_output_allocation",
            "assembly_output_fact_decision",
            "stock_ledger_supplier_receipt_provenance",
            "replenishment_work_item",
            "shelf_projection",
        ):
            conn.exec_driver_sql(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, ledger_generation_id INTEGER)"
            )
        conn.exec_driver_sql(
            "INSERT INTO ledger_generation(id,status) VALUES "
            "(1,'accepted'),(2,'accepted'),(3,'building')"
        )
        conn.exec_driver_sql(
            "INSERT INTO assembly_queue_line(id,ledger_generation_id) VALUES "
            "(11,1),(21,2),(31,3)"
        )
        conn.exec_driver_sql(
            "INSERT INTO drum_schedule(id,ledger_generation_id) VALUES "
            "(10,1),(20,2),(30,3)"
        )
        conn.exec_driver_sql(
            "INSERT INTO drum_slot(id,drum_schedule_id,assembly_queue_line_id) VALUES "
            "(101,10,11),(201,20,21),(301,30,31)"
        )
        conn.exec_driver_sql(
            "INSERT INTO drum_capacity_gap(id,drum_schedule_id,assembly_queue_line_id) VALUES "
            "(102,10,11),(202,20,21),(302,30,31)"
        )
        conn.exec_driver_sql(
            "INSERT INTO assembly_readiness(id,ledger_generation_id) VALUES "
            "(103,1),(203,2),(303,3)"
        )
        for table in (
            "assembly_output_allocation",
            "assembly_output_fact_decision",
            "stock_ledger_supplier_receipt_provenance",
            "replenishment_work_item",
            "shelf_projection",
        ):
            conn.exec_driver_sql(
                f"INSERT INTO {table}(id,ledger_generation_id) VALUES (1,1),(2,2),(3,3)"
            )
        for statement in _RETENTION_STATEMENTS:
            conn.execute(text(statement), {"current_generation_id": 2})

        assert conn.execute(text(
            "SELECT count(*) FROM assembly_queue_line WHERE ledger_generation_id = 1"
        )).scalar_one() == 0
        assert conn.execute(text(
            "SELECT count(*) FROM assembly_queue_line WHERE ledger_generation_id IN (2,3)"
        )).scalar_one() == 2
        assert conn.execute(text("SELECT count(*) FROM drum_slot")).scalar_one() == 2
        assert conn.execute(text("SELECT count(*) FROM drum_capacity_gap")).scalar_one() == 2
        for table in (
            "assembly_output_allocation",
            "assembly_output_fact_decision",
            "stock_ledger_supplier_receipt_provenance",
            "replenishment_work_item",
            "shelf_projection",
        ):
            assert conn.execute(text(
                f"SELECT count(*) FROM {table} WHERE ledger_generation_id = 1"
            )).scalar_one() == 0
            assert conn.execute(text(
                f"SELECT count(*) FROM {table} WHERE ledger_generation_id IN (2,3)"
            )).scalar_one() == 2


def test_runtime_cleanup_is_after_both_current_publications_and_has_fail_closed_gate():
    lifecycle = (
        REPO / "backend/app/services/item_ledger/generation_lifecycle.py"
    ).read_text(encoding="utf-8")
    refresh = (
        REPO / "backend/app/services/obligation_refresh_publish.py"
    ).read_text(encoding="utf-8")
    for source in (lifecycle, refresh):
        publication = source.index("publish_current_obligation_views_from_generation")
        cleanup = source.index("prune_retired_execution_projections", publication)
        assert cleanup > publication
    service = SERVICE.read_text(encoding="utf-8")
    assert "requires the exact accepted truth pointer" in service
    assert "current_execution_scope" in service
    assert "status = 'building'" in service
    assert "drum_excluded" in service
    assert "shelf_projection" in service


def test_migration_requires_operator_verified_postgres_backup_guard():
    migration = _load_migration()

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar(self):
            return self.value

    class _Bind:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self, value):
            self.value = value

        def execute(self, _statement):
            return _Result(self.value)

    with pytest.raises(RuntimeError, match="verified backup"):
        migration._assert_backup_ready(_Bind("off"))
    migration._assert_backup_ready(_Bind("on"))


def test_one_off_tool_requires_complete_execution_contour_when_projection_tables_exist():
    from tools.current_execution_migration import EXPECTED_EXECUTION_SCOPES

    assert EXPECTED_EXECUTION_SCOPES == (
        ("assembly_queue", "assembly:all-live-plans", "assembly_queue"),
        ("assembly_readiness", "assembly:all-live-plans", "assembly_readiness"),
        ("drum_schedule", "drum:all-live-plans", "drum_schedule"),
        ("drum_slot", "drum:all-live-plans", "drum_slot"),
        ("drum_gap", "drum:all-live-plans", "drum_gap"),
        ("drum_excluded", "drum:all-live-plans", "drum_excluded"),
        ("shelf_projection", "shelf:all-live-mrps", "shelf_projection"),
    )
    source = (REPO / "tools/current_execution_migration.py").read_text(encoding="utf-8")
    apply_start = source.index("def apply_current_obligation_migration")
    apply_source = source[apply_start:]
    execution_call = apply_source.index(
        "execution_publisher_result = publish_current_execution_from_generation"
    )
    obligation_call = apply_source.index(
        "publisher_result = publish_current_obligation_views_from_snapshots"
    )
    assert execution_call < obligation_call

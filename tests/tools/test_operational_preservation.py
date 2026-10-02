import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, event, text

from tools.operational_preservation import (
    OPTIONAL_TABLES, SOURCE_REVISION, TABLES, TARGET_REVISION,
    capture_before, capture_inventory, verify_preservation,
)


def _source(url="sqlite:///:memory:"):
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": SOURCE_REVISION})
        for table in TABLES:
            if table in OPTIONAL_TABLES:
                continue
            extra = ", planning_read_snapshot_id INTEGER" if table == "purchase_export_batch" else ""
            connection.execute(text(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, payload TEXT{extra})'))
            if not table.startswith("purchase_export"):
                connection.execute(text(f'INSERT INTO "{table}" (id, payload) VALUES (1, :payload)'), {"payload": "operator data"})
    return engine


def _migrate(engine):
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE purchase_export_batch DROP COLUMN planning_read_snapshot_id"))
        connection.execute(text("ALTER TABLE purchase_export_batch ADD COLUMN current_execution_scope_id BIGINT NOT NULL DEFAULT 1"))
        connection.execute(text("ALTER TABLE purchase_export_batch ADD COLUMN current_execution_source_revision VARCHAR(256) NOT NULL DEFAULT 'r1'"))
        connection.execute(text("UPDATE alembic_version SET version_num=:revision"), {"revision": TARGET_REVISION})


def test_empty_removed_anchor_preserves_all_operational_rows_and_reports_absence():
    engine = _source()
    before = capture_inventory(engine)
    _migrate(engine)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, executemany: statements.append(statement))
    report = verify_preservation(engine, before)
    assert report["status"] == "passed"
    assert len(report["checks"]) == 17
    assert report["checks"]["production_order_lines"] == {"preserved": True, "source_absent": True}
    assert "schema_rule" in report["checks"]["purchase_export_batch"]
    assert all(not statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE", "DROP")) for statement in statements)


@pytest.mark.parametrize("mutation", [
    "DELETE FROM production_material_custody_event",
    "UPDATE sync_link SET payload='corrupted' WHERE id=1",
    "INSERT INTO production_piecework_commands(id,payload) VALUES(2,'new')",
])
def test_real_operational_loss_change_or_extra_command_is_blocked(mutation):
    engine = _source()
    before = capture_inventory(engine)
    _migrate(engine)
    with engine.begin() as connection:
        connection.execute(text(mutation))
    report = verify_preservation(engine, before)
    assert report["status"] == "blocked"
    assert any(finding["reason"] == "operational row count or values changed" for finding in report["findings"])


@pytest.mark.parametrize("mutation", [
    "ALTER TABLE production_orders DROP COLUMN payload",
    "ALTER TABLE production_orders ADD COLUMN undocumented TEXT",
    "DROP TABLE production_orders",
    "CREATE TABLE production_order_lines(id INTEGER PRIMARY KEY)",
])
def test_unexplained_schema_drift_is_blocked_even_for_empty_columns(mutation):
    engine = _source()
    before = capture_inventory(engine)
    _migrate(engine)
    with engine.begin() as connection:
        connection.execute(text(mutation))
    assert verify_preservation(engine, before)["status"] == "blocked"


def test_nonempty_removed_anchor_is_not_ignored_even_if_other_fields_survive():
    engine = _source()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO purchase_export_batch VALUES(1, 'export command', 42)"))
    before = capture_inventory(engine)
    _migrate(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE purchase_export_batch SET current_execution_scope_id=1,current_execution_source_revision='r1'"))
    report = verify_preservation(engine, before)
    assert report["status"] == "blocked"
    assert any("nonempty anchor" in finding["reason"] for finding in report["findings"])


def test_empty_source_cannot_hide_post_migration_insert():
    engine = _source()
    before = capture_inventory(engine)
    _migrate(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO purchase_export_batch VALUES(1, 'new command',1,'r1')"))
    assert verify_preservation(engine, before)["status"] == "blocked"


def test_schema_type_drift_is_blocked_when_row_values_and_names_still_match():
    engine = _source()
    before = capture_inventory(engine)
    _migrate(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE production_orders RENAME TO old_orders"))
        connection.execute(text("CREATE TABLE production_orders(id INTEGER PRIMARY KEY,payload VARCHAR(100))"))
        connection.execute(text("INSERT INTO production_orders SELECT * FROM old_orders"))
        connection.execute(text("DROP TABLE old_orders"))
    report = verify_preservation(engine, before)
    assert report["status"] == "blocked"
    assert any("column type" in finding["reason"] for finding in report["findings"])


def test_wrong_revision_pair_and_incomplete_source_are_rejected():
    engine = _source()
    before = capture_inventory(engine)
    _migrate(engine)
    before["revision"] = "unproved"
    assert verify_preservation(engine, before)["status"] == "blocked"
    del before["tables"]["sync_link"]
    with pytest.raises(ValueError, match="complete operational allowlist"):
        verify_preservation(engine, before)


def test_runner_source_capture_refuses_missing_required_table_before_migration():
    engine = _source()
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE sync_link"))
    with pytest.raises(ValueError, match="required operational table absent: sync_link"):
        capture_before(engine)


def test_legacy_failed_receipt_cannot_be_relabelled_success():
    engine = _source()
    with pytest.raises(ValueError, match="unsupported inventory"):
        verify_preservation(engine, {"columns": {}, "tables": {}, "version": SOURCE_REVISION})


def test_portable_cli_capture_verify_and_refuse_receipt_overwrite(tmp_path):
    db = tmp_path / "rehearsal.db"
    url = "sqlite:///" + db.as_posix()
    engine = _source(url)
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ, DATABASE_URL=url)
    script = root / "tools" / "operational_preservation.py"
    baseline = tmp_path / "before.json"
    receipt = tmp_path / "after.json"
    capture = subprocess.run([sys.executable, str(script), "capture-before", "--output", str(baseline)], env=environment, cwd=root, capture_output=True, text=True)
    assert capture.returncode == 0, capture.stderr
    _migrate(engine)
    verify_args = [sys.executable, str(script), "verify-after", "--before", str(baseline), "--output", str(receipt)]
    verify = subprocess.run(verify_args, env=environment, cwd=root, capture_output=True, text=True)
    assert verify.returncode == 0, verify.stderr
    evidence = receipt.read_bytes()
    assert json.loads(evidence)["status"] == "passed"
    assert url not in evidence.decode()
    repeated = subprocess.run(verify_args, env=environment, cwd=root, capture_output=True, text=True)
    assert repeated.returncode != 0
    assert receipt.read_bytes() == evidence

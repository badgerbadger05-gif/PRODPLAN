import json
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, text

from tools.current_execution_migration import build_manifest


def _engine():
    return create_engine("sqlite:///:memory:")


def _create_inventory_tables(engine):
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE current_execution_scope ("
            "id INTEGER PRIMARY KEY, entity_kind TEXT NOT NULL, "
            "scope_key TEXT NOT NULL, result_ready BOOLEAN NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE current_execution_row ("
            "id INTEGER PRIMARY KEY, entity_kind TEXT NOT NULL, "
            "scope_key TEXT NOT NULL, business_identity TEXT NOT NULL, "
            "payload TEXT NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE planning_read_snapshot ("
            "id INTEGER PRIMARY KEY, ledger_generation_id INTEGER NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE planning_read_row ("
            "id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, row_key TEXT)"
        ))


def test_manifest_is_deterministic_and_classifies_rows_by_transition():
    engine = _engine()
    _create_inventory_tables(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO current_execution_scope "
            "(id, entity_kind, scope_key, result_ready) VALUES (1, 'mrp_result', 'mrp:all', 1)"
        ))
        connection.execute(text(
            "INSERT INTO current_execution_row "
            "(id, entity_kind, scope_key, business_identity, payload) "
            "VALUES (1, 'mrp_result', 'mrp:all', 'mrp-run:1:buy:item:10', '{}')"
        ))
        connection.execute(text(
            "INSERT INTO planning_read_snapshot (id, ledger_generation_id) VALUES (7, 3)"
        ))
        connection.execute(text(
            "INSERT INTO planning_read_row (id, snapshot_id, row_key) VALUES (11, 7, 'legacy:11')"
        ))

    first = build_manifest(engine)
    second = build_manifest(engine)

    assert first == second
    assert first["status"] == "ready"
    assert first["categories"]["preserve"]["current_execution_scope"]["row_count"] == 1
    assert first["categories"]["migrate"]["planning_read_snapshot"]["row_count"] == 1
    assert first["categories"]["migrate"]["planning_read_row"]["row_count"] == 1


def test_unknown_table_blocks_preflight_instead_of_being_deleted():
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE mystery_dependency (id INTEGER PRIMARY KEY)"))

    manifest = build_manifest(engine)

    assert manifest["status"] == "blocked"
    assert manifest["categories"]["unknown"]["mystery_dependency"]["row_count"] == 0
    assert "not classified" in manifest["categories"]["unknown"]["mystery_dependency"]["reason"]


def test_ambiguous_legacy_row_to_current_identity_blocks_preflight():
    engine = _engine()
    _create_inventory_tables(engine)
    payload_a = json.dumps({"legacy_row_id": 11})
    payload_b = json.dumps({"legacy_row_id": 11})
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO current_execution_row "
            "(id, entity_kind, scope_key, business_identity, payload) "
            "VALUES (1, 'purchase_control_journal', 'purchase:all', 'purchase:a', :payload)"
        ), {"payload": payload_a})
        connection.execute(text(
            "INSERT INTO current_execution_row "
            "(id, entity_kind, scope_key, business_identity, payload) "
            "VALUES (2, 'purchase_control_journal', 'purchase:all', 'purchase:b', :payload)"
        ), {"payload": payload_b})

    manifest = build_manifest(engine)

    assert manifest["status"] == "blocked"
    assert any(
        finding["legacy_id"] == "11" and finding["kind"] == "ambiguous"
        for finding in manifest["dependencies"]["unknown"]
    )


def test_unmapped_snapshot_reference_is_unknown_and_fail_closed():
    engine = _engine()
    _create_inventory_tables(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO current_execution_row "
            "(id, entity_kind, scope_key, business_identity, payload) "
            "VALUES (1, 'production_control_journal', 'production:all', 'order:1', :payload)"
        ), {"payload": json.dumps({"planning_read_snapshot_id": 7})})

    manifest = build_manifest(engine)

    assert manifest["status"] == "blocked"
    assert any(
        finding["key"] == "planning_read_snapshot_id"
        for finding in manifest["dependencies"]["unknown"]
    )


def test_repo_root_cli_loads_application_model_inventory(tmp_path):
    database = tmp_path / "model-inventory.db"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE items (item_id INTEGER PRIMARY KEY)"))

    repo = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            "tools/current_execution_migration.py",
            "--database-url",
            f"sqlite:///{database}",
        ],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["categories"]["preserve"]["items"]["row_count"] == 0
    assert "items" not in manifest["categories"]["unknown"]

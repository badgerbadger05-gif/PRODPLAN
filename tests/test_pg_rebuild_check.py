"""Guards for the PostgreSQL rehearsal command.

The rehearsal itself needs a real server, so it is opt-in: set
``PRODPLAN_PG_CHECK=1`` to let it start a disposable ``postgres:15`` container,
or ``PRODPLAN_PG_CHECK_DSN`` to point it at an existing one (a restored dump
copy).  Without either variable — or without Docker — the integration test is
skipped and the suite keeps running on SQLite only.

The remaining tests are pure and always run: they pin the classification rules
that decide whether the verifier's failure on an empty database is the expected
one.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import pg_rebuild_check  # noqa: E402


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=60,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _skip_reason() -> str | None:
    if os.environ.get("PRODPLAN_PG_CHECK_DSN"):
        return None
    if os.environ.get("PRODPLAN_PG_CHECK") != "1":
        return "set PRODPLAN_PG_CHECK=1 or PRODPLAN_PG_CHECK_DSN to run the PostgreSQL rehearsal"
    if not _docker_available():
        return "Docker is unavailable"
    return None


def test_round_trip_preserves_irreversible_current_owner_cutovers():
    path = (
        REPO_ROOT
        / "backend"
        / "alembic"
        / "versions"
        / "20260924_01_specification_rebase_run_state.py"
    )
    spec = importlib.util.spec_from_file_location("material_coverage_round_trip_floor", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert pg_rebuild_check.ROUND_TRIP_FLOOR == module.down_revision


def test_empty_bootstrap_refuses_existing_tables_before_migration(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(pg_rebuild_check, "_alembic", lambda *a, **kw: pytest.fail("existing database must not be migrated or seeded"))
    with pytest.raises(pg_rebuild_check.CheckError, match="no user tables"):
        pg_rebuild_check._bootstrap_empty_rehearsal(None, SimpleNamespace(scalar=lambda sql: "1"))


def test_regular_migration_does_not_seed_truth_or_set_backup_guard(monkeypatch):
    from types import SimpleNamespace

    calls = []
    def migrate(target, *command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="head", stderr="")
    monkeypatch.setattr(pg_rebuild_check, "_alembic", migrate)
    monkeypatch.setattr(pg_rebuild_check, "_bootstrap_empty_rehearsal", lambda *a: pytest.fail("regular migration must not seed"))
    assert pg_rebuild_check.stage_migrate(None).status == "PASS"
    assert calls == [(("upgrade", "head"), {}), (("current",), {})]


def test_empty_bootstrap_never_runs_guarded_upgrade_after_backup_failure(monkeypatch):
    from types import SimpleNamespace

    commands = []
    def migrate(target, *command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)
    def failed_backup(path):
        raise pg_rebuild_check.CheckError("backup verification failed")
    monkeypatch.setattr(pg_rebuild_check, "_alembic", migrate)
    monkeypatch.setattr(pg_rebuild_check, "_seed_guard_fixture", lambda psql: None)
    monkeypatch.setattr(pg_rebuild_check, "_run", lambda *a, **kw: SimpleNamespace(returncode=0))
    client = SimpleNamespace(scalar=lambda sql: "0", verified_backup=failed_backup)
    with pytest.raises(pg_rebuild_check.CheckError, match="backup verification"):
        pg_rebuild_check._bootstrap_empty_rehearsal(SimpleNamespace(url=lambda: "local-test-only"), client)
    assert commands == [("upgrade", pg_rebuild_check.BOOTSTRAP_REVISION)]


def test_summary_projection_is_extracted_from_the_verifier():
    query = pg_rebuild_check.verify_summary_query(
        pg_rebuild_check.VERIFY_SQL.read_text(encoding="utf-8")
    )
    assert query.startswith("WITH current_generation AS (")
    assert query.rstrip().endswith(";")
    # The aborted DO block must not leak into the standalone projection.
    assert "$verify$" not in query
    assert "\\echo" not in query


def test_known_empty_verifier_failures_are_recognised():
    assert pg_rebuild_check.classify_verify_failure(
        "ERROR:  current truth is absent, unaccepted, or differs from expected key/cutoff"
    )
    assert pg_rebuild_check.classify_verify_failure(
        "ERROR:  current assembly queue is empty or has no open quantity"
    )


@pytest.mark.parametrize(
    "message",
    [
        'ERROR:  relation "planning_read_snapshot" does not exist',
        "ERROR:  4 fixed-run MRP requirements have invalid gross/net quantities",
        "ERROR:  7 SLE/reservation realization allocations are duplicated",
        "ERROR:  syntax error at or near \"SELCT\"",
    ],
)
def test_real_defects_are_never_treated_as_emptiness(message):
    assert pg_rebuild_check.classify_verify_failure(message) is None


def test_local_existing_server_can_use_wsl_psql_transport(monkeypatch):
    target = pg_rebuild_check.Target(
        host="127.0.0.1",
        port=55441,
        user="r2_user",
        password="r2_local_only",
        database="prodplan_r2",
    )
    monkeypatch.setattr(pg_rebuild_check.shutil, "which", lambda name: None)
    monkeypatch.setattr(pg_rebuild_check, "_docker_available", lambda: False)
    monkeypatch.setattr(pg_rebuild_check, "_wsl_available", lambda: True)
    client = pg_rebuild_check.Psql(target, image="postgres:15", binary=None, container=None)
    command = client._command([], target.database)
    assert command[:5] == ["wsl.exe", "-d", "Ubuntu", "--", "env"]
    assert "psql" in command
    assert "-p" in command and "55441" in command


@pytest.mark.parametrize("program", ["psql", "pg_dump", "pg_restore"])
def test_native_postgresql_clients_do_not_repeat_the_executable_as_an_argument(program):
    target = pg_rebuild_check.Target(
        host="127.0.0.1", port=55441, user="test", password="test", database="test",
    )
    binary = str(Path("client-bin") / "psql")
    client = pg_rebuild_check.Psql(target, image="postgres:15", binary=binary, container=None)
    command = client._command([], target.database, program=program)
    assert command[0] == str(Path(binary).with_name(program))
    assert not {"psql", "pg_dump", "pg_restore"}.intersection(command[1:])
    assert command[1:5] == ["-h", "127.0.0.1", "-p", "55441"]
    assert ("ON_ERROR_STOP=1" in command) == (program == "psql")


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_ledger_rebuild_sql_executes_on_postgresql(monkeypatch):
    """Smoke rehearsal: migrations round trip, clear COMMITs, verifier runs."""
    dsn = os.environ.get("PRODPLAN_PG_CHECK_DSN")
    if not dsn:
        assert pg_rebuild_check.main([]) == 0
        return
    # Never migrate/clear the supplied database itself. Each run owns a new
    # database, so a retry cannot reuse a partially migrated fixture.
    from uuid import uuid4
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    url = make_url(dsn)
    assert url.host in {"localhost", "127.0.0.1", "::1"}, "rehearsal tests require a local PostgreSQL server"
    assert not url.query, "rehearsal tests do not accept connection-query overrides"
    database = "prodplan_pgcheck_" + uuid4().hex
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
        try:
            monkeypatch.setenv("PRODPLAN_PG_CHECK_DSN", url.set(database=database).render_as_string(hide_password=False))
            assert pg_rebuild_check.main([
                "--bootstrap-empty", "--allow-destructive-clear",
                "--expected-generation-key", pg_rebuild_check.SEED_GENERATION_KEY,
            ]) == 0
        finally:
            with engine.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
    finally:
        engine.dispose()

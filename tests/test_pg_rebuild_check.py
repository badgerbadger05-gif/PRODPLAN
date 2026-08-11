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


def test_round_trip_floor_is_the_parent_of_the_new_revisions():
    path = (
        REPO_ROOT
        / "backend"
        / "alembic"
        / "versions"
        / "20260730_01_material_coverage_generation.py"
    )
    spec = importlib.util.spec_from_file_location("material_coverage_round_trip_floor", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert pg_rebuild_check.ROUND_TRIP_FLOOR == module.down_revision


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


@pytest.mark.skipif(_skip_reason() is not None, reason=_skip_reason() or "")
def test_ledger_rebuild_sql_executes_on_postgresql():
    """Smoke rehearsal: migrations round trip, clear COMMITs, verifier runs."""
    assert pg_rebuild_check.main([]) == 0

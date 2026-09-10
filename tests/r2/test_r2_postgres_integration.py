"""R2 PostgreSQL integration tests; never fall back to production/default DSNs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _r2_dsn() -> str:
    dsn = os.getenv("PRODPLAN_R2_TEST_DSN")
    if not dsn:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured; local R2 PostgreSQL is unavailable")
    from app.r2_local_contract import validate_r2_dsn

    validate_r2_dsn(dsn)
    return dsn


@pytest.mark.integration
def test_empty_postgresql_database_can_be_migrated():
    dsn = _r2_dsn()
    pytest.importorskip("psycopg2")
    import sqlalchemy as sa

    env = os.environ.copy()
    env["DATABASE_URL"] = dsn
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=repo_root / "backend",
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"empty PostgreSQL migration failed:\n{result.stdout}\n{result.stderr}"
    with sa.create_engine(dsn).connect() as connection:
        assert connection.execute(sa.text("SELECT current_database()" )).scalar_one() == "prodplan_r2"
        assert connection.execute(sa.text("SELECT count(*) FROM alembic_version")).scalar_one() == 1


@pytest.mark.integration
def test_two_connections_observe_rollback_and_commit_isolation():
    dsn = _r2_dsn()
    pytest.importorskip("psycopg2")
    import sqlalchemy as sa

    engine = sa.create_engine(dsn, poolclass=sa.pool.NullPool)
    with engine.begin() as setup:
        setup.execute(sa.text("CREATE TABLE IF NOT EXISTS r2_tx_probe (id integer PRIMARY KEY, value text NOT NULL)"))
        setup.execute(sa.text("TRUNCATE r2_tx_probe"))
    conn_a = engine.connect()
    conn_b = engine.connect()
    try:
        tx = conn_a.begin()
        conn_a.execute(sa.text("INSERT INTO r2_tx_probe (id, value) VALUES (1, 'rolled-back')"))
        tx.rollback()
        assert conn_b.execute(sa.text("SELECT count(*) FROM r2_tx_probe")).scalar_one() == 0
        with conn_a.begin():
            conn_a.execute(sa.text("INSERT INTO r2_tx_probe (id, value) VALUES (2, 'committed')"))
        assert conn_b.execute(sa.text("SELECT value FROM r2_tx_probe WHERE id = 2")).scalar_one() == "committed"
    finally:
        conn_a.close()
        conn_b.close()
        engine.dispose()


@pytest.mark.integration
def test_real_fastapi_read_endpoint_returns_measured_latency():
    dsn = _r2_dsn()
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PRODPLAN_R2_TEST_DSN"] = dsn
    result = subprocess.run(
        [sys.executable, "tools/r2-baseline.py", "--dsn", dsn],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"R2 API baseline failed:\n{result.stdout}\n{result.stderr}"
    import json

    payload = json.loads(result.stdout)
    latency = payload["api_latency_ms"]
    assert payload["api_endpoint"] == "/api/v1/items/?skip=0&limit=100"
    assert payload["api_sample_count"] >= 5
    assert latency["p50"] >= 0
    assert latency["p95"] >= latency["p50"]


@pytest.mark.integration
def test_api_baseline_seed_is_idempotent_and_has_required_timestamps():
    dsn = _r2_dsn()
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PRODPLAN_R2_TEST_DSN"] = dsn
    command = [sys.executable, "tools/r2-baseline.py", "--dsn", dsn]
    first = subprocess.run(command, cwd=repo_root, env=env, capture_output=True, text=True)
    second = subprocess.run(command, cwd=repo_root, env=env, capture_output=True, text=True)
    assert first.returncode == 0, f"first baseline failed:\n{first.stdout}\n{first.stderr}"
    assert second.returncode == 0, f"second baseline failed:\n{second.stdout}\n{second.stderr}"
    import json
    import sqlalchemy as sa

    engine = sa.create_engine(dsn, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM items WHERE item_code LIKE 'r2-%'")).scalar_one() == 3
            assert connection.execute(
                sa.text("SELECT count(*) FROM items WHERE item_code LIKE 'r2-%' AND created_at IS NOT NULL AND updated_at IS NOT NULL")
            ).scalar_one() == 3
        assert json.loads(first.stdout)["api_sample_count"] == json.loads(second.stdout)["api_sample_count"]
    finally:
        engine.dispose()

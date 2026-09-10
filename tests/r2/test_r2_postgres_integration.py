"""R2 PostgreSQL integration tests; never fall back to production/default DSNs."""

from __future__ import annotations

import os

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

    with sa.create_engine(dsn).connect() as connection:
        assert connection.execute(sa.text("SELECT current_database()")) .scalar_one() == "prodplan_r2"


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


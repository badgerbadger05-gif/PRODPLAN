"""B5 / M-7: concurrent material-issue creation must not double-claim the same
free workshop stock (section-stock double count, case PP001308915).

create_material_issues now takes a transaction-scoped advisory lock that
serializes the free-stock read-modify-write across concurrent callers. A full
end-to-end concurrent double-claim test would need threads + heavy fixtures;
these tests verify the two things that matter and are not flaky:
  1. the lock primitive is genuinely mutually exclusive on PostgreSQL, and
  2. create_material_issues actually acquires it.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services import production_control_material_issues as mi

PG_URL = os.getenv(
    "PRODPLAN_TEST_PG_URL",
    "postgresql://prodplan:prodplan@localhost:55440/prodplan_test",
)


def _pg_available() -> bool:
    try:
        engine = create_engine(PG_URL)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


requires_pg = pytest.mark.skipif(not _pg_available(), reason="test PostgreSQL not available")


@requires_pg
def test_advisory_lock_serializes_on_postgres():
    engine = create_engine(PG_URL)
    Session = sessionmaker(bind=engine)
    a = Session()
    b = Session()
    try:
        # Session A grabs the material-issue lock through the helper under test.
        mi._lock_material_issue_pool(a)

        # While A's transaction holds it, B cannot acquire the same lock.
        held = b.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"),
            {"k": mi._MATERIAL_ISSUE_LOCK_KEY},
        ).scalar()
        assert held is False, "lock was not mutually exclusive -> concurrent double-claim possible"
        b.rollback()

        # Once A commits (releasing the xact lock), B can take it.
        a.commit()
        got = b.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"),
            {"k": mi._MATERIAL_ISSUE_LOCK_KEY},
        ).scalar()
        assert got is True
        b.rollback()
    finally:
        a.close()
        b.close()
        engine.dispose()


def test_lock_is_noop_on_sqlite(db_session):
    # Must not raise on non-Postgres backends (advisory locks are PG-only).
    mi._lock_material_issue_pool(db_session)


def test_create_material_issues_acquires_lock(db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(mi, "_lock_material_issue_pool", lambda db: calls.append(True))
    result = mi.create_material_issues(db_session, [])
    assert calls == [True], "create_material_issues must take the advisory lock before touching stock"
    assert "created" in result

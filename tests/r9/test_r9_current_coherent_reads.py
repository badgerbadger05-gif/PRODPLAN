"""R9 coherent current-scope reads and ready/empty semantics."""

from __future__ import annotations

import os
import threading
import time
from queue import Queue

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app import models
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    load_current_execution_rows,
    load_current_execution_coherent,
    publish_current_execution_scope,
    require_current_execution_scope,
)


def _dsn() -> str:
    value = os.getenv("PRODPLAN_R2_TEST_DSN")
    if not value:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    from app.r2_local_contract import validate_r2_dsn

    validate_r2_dsn(value)
    return value


def _rows(scope: str, qty: str):
    return [{
        "entity_kind": "r9_coherent_execution",
        "business_identity": "r9:line:1",
        "scope_key": scope,
        "payload": {"qty": qty},
    }]


@pytest.mark.integration
def test_postgresql_reader_waits_for_publication_and_sees_one_complete_version():
    dsn = _dsn()
    pytest.importorskip("psycopg2")
    engine = sa.create_engine(dsn, poolclass=sa.pool.NullPool)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    scope = "r9-pg-coherent-current"
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM current_execution_change WHERE scope_key=:scope"), {"scope": scope})
            conn.execute(sa.text("DELETE FROM current_execution_row WHERE scope_key=:scope"), {"scope": scope})
            conn.execute(sa.text("DELETE FROM current_execution_scope WHERE scope_key=:scope"), {"scope": scope})

        seed = Session()
        try:
            publish_current_execution_scope(
                seed, source_revision="r9-old", scope_key=scope,
                rows=_rows(scope, "old"), entity_kinds=("r9_coherent_execution",),
            )
            seed.commit()
        finally:
            seed.close()

        writer = Session()
        reader_result: Queue = Queue()
        started = threading.Event()
        try:
            publish_current_execution_scope(
                writer, source_revision="r9-new", scope_key=scope,
                rows=_rows(scope, "new"), entity_kinds=("r9_coherent_execution",),
            )
            writer.flush()  # hold the manifest/current-row publication locks

            def read_in_separate_session():
                session = Session()
                started.set()
                try:
                    manifest, rows = load_current_execution_coherent(
                        session, entity_kind="r9_coherent_execution", scope_key=scope,
                    )
                    reader_result.put((manifest.source_revision, rows[0].payload["qty"]))
                    session.commit()
                except Exception as exc:  # pragma: no cover - surfaced below
                    reader_result.put(exc)
                    session.rollback()
                finally:
                    session.close()

            thread = threading.Thread(target=read_in_separate_session, daemon=True)
            thread.start()
            assert started.wait(timeout=2)
            time.sleep(0.2)
            assert thread.is_alive(), "coherent reader did not wait for publication lock"
            writer.commit()
            thread.join(timeout=5)
            assert not thread.is_alive()
            result = reader_result.get_nowait()
            assert not isinstance(result, Exception)
            assert result == ("r9-new", "new")
        finally:
            writer.rollback()
            writer.close()
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM current_execution_change WHERE scope_key=:scope"), {"scope": scope})
            conn.execute(sa.text("DELETE FROM current_execution_row WHERE scope_key=:scope"), {"scope": scope})
            conn.execute(sa.text("DELETE FROM current_execution_scope WHERE scope_key=:scope"), {"scope": scope})
        engine.dispose()


def test_empty_scope_is_valid_only_with_ready_manifest(db_session):
    with pytest.raises(CurrentExecutionUnavailable):
        load_current_execution_coherent(
            db_session, entity_kind="r9_empty_scope", scope_key="r9:empty",
        )

    publish_current_execution_scope(
        db_session,
        source_revision="r9-empty-1",
        scope_key="r9:empty",
        rows=[],
        entity_kinds=("r9_empty_scope",),
    )
    db_session.commit()
    manifest, rows = load_current_execution_coherent(
        db_session, entity_kind="r9_empty_scope", scope_key="r9:empty",
    )
    assert manifest.result_ready is True
    assert rows == []

    manifest.result_ready = False
    db_session.commit()
    with pytest.raises(CurrentExecutionUnavailable):
        load_current_execution_coherent(
            db_session, entity_kind="r9_empty_scope", scope_key="r9:empty",
        )


@pytest.mark.integration
def test_postgresql_reader_holds_old_boundary_while_writer_waits():
    dsn = _dsn()
    pytest.importorskip("psycopg2")
    engine = sa.create_engine(dsn, poolclass=sa.pool.NullPool)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    scope = "r9-pg-coherent-reader-first"
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM current_execution_change WHERE scope_key=:scope"), {"scope": scope})
            conn.execute(sa.text("DELETE FROM current_execution_row WHERE scope_key=:scope"), {"scope": scope})
            conn.execute(sa.text("DELETE FROM current_execution_scope WHERE scope_key=:scope"), {"scope": scope})
        seed = Session()
        try:
            publish_current_execution_scope(
                seed, source_revision="reader-old", scope_key=scope,
                rows=_rows(scope, "old"), entity_kinds=("r9_coherent_execution",),
            )
            seed.commit()
        finally:
            seed.close()

        reader = Session()
        started = threading.Event()
        writer_errors: Queue = Queue()
        try:
            manifest = require_current_execution_scope(
                reader, entity_kind="r9_coherent_execution", scope_key=scope,
            )
            assert manifest.source_revision == "reader-old"
            rows = load_current_execution_rows(
                reader, entity_kind="r9_coherent_execution", scope_key=scope,
            )
            assert rows[0].payload["qty"] == "old"

            def publish_after_reader_boundary():
                writer = Session()
                try:
                    started.set()
                    publish_current_execution_scope(
                        writer, source_revision="reader-new", scope_key=scope,
                        rows=_rows(scope, "new"), entity_kinds=("r9_coherent_execution",),
                    )
                    writer.commit()
                except Exception as exc:  # pragma: no cover - surfaced below
                    writer_errors.put(exc)
                    writer.rollback()
                finally:
                    writer.close()

            thread = threading.Thread(target=publish_after_reader_boundary, daemon=True)
            thread.start()
            assert started.wait(timeout=2)
            time.sleep(0.2)
            assert thread.is_alive(), "writer crossed the reader publication boundary"
            reader.commit()
            thread.join(timeout=5)
            assert not thread.is_alive()
            if not writer_errors.empty():
                raise writer_errors.get()
        finally:
            reader.close()

        check = Session()
        try:
            manifest, rows = load_current_execution_coherent(
                check, entity_kind="r9_coherent_execution", scope_key=scope,
            )
            assert (manifest.source_revision, rows[0].payload["qty"]) == ("reader-new", "new")
        finally:
            check.close()
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM current_execution_change WHERE scope_key=:scope"), {"scope": scope})
            conn.execute(sa.text("DELETE FROM current_execution_row WHERE scope_key=:scope"), {"scope": scope})
            conn.execute(sa.text("DELETE FROM current_execution_scope WHERE scope_key=:scope"), {"scope": scope})
        engine.dispose()

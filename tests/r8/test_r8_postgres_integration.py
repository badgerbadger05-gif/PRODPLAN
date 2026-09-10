"""R8 current-owner MVCC and idempotency proof on the local PostgreSQL contour."""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app import models
from app.services.item_ledger.current_execution import publish_current_execution_scope


def _dsn() -> str:
    value = os.getenv("PRODPLAN_R2_TEST_DSN")
    if not value:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    from app.r2_local_contract import validate_r2_dsn

    validate_r2_dsn(value)
    return value


@pytest.mark.integration
def test_r8_current_owner_is_atomic_on_two_postgresql_sessions():
    dsn = _dsn()
    pytest.importorskip("psycopg2")
    engine = sa.create_engine(dsn, poolclass=sa.pool.NullPool)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    scope = "r8-pg-current-owner"
    identity = "queue:plan-line:r8-pg"
    rows_old = [{
        "entity_kind": "assembly_queue",
        "business_identity": identity,
        "scope_key": scope,
        "payload": {
            "plan_id": 801,
            "plan_line_id": 802,
            "period_from": "2026-09-10",
            "period_to": "2026-09-10",
            "assembly_remaining_qty": "6",
        },
    }]
    rows_new = [{**rows_old[0], "payload": {**rows_old[0]["payload"], "assembly_remaining_qty": "2"}}]
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM current_execution_change WHERE scope_key=:scope"), {"scope": scope})
            conn.execute(sa.text("DELETE FROM current_execution_row WHERE scope_key=:scope"), {"scope": scope})
            conn.execute(sa.text("DELETE FROM current_execution_scope WHERE scope_key=:scope"), {"scope": scope})
        seed = Session()
        try:
            publish_current_execution_scope(seed, source_revision="accepted:r8-1", scope_key=scope, rows=rows_old, entity_kinds=("assembly_queue",))
            seed.commit()
        finally:
            seed.close()

        reader, writer = Session(), Session()
        try:
            before = reader.query(models.CurrentExecutionRow).filter_by(scope_key=scope, business_identity=identity).one()
            row_id = int(before.id)
            assert before.payload["assembly_remaining_qty"] == "6"
            publish_current_execution_scope(writer, source_revision="accepted:r8-2", scope_key=scope, rows=rows_new, entity_kinds=("assembly_queue",))
            writer.flush()
            assert reader.query(models.CurrentExecutionRow).filter_by(scope_key=scope, business_identity=identity).one().payload["assembly_remaining_qty"] == "6"
            writer.commit()
            reader.rollback()
            reader.expire_all()
            after = reader.query(models.CurrentExecutionRow).filter_by(scope_key=scope, business_identity=identity).one()
            assert int(after.id) == row_id
            assert after.payload["assembly_remaining_qty"] == "2"
            changes = reader.query(models.CurrentExecutionChange).filter_by(scope_key=scope).count()
        finally:
            reader.close()
            writer.close()

        retry_a, retry_b = Session(), Session()
        try:
            assert publish_current_execution_scope(retry_a, source_revision="accepted:r8-3", scope_key=scope, rows=rows_new, entity_kinds=("assembly_queue",)).idempotent
            retry_a.commit()
            assert publish_current_execution_scope(retry_b, source_revision="accepted:r8-4", scope_key=scope, rows=rows_new, entity_kinds=("assembly_queue",)).idempotent
            retry_b.commit()
            with engine.connect() as conn:
                assert conn.execute(sa.text("SELECT count(*) FROM current_execution_change WHERE scope_key=:scope"), {"scope": scope}).scalar_one() == changes
        finally:
            retry_a.close()
            retry_b.close()
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM current_execution_change WHERE scope_key=:scope"), {"scope": scope})
            conn.execute(sa.text("DELETE FROM current_execution_row WHERE scope_key=:scope"), {"scope": scope})
            conn.execute(sa.text("DELETE FROM current_execution_scope WHERE scope_key=:scope"), {"scope": scope})
        engine.dispose()

"""R10 purchase-export anchor cutover contracts (PostgreSQL only)."""

from __future__ import annotations

import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.pool import NullPool


def _dsn() -> str:
    value = os.getenv("PRODPLAN_R2_TEST_DSN")
    if not value:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    from app.r2_local_contract import validate_r2_dsn

    validate_r2_dsn(value)
    return value


def _migration():
    path = Path(__file__).resolve().parents[2] / "backend" / "alembic" / "versions" / "20260911_02_r10_purchase_anchor_cutover.py"
    spec = spec_from_file_location("r10_purchase_anchor_cutover", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(conn, schema: str):
    conn.execute(sa.text(f"CREATE SCHEMA {schema}"))
    conn.execute(sa.text(f"SET search_path TO {schema}"))
    conn.execute(sa.text(
        "CREATE TABLE planning_read_snapshot (id BIGINT PRIMARY KEY)"
    ))
    conn.execute(sa.text(
        "CREATE TABLE ledger_generation (id BIGINT PRIMARY KEY)"
    ))
    conn.execute(sa.text(
        "CREATE TABLE current_execution_scope (id BIGINT PRIMARY KEY)"
    ))
    conn.execute(sa.text(
        "CREATE TABLE purchase_export_batch ("
        "id BIGINT PRIMARY KEY, ledger_generation_id BIGINT NOT NULL, "
        "planning_read_snapshot_id BIGINT, current_execution_scope_id BIGINT, "
        "current_execution_source_revision VARCHAR(256), idempotency_key VARCHAR(128) NOT NULL, "
        "CONSTRAINT fk_purchase_export_batch_planning_read_snapshot FOREIGN KEY (planning_read_snapshot_id) REFERENCES planning_read_snapshot(id), "
        "CONSTRAINT fk_purchase_export_batch_ledger_generation FOREIGN KEY (ledger_generation_id) REFERENCES ledger_generation(id), "
        "CONSTRAINT fk_purchase_export_batch_current_execution_scope FOREIGN KEY (current_execution_scope_id) REFERENCES current_execution_scope(id), "
        "CONSTRAINT ck_purchase_export_batch_exactly_one_source_anchor CHECK ("
        "(planning_read_snapshot_id IS NOT NULL AND current_execution_scope_id IS NULL AND current_execution_source_revision IS NULL) OR "
        "(planning_read_snapshot_id IS NULL AND current_execution_scope_id IS NOT NULL AND current_execution_source_revision IS NOT NULL))"
        ")"
    ))
    conn.execute(sa.text(
        "CREATE INDEX ix_purchase_export_batch_planning_read_snapshot_id ON purchase_export_batch(planning_read_snapshot_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX ix_purchase_export_batch_current_execution_scope_id ON purchase_export_batch(current_execution_scope_id)"
    ))


def _run(module, conn, name: str):
    context = MigrationContext.configure(conn)
    module.op = Operations(context)
    return getattr(module, name)()


@pytest.mark.integration
def test_cutover_blocks_unmigrated_legacy_anchor_and_downgrade_is_explicitly_unsafe():
    dsn = _dsn()
    schema = f"r10_cutover_bad_{uuid4().hex}"
    engine = sa.create_engine(dsn, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            _schema(conn, schema)
            conn.execute(sa.text("INSERT INTO ledger_generation(id) VALUES (1)"))
            conn.execute(sa.text("INSERT INTO planning_read_snapshot(id) VALUES (10)"))
            conn.execute(sa.text(
                "INSERT INTO purchase_export_batch(id,ledger_generation_id,planning_read_snapshot_id,idempotency_key) "
                "VALUES (1,1,10,'bad')"
            ))
            module = _migration()
            with pytest.raises(RuntimeError, match="legacy|current anchor"):
                _run(module, conn, "upgrade")
            assert conn.execute(sa.text("SELECT planning_read_snapshot_id FROM purchase_export_batch")).scalar_one() == 10
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        engine.dispose()


@pytest.mark.integration
def test_cutover_removes_legacy_anchor_after_verified_current_mapping_and_downgrade_fails_closed():
    dsn = _dsn()
    schema = f"r10_cutover_good_{uuid4().hex}"
    engine = sa.create_engine(dsn, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            _schema(conn, schema)
            conn.execute(sa.text("INSERT INTO ledger_generation(id) VALUES (1)"))
            conn.execute(sa.text("INSERT INTO current_execution_scope(id) VALUES (20)"))
            conn.execute(sa.text(
                "INSERT INTO purchase_export_batch(id,ledger_generation_id,current_execution_scope_id,current_execution_source_revision,idempotency_key) "
                "VALUES (1,1,20,'accepted:g1:purchase_control_journal','good')"
            ))
            module = _migration()
            _run(module, conn, "upgrade")
            columns = [row[0] for row in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=:schema AND table_name='purchase_export_batch'"
            ), {"schema": schema})]
            assert "planning_read_snapshot_id" not in columns
            assert conn.execute(sa.text(
                "SELECT current_execution_scope_id,current_execution_source_revision FROM purchase_export_batch"
            )).one() == (20, "accepted:g1:purchase_control_journal")
            with pytest.raises(RuntimeError, match="downgrade|backup|irreversible"):
                _run(module, conn, "downgrade")
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        engine.dispose()

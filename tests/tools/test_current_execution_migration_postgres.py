"""R10 local PostgreSQL rehearsal in an isolated schema.

The fixture is deliberately policy-level: it exercises the real migration
runner against PostgreSQL while the canonical publisher seam is supplied by a
small deterministic publisher.  No public-schema table is dropped or
rewritten, and the fixture includes the legacy evidence categories whose
checksums must remain untouched.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

from tools.current_execution_migration import (
    EXPECTED_CURRENT_SCOPES,
    apply_current_obligation_migration,
    build_manifest,
)


def _dsn() -> str:
    value = os.getenv("PRODPLAN_R2_TEST_DSN")
    if not value:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    from app.r2_local_contract import validate_r2_dsn

    validate_r2_dsn(value)
    return value


def _schema_sql(schema: str) -> str:
    return f"""
    CREATE SCHEMA {schema};
    CREATE TABLE {schema}.planning_truth_state (
        id INTEGER PRIMARY KEY, current_generation_id BIGINT
    );
    CREATE TABLE {schema}.ledger_generation (
        id BIGINT PRIMARY KEY, generation_key TEXT NOT NULL, status TEXT NOT NULL
    );
    CREATE TABLE {schema}.planning_run (
        run_id INTEGER PRIMARY KEY, source_plan_id INTEGER, ledger_generation_id BIGINT,
        status TEXT NOT NULL
    );
    CREATE TABLE {schema}.planning_read_snapshot (
        id BIGINT PRIMARY KEY, consumer TEXT NOT NULL, snapshot_key TEXT NOT NULL,
        ledger_generation_id BIGINT NOT NULL, truth_status TEXT NOT NULL
    );
    CREATE TABLE {schema}.current_execution_scope (
        id BIGINT PRIMARY KEY, entity_kind TEXT NOT NULL, scope_key TEXT NOT NULL,
        source_generation_id BIGINT, source_revision TEXT NOT NULL,
        result_ready BOOLEAN NOT NULL, content_hash TEXT NOT NULL, summary JSONB NOT NULL
    );
    CREATE TABLE {schema}.current_execution_row (
        id BIGINT PRIMARY KEY, entity_kind TEXT NOT NULL, scope_key TEXT NOT NULL,
        business_identity TEXT NOT NULL, source_revision TEXT NOT NULL,
        source_generation_id BIGINT, result_status TEXT NOT NULL, result_ready BOOLEAN NOT NULL,
        content_hash TEXT NOT NULL, payload JSONB NOT NULL
    );
    CREATE TABLE {schema}.current_execution_change (
        id BIGINT PRIMARY KEY, current_row_id BIGINT, entity_kind TEXT NOT NULL,
        business_identity TEXT NOT NULL, scope_key TEXT NOT NULL, source_revision TEXT NOT NULL,
        operation TEXT NOT NULL, reason TEXT NOT NULL, before_payload JSONB, after_payload JSONB
    );
    CREATE TABLE {schema}.closed_plan_snapshot (
        id BIGINT PRIMARY KEY, plan_id INTEGER NOT NULL, run_id INTEGER NOT NULL,
        ledger_generation_id BIGINT NOT NULL, payload JSONB NOT NULL
    );
    CREATE TABLE {schema}.stock_ledger_entry (
        id BIGINT PRIMARY KEY, ledger_generation_id BIGINT NOT NULL, business_identity TEXT NOT NULL,
        qty NUMERIC(15,3) NOT NULL
    );
    CREATE TABLE {schema}.stock_ledger_fact_supersession (
        id BIGINT PRIMARY KEY, old_sle_id BIGINT NOT NULL, new_sle_id BIGINT,
        import_batch_id BIGINT NOT NULL
    );
    CREATE TABLE {schema}.mrp_freeze_baseline (
        id BIGINT PRIMARY KEY, run_id INTEGER NOT NULL, frozen_basis_generation_id BIGINT,
        item_id INTEGER NOT NULL
    );
    CREATE TABLE {schema}.sync_link (
        link_id BIGINT PRIMARY KEY, source_doctype TEXT NOT NULL, source_id INTEGER NOT NULL,
        target_entity TEXT NOT NULL, target_ref_key TEXT, ledger_generation_id BIGINT
    );
    CREATE TABLE {schema}.purchase_export_batch (
        id BIGINT PRIMARY KEY, ledger_generation_id BIGINT NOT NULL,
        planning_read_snapshot_id BIGINT, current_execution_scope_id BIGINT,
        current_execution_source_revision TEXT, idempotency_key TEXT NOT NULL
    );
    """


def _seed(conn):
    conn.execute(sa.text(
        "INSERT INTO ledger_generation(id,generation_key,status) VALUES "
        "(101,'r10-old','accepted'),(102,'r10-pointed','accepted'),(103,'r10-newer-unpointed','accepted')"
    ))
    conn.execute(sa.text("INSERT INTO planning_truth_state(id,current_generation_id) VALUES (1,102)"))
    conn.execute(sa.text(
        "INSERT INTO planning_run(run_id,source_plan_id,ledger_generation_id,status) VALUES "
        "(41,7,102,'FIXED_SNAPSHOT'),(42,8,102,'FIXED_SNAPSHOT'),(43,9,101,'CLOSED')"
    ))
    rows = [
        (1, "production_control_journal", "journal:v1", 102),
        (2, "purchase_control_journal", "journal:v1", 102),
        (3, "mrp_result", "run:41", 102),
        (4, "mrp_result", "run:42", 102),
        (5, "period_plan_execution", "plan=7;run=41", 102),
        (6, "period_plan_execution", "plan=8;run=42", 102),
        # Historical duplicate copies remain evidence and are not selected.
        (7, "production_control_journal", "journal:v1", 101),
        (8, "purchase_control_journal", "journal:v1", 101),
    ]
    conn.execute(sa.text(
        "INSERT INTO planning_read_snapshot(id,consumer,snapshot_key,ledger_generation_id,truth_status) "
        "VALUES (:id,:consumer,:snapshot_key,:generation_id,'accepted')"
    ), [
        {"id": row[0], "consumer": row[1], "snapshot_key": row[2], "generation_id": row[3]}
        for row in rows
    ])
    conn.execute(sa.text(
        "INSERT INTO closed_plan_snapshot(id,plan_id,run_id,ledger_generation_id,payload) "
        "VALUES (1,9,43,101,'{""closed"":true}'::jsonb)"
    ))
    conn.execute(sa.text(
        "INSERT INTO stock_ledger_entry(id,ledger_generation_id,business_identity,qty) VALUES "
        "(1,101,'doc:R10:1',10),(2,102,'doc:R10:1',12)"
    ))
    conn.execute(sa.text(
        "INSERT INTO stock_ledger_fact_supersession(id,old_sle_id,new_sle_id,import_batch_id) "
        "VALUES (1,1,2,102)"
    ))
    conn.execute(sa.text(
        "INSERT INTO mrp_freeze_baseline(id,run_id,frozen_basis_generation_id,item_id) VALUES (1,41,101,1001)"
    ))
    conn.execute(sa.text(
        "INSERT INTO sync_link(link_id,source_doctype,source_id,target_entity,target_ref_key,ledger_generation_id) "
        "VALUES (1,'R10',1,'Document_X','ref-r10',101)"
    ))
    conn.execute(sa.text(
        "INSERT INTO purchase_export_batch(id,ledger_generation_id,planning_read_snapshot_id,current_execution_scope_id,current_execution_source_revision,idempotency_key) "
        "VALUES (1,101,1,NULL,NULL,'r10-export')"
    ))


def _publisher(session, generation_id: int):
    """Deterministic current writer used by the isolated policy rehearsal."""
    if session.execute(sa.text("SELECT count(*) FROM current_execution_scope")).scalar_one():
        return {}
    for index, (consumer, scope_key, entity_kind) in enumerate(EXPECTED_CURRENT_SCOPES, start=1):
        revision = f"accepted:g{generation_id}:{consumer}"
        session.execute(sa.text(
            "INSERT INTO current_execution_scope "
            "(id,entity_kind,scope_key,source_generation_id,source_revision,result_ready,content_hash,summary) "
            "VALUES (:id,:kind,:scope,:generation,:revision,true,'r10-scope','{}'::jsonb)"
        ), {"id": index, "kind": entity_kind, "scope": scope_key,
            "generation": generation_id, "revision": revision})
        row_id = index * 10
        identity = f"{entity_kind}:r10"
        session.execute(sa.text(
            "INSERT INTO current_execution_row "
            "(id,entity_kind,scope_key,business_identity,source_revision,source_generation_id,result_status,result_ready,content_hash,payload) "
            "VALUES (:id,:kind,:scope,:identity,:revision,:generation,'accepted',true,'r10-row','{}'::jsonb)"
        ), {"id": row_id, "kind": entity_kind, "scope": scope_key,
            "identity": identity, "revision": revision, "generation": generation_id})
        session.execute(sa.text(
            "INSERT INTO current_execution_change "
            "(id,current_row_id,entity_kind,business_identity,scope_key,source_revision,operation,reason) "
            "VALUES (:id,:row_id,:kind,:identity,:scope,:revision,'insert','r10-rehearsal')"
        ), {"id": index, "row_id": row_id, "kind": entity_kind, "identity": identity,
            "scope": scope_key, "revision": revision})
    return {consumer: {"idempotent": False} for consumer, *_ in EXPECTED_CURRENT_SCOPES}


@pytest.mark.integration
def test_r10_postgres_rehearsal_isolated_pointer_atomic_and_idempotent(monkeypatch):
    dsn = _dsn()
    schema = f"r10_rehearsal_{uuid4().hex}"
    admin = sa.create_engine(dsn, poolclass=NullPool)
    scoped = None
    try:
        with admin.begin() as connection:
            connection.execute(sa.text(_schema_sql(schema)))
        scoped = sa.create_engine(
            dsn,
            poolclass=NullPool,
            connect_args={"options": f"-csearch_path={schema}"},
        )
        with scoped.begin() as connection:
            _seed(connection)

        from tools import current_execution_migration as migration
        monkeypatch.setattr(migration, "publish_current_obligation_views_from_generation", _publisher)

        before = build_manifest(scoped)
        with pytest.raises(RuntimeError, match="fault injection"):
            apply_current_obligation_migration(
                scoped, writers_stopped=True, fault_after_consumer="mrp_result"
            )
        with scoped.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM current_execution_scope")).scalar_one() == 0
            assert connection.execute(sa.text("SELECT count(*) FROM current_execution_row")).scalar_one() == 0
            assert connection.execute(sa.text("SELECT count(*) FROM current_execution_change")).scalar_one() == 0
        assert build_manifest(scoped) == before

        first = apply_current_obligation_migration(scoped, writers_stopped=True)
        second = apply_current_obligation_migration(scoped, writers_stopped=True)
        assert first["generation_id"] == 102
        assert first["source_evidence"]["status"] == "ready"
        assert second["idempotent"] is True
        assert second["change_rows_after"] == first["change_rows_after"]
        with scoped.connect() as connection:
            assert connection.execute(sa.text(
                "SELECT count(*) FROM current_execution_scope WHERE source_generation_id=102"
            )).scalar_one() == 4
            assert connection.execute(sa.text(
                "SELECT count(*) FROM current_execution_row WHERE source_generation_id=103"
            )).scalar_one() == 0
        after = build_manifest(scoped)
        for table in (
            "closed_plan_snapshot", "stock_ledger_entry", "stock_ledger_fact_supersession",
            "mrp_freeze_baseline", "sync_link", "purchase_export_batch",
        ):
            assert after["categories"]["preserve" if table in {"stock_ledger_entry", "stock_ledger_fact_supersession", "mrp_freeze_baseline", "sync_link", "purchase_export_batch"} else "delete"][table]["checksum"] == before["categories"]["preserve" if table in {"stock_ledger_entry", "stock_ledger_fact_supersession", "mrp_freeze_baseline", "sync_link", "purchase_export_batch"} else "delete"][table]["checksum"]
    finally:
        if scoped is not None:
            scoped.dispose()
        with admin.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin.dispose()

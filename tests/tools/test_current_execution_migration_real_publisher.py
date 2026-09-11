"""R10 rehearsal using the real obligation current publisher on PostgreSQL."""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import models
from tools.current_execution_migration import apply_current_obligation_migration, build_manifest


def _dsn() -> str:
    value = os.getenv("PRODPLAN_R2_TEST_DSN")
    if not value:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    from app.r2_local_contract import validate_r2_dsn

    validate_r2_dsn(value)
    return value


def _seed(session: Session):
    stamp = datetime(2026, 9, 11, tzinfo=timezone.utc)
    item = models.Item(item_id=1001, item_code="R10-REAL-1001", item_name="R10 real fixture")
    session.add(item)
    batches = []
    for suffix in ("old", "pointed", "newer"):
        batch = models.PhysicalImportBatch(
            batch_key=f"r10-real-{suffix}-{uuid4().hex}",
            status="completed",
            cutoff=stamp,
            source_watermarks={},
            completed_at=stamp,
            source_complete=True,
        )
        session.add(batch)
        batches.append(batch)
    session.flush()
    generations = []
    for index, (suffix, batch) in enumerate(zip(("old", "pointed", "newer"), batches), start=101):
        generation = models.LedgerGeneration(
            generation_key=f"r10-real-{suffix}-{uuid4().hex}",
            status="accepted",
            cutoff=stamp,
            source_watermarks={},
            capabilities={},
            physical_import_batch_id=batch.id,
            algorithm_version="r10-real-test",
            accepted_at=stamp,
        )
        session.add(generation)
        generations.append(generation)
    session.flush()
    old_generation, pointed_generation, newer_generation = generations
    session.add(models.PlanningTruthState(id=1, current_generation_id=pointed_generation.id))

    plans = []
    for plan_id in (7, 8, 9):
        plan = models.ProductionPlanHeader(
            id=plan_id,
            name=f"R10 real plan {plan_id}",
            period_from=date(2026, 9, 11),
            period_to=date(2026, 9, 12),
            status="fixed" if plan_id < 9 else "closed",
        )
        session.add(plan)
        plans.append(plan)
    session.flush()
    run41 = models.PlanningRun(
        run_id=41, source_plan_id=7, ledger_generation_id=pointed_generation.id,
        status="FIXED_SNAPSHOT", config_snapshot={}, started_at=stamp,
    )
    run42 = models.PlanningRun(
        run_id=42, source_plan_id=8, ledger_generation_id=pointed_generation.id,
        status="FIXED_SNAPSHOT", config_snapshot={}, started_at=stamp,
    )
    closed_run = models.PlanningRun(
        run_id=43, source_plan_id=9, ledger_generation_id=old_generation.id,
        status="CLOSED", config_snapshot={}, started_at=stamp,
    )
    session.add_all((run41, run42, closed_run))
    session.flush()

    snapshots = []
    def snapshot(consumer, key, generation, payload):
        value = models.PlanningReadSnapshot(
            consumer=consumer,
            snapshot_key=key,
            ledger_generation_id=generation.id,
            cutoff=stamp,
            truth_status="accepted",
            payload=payload,
            published_at=stamp,
        )
        snapshots.append(value)
        session.add(value)
        return value

    production = snapshot("production_control_journal", "journal:v1", pointed_generation, {"meta": {}})
    purchase = snapshot("purchase_control_journal", "journal:v1", pointed_generation, {"meta": {}, "rows": []})
    snapshot("mrp_result", "run:41", pointed_generation, {"summary": {}})
    snapshot("mrp_result", "run:42", pointed_generation, {"summary": {}})
    snapshot("period_plan_execution", "plan=7;run=41", pointed_generation, {"plan": {"id": 7}, "run_id": 41, "rows": []})
    snapshot("period_plan_execution", "plan=8;run=42", pointed_generation, {"plan": {"id": 8}, "run_id": 42, "rows": []})
    # Historical copies are retained evidence and must not be selected.
    snapshot("production_control_journal", "journal:v1", old_generation, {"meta": {"historical": True}})
    snapshot("purchase_control_journal", "journal:v1", old_generation, {"meta": {"historical": True}, "rows": []})
    session.flush()

    production_row = models.PlanningReadRow(
        snapshot_id=production.id,
        row_key="work-item:1001",
        row_kind="production",
        item_id=item.item_id,
        sort_key="2026-09-11|000000001001",
        payload={
            "order_id": 9001,
            "product_id": 1001,
            "item_id": 1001,
            "source_mrp_requirement_id": 501,
            "planned_qty": 2,
        },
    )
    session.add(production_row)
    session.flush()
    session.add(models.PlanningReadRootMember(
        snapshot_id=production.id,
        row_id=production_row.id,
        root_key="root:1001",
        root_item_id=item.item_id,
        payload={},
    ))

    # Feasible evidence anchors in the real schema.
    session.add(models.ClosedPlanSnapshot(
        plan_id=9, run_id=43, ledger_generation_id=old_generation.id,
        cutoff=stamp, payload={}, closed_at=stamp,
    ))
    session.add(models.MrpFreezeBaseline(
        run_id=41, freeze_version=1, item_id=item.item_id,
        frozen_at=stamp, frozen_basis_generation_id=old_generation.id,
        planning_stock_pool="default",
    ))
    session.add(models.SyncLink(
        source_doctype="R10Real", source_id=1, target_entity="Document_X",
        target_ref_key="r10-real-ref", ledger_generation_id=old_generation.id,
        status="success",
    ))
    purchase_scope = models.CurrentExecutionScope(
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
        source_generation_id=old_generation.id,
        source_revision=f"accepted:g{old_generation.id}:purchase_control_journal",
        result_ready=True,
        content_hash="r" * 64,
        summary={},
    )
    session.add(purchase_scope)
    session.flush()
    session.add(models.PurchaseExportBatch(
        ledger_generation_id=old_generation.id,
        current_execution_scope_id=purchase_scope.id,
        current_execution_source_revision=purchase_scope.source_revision,
        idempotency_key="r10-real-export",
        status="completed",
    ))
    session.commit()
    return pointed_generation.id, newer_generation.id, production_row.id


@pytest.mark.integration
def test_r10_real_publisher_rehearsal_uses_pointer_and_compacts_current(monkeypatch):
    dsn = _dsn()
    schema = f"r10_real_{uuid4().hex}"
    admin = sa.create_engine(dsn, poolclass=NullPool)
    scoped = None
    try:
        with admin.begin() as connection:
            connection.execute(sa.text(f"CREATE SCHEMA {schema}"))
        scoped = sa.create_engine(
            dsn,
            poolclass=NullPool,
            connect_args={"options": f"-csearch_path={schema}"},
        )
        with scoped.begin() as connection:
            models.Base.metadata.create_all(connection)
        with Session(scoped) as session:
            pointed_id, newer_id, _row_id = _seed(session)

        before = build_manifest(scoped)
        first = apply_current_obligation_migration(scoped, writers_stopped=True)
        assert first["generation_id"] == pointed_id
        assert first["source_evidence"]["status"] == "ready"
        with scoped.connect() as connection:
            assert connection.execute(sa.text(
                "SELECT count(*) FROM current_execution_scope WHERE source_generation_id=:generation"
            ), {"generation": pointed_id}).scalar_one() == 4
            assert connection.execute(sa.text(
                "SELECT count(*) FROM current_execution_scope WHERE source_generation_id=:generation"
            ), {"generation": newer_id}).scalar_one() == 0
            identities = connection.execute(sa.text(
                "SELECT entity_kind,business_identity,source_revision,payload::text "
                "FROM current_execution_row ORDER BY entity_kind,business_identity"
            )).all()
            assert any(row[1] == "production-mrp-requirement:501:1001" for row in identities)
            assert all(str(row[2]).startswith(f"accepted:g{pointed_id}:") for row in identities)
            assert all("work-item:1001" not in str(row[3]) for row in identities)
            change_count = connection.execute(sa.text("SELECT count(*) FROM current_execution_change")).scalar_one()

        second = apply_current_obligation_migration(scoped, writers_stopped=True)
        assert second["idempotent"] is True
        assert second["change_rows_after"] == change_count
        after = build_manifest(scoped)
        for table in ("closed_plan_snapshot", "mrp_freeze_baseline", "sync_link"):
            assert after["categories"]["preserve"][table]["checksum"] == before["categories"]["preserve"][table]["checksum"]
        with scoped.connect() as connection:
            anchor = connection.execute(sa.text(
                "SELECT current_execution_scope_id, current_execution_source_revision, idempotency_key "
                "FROM purchase_export_batch WHERE id=1"
            )).one()
            assert anchor[0] is not None
            assert anchor[1].startswith(f"accepted:g{pointed_id}:purchase_control_journal")
            assert anchor[2] == "r10-real-export"
    finally:
        if scoped is not None:
            scoped.dispose()
        with admin.begin() as connection:
            connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin.dispose()

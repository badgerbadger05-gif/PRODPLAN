from datetime import datetime, timezone
from decimal import Decimal
import asyncio

import pytest
from fastapi import HTTPException

from app import models
from app.routers import plan as plan_router
from app.services import mrp_result_snapshot
from app.services.mrp_result_snapshot import (
    build_mrp_result_snapshot,
    read_mrp_result_manifest,
    read_mrp_result_rows,
)


def _accepted_generation(db):
    physical = models.PhysicalImportBatch(
        batch_key="mrp-result-snapshot-physical",
        status="completed",
        cutoff=datetime(2026, 7, 23, tzinfo=timezone.utc),
        source_watermarks={},
        completed_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    db.add(physical)
    db.flush()
    generation = models.LedgerGeneration(
        generation_key="mrp-result-snapshot-test",
        status="accepted",
        cutoff=datetime(2026, 7, 23, tzinfo=timezone.utc),
        accepted_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
        capabilities={
            "execution_allocations": True,
            "planning_snapshots": True,
        },
        source_watermarks={},
        physical_import_batch_id=physical.id,
        algorithm_version="test",
    )
    db.add(generation)
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    db.flush()
    return generation


def test_missing_snapshot_fails_closed_without_reading_planning_rows(db_session):
    generation = _accepted_generation(db_session)
    item = models.Item(item_code="MISSING-SNAPSHOT", item_name="Legacy-looking row")
    db_session.add(item)
    db_session.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        models.PlannedPurchase(
            run_id=run.run_id,
            item_id=item.item_id,
            requested_qty=Decimal("99"),
            planned_qty=Decimal("99"),
            qty=Decimal("99"),
            need_date=datetime(2026, 8, 1).date(),
            order_date=datetime(2026, 7, 1).date(),
            lead_time_days=1,
            bucket_date=datetime(2026, 8, 1).date(),
        )
    )

    result = read_mrp_result_rows(
        db_session, run.run_id, row_kind="purchase"
    )

    assert result["truth_status"] == "accepted"
    assert result["rows"] == []
    assert result["total"] == 0
    assert "missing" in result["truth_reason"]


def test_builder_publishes_rows_and_frozen_root_membership(
    db_session, monkeypatch
):
    generation = _accepted_generation(db_session)
    root = models.Item(item_code="ROOT-S", item_name="Root")
    component = models.Item(item_code="COMP-S", item_name="Component")
    db_session.add_all([root, component])
    db_session.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=4,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        models.MrpFreezeComponent(
            run_id=run.run_id,
            freeze_version=4,
            parent_item_id=root.item_id,
            component_item_id=component.item_id,
            spec_ref="frozen-spec",
            norm_qty_per_unit=Decimal("2"),
            unit_coef=Decimal("1"),
        )
    )
    db_session.add(
        models.PlannedPurchase(
            run_id=run.run_id,
            item_id=component.item_id,
            requested_qty=Decimal("5"),
            planned_qty=Decimal("5"),
            qty=Decimal("5"),
            need_date=datetime(2026, 8, 1).date(),
            order_date=datetime(2026, 7, 1).date(),
            lead_time_days=1,
            bucket_date=datetime(2026, 8, 1).date(),
            ledger_generation_id=generation.id,
        )
    )
    area = models.ProductionResource(resource_name="Snapshot area")
    db_session.add(area)
    db_session.flush()
    db_session.add(
        models.CapacityLoad(
            run_id=run.run_id,
            area_id=area.resource_id,
            bucket_date=datetime(2026, 8, 2).date(),
            hours_planned=Decimal("10"),
            hours_available=Decimal("8"),
            overload_hours=Decimal("2"),
        )
    )
    db_session.flush()

    snapshot = build_mrp_result_snapshot(db_session, run.run_id)
    result = read_mrp_result_rows(
        db_session,
        run.run_id,
        row_kind="purchase",
        snapshot_id=snapshot.id,
        root_item_id=root.item_id,
    )
    manifest = read_mrp_result_manifest(
        db_session, run.run_id, snapshot_id=snapshot.id
    )
    capacity = read_mrp_result_rows(
        db_session,
        run.run_id,
        row_kind="capacity",
        snapshot_id=snapshot.id,
        area_id=area.resource_id,
        date_from="2026-08-02",
        date_to="2026-08-02",
    )
    monkeypatch.setattr(
        plan_router,
        "get_run_capacity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("legacy capacity getter was called")
        ),
    )
    capacity_endpoint = asyncio.run(
        plan_router.get_planning_result_capacity(
            run_id=run.run_id,
            area_id=area.resource_id,
            bucket_type=None,
            date_from="2026-08-02",
            date_to="2026-08-02",
            limit=200,
            offset=0,
            snapshot_id=snapshot.id,
            db=db_session,
        )
    )

    assert result["snapshot_id"] == snapshot.id
    assert result["ledger_generation"] == generation.id
    assert result["total"] == 1
    assert result["rows"][0]["item_id"] == component.item_id
    assert manifest["snapshot_counts"]["purchase"] == 1
    assert manifest["snapshot_counts"]["capacity"] == 1
    assert capacity["snapshot_id"] == result["snapshot_id"] == snapshot.id
    assert capacity_endpoint["snapshot_id"] == snapshot.id
    assert capacity["total"] == 1
    assert capacity["rows"][0]["overload_hours"] == 2.0
    member = db_session.query(models.PlanningReadRootMember).one()
    assert member.root_item_id == root.item_id
    assert member.payload == {"source": "mrp_freeze_component"}


def test_snapshot_id_cannot_cross_run_or_generation(db_session):
    generation = _accepted_generation(db_session)
    run_a = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    run_b = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db_session.add_all([run_a, run_b])
    db_session.flush()
    snapshot = models.PlanningReadSnapshot(
        consumer="mrp_result",
        snapshot_key=f"run:{run_a.run_id}",
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff,
        truth_status="accepted",
        payload={"run_id": run_a.run_id},
        published_at=datetime.now(timezone.utc),
    )
    db_session.add(snapshot)
    db_session.flush()

    result = read_mrp_result_rows(
        db_session,
        run_b.run_id,
        row_kind="production",
        snapshot_id=snapshot.id,
    )

    assert result["rows"] == []
    assert result["snapshot_id"] is None


def test_builder_rejects_legacy_obligation_without_generation(db_session):
    generation = _accepted_generation(db_session)
    item = models.Item(item_code="LEGACY-PURCHASE", item_name="Legacy purchase")
    db_session.add(item)
    db_session.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        models.PlannedPurchase(
            run_id=run.run_id,
            item_id=item.item_id,
            requested_qty=Decimal("7"),
            planned_qty=Decimal("7"),
            qty=Decimal("7"),
            need_date=datetime(2026, 8, 1).date(),
            order_date=datetime(2026, 7, 1).date(),
            lead_time_days=1,
            bucket_date=datetime(2026, 8, 1).date(),
            ledger_generation_id=None,
        )
    )
    db_session.flush()

    with pytest.raises(ValueError, match="NULL or foreign"):
        build_mrp_result_snapshot(db_session, run.run_id)

    assert db_session.query(models.PlanningReadSnapshot).count() == 0


def test_builder_failure_rolls_back_manifest_rows_and_memberships(
    db_session, monkeypatch
):
    generation = _accepted_generation(db_session)
    item = models.Item(item_code="ATOMIC-PURCHASE", item_name="Atomic purchase")
    db_session.add(item)
    db_session.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        models.PlannedPurchase(
            run_id=run.run_id,
            item_id=item.item_id,
            requested_qty=Decimal("3"),
            planned_qty=Decimal("3"),
            qty=Decimal("3"),
            need_date=datetime(2026, 8, 1).date(),
            order_date=datetime(2026, 7, 1).date(),
            lead_time_days=1,
            bucket_date=datetime(2026, 8, 1).date(),
            ledger_generation_id=generation.id,
        )
    )
    db_session.flush()

    def fail_after_manifest_and_rows(*args, **kwargs):
        raise RuntimeError("injected root membership failure")

    monkeypatch.setattr(
        mrp_result_snapshot, "_frozen_root_membership", fail_after_manifest_and_rows
    )
    with pytest.raises(RuntimeError, match="injected"):
        build_mrp_result_snapshot(db_session, run.run_id)

    assert db_session.query(models.PlanningReadSnapshot).count() == 0
    assert db_session.query(models.PlanningReadRow).count() == 0
    assert db_session.query(models.PlanningReadRootMember).count() == 0


def test_unmigrated_legacy_get_is_rejected_before_service_call(monkeypatch):
    def legacy_service_must_not_run(*args, **kwargs):
        raise AssertionError("legacy capacity service was called")

    monkeypatch.setattr(
        plan_router, "get_run_pegging", legacy_service_must_not_run
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            plan_router.get_planning_result_pegging(
                run_id=123,
                child_item_id=None,
                parent_item_id=None,
                date_from=None,
                date_to=None,
                limit=200,
                offset=0,
                db=None,
            )
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "mrp_result_snapshot_required"
    assert exc.value.detail["rows"] == []


def test_purchase_export_reads_shared_snapshot_not_legacy_getter(
    db_session, monkeypatch
):
    generation = _accepted_generation(db_session)
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()
    snapshot = models.PlanningReadSnapshot(
        consumer="mrp_result",
        snapshot_key=f"run:{run.run_id}",
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff,
        truth_status="accepted",
        payload={
            "run_id": run.run_id,
            "row_counts": {"purchase": 1},
            "total_qty": {"purchase": 4.0},
        },
        published_at=datetime.now(timezone.utc),
    )
    db_session.add(snapshot)
    db_session.flush()
    db_session.add(
        models.PlanningReadRow(
            snapshot_id=snapshot.id,
            row_key="purchase:one",
            row_kind="purchase",
            sort_key="2026-08-01|000000000001|000000000000",
            payload={
                "item_id": 1,
                "item_name": "Snapshot item",
                "item_article": "S-1",
                "qty": 4.0,
                "unit": "шт.",
                "bucket_date": "2026-08-01",
            },
        )
    )
    db_session.flush()

    def legacy_service_must_not_run(*args, **kwargs):
        raise AssertionError("legacy purchase getter was called")

    monkeypatch.setattr(
        plan_router, "get_run_purchases", legacy_service_must_not_run
    )
    result = asyncio.run(
        plan_router.export_planning_result_purchases(
            run_id=run.run_id,
            format="csv",
            root_item_id=None,
            bucket_type=None,
            date_from=None,
            date_to=None,
            sort_by=None,
            sort_dir=None,
            snapshot_id=snapshot.id,
            db=db_session,
        )
    )

    assert result["snapshot_id"] == snapshot.id
    assert result["total_rows"] == 1
    assert "Snapshot item" in result["data"]

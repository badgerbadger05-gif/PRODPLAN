"""End-to-end contract tests for the caller-owned refresh orchestrator."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.services import obligation_refresh_orchestrator as workflow
from app.services.mrp_result_snapshot import read_mrp_result_manifest
from app.services.item_ledger.generation_lifecycle import (
    RESERVATION_CONSUMPTION_ALGORITHM_VERSION,
)
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C
from app.services.obligation_refresh_publish import ObligationRefreshPublishError
from app.services.planning_pool_resolver import PlanningPoolConfigurationError


def _world(db, *, with_parent=True, qty=5, replenishment_method="Покупка", period_from=date(2026, 8, 1)):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="orchestrator-physical", status="completed", cutoff=cutoff,
        source_watermarks={"opening_at": "2025-01-01T00:00:00+00:00"},
        completed_at=cutoff,
    )
    accepted = models.LedgerGeneration(
        generation_key="orchestrator-accepted", status="accepted", cutoff=cutoff,
        source_watermarks={"replay_from": "2026-07-01T00:00:00+00:00"},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
        physical_import_batch=physical, algorithm_version="test", accepted_at=cutoff,
    )
    item = models.Item(item_code="ORCH-PURCHASE", item_name="orchestrator purchase",
                       replenishment_method=replenishment_method)
    resource = models.ProductionResource(
        resource_name="Orchestrator assembly",
        planning_range=30,
        capacity=Decimal("100"),
    )
    warehouse = models.StockWarehouse(
        warehouse_ref1c="WH-OUT",
        warehouse_name="Planning contour",
        is_selected=True,
        is_finished_goods=False,
    )
    db.add_all([physical, accepted, item, warehouse, resource]); db.flush()
    db.add(models.AssemblyRate(
        resource_id=resource.resource_id,
        item_id=item.item_id,
        qty_per_capacity=Decimal("1"),
    ))
    db.add(models.PlanningTruthState(id=1, current_generation_id=accepted.id))
    db.add(models.ProductionMaterialCustodyProjectionManifest(
        ledger_generation_id=accepted.id,
        cutoff=cutoff,
        status="complete",
        is_baseline=True,
        source_event_high_watermark_id=0,
        observed_at=cutoff,
        built_at=cutoff,
    ))
    plan = models.ProductionPlanHeader(name="orchestrator plan", status="fixed",
        period_from=period_from, period_to=date(2026, 8, 31), fixed_at=cutoff)
    db.add(plan); db.flush()
    line = models.ProductionPlanLine(plan_id=plan.id, item_id=item.item_id,
        bucket_date=period_from, qty=Decimal(str(qty)))
    db.add(line); db.flush()
    parent = None
    if with_parent:
        parent = models.PlanningRun(status="FIXED_SNAPSHOT", ledger_generation_id=accepted.id,
            source_plan_id=plan.id, period_from=plan.period_from, period_to=plan.period_to,
            config_snapshot={}, started_at=cutoff, fixed_at=cutoff, finished_at=cutoff,
            pinned=True, active_freeze_version=1)
        db.add(parent); db.flush()
    db.commit()
    return accepted, plan, line, item, parent, cutoff


def _run(db, parent, key, *, add=(), config=None, pool_mapping=None):
    return workflow.run_obligation_refresh(
        db, parent_generation_id=parent.id, generation_key=key, add_plan_ids=add,
        started_by="test", horizon_days=30, config_version_id=None,
        config_snapshot=config or {},
        planning_pool_by_warehouse=pool_mapping,
        accepted_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )


def test_add_only_builds_real_checkpoints_and_promotes_persisted_read_snapshot(db_session):
    accepted, plan, _line, _item, _old, _cutoff = _world(db_session, with_parent=False)
    result = _run(db_session, accepted, "orch-add", add=[plan.id], config={"first": True})
    target = db_session.get(models.LedgerGeneration, result.target_generation_id)
    candidate = db_session.query(models.PlanningRun).filter_by(
        ledger_generation_id=target.id, status="FIXED_SNAPSHOT").one()
    batches = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=target.id).all()
    by_stage = {row.stage: row for row in batches}

    assert result.published is True
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == target.id
    assert target.capabilities == {
        "physical_ledger": True, "reservation_replay": True,
        "execution_allocations": True, "reservation_consumption_allocation": True,
        "supplier_receipt_coverage": True,
        "planning_snapshots": True,
        "replenishment_work_item": True,
        "assembly_output_allocation": True,
        "assembly_queue": True,
        "drum_schedule": True,
        "shelf_projection": True,
        "purchase_control_journal": True,
        "production_control_journal": True,
        "future_supply": True,
    }
    assert {
        "physical_import",
        "reservation_materialize",
        "execution_allocation",
        "replenishment_work_item",
        "reservation_replay",
        "assembly_output_allocation",
        "drum_schedule",
        "shelf_projection",
        "future_supply_capture",
        "snapshot_build",
    } == set(by_stage)
    assert all(row.status == "completed" for row in by_stage.values())
    assert by_stage["snapshot_build"].metrics["future_supply_captured"] is True
    assert by_stage["future_supply_capture"].metrics["rows"] == 0
    snapshot_id = by_stage["snapshot_build"].metrics["candidate_read_snapshot_ids"][str(candidate.run_id)]
    assert db_session.get(models.PlanningReadSnapshot, snapshot_id).truth_status == "accepted"
    production_journal_id = by_stage["snapshot_build"].metrics[
        "production_control_journal_snapshot_id"
    ]
    production_journal = db_session.get(
        models.PlanningReadSnapshot,
        production_journal_id,
    )
    assert production_journal.consumer == "production_control_journal"
    assert production_journal.truth_status == "accepted"
    # This public read function consumes the stored snapshot; it does not run MRP.
    assert read_mrp_result_manifest(db_session, candidate.run_id)["run_id"] == candidate.run_id
    # Journals must not go dark after a refresh: every published generation
    # carries its own period-plan execution snapshots (decisions-log /).
    execution_snapshots = db_session.query(models.PlanningReadSnapshot).filter_by(
        ledger_generation_id=target.id, consumer="period_plan_execution").all()
    assert len(execution_snapshots) == 1
    assert all(row.truth_status == "accepted" for row in execution_snapshots)
    queue = db_session.query(models.PlanningReadSnapshot).filter_by(
        ledger_generation_id=target.id,
        consumer="assembly_queue",
    ).one()
    assert queue.payload["total_rows"] == 1
    assert queue.payload["total_queue_qty"] == 5.0


def test_single_stage_reuses_execution_batch_with_reservation_consumption_algorithm_version(
    db_session,
):
    accepted, plan, _line, _item, _old, _cutoff = _world(
        db_session,
        with_parent=False,
    )
    result = _run(db_session, accepted, "orch-single-stage-execution-version", add=[plan.id])
    target = db_session.get(models.LedgerGeneration, result.target_generation_id)
    execution_batch = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=target.id,
        stage="execution_allocation",
    ).one()

    assert str(execution_batch.algorithm_version) == RESERVATION_CONSUMPTION_ALGORITHM_VERSION


def test_production_refresh_resolves_live_pool_for_supplier_and_wip(db_session):
    accepted, plan, _line, _item, _old, cutoff = _world(
        db_session,
        with_parent=False,
    )
    supplier_item = models.Item(
        item_code="ORCH-SUPPLY",
        item_name="orchestrator supplier supply",
        replenishment_method="Покупка",
    )
    wip_item = models.Item(
        item_code="ORCH-WIP",
        item_name="orchestrator WIP supply",
        replenishment_method="Производство",
    )
    db_session.add_all([supplier_item, wip_item])
    db_session.flush()

    supplier_order = models.SupplierOrder(
        order_number="SO-ORCH",
        order_ref1c="so-orch-ref",
        order_date=cutoff - timedelta(days=3),
        order_state_name="В пути",
        deletion_mark=False,
        created_at=cutoff - timedelta(days=3),
        updated_at=cutoff - timedelta(days=1),
    )
    production_order = models.ProductionOrder(
        order_number="WO-ORCH",
        order_ref1c="wo-orch-ref",
        order_date=cutoff - timedelta(days=2),
        order_state_key="open",
        deletion_mark=False,
        created_at=cutoff - timedelta(days=2),
        updated_at=cutoff - timedelta(days=1),
    )
    db_session.add_all([supplier_order, production_order])
    db_session.flush()
    db_session.add(
        models.SupplierOrderItem(
            order_id=supplier_order.order_id,
            item_id_ref=supplier_item.item_id,
            line_number=1,
            destination_warehouse_ref1c="WH-OUT",
            quantity=Decimal("7"),
            received_qty=Decimal("0"),
            remaining_qty=Decimal("7"),
            delivery_date=cutoff + timedelta(days=10),
            created_at=cutoff - timedelta(days=2),
            updated_at=cutoff - timedelta(days=1),
        )
    )
    product = models.ProductionProduct(
        order_id=production_order.order_id,
        item_id=wip_item.item_id,
        line_number=1,
        destination_warehouse_ref1c="WH-OUT",
        quantity=Decimal("4"),
        produced_qty=Decimal("0"),
        remaining_qty=Decimal("4"),
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        models.ProductionOrderLineState(
            product_id=product.product_id,
            status="ready",
            issue_status="not_requested",
            planned_finish_date=(cutoff + timedelta(days=8)).date(),
        )
    )
    db_session.commit()

    result = _run(
        db_session,
        accepted,
        "orch-live-planning-pools",
        add=[plan.id],
    )
    exact = (
        db_session.query(models.LedgerFutureSupply)
        .filter_by(
            ledger_generation_id=result.target_generation_id,
            evidence_status="exact",
        )
        .all()
    )
    by_kind = {row.supply_kind: row for row in exact}

    assert set(by_kind) >= {"supplier_order", "wip_order"}
    assert by_kind["supplier_order"].planning_stock_pool == "default"
    assert by_kind["wip_order"].planning_stock_pool == "default"
    assert by_kind["supplier_order"].open_qty_at_cutoff == Decimal("7")
    assert by_kind["wip_order"].open_qty_at_cutoff == Decimal("4")


def test_production_refresh_rejects_only_lines_outside_the_live_contour(db_session):
    """One stray destination must cost its own line, never the whole refresh."""
    accepted, plan, _line, _item, _old, cutoff = _world(
        db_session,
        with_parent=False,
    )
    # A finished-goods warehouse is live in 1C but deliberately outside the
    # planning contour; production routinely releases output into it.
    db_session.add(
        models.StockWarehouse(
            warehouse_ref1c="WH-FG",
            warehouse_name="Finished goods",
            is_selected=True,
            is_finished_goods=True,
        )
    )
    supplier_item = models.Item(
        item_code="ORCH-MIXED-SUPPLY",
        item_name="mixed supplier supply",
        replenishment_method="Покупка",
    )
    wip_item = models.Item(
        item_code="ORCH-MIXED-WIP",
        item_name="mixed WIP supply",
        replenishment_method="Производство",
    )
    db_session.add_all([supplier_item, wip_item])
    db_session.flush()

    supplier_order = models.SupplierOrder(
        order_number="SO-MIXED",
        order_ref1c="so-mixed-ref",
        order_date=cutoff - timedelta(days=3),
        order_state_name="В пути",
        deletion_mark=False,
        created_at=cutoff - timedelta(days=3),
        updated_at=cutoff - timedelta(days=1),
    )
    production_order = models.ProductionOrder(
        order_number="WO-MIXED",
        order_ref1c="wo-mixed-ref",
        order_date=cutoff - timedelta(days=2),
        order_state_key="open",
        deletion_mark=False,
        created_at=cutoff - timedelta(days=2),
        updated_at=cutoff - timedelta(days=1),
    )
    db_session.add_all([supplier_order, production_order])
    db_session.flush()
    for line_number, destination, qty in (
        (1, "WH-OUT", "7"),
        (2, "WH-NOT-IN-CONTOUR", "3"),
    ):
        db_session.add(
            models.SupplierOrderItem(
                order_id=supplier_order.order_id,
                item_id_ref=supplier_item.item_id,
                line_number=line_number,
                destination_warehouse_ref1c=destination,
                quantity=Decimal(qty),
                received_qty=Decimal("0"),
                remaining_qty=Decimal(qty),
                delivery_date=cutoff + timedelta(days=10),
                created_at=cutoff - timedelta(days=2),
                updated_at=cutoff - timedelta(days=1),
            )
        )
    for line_number, destination, qty in ((1, "WH-OUT", "4"), (2, "WH-FG", "9")):
        db_session.add(
            models.ProductionProduct(
                order_id=production_order.order_id,
                item_id=wip_item.item_id,
                line_number=line_number,
                destination_warehouse_ref1c=destination,
                quantity=Decimal(qty),
                produced_qty=Decimal("0"),
                remaining_qty=Decimal(qty),
            )
        )
    db_session.commit()

    result = _run(
        db_session,
        accepted,
        "orch-mixed-planning-pool",
        add=[plan.id],
    )

    assert result.published is True
    rows = (
        db_session.query(models.LedgerFutureSupply)
        .filter_by(ledger_generation_id=result.target_generation_id)
        .all()
    )
    by_line = {
        (row.supply_kind, row.source_line_ref): row for row in rows
    }
    assert by_line[("supplier_order", "1")].evidence_status == "exact"
    assert by_line[("supplier_order", "1")].planning_stock_pool == "default"
    assert by_line[("supplier_order", "1")].open_qty_at_cutoff == Decimal("7")
    assert by_line[("wip_order", "1")].evidence_status == "exact"
    assert by_line[("wip_order", "1")].planning_stock_pool == "default"
    assert by_line[("wip_order", "1")].open_qty_at_cutoff == Decimal("4")
    for identity in (("supplier_order", "2"), ("wip_order", "2")):
        rejected = by_line[identity]
        assert rejected.evidence_status == "rejected"
        assert rejected.reason == "planning_pool_not_mapped"
        assert rejected.planning_stock_pool == ""
        assert rejected.open_qty_at_cutoff == Decimal("0")

    capture = (
        db_session.query(models.LedgerBuildBatch)
        .filter_by(
            ledger_generation_id=result.target_generation_id, stage="future_supply_capture"
        )
        .one()
        .metrics["future_supply_capture"]
    )
    assert capture["rows"] == 4
    assert capture["exact_rows"] == 2
    assert capture["non_supply_rows"] == 2


def test_production_refresh_fails_before_build_when_planning_contour_is_empty(
    db_session,
):
    accepted, plan, _line, _item, _old, _cutoff = _world(
        db_session,
        with_parent=False,
    )
    warehouse = db_session.query(models.StockWarehouse).one()
    warehouse.is_selected = False
    db_session.commit()

    with pytest.raises(
        PlanningPoolConfigurationError,
        match="planning warehouse contour is empty",
    ):
        _run(
            db_session,
            accepted,
            "orch-empty-planning-pool",
            add=[plan.id],
        )

    assert (
        db_session.query(models.LedgerGeneration)
        .filter_by(generation_key="orch-empty-planning-pool")
        .count()
        == 0
    )


def test_add_plans_with_mixed_periods_fail_closed(db_session):
    accepted, plan, _line, _item, _old, _cutoff = _world(db_session, with_parent=False)
    other = models.ProductionPlanHeader(
        name="orchestrator plan sep", status="fixed",
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
    )
    db_session.add(other)
    db_session.commit()
    with pytest.raises(
        workflow.ObligationRefreshOrchestratorError,
        match="different period_from",
    ):
        _run(db_session, accepted, "orch-mixed", add=[plan.id, other.id])


def test_add_retains_existing_fixed_obligation_without_refreeze(db_session):
    accepted, plan, _line, _item, old, _cutoff = _world(db_session)
    old_run_id = int(old.run_id)
    old_freeze_version = old.active_freeze_version
    old_requirements = [
        (int(row.id), Decimal(row.net_required_qty))
        for row in db_session.query(models.MrpRequirement)
        .filter_by(run_id=old_run_id)
        .order_by(models.MrpRequirement.id)
    ]
    extra = models.ProductionPlanHeader(name="new", status="fixed",
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30), fixed_at=_cutoff)
    db_session.add(extra); db_session.flush()
    item = db_session.query(models.Item).filter_by(item_code="ORCH-PURCHASE").one()
    db_session.add(models.ProductionPlanLine(plan_id=extra.id, item_id=item.item_id,
        bucket_date=extra.period_from, qty=Decimal("2")))
    db_session.commit()

    result = _run(db_session, accepted, "orch-refresh-add", add=[extra.id], config={"add": 1})
    rows = db_session.query(models.PlanningRun).filter(
        models.PlanningRun.run_id.in_(result.candidate_run_ids)).all()
    assert old.status == "FIXED_SNAPSHOT"
    assert old.run_id == old_run_id
    assert old.active_freeze_version == old_freeze_version
    assert [
        (int(row.id), Decimal(row.net_required_qty))
        for row in db_session.query(models.MrpRequirement)
        .filter_by(run_id=old_run_id)
        .order_by(models.MrpRequirement.id)
    ] == old_requirements
    assert {row.source_plan_id for row in rows} == {extra.id}
    assert all(row.status == "FIXED_SNAPSHOT" and row.pinned for row in rows)


def test_failure_after_freeze_is_reversible_by_outer_transaction(db_session, monkeypatch):
    accepted, _plan, line, _item, old, _cutoff = _world(db_session)
    def fail(*_args, **_kwargs):
        raise RuntimeError("injected after freeze")
    monkeypatch.setattr(workflow, "replay_candidate_realizations", fail)
    outer = db_session.begin()
    with pytest.raises(RuntimeError, match="injected"):
        _run(db_session, accepted, "orch-rollback")
    outer.rollback()
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == accepted.id
    assert old.status == "FIXED_SNAPSHOT"
    assert line.locked_by_run_id is None
    assert db_session.query(models.LedgerGeneration).filter_by(generation_key="orch-rollback").count() == 0


def test_replays_all_realizations_before_materializing_work_items(
    db_session, monkeypatch
):
    accepted, plan, _line, _item, _old, _cutoff = _world(
        db_session,
        with_parent=False,
    )
    calls: list[str] = []

    def record(name, original):
        def wrapper(*args, **kwargs):
            calls.append(name)
            return original(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        workflow,
        "replay_candidate_realizations",
        record("make_replay", workflow.replay_candidate_realizations),
    )
    monkeypatch.setattr(
        workflow,
        "rebuild_supplier_receipt_coverage_from_persisted_provenance",
        record(
            "supplier_replay",
            workflow.rebuild_supplier_receipt_coverage_from_persisted_provenance,
        ),
    )
    monkeypatch.setattr(
        workflow,
        "materialize_replenishment_work_items",
        record("work_items", workflow.materialize_replenishment_work_items),
    )

    _run(db_session, accepted, "orch-replay-before-work-items", add=[plan.id])

    assert calls.index("make_replay") < calls.index("work_items")
    assert calls.index("supplier_replay") < calls.index("work_items")


def test_committed_exact_retry_is_publisher_noop_and_changed_request_is_rejected(db_session):
    accepted, plan, _line, _item, _old, _cutoff = _world(db_session, with_parent=False)
    first = _run(db_session, accepted, "orch-retry", add=[plan.id], config={"v": 1})
    db_session.commit()
    second = _run(db_session, accepted, "orch-retry", add=[plan.id], config={"v": 1})
    assert second.target_generation_id == first.target_generation_id
    assert second.published is False
    # Public callers resolve the parent from the current pointer after a
    # transport timeout.  That pointer now names the published target; exact
    # retry must recover the historical parent from sealed lineage.
    current = db_session.get(models.LedgerGeneration, first.target_generation_id)
    pointer_retry = _run(db_session, current, "orch-retry", add=[plan.id], config={"v": 1})
    assert pointer_retry.target_generation_id == first.target_generation_id
    assert pointer_retry.published is False
    with pytest.raises(workflow.ObligationRefreshOrchestratorError, match="conflicting retry"):
        _run(db_session, accepted, "orch-retry", add=[plan.id], config={"v": 2})


def test_committed_retry_rejects_changed_planning_pool_mapping(db_session):
    accepted, plan, _line, _item, _old, _cutoff = _world(
        db_session, with_parent=False
    )
    first = _run(
        db_session,
        accepted,
        "orch-pool-retry",
        add=[plan.id],
        pool_mapping={"WH-1": "main"},
    )
    db_session.commit()

    exact = _run(
        db_session,
        accepted,
        "orch-pool-retry",
        add=[plan.id],
        pool_mapping={"WH-1": "main"},
    )
    assert exact.target_generation_id == first.target_generation_id
    assert exact.published is False

    with pytest.raises(
        workflow.ObligationRefreshOrchestratorError,
        match="conflicting retry",
    ):
        _run(
            db_session,
            accepted,
            "orch-pool-retry",
            add=[plan.id],
            pool_mapping={"WH-1": "other"},
        )


def test_stale_parent_is_rejected_before_published_retry(db_session):
    accepted, plan, _line, _item, _old, _cutoff = _world(db_session, with_parent=False)
    _run(db_session, accepted, "orch-stale", add=[plan.id])
    # Simulate another accepted generation winning the pointer after publication.
    current = db_session.get(models.PlanningTruthState, 1)
    current.current_generation_id = accepted.id
    db_session.flush()
    with pytest.raises(workflow.ObligationRefreshOrchestratorError, match="published retry requires target"):
        _run(db_session, accepted, "orch-stale", add=[plan.id])

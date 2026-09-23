"""End-to-end contract tests for the caller-owned refresh orchestrator."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from decimal import Decimal

import pytest

from app import models
from app.services import obligation_refresh_orchestrator as workflow
from app.services.mrp_result_projection import read_mrp_result_manifest
from app.services.item_ledger.generation_lifecycle import (
    RESERVATION_CONSUMPTION_ALGORITHM_VERSION,
)
from app.services.item_ledger.obligation_generation import ObligationGenerationError
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
            "planning_snapshots": True,
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
            pinned=True, active_freeze_version=1, ledger_cutoff=cutoff)
        db.add(parent); db.flush()
        line.accepted_output_qty = Decimal("0")
        line.remaining_output_qty = Decimal(str(qty))
        line.locked_by_run_id = int(parent.run_id)
        db.add(models.MrpRunRoot(
            run_id=int(parent.run_id),
            plan_line_id=int(line.id),
            planned_qty=Decimal(str(qty)),
            accepted_qty=Decimal("0"),
            remaining_qty=Decimal(str(qty)),
        ))
    db.commit()
    return accepted, plan, line, item, parent, cutoff


def _run(
    db,
    parent,
    key,
    *,
    add=(),
    retire=(),
    replace=(),
    config=None,
    pool_mapping=None,
):
    return workflow.run_obligation_refresh(
        db, parent_generation_id=parent.id, generation_key=key, add_plan_ids=add,
        retire_plan_ids=retire,
        replace_plan_ids=replace,
        started_by="test", horizon_days=30, config_version_id=None,
        config_snapshot=config or {},
        planning_pool_by_warehouse=pool_mapping,
        accepted_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )


def _accepted_physical_fork(db, parent, *, key):
    physical = models.PhysicalImportBatch(
        batch_key=f"{key}-physical",
        status="completed",
        cutoff=parent.cutoff,
        source_watermarks={},
        completed_at=parent.cutoff,
    )
    child = models.LedgerGeneration(
        generation_key=key,
        status="accepted",
        cutoff=parent.cutoff,
        accepted_at=parent.cutoff,
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": int(parent.id),
            "replay_from": (parent.source_watermarks or {})["replay_from"],
        },
        capabilities=dict(parent.capabilities or {}),
        physical_import_batch=physical,
        algorithm_version="test",
    )
    db.add(child)
    db.flush()
    db.get(models.PlanningTruthState, 1).current_generation_id = int(child.id)
    db.flush()
    return child


def test_replacement_is_end_to_end_same_plan_saved_remainder_with_history_replay(
    db_session,
    monkeypatch,
):
    accepted, plan, line, _item, parent, _cutoff = _world(
        db_session,
        qty=12,
    )
    line.accepted_output_qty = Decimal("10")
    line.remaining_output_qty = Decimal("2")
    line.locked_by_run_id = parent.run_id
    parent_root = db_session.query(models.MrpRunRoot).filter_by(
        run_id=int(parent.run_id), plan_line_id=int(line.id)
    ).one()
    parent_root.accepted_qty = Decimal("10")
    parent_root.remaining_qty = Decimal("2")
    db_session.commit()
    preserve_flags = []
    real_carry = workflow.carry_forward_retained_reservations

    def capture_carry(*args, **kwargs):
        preserve_flags.append(kwargs.get("preserve_realization"))
        return real_carry(*args, **kwargs)

    monkeypatch.setattr(
        workflow,
        "carry_forward_retained_reservations",
        capture_carry,
    )

    result = _run(
        db_session,
        accepted,
        "orch-replace-saved-remainder",
        replace=[plan.id],
    )

    candidate = db_session.query(models.PlanningRun).filter_by(
        prior_run_id=parent.run_id,
    ).one()
    root = db_session.query(models.MrpRunRoot).filter_by(
        run_id=candidate.run_id,
    ).one()
    replay_batch = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=result.target_generation_id,
        stage="reservation_replay",
    ).one()
    execution_scope = db_session.query(models.CurrentExecutionScope).filter_by(
        entity_kind="period_plan_execution",
        scope_key="period-plan:all-live-plans",
    ).one()
    execution_snapshot = execution_scope.summary["snapshots"][
        f"plan:{int(plan.id)}:run:{int(candidate.run_id)}"
    ]
    requirement = db_session.query(models.MrpRequirement).filter_by(
        run_id=candidate.run_id,
    ).one()
    queue_line = db_session.query(models.AssemblyQueueLine).filter_by(
        ledger_generation_id=result.target_generation_id,
        planning_run_id=candidate.run_id,
        plan_line_id=line.id,
    ).one()
    readiness = db_session.query(models.AssemblyReadiness).filter_by(
        ledger_generation_id=result.target_generation_id,
        assembly_queue_line_id=queue_line.id,
    ).one()

    assert result.published is True
    assert db_session.query(models.ProductionPlanHeader).count() == 1
    assert int(candidate.source_plan_id) == int(plan.id)
    assert parent.status == "CLOSED"
    assert candidate.status == "FIXED_SNAPSHOT"
    assert root.planned_qty == Decimal("2")
    assert root.accepted_qty == Decimal("0")
    assert root.remaining_qty == Decimal("2")
    assert requirement.total_required_qty == Decimal("2")
    assert requirement.net_required_qty == Decimal("2")
    assert line.qty == Decimal("12")
    assert line.accepted_output_qty == Decimal("10")
    assert line.remaining_output_qty == Decimal("2")
    assert line.locked_by_run_id == candidate.run_id
    assert replay_batch.status == "completed"
    assert replay_batch.metrics["facts"] == 0
    assert "replay_summary" not in replay_batch.metrics
    assert execution_snapshot["summary"]["execution_completed_qty"] == 0
    assert execution_snapshot["summary"]["execution_base_qty"] == 2
    assert queue_line.assembly_remaining_qty == Decimal("2")
    assert readiness.open_qty == Decimal("2")
    assembly_scope = db_session.query(models.CurrentExecutionScope).filter_by(
        entity_kind="assembly_queue",
        scope_key="assembly:all-live-plans",
    ).one()
    assert int(assembly_scope.source_generation_id) == int(result.target_generation_id)
    snapshot_batch = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=int(result.target_generation_id),
        stage="snapshot_build",
    ).one()
    assert "assembly_queue_materialization_id" not in dict(snapshot_batch.metrics or {})
    # Current refreshes must not invoke the legacy generation-copy writer;
    # retained obligations stay anchored to their stable run identity.
    assert preserve_flags == []


def test_replacement_uses_saved_remainder_through_multiple_accepted_fact_forks(db_session):
    accepted, plan, line, _item, parent, cutoff = _world(
        db_session,
        qty=12,
    )
    parent.ledger_cutoff = cutoff
    line.accepted_output_qty = Decimal("7")
    line.remaining_output_qty = Decimal("5")
    line.locked_by_run_id = int(parent.run_id)
    parent_root = db_session.query(models.MrpRunRoot).filter_by(
        run_id=int(parent.run_id), plan_line_id=int(line.id)
    ).one()
    parent_root.accepted_qty = Decimal("7")
    parent_root.remaining_qty = Decimal("5")
    first_fact_fork = _accepted_physical_fork(
        db_session, accepted, key="orch-fact-fork-1"
    )
    second_fact_fork = _accepted_physical_fork(
        db_session, first_fact_fork, key="orch-fact-fork-2"
    )
    db_session.commit()

    result = _run(
        db_session,
        second_fact_fork,
        "orch-replace-after-two-fact-forks",
        replace=[plan.id],
    )

    candidate = db_session.query(models.PlanningRun).filter_by(
        prior_run_id=int(parent.run_id)
    ).one()
    root = db_session.query(models.MrpRunRoot).filter_by(
        run_id=int(candidate.run_id)
    ).one()
    requirement = db_session.query(models.MrpRequirement).filter_by(
        run_id=int(candidate.run_id)
    ).one()
    queue_line = db_session.query(models.AssemblyQueueLine).filter_by(
        ledger_generation_id=int(result.target_generation_id),
        planning_run_id=int(candidate.run_id),
        plan_line_id=int(line.id),
    ).one()
    readiness = db_session.query(models.AssemblyReadiness).filter_by(
        ledger_generation_id=int(result.target_generation_id),
        assembly_queue_line_id=int(queue_line.id),
    ).one()

    assert parent.status == "CLOSED"
    assert candidate.status == "FIXED_SNAPSHOT"
    assert root.planned_qty == Decimal("5")
    assert root.accepted_qty == Decimal("0")
    assert root.remaining_qty == Decimal("5")
    assert requirement.total_required_qty == Decimal("5")
    assert requirement.net_required_qty == Decimal("5")
    assert line.qty == Decimal("12")
    assert line.accepted_output_qty == Decimal("7")
    assert line.remaining_output_qty == Decimal("5")
    assert queue_line.assembly_remaining_qty == Decimal("5")
    assert readiness.open_qty == Decimal("5")


@pytest.mark.parametrize("truth_state", ["missing", "unaccepted", "incomplete"])
def test_replacement_fails_closed_without_complete_accepted_ledger(
    db_session,
    truth_state,
):
    accepted, plan, line, _item, parent, _cutoff = _world(
        db_session,
        qty=12,
    )
    line.accepted_output_qty = Decimal("7")
    line.remaining_output_qty = Decimal("5")
    parent_root = db_session.query(models.MrpRunRoot).filter_by(
        run_id=int(parent.run_id), plan_line_id=int(line.id)
    ).one()
    parent_root.accepted_qty = Decimal("7")
    parent_root.remaining_qty = Decimal("5")
    if truth_state == "missing":
        db_session.delete(db_session.get(models.PlanningTruthState, 1))
    elif truth_state == "unaccepted":
        accepted.status = "building"
        accepted.accepted_at = None
    else:
        accepted.physical_import_batch.status = "building"
    db_session.flush()

    with pytest.raises(
        (workflow.ObligationRefreshOrchestratorError, ObligationGenerationError)
    ) as exc_info:
        _run(
            db_session,
            accepted,
            f"orch-replacement-{truth_state}-ledger",
            replace=[plan.id],
        )

    assert "accepted" in str(exc_info.value).lower() or "truth" in str(
        exc_info.value
    ).lower() or "physical" in str(exc_info.value).lower()
    assert db_session.query(models.PlanningRun).filter(
        models.PlanningRun.prior_run_id == int(parent.run_id)
    ).count() == 0


def test_zero_remainder_retires_mrp_without_recreating_demand(db_session):
    accepted, plan, line, item, parent, _cutoff = _world(
        db_session,
        qty=12,
    )
    line.accepted_output_qty = Decimal("12")
    line.remaining_output_qty = Decimal("0")
    parent_root = db_session.query(models.MrpRunRoot).filter_by(
        run_id=int(parent.run_id), plan_line_id=int(line.id)
    ).one()
    parent_root.accepted_qty = Decimal("12")
    parent_root.remaining_qty = Decimal("0")
    requirement = models.MrpRequirement(
        run_id=int(parent.run_id),
        item_id=int(item.item_id),
        total_required_qty=Decimal("12"),
        net_required_qty=Decimal("12"),
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=0,
        status="open",
    )
    db_session.add(requirement)
    db_session.flush()
    db_session.add(models.ReservationEntry(
        ledger_generation_id=int(accepted.id),
        item_id=int(item.item_id),
        run_id=int(parent.run_id),
        requirement_id=int(requirement.id),
        freeze_version=1,
        reserved_qty=Decimal("12"),
        replenishment_required_qty=Decimal("12"),
        replenishment_received_qty=Decimal("12"),
        lifecycle_status="active",
        realization_mode="buy",
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
    ))
    db_session.commit()

    result = _run(
        db_session,
        accepted,
        "orch-retire-zero-remainder",
        retire=[plan.id],
    )

    db_session.refresh(requirement)
    assert result.published is True
    assert parent.status == "CLOSED"
    assert plan.status == "closed"
    assert requirement.status == "closed"
    assert requirement.closed_at is not None
    assert line.qty == Decimal("12")
    assert line.accepted_output_qty == Decimal("12")
    assert line.remaining_output_qty == Decimal("0")
    assert db_session.query(models.PlanningRun).filter(
        models.PlanningRun.prior_run_id == int(parent.run_id)
    ).count() == 0
    assert db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=int(result.target_generation_id),
        run_id=int(parent.run_id),
        lifecycle_status="active",
    ).count() == 0
    assert db_session.query(models.AssemblyQueueLine).filter_by(
        ledger_generation_id=int(result.target_generation_id),
        plan_line_id=int(line.id),
    ).count() == 0


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
            "assembly_readiness": True,
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
            "assembly_readiness",
            "drum_schedule",
        "shelf_projection",
        "future_supply_capture",
        "snapshot_build",
    } == set(by_stage)
    assert all(row.status == "completed" for row in by_stage.values())
    assert by_stage["snapshot_build"].metrics["future_supply_captured"] is True
    assert by_stage["future_supply_capture"].metrics["rows"] == 0
    checkpoint = by_stage["snapshot_build"].metrics["current_scope_checkpoint"]
    assert checkpoint["version"] == 1
    assert all(
        {"scope_id", "source_generation_id", "source_revision", "content_hash", "row_count"}
        <= set(entry)
        for entry in checkpoint["scopes"]
    )
    production_scope = db_session.query(models.CurrentExecutionScope).filter_by(
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    ).one()
    assert production_scope.source_generation_id == target.id
    mrp_scope = db_session.query(models.CurrentExecutionScope).filter_by(
        entity_kind="mrp_result", scope_key="mrp:all-live-plans",
    ).one()
    assert mrp_scope.source_generation_id == target.id
    # This public read function consumes the stored current row; it does not run MRP.
    assert read_mrp_result_manifest(db_session, candidate.run_id)["run_id"] == candidate.run_id
    # Journals must not go dark after a refresh: the current period manifest
    # carries each live plan/run payload without copying historical snapshots.
    execution_scope = db_session.query(models.CurrentExecutionScope).filter_by(
        entity_kind="period_plan_execution",
        scope_key="period-plan:all-live-plans",
    ).one()
    execution_snapshots = execution_scope.summary["snapshots"]
    assert len(execution_snapshots) == 1
    assert all(row["truth_status"] == "accepted" for row in execution_snapshots.values())
    queue_scope = db_session.query(models.CurrentExecutionScope).filter_by(
        entity_kind="assembly_queue",
        scope_key="assembly:all-live-plans",
    ).one()
    assert queue_scope.source_generation_id == target.id
    assert queue_scope.summary["total_rows"] == 1
    assert queue_scope.summary["total_queue_qty"] == "5.000"
    queue_rows = db_session.query(models.CurrentExecutionRow).filter_by(
        scope_key="assembly:all-live-plans",
        entity_kind="assembly_queue",
    ).all()
    assert len(queue_rows) == 1
    assert queue_rows[0].payload["assembly_remaining_qty"] == "5.000"


def test_obligation_refresh_publishes_purchase_current_without_purchase_snapshot(
    db_session,
):
    accepted, _plan, _line, _item, parent, _cutoff = _world(
        db_session,
        with_parent=True,
        qty=5,
    )

    result = _run(db_session, accepted, "orch-purchase-current-direct")
    target = db_session.get(models.LedgerGeneration, result.target_generation_id)

    manifest = db_session.query(models.CurrentExecutionScope).filter_by(
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    ).one()
    assert manifest.source_generation_id == target.id
    assert manifest.source_revision == f"accepted:g{target.id}:purchase_control_journal"


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
    # Accepted publication owns future supply in the compact current table;
    # generation rows are bounded staging and are removed after publication.
    exact = db_session.query(models.LedgerFutureSupplyCurrent).all()
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
    # Rejected contour evidence remains represented by the capture checkpoint;
    # only exact business owners survive in the accepted current table.
    rows = db_session.query(models.LedgerFutureSupplyCurrent).all()
    by_line = {
        (row.supply_kind, row.source_line_ref): row for row in rows
    }
    assert by_line[("supplier_order", "1")].evidence_status == "exact"
    assert by_line[("supplier_order", "1")].planning_stock_pool == "default"
    assert by_line[("supplier_order", "1")].open_qty_at_cutoff == Decimal("7")
    assert by_line[("wip_order", "1")].evidence_status == "exact"
    assert by_line[("wip_order", "1")].planning_stock_pool == "default"
    assert by_line[("wip_order", "1")].open_qty_at_cutoff == Decimal("4")
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
    requirement = models.MrpRequirement(
        run_id=int(old.run_id), item_id=int(_item.item_id),
        total_required_qty=Decimal("5"), net_required_qty=Decimal("5"),
        period_from=plan.period_from, period_to=plan.period_to,
        bom_level=0, status="open",
    )
    db_session.add(requirement)
    db_session.flush()
    old_requirements = [
        (int(row.id), Decimal(row.net_required_qty))
        for row in db_session.query(models.MrpRequirement)
        .filter_by(run_id=old_run_id)
        .order_by(models.MrpRequirement.id)
    ]
    db_session.add(models.MrpFreezeBaseline(
        run_id=int(old.run_id), freeze_version=1, item_id=int(_item.item_id),
        characteristic_ref="", organization_ref="", planning_stock_pool="default",
        frozen_at=_cutoff, baseline_at=_cutoff,
        physical_import_batch_id=int(accepted.physical_import_batch_id),
        frozen_basis_generation_id=int(accepted.id), stock_qty=Decimal("0"),
        produced_total=Decimal("0"), received_total=Decimal("0"), unit_coef=Decimal("1"),
    ))
    db_session.flush()
    retained_reservation = models.ReservationEntry(
        ledger_generation_id=int(accepted.id), item_id=int(_item.item_id),
        run_id=int(old.run_id), requirement_id=int(requirement.id),
        freeze_version=1, reserved_qty=Decimal("5"),
        replenishment_required_qty=Decimal("5"),
        replenishment_received_qty=Decimal("1"), realized_qty=Decimal("1"),
        lifecycle_status="active", realization_mode="buy",
        priority_period_from=plan.period_from, priority_period_to=plan.period_to,
        opened_at=_cutoff,
    )
    db_session.add(retained_reservation)
    db_session.flush()
    retained_before = {
        "id": int(retained_reservation.id),
        "generation_id": int(retained_reservation.ledger_generation_id),
        "run_id": int(retained_reservation.run_id),
        "reserved": retained_reservation.reserved_qty,
        "received": retained_reservation.replenishment_received_qty,
    }
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
    db_session.refresh(old)
    db_session.refresh(retained_reservation)
    assert {
        "id": int(retained_reservation.id),
        "generation_id": int(retained_reservation.ledger_generation_id),
        "run_id": int(retained_reservation.run_id),
        "reserved": retained_reservation.reserved_qty,
        "received": retained_reservation.replenishment_received_qty,
    } == retained_before
    assert db_session.query(models.ReservationEntry).filter(
        models.ReservationEntry.ledger_generation_id == int(result.target_generation_id),
        models.ReservationEntry.run_id == int(old.run_id),
    ).count() == 0


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
    assert line.locked_by_run_id == int(old.run_id)
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


def _current_owner_for_parent_run(db, accepted, plan, item, parent):
    """The stable current owner a refresh of this plan has to supersede."""
    requirement = models.MrpRequirement(
        run_id=int(parent.run_id),
        item_id=int(item.item_id),
        total_required_qty=Decimal("5"),
        net_required_qty=Decimal("5"),
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=0,
        planning_stock_pool="selected",
        characteristic_ref="",
        organization_ref="",
        freeze_version=1,
    )
    db.add(requirement)
    db.flush()
    owner = models.ReservationEntry(
        ledger_generation_id=int(accepted.id),
        item_id=int(item.item_id),
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="selected",
        run_id=int(parent.run_id),
        freeze_version=1,
        requirement_id=int(requirement.id),
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
        realization_mode="make",
        reserved_qty=Decimal("5"),
        replenishment_required_qty=Decimal("5"),
        lifecycle_status="active",
        owner_kind="current",
        is_current=True,
        current_identity=f"reservation:req:{int(requirement.id)}:mode:make",
    )
    db.add(owner)
    db.flush()
    return requirement, owner


def test_refresh_promotes_current_reservation_owners_and_retires_the_replaced_ones(
    db_session,
):
    """An obligation refresh must hand over the stable current owners.

    Until this landed no refresh promoted at all: the candidate stayed
    ``owner_kind='building'`` and the *previous* generation's rows remained
    ``is_current``, so every consumer that reads the current owner served an
    obligation the refresh had already superseded.
    """
    accepted, plan, _line, item, parent, cutoff = _world(db_session, qty=5)
    _old_requirement, old_owner = _current_owner_for_parent_run(
        db_session, accepted, plan, item, parent
    )
    # An existing assignment basis on the owner being superseded: the refresh
    # may hand it over or leave it as history, but it may not destroy it.
    basis_fact = models.StockLedgerEntry(
        ingest_batch_id=int(accepted.physical_import_batch_id),
        source_content_hash="item10-basis".ljust(64, "0"),
        item_id=int(item.item_id),
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="WH-OUT",
        qty=Decimal("1"),
        posting_at=cutoff - timedelta(hours=1),
        record_type="Receipt",
        movement_kind="receipt",
        recorder_type="Doc",
        recorder_ref="item10-basis",
        line_no="1",
        ingest_source="seed",
    )
    db_session.add(basis_fact)
    db_session.flush()
    # The compact current fold has to agree with the prefix this fact joins.
    db_session.add(models.StockBin(
        ledger_generation_id=int(accepted.id),
        item_id=int(item.item_id),
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="WH-OUT",
        on_hand=Decimal("1"),
        last_entry_id=int(basis_fact.id),
        is_current=True,
    ))
    db_session.flush()
    db_session.add(models.ReservationConsumptionAllocation(
        ledger_generation_id=int(accepted.id),
        reservation_id=int(old_owner.id),
        sle_id=int(basis_fact.id),
        requirement_id=int(old_owner.requirement_id),
        allocated_qty=Decimal("1"),
        match_rule="fifo",
        fact_ref="seed",
        fact_line_ref="1",
        item_id=int(item.item_id),
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="selected",
        idempotency_key="item10-seed-allocation",
        allocation_role="material_consumption",
        is_current=True,
        event_at=cutoff,
    ))
    db_session.commit()
    allocations_before = db_session.query(
        models.ReservationConsumptionAllocation.id
    ).filter_by(is_current=True).count()

    result = _run(db_session, accepted, "orch-promote-owners", replace=[plan.id])
    assert result.published is True

    candidate = db_session.query(models.PlanningRun).filter_by(
        prior_run_id=parent.run_id,
    ).one()
    promoted = db_session.query(models.ReservationEntry).filter_by(
        run_id=int(candidate.run_id),
    ).all()
    assert promoted, "the refreshed run published no reservations at all"
    assert all(str(row.owner_kind) == "current" for row in promoted)
    assert all(bool(row.is_current) for row in promoted)
    assert all(
        str(row.current_identity)
        == f"reservation:req:{int(row.requirement_id)}:mode:{row.realization_mode}"
        for row in promoted
    )
    # No staging owner survives acceptance.
    assert db_session.query(models.ReservationEntry).filter_by(
        owner_kind="building",
    ).count() == 0

    # The owner this refresh superseded is retired, not left current beside it.
    db_session.refresh(old_owner)
    assert bool(old_owner.is_current) is False
    assert str(old_owner.lifecycle_status) == "closed"

    # Work items point at the promoted owners, not at staging ids.
    work_items = db_session.query(models.ReplenishmentWorkItem).filter_by(
        ledger_generation_id=int(result.target_generation_id),
    ).all()
    assert work_items
    current_owner_ids = {
        int(row.id)
        for row in db_session.query(models.ReservationEntry).filter_by(is_current=True)
    }
    assert {int(row.reservation_id) for row in work_items} <= current_owner_ids

    # Every published BUY row resolves to a current owner.
    purchase_rows = db_session.query(models.CurrentExecutionRow).filter_by(
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    ).all()
    buy_requirement_ids = {
        int(value)
        for row in purchase_rows
        for value in (dict(row.payload or {}).get("requirement_ids") or [])
        if value not in (None, "")
    }
    if buy_requirement_ids:
        owned = {
            int(row.requirement_id)
            for row in db_session.query(models.ReservationEntry).filter(
                models.ReservationEntry.requirement_id.in_(sorted(buy_requirement_ids)),
                models.ReservationEntry.is_current.is_(True),
                models.ReservationEntry.owner_kind == "current",
            )
        }
        assert buy_requirement_ids <= owned

    # R4: the refresh hands the assignment basis over or keeps it as history;
    # it never reduces the set of current allocations.
    allocations_after = db_session.query(
        models.ReservationConsumptionAllocation.id
    ).filter_by(is_current=True).count()
    assert allocations_after >= allocations_before


def test_refresh_that_skips_the_promotion_is_rejected_before_acceptance(
    db_session, monkeypatch
):
    """The gate: a candidate with unpublished staging owners cannot become truth."""
    accepted, plan, _line, _item, _parent, _cutoff = _world(db_session, qty=5)

    # ``raising=False`` on purpose: the point of the gate is that a build which
    # never promoted cannot be accepted, however it came to skip it.
    monkeypatch.setattr(
        workflow,
        "publish_current_reservations",
        lambda db, **kwargs: {},
        raising=False,
    )

    with pytest.raises(
        workflow.ObligationRefreshOrchestratorError,
        match="publish_current_reservations must run",
    ):
        _run(db_session, accepted, "orch-skip-promotion", replace=[plan.id])


def _extra_fixed_plan(db, accepted, cutoff, *, name, code, qty=3):
    """A second live plan with its own fixed run, owner and work item."""
    item = models.Item(
        item_code=code, item_name=name, replenishment_method="Покупка",
    )
    db.add(item)
    db.flush()
    plan = models.ProductionPlanHeader(
        name=name, status="fixed", period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31), fixed_at=cutoff,
    )
    db.add(plan)
    db.flush()
    line = models.ProductionPlanLine(
        plan_id=plan.id, item_id=item.item_id,
        bucket_date=date(2026, 8, 1), qty=Decimal(str(qty)),
        accepted_output_qty=Decimal("0"), remaining_output_qty=Decimal(str(qty)),
    )
    db.add(line)
    db.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT", ledger_generation_id=accepted.id,
        source_plan_id=plan.id, period_from=plan.period_from,
        period_to=plan.period_to, config_snapshot={}, started_at=cutoff,
        fixed_at=cutoff, finished_at=cutoff, pinned=True,
        active_freeze_version=1, ledger_cutoff=cutoff,
    )
    db.add(run)
    db.flush()
    line.locked_by_run_id = int(run.run_id)
    db.add(models.MrpRunRoot(
        run_id=int(run.run_id), plan_line_id=int(line.id),
        planned_qty=Decimal(str(qty)), accepted_qty=Decimal("0"),
        remaining_qty=Decimal(str(qty)),
    ))
    requirement = models.MrpRequirement(
        run_id=int(run.run_id), item_id=item.item_id,
        total_required_qty=Decimal(str(qty)), net_required_qty=Decimal(str(qty)),
        period_from=plan.period_from, period_to=plan.period_to, bom_level=0,
        planning_stock_pool="selected", characteristic_ref="",
        organization_ref="", freeze_version=1,
    )
    db.add(requirement)
    db.flush()
    owner = models.ReservationEntry(
        ledger_generation_id=accepted.id, item_id=item.item_id,
        characteristic_ref="", organization_ref="", planning_stock_pool="selected",
        run_id=int(run.run_id), freeze_version=1, requirement_id=requirement.id,
        priority_period_from=plan.period_from, priority_period_to=plan.period_to,
        realization_mode="buy", reserved_qty=Decimal(str(qty)),
        replenishment_required_qty=Decimal(str(qty)),
        replenishment_received_qty=Decimal("0"), lifecycle_status="active",
        owner_kind="current", is_current=True,
        current_identity=f"reservation:req:{requirement.id}:mode:buy",
    )
    db.add(owner)
    db.flush()
    return SimpleNamespace(plan=plan, run=run, owner=owner, item=item)


def test_replacing_one_plan_keeps_the_other_live_runs_owners_and_work_items(
    db_session,
):
    """A retained run is untouched: a refresh retires only what it replaced.

    ``publish_current_reservations`` defined "removed" as every current owner
    absent from this generation's staging, and an obligation refresh never
    stages a retained run - its obligations deliberately stay anchored to the
    generation that froze them.  So one specification rebase closed every
    owner it had not recomputed: on the stand 8923 closes against 691
    inserts, and the production journal fell from 2959 rows to 319.
    """
    accepted, plan, _line, _item, parent, cutoff = _world(db_session, qty=5)
    kept_a = _extra_fixed_plan(
        db_session, accepted, cutoff, name="retained A", code="ORCH-RETAIN-A",
    )
    kept_b = _extra_fixed_plan(
        db_session, accepted, cutoff, name="retained B", code="ORCH-RETAIN-B",
    )
    db_session.commit()

    result = _run(db_session, accepted, "orch-retain-owners", replace=[plan.id])
    assert result.published is True

    for kept in (kept_a, kept_b):
        db_session.refresh(kept.owner)
        assert bool(kept.owner.is_current) is True
        assert str(kept.owner.owner_kind) == "current"
        assert str(kept.owner.lifecycle_status) == "active"
    # ...and the journals still describe them.
    work_item_runs = {
        int(row.run_id)
        for row in db_session.query(models.ReplenishmentWorkItem).filter_by(
            ledger_generation_id=int(result.target_generation_id)
        )
    }
    assert {int(kept_a.run.run_id), int(kept_b.run.run_id)} <= work_item_runs
    purchase_items = {
        int(row.payload["item_id"])
        for row in db_session.query(models.CurrentExecutionRow).filter_by(
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )
        if (row.payload or {}).get("item_id") is not None
    }
    assert {int(kept_a.item.item_id), int(kept_b.item.item_id)} <= purchase_items


def test_closing_a_plan_retires_only_that_plans_owners(db_session):
    """The retirement scope is the runs the refresh actually superseded."""
    accepted, plan, _line, _item, parent, cutoff = _world(db_session, qty=5)
    closing = _extra_fixed_plan(
        db_session, accepted, cutoff, name="closing", code="ORCH-CLOSE",
    )
    kept = _extra_fixed_plan(
        db_session, accepted, cutoff, name="kept", code="ORCH-KEEP",
    )
    db_session.commit()

    result = _run(
        db_session, accepted, "orch-close-one", retire=[closing.plan.id]
    )
    assert result.published is True

    db_session.refresh(closing.owner)
    db_session.refresh(kept.owner)
    assert str(closing.owner.lifecycle_status) == "closed"
    assert bool(closing.owner.is_current) is False
    assert str(kept.owner.lifecycle_status) == "active"
    assert bool(kept.owner.is_current) is True


def test_obligation_refresh_on_a_pointer_older_than_the_limit_is_refused(
    db_session, monkeypatch,
):
    """§40 exempts only the physical publication and the no-op repair.

    An obligation refresh inherits its parent's cutoff, so it cannot restore
    freshness; it freezes MRP, and MRP on stale truth is forbidden.  Inside
    the publication context it froze anyway (item 24, P1-1).
    """
    from app.services import planning_truth
    from app.services.mrp_freeze import LedgerPoolUnavailable

    monkeypatch.setenv("PLANNING_TRUTH_MAX_AGE_SECONDS", "86400")
    accepted, plan, _line, _item, _old, _cutoff = _world(db_session, with_parent=False)

    with pytest.raises(LedgerPoolUnavailable, match="freshness threshold") as excinfo:
        _run(db_session, accepted, "orch-stale", add=[plan.id])
    db_session.rollback()

    assert planning_truth.inside_publication() is False
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == accepted.id
    assert db_session.query(models.LedgerGeneration).filter_by(
        generation_key="orch-stale"
    ).count() == 0
    assert "freshness threshold" in str(excinfo.value)


# --- Item 25: one current custody owner per publication -----------------------


def _current_custody_at(db, generation, item, *, qty="2"):
    """A compact current custody owner as the last physical publication left it."""
    db.add(models.ProductionMaterialCustodyProjection(
        ledger_generation_id=int(generation.id),
        product_id=int(item.item_id),
        component_item_id=int(item.item_id),
        location_kind="workshop",
        warehouse_ref1c="WH-OUT",
        reserved_qty=Decimal(qty),
        source_event_high_watermark_id=0,
        is_current=True,
    ))
    db.commit()


def _current_custody(db):
    return db.query(models.ProductionMaterialCustodyProjection).filter(
        models.ProductionMaterialCustodyProjection.is_current.is_(True)
    ).all()


def test_obligation_refresh_makes_its_custody_projection_the_current_owner(db_session):
    """The stand: 385 current rows stayed at 1446 through seven refreshes.

    Each refresh built its own custody rows and a complete manifest, but only
    the accept path promoted them; the obligation publication moved the
    pointer and left the compact owner behind.
    """
    from app.services.production_material_custody_projection import (
        load_compact_current_material_custody,
    )

    accepted, plan, _line, item, _old, _cutoff = _world(db_session, with_parent=False)
    _current_custody_at(db_session, accepted, item)

    result = _run(db_session, accepted, "orch-custody", add=[plan.id])
    db_session.commit()

    target_id = int(result.target_generation_id)
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == target_id
    current = _current_custody(db_session)
    assert current and {int(row.ledger_generation_id) for row in current} == {target_id}
    generation_id, _state = load_compact_current_material_custody(
        db_session, consumer="test.after-obligation-refresh"
    )
    assert generation_id == target_id


def test_two_obligation_refreshes_in_a_row_keep_one_current_custody_owner(db_session):
    accepted, plan, _line, item, _old, _cutoff = _world(db_session, with_parent=False)
    _current_custody_at(db_session, accepted, item)
    first = _run(db_session, accepted, "orch-custody-1", add=[plan.id])
    db_session.commit()
    first_target = db_session.get(models.LedgerGeneration, first.target_generation_id)
    second = _run(db_session, first_target, "orch-custody-2", retire=[plan.id])
    db_session.commit()

    assert {int(row.ledger_generation_id) for row in _current_custody(db_session)} <= {
        int(second.target_generation_id)
    }
    from app.services.production_material_custody_projection import (
        load_compact_current_material_custody,
    )
    generation_id, _state = load_compact_current_material_custody(
        db_session, consumer="test.after-two-refreshes"
    )
    assert generation_id == int(second.target_generation_id)


def test_a_bounded_physical_refresh_after_an_obligation_refresh_is_accepted(
    db_session, monkeypatch,
):
    """End to end with the real assembly/readiness/custody/drum/shelf builders.

    The stand's 1458 was refused in ``assembly_payload`` by the readiness
    custody read: "compact current custody provenance is stale or ambiguous".
    """
    from app.services.item_ledger import physical_refresh_current_publish as publisher

    accepted, plan, _line, item, _old, _cutoff = _world(db_session, with_parent=False)
    _current_custody_at(db_session, accepted, item)
    result = _run(db_session, accepted, "orch-then-physical", add=[plan.id])
    db_session.commit()
    parent = db_session.get(models.LedgerGeneration, int(result.target_generation_id))

    target_cutoff = parent.cutoff + timedelta(hours=6)
    batch = models.PhysicalImportBatch(
        batch_key="orch-then-physical-batch", status="completed",
        source_complete=True, cutoff=target_cutoff,
        source_watermarks={}, completed_at=target_cutoff,
    )
    db_session.add(batch)
    db_session.flush()
    target = models.LedgerGeneration(
        generation_key="orch-then-physical-target", status="building",
        cutoff=target_cutoff, source_watermarks={"parent_generation_id": int(parent.id)},
        capabilities={}, physical_import_batch_id=int(batch.id), algorithm_version="test",
    )
    db_session.add(target)
    db_session.flush()
    sle = models.StockLedgerEntry(
        ingest_batch_id=int(batch.id),
        source_content_hash="orch-then-physical-sle",
        business_identity="orch-then-physical-sle",
        item_id=item.item_id, characteristic_ref="", organization_ref="org",
        warehouse_ref1c="WH-OUT", qty=Decimal("2"),
        posting_at=target_cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="transfer_out", recorder_type="Document_Transfer",
        recorder_ref="orch-then-physical", line_no="1", ingest_source="test",
    )
    db_session.add(sle)
    db_session.commit()

    # The same seams the item-20 end-to-end test stubs; everything on the
    # path to the custody read - assembly, readiness, custody, drum, shelf -
    # is the real builder.
    monkeypatch.setattr(
        publisher, "_build_obligation_view_payloads", lambda *a, **kw: ({}, {}),
    )
    monkeypatch.setattr(
        publisher, "publish_current_obligation_views_from_generation",
        lambda *a, **kw: {
            "production_control_journal": SimpleNamespace(changed_rows=0, idempotent=True),
            "purchase_control_journal": SimpleNamespace(changed_rows=0, idempotent=True),
            "mrp_result": SimpleNamespace(changed_rows=0, idempotent=True),
            "period_plan_execution": SimpleNamespace(changed_rows=0, idempotent=True),
        },
    )
    monkeypatch.setattr(
        publisher, "handoff_current_physical_refresh_provenance", lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_production_control_payload",
        lambda *a, **kw: {"rows": [], "meta": {}},
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_purchase_control_payload",
        lambda *a, **kw: {"rows": [], "meta": {}},
    )

    published = publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=int(target.id),
        parent_generation_id=int(parent.id),
        delta_manifest={"rows": (sle,), "supersessions": ()},
        odata_client=None,
        source_revision=int(batch.id),
        planning_pool_by_warehouse={"WH-OUT": "default"},
    )

    assert published.target_generation_id == int(target.id)
    assert str(db_session.get(models.LedgerGeneration, target.id).status) == "accepted"
    db_session.rollback()


# --- Item 26a: a closed owner does not keep counting a fact -------------------

_SUPPLIER_RECEIPT_DOC = "Document_ПриходнаяНакладная"


def _buy_owner_with_a_current_receipt_allocation(db, accepted, plan, item, parent, cutoff, *, qty="2"):
    """The stand's shape: a current BUY owner holding a typed receipt."""
    from app.services.item_ledger.current_replenishment import (
        SUPPLIER_RECEIPT_SOURCE_KEY,
        _scope_key,
    )
    from app.services.item_ledger.supplier_receipt_allocation import (
        RECEIPT_OPERATION,
        build_supplier_receipt_provenance,
    )

    requirement = models.MrpRequirement(
        run_id=int(parent.run_id), item_id=int(item.item_id),
        total_required_qty=Decimal("5"), net_required_qty=Decimal("5"),
        period_from=plan.period_from, period_to=plan.period_to, bom_level=0,
        planning_stock_pool="default", characteristic_ref="", organization_ref="",
        freeze_version=1,
    )
    db.add(requirement)
    db.flush()
    owner = models.ReservationEntry(
        ledger_generation_id=int(accepted.id), item_id=int(item.item_id),
        characteristic_ref="", organization_ref="", planning_stock_pool="default",
        run_id=int(parent.run_id), freeze_version=1, requirement_id=int(requirement.id),
        priority_period_from=plan.period_from, priority_period_to=plan.period_to,
        realization_mode="buy", reserved_qty=Decimal("5"),
        replenishment_required_qty=Decimal("5"), lifecycle_status="active",
        owner_kind="current", is_current=True,
        current_identity=f"reservation:req:{int(requirement.id)}:mode:buy",
    )
    db.add(owner)
    receipt = models.StockLedgerEntry(
        ingest_batch_id=int(accepted.physical_import_batch_id),
        source_content_hash="item26-receipt".ljust(64, "0"),
        item_id=int(item.item_id), characteristic_ref="",
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c="WH-OUT", qty=Decimal(qty),
        posting_at=cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="receipt", recorder_type=_SUPPLIER_RECEIPT_DOC,
        recorder_ref="item26-receipt", line_no="1", ingest_source="seed", active=True,
    )
    db.add(receipt)
    db.flush()
    # The compact current fold has to agree with the prefix this fact joins.
    db.add(models.StockBin(
        ledger_generation_id=int(accepted.id), item_id=int(item.item_id),
        characteristic_ref="", organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c="WH-OUT",
        on_hand=Decimal(qty), last_entry_id=int(receipt.id), is_current=True,
    ))
    db.add(build_supplier_receipt_provenance(
        ledger_generation_id=int(accepted.id), stock_ledger_entry_id=int(receipt.id),
        receipt_doc_type=receipt.recorder_type, receipt_doc_ref=receipt.recorder_ref,
        receipt_doc_line_no=receipt.line_no, operation_kind="supplier_receipt",
        operation_key=RECEIPT_OPERATION, operation_name="приобретение у поставщика",
        item_id=int(item.item_id), signed_qty=receipt.qty,
        match_rule="supplier-receipt-exact-line", match_status="unmatched",
        warehouse_ref1c="WH-OUT", reason="no exact typed supplier order line",
    ))
    scope = (int(item.item_id), "", "", "default", "buy")
    state = models.CurrentReplenishmentState(
        scope_key=_scope_key(scope), source_key=SUPPLIER_RECEIPT_SOURCE_KEY,
        ledger_generation_id=int(accepted.id), source_revision=int(accepted.id),
        scope_checksum="seed".ljust(64, "0"), writer_key="current_replenishment",
        status="completed",
    )
    db.add(state)
    db.add(models.ReservationConsumptionAllocation(
        ledger_generation_id=int(accepted.id), reservation_id=int(owner.id),
        sle_id=int(receipt.id), requirement_id=int(requirement.id),
        allocated_qty=Decimal(qty), match_rule="fifo", fact_ref="item26-receipt",
        fact_line_ref="1", item_id=int(item.item_id), characteristic_ref="",
        organization_ref="", planning_stock_pool="default",
        idempotency_key="item26-seed", allocation_role="replenishment_receipt",
        is_current=True, event_at=receipt.posting_at,
    ))
    db.commit()
    return owner, receipt


def _current_receipt_allocations(db, sle_id):
    return db.query(models.ReservationConsumptionAllocation).filter_by(
        sle_id=int(sle_id), is_current=True, allocation_role="replenishment_receipt",
    ).all()


def test_a_replacement_retires_the_old_owners_allocations_and_counts_the_fact_once(
    db_session,
):
    """The stand: 7287 current rows on closed owners, 477 facts over quantity."""
    accepted, plan, _line, item, parent, cutoff = _world(db_session, qty=5)
    old_owner, receipt = _buy_owner_with_a_current_receipt_allocation(
        db_session, accepted, plan, item, parent, cutoff,
    )

    result = _run(db_session, accepted, "orch-retire-allocations", replace=[plan.id])
    db_session.commit()
    assert result.published is True

    db_session.refresh(old_owner)
    assert str(old_owner.lifecycle_status) == "closed"
    current = _current_receipt_allocations(db_session, receipt.id)
    # The closed owner no longer holds the fact...
    assert all(int(row.reservation_id) != int(old_owner.id) for row in current)
    retired = db_session.query(models.ReservationConsumptionAllocation).filter_by(
        reservation_id=int(old_owner.id), sle_id=int(receipt.id),
    ).one()
    assert retired.is_current is False
    audit = db_session.query(models.CurrentReplenishmentAudit).filter_by(
        reservation_id=int(old_owner.id), sle_id=int(receipt.id),
        reason="owner_retired",
    ).one()
    assert audit.operation == "retire"
    assert int(audit.source_revision) == int(result.target_generation_id)
    # ...and the fact is counted once, by the live owners only.
    assert sum((Decimal(str(row.allocated_qty)) for row in current), Decimal("0")) <= abs(
        Decimal(str(receipt.qty))
    )
    from app.services.item_ledger.current_replenishment import over_allocated_facts

    assert over_allocated_facts(db_session) == []
    live = {
        int(row.id) for row in db_session.query(models.ReservationEntry).filter_by(
            lifecycle_status="active", is_current=True,
        )
    }
    assert {int(row.reservation_id) for row in current} <= live
    # The successor's replay re-allocated the same fact, once.
    candidate = db_session.query(models.PlanningRun).filter_by(
        prior_run_id=parent.run_id,
    ).one()
    successor_ids = {
        int(row.id) for row in db_session.query(models.ReservationEntry).filter_by(
            run_id=int(candidate.run_id), realization_mode="buy",
        )
    }
    assert [
        (int(row.reservation_id) in successor_ids, Decimal(str(row.allocated_qty)))
        for row in current
    ] == [(True, Decimal("2"))]


def test_closed_owner_allocations_are_retired_idempotently(db_session):
    """A database already double counting is healed by the same writer."""
    from app.services.item_ledger.current_replenishment import (
        retire_current_allocations_of_closed_owners,
    )

    accepted, plan, _line, item, parent, cutoff = _world(db_session, qty=5)
    old_owner, receipt = _buy_owner_with_a_current_receipt_allocation(
        db_session, accepted, plan, item, parent, cutoff,
    )
    old_owner.lifecycle_status = "closed"
    db_session.commit()

    first = retire_current_allocations_of_closed_owners(
        db_session, generation_id=int(accepted.id)
    )
    second = retire_current_allocations_of_closed_owners(
        db_session, generation_id=int(accepted.id)
    )
    db_session.commit()

    assert first == {"retired_allocations": 1, "retired_qty_units": 2, "unaudited": 0}
    assert second["retired_allocations"] == 0
    assert _current_receipt_allocations(db_session, receipt.id) == []


def test_a_fact_over_allocated_across_owners_is_refused(db_session):
    """Invariants 2-3: the sum of current allocations never exceeds the fact."""
    from app.services.item_ledger.current_replenishment import (
        CurrentReplenishmentError,
        require_facts_not_over_allocated,
    )

    accepted, plan, _line, item, parent, cutoff = _world(db_session, qty=5)
    old_owner, receipt = _buy_owner_with_a_current_receipt_allocation(
        db_session, accepted, plan, item, parent, cutoff,
    )
    # A second owner claiming the same 2 units: 4 > 2.
    other_run = models.PlanningRun(
        status="FIXED_SNAPSHOT", ledger_generation_id=accepted.id,
        config_snapshot={}, active_freeze_version=1, ledger_cutoff=cutoff,
    )
    db_session.add(other_run)
    db_session.flush()
    other_requirement = models.MrpRequirement(
        run_id=int(other_run.run_id), item_id=int(item.item_id),
        total_required_qty=Decimal("5"), net_required_qty=Decimal("5"),
        period_from=plan.period_from, period_to=plan.period_to, bom_level=0,
        planning_stock_pool="default", characteristic_ref="", organization_ref="",
        freeze_version=1,
    )
    db_session.add(other_requirement)
    db_session.flush()
    second = models.ReservationEntry(
        ledger_generation_id=int(accepted.id), item_id=int(item.item_id),
        characteristic_ref="", organization_ref="", planning_stock_pool="default",
        run_id=int(other_run.run_id), freeze_version=1,
        requirement_id=int(other_requirement.id),
        priority_period_from=plan.period_from, priority_period_to=plan.period_to,
        realization_mode="buy", reserved_qty=Decimal("5"),
        replenishment_required_qty=Decimal("5"), lifecycle_status="active",
        owner_kind="current", is_current=True,
        current_identity=f"reservation:req:{int(other_requirement.id)}:mode:buy",
    )
    db_session.add(second)
    db_session.flush()
    db_session.add(models.ReservationConsumptionAllocation(
        ledger_generation_id=int(accepted.id), reservation_id=int(second.id),
        sle_id=int(receipt.id), requirement_id=int(other_requirement.id),
        allocated_qty=Decimal("2"), match_rule="fifo", fact_ref="item26-receipt",
        fact_line_ref="1", item_id=int(item.item_id), characteristic_ref="",
        organization_ref="", planning_stock_pool="default",
        idempotency_key="item26-double", allocation_role="replenishment_receipt",
        is_current=True, event_at=receipt.posting_at,
    ))
    db_session.commit()

    with pytest.raises(CurrentReplenishmentError, match="exceed their physical fact") as excinfo:
        require_facts_not_over_allocated(db_session)
    assert f"SLE {int(receipt.id)}" in str(excinfo.value)
    # Bounded to other items, the check has nothing to say.
    require_facts_not_over_allocated(db_session, item_ids=[int(item.item_id) + 999])


def test_a_retained_owner_and_a_successor_do_not_both_count_one_fact(db_session):
    """A retained run is not staged but stays live: it is part of the scope.

    Without it the successor's replay was handed a fact the retained owner
    already held - two active owners counting one receipt - which the
    over-allocation gate now refuses.
    """
    from app.services.item_ledger.current_replenishment import over_allocated_facts

    accepted, plan, _line, item, parent, cutoff = _world(db_session, qty=5)
    _old_owner, receipt = _buy_owner_with_a_current_receipt_allocation(
        db_session, accepted, plan, item, parent, cutoff,
    )
    kept = _extra_fixed_plan(db_session, accepted, cutoff, name="kept", code="KEPT-26")
    requirement = db_session.get(models.MrpRequirement, kept.owner.requirement_id)
    requirement.item_id = item.item_id
    requirement.planning_stock_pool = "default"
    kept.owner.item_id = item.item_id
    kept.owner.planning_stock_pool = "default"
    allocation = db_session.query(models.ReservationConsumptionAllocation).one()
    allocation.reservation_id = kept.owner.id
    allocation.requirement_id = requirement.id
    db_session.commit()

    result = _run(db_session, accepted, "orch-retained-scope", replace=[plan.id])
    db_session.commit()

    assert result.published is True
    assert over_allocated_facts(db_session) == []
    current = _current_receipt_allocations(db_session, receipt.id)
    assert sum((Decimal(str(row.allocated_qty)) for row in current), Decimal("0")) == Decimal("2")


# --- Item 26b: StockBin generation is provenance, not membership ---------------


def test_source_warehouse_options_survive_an_obligation_refresh(db_session):
    """After any MRP recalculation the options list was empty.

    The obligation path never restamps ``StockBin`` (canon R6: its generation
    is provenance), and the options filtered ``ledger_generation_id ==
    pointer``.
    """
    from app.services.production_control_material_issues import (
        _auto_select_source_warehouse,
        _source_warehouse_options,
    )

    accepted, plan, _line, item, _old, cutoff = _world(db_session, with_parent=False)
    receipt = models.StockLedgerEntry(
        ingest_batch_id=int(accepted.physical_import_batch_id),
        source_content_hash="item26b-bin".ljust(64, "0"),
        item_id=int(item.item_id), characteristic_ref="",
        organization_ref=DEFAULT_ORGANIZATION_REF1C, warehouse_ref1c="WH-OUT",
        qty=Decimal("4"), posting_at=cutoff - timedelta(hours=1),
        record_type="Receipt", movement_kind="transfer_in", recorder_type="Doc",
        recorder_ref="item26b-bin", line_no="1", ingest_source="seed",
    )
    db_session.add(receipt)
    db_session.flush()
    db_session.add(models.StockBin(
        ledger_generation_id=int(accepted.id), item_id=int(item.item_id),
        characteristic_ref="", organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c="WH-OUT", on_hand=Decimal("4"),
        last_entry_id=int(receipt.id), is_current=True,
    ))
    db_session.commit()

    result = _run(db_session, accepted, "orch-bins", add=[plan.id])
    db_session.commit()
    pointer = int(result.target_generation_id)
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == pointer
    # The bin keeps the physical generation it was folded at.
    assert {
        int(row.ledger_generation_id)
        for row in db_session.query(models.StockBin).filter_by(is_current=True)
    } == {int(accepted.id)}

    options = _source_warehouse_options(
        db_session, [int(item.item_id)], ledger_generation_id=pointer,
    )
    assert [row["ref1c"] for row in options[int(item.item_id)]] == ["WH-OUT"]
    selected, candidates = _auto_select_source_warehouse(
        db_session, [int(item.item_id)], ledger_generation_id=pointer,
    )
    assert selected == "WH-OUT"
    assert [row["ref1c"] for row in candidates] == ["WH-OUT"]

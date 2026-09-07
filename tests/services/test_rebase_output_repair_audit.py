from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger import rebase_output_repair as repair_service
from app.services.item_ledger.assembly_output_persistence import (
    materialize_assembly_output_allocations,
)
from app.services.item_ledger.rebase_output_repair_audit import (
    RebaseOutputRepairAuditError,
    _fixed_boundary_utc,
    apply_rebase_output_repair,
    audit_rebase_output_repair,
)
from app.services.item_ledger.rebase_output_repair import RebaseOutputRepairError
from app.services.item_ledger.output_repair_gate import (
    AssemblyOutputRepairMutationBlocked,
    assert_output_repair_allows,
)
from app.services.item_ledger.physical_refresh_orchestrator import (
    run_physical_refresh,
)
from app.services.obligation_refresh_orchestrator import run_obligation_refresh


def _missed_rebase_world(db):
    cutoff = datetime(2026, 9, 3, 15, 0, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="repair-audit-physical",
        status="completed",
        source_watermarks={"source": "test"},
        cutoff=cutoff,
        completed_at=cutoff,
    )
    db.add(physical)
    db.flush()
    generation = models.LedgerGeneration(
        generation_key="repair-audit-generation",
        status="building",
        cutoff=cutoff,
        capabilities={},
        source_watermarks={},
        physical_import_batch_id=int(physical.id),
        algorithm_version="tests",
    )
    item = models.Item(item_code="REBASE-OUTPUT", item_name="Rebase output")
    db.add_all([generation, item])
    db.flush()

    original_fixed_at = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
    plan = models.ProductionPlanHeader(
        name="repair plan",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="fixed",
        fixed_at=original_fixed_at,
    )
    db.add(plan)
    db.flush()
    predecessor = models.PlanningRun(
        status="REBASED",
        config_snapshot={},
        source_plan_id=int(plan.id),
        period_from=plan.period_from,
        period_to=plan.period_to,
        fixed_at=original_fixed_at,
        started_at=original_fixed_at,
    )
    db.add(predecessor)
    db.flush()
    successor_started_at = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
    successor = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=int(generation.id),
        ledger_cutoff=cutoff,
        active_freeze_version=1,
        source_plan_id=int(plan.id),
        prior_run_id=int(predecessor.run_id),
        period_from=plan.period_from,
        period_to=plan.period_to,
        fixed_at=successor_started_at,
        started_at=successor_started_at,
    )
    line = models.ProductionPlanLine(
        plan_id=int(plan.id),
        item_id=int(item.item_id),
        bucket_date=date(2026, 8, 1),
        qty=Decimal("5"),
        accepted_output_qty=Decimal("0"),
        remaining_output_qty=Decimal("5"),
    )
    db.add_all([successor, line])
    db.flush()
    root = models.MrpRunRoot(
        run_id=int(successor.run_id),
        plan_line_id=int(line.id),
        planned_qty=Decimal("5"),
        accepted_qty=Decimal("0"),
        remaining_qty=Decimal("5"),
    )
    queue = models.AssemblyQueueLine(
        ledger_generation_id=int(generation.id),
        planning_run_id=int(successor.run_id),
        plan_id=int(plan.id),
        plan_line_id=int(line.id),
        item_id=int(item.item_id),
        bucket_date=line.bucket_date,
        period_from=plan.period_from,
        period_to=plan.period_to,
        planned_output_qty=Decimal("5"),
        accepted_plan_output_qty=Decimal("0"),
        assembly_remaining_qty=Decimal("5"),
        eligible_from=successor_started_at,
        original_priority=[],
        sort_key="2026-08-01|2026-08-31|1|1",
        line_status="open",
    )
    fact = models.StockLedgerEntry(
        ingest_batch_id=int(physical.id),
        source_content_hash="a" * 64,
        item_id=int(item.item_id),
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="",
        qty=Decimal("3"),
        qty_after=Decimal("3"),
        posting_at=datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc),
        record_type="Receipt",
        movement_kind="assembly_in",
        recorder_type="Production",
        recorder_ref="repair-recorder",
        line_no="1",
        ingest_source="pull",
        active=True,
    )
    db.add_all([root, queue, fact])
    db.flush()

    # Persist the old, wrong-boundary decision first.  It is surplus because
    # the successor start was incorrectly treated as a new fixation boundary.
    old = materialize_assembly_output_allocations(db, int(generation.id))
    assert old["allocated_qty"] == "0"
    assert old["surplus_total"] == "3"
    generation.status = "accepted"
    generation.capabilities = {"assembly_output_allocation": True}
    generation.accepted_at = cutoff
    db.add(
        models.PlanningTruthState(
            id=1,
            current_generation_id=int(generation.id),
            updated_at=cutoff,
        )
    )
    db.commit()
    return generation, plan, successor, line, fact


def test_audit_recovers_only_the_rebase_boundary_delta_and_is_read_only(db_session):
    generation, plan, successor, line, fact = _missed_rebase_world(db_session)
    before = {
        "facts": db_session.query(models.ProductionPlanExecutionFact).count(),
        "allocations": db_session.query(models.AssemblyOutputAllocation).count(),
        "decisions": db_session.query(models.AssemblyOutputFactDecision).count(),
        "generations": db_session.query(models.LedgerGeneration).count(),
    }

    first = audit_rebase_output_repair(db_session)
    second = audit_rebase_output_repair(db_session)

    assert first == second
    assert first["status"] == "repair_preview"
    assert first["apply_supported"] is True
    assert first["affected_plan_ids"] == [int(plan.id)]
    assert first["affected_run_ids"] == [int(successor.run_id)]
    assert first["facts"] == [
        {
            "stock_ledger_entry_id": int(fact.id),
            "item_id": int(fact.item_id),
            "posting_at": "2026-08-10T08:00:00+00:00",
            "qty": "3",
            "recorder_type": "Production",
            "recorder_ref": "repair-recorder",
            "source_content_hash": "a" * 64,
            "decision_status": "allocatable",
            "surplus_qty": "0",
        }
    ]
    assert first["allocations"] == [
        {
            "stock_ledger_entry_id": int(fact.id),
            "run_id": int(successor.run_id),
            "plan_id": int(plan.id),
            "plan_line_id": int(line.id),
            "item_id": int(fact.item_id),
            "allocated_qty": "3",
            "match_rule": "fifo",
            "requires_mrp_replacement": True,
        }
    ]
    assert first["conservation"] == {
        "recoverable_fact_qty": "3",
        "allocated_qty": "3",
        "surplus_qty": "0",
        "balanced": True,
    }
    after = {
        "facts": db_session.query(models.ProductionPlanExecutionFact).count(),
        "allocations": db_session.query(models.AssemblyOutputAllocation).count(),
        "decisions": db_session.query(models.AssemblyOutputFactDecision).count(),
        "generations": db_session.query(models.LedgerGeneration).count(),
    }
    assert after == before
    assert db_session.get(models.ProductionPlanLine, int(line.id)).remaining_output_qty == Decimal("5")


def test_legacy_fixation_wall_clock_matches_aware_queue_boundary():
    moscow = timezone(timedelta(hours=3))

    assert _fixed_boundary_utc(
        datetime(2026, 6, 2, 11, 12, 50),
        datetime(2026, 6, 2, 11, 12, 50, tzinfo=moscow),
    ) == datetime(2026, 6, 2, 8, 12, 50, tzinfo=timezone.utc)


def test_audit_fails_closed_without_assembly_output_capability(db_session):
    generation, *_ = _missed_rebase_world(db_session)
    generation.capabilities = {}
    db_session.commit()

    with pytest.raises(RebaseOutputRepairAuditError, match="lacks assembly-output"):
        audit_rebase_output_repair(db_session)


def test_apply_requires_the_full_approved_checksum(db_session):
    with pytest.raises(RebaseOutputRepairError, match="64-character"):
        apply_rebase_output_repair(db_session, audit_checksum="example")


def test_prepare_job_persists_approved_evidence_and_corrected_roots(db_session):
    generation, plan, successor, line, fact = _missed_rebase_world(db_session)
    audit = audit_rebase_output_repair(db_session)

    job = repair_service._prepare_job(
        db_session,
        audit_checksum=str(audit["audit_checksum"]),
        approved_by="test-owner",
    )
    repeated = repair_service._prepare_job(
        db_session,
        audit_checksum=str(audit["audit_checksum"]),
        approved_by="someone-else",
    )

    assert int(repeated.id) == int(job.id)
    assert job.status == "pending"
    assert job.source_generation_id == int(generation.id)
    assert job.approved_by == "test-owner"
    assert job.expected_fact_qty == Decimal("3")
    assert job.expected_allocated_qty == Decimal("3")
    assert job.expected_surplus_qty == Decimal("0")
    assert [(row.stock_ledger_entry_id, row.fact_qty) for row in job.facts] == [
        (int(fact.id), Decimal("3"))
    ]
    assert [
        (
            row.stock_ledger_entry_id,
            row.plan_line_id,
            row.audited_run_id,
            row.allocated_qty,
        )
        for row in job.allocations
    ] == [
        (int(fact.id), int(line.id), int(successor.run_id), Decimal("3"))
    ]
    assert len(job.targets) == 1
    assert job.targets[0].plan_id == int(plan.id)
    assert job.targets[0].predecessor_run_id == int(successor.run_id)
    assert job.targets[0].expected_roots == [
        {
            "plan_line_id": int(line.id),
            "item_id": int(fact.item_id),
            "bucket_date": "2026-08-01",
            "planned_qty": "5",
            "expected_accepted_qty": "3",
            "expected_remaining_qty": "2",
        }
    ]


def test_failed_job_resumes_from_durable_phase_checkpoint(db_session, monkeypatch):
    generation, *_ = _missed_rebase_world(db_session)
    audit = audit_rebase_output_repair(db_session)
    job = repair_service._prepare_job(
        db_session,
        audit_checksum=str(audit["audit_checksum"]),
        approved_by="test-owner",
    )
    job.phase1_generation_id = int(generation.id)
    job.status = "failed"
    job.last_error = "uncertain worker exit"
    db_session.commit()

    def unexpected_phase1(*_args, **_kwargs):
        raise AssertionError("phase one must not be published twice")

    monkeypatch.setattr(repair_service, "_phase1", unexpected_phase1)
    report = repair_service.apply_rebase_output_repair(
        db_session,
        audit_checksum=str(audit["audit_checksum"]),
        approved_by="retry-worker",
        max_rebases=0,
    )

    assert report["job_id"] == int(job.id)
    assert report["status"] == "rebasing"
    refreshed = db_session.get(models.AssemblyOutputRepairJob, int(job.id))
    assert refreshed.last_error is None
    assert refreshed.phase1_generation_id == int(generation.id)


def test_active_repair_blocks_competing_fact_and_obligation_publications(db_session):
    generation, *_ = _missed_rebase_world(db_session)
    audit = audit_rebase_output_repair(db_session)
    job = repair_service._prepare_job(
        db_session,
        audit_checksum=str(audit["audit_checksum"]),
        approved_by="test-owner",
    )

    with pytest.raises(
        AssemblyOutputRepairMutationBlocked,
        match=rf"repair job\(s\): {int(job.id)}",
    ):
        run_obligation_refresh(
            db_session,
            parent_generation_id=int(generation.id),
            generation_key="competing-obligation-refresh",
            started_by="ordinary-plan-worker",
        )

    with pytest.raises(
        AssemblyOutputRepairMutationBlocked,
        match=rf"repair job\(s\): {int(job.id)}",
    ):
        run_physical_refresh(
            db_session,
            generation_key="competing-physical-refresh",
            target_cutoff=generation.cutoff + timedelta(hours=1),
            client=object(),
            balance_snapshot={},
            started_by="auto-sync",
        )


def test_repair_actor_owns_gate_and_failed_job_keeps_it_closed(db_session):
    _generation, *_ = _missed_rebase_world(db_session)
    audit = audit_rebase_output_repair(db_session)
    job = repair_service._prepare_job(
        db_session,
        audit_checksum=str(audit["audit_checksum"]),
        approved_by="test-owner",
    )

    assert_output_repair_allows(
        db_session,
        operation="repair phase one",
        actor=f"assembly-output-repair:{int(job.id)}:facts",
    )
    job.status = "failed"
    db_session.commit()
    with pytest.raises(AssemblyOutputRepairMutationBlocked):
        assert_output_repair_allows(
            db_session,
            operation="ordinary refresh",
            actor="operator",
        )

    job.status = "completed"
    db_session.commit()
    assert_output_repair_allows(
        db_session,
        operation="ordinary refresh",
        actor="operator",
    )


def _closed_and_live_open_requirements(db_session):
    items = [
        models.Item(item_code="CLOSED-REQ-AUDIT-1", item_name="Closed req audit 1"),
        models.Item(item_code="CLOSED-REQ-AUDIT-2", item_name="Closed req audit 2"),
    ]
    db_session.add_all(items)
    db_session.flush()
    closed_run = models.PlanningRun(status="CLOSED", config_snapshot={})
    live_run = models.PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={})
    db_session.add_all([closed_run, live_run])
    db_session.flush()
    closed_requirements = [
        models.MrpRequirement(
            run_id=int(closed_run.run_id),
            item_id=int(item.item_id),
            total_required_qty=qty,
            net_required_qty=qty,
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 30),
            status="open",
            bom_level=0,
        )
        for item, qty in zip(items, (Decimal("3.25"), Decimal("4")))
    ]
    live_requirement = models.MrpRequirement(
        run_id=int(live_run.run_id),
        item_id=int(items[0].item_id),
        total_required_qty=Decimal("9"),
        net_required_qty=Decimal("9"),
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        status="open",
        bom_level=0,
    )
    db_session.add_all([*closed_requirements, live_requirement])
    db_session.commit()
    return closed_run, live_run, closed_requirements, live_requirement


def test_closed_run_requirement_audit_is_read_only_and_excludes_live_mrp(db_session):
    closed_run, _live_run, closed_requirements, live_requirement = (
        _closed_and_live_open_requirements(db_session)
    )

    first = repair_service.audit_closed_run_open_requirements(db_session)
    second = repair_service.audit_closed_run_open_requirements(db_session)

    assert first == second
    assert first["status"] == "repair_preview"
    assert first["requirement_count"] == 2
    assert first["run_count"] == 1
    assert first["net_required_qty"] == "7.25"
    assert [row["requirement_id"] for row in first["requirements"]] == [
        int(row.id) for row in closed_requirements
    ]
    assert {row["run_id"] for row in first["requirements"]} == {
        int(closed_run.run_id)
    }
    assert all(row.status == "open" for row in closed_requirements)
    assert live_requirement.status == "open"


def test_closed_run_requirement_repair_is_checksum_bound_and_idempotent(db_session):
    _closed_run, _live_run, closed_requirements, live_requirement = (
        _closed_and_live_open_requirements(db_session)
    )
    audit = repair_service.audit_closed_run_open_requirements(db_session)

    with pytest.raises(RebaseOutputRepairError, match="checksum is stale"):
        repair_service.repair_closed_run_open_requirements(
            db_session,
            audit_checksum="0" * 64,
            repaired_by="test",
        )
    assert all(row.status == "open" for row in closed_requirements)

    first = repair_service.repair_closed_run_open_requirements(
        db_session,
        audit_checksum=str(audit["audit_checksum"]),
        repaired_by="test",
    )
    closed_at = [row.closed_at for row in closed_requirements]
    second = repair_service.repair_closed_run_open_requirements(
        db_session,
        audit_checksum=str(audit["audit_checksum"]),
        repaired_by="retry",
    )

    assert first["status"] == "repaired"
    assert first["requirement_count"] == 2
    assert all(row.status == "closed" for row in closed_requirements)
    assert all(value is not None for value in closed_at)
    assert [row.closed_at for row in closed_requirements] == closed_at
    assert second["status"] == "already_clean"
    assert second["requirement_count"] == 0
    assert live_requirement.status == "open"

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app import models
from app.services.specification_mrp_rebase import (
    REBASE_REASON,
    rebase_fixed_plan_remaining_roots,
)


CUTOFF = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)


def _world(db, *, accepted_qty: Decimal):
    physical = models.PhysicalImportBatch(
        batch_key=f"spec-rebase-physical-{accepted_qty}",
        status="completed",
        cutoff=CUTOFF,
        source_watermarks={},
        completed_at=CUTOFF,
    )
    generation = models.LedgerGeneration(
        generation_key=f"spec-rebase-parent-{accepted_qty}",
        status="accepted",
        cutoff=CUTOFF,
        source_watermarks={},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="test",
        accepted_at=CUTOFF,
    )
    item = models.Item(
        item_code=f"ROOT-{accepted_qty}",
        item_name="Root",
        status="active",
    )
    db.add_all([physical, generation, item])
    db.flush()
    db.add(
        models.PlanningTruthState(
            id=1,
            current_generation_id=int(generation.id),
        )
    )
    plan = models.ProductionPlanHeader(
        name="Original 10",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="fixed",
        fixed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    db.add(plan)
    db.flush()
    run = models.PlanningRun(
        source_plan_id=int(plan.id),
        status="FIXED_SNAPSHOT",
        ledger_generation_id=int(generation.id),
        period_from=plan.period_from,
        period_to=plan.period_to,
        fixed_at=plan.fixed_at,
        pinned=True,
        config_snapshot={},
    )
    db.add(run)
    db.flush()
    line = models.ProductionPlanLine(
        plan_id=int(plan.id),
        item_id=int(item.item_id),
        bucket_date=date(2026, 8, 1),
        qty=Decimal("10"),
        locked_by_run_id=int(run.run_id),
    )
    sle = models.StockLedgerEntry(
        ingest_batch_id=int(physical.id),
        source_content_hash=f"output-{accepted_qty}",
        item_id=int(item.item_id),
        qty=accepted_qty,
        qty_after=accepted_qty,
        posting_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        movement_kind="assembly_in",
        record_type="Receipt",
        recorder_type="test",
        recorder_ref=f"output-{accepted_qty}",
        line_no="1",
        ingest_source="test",
    )
    db.add_all([line, sle])
    db.flush()
    if accepted_qty > 0:
        db.add(
            models.AssemblyOutputAllocation(
                ledger_generation_id=int(generation.id),
                stock_ledger_entry_id=int(sle.id),
                plan_id=int(plan.id),
                plan_line_id=int(line.id),
                allocated_qty=accepted_qty,
                match_rule="exact",
                allocation_ordinal=0,
            )
        )
    db.commit()
    return generation, plan, run, line


def _stub_publication(monkeypatch, db):
    from app.services import obligation_refresh_orchestrator as orchestrator
    from app.services import period_plan_service

    monkeypatch.setattr(
        period_plan_service,
        "_read_period_plan_execution_payload_for_run",
        lambda *args, **kwargs: {"status": "ok", "historical": True},
    )

    def publish(*args, **kwargs):
        parent = db.get(models.LedgerGeneration, int(kwargs["parent_generation_id"]))
        target = models.LedgerGeneration(
            generation_key=kwargs["generation_key"],
            status="accepted",
            cutoff=parent.cutoff,
            source_watermarks={},
            capabilities={},
            physical_import_batch_id=int(parent.physical_import_batch_id),
            algorithm_version="test",
            accepted_at=CUTOFF,
        )
        db.add(target)
        db.flush()
        for plan_id in kwargs["retire_plan_ids"]:
            old_run = db.query(models.PlanningRun).filter_by(
                source_plan_id=int(plan_id), status="FIXED_SNAPSHOT"
            ).one()
            old_run.status = "CLOSED"
            db.get(models.ProductionPlanHeader, int(plan_id)).status = "closed"
        for plan_id in kwargs["add_plan_ids"]:
            successor = db.get(models.ProductionPlanHeader, int(plan_id))
            db.add(
                models.PlanningRun(
                    source_plan_id=int(plan_id),
                    prior_run_id=int(successor.predecessor_run_id),
                    status="FIXED_SNAPSHOT",
                    ledger_generation_id=int(target.id),
                    period_from=successor.period_from,
                    period_to=successor.period_to,
                    fixed_at=successor.fixed_at,
                    finished_at=CUTOFF,
                    pinned=True,
                    config_snapshot={},
                )
            )
        db.flush()
        db.get(models.PlanningTruthState, 1).current_generation_id = int(target.id)
        return SimpleNamespace(target_generation_id=int(target.id), published=True)

    monkeypatch.setattr(orchestrator, "run_obligation_refresh", publish)


def test_rebase_creates_successor_only_for_unproduced_roots(db_session, monkeypatch):
    _, plan, run, _ = _world(db_session, accepted_qty=Decimal("8"))
    _stub_publication(monkeypatch, db_session)

    result = rebase_fixed_plan_remaining_roots(
        db_session,
        int(run.run_id),
        changed_spec_refs=("spec-b", "spec-a", "spec-b"),
        started_by="test",
    )

    assert result["status"] == "rebased"
    assert result["remaining_root_lines"][0]["qty"] == "2.000"
    successor = db_session.get(
        models.ProductionPlanHeader, int(result["successor_plan_id"])
    )
    assert int(successor.predecessor_plan_id) == int(plan.id)
    assert int(successor.predecessor_run_id) == int(run.run_id)
    assert successor.lineage_reason == REBASE_REASON
    assert successor.lineage_context["changed_spec_refs"] == ["spec-a", "spec-b"]
    successor_line = db_session.query(models.ProductionPlanLine).filter_by(
        plan_id=int(successor.id)
    ).one()
    assert Decimal(successor_line.qty) == Decimal("2")
    assert successor_line.bucket_date == date(2026, 8, 4)
    successor_run = db_session.get(
        models.PlanningRun, int(result["successor_run_id"])
    )
    assert int(successor_run.prior_run_id) == int(run.run_id)
    assert db_session.get(models.PlanningRun, int(run.run_id)).status == "CLOSED"
    assert db_session.get(models.ProductionPlanHeader, int(plan.id)).closed_reason == REBASE_REASON
    assert db_session.query(models.ClosedPlanSnapshot).filter_by(
        plan_id=int(plan.id), run_id=int(run.run_id)
    ).count() == 1

    retry = rebase_fixed_plan_remaining_roots(db_session, int(run.run_id))
    assert retry["status"] == "already_rebased"
    assert retry["successor_plan_id"] == result["successor_plan_id"]


def test_rebase_closes_fully_produced_plan_without_empty_successor(
    db_session, monkeypatch
):
    _, plan, run, _ = _world(db_session, accepted_qty=Decimal("10"))
    _stub_publication(monkeypatch, db_session)

    result = rebase_fixed_plan_remaining_roots(db_session, int(run.run_id))

    assert result["status"] == "closed_complete"
    assert result["successor_plan_id"] is None
    assert result["remaining_root_lines"] == []
    assert db_session.query(models.ProductionPlanHeader).filter(
        models.ProductionPlanHeader.predecessor_run_id == int(run.run_id)
    ).count() == 0
    assert db_session.get(models.ProductionPlanHeader, int(plan.id)).status == "closed"

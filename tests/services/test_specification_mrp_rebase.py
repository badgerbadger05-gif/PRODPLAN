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
        accepted_output_qty=accepted_qty,
        remaining_output_qty=max(Decimal("10") - accepted_qty, Decimal("0")),
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
    db.add(models.MrpRunRoot(
        run_id=int(run.run_id),
        plan_line_id=int(line.id),
        planned_qty=Decimal("10"),
        accepted_qty=accepted_qty,
        remaining_qty=max(Decimal("10") - accepted_qty, Decimal("0")),
    ))
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
        for plan_id in kwargs.get("replace_plan_ids", ()):
            old_run = db.query(models.PlanningRun).filter_by(
                source_plan_id=int(plan_id), status="FIXED_SNAPSHOT"
            ).one()
            old_run.status = "CLOSED"
            new_run = models.PlanningRun(
                    source_plan_id=int(plan_id),
                    prior_run_id=int(old_run.run_id),
                    status="FIXED_SNAPSHOT",
                    ledger_generation_id=int(target.id),
                    period_from=db.get(models.ProductionPlanHeader, int(plan_id)).period_from,
                    period_to=db.get(models.ProductionPlanHeader, int(plan_id)).period_to,
                    fixed_at=CUTOFF,
                    finished_at=CUTOFF,
                    pinned=True,
                    config_snapshot={},
                )
            db.add(new_run)
            db.flush()
            for line in db.query(models.ProductionPlanLine).filter_by(plan_id=int(plan_id)):
                if Decimal(line.remaining_output_qty or 0) > 0:
                    db.add(models.MrpRunRoot(
                        run_id=int(new_run.run_id),
                        plan_line_id=int(line.id),
                        planned_qty=line.remaining_output_qty,
                        accepted_qty=Decimal("0"),
                        remaining_qty=line.remaining_output_qty,
                    ))
                    line.locked_by_run_id = int(new_run.run_id)
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
    assert result["successor_plan_id"] == int(plan.id)
    assert db_session.query(models.ProductionPlanHeader).count() == 1
    successor_line = db_session.query(models.ProductionPlanLine).filter_by(
        plan_id=int(plan.id)
    ).one()
    assert Decimal(successor_line.qty) == Decimal("10")
    assert Decimal(successor_line.accepted_output_qty) == Decimal("8")
    assert Decimal(successor_line.remaining_output_qty) == Decimal("2")
    successor_run = db_session.get(
        models.PlanningRun, int(result["successor_run_id"])
    )
    assert int(successor_run.prior_run_id) == int(run.run_id)
    assert db_session.get(models.PlanningRun, int(run.run_id)).status == "CLOSED"
    assert db_session.get(models.ProductionPlanHeader, int(plan.id)).status == "fixed"
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


def test_rebase_tolerates_closed_snapshot_from_earlier_generation(
    db_session, monkeypatch
):
    """A stale ClosedPlanSnapshot from an older generation must not block a
    re-rebase.

    The existing-snapshot integrity check must re-derive the execution payload
    at the snapshot's OWN generation, mirroring ``close_fixed_plan``.  Comparing
    against a payload re-read at the current (advanced) accepted truth bakes the
    ever-changing ledger_generation/cutoff metadata into the equality, so once
    truth moves past the close it can never match and every re-rebase of the
    same run fails with a phantom "payload conflicts" error — which deadlocks
    the automatic rebase queue on the earliest affected run.
    """
    generation, plan, run, _ = _world(db_session, accepted_qty=Decimal("8"))

    # An older accepted generation at which the run was previously closed.
    older_physical = models.PhysicalImportBatch(
        batch_key="spec-rebase-older-physical",
        status="completed",
        cutoff=CUTOFF,
        source_watermarks={},
        completed_at=CUTOFF,
    )
    older_generation = models.LedgerGeneration(
        generation_key="spec-rebase-older",
        status="accepted",
        cutoff=CUTOFF,
        source_watermarks={},
        capabilities={},
        physical_import_batch=older_physical,
        algorithm_version="test",
        accepted_at=CUTOFF,
    )
    db_session.add_all([older_physical, older_generation])
    db_session.flush()

    # Immutable close captured at the OLDER generation, with a payload that (like
    # the real one) embeds that generation's identity.
    db_session.add(
        models.ClosedPlanSnapshot(
            plan_id=int(plan.id),
            run_id=int(run.run_id),
            ledger_generation_id=int(older_generation.id),
            cutoff=older_generation.cutoff,
            payload={"gen": int(older_generation.id)},
            closed_at=CUTOFF,
        )
    )
    db_session.commit()

    _stub_publication(monkeypatch, db_session)

    # A generation-sensitive payload: the real function embeds
    # ledger_generation/cutoff, so its result differs per generation.  Override
    # the constant stub installed by ``_stub_publication`` so the guard is
    # genuinely exercised (buggy code reads at the current truth generation and
    # would mismatch; the fix reads at the snapshot's generation and matches).
    from app.services import period_plan_service

    monkeypatch.setattr(
        period_plan_service,
        "_read_period_plan_execution_payload_for_run",
        lambda *args, **kwargs: {"gen": int(kwargs["generation_id"])},
    )

    result = rebase_fixed_plan_remaining_roots(
        db_session,
        int(run.run_id),
        changed_spec_refs=("spec-a",),
        started_by="test",
    )

    assert result["status"] == "rebased"
    # The stale close record is preserved, not duplicated or overwritten.
    assert db_session.query(models.ClosedPlanSnapshot).filter_by(
        plan_id=int(plan.id), run_id=int(run.run_id)
    ).count() == 1

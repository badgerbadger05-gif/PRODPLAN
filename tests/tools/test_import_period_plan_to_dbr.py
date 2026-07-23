from datetime import date, datetime

from app.models import (
    DbrAssemblyRate,
    DbrProductionProgram,
    Item,
    LedgerGeneration,
    PhysicalImportBatch,
    PlanningRun,
    PlanningTruthState,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionResource,
)
from tools.import_period_plan_to_dbr import import_period_plan


def _plan(db, *, plan_id=5, status="fixed"):
    first = Item(item_code="SLED-A", item_name="Снегоход A")
    second = Item(item_code="SLED-B", item_name="Снегоход B")
    resource = ProductionResource(resource_name="Сборка")
    db.add_all([first, second, resource])
    db.flush()
    plan = ProductionPlanHeader(
        id=plan_id,
        name="План №5",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status=status,
    )
    db.add(plan)
    db.flush()
    db.add_all(
        [
            ProductionPlanLine(plan_id=plan.id, item_id=first.item_id, bucket_date=date(2026, 8, 3), qty=10),
            ProductionPlanLine(plan_id=plan.id, item_id=first.item_id, bucket_date=date(2026, 8, 10), qty=5),
            ProductionPlanLine(plan_id=plan.id, item_id=second.item_id, bucket_date=date(2026, 8, 3), qty=7),
            ProductionPlanLine(plan_id=plan.id, item_id=second.item_id, bucket_date=date(2026, 8, 17), qty=0),
        ]
    )
    db.add_all(
        [
            DbrAssemblyRate(resource_id=resource.resource_id, item_id=first.item_id, qty_per_capacity=5),
            DbrAssemblyRate(resource_id=resource.resource_id, item_id=second.item_id, qty_per_capacity=4),
        ]
    )
    db.commit()
    batch = PhysicalImportBatch(
        batch_key=f"period-plan-import-{plan_id}",
        status="completed",
        cutoff=datetime(2026, 7, 23),
        source_watermarks={},
        completed_at=datetime(2026, 7, 23),
    )
    generation = LedgerGeneration(
        generation_key=f"period-plan-import-{plan_id}",
        status="accepted",
        cutoff=datetime(2026, 7, 23),
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
        physical_import_batch=batch,
        algorithm_version="tests/period-plan-import",
        accepted_at=datetime(2026, 7, 23),
    )
    db.add(generation)
    db.flush()
    db.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        source_plan_id=plan.id,
        active_freeze_version=1,
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
    )
    db.add(run)
    db.commit()
    return plan, first, second, run


def test_plan_5_like_success_and_optional_approve(db_session):
    plan, _, _, run = _plan(db_session)

    report = import_period_plan(
        db_session, plan_id=plan.id, source_run_id=run.run_id, approve=True
    )

    assert report["ok"] is True
    assert report["mode"] == "commit"
    assert report["approved"] is True
    assert report["counts"] == {
        "positive_source_rows": 3,
        "ignored_non_positive_rows": 1,
        "distinct_items": 2,
        "created_programs": 1,
    }
    program = db_session.query(DbrProductionProgram).one()
    assert program.title == "План №5"
    assert program.source_run_id == run.run_id
    assert program.created_by.startswith(f"shadow-import:period-plan:5:run:{run.run_id}:")
    assert program.status == "approved"
    assert len(program.items) == 3


def test_missing_rate_fails_before_write_with_codes(db_session):
    plan, _, second, run = _plan(db_session)
    db_session.query(DbrAssemblyRate).filter(DbrAssemblyRate.item_id == second.item_id).delete()
    db_session.commit()

    report = import_period_plan(db_session, plan_id=plan.id, source_run_id=run.run_id)

    assert report["ok"] is False
    assert report["missing"]["assembly_rate_item_codes"] == ["SLED-B"]
    assert "SLED-B" in report["errors"][0]
    assert db_session.query(DbrProductionProgram).count() == 0


def test_dry_run_rolls_back_created_program(db_session):
    plan, _, _, run = _plan(db_session)

    report = import_period_plan(
        db_session, plan_id=plan.id, source_run_id=run.run_id, dry_run=True, approve=True
    )

    assert report["ok"] is True
    assert report["mode"] == "dry-run"
    assert report["counts"]["created_programs"] == 1
    assert db_session.query(DbrProductionProgram).count() == 0


def test_repeat_is_idempotent(db_session):
    plan, _, _, run = _plan(db_session)
    first = import_period_plan(db_session, plan_id=plan.id, source_run_id=run.run_id)

    second = import_period_plan(db_session, plan_id=plan.id, source_run_id=run.run_id)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["existing"] is True
    assert second["counts"]["created_programs"] == 0
    assert second["program_id"] == first["program_id"]
    assert db_session.query(DbrProductionProgram).count() == 1


def test_existing_program_drift_is_rejected(db_session):
    plan, _, _, run = _plan(db_session)
    first = import_period_plan(db_session, plan_id=plan.id, source_run_id=run.run_id)
    program = db_session.get(DbrProductionProgram, first["program_id"])
    program.title = "ручное изменение"
    db_session.commit()

    report = import_period_plan(db_session, plan_id=plan.id, source_run_id=run.run_id)

    assert report["ok"] is False
    assert report["existing"] is True
    assert "drifted" in report["errors"][0]
    assert db_session.query(DbrProductionProgram).count() == 1

from datetime import date, datetime, timezone
from decimal import Decimal

from app import models
from app.services.production_control_journal import materialize_make_work_items


def _scope(db):
    cutoff = datetime(2026, 7, 26, 8, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="make-work-item-physical", status="completed", cutoff=cutoff
    )
    generation = models.LedgerGeneration(
        generation_key="make-work-item-generation",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        physical_import_batch=physical,
        algorithm_version="test",
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
    )
    item = models.Item(
        item_code="MAKE-WORK",
        item_name="Make work item",
        replenishment_method="Производство",
        optimal_batch=Decimal("4"),
    )
    plan = models.ProductionPlanHeader(
        name="Make work plan",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="fixed",
    )
    db.add_all([physical, generation, item, plan])
    db.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        ledger_generation_id=generation.id,
        ledger_cutoff=cutoff,
        active_freeze_version=1,
    )
    db.add(run)
    db.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=10,
        net_required_qty=10,
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=0,
        freeze_version=1,
    )
    db.add(requirement)
    db.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        run_id=run.run_id,
        freeze_version=1,
        requirement_id=requirement.id,
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
        realization_mode="make",
        reserved_qty=10,
        covered_from_stock_at_freeze_qty=0,
        replenishment_required_qty=10,
        replenishment_received_qty=2,
        realized_qty=2,
        lifecycle_status="active",
    )
    db.add(reservation)
    db.flush()
    work = models.ReplenishmentWorkItem(
        ledger_generation_id=generation.id,
        reservation_id=reservation.id,
        plan_id=plan.id,
        run_id=run.run_id,
        requirement_id=requirement.id,
        item_id=item.item_id,
        replenishment_method="make",
        replenishment_required_qty=10,
        replenishment_fulfilled_qty=2,
        replenishment_remaining_qty=8,
    )
    db.add(work)
    db.flush()
    db.add(
        models.PlanningTruthState(
            id=1,
            current_generation_id=generation.id,
        )
    )
    db.commit()
    return work, requirement, reservation


def test_materialize_make_work_item_is_idempotent_and_does_not_mutate_truth(db_session):
    work, requirement, reservation = _scope(db_session)

    first = materialize_make_work_items(db_session, [work.id])
    second = materialize_make_work_items(db_session, [work.id])

    assert [row["qty"] for row in first["created"]] == [4.0, 4.0]
    assert second["created"] == []
    assert len(second["reused"]) == 2
    db_session.refresh(work)
    db_session.refresh(requirement)
    db_session.refresh(reservation)
    assert Decimal(work.replenishment_remaining_qty) == Decimal("8")
    assert Decimal(requirement.net_required_qty) == Decimal("10")
    assert Decimal(reservation.replenishment_required_qty) == Decimal("10")

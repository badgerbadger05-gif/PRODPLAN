from datetime import date
from decimal import Decimal

from app import models
from app.services.item_ledger.replenishment_work_item_builder import (
    materialize_replenishment_work_items,
)


def test_builder_creates_one_make_or_buy_item_per_active_reservation(
    db_session,
    building_ledger_generation,
):
    generation = building_ledger_generation
    plan = models.ProductionPlanHeader(
        name="work-items",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="fixed",
    )
    item = models.Item(item_code="WORK-BUY", item_name="Work buy")
    db_session.add_all([plan, item])
    db_session.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        ledger_generation_id=generation.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        config_snapshot={},
    )
    db_session.add(run)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=Decimal("10"),
        net_required_qty=Decimal("7"),
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=1,
    )
    db_session.add(requirement)
    db_session.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        run_id=run.run_id,
        requirement_id=requirement.id,
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
        realization_mode="buy",
        reserved_qty=Decimal("10"),
        covered_from_stock_at_freeze_qty=Decimal("3"),
        replenishment_required_qty=Decimal("7"),
        replenishment_received_qty=Decimal("2"),
        realized_qty=Decimal("2"),
        lifecycle_status="active",
    )
    db_session.add(reservation)
    batch = models.LedgerBuildBatch(
        ledger_generation_id=generation.id,
        stage="replenishment_work_item",
        batch_key="test-work-items",
        status="building",
        algorithm_version="tests/work-items",
        metrics={},
    )
    db_session.add(batch)
    db_session.flush()

    metrics = materialize_replenishment_work_items(
        db_session, generation.id, batch.id
    )

    row = db_session.query(models.ReplenishmentWorkItem).one()
    assert row.reservation_id == reservation.id
    assert row.replenishment_method == "buy"
    assert row.replenishment_required_qty == Decimal("7")
    assert row.replenishment_fulfilled_qty == Decimal("2")
    assert row.replenishment_remaining_qty == Decimal("5")
    assert metrics["replenishment_work_items"] == 1
    assert metrics["replenishment_work_item_methods"] == {"make": 0, "buy": 1}


def test_builder_skips_stock_covered_and_inactive_reservations(
    db_session,
    building_ledger_generation,
):
    generation = building_ledger_generation
    batch = models.LedgerBuildBatch(
        ledger_generation_id=generation.id,
        stage="replenishment_work_item",
        batch_key="test-work-items-empty",
        status="building",
        algorithm_version="tests/work-items",
        metrics={},
    )
    db_session.add(batch)
    db_session.flush()

    metrics = materialize_replenishment_work_items(
        db_session, generation.id, batch.id
    )

    assert db_session.query(models.ReplenishmentWorkItem).count() == 0
    assert metrics["replenishment_work_items"] == 0

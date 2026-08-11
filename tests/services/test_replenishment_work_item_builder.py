from datetime import date
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.replenishment_work_item_builder import (
    ReplenishmentWorkItemBuilderError,
    materialize_replenishment_work_items,
)


def _one_reservation_world(db_session, generation, *, suffix=""):
    """One active buy reservation with full plan/run/requirement lineage."""
    plan = models.ProductionPlanHeader(
        name=f"work-items{suffix}",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="fixed",
    )
    item = models.Item(item_code=f"WORK-BUY{suffix}", item_name="Work buy")
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
    db_session.flush()
    return reservation


def _batch(db_session, generation, *, key, status="building"):
    batch = models.LedgerBuildBatch(
        ledger_generation_id=generation.id,
        stage="replenishment_work_item",
        batch_key=key,
        status=status,
        algorithm_version="tests/work-items",
        metrics={},
    )
    db_session.add(batch)
    db_session.flush()
    return batch


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


def test_builder_keeps_known_rework_out_of_work_journals(
    db_session, building_ledger_generation
):
    generation = building_ledger_generation
    reservation = _one_reservation_world(db_session, generation, suffix="-rework")
    reservation.realization_mode = "rework"
    batch = _batch(db_session, generation, key="rework-without-executor")
    db_session.flush()

    metrics = materialize_replenishment_work_items(
        db_session, generation.id, batch.id
    )

    assert db_session.query(models.ReplenishmentWorkItem).count() == 0
    assert metrics["replenishment_work_item_methods"] == {"make": 0, "buy": 0}
    assert metrics["executorless_rework_reservations"] == 1


# ---------------------------------------------------------------------------
# Resume: the pass is re-entered when its own batch is already COMPLETED
# ---------------------------------------------------------------------------

def test_completed_batch_is_replayed_idempotently(db_session, building_ledger_generation):
    """A resumed refresh replays every stage; this one used to hard-fail.

    The batch is COMPLETED because the interrupted worker got that far, so the
    rebuilt set must simply be recognised and the sealed metrics returned.
    """
    generation = building_ledger_generation
    reservation = _one_reservation_world(db_session, generation)
    batch = _batch(db_session, generation, key="resume-work-items")

    first = materialize_replenishment_work_items(db_session, generation.id, batch.id)
    batch.status = "completed"
    batch.metrics = dict(first)
    db_session.flush()

    second = materialize_replenishment_work_items(db_session, generation.id, batch.id)

    assert second == first
    row = db_session.query(models.ReplenishmentWorkItem).one()
    assert row.reservation_id == reservation.id
    assert row.replenishment_remaining_qty == Decimal("5")


def test_completed_batch_rebuilds_only_the_missing_rows(
    db_session, building_ledger_generation
):
    """The crash may land between the row write and the batch seal."""
    generation = building_ledger_generation
    reservation = _one_reservation_world(db_session, generation)
    batch = _batch(db_session, generation, key="partial-work-items")

    first = materialize_replenishment_work_items(db_session, generation.id, batch.id)
    batch.status = "completed"
    batch.metrics = dict(first)
    db_session.query(models.ReplenishmentWorkItem).delete(synchronize_session=False)
    db_session.flush()

    second = materialize_replenishment_work_items(db_session, generation.id, batch.id)

    assert second == first
    row = db_session.query(models.ReplenishmentWorkItem).one()
    assert row.reservation_id == reservation.id
    assert row.replenishment_required_qty == Decimal("7")


def test_completed_batch_refuses_a_replenishment_set_that_moved(
    db_session, building_ledger_generation
):
    generation = building_ledger_generation
    reservation = _one_reservation_world(db_session, generation)
    batch = _batch(db_session, generation, key="drifted-work-items")

    first = materialize_replenishment_work_items(db_session, generation.id, batch.id)
    batch.status = "completed"
    batch.metrics = dict(first)
    reservation.replenishment_required_qty = Decimal("9")
    db_session.flush()

    with pytest.raises(ReplenishmentWorkItemBuilderError, match="conflicts"):
        materialize_replenishment_work_items(db_session, generation.id, batch.id)


def test_batch_in_any_other_state_is_still_refused(
    db_session, building_ledger_generation
):
    generation = building_ledger_generation
    _one_reservation_world(db_session, generation)
    batch = _batch(db_session, generation, key="rejected-work-items", status="rejected")

    with pytest.raises(ReplenishmentWorkItemBuilderError, match="must be BUILDING"):
        materialize_replenishment_work_items(db_session, generation.id, batch.id)

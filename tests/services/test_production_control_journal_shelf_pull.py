"""Mech-shop launch follows the saved shelf projection, not the raw MRP remainder.

Contract: `.docs/shelves-buffers-and-mechshop-pull.md`.

    pull_qty        = min(shelf_gap_qty, unlaunched_mrp_qty)
    materialized_qty= min(round_up(pull_qty, batch_multiple), unlaunched_mrp_qty)
    latest_start_date = first_shortage_date - replenishment_time

The journal reads those saved values; it never recomputes them and it never
falls back to `item.optimal_batch` for an item that has a shelf.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from app import models
from app.services.production_control_journal import (
    list_journal,
    materialize_make_work_items,
)


CUTOFF = datetime(2026, 7, 26, 8, tzinfo=timezone.utc)


def _scope(db, *, key: str, optimal_batch: str = "4", remaining: str = "8"):
    """One accepted generation with a single make work item of `remaining` pcs."""
    physical = models.PhysicalImportBatch(
        batch_key=f"shelf-pull-physical-{key}", status="completed", cutoff=CUTOFF
    )
    generation = models.LedgerGeneration(
        generation_key=f"shelf-pull-generation-{key}",
        status="accepted",
        cutoff=CUTOFF,
        accepted_at=CUTOFF,
        physical_import_batch=physical,
        algorithm_version="tests/shelf-pull",
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
    )
    item = models.Item(
        item_code=f"SHELF-PULL-{key}",
        item_name="Mech shop part",
        replenishment_method="Производство",
        optimal_batch=Decimal(optimal_batch),
    )
    plan = models.ProductionPlanHeader(
        name=f"shelf pull plan {key}",
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
        ledger_cutoff=CUTOFF,
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
        run_id=run.run_id,
        freeze_version=1,
        requirement_id=requirement.id,
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
        realization_mode="make",
        reserved_qty=10,
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
        replenishment_fulfilled_qty=Decimal("10") - Decimal(remaining),
        replenishment_remaining_qty=Decimal(remaining),
    )
    db.add(work)
    db.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    db.commit()
    return generation, run, item, work


def _shelf(
    db,
    *,
    generation,
    item,
    batch_multiple: str = "3",
    pull_qty: str = "5",
    materialized_qty: str = "6",
    latest_start_date=date(2026, 8, 4),
    first_shortage_date=date(2026, 8, 9),
    active: bool = True,
):
    policy = models.ShelfPolicy(
        item_id=item.item_id,
        warehouse_ref1c="SHELF",
        replenishment_time_days=5,
        review_cycle_days=3,
        safety_days=2,
        batch_multiple=Decimal(batch_multiple),
        active=active,
    )
    db.add(policy)
    db.flush()
    projection = models.ShelfProjection(
        ledger_generation_id=generation.id,
        shelf_policy_id=policy.id,
        item_id=item.item_id,
        warehouse_ref1c="SHELF",
        as_of_date=CUTOFF.date(),
        protection_until=date(2026, 8, 5),
        target_qty=Decimal("12"),
        shelf_physical_qty=Decimal("3"),
        other_stock_qty=Decimal("0"),
        confirmed_open_production_qty=Decimal("0"),
        projected_qty=Decimal("3"),
        gap_qty=Decimal("9"),
        transfer_qty=Decimal("0"),
        unlaunched_mrp_qty=Decimal("10"),
        pull_qty=Decimal(pull_qty),
        materialized_qty=Decimal(materialized_qty),
        first_shortage_date=first_shortage_date,
        latest_start_date=latest_start_date,
        demand_manifest=[],
    )
    db.add(projection)
    db.commit()
    return policy, projection


def test_shelf_item_launches_pull_qty_rounded_to_policy_batch_multiple(db_session):
    generation, _run, item, work = _scope(db_session, key="pull")
    _shelf(db_session, generation=generation, item=item)

    result = materialize_make_work_items(db_session, [work.id])

    # 8 pcs of MRP remainder split by optimal_batch 4 would have been [4, 4].
    # The shelf pulls 5, rounded up to the policy multiple 3 -> one line of 6.
    assert [row["qty"] for row in result["created"]] == [6.0]
    created = result["created"][0]
    assert created["launch_source"] == "shelf_pull"
    assert created["shelf_pull_qty"] == 5.0
    assert created["shelf_warehouse_ref1c"] == "SHELF"
    assert created["shelf_latest_start_date"] == "2026-08-04"

    state = (
        db_session.query(models.ProductionOrderLineState)
        .filter(models.ProductionOrderLineState.product_id == created["product_id"])
        .one()
    )
    assert state.planned_start_date == date(2026, 8, 4)
    assert state.planned_finish_date == date(2026, 8, 9)


def test_shelf_pull_never_exceeds_the_work_item_remainder(db_session):
    generation, _run, item, work = _scope(db_session, key="cap", remaining="2")
    _shelf(db_session, generation=generation, item=item)

    result = materialize_make_work_items(db_session, [work.id])

    # materialized_qty 6 is an item-level величина; a single requirement can
    # never be launched above its own unlaunched replenishment remainder.
    assert [row["qty"] for row in result["created"]] == [2.0]


def test_closed_shelf_buffer_launches_nothing(db_session):
    generation, _run, item, work = _scope(db_session, key="closed")
    _shelf(
        db_session,
        generation=generation,
        item=item,
        pull_qty="0",
        materialized_qty="0",
        latest_start_date=None,
        first_shortage_date=None,
    )

    result = materialize_make_work_items(db_session, [work.id])

    assert result["created"] == []
    assert [row["reason"] for row in result["skipped"]] == [
        "буфер полки закрыт: вытягивание не требуется"
    ]


def test_item_without_shelf_keeps_the_legacy_optimal_batch_launch(db_session):
    _generation, _run, _item, work = _scope(db_session, key="fallback")

    result = materialize_make_work_items(db_session, [work.id])

    assert [row["qty"] for row in result["created"]] == [4.0, 4.0]
    assert {row["launch_source"] for row in result["created"]} == {"mrp_remaining"}
    assert all(row["shelf_pull_qty"] is None for row in result["created"])


def test_inactive_shelf_policy_falls_back_to_the_mrp_remainder(db_session):
    generation, _run, item, work = _scope(db_session, key="inactive")
    _shelf(db_session, generation=generation, item=item, active=False)

    result = materialize_make_work_items(db_session, [work.id])

    assert [row["qty"] for row in result["created"]] == [4.0, 4.0]
    assert {row["launch_source"] for row in result["created"]} == {"mrp_remaining"}


def _journal_line(db, *, run, item, planned_dates: bool):
    order = models.ProductionOrder(
        order_number="SHELF-JOURNAL",
        order_date=datetime(2026, 7, 26),
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db.add(order)
    db.flush()
    product = models.ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=6,
        produced_qty=0,
        remaining_qty=6,
    )
    db.add(product)
    db.flush()
    db.add(
        models.ProductionOrderLineState(
            product_id=product.product_id,
            status="shortage",
            planned_start_date=date(2026, 8, 20) if planned_dates else None,
            planned_finish_date=date(2026, 8, 25) if planned_dates else None,
        )
    )
    db.add(
        models.PlannedOrder(
            run_id=run.run_id,
            item_id=item.item_id,
            requested_qty=6,
            planned_qty=6,
            qty=6,
            need_date=date(2026, 8, 28),
            start_date=date(2026, 8, 26),
            finish_date=date(2026, 8, 28),
            bucket_date=date(2026, 8, 28),
            ledger_generation_id=run.ledger_generation_id,
        )
    )
    db.commit()
    return product


def test_journal_row_uses_shelf_latest_start_date_over_fixed_plan_dates(db_session):
    generation, run, item, _work = _scope(db_session, key="journal")
    _shelf(db_session, generation=generation, item=item)
    _journal_line(db_session, run=run, item=item, planned_dates=False)

    row = list_journal(db_session)["rows"][0]

    # The fixed-plan obligation says 2026-08-26 / 2026-08-28.
    assert row["planned_start_date"] == "2026-08-04"
    assert row["planned_finish_date"] == "2026-08-09"
    assert row["launch_source"] == "shelf_pull"
    assert row["shelf_pull_qty"] == 5.0
    assert row["shelf_materialized_qty"] == 6.0
    assert row["shelf_latest_start_date"] == "2026-08-04"
    assert row["shelf_warehouse_ref1c"] == "SHELF"


def test_journal_row_without_shelf_uses_accepted_plan_dates(db_session):
    _generation, run, item, _work = _scope(db_session, key="journal-legacy")
    _journal_line(db_session, run=run, item=item, planned_dates=False)

    row = list_journal(db_session)["rows"][0]

    assert row["planned_start_date"] == "2026-08-01"
    assert row["planned_finish_date"] == "2026-08-31"
    assert row["launch_source"] == "mrp_remaining"
    assert row["shelf_latest_start_date"] is None


def test_explicit_line_state_dates_still_win_over_the_shelf(db_session):
    generation, run, item, _work = _scope(db_session, key="journal-manual")
    _shelf(db_session, generation=generation, item=item)
    _journal_line(db_session, run=run, item=item, planned_dates=True)

    row = list_journal(db_session)["rows"][0]

    assert row["planned_start_date"] == "2026-08-20"
    assert row["planned_finish_date"] == "2026-08-25"
    # The shelf still explains what governs the launch quantity.
    assert row["launch_source"] == "shelf_pull"

"""Persistence-level guards for the shelf projection inputs.

Covers three ways the projection used to read more demand or more usable stock
than the canon allows:

* frozen norms were joined by ``run_id`` only, so every historical freeze
  version of the same run inflated ``shelf_target_qty``;
* every non-shelf warehouse counted as transferable, including the ignored
  ones (tolling stock, scrap isolator);
* confirmed production was collapsed into one undated scalar under a
  "finishes before the end of the protection window" filter, so an order that
  lands *after* an earlier drum slot still counted as its coverage.
"""
from datetime import date, datetime, timezone
from decimal import Decimal

from app import models
from app.services.item_ledger.drum_schedule_persistence import (
    materialize_drum_schedule,
)
from app.services.item_ledger.shelf_projection_persistence import (
    materialize_shelf_projections,
)


CUTOFF = datetime(2026, 7, 27, tzinfo=timezone.utc)


def _contour(db, *, key: str, active_freeze_version: int):
    """One fixed plan line on a 2-day/5-per-day assembly resource."""
    physical = models.PhysicalImportBatch(
        batch_key=f"shelf-{key}",
        status="completed",
        cutoff=CUTOFF,
        source_watermarks={},
        completed_at=CUTOFF,
    )
    db.add(physical)
    db.flush()
    generation = models.LedgerGeneration(
        generation_key=f"shelf-generation-{key}",
        status="building",
        cutoff=CUTOFF,
        capabilities={},
        source_watermarks={},
        physical_import_batch_id=physical.id,
        algorithm_version="tests/shelf-projection",
    )
    item = models.Item(item_code=f"FG-{key}", item_name="Finished good")
    component = models.Item(
        item_code=f"COMP-{key}",
        item_name="Shelf component",
        replenishment_method="Производство",
    )
    resource = models.ProductionResource(
        resource_name="Assembly",
        planning_range=2,
        capacity=Decimal("5"),
    )
    plan = models.ProductionPlanHeader(
        name=f"shelf-plan-{key}",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="fixed",
    )
    db.add_all([generation, item, component, resource, plan])
    db.flush()

    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=int(generation.id),
        ledger_cutoff=generation.cutoff,
        active_freeze_version=active_freeze_version,
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db.add(run)
    db.flush()
    db.add(
        models.ProductionPlanLine(
            plan_id=plan.id,
            item_id=item.item_id,
            bucket_date=date(2026, 8, 3),
            qty=Decimal("12"),
        )
    )
    db.add(
        models.AssemblyRate(
            resource_id=resource.resource_id,
            item_id=item.item_id,
            qty_per_capacity=Decimal("1"),
        )
    )
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=component.item_id,
        total_required_qty=Decimal("100"),
        net_required_qty=Decimal("100"),
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=1,
    )
    db.add(requirement)
    db.flush()
    db.add_all(
        [
            models.ReservationEntry(
                ledger_generation_id=generation.id,
                item_id=component.item_id,
                run_id=run.run_id,
                requirement_id=requirement.id,
                priority_period_from=plan.period_from,
                priority_period_to=plan.period_to,
                realization_mode="make",
                reserved_qty=Decimal("100"),
                replenishment_required_qty=Decimal("100"),
            ),
            models.ShelfPolicy(
                item_id=component.item_id,
                warehouse_ref1c="SHELF",
                replenishment_time_days=5,
                review_cycle_days=3,
                safety_days=2,
                batch_multiple=Decimal("1"),
            ),
        ]
    )
    db.flush()
    return generation, item, component, run


def _freeze_component(db, *, run, parent, component, version: int, norm: str):
    db.add(
        models.MrpFreezeComponent(
            run_id=run.run_id,
            freeze_version=version,
            parent_item_id=parent.item_id,
            component_item_id=component.item_id,
            spec_ref="test",
            norm_qty_per_unit=Decimal(norm),
        )
    )
    db.flush()


def _confirmed_order(db, *, component, requirement_id, qty: str, finish: date | None):
    """One confirmed production order onto the shelf, finishing on ``finish``."""
    order = models.ProductionOrder(
        order_number=f"CONF-{requirement_id}-{finish}",
        order_date=datetime(2026, 7, 20, tzinfo=timezone.utc),
        deletion_mark=False,
        is_posted=False,
        source="mrp",
    )
    db.add(order)
    db.flush()
    product = models.ProductionProduct(
        order_id=order.order_id,
        item_id=component.item_id,
        line_number=1,
        destination_warehouse_ref1c="SHELF",
        quantity=Decimal(qty),
        produced_qty=Decimal("0"),
        remaining_qty=Decimal(qty),
        source_mrp_requirement_id=int(requirement_id),
    )
    db.add(product)
    db.flush()
    db.add(
        models.ProductionOrderLineState(
            product_id=product.product_id,
            status="in_progress",
            planned_finish_date=finish,
        )
    )
    db.flush()
    return product


def test_confirmed_production_covers_only_the_slots_it_lands_before(db_session):
    """A late order must not close an earlier shortage.

    The drum needs 10 units on 2026-07-27 and 10 more on 2026-07-28.  A
    confirmed order finishing on 2026-08-01 is inside the protection window
    (which runs to 2026-08-06) — the old scalar therefore counted its whole 20
    as coverage and reported no gap at all.  Dated receipts make the core see
    that nothing has landed by either need date.
    """
    generation, item, component, run = _contour(
        db_session, key="late-receipt", active_freeze_version=1
    )
    _freeze_component(
        db_session, run=run, parent=item, component=component, version=1, norm="2"
    )
    requirement_id = int(
        db_session.query(models.MrpRequirement.id)
        .filter(models.MrpRequirement.run_id == run.run_id)
        .scalar()
    )
    _confirmed_order(
        db_session,
        component=component,
        requirement_id=requirement_id,
        qty="20",
        finish=date(2026, 8, 1),
    )

    materialize_drum_schedule(db_session, generation.id)
    materialize_shelf_projections(db_session, generation.id)

    row = db_session.query(models.ShelfProjection).one()
    assert row.target_qty == Decimal("20")
    # The order is still confirmed — it just is not coverage for these dates.
    assert row.confirmed_open_production_qty == Decimal("20")
    assert row.projected_qty == Decimal("0")
    assert row.gap_qty == Decimal("20")
    assert row.first_shortage_date == date(2026, 7, 27)
    # replenishment_time_days=5 back from the first shortage.
    assert row.latest_start_date == date(2026, 7, 22)
    # 100 open MRP minus the 20 already launched stays pullable.
    assert row.unlaunched_mrp_qty == Decimal("80")
    assert row.pull_qty == Decimal("20")


def test_confirmed_production_landing_before_the_slot_still_covers_it(db_session):
    """The same order one week earlier is coverage, and the gap closes."""
    generation, item, component, run = _contour(
        db_session, key="early-receipt", active_freeze_version=1
    )
    _freeze_component(
        db_session, run=run, parent=item, component=component, version=1, norm="2"
    )
    requirement_id = int(
        db_session.query(models.MrpRequirement.id)
        .filter(models.MrpRequirement.run_id == run.run_id)
        .scalar()
    )
    _confirmed_order(
        db_session,
        component=component,
        requirement_id=requirement_id,
        qty="20",
        finish=date(2026, 7, 26),
    )

    materialize_drum_schedule(db_session, generation.id)
    materialize_shelf_projections(db_session, generation.id)

    row = db_session.query(models.ShelfProjection).one()
    assert row.projected_qty == Decimal("20")
    assert row.gap_qty == Decimal("0")
    assert row.first_shortage_date is None
    assert row.pull_qty == Decimal("0")


def test_undated_confirmed_production_is_never_shelf_coverage(db_session):
    """An order with no planned finish date cannot be time-phased at all."""
    generation, item, component, run = _contour(
        db_session, key="undated-receipt", active_freeze_version=1
    )
    _freeze_component(
        db_session, run=run, parent=item, component=component, version=1, norm="2"
    )
    requirement_id = int(
        db_session.query(models.MrpRequirement.id)
        .filter(models.MrpRequirement.run_id == run.run_id)
        .scalar()
    )
    _confirmed_order(
        db_session,
        component=component,
        requirement_id=requirement_id,
        qty="20",
        finish=None,
    )

    materialize_drum_schedule(db_session, generation.id)
    materialize_shelf_projections(db_session, generation.id)

    row = db_session.query(models.ShelfProjection).one()
    assert row.confirmed_open_production_qty == Decimal("0")
    assert row.gap_qty == Decimal("20")
    assert row.pull_qty == Decimal("20")


def test_shelf_demand_reads_only_the_active_freeze_version(db_session):
    generation, item, component, run = _contour(
        db_session, key="freeze", active_freeze_version=2
    )
    # A superseded freeze of the same run must be invisible to the projection.
    _freeze_component(
        db_session, run=run, parent=item, component=component, version=1, norm="5"
    )
    _freeze_component(
        db_session, run=run, parent=item, component=component, version=2, norm="2"
    )

    materialize_drum_schedule(db_session, generation.id)
    materialize_shelf_projections(db_session, generation.id)

    row = db_session.query(models.ShelfProjection).one()
    # 10 scheduled units within the protection window * active norm 2 = 20.
    # Summing both versions (norms 5 + 2) would have produced 70.
    assert row.target_qty == Decimal("20")
    assert {
        entry["freeze_component_id"] for entry in row.demand_manifest
    } == {
        int(row_id)
        for (row_id,) in db_session.query(models.MrpFreezeComponent.id)
        .filter(models.MrpFreezeComponent.freeze_version == 2)
        .all()
    }


def test_ignored_warehouses_never_become_transferable_stock(db_session):
    generation, item, component, run = _contour(
        db_session, key="ignored", active_freeze_version=1
    )
    _freeze_component(
        db_session, run=run, parent=item, component=component, version=1, norm="2"
    )
    db_session.add_all(
        [
            models.StockBin(
                ledger_generation_id=generation.id,
                item_id=component.item_id,
                warehouse_ref1c="OTHER",
                on_hand=Decimal("4"),
            ),
            models.StockBin(
                ledger_generation_id=generation.id,
                item_id=component.item_id,
                warehouse_ref1c="TOLLING",
                on_hand=Decimal("100"),
            ),
            models.IgnoredWarehouse(
                warehouse_ref1c="TOLLING",
                warehouse_name="Давальческий",
                reason="not ours to move",
            ),
        ]
    )
    db_session.flush()

    materialize_drum_schedule(db_session, generation.id)
    materialize_shelf_projections(db_session, generation.id)

    row = db_session.query(models.ShelfProjection).one()
    assert row.target_qty == Decimal("20")
    # Only the plain OTHER warehouse is transferable; the 100 sitting on the
    # ignored warehouse must not suppress the mech-shop pull.
    assert row.other_stock_qty == Decimal("4")
    assert row.transfer_qty == Decimal("4")
    assert row.pull_qty == Decimal("16")

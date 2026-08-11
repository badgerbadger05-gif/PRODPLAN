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
        fixed_at=CUTOFF,
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


def _freeze_component(db, *, run, root, component, version: int, norm: str):
    db.add(
        models.MrpFreezeComponentCumulative(
            run_id=run.run_id,
            freeze_version=version,
            root_item_id=root.item_id,
            component_item_id=component.item_id,
            cumulative_norm_qty_per_root_unit=Decimal(norm),
        )
    )
    db.flush()


def _confirmed_order(db, *, generation, component, requirement_id, qty: str, finish: date | None):
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
    capture = models.LedgerBuildBatch(
        ledger_generation_id=int(generation.id),
        stage="snapshot_build",
        batch_key=f"shelf-wip:{product.product_id}",
        status="completed",
        algorithm_version="tests/shelf-wip",
        metrics={},
        completed_at=CUTOFF,
    )
    db.add(capture)
    db.flush()
    db.add(models.LedgerFutureSupply(
        ledger_generation_id=int(generation.id),
        supply_kind="wip_order",
        item_id=int(component.item_id),
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        destination_warehouse_ref1c="SHELF",
        source_ref=f"order:{order.order_id}",
        source_line_ref=f"product:{product.product_id}",
        source_local_id=f"production_product:{product.product_id}",
        source_requirement_id=int(requirement_id),
        ordered_qty_at_cutoff=Decimal(qty),
        realized_qty_at_cutoff=Decimal("0"),
        open_qty_at_cutoff=Decimal(qty),
        eta_date=finish,
        source_state_key="in_progress",
        source_updated_at=CUTOFF,
        capture_cutoff=CUTOFF,
        source_content_hash=f"shelf-wip-{product.product_id}",
        capture_batch_id=int(capture.id),
        evidence_status="exact",
    ))
    db.flush()
    return product


def _confirmed_order_without_requirement(
    db,
    *,
    generation,
    component,
    qty: str,
    finish: date | None,
):
    order = models.ProductionOrder(
        order_number=f"CONF-NOREQ-{finish}",
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
        source_mrp_requirement_id=None,
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
    capture = models.LedgerBuildBatch(
        ledger_generation_id=int(generation.id), stage="snapshot_build",
        batch_key=f"shelf-wip:{product.product_id}", status="completed",
        algorithm_version="tests/shelf-wip", metrics={}, completed_at=CUTOFF,
    )
    db.add(capture)
    db.flush()
    db.add(models.LedgerFutureSupply(
        ledger_generation_id=int(generation.id), supply_kind="wip_order",
        item_id=int(component.item_id), characteristic_ref="", organization_ref="",
        planning_stock_pool="default", destination_warehouse_ref1c="SHELF",
        source_ref=f"order:{order.order_id}", source_line_ref=f"product:{product.product_id}",
        source_local_id=f"production_product:{product.product_id}",
        source_requirement_id=None, ordered_qty_at_cutoff=Decimal(qty),
        realized_qty_at_cutoff=Decimal("0"), open_qty_at_cutoff=Decimal(qty),
        eta_date=finish, source_state_key="in_progress", source_updated_at=CUTOFF,
        capture_cutoff=CUTOFF, source_content_hash=f"shelf-wip-{product.product_id}",
        capture_batch_id=int(capture.id), evidence_status="exact",
    ))
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
        db_session, run=run, root=item, component=component, version=1, norm="2"
    )
    requirement_id = int(
        db_session.query(models.MrpRequirement.id)
        .filter(models.MrpRequirement.run_id == run.run_id)
        .scalar()
    )
    _confirmed_order(
        db_session,
        generation=generation,
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
        db_session, run=run, root=item, component=component, version=1, norm="2"
    )
    requirement_id = int(
        db_session.query(models.MrpRequirement.id)
        .filter(models.MrpRequirement.run_id == run.run_id)
        .scalar()
    )
    _confirmed_order(
        db_session,
        generation=generation,
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


def test_shelf_receipt_is_immutable_after_live_product_quantity_changes(db_session):
    generation, item, component, run = _contour(
        db_session, key="corrupt-cache", active_freeze_version=1
    )
    _freeze_component(
        db_session, run=run, root=item, component=component, version=1, norm="2"
    )
    requirement_id = int(
        db_session.query(models.MrpRequirement.id)
        .filter(models.MrpRequirement.run_id == run.run_id)
        .scalar()
    )
    product = _confirmed_order(
        db_session,
        generation=generation,
        component=component,
        requirement_id=requirement_id,
        qty="20",
        finish=date(2026, 7, 26),
    )
    product.produced_qty = Decimal("5")
    product.remaining_qty = Decimal("999")
    db_session.flush()

    materialize_drum_schedule(db_session, generation.id)
    materialize_shelf_projections(db_session, generation.id)

    row = db_session.query(models.ShelfProjection).one()
    assert row.confirmed_open_production_qty == Decimal("20")
    assert row.projected_qty == Decimal("20")
    assert row.gap_qty == Decimal("0")
    assert row.unlaunched_mrp_qty == Decimal("80")


def test_shelf_receipt_is_immutable_after_live_order_is_cancelled(db_session):
    generation, item, component, run = _contour(
        db_session, key="cancelled-order", active_freeze_version=1
    )
    _freeze_component(
        db_session, run=run, root=item, component=component, version=1, norm="2"
    )
    requirement_id = int(
        db_session.query(models.MrpRequirement.id)
        .filter(models.MrpRequirement.run_id == run.run_id)
        .scalar()
    )
    product = _confirmed_order(
        db_session,
        generation=generation,
        component=component,
        requirement_id=requirement_id,
        qty="20",
        finish=date(2026, 7, 26),
    )
    state = db_session.query(models.ProductionOrderLineState).filter_by(
        product_id=product.product_id
    ).one()
    state.status = "cancelled"
    db_session.flush()

    materialize_drum_schedule(db_session, generation.id)
    materialize_shelf_projections(db_session, generation.id)

    row = db_session.query(models.ShelfProjection).one()
    assert row.confirmed_open_production_qty == Decimal("20")
    assert row.gap_qty == Decimal("0")
    assert row.pull_qty == Decimal("0")


def test_undated_confirmed_production_is_never_shelf_coverage(db_session):
    """An order with no planned finish date cannot be time-phased at all."""
    generation, item, component, run = _contour(
        db_session, key="undated-receipt", active_freeze_version=1
    )
    _freeze_component(
        db_session, run=run, root=item, component=component, version=1, norm="2"
    )
    requirement_id = int(
        db_session.query(models.MrpRequirement.id)
        .filter(models.MrpRequirement.run_id == run.run_id)
        .scalar()
    )
    _confirmed_order(
        db_session,
        generation=generation,
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


def test_confirmed_production_without_source_requirement_is_not_counted(db_session):
    """Only linked WIP with a source requirement can cover shelf demand."""
    generation, item, component, run = _contour(
        db_session, key="no-source-req", active_freeze_version=1
    )
    _freeze_component(
        db_session, run=run, root=item, component=component, version=1, norm="2"
    )
    _confirmed_order_without_requirement(
        db_session,
        generation=generation,
        component=component,
        qty="20",
        finish=date(2026, 7, 26),
    )

    materialize_drum_schedule(db_session, generation.id)
    materialize_shelf_projections(db_session, generation.id)

    row = db_session.query(models.ShelfProjection).one()
    assert row.confirmed_open_production_qty == Decimal("0")
    assert row.projected_qty == Decimal("0")
    assert row.gap_qty == Decimal("20")
    assert row.pull_qty == Decimal("20")


def test_shelf_demand_reads_only_the_active_freeze_version(db_session):
    generation, item, component, run = _contour(
        db_session, key="freeze", active_freeze_version=2
    )
    # A superseded freeze of the same run must be invisible to the projection.
    _freeze_component(
        db_session, run=run, root=item, component=component, version=1, norm="5"
    )
    _freeze_component(
        db_session, run=run, root=item, component=component, version=2, norm="2"
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
        for (row_id,) in db_session.query(models.MrpFreezeComponentCumulative.id)
        .filter(models.MrpFreezeComponentCumulative.freeze_version == 2)
        .all()
    }


def test_nested_shelf_demand_uses_cumulative_bom_norms(db_session):
    generation, item, component, run = _contour(
        db_session, key="nested", active_freeze_version=1
    )
    subassembly = models.Item(
        item_code="SUB-NEST",
        item_name="Nested subassembly",
        replenishment_method="Производство",
    )
    db_session.add(subassembly)
    db_session.flush()

    # FG -> SUB x2; SUB -> DETAIL x3
    _freeze_component(
        db_session,
        run=run,
        root=item,
        component=subassembly,
        version=1,
        norm="2",
    )
    _freeze_component(
        db_session,
        run=run,
        root=item,
        component=component,
        version=1,
        norm="6",
    )
    _freeze_component(
        db_session,
        run=run,
        root=subassembly,
        component=component,
        version=1,
        norm="3",
    )

    materialize_drum_schedule(db_session, generation.id)
    materialize_shelf_projections(db_session, generation.id)

    row = db_session.query(models.ShelfProjection).one()
    # base FG demand inside protection window is 10; cumulative 2*3 = 6
    # so projected target must be 60.
    assert row.target_qty == Decimal("60")


def test_multiple_paths_to_the_same_component_are_aggregated(db_session):
    generation, root, component, run = _contour(
        db_session, key="multi-path", active_freeze_version=1
    )
    left = models.Item(
        item_code="SUB-L",
        item_name="Left branch",
        replenishment_method="Производство",
    )
    right = models.Item(
        item_code="SUB-R",
        item_name="Right branch",
        replenishment_method="Производство",
    )
    db_session.add_all([left, right])
    db_session.flush()

    # FG -> LEFT x2; FG -> RIGHT x1; both -> DETAIL x1
    _freeze_component(
        db_session,
        run=run,
        root=root,
        component=left,
        version=1,
        norm="2",
    )
    _freeze_component(
        db_session,
        run=run,
        root=root,
        component=right,
        version=1,
        norm="1",
    )
    _freeze_component(
        db_session,
        run=run,
        root=root,
        component=component,
        version=1,
        norm="3",
    )
    _freeze_component(
        db_session,
        run=run,
        root=left,
        component=component,
        version=1,
        norm="1",
    )
    _freeze_component(
        db_session,
        run=run,
        root=right,
        component=component,
        version=1,
        norm="1",
    )

    materialize_drum_schedule(db_session, generation.id)
    materialize_shelf_projections(db_session, generation.id)

    row = db_session.query(models.ShelfProjection).one()
    # base FG demand inside protection window is 10; cumulative from two paths = 3
    assert row.target_qty == Decimal("30")


def test_ignored_warehouses_never_become_transferable_stock(db_session):
    generation, item, component, run = _contour(
        db_session, key="ignored", active_freeze_version=1
    )
    _freeze_component(
        db_session, run=run, root=item, component=component, version=1, norm="2"
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
    # Legacy transfer from live stock no longer suppresses pull. A 4-unit OTHER
    # balance is still reported for diagnostics, but cannot reduce `pull_qty`.
    assert row.other_stock_qty == Decimal("4")
    assert row.transfer_qty == Decimal("0")
    assert row.pull_qty == Decimal("20")

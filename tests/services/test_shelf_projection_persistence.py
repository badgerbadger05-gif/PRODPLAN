"""Persistence-level guards for the shelf projection inputs.

Covers two ways the projection used to read more demand or more usable stock
than the canon allows:

* frozen norms were joined by ``run_id`` only, so every historical freeze
  version of the same run inflated ``shelf_target_qty``;
* every non-shelf warehouse counted as transferable, including the ignored
  ones (tolling stock, scrap isolator).
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

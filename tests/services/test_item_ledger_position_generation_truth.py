"""Generation-bound truth tests for the Item Ledger position projection."""

from datetime import date, datetime

from app import models
from app.services.item_ledger.reservation_ledger import item_ledger_position


def _item_requirement(db, code: str):
    item = models.Item(item_code=code, item_name=code)
    run = models.PlanningRun(config_snapshot={})
    db.add_all([item, run])
    db.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
    )
    db.add(requirement)
    db.flush()
    return item, run, requirement


def _reservation(
    db,
    *,
    generation_id: int,
    item,
    run,
    requirement,
    supplier: float = 0,
    wip: float = 0,
):
    row = models.ReservationEntry(
        ledger_generation_id=generation_id,
        item_id=item.item_id,
        run_id=run.run_id,
        requirement_id=requirement.id,
        priority_period_from=date(2026, 7, 1),
        priority_period_to=date(2026, 7, 31),
        realization_mode="make",
        reserved_qty=10,
        realized_qty=0,
        replenishment_required_qty=10,
        replenishment_received_qty=0,
        lifecycle_status="active",
    )
    db.add(row)
    db.flush()
    return row


def test_live_legacy_supply_mirrors_do_not_change_ledger_position(
    db_session,
    building_ledger_generation,
):
    db = db_session
    generation = building_ledger_generation
    item, _run, _requirement = _item_requirement(db, "POSITION-LEGACY-ISOLATION")

    production_order = models.ProductionOrder(
        order_number="LEGACY-WIP",
        order_date=datetime(2026, 7, 1),
        is_posted=True,
        deletion_mark=False,
    )
    supplier_order = models.SupplierOrder(
        order_number="LEGACY-SUPPLIER",
        order_date=datetime(2026, 7, 1),
        order_ref1c="legacy-supplier-position",
        is_posted=True,
        deletion_mark=False,
    )
    db.add_all([production_order, supplier_order])
    db.flush()
    product = models.ProductionProduct(
        order_id=production_order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=4,
        produced_qty=0,
        remaining_qty=4,
    )
    db.add(product)
    db.flush()
    db.add(models.ProductionOrderLineState(
        product_id=product.product_id,
        status="ready",
        issue_status="not_requested",
        planned_finish_date=date(2026, 8, 1),
    ))
    db.add(models.SupplierOrderItem(
        order_id=supplier_order.order_id,
        item_id_ref=item.item_id,
        line_number=1,
        quantity=6,
        received_qty=0,
        remaining_qty=6,
    ))
    db.flush()

    position = item_ledger_position(
        db,
        [item.item_id],
        ledger_generation_id=generation.id,
    )[item.item_id]

    assert position["incoming_supplier"] == 0
    assert position["incoming_wip"] == 0
    assert position["incoming"] == 0


def test_position_incoming_is_isolated_by_ledger_generation(
    db_session,
    building_ledger_generation,
):
    db = db_session
    first = building_ledger_generation
    item, run, requirement = _item_requirement(db, "POSITION-GENERATION-ISOLATION")
    _reservation(
        db,
        generation_id=first.id,
        item=item,
        run=run,
        requirement=requirement,
        supplier=3,
    )

    second = models.LedgerGeneration(
        generation_key="position-generation-two",
        status="building",
        physical_import_batch_id=first.physical_import_batch_id,
        algorithm_version="tests/position-2",
        source_watermarks={},
        capabilities={},
    )
    db.add(second)
    db.flush()
    _reservation(
        db,
        generation_id=second.id,
        item=item,
        run=run,
        requirement=requirement,
        wip=7,
    )

    first_position = item_ledger_position(
        db, [item.item_id], ledger_generation_id=first.id,
    )[item.item_id]
    second_position = item_ledger_position(
        db, [item.item_id], ledger_generation_id=second.id,
    )[item.item_id]

    assert first_position["incoming_supplier"] == 0
    assert first_position["incoming_wip"] == 0
    assert second_position["incoming_supplier"] == 0
    assert second_position["incoming_wip"] == 0

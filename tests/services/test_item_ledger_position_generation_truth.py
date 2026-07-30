"""Generation-bound truth tests for the Item Ledger position projection."""

from datetime import date, datetime

import pytest
from sqlalchemy import event

from app import models
from app.services.item_ledger.reservation_ledger import item_ledger_position


def test_position_fails_closed_for_unaccepted_generation(
    db_session,
    building_ledger_generation,
):
    with pytest.raises(ValueError, match="reads require accepted truth"):
        item_ledger_position(
            db_session,
            [],
            ledger_generation_id=building_ledger_generation.id,
        )

    building_ledger_generation.status = "rejected"
    db_session.flush()
    with pytest.raises(ValueError, match="reads require accepted truth"):
        item_ledger_position(
            db_session,
            [],
            ledger_generation_id=building_ledger_generation.id,
            allow_building_read=True,
        )


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
        allow_building_read=True,
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
        allow_building_read=True,
    )[item.item_id]
    second_position = item_ledger_position(
        db, [item.item_id], ledger_generation_id=second.id,
        allow_building_read=True,
    )[item.item_id]

    assert first_position["incoming_supplier"] == 0
    assert first_position["incoming_wip"] == 0
    assert second_position["incoming_supplier"] == 0
    assert second_position["incoming_wip"] == 0


def _future_supply(
    db,
    *,
    generation,
    item,
    supply_kind: str,
    open_qty,
    source_ref: str,
    evidence_status: str = "exact",
):
    """One captured future-supply row scoped to a generation."""
    batch = models.LedgerBuildBatch(
        ledger_generation_id=int(generation.id),
        stage="snapshot_build",
        batch_key=f"capture-{generation.id}-{source_ref}",
        status="completed",
        algorithm_version="tests/1",
        metrics={},
    )
    db.add(batch)
    db.flush()
    row = models.LedgerFutureSupply(
        ledger_generation_id=int(generation.id),
        capture_batch_id=int(batch.id),
        supply_kind=supply_kind,
        item_id=item.item_id,
        planning_stock_pool="default",
        destination_warehouse_ref1c="WH",
        source_ref=source_ref,
        source_line_ref="1",
        ordered_qty_at_cutoff=open_qty,
        realized_qty_at_cutoff=0,
        open_qty_at_cutoff=open_qty,
        source_state_key="ready",
        capture_cutoff=datetime(2026, 7, 20, 12),
        source_content_hash=f"hash-{source_ref}",
        evidence_status=evidence_status,
    )
    db.add(row)
    db.flush()
    return row


def test_position_incoming_reads_the_generation_future_supply_capture(
    db_session,
    building_ledger_generation,
):
    """``incoming`` is the generation's own capture, not a hardcoded zero."""
    db = db_session
    generation = building_ledger_generation
    item, _run, _requirement = _item_requirement(db, "POSITION-FUTURE-SUPPLY")
    _future_supply(
        db,
        generation=generation,
        item=item,
        supply_kind="supplier_order",
        open_qty=6,
        source_ref="supplier-1",
    )
    _future_supply(
        db,
        generation=generation,
        item=item,
        supply_kind="wip_order",
        open_qty=4,
        source_ref="wip-1",
    )
    # Non-exact evidence is retained for audit but is not open supply.
    _future_supply(
        db,
        generation=generation,
        item=item,
        supply_kind="supplier_order",
        open_qty=0,
        source_ref="supplier-ambiguous",
        evidence_status="ambiguous",
    )

    position = item_ledger_position(
        db, [item.item_id], ledger_generation_id=generation.id,
        allow_building_read=True,
    )[item.item_id]

    assert position["incoming_supplier"] == 6
    assert position["incoming_wip"] == 4
    assert position["incoming"] == 10
    assert position["projected"] == 10


def test_requested_items_are_filtered_at_every_sql_source(
    db_session,
    building_ledger_generation,
):
    db = db_session
    generation = building_ledger_generation
    wanted, wanted_run, wanted_requirement = _item_requirement(
        db,
        "POSITION-SQL-WANTED",
    )
    irrelevant, irrelevant_run, irrelevant_requirement = _item_requirement(
        db,
        "POSITION-SQL-IRRELEVANT",
    )
    db.add_all([
        models.StockBin(
            ledger_generation_id=generation.id,
            item_id=wanted.item_id,
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c="WH",
            on_hand=3,
        ),
        models.StockBin(
            ledger_generation_id=generation.id,
            item_id=irrelevant.item_id,
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c="WH",
            on_hand=999,
        ),
    ])
    _future_supply(
        db,
        generation=generation,
        item=wanted,
        supply_kind="supplier_order",
        open_qty=4,
        source_ref="wanted",
    )
    _future_supply(
        db,
        generation=generation,
        item=irrelevant,
        supply_kind="supplier_order",
        open_qty=999,
        source_ref="irrelevant",
    )
    _reservation(
        db,
        generation_id=generation.id,
        item=wanted,
        run=wanted_run,
        requirement=wanted_requirement,
    )
    _reservation(
        db,
        generation_id=generation.id,
        item=irrelevant,
        run=irrelevant_run,
        requirement=irrelevant_requirement,
    )
    db.flush()

    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(str(statement).lower())

    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        result = item_ledger_position(
            db,
            [wanted.item_id],
            ledger_generation_id=generation.id,
            allow_building_read=True,
        )
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)

    assert set(result) == {wanted.item_id}
    assert result[wanted.item_id]["on_hand"] == 3
    assert result[wanted.item_id]["incoming_supplier"] == 4
    assert result[wanted.item_id]["reserved_soft"] == 10
    for table in ("stock_bin", "ledger_future_supply", "reservation_entry"):
        source_queries = [
            statement for statement in statements
            if f"from {table}" in statement
        ]
        assert source_queries, f"no query captured for {table}"
        assert all("item_id in" in statement for statement in source_queries)


def test_empty_requested_item_set_does_not_scan_position_sources(
    db_session,
    building_ledger_generation,
):
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(str(statement).lower())

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        result = item_ledger_position(
            db_session,
            [],
            ledger_generation_id=building_ledger_generation.id,
            allow_building_read=True,
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)

    assert result == {}
    assert not any(
        f"from {table}" in statement
        for statement in statements
        for table in ("stock_bin", "ledger_future_supply", "reservation_entry")
    )


def test_position_incoming_stays_scoped_to_its_own_generation(
    db_session,
    building_ledger_generation,
):
    db = db_session
    first = building_ledger_generation
    item, _run, _requirement = _item_requirement(db, "POSITION-FS-ISOLATION")
    second = models.LedgerGeneration(
        generation_key="position-future-supply-two",
        status="building",
        physical_import_batch_id=first.physical_import_batch_id,
        algorithm_version="tests/position-fs-2",
        source_watermarks={},
        capabilities={},
    )
    db.add(second)
    db.flush()
    _future_supply(
        db,
        generation=first,
        item=item,
        supply_kind="supplier_order",
        open_qty=5,
        source_ref="fs-isolation",
    )

    first_position = item_ledger_position(
        db, [item.item_id], ledger_generation_id=first.id,
        allow_building_read=True,
    )[item.item_id]
    second_position = item_ledger_position(
        db, [item.item_id], ledger_generation_id=second.id,
        allow_building_read=True,
    )[item.item_id]

    assert first_position["incoming_supplier"] == 5
    assert second_position["incoming_supplier"] == 0
    assert second_position["incoming"] == 0

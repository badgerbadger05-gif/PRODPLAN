from datetime import date, datetime, timezone
from decimal import Decimal

from app import models
from app.services.item_ledger.reservation_consumption_persistence import (
    materialize_reservation_consumption_allocations,
)


def _building_generation(db, *, key: str, cutoff):
    physical = models.PhysicalImportBatch(
        batch_key=f"rc-consume-{key}",
        status="completed",
        source_watermarks={"request": key},
        cutoff=cutoff,
        completed_at=cutoff,
    )
    db.add(physical)
    db.flush()
    generation = models.LedgerGeneration(
        generation_key=f"rc-consume-generation-{key}",
        status="building",
        cutoff=cutoff,
        capabilities={},
        source_watermarks={},
        physical_import_batch=physical,
        algorithm_version="tests",
    )
    db.add(generation)
    db.flush()
    return generation, physical


def _item(db, code: str):
    item = models.Item(item_code=code, item_name=code)
    db.add(item)
    db.flush()
    return item


def _planning_run_and_requirement(db, *, generation, item, period_from, period_to=None):
    run = models.PlanningRun(
        status="BUILDING_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        source_plan_id=None,
        period_from=period_from,
        period_to=period_to or date(2026, 12, 31),
        active_freeze_version=1,
    )
    db.add(run)
    db.flush()

    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=Decimal("100"),
        net_required_qty=Decimal("100"),
        period_from=period_from,
        period_to=period_to or date(2026, 12, 31),
        bom_level=0,
        planning_stock_pool="default",
        characteristic_ref="",
        organization_ref="",
        freeze_version=1,
    )
    db.add(requirement)
    db.flush()
    return run, requirement


def _reserve_entry(db, *, generation, item, run, requirement, qty_reserved, qty_replenishment, period_from):
    db.add(
        models.ReservationEntry(
            ledger_generation_id=generation.id,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            planning_stock_pool="default",
            run_id=run.run_id,
            freeze_version=1,
            requirement_id=requirement.id,
            priority_period_from=period_from,
            priority_period_to=period_from,
            realization_mode="make",
            reserved_qty=Decimal(qty_reserved),
            replenishment_required_qty=Decimal(qty_replenishment),
            lifecycle_status="active",
        )
    )


def _freeze_baseline(db, *, generation, run, item, baseline_at):
    db.add(
        models.MrpFreezeBaseline(
            run_id=run.run_id,
            freeze_version=1,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            planning_stock_pool="default",
            baseline_at=baseline_at,
            physical_import_batch_id=int(generation.physical_import_batch_id),
            stock_qty=Decimal("0"),
            produced_total=Decimal("0"),
            received_total=Decimal("0"),
        )
    )


def _ledger_out_row(db, *, batch, item, warehouse, qty, posting_at, recorder_type, recorder_ref):
    db.add(
        models.StockLedgerEntry(
            ingest_batch_id=batch.id,
            source_content_hash=f"consumption-{str(recorder_ref)}",
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c=warehouse,
            qty=Decimal(str(qty)),
            qty_after=Decimal(str(qty)),
            posting_at=posting_at,
            record_type="Expense",
            movement_kind="assembly_out",
            recorder_type=recorder_type,
            recorder_ref=recorder_ref,
            line_no="1",
            ingest_source="pull",
            active=True,
        )
    )


def test_addressed_allocation_prefers_replenishment_required_and_synclink_identity(db_session):
    cutoff = datetime(2026, 8, 31, 10, 0, 0, tzinfo=timezone.utc)
    db_session.add(models.StockWarehouse(warehouse_ref1c="WH", warehouse_name="Main WH", is_selected=True, is_finished_goods=False))
    db_session.flush()
    generation, batch = _building_generation(db_session, key="addr", cutoff=cutoff)
    item = _item(db_session, "RC-ADDR")

    run_older, req_older = _planning_run_and_requirement(db_session, generation=generation, item=item, period_from=date(2026, 8, 1))
    run_younger, req_younger = _planning_run_and_requirement(db_session, generation=generation, item=item, period_from=date(2026, 8, 5))

    _reserve_entry(
        db=db_session,
        generation=generation,
        item=item,
        run=run_older,
        requirement=req_older,
        qty_reserved="40",
        qty_replenishment="40",
        period_from=date(2026, 8, 1),
    )
    _reserve_entry(
        db=db_session,
        generation=generation,
        item=item,
        run=run_younger,
        requirement=req_younger,
        qty_reserved="100",
        qty_replenishment="100",
        period_from=date(2026, 8, 5),
    )

    _freeze_baseline(
        db=db_session,
        generation=generation,
        run=run_older,
        item=item,
        baseline_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    _freeze_baseline(
        db=db_session,
        generation=generation,
        run=run_younger,
        item=item,
        baseline_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    order = models.ProductionOrder(order_number="PO-1", order_date=cutoff)
    db_session.add(order)
    db_session.flush()
    product = models.ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=Decimal("100"),
        remaining_qty=Decimal("100"),
        source_mrp_requirement_id=req_older.id,
    )
    db_session.add(product)
    db_session.flush()
    manufacture = models.ProductionManufacture(
        product_id=product.product_id,
        order_id=order.order_id,
        qty=Decimal("40"),
    )
    db_session.add(manufacture)
    db_session.flush()

    db_session.add(
        models.SyncLink(
            source_doctype="manufacture",
            source_id=manufacture.manufacture_id,
            target_system="1C",
            target_entity="Document_СборкаЗапасов",
            target_ref_key="asm-1",
            status="success",
        )
    )

    _ledger_out_row(
        db_session,
        batch=batch,
        item=item,
        warehouse="WH",
        qty="-100",
        posting_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        recorder_type="StandardODATA.Document_СборкаЗапасов",
        recorder_ref="asm-1",
    )

    db_session.flush()
    result = materialize_reservation_consumption_allocations(db_session, generation.id)
    rows = db_session.query(models.ReservationConsumptionAllocation).filter_by(
        ledger_generation_id=generation.id
    ).order_by(models.ReservationConsumptionAllocation.reservation_id.asc()).all()

    assert result["allocations"] == 2
    assert result["surplus_qty"] == "0"
    assert [Decimal(r.allocated_qty) for r in rows] == [Decimal("40"), Decimal("60")]
    assert rows[0].match_rule == "pegged"
    assert rows[1].match_rule == "fifo"
    assert rows[0].requirement_id == int(req_older.id)
    assert rows[1].requirement_id == int(req_younger.id)


def test_recorder_type_prefix_is_normalized_and_addressing_is_exact(db_session):
    cutoff = datetime(2026, 8, 31, 11, 0, 0, tzinfo=timezone.utc)
    db_session.add(models.StockWarehouse(warehouse_ref1c="WH", warehouse_name="Main WH", is_selected=True, is_finished_goods=False))
    db_session.flush()
    generation, batch = _building_generation(db_session, key="type", cutoff=cutoff)
    item = _item(db_session, "RC-TYPE")

    run, req = _planning_run_and_requirement(db_session, generation=generation, item=item, period_from=date(2026, 8, 1))
    _reserve_entry(
        db=db_session,
        generation=generation,
        item=item,
        run=run,
        requirement=req,
        qty_reserved="50",
        qty_replenishment="50",
        period_from=date(2026, 8, 1),
    )
    _freeze_baseline(
        db=db_session,
        generation=generation,
        run=run,
        item=item,
        baseline_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    _ledger_out_row(
        db_session,
        batch=batch,
        item=item,
        warehouse="WH",
        qty="-20",
        posting_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        recorder_type="SomePrefix.Document_СборкаЗапасов",
        recorder_ref="doc-1",
    )
    _ledger_out_row(
        db_session,
        batch=batch,
        item=item,
        warehouse="WH",
        qty="-20",
        posting_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        recorder_type="DocWithoutMarker",
        recorder_ref="doc-2",
    )

    db_session.flush()
    result = materialize_reservation_consumption_allocations(db_session, generation.id)
    rows = db_session.query(models.ReservationConsumptionAllocation).filter_by(
        ledger_generation_id=generation.id
    ).order_by(models.ReservationConsumptionAllocation.id.asc()).all()

    assert result["allocations"] == 2
    assert result["allocated_qty"] == "40"
    assert all(row.match_rule == "fifo" for row in rows)


def test_order_ref_is_not_used_when_synclink_is_absent(db_session):
    cutoff = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
    db_session.add(models.StockWarehouse(warehouse_ref1c="WH", warehouse_name="Main WH", is_selected=True, is_finished_goods=False))
    db_session.flush()
    generation, batch = _building_generation(db_session, key="order", cutoff=cutoff)
    item = _item(db_session, "RC-ORDER")

    run_old, req_old = _planning_run_and_requirement(db_session, generation=generation, item=item, period_from=date(2026, 8, 1))
    run_new, req_new = _planning_run_and_requirement(db_session, generation=generation, item=item, period_from=date(2026, 8, 10))

    _reserve_entry(
        db=db_session,
        generation=generation,
        item=item,
        run=run_old,
        requirement=req_old,
        qty_reserved="40",
        qty_replenishment="40",
        period_from=date(2026, 8, 1),
    )
    _reserve_entry(
        db=db_session,
        generation=generation,
        item=item,
        run=run_new,
        requirement=req_new,
        qty_reserved="50",
        qty_replenishment="50",
        period_from=date(2026, 8, 10),
    )

    _freeze_baseline(
        db=db_session,
        generation=generation,
        run=run_old,
        item=item,
        baseline_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    _freeze_baseline(
        db=db_session,
        generation=generation,
        run=run_new,
        item=item,
        baseline_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    order = models.ProductionOrder(order_number="PO-2", order_date=cutoff, order_ref1c="legacy-order-ref")
    db_session.add(order)
    db_session.flush()

    _ledger_out_row(
        db_session,
        batch=batch,
        item=item,
        warehouse="WH",
        qty="-90",
        posting_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        recorder_type="Document_СборкаЗапасов",
        recorder_ref="legacy-order-ref",
    )

    db_session.flush()
    result = materialize_reservation_consumption_allocations(db_session, generation.id)
    rows = db_session.query(models.ReservationConsumptionAllocation).filter_by(
        ledger_generation_id=generation.id
    ).order_by(models.ReservationConsumptionAllocation.id.asc()).all()

    assert result["allocations"] == 2
    assert all(row.match_rule == "fifo" for row in rows)
    assert [Decimal(r.allocated_qty) for r in rows] == [Decimal("40"), Decimal("50")]
    assert rows[0].requirement_id == int(req_old.id)
    assert rows[1].requirement_id == int(req_new.id)


def test_visible_assembly_out_with_no_reservations_becomes_surplus(db_session):
    cutoff = datetime(2026, 8, 31, 13, 0, 0, tzinfo=timezone.utc)
    db_session.add(
        models.StockWarehouse(
            warehouse_ref1c="WH",
            warehouse_name="Main WH",
            is_selected=True,
            is_finished_goods=False,
        )
    )
    db_session.flush()
    generation, batch = _building_generation(db_session, key="no-reserves", cutoff=cutoff)
    item = _item(db_session, "RC-NO-RESERVES")

    _ledger_out_row(
        db_session,
        batch=batch,
        item=item,
        warehouse="WH",
        qty="-15",
        posting_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        recorder_type="StandardODATA.Document_СборкаЗапасов",
        recorder_ref="asm-none",
    )

    db_session.flush()
    result = materialize_reservation_consumption_allocations(db_session, generation.id)
    rows = (
        db_session.query(models.ReservationConsumptionAllocation)
        .filter_by(ledger_generation_id=generation.id)
        .all()
    )

    assert result["facts"] == 1
    assert result["allocations"] == 0
    assert result["fact_qty"] == "15"
    assert result["allocated_qty"] == "0"
    assert result["surplus_qty"] == "15"
    assert len(rows) == 0

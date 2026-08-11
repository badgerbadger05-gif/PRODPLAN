from datetime import datetime
from decimal import Decimal

from app import models
from app.services.item_ledger.future_supply_capture import (
    FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION,
    FUTURE_SUPPLY_CAPTURE_STAGE,
)
from app.services.item_ledger.wip_future_supply import (
    capture_wip_future_supply,
    collect_wip_future_supply_evidence,
)
from app.services.production_control_common import DONE_STATE_KEY


def _scope(db, suffix="one"):
    cutoff = datetime(2026, 7, 31, 23, 59)
    batch = models.PhysicalImportBatch(
        batch_key=f"wip-physical-{suffix}", status="completed", cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=f"wip-generation-{suffix}", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, source_watermarks={}, capabilities={},
        physical_import_batch=batch, algorithm_version="test",
    )
    item = models.Item(item_code=f"WIP-{suffix}", item_name="WIP")
    warehouse = models.StockWarehouse(
        warehouse_ref1c=f"WH-{suffix}", warehouse_name="Contour", is_selected=True,
    )
    db.add_all([generation, item, warehouse])
    db.flush()
    build = models.LedgerBuildBatch(
        ledger_generation_id=generation.id, stage="snapshot_build", status="building",
        batch_key=f"wip-build-{suffix}", algorithm_version="test", metrics={},
    )
    db.add(build)
    db.flush()
    return generation, batch, build, item, warehouse


def _product(db, item, warehouse, *, order_ref="ORDER-1", line=1, qty="10", char=""):
    order = models.ProductionOrder(
        order_number="WO", order_ref1c=order_ref, order_date=datetime(2026, 7, 20),
        order_state_key="open",
    )
    db.add(order)
    db.flush()
    product = models.ProductionProduct(
        order_id=order.order_id, item_id=item.item_id, line_number=line,
        characteristic_ref1c=char or None, destination_warehouse_ref1c=warehouse.warehouse_ref1c,
        quantity=Decimal(qty), produced_qty=Decimal("999"), remaining_qty=Decimal("0"),
    )
    db.add(product)
    db.flush()
    return order, product


def _sle(db, generation, item, *, recorder, qty="3", kind="assembly_in", char="", line="1", warehouse="WH-one"):
    row = models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id, source_content_hash=f"hash-{recorder}-{line}",
        item_id=item.item_id, characteristic_ref=char, organization_ref="", warehouse_ref1c=warehouse,
        qty=Decimal(qty), qty_after=Decimal(qty), posting_at=datetime(2026, 7, 30),
        record_type="Receipt", movement_kind=kind, recorder_type="Document", recorder_ref=recorder,
        line_no=line, ingest_source="pull",
    )
    db.add(row)
    db.flush()
    return row



def _capture_target(db, source):
    """A BUILDING sibling of the accepted generation plus its capture batch."""
    target = models.LedgerGeneration(
        generation_key=f"wip-target-{source.generation_key}", status="building",
        cutoff=source.cutoff, source_watermarks={}, capabilities={},
        physical_import_batch_id=source.physical_import_batch_id,
        algorithm_version="test",
    )
    db.add(target)
    db.flush()
    batch = models.LedgerBuildBatch(
        ledger_generation_id=target.id, stage=FUTURE_SUPPLY_CAPTURE_STAGE,
        status="building", batch_key=f"wip-future-supply-{target.id}",
        algorithm_version=FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION, metrics={},
    )
    db.add(batch)
    db.flush()
    return target, batch


def test_order_completed_in_1c_is_not_open_future_supply(db_session):
    """ЗСНФ-001878: «Завершен» in 1C with 0 of 16 received is not an arrival.

    Closing is one-sided in 1C and read back here.  The unreceived remainder of
    a closed order will never be produced against it, so it must not keep
    inflating the projection.
    """
    generation, _physical, _build, item, warehouse = _scope(db_session, "done")
    order, _product_row = _product(
        db_session, item, warehouse, order_ref="ORDER-DONE", qty="16",
    )
    order.order_state_key = DONE_STATE_KEY
    db_session.flush()

    row = collect_wip_future_supply_evidence(
        db_session, generation.id,
        planning_pool_by_warehouse={warehouse.warehouse_ref1c: "assembly-pool"},
    )[0]

    assert row.evidence_status == "rejected"
    assert row.reason == "production order is completed in 1C"
    # The obligation itself stays auditable; only the open remainder dies.
    assert row.ordered_qty_at_cutoff == Decimal("16")
    assert row.realized_qty_at_cutoff == Decimal("0")
    assert row.source_state_key == DONE_STATE_KEY

    target, batch = _capture_target(db_session, generation)
    metrics = capture_wip_future_supply(
        db_session, generation.id, target.id, batch.id,
        planning_pool_by_warehouse={warehouse.warehouse_ref1c: "assembly-pool"},
    )

    assert metrics["open_qty"] == Decimal("0")
    stored = db_session.query(models.LedgerFutureSupply).filter(
        models.LedgerFutureSupply.ledger_generation_id == target.id
    ).one()
    assert Decimal(str(stored.open_qty_at_cutoff)) == Decimal("0")
    assert stored.evidence_status == "rejected"


def test_done_state_key_is_matched_case_insensitively(db_session):
    generation, _physical, _build, item, warehouse = _scope(db_session, "doneupper")
    order, _product_row = _product(
        db_session, item, warehouse, order_ref="ORDER-DONE-UPPER", qty="7",
    )
    order.order_state_key = DONE_STATE_KEY.upper()
    db_session.flush()

    row = collect_wip_future_supply_evidence(
        db_session, generation.id,
        planning_pool_by_warehouse={warehouse.warehouse_ref1c: "assembly-pool"},
    )[0]

    assert row.evidence_status == "rejected"
    assert row.reason == "production order is completed in 1C"


def test_deleted_order_is_not_open_future_supply(db_session):
    generation, _physical, _build, item, warehouse = _scope(db_session, "deleted")
    order, _product_row = _product(
        db_session, item, warehouse, order_ref="ORDER-DELETED", qty="9",
    )
    order.deletion_mark = True
    db_session.flush()

    row = collect_wip_future_supply_evidence(
        db_session, generation.id,
        planning_pool_by_warehouse={warehouse.warehouse_ref1c: "assembly-pool"},
    )[0]

    assert row.evidence_status == "rejected"
    assert row.reason == "production order is marked for deletion in 1C"
    assert row.ordered_qty_at_cutoff == Decimal("9")

    target, batch = _capture_target(db_session, generation)
    metrics = capture_wip_future_supply(
        db_session, generation.id, target.id, batch.id,
        planning_pool_by_warehouse={warehouse.warehouse_ref1c: "assembly-pool"},
    )

    assert metrics["open_qty"] == Decimal("0")


def test_working_order_still_reports_its_open_remainder(db_session):
    """The normal path must survive the closed-order rejection."""
    generation, _physical, _build, item, warehouse = _scope(db_session, "working")
    order, _product_row = _product(
        db_session, item, warehouse, order_ref="ORDER-WORKING", qty="16",
    )
    assert order.order_state_key == "open"
    assert bool(order.deletion_mark) is False

    row = collect_wip_future_supply_evidence(
        db_session, generation.id,
        planning_pool_by_warehouse={warehouse.warehouse_ref1c: "assembly-pool"},
    )[0]

    assert row.evidence_status == "exact"
    assert row.reason is None

    target, batch = _capture_target(db_session, generation)
    metrics = capture_wip_future_supply(
        db_session, generation.id, target.id, batch.id,
        planning_pool_by_warehouse={warehouse.warehouse_ref1c: "assembly-pool"},
    )

    assert metrics["open_qty"] == Decimal("16")
    stored = db_session.query(models.LedgerFutureSupply).filter(
        models.LedgerFutureSupply.ledger_generation_id == target.id
    ).one()
    assert Decimal(str(stored.open_qty_at_cutoff)) == Decimal("16")


def test_order_ref_route_requires_one_item_characteristic_candidate(db_session):
    generation, _physical, _build, item, warehouse = _scope(db_session, "amb")
    order, first = _product(db_session, item, warehouse, order_ref="ORDER-AMB", line=1)
    second = models.ProductionProduct(
        order_id=order.order_id, item_id=item.item_id, line_number=2,
        destination_warehouse_ref1c=warehouse.warehouse_ref1c, quantity=Decimal("5"),
        produced_qty=Decimal("0"), remaining_qty=Decimal("5"),
    )
    db_session.add(second)
    db_session.flush()
    _sle(db_session, generation, item, recorder="ASSEMBLY-AMB", warehouse=warehouse.warehouse_ref1c)
    db_session.add(models.StockRecorderPull(
        recorder_type="Document", recorder_ref="ASSEMBLY-AMB", order_ref="ORDER-AMB", status="done",
    ))
    db_session.flush()

    evidence = collect_wip_future_supply_evidence(
        db_session, generation.id,
        planning_pool_by_warehouse={warehouse.warehouse_ref1c: "assembly-pool"},
    )
    assert len(evidence) == 2
    assert {row.evidence_status for row in evidence} == {"rejected"}
    assert all("ambiguous" in (row.reason or "") for row in evidence)


def test_missing_destination_is_rejected_not_a_zero_fact(db_session):
    generation, _physical, _build, item, warehouse = _scope(db_session, "dest")
    _order, product = _product(db_session, item, warehouse)
    product.destination_warehouse_ref1c = None
    db_session.flush()

    row = collect_wip_future_supply_evidence(db_session, generation.id)[0]
    assert row.evidence_status == "rejected"
    assert row.reason == "missing destination warehouse mapping"
    assert row.ordered_qty_at_cutoff == Decimal("10")
    assert row.realized_qty_at_cutoff == Decimal("0")


def test_destination_outside_contour_is_rejected_without_failing_the_capture(db_session):
    generation, _physical, _build, item, warehouse = _scope(db_session, "pool")
    _product(db_session, item, warehouse, order_ref="ORDER-IN", line=1)
    _order, outside = _product(db_session, item, warehouse, order_ref="ORDER-OUT", line=2)
    outside.destination_warehouse_ref1c = "WH-finished-goods"
    db_session.flush()

    rows = collect_wip_future_supply_evidence(
        db_session,
        generation.id,
        planning_pool_by_warehouse={warehouse.warehouse_ref1c: "assembly-pool"},
    )

    by_ref = {row.source_ref: row for row in rows}
    assert by_ref["ORDER-IN"].evidence_status == "exact"
    assert by_ref["ORDER-IN"].planning_stock_pool == "assembly-pool"
    assert by_ref["ORDER-OUT"].evidence_status == "rejected"
    assert by_ref["ORDER-OUT"].reason == "planning_pool_not_mapped"
    assert by_ref["ORDER-OUT"].ordered_qty_at_cutoff == Decimal("10")

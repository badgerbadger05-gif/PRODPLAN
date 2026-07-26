from datetime import datetime
from decimal import Decimal

from app import models
from app.services.item_ledger.wip_future_supply import (
    capture_wip_future_supply,
    collect_wip_future_supply_evidence,
)


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


def test_no_explicit_pool_mapping_is_rejected_even_for_selected_warehouse(db_session):
    generation, _physical, _build, item, warehouse = _scope(db_session, "pool")
    _product(db_session, item, warehouse)

    row = collect_wip_future_supply_evidence(db_session, generation.id)[0]
    assert row.evidence_status == "rejected"
    assert row.reason == "planning_pool_not_mapped"

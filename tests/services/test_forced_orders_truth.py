from datetime import datetime, timezone

from app import models
from app.services.forced_orders import _build_order_qty_calculator_for_single_item


def test_forced_order_uses_accepted_ledger_stock_and_wip(
    db_session,
    building_ledger_generation,
):
    generation = building_ledger_generation
    now = datetime.now(timezone.utc)
    generation.status = "accepted"
    generation.cutoff = now
    generation.accepted_at = now
    generation.capabilities = {
        "physical_ledger": True,
        "future_supply": True,
    }
    item = models.Item(
        item_code="FORCED-LEDGER",
        item_name="Forced Ledger",
        stock_qty=999,
    )
    warehouse = models.StockWarehouse(
        warehouse_ref1c="FORCED-WH",
        warehouse_name="Forced selected",
        is_selected=True,
        is_finished_goods=False,
    )
    db_session.add_all([item, warehouse])
    db_session.flush()
    db_session.add(
        models.StockBin(
            ledger_generation_id=generation.id,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c=warehouse.warehouse_ref1c,
            on_hand=2,
        )
    )
    batch = models.LedgerBuildBatch(
        ledger_generation_id=generation.id,
        stage="snapshot_build",
        batch_key="forced-order-future",
        status="completed",
        algorithm_version="test",
        metrics={},
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add(
        models.LedgerFutureSupply(
            ledger_generation_id=generation.id,
            capture_batch_id=batch.id,
            supply_kind="wip_order",
            item_id=item.item_id,
            planning_stock_pool="selected",
            destination_warehouse_ref1c=warehouse.warehouse_ref1c,
            source_ref="forced-wip",
            source_line_ref="1",
            ordered_qty_at_cutoff=3,
            realized_qty_at_cutoff=0,
            open_qty_at_cutoff=3,
            source_state_key="in_progress",
            capture_cutoff=now,
            source_content_hash="forced-wip-hash",
            evidence_status="exact",
        )
    )
    db_session.flush()

    calculator = _build_order_qty_calculator_for_single_item(
        db_session,
        snapshot={"toggles": {"include_wip": True}},
        item_id=item.item_id,
        requested_qty=10,
    )

    assert calculator.stock_by_item[item.item_id] == 2
    assert calculator.wip_by_item[item.item_id] == 3

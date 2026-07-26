"""Schema/defaults test for the additive Item Ledger tables.

Creates one minimal row per new table and asserts server-side defaults apply.
No business logic — only create_all + insert defaults (mirrors the existing
test_ledger_v2_schema.py convention).
"""

import datetime

from app import models


def _lineage(db_session):
    imported = models.PhysicalImportBatch(
        batch_key="schema-physical",
        status="completed",
        source_watermarks={"fixture": "schema"},
        completed_at=datetime.datetime(2026, 7, 1),
    )
    generation = models.LedgerGeneration(
        generation_key="schema-generation",
        status="building",
        source_watermarks={},
        capabilities={},
        physical_import_batch=imported,
        algorithm_version="tests/1",
    )
    db_session.add(generation)
    db_session.flush()
    return imported, generation


def _mk_item(db_session, code="IL-SCHEMA"):
    item = models.Item(item_code=code, item_name=code)
    db_session.add(item)
    db_session.flush()
    return item


def test_stock_warehouse_finished_goods_default(db_session):
    wh = models.StockWarehouse(warehouse_ref1c="WH-A", warehouse_name="A")
    db_session.add(wh)
    db_session.commit()
    db_session.refresh(wh)
    assert wh.is_finished_goods is False


def test_stock_ledger_entry_defaults(db_session):
    item = _mk_item(db_session)
    imported, _generation = _lineage(db_session)
    row = models.StockLedgerEntry(
        ingest_batch_id=imported.id, source_content_hash="a" * 64,
        item_id=item.item_id, qty=1, recorder_type="Doc", recorder_ref="X", line_no="1",
        posting_at=datetime.datetime(2026, 7, 1),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    assert row.characteristic_ref == "" and row.organization_ref == "" and row.warehouse_ref1c == ""
    assert float(row.qty_after) == 0
    assert row.record_type == "" and row.movement_kind == "" and row.ingest_source == ""
    assert row.active is True


def test_stock_bin_defaults(db_session):
    item = _mk_item(db_session, code="IL-BIN")
    _imported, generation = _lineage(db_session)
    row = models.StockBin(
        ledger_generation_id=generation.id,
        item_id=item.item_id, warehouse_ref1c="WH1",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    assert float(row.on_hand) == 0 and float(row.reconcile_pending_qty) == 0
    assert row.last_entry_id is None


def test_stock_recorder_pull_and_anchor_defaults(db_session):
    item = _mk_item(db_session, code="IL-PULL")
    imported, _generation = _lineage(db_session)
    pull = models.StockRecorderPull(recorder_type="Doc", recorder_ref="R1")
    db_session.add(pull)
    anchor = models.StockLedgerAnchor(
        ingest_batch_id=imported.id,
        item_id=item.item_id, warehouse_ref1c="WH1", anchor_period=datetime.date(2026, 7, 1),
    )
    db_session.add(anchor)
    db_session.commit()
    db_session.refresh(pull)
    db_session.refresh(anchor)
    assert pull.line_count == 0 and pull.status == "pulled"
    assert float(anchor.balance_qty) == 0 and anchor.source == "balance_seed"


def _mk_reservation_row(db_session):
    item = _mk_item(db_session, code="IL-RES")
    _imported, generation = _lineage(db_session)
    run = models.PlanningRun(config_snapshot={})
    db_session.add(run)
    db_session.flush()
    req = models.MrpRequirement(
        run_id=run.run_id, item_id=item.item_id,
        period_from=datetime.date(2026, 7, 1), period_to=datetime.date(2026, 7, 15),
    )
    db_session.add(req)
    db_session.flush()
    entry = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id, requirement_id=req.id,
        priority_period_from=datetime.date(2026, 7, 1), priority_period_to=datetime.date(2026, 7, 15),
    )
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)
    return item, entry


def test_reservation_entry_defaults(db_session):
    _item, entry = _mk_reservation_row(db_session)
    assert entry.planning_stock_pool == "default"
    assert entry.realization_mode == "make"
    assert float(entry.reserved_qty) == 0 and float(entry.realized_qty) == 0
    assert float(entry.covered_from_stock_at_freeze_qty) == 0
    assert float(entry.replenishment_required_qty) == 0
    assert float(entry.replenishment_received_qty) == 0
    assert entry.lifecycle_status == "active"
    assert int(entry.freeze_version) == 0


def test_reservation_event_defaults(db_session):
    item, entry = _mk_reservation_row(db_session)
    ev = models.ReservationEvent(
        ledger_generation_id=entry.ledger_generation_id,
        reservation_id=entry.id, item_id=item.item_id, event_kind="open",
        reserved_delta=6, idempotency_key="open:1",
    )
    db_session.add(ev)
    db_session.commit()
    db_session.refresh(ev)
    assert ev.planning_stock_pool == "default" and ev.match_rule == "" and ev.sle_id is None

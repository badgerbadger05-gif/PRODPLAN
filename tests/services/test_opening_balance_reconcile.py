"""Contract tests for reconciling the opening balance with 1C at the anchor."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.opening_balance_reconcile import (
    ADJUSTMENT_RECORDER_TYPE,
    OpeningBalanceReconcileError,
    opening_boundary,
    reconcile_opening_balance,
)
from app.services.item_ledger.physical import LedgerKey
from app.services.item_ledger.physical_visibility import visible_sles_for_generation

OPENING_AT = datetime(2026, 6, 2, tzinfo=timezone.utc)
# posting_at / anchor_at are TIMESTAMP columns without a zone; SQLite hands them
# back naive, so compare against the same instant with the zone dropped.
OPENING_AT_NAIVE = OPENING_AT.replace(tzinfo=None)
ORG = "ORG-1"
WH = "WH-1"


def _world(db_session, *, seeded_qty=Decimal("2320")):
    """One seeded key at the anchor, inside a BUILDING refresh generation."""
    opening_batch = models.PhysicalImportBatch(
        batch_key="historical-bootstrap-opening:g1:hash",
        status="completed",
        cutoff=OPENING_AT,
        source_watermarks={"source": "seed", "opening_at": OPENING_AT.isoformat()},
        completed_at=OPENING_AT,
    )
    db_session.add(opening_batch)
    item = models.Item(item_code="FASTENER-1", item_name="Nut", item_ref1c="ITEM-1")
    db_session.add_all([item, models.StockWarehouse(warehouse_ref1c=WH, warehouse_name="WH")])
    db_session.flush()

    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=int(opening_batch.id),
        source_content_hash="s" * 64,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref=ORG,
        warehouse_ref1c=WH,
        qty=seeded_qty,
        qty_after=seeded_qty,
        posting_at=OPENING_AT,
        record_type="Receipt",
        movement_kind="seed",
        recorder_type="seed",
        recorder_ref="seed:2026-06-02:abc",
        line_no="0",
        ingest_source="seed",
    ))
    db_session.add(models.StockLedgerAnchor(
        ingest_batch_id=int(opening_batch.id),
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref=ORG,
        warehouse_ref1c=WH,
        anchor_period=OPENING_AT.date(),
        anchor_at=OPENING_AT,
        balance_qty=seeded_qty,
        source="balance_seed",
    ))
    generation = models.LedgerGeneration(
        generation_key="refresh-candidate",
        status="building",
        cutoff=OPENING_AT + timedelta(days=55),
        source_watermarks={"generation_kind": "physical_refresh", "parent_generation_id": 1},
        physical_import_batch=opening_batch,
        algorithm_version="ledger-physical-refresh-generation/1",
    )
    db_session.add(generation)
    db_session.flush()
    db_session.commit()
    return generation, item


def test_opening_boundary_is_found_by_watermark_not_source_tag(db_session):
    generation, _item = _world(db_session)
    boundary = opening_boundary(db_session)
    assert boundary is not None
    batch, opening_at = boundary
    assert batch.source_watermarks["source"] == "seed"
    assert opening_at == OPENING_AT
    assert generation.physical_import_batch_id == batch.id


def test_agreeing_opening_balance_writes_nothing(db_session):
    generation, item = _world(db_session)

    result = reconcile_opening_balance(
        db_session,
        ledger_generation_id=int(generation.id),
        opening_snapshot={LedgerKey(item.item_id, "", ORG, WH): Decimal("2320")},
    )

    assert result.created is False
    assert result.adjusted_keys == 0
    assert result.physical_import_batch_id is None
    assert db_session.query(models.LedgerBuildBatch).count() == 0


def test_backdated_document_behind_the_anchor_becomes_an_adjustment(db_session):
    """1C now reports 140 less at T0 than the seed captured."""
    generation, item = _world(db_session)

    result = reconcile_opening_balance(
        db_session,
        ledger_generation_id=int(generation.id),
        opening_snapshot={LedgerKey(item.item_id, "", ORG, WH): Decimal("2180")},
    )

    assert result.created is True
    assert result.adjusted_keys == 1
    assert result.net_delta == "-140"
    adjustment = result.adjustments[0]
    assert adjustment.ledger_qty == "2320"
    assert adjustment.balance_qty == "2180"

    db_session.commit()
    rows = visible_sles_for_generation(db_session, int(generation.id))
    assert sum(row.qty for row in rows) == Decimal("2180")
    written = [row for row in rows if row.recorder_type == ADJUSTMENT_RECORDER_TYPE]
    assert len(written) == 1
    assert written[0].qty == Decimal("-140")
    assert written[0].record_type == "Expense"
    assert written[0].posting_at == OPENING_AT_NAIVE
    assert generation.physical_import_batch_id == result.physical_import_batch_id


def test_second_run_over_the_same_truth_is_a_no_op(db_session):
    """The correction must not repeat once it is in the ledger."""
    generation, item = _world(db_session)
    snapshot = {LedgerKey(item.item_id, "", ORG, WH): Decimal("2180")}

    first = reconcile_opening_balance(
        db_session, ledger_generation_id=int(generation.id), opening_snapshot=snapshot
    )
    db_session.commit()
    assert first.created is True

    later = models.LedgerGeneration(
        generation_key="refresh-candidate-2",
        status="building",
        cutoff=generation.cutoff + timedelta(days=1),
        source_watermarks={"generation_kind": "physical_refresh", "parent_generation_id": int(generation.id)},
        physical_import_batch_id=first.physical_import_batch_id,
        algorithm_version="ledger-physical-refresh-generation/1",
    )
    db_session.add(later)
    db_session.flush()

    second = reconcile_opening_balance(
        db_session, ledger_generation_id=int(later.id), opening_snapshot=snapshot
    )
    assert second.created is False
    assert second.adjusted_keys == 0
    assert (
        db_session.query(models.StockLedgerEntry)
        .filter(models.StockLedgerEntry.recorder_type == ADJUSTMENT_RECORDER_TYPE)
        .count()
    ) == 1


def test_completed_checkpoint_short_circuits_a_resumed_generation(db_session):
    generation, item = _world(db_session)
    snapshot = {LedgerKey(item.item_id, "", ORG, WH): Decimal("2180")}
    first = reconcile_opening_balance(
        db_session, ledger_generation_id=int(generation.id), opening_snapshot=snapshot
    )
    db_session.commit()

    resumed = reconcile_opening_balance(
        db_session,
        ledger_generation_id=int(generation.id),
        # A different answer must not be applied on top of a sealed checkpoint.
        opening_snapshot={LedgerKey(item.item_id, "", ORG, WH): Decimal("999")},
    )
    assert resumed.created is False
    assert resumed.checkpoint_id == first.checkpoint_id
    assert resumed.adjusted_keys == 1
    assert (
        db_session.query(models.StockLedgerEntry)
        .filter(models.StockLedgerEntry.recorder_type == ADJUSTMENT_RECORDER_TYPE)
        .count()
    ) == 1


def test_key_unknown_to_the_seed_also_gets_an_anchor(db_session):
    """Without an anchor a later pull would re-import that key's pre-T0 history."""
    generation, _item = _world(db_session)
    fresh = models.Item(item_code="FASTENER-2", item_name="Bolt", item_ref1c="ITEM-2")
    db_session.add(fresh)
    db_session.flush()

    reconcile_opening_balance(
        db_session,
        ledger_generation_id=int(generation.id),
        opening_snapshot={
            LedgerKey(_item.item_id, "", ORG, WH): Decimal("2320"),
            LedgerKey(fresh.item_id, "", ORG, WH): Decimal("2"),
        },
    )

    anchor = db_session.query(models.StockLedgerAnchor).filter_by(
        item_id=fresh.item_id, anchor_period=OPENING_AT.date()
    ).one()
    assert anchor.anchor_at == OPENING_AT_NAIVE
    assert anchor.balance_qty == Decimal("2")


def test_mass_shift_is_refused_rather_than_applied(db_session):
    generation, item = _world(db_session)
    extra = []
    for index in range(4):
        row = models.Item(item_code=f"BULK-{index}", item_name="Bulk", item_ref1c=f"BULK-{index}")
        db_session.add(row)
        extra.append(row)
    db_session.flush()

    snapshot = {LedgerKey(item.item_id, "", ORG, WH): Decimal("2180")}
    for row in extra:
        snapshot[LedgerKey(row.item_id, "", ORG, WH)] = Decimal("5")

    with pytest.raises(OpeningBalanceReconcileError, match="safety limit"):
        reconcile_opening_balance(
            db_session,
            ledger_generation_id=int(generation.id),
            opening_snapshot=snapshot,
            max_adjusted_keys=3,
        )
    assert (
        db_session.query(models.StockLedgerEntry)
        .filter(models.StockLedgerEntry.recorder_type == ADJUSTMENT_RECORDER_TYPE)
        .count()
    ) == 0

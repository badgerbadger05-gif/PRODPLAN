"""Phase-0 helpers: opening seed + read-only Balance convergence gate."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.generation_bootstrap import create_historical_generation
from app.services.item_ledger.historical_bootstrap_phase0 import (
    CONVERGENCE_KEY,
    OPENING_BALANCE_KEY,
    OPENING_AT_KEY,
    OPENING_CONTENT_HASH_KEY,
    Phase0BootstrapError,
    BalanceConvergenceResult,
    seed_historical_opening_balance,
    evaluate_historical_balance_convergence,
)
from app.services.item_ledger.physical import LedgerKey


def _generation(
    db_session,
    key: str = "phase0-generation",
):
    historical_from = datetime(2026, 6, 1, tzinfo=timezone.utc)
    replay_from = datetime(2026, 7, 1, tzinfo=timezone.utc)
    cutoff = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)
    return create_historical_generation(
        db_session,
        generation_key=key,
        historical_from_exclusive=historical_from,
        replay_from=replay_from,
        cutoff=cutoff,
    )


def _item(db_session, code: str):
    item = models.Item(item_code=code, item_name=code)
    db_session.add(item)
    db_session.flush()
    return item


def test_seed_historical_opening_balance_is_idempotent_and_conflict_guarded(db_session):
    created = _generation(db_session, "phase0-seed-idem")
    item = _item(db_session, "PH0-ITEM")
    first_snapshot = {LedgerKey(item.item_id, "", "", "WH-1"): Decimal("7")}
    second_snapshot = {LedgerKey(item.item_id, "", "", "WH-1"): Decimal("7.00")}

    first = seed_historical_opening_balance(
        db_session,
        ledger_generation_id=created.ledger_generation_id,
        balance_snapshot=first_snapshot,
    )
    db_session.flush()

    assert isinstance(first.physical_import_batch_id, int)
    assert first.created
    assert first.entries_created == 1
    assert first.opening_at == datetime(2026, 6, 1, tzinfo=timezone.utc)
    generation = db_session.get(models.LedgerGeneration, created.ledger_generation_id)
    opening = generation.source_watermarks[OPENING_BALANCE_KEY]
    assert opening[OPENING_CONTENT_HASH_KEY] == first.content_hash
    assert opening[OPENING_AT_KEY] == first.opening_at.isoformat()
    assert int(opening["physical_import_batch_id"]) == first.physical_import_batch_id
    assert int(generation.physical_import_batch_id) == first.physical_import_batch_id

    repeat = seed_historical_opening_balance(
        db_session,
        ledger_generation_id=created.ledger_generation_id,
        balance_snapshot=second_snapshot,
    )
    assert repeat.created is False
    assert repeat.physical_import_batch_id == first.physical_import_batch_id
    assert repeat.entries_created == 0
    assert repeat.content_hash == first.content_hash

    with pytest.raises(
        Phase0BootstrapError,
        match="snapshot conflicts",
    ):
        seed_historical_opening_balance(
            db_session,
            ledger_generation_id=created.ledger_generation_id,
            balance_snapshot={LedgerKey(item.item_id, "", "", "WH-1"): Decimal("9")},
        )


def test_seed_historical_opening_rejects_preexisting_global_anchor(db_session):
    created = _generation(db_session, "phase0-seed-anchor")
    item = _item(db_session, "PH0-ANCHOR")
    prior = models.PhysicalImportBatch(
        batch_key="global-anchor-batch",
        status="completed",
        source_watermarks={"source": "legacy"},
        completed_at=datetime(2026, 5, 31, tzinfo=timezone.utc),
    )
    db_session.add(prior)
    db_session.flush()
    db_session.add(
        models.StockLedgerAnchor(
            ingest_batch_id=prior.id,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c="WH-1",
            anchor_period=datetime(2026, 7, 24).date(),
            anchor_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
            balance_qty=Decimal("3"),
            source="balance_seed",
        )
    )
    db_session.commit()

    with pytest.raises(
        Phase0BootstrapError,
        match="empty physical Ledger",
    ):
        seed_historical_opening_balance(
            db_session,
            ledger_generation_id=created.ledger_generation_id,
            balance_snapshot={LedgerKey(item.item_id, "", "", "WH-1"): Decimal("7")},
        )


def test_seed_historical_opening_rejects_existing_physical_import_checkpoint(db_session):
    created = _generation(db_session, "phase0-seed-checkpoint")
    db_session.add(
        models.LedgerBuildBatch(
            ledger_generation_id=created.ledger_generation_id,
            stage="physical_import",
            batch_key="preexisting-checkpoint",
            status="completed",
            algorithm_version="historical-physical-import/1",
            metrics={},
            completed_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )
    )
    db_session.commit()

    item = _item(db_session, "PH0-CHK")
    with pytest.raises(
        Phase0BootstrapError,
        match="before physical_import checkpoints",
    ):
        seed_historical_opening_balance(
            db_session,
            ledger_generation_id=created.ledger_generation_id,
            balance_snapshot={LedgerKey(item.item_id, "", "", "WH-1"): Decimal("1")},
        )


def test_balance_convergence_passes_and_persists_metadata(db_session):
    created = _generation(db_session, "phase0-conv-pass")
    item = _item(db_session, "PH0-CONV")
    result_open = seed_historical_opening_balance(
        db_session,
        ledger_generation_id=created.ledger_generation_id,
        balance_snapshot={LedgerKey(item.item_id, "", "", "WH-1"): Decimal("5")},
    )
    db_session.add(
        models.StockLedgerEntry(
            ingest_batch_id=result_open.physical_import_batch_id,
            source_content_hash="phase0-pass",
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c="WH-1",
            qty=Decimal("1"),
            qty_after=Decimal("0"),
            posting_at=result_open.opening_at + timedelta(hours=1),
            record_type="Receipt",
            movement_kind="test",
            recorder_type="phase0",
            recorder_ref="move-1",
            line_no="1",
        )
    )
    db_session.commit()

    checked = evaluate_historical_balance_convergence(
        db_session,
        ledger_generation_id=created.ledger_generation_id,
        balance_snapshot={LedgerKey(item.item_id, "", "", "WH-1"): Decimal("6")},
    )
    assert isinstance(checked, BalanceConvergenceResult)
    assert checked.valid is True
    assert checked.compared == 1
    assert checked.mismatched == 0
    assert checked.matched == 1
    generation = db_session.get(models.LedgerGeneration, created.ledger_generation_id)
    convergence = generation.source_watermarks[CONVERGENCE_KEY]
    assert convergence["valid"] is True
    assert convergence["content_hash"] == checked.content_hash
    assert convergence["physical_import_batch_id"] == result_open.physical_import_batch_id
    assert convergence["cutoff"] == generation.cutoff.replace(tzinfo=timezone.utc).isoformat()


def test_balance_convergence_detects_mismatch_and_marks_invalid(db_session):
    created = _generation(db_session, "phase0-conv-miss")
    item = _item(db_session, "PH0-CONV-MISS")
    result_open = seed_historical_opening_balance(
        db_session,
        ledger_generation_id=created.ledger_generation_id,
        balance_snapshot={LedgerKey(item.item_id, "", "", "WH-1"): Decimal("5")},
    )
    db_session.add(
        models.StockLedgerEntry(
            ingest_batch_id=result_open.physical_import_batch_id,
            source_content_hash="phase0-miss",
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c="WH-1",
            qty=Decimal("1"),
            qty_after=Decimal("0"),
            posting_at=result_open.opening_at + timedelta(hours=1),
            record_type="Receipt",
            movement_kind="test",
            recorder_type="phase0",
            recorder_ref="move-1",
            line_no="1",
        )
    )
    db_session.commit()

    checked = evaluate_historical_balance_convergence(
        db_session,
        ledger_generation_id=created.ledger_generation_id,
        balance_snapshot={LedgerKey(item.item_id, "", "", "WH-1"): Decimal("3")},
    )

    assert checked.valid is False
    assert checked.mismatched == 1
    assert checked.deltas[0].delta_qty == "-3"
    generation = db_session.get(models.LedgerGeneration, created.ledger_generation_id)
    assert generation.source_watermarks[CONVERGENCE_KEY]["valid"] is False

"""Ledger-1 running-balance / seed tests + ledger-2 DB fold (design §2, §6, §9).

Covers INV-fold (bin.on_hand == Σ qty == last qty_after), INV-anchor (seed
writes an anchor, idempotent), narrow rebuild_running_balance, and the DB
materialization of the reservation event fold (INV-RES-fold).
"""

import datetime
from decimal import Decimal

from app import models
from app.services.item_ledger import (
    LedgerKey,
    fold_reservation_entry,
    fold_running_balance,
    rebuild_running_balance,
    seed_from_balance,
)

D = Decimal


def _mk_item(db_session, code="LK-1"):
    item = models.Item(item_code=code, item_name=code)
    db_session.add(item)
    db_session.flush()
    return item


def _f(x):
    return float(x)


def _generation(db_session, suffix):
    batch = models.PhysicalImportBatch(
        batch_key=f"physical-test-{suffix}",
        status="completed",
        cutoff=datetime.datetime(2026, 7, 31),
        source_watermarks={},
        completed_at=datetime.datetime(2026, 7, 31),
    )
    generation = models.LedgerGeneration(
        generation_key=f"physical-test-{suffix}",
        status="building",
        cutoff=datetime.datetime(2026, 7, 31),
        source_watermarks={},
        capabilities={},
        physical_import_batch=batch,
        algorithm_version="test/physical",
    )
    db_session.add(generation)
    db_session.flush()
    return generation


# ---------------------------------------------------------------------------
# fold_running_balance — pure (§2.1 R-A)
# ---------------------------------------------------------------------------


def test_fold_running_balance_pure():
    qty_after, final = fold_running_balance([10, -3, 8])
    assert [_f(x) for x in qty_after] == [10, 7, 15]
    assert _f(final) == 15


def test_fold_running_balance_allows_negative():
    qty_after, final = fold_running_balance([2, -5])
    assert [_f(x) for x in qty_after] == [2, -3]  # not clamped (§4a)
    assert _f(final) == -3


# ---------------------------------------------------------------------------
# seed_from_balance + INV-anchor (§2.1 seed)
# ---------------------------------------------------------------------------


def test_seed_from_balance_writes_sle_bin_anchor(db_session):
    item = _mk_item(db_session)
    generation = _generation(db_session, "seed")
    key = LedgerKey(item.item_id, "", "", "WH1")
    created = seed_from_balance(
        db_session,
        {key: 12},
        anchor_period=datetime.date(2026, 7, 1),
        ledger_generation_id=generation.id,
    )
    db_session.commit()

    assert len(created) == 1
    sle = created[0]
    assert _f(sle.qty) == 12 and _f(sle.qty_after) == 12
    assert sle.movement_kind == "seed" and sle.ingest_source == "seed"

    bin_row = db_session.query(models.StockBin).filter_by(item_id=item.item_id, warehouse_ref1c="WH1").one()
    assert _f(bin_row.on_hand) == 12  # INV-fold

    anchor = db_session.query(models.StockLedgerAnchor).filter_by(item_id=item.item_id, warehouse_ref1c="WH1").one()
    assert _f(anchor.balance_qty) == 12  # INV-anchor
    assert anchor.anchor_period == datetime.date(2026, 7, 1)


def test_seed_from_balance_idempotent(db_session):
    item = _mk_item(db_session, code="LK-IDEM")
    key = LedgerKey(item.item_id, "", "", "WH1")
    seed_from_balance(db_session, {key: 5}, anchor_period=datetime.date(2026, 7, 1))
    db_session.commit()
    # re-seed same period/key → skipped, no duplicate SLE / anchor.
    again = seed_from_balance(db_session, {key: 999}, anchor_period=datetime.date(2026, 7, 1))
    db_session.commit()
    assert again == []
    assert db_session.query(models.StockLedgerEntry).filter_by(item_id=item.item_id).count() == 1
    assert db_session.query(models.StockLedgerAnchor).filter_by(item_id=item.item_id).count() == 1


# ---------------------------------------------------------------------------
# rebuild_running_balance (§6 narrow rebuild + on_hand fold)
# ---------------------------------------------------------------------------


def test_rebuild_running_balance_full(db_session):
    item = _mk_item(db_session, code="LK-RB")
    generation = _generation(db_session, "full")
    key = LedgerKey(item.item_id, "", "", "WH1")
    seed = seed_from_balance(
        db_session,
        {key: 10},
        anchor_period=datetime.date(2026, 7, 1),
        ledger_generation_id=generation.id,
    )
    # append two movements after the seed.
    db_session.add(
        models.StockLedgerEntry(
            ingest_batch_id=seed[0].ingest_batch_id, source_content_hash="1" * 64,
            item_id=item.item_id, warehouse_ref1c="WH1", qty=D("8"), qty_after=D("0"),
            posting_at=datetime.datetime(2026, 7, 5), movement_kind="receipt",
            recorder_type="Doc", recorder_ref="R1", line_no="1",
        )
    )
    db_session.add(
        models.StockLedgerEntry(
            ingest_batch_id=seed[0].ingest_batch_id, source_content_hash="2" * 64,
            item_id=item.item_id, warehouse_ref1c="WH1", qty=D("-5"), qty_after=D("0"),
            posting_at=datetime.datetime(2026, 7, 6), movement_kind="assembly_out",
            recorder_type="Doc", recorder_ref="R2", line_no="1",
        )
    )
    db_session.flush()

    final = rebuild_running_balance(
        db_session, key, ledger_generation_id=generation.id
    )
    assert _f(final) == 13  # 10 + 8 - 5

    rows = (
        db_session.query(models.StockLedgerEntry)
        .filter_by(item_id=item.item_id)
        .order_by(models.StockLedgerEntry.posting_at.asc())
        .all()
    )
    assert [_f(r.qty_after) for r in rows] == [10, 18, 13]

    bin_row = db_session.query(models.StockBin).filter_by(item_id=item.item_id, warehouse_ref1c="WH1").one()
    assert _f(bin_row.on_hand) == 13  # INV-fold: on_hand == last qty_after == Σ qty


def test_rebuild_running_balance_narrow_from_point(db_session):
    item = _mk_item(db_session, code="LK-NARROW")
    generation = _generation(db_session, "narrow")
    key = LedgerKey(item.item_id, "", "", "WH1")
    seed = seed_from_balance(
        db_session,
        {key: 10},
        anchor_period=datetime.date(2026, 7, 1),
        ledger_generation_id=generation.id,
    )
    db_session.add(
        models.StockLedgerEntry(
            ingest_batch_id=seed[0].ingest_batch_id, source_content_hash="3" * 64,
            item_id=item.item_id, warehouse_ref1c="WH1", qty=D("4"), qty_after=D("999"),
            posting_at=datetime.datetime(2026, 7, 10), movement_kind="receipt",
            recorder_type="Doc", recorder_ref="RN", line_no="1",
        )
    )
    db_session.flush()
    final = rebuild_running_balance(
        db_session,
        key,
        from_posting_at=datetime.datetime(2026, 7, 9),
        ledger_generation_id=generation.id,
    )
    assert _f(final) == 14  # opening 10 (seed) + 4


# ---------------------------------------------------------------------------
# ledger-2 DB fold materialization (INV-RES-fold, §9)
# ---------------------------------------------------------------------------


def _mk_reservation(db_session):
    item = _mk_item(db_session, code="RES-FOLD")
    generation = _generation(db_session, "reservation")
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
        item_id=item.item_id, requirement_id=req.id, run_id=run.run_id, freeze_version=1,
        priority_period_from=datetime.date(2026, 7, 1), priority_period_to=datetime.date(2026, 7, 15),
        realization_mode="consume",
    )
    db_session.add(entry)
    db_session.flush()
    return item, entry


def test_fold_reservation_entry_materializes_cache(db_session):
    item, entry = _mk_reservation(db_session)
    for i, (kind, r_delta, z_delta) in enumerate(
        [("open", 6, 0), ("realize", 0, 4), ("amend", -1, 0)]
    ):
        db_session.add(
            models.ReservationEvent(
                ledger_generation_id=entry.ledger_generation_id,
                reservation_id=entry.id, item_id=item.item_id, event_kind=kind,
                reserved_delta=D(str(r_delta)), realized_delta=D(str(z_delta)),
                idempotency_key=f"ev:{entry.id}:{i}",
            )
        )
    db_session.flush()

    fold = fold_reservation_entry(db_session, entry.id)
    db_session.commit()
    db_session.refresh(entry)
    # reserved = 6-1 = 5, realized = 4, outstanding = 1.
    assert _f(fold.reserved_qty) == 5 and _f(fold.realized_qty) == 4 and _f(fold.outstanding) == 1
    assert _f(entry.reserved_qty) == 5 and _f(entry.realized_qty) == 4  # INV-RES-fold


def test_reservation_event_idempotency_key_unique(db_session):
    import pytest
    from sqlalchemy.exc import IntegrityError

    item, entry = _mk_reservation(db_session)
    db_session.add(
        models.ReservationEvent(
            ledger_generation_id=entry.ledger_generation_id,
            reservation_id=entry.id, item_id=item.item_id, event_kind="open",
            reserved_delta=D("6"), idempotency_key="open:dup",
        )
    )
    db_session.flush()
    db_session.add(
            models.ReservationEvent(
                ledger_generation_id=entry.ledger_generation_id,
                reservation_id=entry.id, item_id=item.item_id, event_kind="open",
            reserved_delta=D("6"), idempotency_key="open:dup",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()

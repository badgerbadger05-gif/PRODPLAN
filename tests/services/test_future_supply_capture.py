from datetime import date, datetime
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import text

from app import models
from app.services.item_ledger.future_supply_capture import (
    FutureSupplyCaptureError,
    FutureSupplyEvidence,
    future_supply_evidence_hash,
    replace_future_supply_capture,
)


def _context(db, suffix="one"):
    cutoff = datetime(2026, 7, 31, 23, 59)
    physical = models.PhysicalImportBatch(
        batch_key=f"future-physical-{suffix}", status="completed", cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=f"future-generation-{suffix}", status="building", cutoff=cutoff,
        source_watermarks={}, capabilities={}, physical_import_batch=physical,
        algorithm_version="test",
    )
    item = models.Item(item_code=f"FUT-{suffix}", item_name="Future")
    db.add_all([generation, item])
    db.flush()
    batch = models.LedgerBuildBatch(
        ledger_generation_id=generation.id, stage="snapshot_build", status="building",
        batch_key=f"future-snapshot-{suffix}", algorithm_version="test", metrics={},
    )
    db.add(batch)
    db.flush()
    return generation, batch, item


def _evidence(generation, item, *, kind="wip_order", ref="order-1", line="1", status="exact", ordered="10", realized="4", **changes):
    values = dict(
        supply_kind=kind, item_id=item.item_id, planning_stock_pool="main",
        destination_warehouse_ref1c="WH-1", source_ref=ref, source_line_ref=line,
        source_local_id="local-1", ordered_qty_at_cutoff=Decimal(ordered),
        realized_qty_at_cutoff=Decimal(realized), eta_date=date(2026, 8, 5),
        source_state_key="open", source_updated_at=datetime(2026, 7, 31, 12),
        capture_cutoff=generation.cutoff, evidence_status=status,
    )
    values.update(changes)
    unsigned = FutureSupplyEvidence(**values)
    return FutureSupplyEvidence(**{**values, "source_content_hash": future_supply_evidence_hash(unsigned)})


def test_captures_exact_wip_and_supplier_evidence(db_session):
    generation, batch, item = _context(db_session)
    rows = [
        _evidence(generation, item, kind="wip_order", ref="work-1", line="1", ordered="12", realized="3"),
        _evidence(generation, item, kind="supplier_order", ref="supplier-1", line="2", ordered="5", realized="7"),
    ]

    metrics = replace_future_supply_capture(db_session, generation.id, batch.id, rows)

    stored = db_session.query(models.LedgerFutureSupply).order_by(models.LedgerFutureSupply.supply_kind).all()
    assert [row.supply_kind for row in stored] == ["supplier_order", "wip_order"]
    assert [Decimal(str(row.open_qty_at_cutoff)) for row in stored] == [Decimal("0"), Decimal("9")]
    assert metrics["open_qty"] == Decimal("9")
    assert metrics["surplus_qty"] == Decimal("2")
    assert batch.status == "building"


@pytest.mark.parametrize("status", ["rejected", "ambiguous", "unmatched"])
def test_non_exact_evidence_is_preserved_but_never_supply(db_session, status):
    generation, batch, item = _context(db_session)
    rejected = _evidence(
        generation, item, status=status, ref=None, line=None,
        destination_warehouse_ref1c="", ordered="8", realized="1", reason="two origins",
    )

    metrics = replace_future_supply_capture(db_session, generation.id, batch.id, [rejected])

    row = db_session.query(models.LedgerFutureSupply).one()
    assert row.evidence_status == status
    assert Decimal(str(row.open_qty_at_cutoff)) == 0
    assert metrics["non_supply_rows"] == 1


@pytest.mark.parametrize("broken", ["duplicate", "hash", "cutoff", "item", "pool", "state"])
def test_rejects_duplicate_identity_hash_conflict_and_cutoff(db_session, broken):
    generation, batch, item = _context(db_session, broken)
    first = _evidence(generation, item)
    if broken == "duplicate":
        values = [first, _evidence(generation, item, ordered="11")]
    elif broken == "hash":
        values = [first, replace(_evidence(generation, item, ref="other"), source_content_hash=first.source_content_hash)]
    elif broken == "item":
        values = [_evidence(generation, item, status="rejected", item_id=None)]
    elif broken == "pool":
        values = [_evidence(generation, item, planning_stock_pool="")]
    elif broken == "state":
        values = [_evidence(generation, item, source_state_key="")]
    else:
        values = [_evidence(generation, item, capture_cutoff=datetime(2026, 8, 1))]

    with pytest.raises(FutureSupplyCaptureError):
        replace_future_supply_capture(db_session, generation.id, batch.id, values)
    assert db_session.query(models.LedgerFutureSupply).count() == 0


def test_double_identical_capture_is_byte_stable(db_session):
    generation, batch, item = _context(db_session)
    evidence = [_evidence(generation, item)]
    first = replace_future_supply_capture(db_session, generation.id, batch.id, evidence)
    db_session.flush()
    first_row = db_session.query(models.LedgerFutureSupply).one()
    first_values = (
        first_row.id, first_row.created_at, first_row.source_content_hash,
        str(first_row.open_qty_at_cutoff), first_row.capture_cutoff,
    )

    second = replace_future_supply_capture(db_session, generation.id, batch.id, evidence)
    second_row = db_session.query(models.LedgerFutureSupply).one()
    assert first == second
    assert (
        second_row.id, second_row.created_at, second_row.source_content_hash,
        str(second_row.open_qty_at_cutoff), second_row.capture_cutoff,
    ) == first_values
    assert db_session.query(models.LedgerFutureSupply).count() == 1


def test_reversed_input_has_same_metrics_and_does_not_rewrite(db_session):
    generation, batch, item = _context(db_session)
    rows = [
        _evidence(generation, item, kind="wip_order", ref="work", line="1"),
        _evidence(generation, item, kind="supplier_order", ref="supplier", line="2"),
    ]
    first = replace_future_supply_capture(db_session, generation.id, batch.id, rows)
    before = [(row.id, row.created_at) for row in db_session.query(models.LedgerFutureSupply).order_by(models.LedgerFutureSupply.id)]

    second = replace_future_supply_capture(db_session, generation.id, batch.id, list(reversed(rows)))
    after = [(row.id, row.created_at) for row in db_session.query(models.LedgerFutureSupply).order_by(models.LedgerFutureSupply.id)]

    assert second == first
    assert after == before


def test_sealed_snapshot_batch_is_re_read_but_never_overwritten(db_session):
    """A BUILDING generation may be resumed after its snapshot batch was sealed.

    ``close_fixed_plan`` replays every stage from the top, so the capture is
    re-entered once ``snapshot_build`` is already COMPLETED.  Insisting on a
    BUILDING batch killed that resume outright.  A completed batch is now
    accepted as a re-read only: identical evidence returns the same metrics and
    touches nothing, while a changed capture is a conflict, not an overwrite.
    """
    generation, batch, item = _context(db_session, "sealed")
    evidence = [_evidence(generation, item)]
    first = replace_future_supply_capture(db_session, generation.id, batch.id, evidence)
    db_session.flush()
    before = [
        (row.id, row.created_at)
        for row in db_session.query(models.LedgerFutureSupply).order_by(
            models.LedgerFutureSupply.id
        )
    ]
    batch.status = "completed"
    db_session.flush()

    assert replace_future_supply_capture(
        db_session, generation.id, batch.id, evidence
    ) == first
    assert [
        (row.id, row.created_at)
        for row in db_session.query(models.LedgerFutureSupply).order_by(
            models.LedgerFutureSupply.id
        )
    ] == before

    with pytest.raises(FutureSupplyCaptureError, match="completed capture batch"):
        replace_future_supply_capture(
            db_session,
            generation.id,
            batch.id,
            [_evidence(generation, item, ordered="99")],
        )
    assert db_session.query(models.LedgerFutureSupply).count() == 1


def test_capture_batch_of_another_stage_or_generation_is_still_refused(db_session):
    generation, batch, item = _context(db_session, "foreign")
    other = models.LedgerBuildBatch(
        ledger_generation_id=generation.id, stage="shelf_projection",
        status="completed", batch_key="foreign-stage", algorithm_version="test",
        metrics={},
    )
    db_session.add(other)
    db_session.flush()

    with pytest.raises(FutureSupplyCaptureError, match="snapshot_build batch"):
        replace_future_supply_capture(
            db_session, generation.id, other.id, [_evidence(generation, item)]
        )


def test_outer_rollback_removes_capture(db_session):
    generation, batch, item = _context(db_session)
    db_session.commit()

    outer = db_session.begin()
    # SQLite starts a real transaction only on a write; establish it before
    # the service's inner savepoint so rollback exercises outer ownership.
    db_session.execute(text("UPDATE ledger_generation SET id = id WHERE id = :id"), {"id": generation.id})
    replace_future_supply_capture(db_session, generation.id, batch.id, [_evidence(generation, item)])
    outer.rollback()

    assert db_session.query(models.LedgerFutureSupply).count() == 0

from datetime import date, datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app import models


def _lineage(db_session, suffix):
    imported = models.PhysicalImportBatch(
        batch_key=f"physical-{suffix}",
        status="completed",
        cutoff=datetime(2026, 7, 23, tzinfo=timezone.utc),
        source_watermarks={"1c": suffix},
        completed_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    generation = models.LedgerGeneration(
        generation_key=f"generation-{suffix}",
        status="building",
        cutoff=datetime(2026, 7, 23, tzinfo=timezone.utc),
        source_watermarks={},
        capabilities={},
        algorithm_version="test/1",
        physical_import_batch=imported,
    )
    db_session.add(generation)
    db_session.flush()
    return imported, generation


def _item_and_requirement(db_session):
    item = models.Item(item_code="LINEAGE-A", item_name="Lineage item")
    db_session.add(item)
    db_session.flush()
    run = models.PlanningRun(
        status="COMPLETED",
        config_snapshot={},
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
    )
    db_session.add(run)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=1,
        net_required_qty=1,
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
    )
    db_session.add(requirement)
    db_session.flush()
    return item, requirement


def test_physical_fact_revision_is_shared_across_generations(db_session):
    imported_one, generation_one = _lineage(db_session, "one")
    imported_two, generation_two = _lineage(db_session, "two")
    item, _ = _item_and_requirement(db_session)
    old = models.StockLedgerEntry(
        ingest_batch_id=imported_one.id,
        source_content_hash="a" * 64,
        item_id=item.item_id,
        qty=1,
        posting_at=datetime(2026, 7, 1),
        recorder_type="receipt",
        recorder_ref="DOC-1",
        line_no="1",
    )
    new = models.StockLedgerEntry(
        ingest_batch_id=imported_two.id,
        source_content_hash="b" * 64,
        item_id=item.item_id,
        qty=2,
        posting_at=datetime(2026, 7, 1),
        recorder_type="receipt",
        recorder_ref="DOC-1",
        line_no="1",
    )
    db_session.add_all([old, new])
    db_session.flush()
    edge = models.StockLedgerFactSupersession(
        old_sle_id=old.id,
        new_sle_id=new.id,
        import_batch_id=imported_two.id,
    )
    db_session.add(edge)
    db_session.commit()

    assert old.ingest_batch_id == generation_one.physical_import_batch_id
    assert new.ingest_batch_id == generation_two.physical_import_batch_id
    assert edge.old_sle_id == old.id


def test_reservation_identity_is_generation_scoped(db_session):
    _, generation_one = _lineage(db_session, "one")
    _, generation_two = _lineage(db_session, "two")
    item, requirement = _item_and_requirement(db_session)
    common = {
        "item_id": item.item_id,
        "requirement_id": requirement.id,
        "run_id": requirement.run_id,
        "priority_period_from": date(2026, 7, 1),
        "priority_period_to": date(2026, 7, 31),
        "realization_mode": "make",
    }
    db_session.add_all([
        models.ReservationEntry(ledger_generation_id=generation_one.id, **common),
        models.ReservationEntry(ledger_generation_id=generation_two.id, **common),
    ])
    db_session.commit()

    assert db_session.query(models.ReservationEntry).count() == 2


def test_reservation_idempotency_key_is_generation_scoped(db_session):
    _, generation_one = _lineage(db_session, "one")
    _, generation_two = _lineage(db_session, "two")
    item, requirement = _item_and_requirement(db_session)
    entries = []
    for generation in (generation_one, generation_two):
        entry = models.ReservationEntry(
            ledger_generation_id=generation.id,
            item_id=item.item_id,
            requirement_id=requirement.id,
            run_id=requirement.run_id,
            priority_period_from=date(2026, 7, 1),
            priority_period_to=date(2026, 7, 31),
            realization_mode="make",
        )
        db_session.add(entry)
        db_session.flush()
        entries.append(entry)
        db_session.add(models.ReservationEvent(
            ledger_generation_id=generation.id,
            reservation_id=entry.id,
            item_id=item.item_id,
            event_kind="open",
            idempotency_key="open:same-requirement",
        ))
    db_session.commit()

    assert db_session.query(models.ReservationEvent).count() == 2


def test_duplicate_revision_hash_is_rejected(db_session):
    imported, _ = _lineage(db_session, "one")
    item, _ = _item_and_requirement(db_session)
    values = {
        "ingest_batch_id": imported.id,
        "source_content_hash": "a" * 64,
        "item_id": item.item_id,
        "qty": 1,
        "posting_at": datetime(2026, 7, 1),
        "recorder_type": "receipt",
        "recorder_ref": "DOC-1",
        "line_no": "1",
    }
    db_session.add_all([
        models.StockLedgerEntry(**values),
        models.StockLedgerEntry(**values),
    ])

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_supersession_allows_removal_tombstone(db_session):
    imported, _ = _lineage(db_session, "remove")
    item, _ = _item_and_requirement(db_session)
    old = models.StockLedgerEntry(
        ingest_batch_id=imported.id,
        source_content_hash="c" * 64,
        item_id=item.item_id,
        qty=1,
        posting_at=datetime(2026, 7, 1),
        recorder_type="receipt",
        recorder_ref="REMOVED-DOC",
        line_no="1",
    )
    db_session.add(old)
    db_session.flush()
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=old.id,
        new_sle_id=None,
        import_batch_id=imported.id,
    ))
    db_session.commit()

    edge = db_session.query(models.StockLedgerFactSupersession).one()
    assert edge.new_sle_id is None

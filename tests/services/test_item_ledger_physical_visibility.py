from datetime import datetime
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.physical_visibility import (
    PhysicalVisibilityError,
    import_batch_provenance,
    visible_sles,
    visible_sles_for_generation,
)


def _batch(db, key, content):
    batch = models.PhysicalImportBatch(
        batch_key=key,
        status="completed",
        cutoff=datetime(2026, 7, 23, 12, 0),
        source_watermarks={
            "recorder_type": "Document_Test",
            "recorder_ref": "DOC-1",
            "content_hash": content,
        },
        completed_at=datetime(2026, 7, 23, 12, 0),
    )
    db.add(batch)
    db.flush()
    return batch


def _sle(db, batch, item, content, qty, *, active):
    row = models.StockLedgerEntry(
        ingest_batch_id=batch.id,
        source_content_hash=content,
        item_id=item.item_id,
        qty=Decimal(qty),
        posting_at=datetime(2026, 7, 1),
        recorder_type="Document_Test",
        recorder_ref="DOC-1",
        line_no="1",
        active=active,
    )
    db.add(row)
    db.flush()
    return row


def test_visibility_replays_a_to_b_to_a_and_tombstone(db_session):
    item = models.Item(item_code="VIS-A", item_name="Visible item")
    db_session.add(item)
    db_session.flush()
    batch_a = _batch(db_session, "doc-1/revision-1", "a" * 64)
    revision_a = _sle(
        db_session, batch_a, item, "a" * 64, "1", active=False,
    )
    batch_b = _batch(db_session, "doc-1/revision-2", "b" * 64)
    revision_b = _sle(
        db_session, batch_b, item, "b" * 64, "2", active=False,
    )
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=revision_a.id,
        new_sle_id=revision_b.id,
        import_batch_id=batch_b.id,
    ))
    batch_a2 = _batch(db_session, "doc-1/revision-3", "a" * 64)
    revision_a2 = _sle(
        db_session, batch_a2, item, "a" * 64, "1", active=False,
    )
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=revision_b.id,
        new_sle_id=revision_a2.id,
        import_batch_id=batch_a2.id,
    ))
    batch_removed = _batch(db_session, "doc-1/revision-4", "tombstone")
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=revision_a2.id,
        new_sle_id=None,
        import_batch_id=batch_removed.id,
    ))
    db_session.commit()

    assert [row.id for row in visible_sles(
        db_session, physical_import_batch_id=batch_a.id,
    )] == [revision_a.id]
    assert [row.id for row in visible_sles(
        db_session, physical_import_batch_id=batch_b.id,
    )] == [revision_b.id]
    assert [row.id for row in visible_sles(
        db_session, physical_import_batch_id=batch_a2.id,
    )] == [revision_a2.id]
    assert visible_sles(
        db_session, physical_import_batch_id=batch_removed.id,
    ) == []


def test_accepted_generation_remains_reproducible_after_later_import(db_session):
    item = models.Item(item_code="VIS-GEN", item_name="Generation item")
    db_session.add(item)
    db_session.flush()
    initial_batch = _batch(db_session, "gen-doc/revision-1", "a" * 64)
    initial = _sle(
        db_session, initial_batch, item, "a" * 64, "3", active=False,
    )
    generation = models.LedgerGeneration(
        generation_key="accepted-before-correction",
        status="accepted",
        cutoff=datetime(2026, 7, 23, 12, 0),
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch_id=initial_batch.id,
        algorithm_version="visibility/1",
        accepted_at=datetime(2026, 7, 23, 12, 0),
    )
    db_session.add(generation)
    later_batch = _batch(db_session, "gen-doc/revision-2", "b" * 64)
    later = _sle(
        db_session, later_batch, item, "b" * 64, "5", active=True,
    )
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=initial.id,
        new_sle_id=later.id,
        import_batch_id=later_batch.id,
    ))
    db_session.commit()

    rows = visible_sles_for_generation(db_session, generation.id)

    assert [row.id for row in rows] == [initial.id]
    assert rows[0].active is False


def test_visibility_requires_completed_deterministic_batch(db_session):
    batch = models.PhysicalImportBatch(
        batch_key="incomplete",
        status="building",
        source_watermarks={},
    )
    db_session.add(batch)
    db_session.commit()

    with pytest.raises(PhysicalVisibilityError, match="completed required"):
        visible_sles(db_session, physical_import_batch_id=batch.id)


def test_provenance_preserves_recorder_and_content_metadata(db_session):
    batch = _batch(db_session, "doc-meta/revision-1", "c" * 64)
    db_session.commit()

    provenance = import_batch_provenance(db_session, batch.id)

    assert provenance["batch_key"] == "doc-meta/revision-1"
    assert provenance["source_watermarks"] == {
        "recorder_type": "Document_Test",
        "recorder_ref": "DOC-1",
        "content_hash": "c" * 64,
    }

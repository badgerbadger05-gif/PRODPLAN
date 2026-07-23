from datetime import datetime
from decimal import Decimal
import re

from app import models
from app.services.item_ledger.ingest import (
    EMPTY_GUID,
    INGEST_SOURCE,
    pull_recorder_movements,
)
from app.services.item_ledger.physical_visibility import visible_sles


ASSEMBLY = "Document_СборкаЗапасов"


class RecorderClient:
    def __init__(self, lines):
        self.lines = lines

    def get_all(self, entity_name, filter_query=None, order_by=None, **kwargs):
        if entity_name != "AccumulationRegister_ЗапасыНаСкладах":
            return []
        match = re.search(r"guid'([^']+)'", filter_query or "")
        if not match:
            return []
        return [{"RecordSet": list(self.lines)}]


def _line(qty):
    return {
        "Period": "2026-07-10T10:00:00",
        "LineNumber": "1",
        "Active": True,
        "RecordType": "Receipt",
        "Организация_Key": "ORG1",
        "Номенклатура_Key": "ITEM-REF",
        "Характеристика_Key": EMPTY_GUID,
        "СтруктурнаяЕдиница_Key": "WH-REF",
        "Количество": qty,
    }


def _lineage(db):
    batch = models.PhysicalImportBatch(
        batch_key="test-projection-input",
        status="completed",
        cutoff=datetime(2026, 7, 10),
        source_watermarks={},
        completed_at=datetime(2026, 7, 10),
    )
    generation = models.LedgerGeneration(
        generation_key="test-projection-generation",
        status="building",
        cutoff=datetime(2026, 7, 10),
        source_watermarks={},
        capabilities={},
        physical_import_batch=batch,
        algorithm_version="test/physical-revision",
    )
    db.add(generation)
    db.flush()
    return generation


def _catalog(db):
    item = models.Item(
        item_code="REVISION-ITEM",
        item_name="Revision item",
        item_ref1c="ITEM-REF",
    )
    warehouse = models.StockWarehouse(
        warehouse_ref1c="WH-REF",
        warehouse_name="Revision warehouse",
    )
    db.add_all([item, warehouse])
    db.flush()
    return item


def _recorder_batches(db, recorder_ref):
    return [
        batch
        for batch in db.query(models.PhysicalImportBatch)
        .order_by(models.PhysicalImportBatch.id)
        .all()
        if (batch.source_watermarks or {}).get("recorder_ref") == recorder_ref
    ]


def test_identical_pull_is_true_noop_and_preserves_sle_id(db_session):
    item = _catalog(db_session)
    generation = _lineage(db_session)

    first = pull_recorder_movements(
        db_session,
        ASSEMBLY,
        "DOC-1",
        client=RecorderClient([_line(5)]),
        ledger_generation_id=generation.id,
    )
    db_session.flush()
    sle_id = db_session.query(models.StockLedgerEntry.id).scalar()
    batch_count = db_session.query(models.PhysicalImportBatch).count()

    second = pull_recorder_movements(
        db_session,
        ASSEMBLY,
        "DOC-1",
        client=RecorderClient([_line(Decimal("5.0"))]),
        ledger_generation_id=generation.id,
    )
    db_session.flush()

    active = db_session.query(models.StockLedgerEntry).filter_by(active=True).one()
    assert first.inserted == 1
    assert second.inserted == 0 and second.deleted == 0
    assert active.id == sle_id
    assert active.ingest_batch_id is not None
    assert len(active.source_content_hash) == 64
    assert db_session.query(models.PhysicalImportBatch).count() == batch_count
    assert (
        db_session.query(models.StockBin)
        .filter_by(ledger_generation_id=generation.id, item_id=item.item_id)
        .one()
        .on_hand
        == Decimal("5")
    )


def test_changed_pull_keeps_inactive_fact_and_links_revision(db_session):
    item = _catalog(db_session)
    generation = _lineage(db_session)
    pull_recorder_movements(
        db_session,
        ASSEMBLY,
        "DOC-2",
        client=RecorderClient([_line(5)]),
        ledger_generation_id=generation.id,
    )
    db_session.flush()
    old = db_session.query(models.StockLedgerEntry).one()

    result = pull_recorder_movements(
        db_session,
        ASSEMBLY,
        "DOC-2",
        client=RecorderClient([_line(8)]),
        ledger_generation_id=generation.id,
    )
    db_session.flush()

    rows = (
        db_session.query(models.StockLedgerEntry)
        .filter_by(recorder_ref="DOC-2", ingest_source=INGEST_SOURCE)
        .order_by(models.StockLedgerEntry.id)
        .all()
    )
    assert result.deleted == 0 and result.inserted == 1
    assert len(rows) == 2
    assert rows[0].id == old.id and rows[0].active is False
    assert rows[1].active is True and rows[1].qty == Decimal("8")
    edge = db_session.query(models.StockLedgerFactSupersession).one()
    assert (edge.old_sle_id, edge.new_sle_id) == (rows[0].id, rows[1].id)
    bin_row = (
        db_session.query(models.StockBin)
        .filter_by(ledger_generation_id=generation.id, item_id=item.item_id)
        .one()
    )
    assert bin_row.on_hand == Decimal("8")


def test_a_to_b_to_a_creates_three_monotonic_boundaries(db_session):
    item = _catalog(db_session)
    generation = _lineage(db_session)

    for qty in (5, 8, 5):
        pull_recorder_movements(
            db_session,
            ASSEMBLY,
            "DOC-ABA",
            client=RecorderClient([_line(qty)]),
            ledger_generation_id=generation.id,
        )
        db_session.flush()

    batches = _recorder_batches(db_session, "DOC-ABA")
    rows = (
        db_session.query(models.StockLedgerEntry)
        .filter_by(recorder_ref="DOC-ABA")
        .order_by(models.StockLedgerEntry.id)
        .all()
    )
    edges = (
        db_session.query(models.StockLedgerFactSupersession)
        .order_by(models.StockLedgerFactSupersession.id)
        .all()
    )

    assert len(batches) == 3
    assert [batch.id for batch in batches] == sorted(batch.id for batch in batches)
    assert len({batch.batch_key for batch in batches}) == 3
    assert [
        batch.source_watermarks["previous_import_batch_id"] for batch in batches
    ] == [None, batches[0].id, batches[1].id]
    assert [
        batch.source_watermarks["content_hash"] for batch in batches
    ][0] == batches[2].source_watermarks["content_hash"]
    assert len(rows) == 3
    assert [row.qty for row in rows] == [
        Decimal("5"), Decimal("8"), Decimal("5")
    ]
    assert [row.active for row in rows] == [False, False, True]
    assert [(edge.old_sle_id, edge.new_sle_id) for edge in edges] == [
        (rows[0].id, rows[1].id),
        (rows[1].id, rows[2].id),
    ]
    assert [
        visible_sles(
            db_session, physical_import_batch_id=batch.id
        )[0].qty
        for batch in batches
    ] == [Decimal("5"), Decimal("8"), Decimal("5")]

    # Consecutive identical A remains a true no-op.
    last_id = rows[-1].id
    result = pull_recorder_movements(
        db_session,
        ASSEMBLY,
        "DOC-ABA",
        client=RecorderClient([_line(5)]),
        ledger_generation_id=generation.id,
    )
    db_session.flush()
    assert result.inserted == 0
    assert len(_recorder_batches(db_session, "DOC-ABA")) == 3
    assert (
        db_session.query(models.StockLedgerEntry)
        .filter_by(recorder_ref="DOC-ABA", active=True)
        .one()
        .id
        == last_id
    )


def test_repeated_empty_revision_is_noop_with_tombstone_boundary(db_session):
    _catalog(db_session)
    generation = _lineage(db_session)
    pull_recorder_movements(
        db_session,
        ASSEMBLY,
        "DOC-EMPTY",
        client=RecorderClient([_line(5)]),
        ledger_generation_id=generation.id,
    )
    db_session.flush()
    old = (
        db_session.query(models.StockLedgerEntry)
        .filter_by(recorder_ref="DOC-EMPTY")
        .one()
    )

    first_empty = pull_recorder_movements(
        db_session,
        ASSEMBLY,
        "DOC-EMPTY",
        client=RecorderClient([]),
        ledger_generation_id=generation.id,
    )
    db_session.flush()
    empty_batch = _recorder_batches(db_session, "DOC-EMPTY")[-1]
    batch_count = len(_recorder_batches(db_session, "DOC-EMPTY"))
    edge = db_session.query(models.StockLedgerFactSupersession).one()

    second_empty = pull_recorder_movements(
        db_session,
        ASSEMBLY,
        "DOC-EMPTY",
        client=RecorderClient([]),
        ledger_generation_id=generation.id,
    )
    db_session.flush()

    assert first_empty.status == "empty"
    assert second_empty.status == "empty" and second_empty.inserted == 0
    assert len(_recorder_batches(db_session, "DOC-EMPTY")) == batch_count == 2
    assert old.active is False
    assert edge.old_sle_id == old.id and edge.new_sle_id is None
    assert visible_sles(
        db_session, physical_import_batch_id=empty_batch.id
    ) == []

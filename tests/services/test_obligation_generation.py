from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.obligation_generation import (
    ALGORITHM_VERSION,
    GENERATION_KIND,
    REPLAY_VERSION,
    ObligationGenerationError,
    fork_obligation_generation,
)


def _accepted_parent(db, *, key="accepted", pointer=True, status="accepted"):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    replay_from = datetime(2026, 7, 1, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key=f"physical:{key}", status="completed", cutoff=cutoff,
        source_watermarks={"rows_read": 1}, completed_at=cutoff,
    )
    parent = models.LedgerGeneration(
        generation_key=key, status=status, cutoff=cutoff,
        source_watermarks={"replay_from": replay_from.isoformat()},
        capabilities={"physical_ledger": True}, physical_import_batch=physical,
        algorithm_version="accepted/1", accepted_at=cutoff,
    )
    db.add(parent)
    db.flush()
    if pointer:
        db.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    db.commit()
    return parent, physical


def _supplier_provenance(db, parent, physical):
    """Create the three immutable evidence outcomes that must survive a fork."""
    item = models.Item(item_code="PROVENANCE-ITEM", item_name="Provenance item")
    db.add(item)
    db.flush()
    rows = []
    for line_no, status, operation_kind, extras in (
        ("1", "exact", "supplier_receipt", {
            "supplier_order_ref": "order-1", "supplier_order_line_no": "4",
            "ambiguity_count": 0, "reason": None,
        }),
        ("2", "ambiguous", "correction", {
            "supplier_order_ref": None, "supplier_order_line_no": None,
            "ambiguity_count": 2, "reason": "two matching order lines",
        }),
        ("3", "unmatched", "supplier_return", {
            "supplier_order_ref": None, "supplier_order_line_no": None,
            "ambiguity_count": 0, "reason": "no supplier order link",
        }),
    ):
        entry = models.StockLedgerEntry(
            ingest_batch_id=physical.id, source_content_hash=(line_no * 64)[:64],
            item_id=item.item_id, qty=Decimal("1"), posting_at=parent.cutoff,
            recorder_type="Document_Receipt", recorder_ref="receipt-1", line_no=line_no,
        )
        db.add(entry)
        db.flush()
        rows.append(models.StockLedgerSupplierReceiptProvenance(
            ledger_generation_id=parent.id,
            stock_ledger_entry_id=entry.id,
            receipt_doc_type="Document_Receipt", receipt_doc_ref="receipt-1",
            receipt_doc_line_no=line_no, operation_kind=operation_kind,
            operation_key=f"operation-{line_no}", operation_name=f"Operation {line_no}",
            correction_receipt_ref="original-1" if line_no == "2" else None,
            evidence_hash=("a" if line_no == "1" else "b" if line_no == "2" else "c") * 64,
            evidence_payload={"line": line_no, "signed_qty": "1"},
            match_rule="exact-document-line", match_status=status, **extras,
        ))
    db.add_all(rows)
    db.commit()
    return rows


def test_fork_reuses_exact_prefix_and_materializes_bins_without_publishing(db_session):
    parent, physical = _accepted_parent(db_session)
    item = models.Item(item_code="FORK-ITEM", item_name="Fork item")
    db_session.add(item)
    db_session.flush()
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=physical.id, source_content_hash="f" * 64,
        item_id=item.item_id, qty=Decimal("7"), posting_at=parent.cutoff,
        recorder_type="Document_Test", recorder_ref="fork-doc", line_no="1",
        active=False,
    ))
    db_session.commit()

    result = fork_obligation_generation(db_session, parent.id, "obligation-1")

    candidate = db_session.get(models.LedgerGeneration, result.ledger_generation_id)
    bins = db_session.query(models.StockBin).filter(
        models.StockBin.ledger_generation_id == candidate.id
    ).all()
    checkpoints = db_session.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == candidate.id
    ).all()
    assert result.created is True
    assert candidate.status == "building"
    assert candidate.physical_import_batch_id == physical.id
    assert candidate.cutoff == parent.cutoff.replace(tzinfo=None)
    assert candidate.source_watermarks == {
        "parent_generation_id": parent.id,
        "generation_kind": GENERATION_KIND,
        "replay_from": parent.source_watermarks["replay_from"],
    }
    assert candidate.capabilities == {}
    assert candidate.algorithm_version == ALGORITHM_VERSION
    assert candidate.replay_version == REPLAY_VERSION
    assert len(bins) == 1 and Decimal(str(bins[0].on_hand)) == Decimal("7")
    assert len(checkpoints) == 1 and checkpoints[0].status == "completed"
    assert checkpoints[0].metrics["physical_import_batch_id"] == physical.id
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id


def test_fork_is_idempotent_only_for_exact_building_candidate(db_session):
    parent, _physical = _accepted_parent(db_session)
    first = fork_obligation_generation(db_session, parent.id, "obligation-idempotent")
    db_session.commit()
    second = fork_obligation_generation(db_session, parent.id, "obligation-idempotent")

    assert first.created is True
    assert second.created is False
    assert second.ledger_generation_id == first.ledger_generation_id

    candidate = db_session.get(models.LedgerGeneration, first.ledger_generation_id)
    candidate.capabilities = {"bad": True}
    db_session.commit()
    with pytest.raises(ObligationGenerationError, match="different"):
        fork_obligation_generation(db_session, parent.id, "obligation-idempotent")


def test_fork_clones_all_supplier_provenance_outcomes_and_checks_exact_retry(db_session):
    parent, physical = _accepted_parent(db_session)
    source_rows = _supplier_provenance(db_session, parent, physical)

    first = fork_obligation_generation(db_session, parent.id, "provenance-copy")
    db_session.commit()
    copied = db_session.query(models.StockLedgerSupplierReceiptProvenance).filter_by(
        ledger_generation_id=first.ledger_generation_id
    ).order_by(models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id).all()

    assert [row.match_status for row in copied] == ["exact", "ambiguous", "unmatched"]
    assert [row.operation_kind for row in copied] == [
        "supplier_receipt", "correction", "supplier_return",
    ]
    assert [row.evidence_hash for row in copied] == [row.evidence_hash for row in source_rows]
    assert [row.evidence_payload for row in copied] == [row.evidence_payload for row in source_rows]
    checkpoint = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=first.ledger_generation_id, stage="physical_import"
    ).one()
    assert checkpoint.metrics["supplier_receipt_provenance"]["count"] == 3
    assert len(checkpoint.metrics["supplier_receipt_provenance"]["checksum"]) == 64

    second = fork_obligation_generation(db_session, parent.id, "provenance-copy")
    assert second.created is False
    assert db_session.query(models.StockLedgerSupplierReceiptProvenance).filter_by(
        ledger_generation_id=first.ledger_generation_id
    ).count() == 3

    copied[0].evidence_hash = "z" * 64
    db_session.commit()
    with pytest.raises(ObligationGenerationError, match="provenance conflicts"):
        fork_obligation_generation(db_session, parent.id, "provenance-copy")


def test_fork_is_removed_by_outer_rollback_and_reused_after_outer_commit(db_session):
    parent, physical = _accepted_parent(db_session)
    _supplier_provenance(db_session, parent, physical)
    rolled_back = fork_obligation_generation(db_session, parent.id, "outer-rollback")
    db_session.rollback()

    assert db_session.get(models.LedgerGeneration, rolled_back.ledger_generation_id) is None
    assert db_session.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == rolled_back.ledger_generation_id
    ).count() == 0
    assert db_session.query(models.StockBin).filter(
        models.StockBin.ledger_generation_id == rolled_back.ledger_generation_id
    ).count() == 0
    assert db_session.query(models.StockLedgerSupplierReceiptProvenance).filter(
        models.StockLedgerSupplierReceiptProvenance.ledger_generation_id
        == rolled_back.ledger_generation_id
    ).count() == 0
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id

    first = fork_obligation_generation(db_session, parent.id, "outer-commit")
    db_session.commit()
    repeated = fork_obligation_generation(db_session, parent.id, "outer-commit")
    assert repeated.created is False
    assert repeated.ledger_generation_id == first.ledger_generation_id


@pytest.mark.parametrize("pointer,status", [(False, "accepted"), (True, "building")])
def test_fork_rejects_noncurrent_or_nonaccepted_parent(db_session, pointer, status):
    parent, _physical = _accepted_parent(
        db_session, key=f"parent-{pointer}-{status}", pointer=pointer, status=status
    )
    with pytest.raises(ObligationGenerationError):
        fork_obligation_generation(db_session, parent.id, "rejected-parent")


def test_fork_rejects_foreign_parent_even_when_current_pointer_is_valid(db_session):
    current, _physical = _accepted_parent(db_session, key="current")
    foreign, _ = _accepted_parent(db_session, key="foreign", pointer=False)
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == current.id
    with pytest.raises(ObligationGenerationError, match="not the current"):
        fork_obligation_generation(db_session, foreign.id, "foreign-child")

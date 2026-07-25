from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.physical_refresh_generation import (
    GENERATION_KIND,
    PhysicalRefreshGenerationError,
    fork_physical_refresh_generation,
)


def _accepted_parent(db, *, key="accepted", pointer=True, status="accepted"):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    replay_from = datetime(2026, 7, 1, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key=f"physical:{key}",
        status="completed",
        cutoff=cutoff,
        source_watermarks={"rows_read": 1},
        completed_at=cutoff,
    )
    parent = models.LedgerGeneration(
        generation_key=key,
        status=status,
        cutoff=cutoff,
        source_watermarks={"replay_from": replay_from.isoformat()},
        capabilities={"physical_ledger": True},
        physical_import_batch=physical,
        algorithm_version="accepted/1",
        accepted_at=cutoff,
    )
    db.add(parent)
    db.flush()
    if pointer:
        db.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    db.commit()
    return parent, physical


def _supplier_provenance(db, parent, physical):
    item = models.Item(item_code="PROVENANCE-ITEM", item_name="Provenance item")
    db.add(item)
    db.flush()
    entry = models.StockLedgerEntry(
        ingest_batch_id=physical.id,
        source_content_hash="a" * 64,
        item_id=item.item_id,
        qty=Decimal("5"),
        posting_at=parent.cutoff,
        recorder_type="Document_Receipt",
        recorder_ref="receipt-1",
        line_no="1",
    )
    db.add(entry)
    db.flush()
    row = models.StockLedgerSupplierReceiptProvenance(
        ledger_generation_id=parent.id,
        stock_ledger_entry_id=entry.id,
        receipt_doc_type="Document_Receipt",
        receipt_doc_ref="receipt-1",
        receipt_doc_line_no="1",
        supplier_order_ref="order-1",
        supplier_order_line_no="4",
        operation_kind="supplier_receipt",
        operation_key="operation-1",
        operation_name="Operation 1",
        correction_receipt_ref=None,
        evidence_hash="z" * 64,
        evidence_payload={"line": "1", "signed_qty": "5"},
        match_rule="exact-document-line",
        match_status="exact",
        ambiguity_count=0,
        reason=None,
    )
    db.add(row)
    db.commit()
    return [row]


def test_create_physical_refresh_generation_reuses_current_accepted_prefix(db_session):
    parent, physical = _accepted_parent(db_session)
    item = models.Item(item_code="REFRESH-ITEM", item_name="Refresh item")
    db_session.add(item)
    db_session.flush()
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=physical.id,
        source_content_hash="f" * 64,
        item_id=item.item_id,
        qty=Decimal("7"),
        posting_at=parent.cutoff,
        recorder_type="Document_Test",
        recorder_ref="refresh-doc",
        line_no="1",
        active=False,
    ))
    db_session.commit()

    result = fork_physical_refresh_generation(
        db_session,
        parent.id,
        "physical-refresh-1",
        from_cutoff=parent.cutoff,
        target_cutoff=parent.cutoff + timedelta(days=1),
    )

    candidate = db_session.get(models.LedgerGeneration, result.ledger_generation_id)
    bins = db_session.query(models.StockBin).filter(
        models.StockBin.ledger_generation_id == candidate.id
    ).all()
    checkpoint = db_session.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == candidate.id,
        models.LedgerBuildBatch.stage == "physical_import",
    ).one()
    assert result.created is True
    assert candidate.status == "building"
    assert candidate.physical_import_batch_id == physical.id
    assert candidate.cutoff == parent.cutoff + timedelta(days=1)
    assert candidate.source_watermarks == {
        "parent_generation_id": parent.id,
        "parent_physical_import_batch_id": parent.physical_import_batch_id,
        "generation_kind": GENERATION_KIND,
        "from_cutoff": parent.cutoff.replace(tzinfo=timezone.utc).isoformat(),
        "replay_from": parent.source_watermarks["replay_from"],
    }
    assert candidate.capabilities == {}
    assert candidate.algorithm_version == "ledger-physical-refresh-generation/1"
    assert candidate.replay_version == "ledger-physical-refresh-replay/1"
    assert len(bins) == 1 and Decimal(str(bins[0].on_hand)) == Decimal("7")
    assert checkpoint.status == "completed"
    assert checkpoint.batch_key == "physical-refresh:physical-refresh-1"
    assert checkpoint.metrics["parent_generation_id"] == parent.id
    assert checkpoint.metrics["parent_physical_import_batch_id"] == physical.id
    assert checkpoint.metrics["from_cutoff"] == parent.cutoff.replace(
        tzinfo=timezone.utc
    ).isoformat()
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id


def test_retry_is_idempotent_for_exact_candidate(db_session):
    parent, _ = _accepted_parent(db_session)
    from_cutoff = parent.cutoff
    target_cutoff = parent.cutoff + timedelta(days=1)

    first = fork_physical_refresh_generation(
        db_session,
        parent.id,
        "physical-refresh-idempotent",
        from_cutoff=from_cutoff,
        target_cutoff=target_cutoff,
    )
    db_session.commit()
    second = fork_physical_refresh_generation(
        db_session,
        parent.id,
        "physical-refresh-idempotent",
        from_cutoff=from_cutoff,
        target_cutoff=target_cutoff,
    )

    assert first.created is True
    assert second.created is False
    assert second.ledger_generation_id == first.ledger_generation_id


def test_retry_allows_completed_import_window_checkpoints(db_session):
    parent, _ = _accepted_parent(db_session)
    target_cutoff = parent.cutoff + timedelta(days=1)
    first = fork_physical_refresh_generation(
        db_session,
        parent.id,
        "physical-refresh-resume",
        from_cutoff=parent.cutoff,
        target_cutoff=target_cutoff,
    )
    next_physical = models.PhysicalImportBatch(
        batch_key="physical-refresh-window",
        status="completed",
        cutoff=target_cutoff,
        source_watermarks={"previous_import_batch_id": first.physical_import_batch_id},
        completed_at=target_cutoff,
    )
    db_session.add(next_physical)
    db_session.flush()
    candidate = db_session.get(models.LedgerGeneration, first.ledger_generation_id)
    candidate.physical_import_batch_id = next_physical.id
    candidate.source_watermarks = {
        **dict(candidate.source_watermarks),
        "historical_import_completed_through": target_cutoff.replace(
            tzinfo=timezone.utc
        ).isoformat(),
        "historical_physical_import_batch_id": next_physical.id,
    }
    db_session.add(models.LedgerBuildBatch(
        ledger_generation_id=first.ledger_generation_id,
        stage="physical_import",
        batch_key="historical-window:resume",
        status="completed",
        algorithm_version="historical-physical-import/1",
        metrics={"physical_import_batch_id": next_physical.id},
    ))
    db_session.commit()

    retry = fork_physical_refresh_generation(
        db_session,
        parent.id,
        "physical-refresh-resume",
        from_cutoff=parent.cutoff,
        target_cutoff=target_cutoff,
    )

    assert retry.created is False
    assert retry.ledger_generation_id == first.ledger_generation_id
    assert retry.physical_import_batch_id == next_physical.id


def test_reuses_supplier_receipt_provenance_exactly(db_session):
    parent, physical = _accepted_parent(db_session)
    source_rows = _supplier_provenance(db_session, parent, physical)
    target_cutoff = parent.cutoff + timedelta(days=1)

    result = fork_physical_refresh_generation(
        db_session,
        parent.id,
        "provenance-copy",
        from_cutoff=parent.cutoff,
        target_cutoff=target_cutoff,
    )
    db_session.commit()

    candidate_id = result.ledger_generation_id
    copied = db_session.query(models.StockLedgerSupplierReceiptProvenance).filter(
        models.StockLedgerSupplierReceiptProvenance.ledger_generation_id == candidate_id
    ).order_by(models.StockLedgerSupplierReceiptProvenance.id).all()
    checkpoint = db_session.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == candidate_id,
        models.LedgerBuildBatch.stage == "physical_import",
    ).one()

    assert len(copied) == len(source_rows) == 1
    assert [row.evidence_hash for row in copied] == [row.evidence_hash for row in source_rows]
    assert [row.evidence_payload for row in copied] == [row.evidence_payload for row in source_rows]
    assert checkpoint.metrics["supplier_receipt_provenance"]["count"] == 1


def test_new_refresh_never_adopts_foreign_completed_terminal(db_session):
    parent, parent_physical = _accepted_parent(db_session)
    foreign = models.PhysicalImportBatch(
        batch_key="foreign-unpublished",
        status="completed",
        cutoff=parent.cutoff,
        source_watermarks={"owner": "other-building-generation"},
        completed_at=parent.cutoff,
    )
    db_session.add(foreign)
    db_session.commit()

    result = fork_physical_refresh_generation(
        db_session,
        parent.id,
        "no-foreign-terminal",
        from_cutoff=parent.cutoff,
        target_cutoff=parent.cutoff + timedelta(days=1),
    )

    candidate = db_session.get(
        models.LedgerGeneration, result.ledger_generation_id
    )
    assert candidate.physical_import_batch_id == parent_physical.id
    assert candidate.physical_import_batch_id != foreign.id


def test_reject_stale_parent_generation(db_session):
    parent, _ = _accepted_parent(db_session, status="stale")
    with pytest.raises(PhysicalRefreshGenerationError, match="must be ACCEPTED"):
        fork_physical_refresh_generation(
            db_session,
            parent.id,
            "stale-parent",
            from_cutoff=parent.cutoff,
            target_cutoff=parent.cutoff + timedelta(days=1),
        )


def test_reject_bad_cutoff_and_noncurrent_parent(db_session):
    parent, _ = _accepted_parent(db_session, key="current")
    with pytest.raises(PhysicalRefreshGenerationError, match="after parent cutoff"):
        fork_physical_refresh_generation(
            db_session,
            parent.id,
            "bad-cutoff",
            from_cutoff=parent.cutoff,
            target_cutoff=parent.cutoff,
        )

    foreign, _ = _accepted_parent(db_session, key="foreign", pointer=False)
    with pytest.raises(PhysicalRefreshGenerationError, match="not the current"):
        fork_physical_refresh_generation(
            db_session,
            foreign.id,
            "foreign-parent",
            from_cutoff=foreign.cutoff,
            target_cutoff=foreign.cutoff + timedelta(days=1),
        )


def test_reject_non_exact_retry_as_conflict(db_session):
    parent, _ = _accepted_parent(db_session)
    from_cutoff = parent.cutoff
    target_cutoff = parent.cutoff + timedelta(days=1)
    first = fork_physical_refresh_generation(
        db_session,
        parent.id,
        "conflict-retry",
        from_cutoff=from_cutoff,
        target_cutoff=target_cutoff,
    )
    db_session.commit()

    candidate = db_session.get(models.LedgerGeneration, first.ledger_generation_id)
    candidate.capabilities = {"bad": True}
    db_session.commit()
    with pytest.raises(PhysicalRefreshGenerationError, match="different or non-BUILDING"):
        fork_physical_refresh_generation(
            db_session,
            parent.id,
            "conflict-retry",
            from_cutoff=from_cutoff,
            target_cutoff=target_cutoff,
        )


def test_reject_if_parent_lineage_changes_between_retries(db_session):
    parent, _ = _accepted_parent(db_session)
    from_cutoff = parent.cutoff
    target_cutoff = parent.cutoff + timedelta(days=1)
    fork_physical_refresh_generation(
        db_session,
        parent.id,
        "immutable-parent",
        from_cutoff=from_cutoff,
        target_cutoff=target_cutoff,
    )
    db_session.commit()

    parent.source_watermarks = {"replay_from": "1970-01-01T00:00:00+00:00"}
    db_session.commit()
    with pytest.raises(PhysicalRefreshGenerationError, match="different or non-BUILDING"):
        fork_physical_refresh_generation(
            db_session,
            parent.id,
            "immutable-parent",
            from_cutoff=from_cutoff,
            target_cutoff=target_cutoff,
        )

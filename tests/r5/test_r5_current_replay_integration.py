"""R5 current-writer integration contract (red before implementation)."""

from datetime import datetime, timezone
from decimal import Decimal
import os

import pytest
import sqlalchemy as sa

from app import models
from app.services.item_ledger.supplier_receipt_allocation import ReceiptFact
from app.services.item_ledger.current_replenishment import (
    CurrentReplenishmentError,
    apply_current_receipt_replay,
)
from app.services.item_ledger.physical_visibility import visible_sles_for_generation
from tests.services.test_current_replenishment_transaction import _reserves, _world


def _receipt_facts(facts):
    return tuple(
        ReceiptFact(
            sle_id=int(row.fact_id),
            posting_at=row.posting_at,
            known_at=row.posting_at,
            signed_qty=Decimal(str(row.qty)),
            item_id=int(row.item_id),
            supplier_order_ref="order-r5",
            supplier_order_line_no="1",
            receipt_ref=f"receipt-{row.fact_id}",
            receipt_line_no="1",
            planning_stock_pool="selected",
        )
        for row in facts
    )


def test_current_receipt_replay_audit_has_reason_basis_and_exactly_once(db_session):
    generation_id, _item_id, reservations, facts = _world(
        db_session, prefix="r5-audit"
    )
    receipts = _receipt_facts(facts[:1])
    first = apply_current_receipt_replay(
        db_session,
        generation_id=generation_id,
        source_key="physical:r5-audit",
        source_revision=1,
        receipt_facts=receipts,
        reserves=_reserves(reservations),
        complete_scope=True,
        history_mode="as_occurred",
    )
    db_session.commit()
    correction_row = models.StockLedgerEntry(
        ingest_batch_id=facts[0].fact_id and db_session.get(
            models.StockLedgerEntry, int(facts[0].fact_id)
        ).ingest_batch_id,
        source_content_hash="r5-correction".ljust(64, "0"),
        business_identity="r5:audit:correction",
        item_id=facts[0].item_id,
        qty=Decimal("-2"),
        qty_after=Decimal("6"),
        posting_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        known_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
        record_type="Expense",
        movement_kind="receipt",
        recorder_type="Doc",
        recorder_ref="r5-correction",
        line_no="1",
        ingest_source="seed",
    )
    db_session.add(correction_row)
    db_session.flush()
    correction = ReceiptFact(
        **{
            **receipts[0].__dict__,
            "sle_id": int(correction_row.id),
            "signed_qty": Decimal("-2"),
            "receipt_ref": "r5-correction",
            "correction_receipt_ref": receipts[0].receipt_ref,
            "posting_at": datetime(2026, 9, 10, tzinfo=timezone.utc),
            "known_at": datetime(2026, 9, 11, tzinfo=timezone.utc),
        }
    )
    second = apply_current_receipt_replay(
        db_session,
        generation_id=generation_id,
        source_key="physical:r5-audit",
        source_revision=2,
        receipt_facts=(*receipts, correction),
        reserves=_reserves(reservations),
        complete_scope=True,
        history_mode="as_occurred",
    )
    db_session.commit()
    audits = db_session.query(models.CurrentReplenishmentAudit).all()
    correction_audits = [row for row in audits if row.source_revision == 2]
    assert second.changed_pairs > 0
    assert audits
    assert {row.reason for row in audits} == {"r5_signed_replay"}
    assert all(sorted(row.basis_fact_ids) == sorted([int(facts[0].fact_id), int(correction_row.id)]) for row in correction_audits), [
        (row.operation, row.sle_id, row.basis_fact_ids) for row in audits
    ]
    audit_count = len(audits)
    retry = apply_current_receipt_replay(
        db_session,
        generation_id=generation_id,
        source_key="physical:r5-audit",
        source_revision=2,
        receipt_facts=(*receipts, correction),
        reserves=_reserves(reservations),
        complete_scope=True,
        history_mode="as_occurred",
    )
    assert retry.idempotent is True
    assert db_session.query(models.CurrentReplenishmentAudit).count() == audit_count
    assert first.changed_pairs > 0


def test_out_of_order_supersession_uses_new_version_for_current_replay(db_session):
    item = models.Item(item_code="R5-SUPERSEDE", item_name="R5 supersession")
    db_session.add(item)
    db_session.flush()
    batch_old = models.PhysicalImportBatch(
        batch_key="r5-super-old", status="completed",
        source_watermarks={}, completed_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )
    batch_new = models.PhysicalImportBatch(
        batch_key="r5-super-new", status="completed",
        source_watermarks={}, completed_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )
    db_session.add_all([batch_old, batch_new])
    db_session.flush()
    old = models.StockLedgerEntry(
        ingest_batch_id=batch_old.id, source_content_hash="r5-old".ljust(64, "0"),
        business_identity="r5:stable:line-1", item_id=item.item_id, qty=Decimal("4"),
        qty_after=Decimal("4"), posting_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        known_at=datetime(2026, 9, 10, tzinfo=timezone.utc), record_type="Receipt",
        movement_kind="receipt", recorder_type="Doc", recorder_ref="line-1", line_no="1",
    )
    new = models.StockLedgerEntry(
        ingest_batch_id=batch_new.id, source_content_hash="r5-new".ljust(64, "0"),
        business_identity="r5:stable:line-1", item_id=item.item_id, qty=Decimal("6"),
        qty_after=Decimal("6"), posting_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        known_at=datetime(2026, 9, 11, tzinfo=timezone.utc), record_type="Receipt",
        movement_kind="receipt", recorder_type="Doc", recorder_ref="line-1", line_no="1",
    )
    db_session.add_all([old, new])
    db_session.flush()
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=old.id, new_sle_id=new.id, import_batch_id=batch_new.id,
    ))
    generation = models.LedgerGeneration(
        generation_key="r5-super-generation", status="accepted",
        cutoff=datetime(2026, 9, 11, tzinfo=timezone.utc), source_watermarks={},
        capabilities={"physical_ledger": True}, physical_import_batch_id=batch_new.id,
        algorithm_version="r5-tests", accepted_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )
    db_session.add(generation)
    db_session.commit()
    rows = visible_sles_for_generation(db_session, generation.id)
    assert [row.id for row in rows] == [new.id]
    assert rows[0].qty == Decimal("6")


@pytest.mark.integration
def test_postgresql_receipt_replay_retry_and_stale_revision_are_atomic():
    dsn = os.getenv("PRODPLAN_R2_TEST_DSN")
    if not dsn:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    from app.r2_local_contract import validate_r2_dsn

    validate_r2_dsn(dsn)
    from sqlalchemy.orm import sessionmaker

    engine = sa.create_engine(dsn, poolclass=sa.pool.NullPool)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    writer = Session()
    try:
        generation_id, _item_id, reservations, facts = _world(
            writer, prefix="r5-pg"
        )
        receipts = _receipt_facts(facts[:1])
        first = apply_current_receipt_replay(
            writer,
            generation_id=generation_id,
            source_key="physical:r5-pg",
            source_revision=1,
            receipt_facts=receipts,
            reserves=_reserves(reservations),
            complete_scope=True,
            history_mode="as_occurred",
        )
        writer.commit()
        retry = apply_current_receipt_replay(
            writer,
            generation_id=generation_id,
            source_key="physical:r5-pg",
            source_revision=1,
            receipt_facts=receipts,
            reserves=_reserves(reservations),
            complete_scope=True,
            history_mode="as_occurred",
        )
        assert retry.idempotent is True
        with pytest.raises(CurrentReplenishmentError, match="stale"):
            apply_current_receipt_replay(
                writer,
                generation_id=generation_id,
                source_key="physical:r5-pg",
                source_revision=0,
                receipt_facts=receipts,
                reserves=_reserves(reservations),
                complete_scope=True,
                history_mode="as_occurred",
            )
        assert writer.query(models.CurrentReplenishmentAudit).count() == first.audit_events
    finally:
        writer.rollback()
        writer.close()
        engine.dispose()

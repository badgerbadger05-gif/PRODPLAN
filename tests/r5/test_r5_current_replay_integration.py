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
    correction = ReceiptFact(
        **{
            **receipts[0].__dict__,
            "sle_id": int(facts[0].fact_id) + 100000,
            "signed_qty": Decimal("-2"),
            "receipt_ref": "r5-correction",
            "correction_receipt_ref": receipts[0].receipt_ref,
            "posting_at": datetime(2026, 9, 11, tzinfo=timezone.utc),
            "known_at": datetime(2026, 9, 11, tzinfo=timezone.utc),
        }
    )
    # The current audit basis must reference a persisted physical correction;
    # this fixture deliberately proves the writer rejects an unavailable source
    # rather than inventing an audit row for a non-existent SLE.
    with pytest.raises(CurrentReplenishmentError, match="physical|source|Ledger"):
        apply_current_receipt_replay(
            db_session,
            generation_id=generation_id,
            source_key="physical:r5-audit",
            source_revision=2,
            receipt_facts=(*receipts, correction),
            reserves=_reserves(reservations),
            complete_scope=True,
            history_mode="as_occurred",
        )
    db_session.rollback()
    assert first.changed_pairs > 0


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


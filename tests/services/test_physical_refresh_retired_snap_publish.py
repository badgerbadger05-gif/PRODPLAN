"""Only canonical, quantity-backed cutoff snap remainders enter bounded publish."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger import physical_refresh_current_publish as publish
from app.services.item_ledger import physical_refresh_orchestrator as refresh
from app.services.item_ledger.r3_contract import business_identity_for_cutoff_balance_adjustment


def _retired_remainder(db, *, old_qty="10", fact_qty="7", later_fact_qty=None):
    cutoff = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    parent_batch = models.PhysicalImportBatch(
        batch_key="retirement-proof-parent", status="completed", cutoff=cutoff,
        source_watermarks={}, completed_at=cutoff,
    )
    fact_batch = models.PhysicalImportBatch(
        batch_key="retirement-proof-fact", status="completed", cutoff=cutoff + timedelta(days=1),
        source_watermarks={}, completed_at=cutoff,
    )
    item = models.Item(item_code="RET-PROOF", item_name="Retirement proof")
    db.add_all((parent_batch, fact_batch, item))
    db.flush()
    old = models.StockLedgerEntry(
        ingest_batch_id=parent_batch.id, source_content_hash="old-snap",
        business_identity=business_identity_for_cutoff_balance_adjustment(
            "old-snap", "0", item_id=item.item_id, characteristic_ref="",
            organization_ref="org", warehouse_ref1c="wh", snap_content_hash="old-hash",
        ),
        item_id=item.item_id, characteristic_ref="", organization_ref="org",
        warehouse_ref1c="wh", qty=Decimal(old_qty), posting_at=cutoff,
        record_type="Receipt", movement_kind="cutoff_balance_adjustment",
        recorder_type="cutoff_balance_adjustment", recorder_ref="old-snap",
        line_no="0", ingest_source="cutoff_balance_adjustment",
    )
    fact = models.StockLedgerEntry(
        ingest_batch_id=fact_batch.id, source_content_hash="real-document",
        business_identity="movement:real-document", item_id=item.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal(fact_qty), posting_at=cutoff - timedelta(days=8),
        record_type="Receipt", movement_kind="receipt",
        recorder_type="Document_ПриходнаяНакладная", recorder_ref="real-document",
        line_no="1", ingest_source="document_pull",
    )
    db.add_all((old, fact))
    facts = [fact]
    if later_fact_qty is not None:
        later = models.StockLedgerEntry(
            ingest_batch_id=fact_batch.id, source_content_hash="later-document",
            business_identity="movement:later-document", item_id=item.item_id,
            characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
            qty=Decimal(later_fact_qty), posting_at=cutoff + timedelta(days=1),
            record_type="Receipt", movement_kind="receipt",
            recorder_type="Document_ПриходнаяНакладная", recorder_ref="later-document",
            line_no="1", ingest_source="document_pull",
        )
        db.add(later)
        facts.append(later)
    db.flush()
    # The maintenance writer reads persisted NUMERIC(15, 3) values; reload to
    # exercise the same decimal representation used in its batch content hash.
    old_id, fact_ids = old.id, [row.id for row in facts]
    db.expire_all()
    old = db.get(models.StockLedgerEntry, old_id)
    facts = [db.get(models.StockLedgerEntry, fact_id) for fact_id in fact_ids]
    fact = facts[0]
    result = refresh.retire_cutoff_snaps_absorbing_facts(
        db, fact_rows=tuple(facts), previous_import_batch_id=fact_batch.id,
        reason="test accepted document",
    )
    assert result is not None and result.retired_rows == result.reissued_rows == 1
    edge = db.query(models.StockLedgerFactSupersession).filter_by(
        import_batch_id=result.import_batch_id,
    ).one()
    replacement = db.get(models.StockLedgerEntry, edge.new_sle_id)
    assert replacement is not None and Decimal(replacement.qty) == (
        Decimal(old_qty) - Decimal(fact_qty) - Decimal(later_fact_qty or 0)
    )
    return cutoff, parent_batch, fact_batch, old, fact, edge, replacement


def _validate(db, case):
    cutoff, parent_batch, _, _, _, _, replacement = case
    publish._assert_supported_delta(
        (replacement,), db=db,
        parent_batch_id=parent_batch.id,
        target_batch_id=replacement.ingest_batch_id,
        parent_cutoff=cutoff, target_cutoff=cutoff + timedelta(days=1),
        backdate_from=cutoff - timedelta(days=8),
    )


def test_canonical_retired_cutoff_remainder_is_supported(db_session):
    case = _retired_remainder(db_session)
    _validate(db_session, case)


def test_retirement_uses_batch_net_even_when_a_fact_posts_after_the_old_snap(db_session):
    case = _retired_remainder(
        db_session, old_qty="12", fact_qty="5", later_fact_qty="5",
    )
    # The writer consumes ten units of the old +12 snap and reissues +2.
    # Only five source units predate the old snap; the canonical rule uses the
    # whole imported batch and its earliest date, not quantity as of snap date.
    assert Decimal(case[-1].qty) == Decimal("2")
    _validate(db_session, case)


def test_second_canonical_retirement_can_consume_first_remainder(db_session):
    cutoff, parent_batch, _, _, _, _, first = _retired_remainder(db_session)
    second_batch = models.PhysicalImportBatch(
        batch_key="retirement-proof-second-fact", status="completed", cutoff=cutoff,
        source_watermarks={}, completed_at=cutoff,
    )
    db_session.add(second_batch)
    db_session.flush()
    second_fact = models.StockLedgerEntry(
        ingest_batch_id=second_batch.id, source_content_hash="second-document",
        business_identity="movement:second-document", item_id=first.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("2"), posting_at=cutoff - timedelta(days=7),
        record_type="Receipt", movement_kind="receipt",
        recorder_type="Document_ПриходнаяНакладная", recorder_ref="second-document",
        line_no="1", ingest_source="document_pull",
    )
    db_session.add(second_fact)
    db_session.flush()
    db_session.expire_all()
    second_fact = db_session.get(models.StockLedgerEntry, second_fact.id)
    result = refresh.retire_cutoff_snaps_absorbing_facts(
        db_session, fact_rows=(second_fact,), previous_import_batch_id=second_batch.id,
        reason="second accepted document",
    )
    assert result is not None and result.reissued_rows == 1
    edge = db_session.query(models.StockLedgerFactSupersession).filter_by(
        import_batch_id=result.import_batch_id,
    ).one()
    second = db_session.get(models.StockLedgerEntry, edge.new_sle_id)
    assert edge.old_sle_id == first.id and Decimal(second.qty) == Decimal("1")
    assert first.active is False
    publish._assert_supported_delta(
        (second,), db=db_session,
        parent_batch_id=parent_batch.id, target_batch_id=second.ingest_batch_id,
        parent_cutoff=cutoff, target_cutoff=cutoff + timedelta(days=1),
        backdate_from=cutoff - timedelta(days=8),
    )


@pytest.mark.parametrize("forgery", [
    "quantity", "warehouse", "date", "identity", "foreign_batch", "missing_edge", "missing_fact",
])
def test_retired_cutoff_remainder_rejects_incomplete_or_forged_proof(db_session, forgery):
    case = _retired_remainder(db_session)
    cutoff, _, _, _, fact, edge, replacement = case
    if forgery == "quantity":
        replacement.qty = Decimal("4")
    elif forgery == "warehouse":
        replacement.warehouse_ref1c = "foreign"
    elif forgery == "date":
        replacement.posting_at = cutoff - timedelta(days=1)
    elif forgery == "identity":
        replacement.business_identity = "movement:forged"
    elif forgery == "foreign_batch":
        batch = db_session.get(models.PhysicalImportBatch, replacement.ingest_batch_id)
        batch.source_watermarks = {**batch.source_watermarks, "operation": "foreign"}
    elif forgery == "missing_edge":
        db_session.delete(edge)
    else:
        db_session.delete(fact)
    db_session.flush()
    with pytest.raises(publish.ForwardPhysicalRefreshUnavailable, match="canonical retirement proof"):
        _validate(db_session, case)

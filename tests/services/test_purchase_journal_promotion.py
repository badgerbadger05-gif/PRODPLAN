"""Promotion of the purchase-journal candidate into readable truth.

Readers accept only `truth_status='accepted'`, and the journal is always written
as a candidate, so a publish path that forgets to promote leaves the purchases
screen dark while every other signal says the generation is healthy.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import models
from app.services.item_ledger.physical_refresh_orchestrator import (
    PhysicalRefreshOrchestratorError,
    _publish_refresh_read_snapshots,
)
from app.services.purchase_control_snapshot import (
    CONSUMER,
    SNAPSHOT_KEY,
    PurchaseJournalPromotionError,
    promote_candidate_snapshot,
)

CUTOFF = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)


def _buy_row(key="buy:1"):
    """A row the journal's own contract accepts: realized + covered + to_order
    must add up to required, and lineage ids must be present."""
    return {
        "row_generator": "mrp_reservation",
        "row_key": key,
        "required_qty": 12.0,
        "realized_qty": 4.0,
        "received_qty": 4.0,
        "open_order_covered_qty": 2.0,
        "to_order_qty": 6.0,
        "quantity": 12.0,
        "remaining_qty": 6.0,
        "reservation_ids": [1],
        "requirement_ids": [1],
    }


def _generation(db_session, *, capabilities=None):
    batch = models.PhysicalImportBatch(
        batch_key=f"boundary-{db_session.query(models.PhysicalImportBatch).count()}",
        status="completed",
        cutoff=CUTOFF,
        source_watermarks={},
        completed_at=CUTOFF,
    )
    db_session.add(batch)
    db_session.flush()
    generation = models.LedgerGeneration(
        physical_import_batch_id=int(batch.id),
        generation_key="accepted-refresh",
        status="accepted",
        cutoff=CUTOFF,
        source_watermarks={"generation_kind": "physical_refresh"},
        capabilities=capabilities if capabilities is not None else {},
        algorithm_version="ledger-physical-refresh-generation/1",
        accepted_at=CUTOFF,
    )
    db_session.add(generation)
    db_session.flush()
    return generation


def _candidate(db_session, generation, *, meta=None, rows=None, cards=None):
    snapshot = models.PlanningReadSnapshot(
        consumer=CONSUMER,
        snapshot_key=SNAPSHOT_KEY,
        ledger_generation_id=int(generation.id),
        cutoff=generation.cutoff,
        truth_status="building",
        reason="unpublished Ledger-native purchase journal",
        payload={
            "meta": meta if meta is not None else {
                "read_only": True,
                "fact_source": "ledger",
                "ledger_generation_id": int(generation.id),
            },
            "rows": rows if rows is not None else [_buy_row()],
            "cards": cards if cards is not None else {},
        },
        published_at=CUTOFF,
    )
    db_session.add(snapshot)
    db_session.flush()
    return snapshot


def test_valid_candidate_becomes_accepted_truth(db_session):
    generation = _generation(db_session)
    candidate = _candidate(db_session, generation)

    promoted = promote_candidate_snapshot(
        db_session, generation=generation, accepted_at=CUTOFF
    )

    assert promoted is candidate
    assert candidate.truth_status == "accepted"
    assert candidate.reason is None
    assert candidate.published_at == CUTOFF


def test_generation_without_a_candidate_promotes_nothing(db_session):
    generation = _generation(db_session)
    assert promote_candidate_snapshot(
        db_session, generation=generation, accepted_at=CUTOFF
    ) is None


def test_candidate_of_another_generation_is_not_promoted(db_session):
    generation = _generation(db_session)
    other_batch = models.PhysicalImportBatch(
        batch_key="boundary-other", status="completed", cutoff=CUTOFF,
        source_watermarks={}, completed_at=CUTOFF)
    db_session.add(other_batch)
    db_session.flush()
    other = models.LedgerGeneration(
        physical_import_batch_id=int(other_batch.id),
        generation_key="somebody-else",
        status="building",
        cutoff=CUTOFF + timedelta(days=1),
        source_watermarks={},
        algorithm_version="x/1",
    )
    db_session.add(other)
    db_session.flush()
    foreign = _candidate(db_session, other)

    assert promote_candidate_snapshot(
        db_session, generation=generation, accepted_at=CUTOFF
    ) is None
    assert foreign.truth_status == "building"


@pytest.mark.parametrize(
    "meta_override",
    [
        {"read_only": False, "fact_source": "ledger"},
        {"read_only": True, "fact_source": "legacy"},
    ],
    ids=["not-read-only", "not-ledger-sourced"],
)
def test_candidate_that_is_not_ledger_native_is_refused(db_session, meta_override):
    generation = _generation(db_session)
    meta = {"ledger_generation_id": 0, **meta_override}
    meta["ledger_generation_id"] = int(generation.id)
    candidate = _candidate(db_session, generation, meta=meta)

    with pytest.raises(PurchaseJournalPromotionError, match="missing or stale"):
        promote_candidate_snapshot(
            db_session, generation=generation, accepted_at=CUTOFF
        )
    assert candidate.truth_status == "building"


def test_candidate_with_inconsistent_quantities_is_refused(db_session):
    generation = _generation(db_session)
    broken = _buy_row()
    broken["quantity"] = 99.0  # no longer equal to required_qty
    candidate = _candidate(db_session, generation, rows=[broken])

    with pytest.raises(PurchaseJournalPromotionError, match="fact contract"):
        promote_candidate_snapshot(
            db_session, generation=generation, accepted_at=CUTOFF
        )
    assert candidate.truth_status == "building"


def test_duplicate_row_keys_are_refused(db_session):
    generation = _generation(db_session)
    candidate = _candidate(db_session, generation, rows=[_buy_row(), _buy_row()])

    with pytest.raises(PurchaseJournalPromotionError, match="fact contract"):
        promote_candidate_snapshot(
            db_session, generation=generation, accepted_at=CUTOFF
        )
    assert candidate.truth_status == "building"


def test_refresh_publishes_the_journal_of_the_generation_it_accepted(db_session):
    generation = _generation(
        db_session, capabilities={"purchase_control_journal": True}
    )
    candidate = _candidate(db_session, generation)

    _publish_refresh_read_snapshots(
        db_session, generation=generation, fixed_run_ids=()
    )

    assert candidate.truth_status == "accepted"


def test_refresh_fails_closed_when_a_claimed_journal_is_absent(db_session):
    """A missing snapshot reads to the operator as an outage of the whole screen."""
    generation = _generation(
        db_session, capabilities={"purchase_control_journal": True}
    )

    with pytest.raises(
        PhysicalRefreshOrchestratorError,
        match="claims the purchase_control_journal capability",
    ):
        _publish_refresh_read_snapshots(
            db_session, generation=generation, fixed_run_ids=()
        )


def test_refresh_tolerates_a_generation_that_claims_no_journal(db_session):
    generation = _generation(db_session, capabilities={})
    _publish_refresh_read_snapshots(
        db_session, generation=generation, fixed_run_ids=()
    )

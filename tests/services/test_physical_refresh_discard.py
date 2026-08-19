"""Contract tests for discarding an unpublishable physical-refresh candidate."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.ingest import EMPTY_GUID, pull_recorder_movements
from app.services.item_ledger.physical_refresh_discard import (
    PhysicalRefreshDiscardError,
    discard_physical_refresh_candidate,
    restore_active_invariant,
)
from app.services.item_ledger.physical_visibility import visible_sle_query

CUTOFF = datetime(2026, 7, 26, 12, tzinfo=timezone.utc)


def _batch(db_session, key, **kw):
    batch = models.PhysicalImportBatch(
        batch_key=key,
        status="completed",
        cutoff=CUTOFF,
        source_watermarks=kw.pop("marks", {"origin": "test"}),
        completed_at=CUTOFF,
        **kw,
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def _entry(
    db_session,
    batch,
    *,
    recorder_ref,
    qty,
    item_id,
    line_no="1",
    active=True,
    content_hash="h" * 64,
):
    row = models.StockLedgerEntry(
        ingest_batch_id=int(batch.id),
        source_content_hash=content_hash,
        item_id=item_id,
        characteristic_ref="",
        organization_ref="ORG",
        warehouse_ref1c="WH",
        qty=Decimal(qty),
        posting_at=CUTOFF - timedelta(days=1),
        record_type="Receipt",
        recorder_type="Document_ПеремещениеЗапасов",
        recorder_ref=recorder_ref,
        line_no=line_no,
        ingest_source="document_pull",
        active=active,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _world(db_session):
    """Accepted parent at batch 1, plus a failed candidate that re-pulled a recorder.

    The candidate superseded the parent's revision of `doc-a`, exactly as a real
    recorder audit would.
    """
    item = models.Item(item_code="ITEM-1", item_name="Item")
    db_session.add(item)
    db_session.flush()

    parent_batch = _batch(db_session, "accepted-boundary")
    kept = _entry(db_session, parent_batch, recorder_ref="doc-a", qty="10", item_id=item.item_id)
    _entry(db_session, parent_batch, recorder_ref="doc-b", qty="5", item_id=item.item_id)
    parent = models.LedgerGeneration(
        generation_key="accepted-parent",
        status="accepted",
        cutoff=CUTOFF,
        source_watermarks={"replay_from": CUTOFF.isoformat()},
        physical_import_batch=parent_batch,
        algorithm_version="accepted/1",
        accepted_at=CUTOFF,
    )
    db_session.add(parent)
    db_session.flush()
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))

    candidate_batch = _batch(db_session, "candidate-repull")
    replacement = _entry(
        db_session, candidate_batch, recorder_ref="doc-a", qty="12", item_id=item.item_id
    )
    kept.active = False
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=kept.id,
        new_sle_id=replacement.id,
        import_batch_id=int(candidate_batch.id),
    ))
    candidate = models.LedgerGeneration(
        generation_key="failed-candidate",
        status="building",
        cutoff=CUTOFF + timedelta(days=1),
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": int(parent.id),
        },
        physical_import_batch_id=int(candidate_batch.id),
        algorithm_version="ledger-physical-refresh-generation/1",
    )
    db_session.add(candidate)
    db_session.flush()
    db_session.add(models.StockBin(
        ledger_generation_id=int(candidate.id),
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="ORG",
        warehouse_ref1c="WH",
        on_hand=Decimal("12"),
    ))
    db_session.add(models.LedgerBuildBatch(
        ledger_generation_id=int(candidate.id),
        stage="physical_import",
        batch_key="physical-refresh-recorder-audit:candidate:2",
        status="completed",
        algorithm_version="ledger-physical-refresh-import/2",
        metrics={},
    ))
    db_session.commit()
    return parent, candidate, kept, item


def test_discard_returns_the_sequence_to_the_parent_boundary(db_session):
    parent, candidate, kept, _item = _world(db_session)
    before = visible_sle_query(
        db_session, physical_import_batch_id=int(parent.physical_import_batch_id)).all()

    result = discard_physical_refresh_candidate(
        db_session,
        ledger_generation_id=int(candidate.id),
        reason="convergence failed and the candidate blocks the next refresh",
    )
    db_session.commit()

    assert result.boundary_after == int(parent.physical_import_batch_id)
    assert result.deleted_physical_batches == 1
    assert result.deleted_ledger_entries == 1
    assert result.deleted_supersessions == 1
    assert result.deleted_generation_rows == {"stock_bin": 1, "ledger_build_batch": 1}
    assert candidate.status == "rejected"
    assert candidate.physical_import_batch_id == parent.physical_import_batch_id
    assert "convergence failed" in candidate.source_watermarks["rejected_reason"]
    assert candidate.source_watermarks["rejected_physical_import_batch_id"] == result.boundary_before

    after = visible_sle_query(
        db_session, physical_import_batch_id=int(parent.physical_import_batch_id)).all()
    assert [(r.id, r.qty) for r in after] == [(r.id, r.qty) for r in before]
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id


def test_discard_reactivates_rows_whose_retiring_edge_it_removed(db_session):
    """The trap: `active` is ignored when reading but drives supersession on write.

    Left inactive, `doc-a`'s parent revision would be invisible to the next
    re-pull, which would then insert a second live revision beside it.
    """
    _parent, candidate, kept, _item = _world(db_session)
    assert kept.active is False

    result = discard_physical_refresh_candidate(
        db_session,
        ledger_generation_id=int(candidate.id),
        reason="rollback",
    )
    db_session.commit()

    db_session.refresh(kept)
    assert kept.active is True
    assert result.reactivated_entries == 1


def test_restore_active_invariant_fixes_both_directions(db_session):
    _parent, candidate, kept, item = _world(db_session)
    # A row that is live but wrongly flagged inactive, and one already retired
    # yet still flagged active.
    stray = _entry(
        db_session,
        db_session.get(models.PhysicalImportBatch, int(candidate.physical_import_batch_id)),
        recorder_ref="doc-c", qty="3", item_id=item.item_id, active=False,
    )
    kept.active = True  # retired by an edge, so this is wrong
    db_session.flush()

    changed = restore_active_invariant(db_session)
    db_session.flush()

    db_session.refresh(kept)
    db_session.refresh(stray)
    assert changed == 2
    assert kept.active is False
    assert stray.active is True


class _RecorderClient:
    """Returns one register line for the audited recorder."""

    def __init__(self, qty):
        self.qty = qty

    def get_all(self, entity_name, filter_query=None, **kwargs):
        if entity_name != "AccumulationRegister_ЗапасыНаСкладах":
            return []
        return [{"RecordSet": [{
            "Period": "2026-07-25T10:00:00",
            "LineNumber": "1",
            "Active": True,
            "RecordType": "Receipt",
            "Организация_Key": "ORG",
            "Номенклатура_Key": "ITEM-REF",
            "Характеристика_Key": EMPTY_GUID,
            "СтруктурнаяЕдиница_Key": "WH",
            "Количество": self.qty,
        }]}]


def test_repull_after_discard_supersedes_instead_of_duplicating(db_session):
    """The failure this whole module exists to prevent.

    Before the active flag was restored on rollback, the next pull saw no active
    revision of `doc-a`, inserted a new one, and left the old one live too — the
    ledger then counted the recorder twice.
    """
    parent, candidate, kept, item = _world(db_session)
    item.item_ref1c = "ITEM-REF"
    db_session.add(models.StockWarehouse(warehouse_ref1c="WH", warehouse_name="WH"))
    db_session.flush()

    discard_physical_refresh_candidate(
        db_session, ledger_generation_id=int(candidate.id), reason="rollback"
    )
    db_session.commit()

    pull_recorder_movements(
        db_session,
        "Document_ПеремещениеЗапасов",
        "doc-a",
        client=_RecorderClient(21),
        source="test-repull",
    )
    db_session.commit()

    live = db_session.query(models.StockLedgerEntry).filter(
        models.StockLedgerEntry.recorder_ref == "doc-a",
        models.StockLedgerEntry.active.is_(True),
    ).all()
    assert [row.qty for row in live] == [Decimal("21")], (
        "the re-pull must retire the restored revision, not stack a second one"
    )
    db_session.refresh(kept)
    assert kept.active is False
    assert db_session.query(models.StockLedgerFactSupersession).filter(
        models.StockLedgerFactSupersession.old_sle_id == kept.id
    ).count() == 1


def test_accepted_generation_is_never_discardable(db_session):
    parent, _candidate, _kept, _item = _world(db_session)
    with pytest.raises(PhysicalRefreshDiscardError, match="ACCEPTED"):
        discard_physical_refresh_candidate(
            db_session, ledger_generation_id=int(parent.id), reason="no"
        )


def test_current_planning_truth_pointer_is_never_discardable(db_session):
    parent, candidate, _kept, _item = _world(db_session)
    db_session.get(models.PlanningTruthState, 1).current_generation_id = candidate.id
    candidate.status = "building"
    db_session.flush()
    with pytest.raises(PhysicalRefreshDiscardError, match="planning truth pointer"):
        discard_physical_refresh_candidate(
            db_session, ledger_generation_id=int(candidate.id), reason="no"
        )
    assert parent.status == "accepted"


def test_refuses_while_another_generation_sits_above_the_boundary(db_session):
    parent, candidate, _kept, _item = _world(db_session)
    later_batch = _batch(db_session, "even-later")
    db_session.add(models.LedgerGeneration(
        generation_key="stacked-candidate",
        status="building",
        cutoff=CUTOFF + timedelta(days=2),
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": int(parent.id),
        },
        physical_import_batch_id=int(later_batch.id),
        algorithm_version="ledger-physical-refresh-generation/1",
    ))
    db_session.flush()

    with pytest.raises(PhysicalRefreshDiscardError, match="discard them first"):
        discard_physical_refresh_candidate(
            db_session, ledger_generation_id=int(candidate.id), reason="no"
        )


def test_reason_is_mandatory(db_session):
    _parent, candidate, _kept, _item = _world(db_session)
    with pytest.raises(ValueError, match="reason is required"):
        discard_physical_refresh_candidate(
            db_session, ledger_generation_id=int(candidate.id), reason="   "
        )


def test_candidate_without_parent_lineage_is_refused(db_session):
    _parent, candidate, _kept, _item = _world(db_session)
    candidate.source_watermarks = {"generation_kind": "physical_refresh"}
    db_session.flush()
    with pytest.raises(PhysicalRefreshDiscardError, match="no parent lineage"):
        discard_physical_refresh_candidate(
            db_session, ledger_generation_id=int(candidate.id), reason="no"
        )


def test_damage_below_the_boundary_does_not_veto_the_rollback(db_session):
    """Two lines broken weeks earlier must not fence the physical contour.

    The shadow stand carried exactly this: a recorder line with two live
    revisions from an old incident, far below the accepted boundary.  Judged as
    an absolute post-condition it made every rollback unprovable, so the stuck
    candidate kept the physical terminal, the Ledger cutoff stopped moving and a
    day later every consumer failed closed on the freshness threshold.
    """
    parent, candidate, _kept, item = _world(db_session)
    parent_batch = db_session.get(
        models.PhysicalImportBatch, int(parent.physical_import_batch_id)
    )
    # Pre-existing damage: one recorder line with two unsuperseded revisions,
    # both below the boundary this rollback returns to.
    _entry(
        db_session,
        parent_batch,
        recorder_ref="doc-b",
        qty="5",
        item_id=item.item_id,
        content_hash="d" * 64,
    )
    db_session.commit()

    result = discard_physical_refresh_candidate(
        db_session,
        ledger_generation_id=int(candidate.id),
        reason="rollback under pre-existing damage",
    )
    db_session.commit()

    assert result.boundary_after == int(parent.physical_import_batch_id)
    # Reported, never hidden — and never repaired by a rollback either.
    assert result.preexisting_live_revision_conflicts == 1
    assert (
        db_session.get(models.LedgerGeneration, int(candidate.id)).status
        == "rejected"
    )


def test_rollback_that_would_add_a_live_revision_is_still_refused(db_session):
    """The differential rule still fails closed on damage the rollback causes."""
    parent, candidate, kept, item = _world(db_session)
    candidate_batch = db_session.get(
        models.PhysicalImportBatch, int(candidate.physical_import_batch_id)
    )
    # A second parent-side revision of `doc-a` that only the candidate's edge
    # retires: deleting that edge leaves two live revisions of one line.
    second = _entry(
        db_session,
        db_session.get(
            models.PhysicalImportBatch, int(parent.physical_import_batch_id)
        ),
        recorder_ref="doc-a",
        qty="7",
        item_id=item.item_id,
        active=False,
        content_hash="e" * 64,
    )
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=int(second.id),
        new_sle_id=int(kept.id),
        import_batch_id=int(candidate_batch.id),
    ))
    db_session.commit()

    with pytest.raises(PhysicalRefreshDiscardError) as exc:
        discard_physical_refresh_candidate(
            db_session,
            ledger_generation_id=int(candidate.id),
            reason="rollback that would double-count doc-a",
        )
    db_session.rollback()

    assert "more than one live revision" in str(exc.value)
    assert "0 before the rollback" in str(exc.value)


def test_discard_takes_the_candidate_custody_events_with_it(db_session):
    """A custody event is derived from a fact — it cannot outlive it.

    Left behind, the event names an SLE row the rollback deleted: the fold
    cannot see it, so the issue keeps an open transit reservation nothing ever
    consumes, while the projector refuses to re-append the pair on the next
    import because it still recognises the orphan by its stable identity.  On
    the shadow stand that combination made every launch fail with a negative
    workshop reservation.
    """
    _parent, candidate, _kept, item = _world(db_session)
    candidate_entry = (
        db_session.query(models.StockLedgerEntry)
        .filter(
            models.StockLedgerEntry.ingest_batch_id
            == int(candidate.physical_import_batch_id)
        )
        .one()
    )
    db_session.add(models.ProductionMaterialCustodyEvent(
        issue_id=4242,
        product_id=99,
        component_item_id=int(item.item_id),
        source_kind="transfer_posted",
        source_sle_id=int(candidate_entry.id),
        effective_at=CUTOFF,
        location_kind="transit",
        warehouse_ref1c="WH",
        delta_qty=Decimal("-7"),
        idempotency_key="custody-event:rolled-back-fact",
    ))
    db_session.commit()

    result = discard_physical_refresh_candidate(
        db_session,
        ledger_generation_id=int(candidate.id),
        reason="rollback drops the facts, so it drops what was derived from them",
    )
    db_session.commit()

    assert result.deleted_custody_events == 1
    assert db_session.query(models.ProductionMaterialCustodyEvent).count() == 0

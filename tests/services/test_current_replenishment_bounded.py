from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.current_replenishment import (
    CurrentReplenishmentError,
    apply_current_replenishment_for_bounded_make_scopes,
)


def _world(db):
    parent_batch = models.PhysicalImportBatch(
        batch_key="bounded-make-parent-batch",
        status="completed",
        cutoff=datetime(2026, 9, 10, tzinfo=timezone.utc),
        source_watermarks={},
        completed_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )
    target_batch = models.PhysicalImportBatch(
        batch_key="bounded-make-target-batch",
        status="completed",
        cutoff=datetime(2026, 9, 11, tzinfo=timezone.utc),
        source_watermarks={},
        completed_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )
    db.add_all([parent_batch, target_batch])
    db.flush()
    parent = models.LedgerGeneration(
        generation_key="bounded-make-parent-generation",
        status="accepted",
        cutoff=parent_batch.cutoff,
        accepted_at=parent_batch.cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=parent_batch,
        algorithm_version="bounded-tests",
    )
    target = models.LedgerGeneration(
        generation_key="bounded-make-target-generation",
        status="building",
        cutoff=target_batch.cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=target_batch,
        algorithm_version="bounded-tests",
    )
    db.add_all([parent, target])
    items = [
        models.Item(item_code="BOUNDED-MAKE-1", item_name="make one"),
        models.Item(item_code="BOUNDED-MAKE-2", item_name="make two"),
    ]
    db.add_all(items)
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    owners = {}
    for item in items:
        run = models.PlanningRun(
            status="BUILDING_SNAPSHOT",
            config_snapshot={},
            ledger_generation_id=parent.id,
            ledger_cutoff=parent.cutoff,
            source_plan_id=None,
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 30),
            active_freeze_version=1,
        )
        db.add(run)
        db.flush()
        requirement = models.MrpRequirement(
            run_id=run.run_id,
            item_id=item.item_id,
            total_required_qty=Decimal("10"),
            net_required_qty=Decimal("10"),
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 30),
            bom_level=0,
            planning_stock_pool="selected",
            characteristic_ref="",
            organization_ref="",
            freeze_version=1,
        )
        db.add(requirement)
        db.flush()
        owner = models.ReservationEntry(
            ledger_generation_id=parent.id,
            item_id=item.item_id,
            run_id=run.run_id,
            freeze_version=1,
            requirement_id=requirement.id,
            priority_period_from=date(2026, 9, 1),
            priority_period_to=date(2026, 9, 30),
            realization_mode="make",
            planning_stock_pool="selected",
            reserved_qty=Decimal("10"),
            replenishment_required_qty=Decimal("10"),
            lifecycle_status="active",
            current_identity=f"reservation:req:{requirement.id}:mode:make",
            owner_kind="current",
            is_current=True,
        )
        db.add(owner)
        db.flush()
        owners[item.item_id] = owner

    facts = {}
    for item, quantity, ref in zip(items, ("3", "4"), ("M1", "M2")):
        row = models.StockLedgerEntry(
            ingest_batch_id=target_batch.id,
            source_content_hash=(f"bounded-{ref}").ljust(64, "0"),
            business_identity=f"bounded:{ref}",
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c="WH-MAKE",
            qty=Decimal(quantity),
            qty_after=Decimal(quantity),
            posting_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
            known_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
            record_type="Receipt",
            movement_kind="assembly_in",
            recorder_type="Assembly",
            recorder_ref=ref,
            line_no="1",
            ingest_source="pull",
        )
        db.add(row)
        db.flush()
        facts[item.item_id] = row
    db.commit()
    return parent, target, items, owners, facts


def _scope(item_id):
    return (int(item_id), "", "", "selected", "make")


def test_bounded_make_reads_only_affected_item_and_keeps_current_owner_ids(
    db_session, monkeypatch
):
    parent, target, items, owners, facts = _world(db_session)

    from app.services.item_ledger import physical_visibility

    def explode(*_args, **_kwargs):
        raise AssertionError("full generation visibility is forbidden")

    monkeypatch.setattr(physical_visibility, "visible_sles_for_generation", explode)
    result = apply_current_replenishment_for_bounded_make_scopes(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        target_cutoff=target.cutoff,
        affected_scopes=(_scope(items[0].item_id),),
        source_revision=1,
    )
    db_session.commit()

    assert result.affected_scopes == (_scope(items[0].item_id),)
    assert result.scope_history_rows == 1
    assert result.results[0].inserted == 1
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id
    allocation = db_session.query(models.ReservationConsumptionAllocation).one()
    assert allocation.reservation_id == owners[items[0].item_id].id
    assert allocation.sle_id == facts[items[0].item_id].id
    assert db_session.query(models.ReservationEntry).count() == 2
    assert db_session.query(models.ReservationEvent).count() == 0
    assert all(
        row.ledger_generation_id == parent.id
        for row in db_session.query(models.ReservationEntry).all()
    )
    assert db_session.query(models.ReservationConsumptionAllocation).filter(
        models.ReservationConsumptionAllocation.item_id == items[1].item_id
    ).count() == 0


def test_bounded_make_exact_retry_is_zero_audit_and_stable(db_session):
    parent, target, items, owners, _facts = _world(db_session)
    kwargs = dict(
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        target_cutoff=target.cutoff,
        affected_scopes=(_scope(items[0].item_id),),
        source_revision=7,
    )
    first = apply_current_replenishment_for_bounded_make_scopes(db_session, **kwargs)
    db_session.commit()
    allocation_id = db_session.query(models.ReservationConsumptionAllocation.id).scalar()
    audit_count = db_session.query(models.CurrentReplenishmentAudit).count()

    second = apply_current_replenishment_for_bounded_make_scopes(db_session, **kwargs)
    db_session.commit()

    assert first.audit_events == 1
    assert second.audit_events == 0
    assert second.results[0].idempotent is True
    assert db_session.query(models.ReservationConsumptionAllocation.id).scalar() == allocation_id
    assert db_session.query(models.CurrentReplenishmentAudit).count() == audit_count


def test_bounded_make_correction_reallocates_only_affected_scope(db_session):
    parent, target, items, owners, facts = _world(db_session)
    both = (_scope(items[0].item_id), _scope(items[1].item_id))
    apply_current_replenishment_for_bounded_make_scopes(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        target_cutoff=target.cutoff,
        affected_scopes=both,
        source_revision=1,
    )
    db_session.flush()
    item2_before = db_session.query(models.ReservationConsumptionAllocation).filter_by(
        item_id=items[1].item_id
    ).one()
    target.status = "accepted"
    target.accepted_at = target.cutoff
    replacement_batch = models.PhysicalImportBatch(
        batch_key="bounded-make-replacement-batch",
        status="completed",
        cutoff=datetime(2026, 9, 12, tzinfo=timezone.utc),
        source_watermarks={},
        completed_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
    )
    db_session.add(replacement_batch)
    db_session.flush()
    replacement = models.StockLedgerEntry(
        ingest_batch_id=replacement_batch.id,
        source_content_hash="bounded-replacement".ljust(64, "0"),
        business_identity="bounded:M1:replacement",
        item_id=items[0].item_id,
        qty=Decimal("8"),
        qty_after=Decimal("8"),
        posting_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
        known_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        record_type="Receipt",
        movement_kind="assembly_in",
        recorder_type="Assembly",
        recorder_ref="M1-replacement",
        line_no="1",
        ingest_source="pull",
    )
    db_session.add(replacement)
    db_session.flush()
    db_session.add(
        models.StockLedgerFactSupersession(
            old_sle_id=facts[items[0].item_id].id,
            new_sle_id=replacement.id,
            import_batch_id=replacement_batch.id,
        )
    )
    correction = models.LedgerGeneration(
        generation_key="bounded-make-correction-generation",
        status="building",
        cutoff=replacement_batch.cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=replacement_batch,
        algorithm_version="bounded-tests",
    )
    db_session.add(correction)
    db_session.flush()

    result = apply_current_replenishment_for_bounded_make_scopes(
        db_session,
        target_generation_id=correction.id,
        parent_generation_id=target.id,
        target_cutoff=correction.cutoff,
        affected_scopes=(_scope(items[0].item_id),),
        source_revision=2,
    )
    db_session.commit()

    assert result.scope_history_rows == 1
    item1_after = db_session.query(models.ReservationConsumptionAllocation).filter_by(
        item_id=items[0].item_id
    ).one()
    item2_after = db_session.query(models.ReservationConsumptionAllocation).filter_by(
        item_id=items[1].item_id
    ).one()
    assert item1_after.sle_id == replacement.id
    assert item1_after.reservation_id == owners[items[0].item_id].id
    assert item2_after.id == item2_before.id
    assert item2_after.sle_id == facts[items[1].item_id].id


def test_bounded_make_preflight_failure_leaves_caller_transaction_clean(db_session):
    parent, target, items, _owners, _facts = _world(db_session)
    with pytest.raises(CurrentReplenishmentError, match="no stable current reservation"):
        apply_current_replenishment_for_bounded_make_scopes(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            target_cutoff=target.cutoff,
            affected_scopes=(
                _scope(items[0].item_id),
                (999999, "", "", "selected", "make"),
            ),
            source_revision=1,
        )
    db_session.rollback()
    assert db_session.query(models.ReservationConsumptionAllocation).count() == 0
    assert db_session.query(models.CurrentReplenishmentState).count() == 0


def test_bounded_make_failure_after_one_scope_is_rollbackable_by_caller(
    db_session, monkeypatch
):
    parent, target, items, _owners, _facts = _world(db_session)
    import app.services.item_ledger.current_replenishment as module

    original = module.apply_current_replenishment
    calls = 0

    def fail_on_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 2:
            raise CurrentReplenishmentError("injected bounded scope failure")
        return result

    monkeypatch.setattr(module, "apply_current_replenishment", fail_on_second)
    with pytest.raises(CurrentReplenishmentError, match="injected bounded scope failure"):
        apply_current_replenishment_for_bounded_make_scopes(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            target_cutoff=target.cutoff,
            affected_scopes=(_scope(items[0].item_id), _scope(items[1].item_id)),
            source_revision=1,
        )
    db_session.rollback()
    assert db_session.query(models.ReservationConsumptionAllocation).count() == 0
    assert db_session.query(models.CurrentReplenishmentState).count() == 0
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id


def test_bounded_make_stamps_the_publishing_generation_by_default(db_session):
    """R4 revision rule: the marker names the generation, not the batch."""
    parent, target, items, _owners, _facts = _world(db_session)
    result = apply_current_replenishment_for_bounded_make_scopes(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        target_cutoff=target.cutoff,
        affected_scopes=(_scope(items[0].item_id),),
    )
    db_session.commit()

    assert result.source_revision == int(target.id)
    assert {
        (int(row.source_revision), int(row.ledger_generation_id))
        for row in db_session.query(models.CurrentReplenishmentState).all()
    } == {(int(target.id), int(target.id))}

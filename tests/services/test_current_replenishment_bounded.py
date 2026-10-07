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


def test_bounded_make_credits_document_net_output_not_raw_assembly_in(db_session):
    """Canon: the internal transport of one document is not production output.

    ``СборкаЗапасов`` writes a receipt on the production warehouse, an issue
    from it and a receipt on the destination warehouse.  The canonical net
    output of that document is owned by ``document_net_output.py`` and both the
    replenishment replay and the plan-output allocation read it; crediting the
    raw ``assembly_in`` lines counted a component's pass-through movement as
    this item's production (item 8945 on the 28.09 stand copy: 3660 raw units
    against 300 units of real net output).
    """
    parent, target, items, owners, facts = _world(db_session)
    item = items[0]
    # The item's own fact of the world is 3 units on document ``M1``; add the
    # transport legs of that same document, which cancel it, plus a second
    # document that really produced 2 units.
    for quantity, kind, ref, line_no, warehouse in (
        ("-3", "assembly_out", "M1", "2", "WH-MAKE"),
        ("2", "assembly_in", "M3", "1", "WH-MAKE"),
    ):
        db_session.add(models.StockLedgerEntry(
            ingest_batch_id=target.physical_import_batch_id,
            source_content_hash=f"net-{ref}-{line_no}".ljust(64, "0"),
            business_identity=f"net:{ref}:{line_no}",
            item_id=item.item_id, characteristic_ref="", organization_ref="",
            warehouse_ref1c=warehouse, qty=Decimal(quantity),
            posting_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
            known_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
            record_type="Receipt" if Decimal(quantity) > 0 else "Expense",
            movement_kind=kind, recorder_type="Assembly", recorder_ref=ref,
            line_no=line_no, ingest_source="pull",
        ))
    db_session.flush()

    result = apply_current_replenishment_for_bounded_make_scopes(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        target_cutoff=target.cutoff,
        affected_scopes=(_scope(item.item_id),),
    )
    db_session.commit()

    assert result.netted_internal_transfer_rows == 1
    owner = db_session.get(models.ReservationEntry, owners[item.item_id].id)
    assert Decimal(str(owner.replenishment_received_qty)) == Decimal("2")
    allocated = {
        int(row.sle_id): Decimal(str(row.allocated_qty))
        for row in db_session.query(models.ReservationConsumptionAllocation).filter(
            models.ReservationConsumptionAllocation.reservation_id == owner.id,
            models.ReservationConsumptionAllocation.is_current.is_(True),
        ).all()
    }
    assert int(facts[item.item_id].id) not in allocated
    assert sum(allocated.values()) == Decimal("2")


def test_the_over_allocation_guard_measures_an_output_against_its_net(db_session):
    """Invariants 2-3: an ``assembly_in`` line exists only as its net output.

    Measured against the raw line, an allocation of a pure transport leg
    satisfied the invariant while counting a movement that produced nothing.
    A supplier receipt and a material issue keep their own ``|qty|``.
    """
    from app.services.item_ledger.current_replenishment import over_allocated_facts

    parent, _target, items, owners, _facts = _world(db_session)
    item = items[0]
    owner = owners[item.item_id]
    posted = parent.cutoff

    def leg(qty, kind, line_no, suffix):
        row = models.StockLedgerEntry(
            ingest_batch_id=parent.physical_import_batch_id,
            source_content_hash=f"guard-{suffix}".ljust(64, "0"),
            business_identity=f"guard:{suffix}",
            item_id=item.item_id, characteristic_ref="", organization_ref="",
            warehouse_ref1c="WH-MAKE", qty=Decimal(qty), posting_at=posted,
            record_type="Receipt" if Decimal(qty) > 0 else "Expense",
            movement_kind=kind, recorder_type="Assembly", recorder_ref="G1",
            line_no=line_no, ingest_source="pull",
        )
        db_session.add(row)
        db_session.flush()
        return row

    # One document: a receipt on the production warehouse and the issue that
    # takes it away again.  It produced nothing.
    receipt = leg("3", "assembly_in", "1", "in")
    leg("-3", "assembly_out", "2", "out")
    db_session.add(models.ReservationConsumptionAllocation(
        ledger_generation_id=parent.id, reservation_id=owner.id,
        sle_id=receipt.id, requirement_id=owner.requirement_id,
        allocated_qty=Decimal("3"), match_rule="fifo", fact_ref="G1",
        fact_line_ref="1", item_id=item.item_id, characteristic_ref="",
        organization_ref="", planning_stock_pool="selected",
        idempotency_key="guard-1", allocation_role="replenishment_receipt",
        is_current=True, event_at=posted,
    ))
    db_session.flush()

    offenders = over_allocated_facts(db_session, limit=0)
    assert [(row[0], row[2], row[3]) for row in offenders] == [
        (int(receipt.id), Decimal("3"), Decimal("0"))
    ]


def test_an_assembly_out_leg_alone_opens_the_make_scope(db_session):
    """The netted kinds both decide how much a document produced.

    An ``assembly_out`` leg arriving on its own - a re-posted or backdated
    document line - changes the net output of its document, so it has to
    replay the MAKE scope; scoping on receipts alone left the stale credit in
    place.
    """
    from types import SimpleNamespace

    from app.services.item_ledger import physical_refresh_current_publish as publisher

    parent, _target, items, owners, _facts = _world(db_session)
    owner = owners[items[0].item_id]
    row = SimpleNamespace(
        id=1, item_id=int(items[0].item_id), characteristic_ref="",
        organization_ref="", warehouse_ref1c="WH-MAKE",
        movement_kind="assembly_out", recorder_type="Document_СборкаЗапасов",
    )
    current_owners = publisher._current_owner_rows(
        db_session, (row,), planning_pool_by_warehouse={"WH-MAKE": "default"},
    )
    assert {int(entry.id) for entry in current_owners} == {int(owner.id)}
    assert publisher._current_scopes(
        (row,),
        planning_pool_by_warehouse={"WH-MAKE": "default"},
        current_owners=current_owners,
    ) == ((int(items[0].item_id), "", "", "default", "make"),)


@pytest.mark.parametrize("covered", [Decimal("0"), Decimal("17")])
def test_bounded_successor_uses_signed_pre_freeze_stock_once(db_session, covered):
    from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C
    parent, target, items, owners, facts = _world(db_session)
    item, owner, old = items[0], owners[items[0].item_id], facts[items[0].item_id]
    db_session.add(models.StockWarehouse(warehouse_ref1c="WH-MAKE", warehouse_name="Planning", is_selected=True))
    owner.reserved_qty = Decimal("259") + covered
    owner.covered_from_stock_at_freeze_qty = covered
    owner.replenishment_required_qty = Decimal("259")
    old.ingest_batch_id = parent.physical_import_batch_id
    old.posting_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
    old.organization_ref = DEFAULT_ORGANIZATION_REF1C
    old.qty = Decimal("100")
    old.qty_after = Decimal("137")
    db_session.add(models.MrpFreezeBaseline(run_id=owner.run_id, freeze_version=1, item_id=item.item_id,
        characteristic_ref="", organization_ref="", planning_stock_pool="selected",
        baseline_at=parent.cutoff, physical_import_batch_id=parent.physical_import_batch_id,
        frozen_basis_generation_id=parent.id, stock_qty=17))
    last_receipt = None
    for day, quantity, kind in [(1,37,"receipt"),(3,61,"assembly_in"),(4,44,"assembly_in"),
                                (5,-215,"assembly_out"),(6,-10,"expense")]:
        row = models.StockLedgerEntry(ingest_batch_id=parent.physical_import_batch_id,
            source_content_hash=("signed-freeze-"+str(day)).ljust(64,"0"),
            business_identity="signed-freeze:"+str(day), item_id=item.item_id,
            characteristic_ref="", organization_ref=DEFAULT_ORGANIZATION_REF1C, warehouse_ref1c="WH-MAKE",
            qty=Decimal(quantity), qty_after=0, posting_at=datetime(2026,9,day,tzinfo=timezone.utc),
            known_at=parent.cutoff, record_type="Receipt" if quantity>0 else "Expense",
            movement_kind=kind, recorder_type="Assembly", recorder_ref="signed-"+str(day),
            line_no="1", ingest_source="pull")
        db_session.add(row)
        if day == 4:
            last_receipt = row
    db_session.commit()
    args=dict(target_generation_id=target.id,parent_generation_id=parent.id,target_cutoff=target.cutoff,
              affected_scopes=(_scope(item.item_id),),source_revision=1)
    result=apply_current_replenishment_for_bounded_make_scopes(db_session,**args)
    db_session.commit()
    assert owner.replenishment_received_qty == 17-covered
    assert owner.replenishment_required_qty == 259
    assert owner.covered_from_stock_at_freeze_qty == covered
    rows=db_session.query(models.ReservationConsumptionAllocation).filter_by(
        reservation_id=owner.id,is_current=True,allocation_role="replenishment_receipt").all()
    assert sum((row.allocated_qty for row in rows),Decimal("0"))==17-covered
    assert all(row.sle_id==last_receipt.id for row in rows)
    again=apply_current_replenishment_for_bounded_make_scopes(db_session,**args)
    assert again.results[0].inserted==0 and again.results[0].updated==0

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.current_replenishment import (
    BoundedBuyReceiptDeltaManifest,
    CurrentReplenishmentError,
    apply_current_replenishment_for_bounded_buy_scopes,
)
from app.services.item_ledger.supplier_receipt_allocation import (
    SUPPLIER_ORDER_TYPE,
    ReceiptFact,
)


def _scope(item_id: int):
    return (int(item_id), "", "", "default", "buy")


def _batch(key: str, cutoff: datetime):
    return models.PhysicalImportBatch(
        batch_key=key,
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
        source_complete=True,
    )


def _world(db_session, *, item_count=1):
    parent_cutoff = datetime(2026, 9, 10, tzinfo=timezone.utc)
    target_cutoff = datetime(2026, 9, 11, tzinfo=timezone.utc)
    parent_batch = _batch("bounded-buy-parent", parent_cutoff)
    target_batch = _batch("bounded-buy-target", target_cutoff)
    db_session.add_all([parent_batch, target_batch])
    db_session.flush()
    parent = models.LedgerGeneration(
        generation_key="bounded-buy-parent",
        status="accepted",
        cutoff=parent_cutoff,
        accepted_at=parent_cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=parent_batch,
        algorithm_version="bounded-buy-tests",
    )
    target = models.LedgerGeneration(
        generation_key="bounded-buy-target",
        status="building",
        cutoff=target_cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=target_batch,
        algorithm_version="bounded-buy-tests",
    )
    db_session.add_all([parent, target])
    db_session.flush()
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    items = []
    owners = []
    for index in range(item_count):
        item = models.Item(item_code=f"BOUNDED-BUY-{index}", item_name="bounded buy")
        db_session.add(item)
        db_session.flush()
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
        db_session.add(run)
        db_session.flush()
        requirement = models.MrpRequirement(
            run_id=run.run_id,
            item_id=item.item_id,
            total_required_qty=Decimal("10"),
            net_required_qty=Decimal("10"),
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 30),
            bom_level=0,
            planning_stock_pool="default",
            characteristic_ref="",
            organization_ref="",
            freeze_version=1,
        )
        db_session.add(requirement)
        db_session.flush()
        owner = models.ReservationEntry(
            ledger_generation_id=parent.id,
            item_id=item.item_id,
            run_id=run.run_id,
            freeze_version=1,
            requirement_id=requirement.id,
            priority_period_from=date(2026, 9, 1),
            priority_period_to=date(2026, 9, 30),
            realization_mode="buy",
            planning_stock_pool="default",
            reserved_qty=Decimal("10"),
            replenishment_required_qty=Decimal("10"),
            lifecycle_status="active",
            current_identity=f"reservation:req:{requirement.id}:mode:buy",
            owner_kind="current",
            is_current=True,
        )
        db_session.add(owner)
        db_session.flush()
        items.append(item)
        owners.append(owner)
    db_session.commit()
    return parent, target, target_batch, items, owners


def _receipt(
    db_session,
    batch,
    item,
    *,
    quantity="4",
    ref="receipt-1",
    at=None,
    order_ref="order-1",
    order_line="1",
):
    at = at or batch.cutoff
    row = models.StockLedgerEntry(
        ingest_batch_id=batch.id,
        source_content_hash=(f"bounded-{ref}").ljust(64, "0"),
        business_identity=f"bounded:{ref}",
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="WH-BUY",
        qty=Decimal(quantity),
        qty_after=Decimal(quantity),
        posting_at=at,
        known_at=batch.cutoff,
        record_type="Receipt",
        movement_kind="supplier_receipt",
        recorder_type="Purchase",
        recorder_ref=ref,
        line_no="1",
        ingest_source="pull",
    )
    db_session.add(row)
    db_session.flush()
    return row, ReceiptFact(
        sle_id=row.id,
        posting_at=at,
        known_at=batch.cutoff,
        signed_qty=Decimal(quantity),
        item_id=item.item_id,
        supplier_order_ref=order_ref,
        supplier_order_line_no=order_line,
        receipt_ref=ref,
        receipt_line_no="1",
        planning_stock_pool="default",
        # What the normalizer records for an exact line: the order document.
        supplier_order_type=SUPPLIER_ORDER_TYPE if order_ref and order_line else "",
    )


def _call(db_session, parent, target, fact, *, scopes=None, source_revision=1, full=()):
    scopes = scopes or (_scope(fact.item_id),)
    manifest = BoundedBuyReceiptDeltaManifest(
        new_sle_ids=(fact.sle_id,),
        receipt_facts=(fact,),
        scope_receipt_facts=tuple(full),
    )
    return apply_current_replenishment_for_bounded_buy_scopes(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        target_cutoff=target.cutoff,
        affected_scopes=scopes,
        source_revision=source_revision,
        delta_manifest=manifest,
    )


def test_bounded_buy_forward_receipt_is_scoped_and_never_reads_visible_prefix(
    db_session, monkeypatch
):
    parent, target, batch, items, owners = _world(db_session)
    row, fact = _receipt(db_session, batch, items[0])
    from app.services.item_ledger import physical_visibility

    monkeypatch.setattr(
        physical_visibility,
        "visible_sles_for_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bounded BUY must not scan historical visible SLEs")
        ),
    )
    result = _call(db_session, parent, target, fact)
    db_session.commit()

    assert result.delta_fact_rows == 1
    assert result.scope_replay_rows == 1
    allocation = db_session.query(models.ReservationConsumptionAllocation).one()
    assert allocation.sle_id == row.id
    assert allocation.reservation_id == owners[0].id
    assert allocation.is_current is True
    assert db_session.query(models.ReservationEvent).count() == 0
    assert db_session.query(models.StockLedgerSupplierReceiptProvenance).count() == 1


def test_bounded_buy_empty_manifest_is_true_noop(db_session):
    parent, target, _batch_row, items, _owners = _world(db_session)
    result = apply_current_replenishment_for_bounded_buy_scopes(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        target_cutoff=target.cutoff,
        affected_scopes=(_scope(items[0].item_id),),
        source_revision=1,
        delta_manifest=BoundedBuyReceiptDeltaManifest(),
    )
    assert result.delta_fact_rows == 0
    assert result.results == ()
    assert db_session.query(models.ReservationConsumptionAllocation).count() == 0
    assert db_session.query(models.StockLedgerSupplierReceiptProvenance).count() == 0


def test_bounded_buy_writes_the_matched_order_type_on_an_exact_row(db_session):
    """The obligation-refresh rebuild re-derives ``exact`` from this type.

    Without it the row replays as ``unmatched`` and the receipt stops
    allocating to its BUY owner, so the writer takes the type the manifest
    matched, and refuses an exact fact that does not carry one.
    """
    parent, target, batch, items, _owners = _world(db_session)
    row, fact = _receipt(db_session, batch, items[0])
    _call(db_session, parent, target, fact)
    db_session.commit()

    provenance = db_session.query(models.StockLedgerSupplierReceiptProvenance).one()
    assert provenance.match_status == "exact"
    assert provenance.evidence_payload["supplier_order_type"] == SUPPLIER_ORDER_TYPE
    assert provenance.receipt_doc_type == row.recorder_type


def test_bounded_buy_refuses_an_exact_fact_without_the_order_type(db_session):
    from dataclasses import replace

    parent, target, batch, items, _owners = _world(db_session)
    _row, fact = _receipt(db_session, batch, items[0])
    with pytest.raises(CurrentReplenishmentError, match="requires supplier order type"):
        _call(db_session, parent, target, replace(fact, supplier_order_type=""))


def test_bounded_buy_exact_retry_has_no_new_audit_or_provenance(db_session):
    parent, target, batch, items, _owners = _world(db_session)
    _row, fact = _receipt(db_session, batch, items[0])
    kwargs = dict(parent=parent, target=target, fact=fact, source_revision=9)
    first = _call(db_session, **kwargs)
    db_session.commit()
    allocation_id = db_session.query(models.ReservationConsumptionAllocation.id).scalar()
    audit_count = db_session.query(models.CurrentReplenishmentAudit).count()
    provenance_count = db_session.query(models.StockLedgerSupplierReceiptProvenance).count()
    second = _call(db_session, **kwargs)
    db_session.commit()
    assert first.audit_events == 1
    assert second.audit_events == 0
    assert second.results[0].idempotent is True
    assert db_session.query(models.ReservationConsumptionAllocation.id).scalar() == allocation_id
    assert db_session.query(models.CurrentReplenishmentAudit).count() == audit_count
    assert db_session.query(models.StockLedgerSupplierReceiptProvenance).count() == provenance_count


def test_bounded_buy_foreign_and_incomplete_manifest_fail_closed(db_session):
    parent, target, batch, items, _owners = _world(db_session)
    _row, fact = _receipt(db_session, batch, items[0])
    foreign = ReceiptFact(**{**fact.__dict__, "item_id": 999})
    with pytest.raises(CurrentReplenishmentError, match="scope|contradicts"):
        _call(db_session, parent, target, foreign)
    negative_row, negative_fact = _receipt(
        db_session, batch, items[0], quantity="-1", ref="return-1"
    )
    with pytest.raises(CurrentReplenishmentError, match="complete bounded scope"):
        _call(db_session, parent, target, negative_fact)


def test_bounded_buy_two_successive_targets_reuse_old_typed_evidence(db_session):
    parent, target1, batch1, items, owners = _world(db_session)
    _row1, fact1 = _receipt(db_session, batch1, items[0], quantity="3", ref="receipt-1")
    _call(db_session, parent, target1, fact1)
    db_session.flush()
    target1.status = "accepted"
    target1.accepted_at = target1.cutoff
    pointer = db_session.get(models.PlanningTruthState, 1)
    pointer.current_generation_id = target1.id
    batch2 = _batch("bounded-buy-target-2", datetime(2026, 9, 12, tzinfo=timezone.utc))
    db_session.add(batch2)
    db_session.flush()
    target2 = models.LedgerGeneration(
        generation_key="bounded-buy-target-2",
        status="building",
        cutoff=batch2.cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=batch2,
        algorithm_version="bounded-buy-tests",
    )
    db_session.add(target2)
    db_session.flush()
    _row2, fact2 = _receipt(db_session, batch2, items[0], quantity="2", ref="receipt-2")

    result = _call(db_session, target1, target2, fact2, source_revision=2)
    db_session.commit()

    assert result.delta_fact_rows == 1
    assert db_session.query(models.StockLedgerSupplierReceiptProvenance).count() == 2
    allocations = db_session.query(models.ReservationConsumptionAllocation).filter(
        models.ReservationConsumptionAllocation.is_current.is_(True)
    ).order_by(models.ReservationConsumptionAllocation.sle_id).all()
    assert [row.sle_id for row in allocations] == [_row1.id, _row2.id]
    assert {row.reservation_id for row in allocations} == {owners[0].id}


def test_bounded_buy_successive_targets_reuse_consistent_unmatched_evidence(db_session):
    parent, target1, batch1, items, owners = _world(db_session)
    row1, fact1 = _receipt(
        db_session,
        batch1,
        items[0],
        quantity="3",
        ref="direct-receipt-1",
        order_ref="",
        order_line="",
    )
    _call(db_session, parent, target1, fact1)
    db_session.flush()
    target1.status = "accepted"
    target1.accepted_at = target1.cutoff
    db_session.get(models.PlanningTruthState, 1).current_generation_id = target1.id
    batch2 = _batch("bounded-buy-unmatched-target-2", datetime(2026, 9, 12, tzinfo=timezone.utc))
    db_session.add(batch2)
    db_session.flush()
    target2 = models.LedgerGeneration(
        generation_key="bounded-buy-unmatched-target-2",
        status="building",
        cutoff=batch2.cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=batch2,
        algorithm_version="bounded-buy-tests",
    )
    db_session.add(target2)
    db_session.flush()
    row2, fact2 = _receipt(
        db_session,
        batch2,
        items[0],
        quantity="2",
        ref="direct-receipt-2",
        order_ref="",
        order_line="",
    )

    result = _call(db_session, target1, target2, fact2, source_revision=2)
    db_session.commit()

    assert result.delta_fact_rows == 1
    assert db_session.query(models.StockLedgerSupplierReceiptProvenance).count() == 2
    allocations = db_session.query(models.ReservationConsumptionAllocation).filter(
        models.ReservationConsumptionAllocation.is_current.is_(True)
    ).all()
    assert [(row.sle_id, row.reservation_id) for row in allocations] == [
        (row1.id, owners[0].id),
        (row2.id, owners[0].id),
    ]


def test_bounded_buy_return_replays_complete_affected_scope_only(db_session):
    parent, target1, batch1, items, owners = _world(db_session)
    row1, fact1 = _receipt(db_session, batch1, items[0], quantity="3", ref="receipt-1")
    _call(db_session, parent, target1, fact1)
    db_session.flush()
    target1.status = "accepted"
    target1.accepted_at = target1.cutoff
    db_session.get(models.PlanningTruthState, 1).current_generation_id = target1.id
    batch2 = _batch("bounded-buy-return", datetime(2026, 9, 12, tzinfo=timezone.utc))
    db_session.add(batch2)
    db_session.flush()
    target2 = models.LedgerGeneration(
        generation_key="bounded-buy-return",
        status="building",
        cutoff=batch2.cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=batch2,
        algorithm_version="bounded-buy-tests",
    )
    db_session.add(target2)
    db_session.flush()
    _row2, fact2 = _receipt(db_session, batch2, items[0], quantity="-1", ref="return-1")

    result = _call(
        db_session,
        target1,
        target2,
        fact2,
        source_revision=2,
        full=(fact1, fact2),
    )
    db_session.commit()

    assert result.delta_fact_rows == 1
    assert result.scope_replay_rows == 2
    assert db_session.query(models.StockLedgerSupplierReceiptProvenance).count() == 2
    current = db_session.query(models.ReservationConsumptionAllocation).filter(
        models.ReservationConsumptionAllocation.is_current.is_(True)
    ).all()
    assert [(row.sle_id, row.reservation_id) for row in current] == [(row1.id, owners[0].id)]


def test_bounded_buy_partitions_multiple_scopes_without_neighbor_churn(db_session):
    parent, target, batch, items, owners = _world(db_session, item_count=2)
    _row1, fact1 = _receipt(db_session, batch, items[0], quantity="3", ref="receipt-1")
    _row2, fact2 = _receipt(db_session, batch, items[1], quantity="4", ref="receipt-2")
    result = apply_current_replenishment_for_bounded_buy_scopes(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        target_cutoff=target.cutoff,
        affected_scopes=(_scope(items[0].item_id), _scope(items[1].item_id)),
        source_revision=1,
        delta_manifest=BoundedBuyReceiptDeltaManifest(
            new_sle_ids=(fact1.sle_id, fact2.sle_id),
            receipt_facts=(fact1, fact2),
        ),
    )
    db_session.commit()

    assert result.delta_fact_rows == 2
    assert result.scope_replay_rows == 2
    allocations = db_session.query(models.ReservationConsumptionAllocation).filter(
        models.ReservationConsumptionAllocation.is_current.is_(True)
    ).order_by(models.ReservationConsumptionAllocation.item_id).all()
    assert [(row.item_id, row.reservation_id) for row in allocations] == [
        (items[0].item_id, owners[0].id),
        (items[1].item_id, owners[1].id),
    ]
    assert db_session.query(models.ReservationEvent).count() == 0


def test_bounded_buy_fifo_split_uses_all_stable_current_owners(db_session):
    parent, target, batch, items, owners = _world(db_session)
    item = items[0]
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
    db_session.add(run)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=Decimal("5"),
        net_required_qty=Decimal("5"),
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        bom_level=0,
        planning_stock_pool="default",
        characteristic_ref="",
        organization_ref="",
        freeze_version=1,
    )
    db_session.add(requirement)
    db_session.flush()
    second_owner = models.ReservationEntry(
        ledger_generation_id=parent.id,
        item_id=item.item_id,
        run_id=run.run_id,
        freeze_version=1,
        requirement_id=requirement.id,
        priority_period_from=date(2026, 9, 2),
        priority_period_to=date(2026, 9, 30),
        realization_mode="buy",
        planning_stock_pool="default",
        reserved_qty=Decimal("5"),
        replenishment_required_qty=Decimal("5"),
        lifecycle_status="active",
        current_identity=f"reservation:req:{requirement.id}:mode:buy",
        owner_kind="current",
        is_current=True,
    )
    db_session.add(second_owner)
    db_session.flush()
    _row, fact = _receipt(db_session, batch, item, quantity="12", ref="receipt-fifo")

    _call(db_session, parent, target, fact)
    db_session.commit()

    allocations = db_session.query(models.ReservationConsumptionAllocation).filter(
        models.ReservationConsumptionAllocation.is_current.is_(True)
    ).order_by(models.ReservationConsumptionAllocation.reservation_id).all()
    assert [(row.reservation_id, row.allocated_qty) for row in allocations] == [
        (owners[0].id, Decimal("10")),
        (second_owner.id, Decimal("2")),
    ]


def test_bounded_buy_exact_cap_uses_current_owner_not_parent_generation(db_session):
    parent, _target, target_batch, items, owners = _world(db_session, item_count=2)
    legacy_batch = _batch(
        "bounded-buy-cap-legacy-batch",
        datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    db_session.add(legacy_batch)
    db_session.flush()
    legacy_generation = models.LedgerGeneration(
        generation_key="bounded-buy-cap-legacy-generation",
        status="accepted",
        cutoff=legacy_batch.cutoff,
        accepted_at=legacy_batch.cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=legacy_batch,
        algorithm_version="bounded-buy-tests",
    )
    db_session.add(legacy_generation)
    db_session.flush()
    owners[0].ledger_generation_id = legacy_generation.id
    owners[1].ledger_generation_id = legacy_generation.id
    scope = models.CurrentExecutionScope(
        entity_kind="purchase_control_journal",
        scope_key="bounded-buy-cap-scope",
        source_generation_id=legacy_generation.id,
        source_revision="legacy",
        result_ready=True,
        content_hash="c" * 64,
        summary={},
    )
    db_session.add(scope)
    db_session.flush()
    export_batch = models.PurchaseExportBatch(
        ledger_generation_id=legacy_generation.id,
        current_execution_scope_id=scope.id,
        current_execution_source_revision="legacy",
        idempotency_key="bounded-buy-cap-export",
        status="completed",
        payload_hash="d" * 64,
        request_payload={},
        result_payload={},
    )
    db_session.add(export_batch)
    db_session.flush()
    db_session.add_all(
        [
            models.PurchaseExportObligationAllocation(
                batch_id=export_batch.id,
                reservation_id=owners[0].id,
                supplier_order_ref="order-1",
                supplier_order_line_no="1",
                allocated_qty=Decimal("2"),
                ledger_generation_id=legacy_generation.id,
                item_id=items[0].item_id,
                planning_stock_pool="default",
            ),
            models.PurchaseExportObligationAllocation(
                batch_id=export_batch.id,
                reservation_id=owners[1].id,
                supplier_order_ref="order-1",
                supplier_order_line_no="1",
                allocated_qty=Decimal("9"),
                ledger_generation_id=legacy_generation.id,
                item_id=items[1].item_id,
                planning_stock_pool="default",
            ),
        ]
    )
    db_session.flush()

    from app.services.item_ledger.supplier_receipt_allocation import (
        _exact_allocation_caps_by_order_line,
    )

    caps = _exact_allocation_caps_by_order_line(
        db_session,
        ledger_generation_id=parent.id,
        item_ids={items[0].item_id},
        current_owner_ids={owners[0].id},
    )
    assert caps == {(items[0].item_id, "order-1", "1"): {owners[0].id: Decimal("2")}}


def test_bounded_buy_failure_is_caller_rollbackable(db_session, monkeypatch):
    parent, target, batch, items, _owners = _world(db_session, item_count=2)
    _row1, fact1 = _receipt(db_session, batch, items[0], ref="receipt-1")
    _row2, fact2 = _receipt(db_session, batch, items[1], ref="receipt-2")
    import app.services.item_ledger.current_replenishment as module

    original = module.apply_current_receipt_replay
    calls = 0

    def fail_on_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 2:
            raise CurrentReplenishmentError("injected bounded BUY failure")
        return result

    monkeypatch.setattr(module, "apply_current_receipt_replay", fail_on_second)
    with pytest.raises(CurrentReplenishmentError, match="injected bounded BUY failure"):
        apply_current_replenishment_for_bounded_buy_scopes(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            target_cutoff=target.cutoff,
            affected_scopes=(_scope(items[0].item_id), _scope(items[1].item_id)),
            source_revision=1,
            delta_manifest=BoundedBuyReceiptDeltaManifest(
                new_sle_ids=(fact1.sle_id, fact2.sle_id),
                receipt_facts=(fact1, fact2),
            ),
        )
    db_session.rollback()
    assert db_session.query(models.ReservationConsumptionAllocation).count() == 0
    assert db_session.query(models.CurrentReplenishmentState).count() == 0
    assert db_session.query(models.StockLedgerSupplierReceiptProvenance).count() == 0


def test_bounded_buy_stale_parent_is_rejected(db_session):
    parent, target, batch, items, _owners = _world(db_session)
    _row, fact = _receipt(db_session, batch, items[0])
    pointer = db_session.get(models.PlanningTruthState, 1)
    pointer.current_generation_id = target.id
    with pytest.raises(CurrentReplenishmentError, match="not current truth"):
        _call(db_session, parent, target, fact)

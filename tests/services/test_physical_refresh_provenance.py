from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.future_supply_read import future_supply_model
from app.services.item_ledger import physical_refresh_orchestrator as workflow
from app.services.item_ledger.physical_refresh_provenance import (
    PhysicalRefreshProvenanceUnavailable,
    apply_bounded_current_material_custody_events,
    handoff_current_future_supply_provenance,
    handoff_current_material_custody_provenance,
    canonical_issue_backfill_source_ids,
)
from app.services.production_material_custody_projection import (
    _event_high_watermark_id_at_cutoff,
    _same_1c_timestamp,
    _require_manifest_cutoff,
    _resolve_projection_baseline,
    load_compact_current_material_custody,
)
from app.services.production_material_custody_events import _custody_event_idempotency_key


def _world(db):
    cutoff = datetime(2026, 9, 15, tzinfo=timezone.utc)
    parent_batch = models.PhysicalImportBatch(
        batch_key="handoff-parent-batch", status="completed",
        source_watermarks={}, cutoff=cutoff,
    )
    target_batch = models.PhysicalImportBatch(
        batch_key="handoff-target-batch", status="completed",
        source_watermarks={}, cutoff=cutoff,
    )
    parent = models.LedgerGeneration(
        generation_key="handoff-parent", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, source_watermarks={}, capabilities={},
        physical_import_batch=parent_batch, algorithm_version="test",
    )
    target = models.LedgerGeneration(
        generation_key="handoff-target", status="building", cutoff=cutoff,
        source_watermarks={"parent_generation_id": 1}, capabilities={},
        physical_import_batch=target_batch, algorithm_version="test",
    )
    item = models.Item(item_code="HANDOFF-ITEM", item_name="handoff")
    order = models.ProductionOrder(
        order_number="HANDOFF-ORDER", order_date=cutoff, order_ref1c="handoff-order",
    )
    db.add_all([parent_batch, target_batch, parent, target, item, order])
    db.flush()
    batch = models.LedgerBuildBatch(
        ledger_generation_id=parent.id, stage="future_supply_capture",
        batch_key="handoff-capture", status="completed", algorithm_version="test",
        metrics={},
    )
    product = models.ProductionProduct(
        order_id=order.order_id, item_id=item.item_id, quantity=Decimal("1"),
        remaining_qty=Decimal("1"), produced_qty=Decimal("0"),
    )
    db.add_all([batch, product])
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    current = models.LedgerFutureSupplyCurrent(
        current_identity="handoff:supply",
        source_generation_id=parent.id, source_capture_batch_id=batch.id,
        supply_kind="wip_order", item_id=item.item_id,
        planning_stock_pool="main", destination_warehouse_ref1c="WH",
        ordered_qty_at_cutoff=Decimal("4"), realized_qty_at_cutoff=Decimal("1"),
        open_qty_at_cutoff=Decimal("3"), source_state_key="open",
        capture_cutoff=cutoff, source_content_hash="a" * 64, evidence_status="exact",
    )
    custody = models.ProductionMaterialCustodyProjection(
        ledger_generation_id=parent.id, product_id=product.product_id,
        component_item_id=item.item_id, location_kind="workshop",
        warehouse_ref1c="WH", reserved_qty=Decimal("2"),
        source_event_high_watermark_id=0, is_current=True,
    )
    manifest = models.ProductionMaterialCustodyProjectionManifest(
        ledger_generation_id=parent.id, baseline_generation_id=parent.id,
        cutoff=cutoff, status="complete", source_event_high_watermark_id=0,
    )
    db.add_all([current, custody, manifest])
    db.flush()
    return parent, target, current, custody


@pytest.mark.parametrize('mutation', ['valid', 'bad_key', 'backdated', 'future', 'foreign_warehouse'])
def test_forward_operator_custody_waiting_behind_physical_tail_is_proved(db_session, mutation):
    parent, target, _, custody = _world(db_session)
    target.cutoff = parent.cutoff + timedelta(days=1)
    target.physical_import_batch.cutoff = target.cutoff
    product = db_session.get(models.ProductionProduct, custody.product_id)
    issue = models.ProductionMaterialIssue(document_number='LOCAL-COMMAND', product_id=product.product_id,
        order_id=product.order_id, status='posted', direction='issue', source_warehouse_ref1c='WH', warehouse_ref1c='WH')
    db_session.add(issue); db_session.flush()
    line = models.ProductionMaterialIssueLine(issue_id=issue.issue_id, component_item_id=custody.component_item_id,
        required_qty=4, issued_qty=4, custody_event_revision=1)
    db_session.add(line); db_session.flush()
    event = models.ProductionMaterialCustodyEvent(issue_id=issue.issue_id, product_id=product.product_id,
        component_item_id=line.component_item_id, source_kind='issue_created', source_sle_id=None,
        effective_at=parent.cutoff+timedelta(hours=1), location_kind='workshop', warehouse_ref1c='WH',
        source_ref1c='WH', delta_qty=Decimal('4'), document_number=issue.document_number,
        document_line_no=str(line.line_id), idempotency_key=_custody_event_idempotency_key(
            issue_id=issue.issue_id,line_id=line.line_id,revision=1,source_kind='issue_created',
            location_kind='workshop',warehouse_ref1c='WH',delta_qty=4,source_sle_id=None))
    if mutation == 'bad_key':event.idempotency_key='forged'
    elif mutation == 'backdated':event.effective_at=parent.cutoff
    elif mutation == 'future':event.effective_at=target.cutoff+timedelta(hours=1)
    elif mutation == 'foreign_warehouse':event.warehouse_ref1c='FOREIGN'
    db_session.add(event);db_session.flush()
    if mutation == 'valid':
        assert canonical_issue_backfill_source_ids(db_session, events=[event], physical_import_batch_id=target.physical_import_batch_id,
                   parent_cutoff=parent.cutoff, target_cutoff=target.cutoff)==()
        assert apply_bounded_current_material_custody_events(db_session,parent_generation_id=parent.id,
                   target_generation_id=target.id,source_sle_ids=())==1
        assert custody.reserved_qty==Decimal('6')
    else:
        with pytest.raises(PhysicalRefreshProvenanceUnavailable):
            canonical_issue_backfill_source_ids(db_session, events=[event],physical_import_batch_id=target.physical_import_batch_id,
                        parent_cutoff=parent.cutoff,target_cutoff=target.cutoff)


def _building_generation(db, *, key: str, cutoff: datetime) -> models.LedgerGeneration:
    """One more BUILDING physical candidate for a consecutive bounded refresh."""
    batch = models.PhysicalImportBatch(
        batch_key=f"{key}-batch", status="completed",
        source_watermarks={}, cutoff=cutoff,
    )
    generation = models.LedgerGeneration(
        generation_key=key, status="building", cutoff=cutoff,
        source_watermarks={}, capabilities={},
        physical_import_batch=batch, algorithm_version="test",
    )
    db.add_all([batch, generation])
    db.flush()
    return generation


def _custody_revision_world(
    db, *, chain: bool = False, tombstone: bool = False, clipped: bool = False,
):
    """Accepted −64/+64 transfer, then a physical revision in one refresh."""
    parent, target, _supply, workshop = _world(db)
    earlier = parent.cutoff - timedelta(days=14)
    before = parent.cutoff.replace(tzinfo=None) - timedelta(days=1)
    baseline = _building_generation(db, key="custody-explicit-baseline", cutoff=earlier)
    baseline.status = "accepted"
    baseline.accepted_at = earlier
    db.add(models.ProductionMaterialCustodyProjectionManifest(
        ledger_generation_id=baseline.id, baseline_generation_id=baseline.id,
        cutoff=earlier, status="complete", is_baseline=True,
        source_event_high_watermark_id=0,
    ))
    target.cutoff = parent.cutoff + timedelta(days=1)
    target.physical_import_batch.cutoff = target.cutoff
    workshop.reserved_qty = Decimal("64")
    workshop.warehouse_ref1c = "WORKSHOP"
    transit = models.ProductionMaterialCustodyProjection(
        ledger_generation_id=parent.id, product_id=workshop.product_id,
        component_item_id=workshop.component_item_id,
        location_kind="transit", warehouse_ref1c="TRANSIT",
        reserved_qty=Decimal("48"), source_event_high_watermark_id=0,
        is_current=True,
    )
    db.add(transit)
    db.flush()

    def sle(batch, name, qty, warehouse):
        row = models.StockLedgerEntry(
            ingest_batch_id=batch.id, source_content_hash=name,
            business_identity=f"custody-{warehouse}",
            item_id=int(workshop.component_item_id),
            characteristic_ref="", organization_ref="org",
            warehouse_ref1c=warehouse, qty=Decimal(str(qty)),
            posting_at=before, record_type="Expense" if qty < 0 else "Receipt",
            movement_kind="transfer_out" if qty < 0 else "transfer_in",
            recorder_type="Document_Transfer",
            recorder_ref="custody-transit-document" if warehouse == "TRANSIT"
            else "custody-workshop-document",
            line_no="1", ingest_source="test",
        )
        db.add(row)
        db.flush()
        return row

    def event(kind, source, qty, warehouse, location, name):
        row = models.ProductionMaterialCustodyEvent(
            product_id=int(workshop.product_id),
            component_item_id=int(workshop.component_item_id),
            source_kind=kind, source_sle_id=None if source is None else source.id,
            effective_at=before if source is not None else before - timedelta(days=1),
            location_kind=location, warehouse_ref1c=warehouse,
            delta_qty=Decimal(str(qty)), idempotency_key=name,
        )
        db.add(row)
        db.flush()
        return row

    event("issue_created", None, 112, "TRANSIT", "transit", "custody-open-112")
    old_t = sle(parent.physical_import_batch, "old-transit", -64, "TRANSIT")
    old_w = sle(parent.physical_import_batch, "old-workshop", 64, "WORKSHOP")
    old_events = [
        event("transfer_posted", old_t, -64, "TRANSIT", "transit", "old-transit-event"),
        event("transfer_posted", old_w, 64, "WORKSHOP", "workshop", "old-workshop-event"),
    ]
    last_parent_event = old_events[-1]
    if clipped:
        last_parent_event = models.ProductionMaterialCustodyEvent(
            product_id=int(workshop.product_id),
            component_item_id=int(workshop.component_item_id),
            source_kind="terminal_release", source_sle_id=None,
            source_ref2c="order-terminal-v1:revision-test",
            effective_at=before + timedelta(hours=1),
            location_kind="transit", warehouse_ref1c="TRANSIT",
            delta_qty=Decimal("-100"), idempotency_key="terminal-clipping-test",
        )
        db.add(last_parent_event)
        db.flush()
        db.delete(transit)
    parent_manifest = db.get(models.ProductionMaterialCustodyProjectionManifest, parent.id)
    parent_manifest.source_event_high_watermark_id = last_parent_event.id
    workshop.source_event_high_watermark_id = last_parent_event.id
    if not clipped:
        transit.source_event_high_watermark_id = last_parent_event.id
    explicit = {old_t.id, old_w.id}
    tail = []
    for old, qty, warehouse, location in (
        (old_t, -56, "TRANSIT", "transit"),
        (old_w, 56, "WORKSHOP", "workshop"),
    ):
        predecessor = old
        if chain:
            intermediate = sle(target.physical_import_batch, f"middle-{location}",
                               -60 if qty < 0 else 60, warehouse)
            db.add(models.StockLedgerFactSupersession(
                old_sle_id=old.id, new_sle_id=intermediate.id,
                import_batch_id=target.physical_import_batch_id,
            ))
            tail.append(event("transfer_posted", intermediate,
                              -60 if qty < 0 else 60, warehouse, location,
                              f"middle-{location}-event"))
            explicit.add(intermediate.id)
            predecessor = intermediate
        replacement = None if tombstone else sle(
            target.physical_import_batch, f"new-{location}", qty, warehouse,
        )
        db.add(models.StockLedgerFactSupersession(
            old_sle_id=predecessor.id,
            new_sle_id=None if replacement is None else replacement.id,
            import_batch_id=target.physical_import_batch_id,
        ))
        if replacement is not None:
            tail.append(event("transfer_posted", replacement, qty, warehouse,
                              location, f"new-{location}-event"))
            explicit.add(replacement.id)
    db.flush()
    return parent, target, workshop, transit, explicit, tail


@pytest.mark.parametrize("chain", [False, True], ids=["direct", "transient-chain"])
def test_bounded_custody_revision_refolds_both_warehouse_legs(db_session, chain):
    parent, target, workshop, transit, explicit, tail = _custody_revision_world(
        db_session, chain=chain,
    )
    count = apply_bounded_current_material_custody_events(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        source_sle_ids=tuple(explicit),
    )
    assert count == len(tail)
    assert Decimal(str(transit.reserved_qty)) == Decimal("56")
    assert Decimal(str(workshop.reserved_qty)) == Decimal("56")
    handoff_current_material_custody_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
    )
    _accept(db_session, target)
    assert apply_bounded_current_material_custody_events(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        source_sle_ids=tuple(explicit),
    ) == 0


def test_bounded_custody_revision_tombstone_refolds_without_event_tail(db_session):
    parent, target, workshop, transit, explicit, tail = _custody_revision_world(
        db_session, tombstone=True,
    )
    assert not tail
    assert apply_bounded_current_material_custody_events(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        source_sle_ids=tuple(explicit),
    ) == 0
    assert Decimal(str(transit.reserved_qty)) == Decimal("112")
    assert db_session.get(models.ProductionMaterialCustodyProjection, workshop.id) is None


def test_bounded_custody_revision_requires_complete_physical_basis(db_session):
    parent, target, workshop, transit, explicit, _tail = _custody_revision_world(db_session)
    old_source = db_session.query(models.ProductionMaterialCustodyEvent).filter(
        models.ProductionMaterialCustodyEvent.id
        <= db_session.get(models.ProductionMaterialCustodyProjectionManifest, parent.id)
        .source_event_high_watermark_id,
        models.ProductionMaterialCustodyEvent.source_sle_id.isnot(None),
    ).first().source_sle_id
    with pytest.raises(PhysicalRefreshProvenanceUnavailable,
                       match="correction chain is absent"):
        apply_bounded_current_material_custody_events(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
            source_sle_ids=tuple(explicit - {int(old_source)}),
        )
    assert Decimal(str(transit.reserved_qty)) == Decimal("48")
    assert Decimal(str(workshop.reserved_qty)) == Decimal("64")


def _replace_revision_with_exact_reimport(db):
    parent, target, workshop, transit, explicit, tail = _custody_revision_world(db)
    for event in tail:
        replacement = db.get(models.StockLedgerEntry, event.source_sle_id)
        edge = db.query(models.StockLedgerFactSupersession).filter_by(
            new_sle_id=replacement.id,
        ).one()
        original = db.get(models.StockLedgerEntry, edge.old_sle_id)
        replacement.source_content_hash = original.source_content_hash
        replacement.qty = original.qty
        db.delete(event)
    db.flush()
    return parent, target, workshop, transit, explicit


def test_bounded_custody_exact_reimport_keeps_original_events_and_balances(db_session):
    parent, target, workshop, transit, explicit = _replace_revision_with_exact_reimport(db_session)
    original_event_ids = [row.id for row in db_session.query(models.ProductionMaterialCustodyEvent)]
    original_row_ids = (workshop.id, transit.id)
    assert apply_bounded_current_material_custody_events(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        source_sle_ids=tuple(explicit),
    ) == 0
    assert Decimal(str(transit.reserved_qty)) == Decimal("48")
    assert Decimal(str(workshop.reserved_qty)) == Decimal("64")
    assert (workshop.id, transit.id) == original_row_ids
    assert [row.id for row in db_session.query(models.ProductionMaterialCustodyEvent)] == original_event_ids
    handoff_current_material_custody_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
    )
    _accept(db_session, target)
    assert load_compact_current_material_custody(db_session, consumer="exact-reimport")[0] == target.id


def _unpublished_tail_reimport_world(db):
    parent, target, workshop, transit, explicit = _replace_revision_with_exact_reimport(db)
    opening = db.query(models.ProductionMaterialCustodyEvent).filter_by(
        source_kind="issue_created",
    ).one()
    manifest = db.get(models.ProductionMaterialCustodyProjectionManifest, parent.id)
    manifest.source_event_high_watermark_id = opening.id
    transit.source_event_high_watermark_id = opening.id
    transit.reserved_qty = Decimal("112")
    product_id, component_id = workshop.product_id, workshop.component_item_id
    db.delete(workshop)
    db.flush()
    return parent, target, transit, explicit, product_id, component_id


def test_bounded_custody_unpublished_tail_exact_reimport_folds_once(db_session):
    parent, target, transit, explicit, product_id, component_id = _unpublished_tail_reimport_world(db_session)
    event_ids = [row.id for row in db_session.query(models.ProductionMaterialCustodyEvent)]
    assert apply_bounded_current_material_custody_events(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        source_sle_ids=tuple(explicit),
    ) == 2
    workshop = db_session.query(models.ProductionMaterialCustodyProjection).filter_by(
        product_id=product_id, component_item_id=component_id,
        location_kind="workshop", is_current=True,
    ).one()
    assert transit.reserved_qty == Decimal("48")
    assert workshop.reserved_qty == Decimal("64")
    assert [row.id for row in db_session.query(models.ProductionMaterialCustodyEvent)] == event_ids
    assert apply_bounded_current_material_custody_events(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        source_sle_ids=tuple(explicit),
    ) == 0
    assert transit.reserved_qty == Decimal("48")
    assert workshop.reserved_qty == Decimal("64")
    handoff_current_material_custody_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
    )
    _accept(db_session, target)
    assert load_compact_current_material_custody(db_session, consumer="tail-reimport")[0] == target.id


@pytest.mark.parametrize("mutation", ["qty", "hash", "missing_successor"])
def test_bounded_custody_unpublished_tail_correction_requires_new_event(db_session, mutation):
    parent, target, transit, explicit, _product_id, _component_id = _unpublished_tail_reimport_world(db_session)
    replacement = db_session.query(models.StockLedgerEntry).filter(
        models.StockLedgerEntry.ingest_batch_id == target.physical_import_batch_id,
        models.StockLedgerEntry.movement_kind == "transfer_out",
    ).one()
    if mutation == "qty":
        replacement.qty = Decimal("-56")
    elif mutation == "hash":
        replacement.source_content_hash = "corrected-without-event"
    else:
        explicit.remove(replacement.id)
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable,
                       match="no event for target-visible transfer|tail correction chain is absent|lacks a bounded supersession"):
        apply_bounded_current_material_custody_events(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
            source_sle_ids=tuple(explicit),
        )
    assert transit.reserved_qty == Decimal("112")
    assert db_session.query(models.ProductionMaterialCustodyProjection).filter_by(
        location_kind="workshop", is_current=True,
    ).count() == 0


@pytest.mark.parametrize("mutation", ["qty", "hash", "warehouse", "movement", "posting"])
def test_bounded_custody_reimport_requires_identical_physical_fact(db_session, mutation):
    parent, target, workshop, transit, explicit = _replace_revision_with_exact_reimport(db_session)
    replacement = db_session.query(models.StockLedgerEntry).filter(
        models.StockLedgerEntry.ingest_batch_id == target.physical_import_batch_id,
        models.StockLedgerEntry.movement_kind == "transfer_out",
    ).one()
    if mutation == "qty":
        replacement.qty = Decimal("-56")
    elif mutation == "hash":
        replacement.source_content_hash = "corrected-hash"
    elif mutation == "warehouse":
        replacement.warehouse_ref1c = "FOREIGN"
    elif mutation == "movement":
        replacement.movement_kind = "assembly_out"
    else:
        replacement.posting_at += timedelta(seconds=1)
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable,
                       match="no event for target-visible transfer"):
        apply_bounded_current_material_custody_events(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
            source_sle_ids=tuple(explicit),
        )
    assert Decimal(str(transit.reserved_qty)) == Decimal("48")
    assert Decimal(str(workshop.reserved_qty)) == Decimal("64")


def test_bounded_custody_revision_rejects_mismatched_compact_parent(db_session):
    parent, target, workshop, transit, explicit, _tail = _custody_revision_world(db_session)
    transit.reserved_qty = Decimal("47.999")
    with pytest.raises(PhysicalRefreshProvenanceUnavailable, match="parent basis differs"):
        apply_bounded_current_material_custody_events(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
            source_sle_ids=tuple(explicit),
        )


def test_bounded_custody_revision_requires_retained_early_baseline(db_session):
    parent, target, _workshop, _transit, explicit, _tail = _custody_revision_world(db_session)
    baseline = db_session.query(models.ProductionMaterialCustodyProjectionManifest).filter(
        models.ProductionMaterialCustodyProjectionManifest.is_baseline.is_(True),
    ).one()
    baseline.status = "building"
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable,
                       match="no canonical bounded replay basis"):
        apply_bounded_current_material_custody_events(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
            source_sle_ids=tuple(explicit),
        )


def test_bounded_custody_revision_rejects_missing_lineage_edge(db_session):
    parent, target, workshop, transit, explicit, _tail = _custody_revision_world(db_session)
    old = db_session.query(models.StockLedgerEntry).filter(
        models.StockLedgerEntry.source_content_hash == "old-transit",
    ).one()
    edge = db_session.query(models.StockLedgerFactSupersession).filter_by(
        old_sle_id=old.id,
    ).one()
    db_session.delete(edge)
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable,
                       match="lacks a bounded supersession"):
        apply_bounded_current_material_custody_events(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
            source_sle_ids=tuple(explicit),
        )
    assert Decimal(str(transit.reserved_qty)) == Decimal("48")
    assert Decimal(str(workshop.reserved_qty)) == Decimal("64")


def test_bounded_custody_revision_rejects_future_tail(db_session):
    parent, target, _workshop, _transit, explicit, tail = _custody_revision_world(db_session)
    tail[0].effective_at = target.cutoff.replace(tzinfo=None) + timedelta(seconds=1)
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable,
                       match="extends beyond the target cutoff"):
        apply_bounded_current_material_custody_events(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
            source_sle_ids=tuple(explicit),
        )


def test_bounded_custody_revision_rejects_missing_terminal_event(db_session):
    parent, target, workshop, transit, explicit, tail = _custody_revision_world(db_session)
    db_session.delete(tail[0])
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable,
                       match="no event for target-visible transfer"):
        apply_bounded_current_material_custody_events(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
            source_sle_ids=tuple(explicit),
        )
    assert Decimal(str(transit.reserved_qty)) == Decimal("48")
    assert Decimal(str(workshop.reserved_qty)) == Decimal("64")


def test_bounded_custody_revision_uses_canonical_terminal_clipping(db_session):
    parent, target, workshop, _transit, explicit, tail = _custody_revision_world(
        db_session, clipped=True,
    )
    outside = models.ProductionMaterialCustodyProjection(
        ledger_generation_id=parent.id, product_id=workshop.product_id,
        component_item_id=workshop.component_item_id,
        location_kind="workshop", warehouse_ref1c="OUTSIDE",
        reserved_qty=Decimal("7"),
        source_event_high_watermark_id=db_session.get(
            models.ProductionMaterialCustodyProjectionManifest, parent.id,
        ).source_event_high_watermark_id,
        is_current=True,
    )
    db_session.add(outside)
    db_session.flush()
    assert apply_bounded_current_material_custody_events(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        source_sle_ids=tuple(explicit),
    ) == len(tail)
    assert Decimal(str(workshop.reserved_qty)) == Decimal("56")
    assert Decimal(str(outside.reserved_qty)) == Decimal("7")
    assert not db_session.query(models.ProductionMaterialCustodyProjection.id).filter(
        models.ProductionMaterialCustodyProjection.is_current.is_(True),
        models.ProductionMaterialCustodyProjection.location_kind == "transit",
        models.ProductionMaterialCustodyProjection.warehouse_ref1c == "TRANSIT",
    ).first()


def _accept(db, generation: models.LedgerGeneration) -> None:
    generation.status = "accepted"
    generation.accepted_at = generation.cutoff
    db.get(models.PlanningTruthState, 1).current_generation_id = generation.id
    db.flush()


def test_custody_handoff_stamps_target_cutoff_for_the_next_bounded_generation(db_session):
    """Two consecutive bounded publications must leave a resolvable baseline."""
    parent, first, _current, _custody = _world(db_session)
    handoff_current_material_custody_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=first.id,
    )
    _accept(db_session, first)

    second = _building_generation(
        db_session, key="handoff-second", cutoff=first.cutoff + timedelta(hours=13),
    )
    handoff_current_material_custody_provenance(
        db_session, parent_generation_id=first.id, target_generation_id=second.id,
    )
    _accept(db_session, second)

    manifest = db_session.get(
        models.ProductionMaterialCustodyProjectionManifest, second.id
    )
    assert manifest is not None
    # The canonical gate every later candidate runs on its parent manifest.
    _require_manifest_cutoff(manifest, second)
    assert _same_1c_timestamp(manifest.cutoff, second.cutoff)
    assert not _same_1c_timestamp(manifest.cutoff, first.cutoff)

    third = _building_generation(
        db_session, key="handoff-third", cutoff=second.cutoff + timedelta(hours=1),
    )
    baseline_generation_id, baseline_manifest, baseline_generation = (
        _resolve_projection_baseline(
            db_session,
            generation=third,
            target_high_watermark_id=_event_high_watermark_id_at_cutoff(
                db_session, cutoff=third.cutoff,
            ),
        )
    )
    assert baseline_generation_id == int(second.id)
    assert int(baseline_manifest.ledger_generation_id) == int(second.id)
    assert _same_1c_timestamp(baseline_generation.cutoff, second.cutoff)


def test_custody_handoff_keeps_the_folded_watermark_on_the_target_manifest(db_session):
    parent, target, _current, custody = _world(db_session)
    target.cutoff = parent.cutoff + timedelta(hours=6)
    target.physical_import_batch.cutoff = target.cutoff
    db_session.flush()
    sle = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="watermark-tail-sle",
        business_identity="watermark-tail-sle",
        item_id=int(custody.component_item_id),
        characteristic_ref="",
        organization_ref="org",
        warehouse_ref1c="WH",
        qty=Decimal("1"),
        posting_at=parent.cutoff + timedelta(hours=1),
        record_type="Receipt",
        movement_kind="transfer_in",
        recorder_type="Document_Transfer",
        recorder_ref="watermark-tail",
        line_no="1",
        ingest_source="test",
    )
    db_session.add(sle)
    db_session.flush()
    event = models.ProductionMaterialCustodyEvent(
        product_id=int(custody.product_id),
        component_item_id=int(custody.component_item_id),
        source_kind="transfer_posted",
        source_sle_id=int(sle.id),
        effective_at=parent.cutoff + timedelta(hours=1),
        location_kind="workshop",
        warehouse_ref1c="WH",
        delta_qty=Decimal("1"),
        idempotency_key="watermark-tail-event",
    )
    db_session.add(event)
    db_session.flush()

    apply_bounded_current_material_custody_events(
        db_session,
        parent_generation_id=parent.id,
        target_generation_id=target.id,
        source_sle_ids=(int(sle.id),),
    )
    result = handoff_current_material_custody_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
    )
    _accept(db_session, target)

    manifest = db_session.get(
        models.ProductionMaterialCustodyProjectionManifest, target.id
    )
    _require_manifest_cutoff(manifest, target)
    expected = _event_high_watermark_id_at_cutoff(db_session, cutoff=target.cutoff)
    assert expected == int(event.id)
    assert int(manifest.source_event_high_watermark_id) == expected
    assert int(result.custody_event_watermark) == expected
    assert int(custody.source_event_high_watermark_id) == expected


def test_custody_handoff_refuses_stale_cell_watermark_and_backwards_cutoff(db_session):
    parent, target, _current, custody = _world(db_session)
    custody.source_event_high_watermark_id = 7
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable, match="watermark is mixed or stale"):
        handoff_current_material_custody_provenance(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        )
    db_session.rollback()

    parent, target, _current, custody = _world(db_session)
    target.cutoff = parent.cutoff - timedelta(hours=1)
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable, match="backwards"):
        handoff_current_material_custody_provenance(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        )


def test_future_supply_handoff_keeps_row_and_emits_no_change_audit(db_session):
    parent, target, current, _custody = _world(db_session)
    row_id = int(current.id)
    result = handoff_current_future_supply_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
    )
    assert result.future_supply_rows == 1
    assert int(current.id) == row_id
    assert int(current.source_generation_id) == int(target.id)
    assert db_session.query(models.LedgerFutureSupplyCurrentChange).count() == 0
    assert db_session.query(models.LedgerFutureSupply).count() == 0
    target.status = "accepted"
    target.accepted_at = target.cutoff
    db_session.get(models.PlanningTruthState, 1).current_generation_id = target.id
    db_session.flush()
    repeated = handoff_current_future_supply_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
    )
    assert repeated.future_supply_idempotent is True


def test_custody_handoff_keeps_projection_id_and_current_loader_after_pointer(db_session):
    parent, target, _current, custody = _world(db_session)
    row_id = int(custody.id)
    result = handoff_current_material_custody_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
    )
    assert result.custody_rows == 1
    assert int(custody.id) == row_id
    assert int(custody.ledger_generation_id) == int(target.id)
    assert db_session.get(models.ProductionMaterialCustodyProjectionManifest, parent.id) is None
    assert db_session.get(models.ProductionMaterialCustodyProjectionManifest, target.id) is not None
    target.status = "accepted"
    target.accepted_at = target.cutoff
    db_session.get(models.PlanningTruthState, 1).current_generation_id = target.id
    db_session.flush()
    generation_id, state = load_compact_current_material_custody(
        db_session, consumer="handoff-test"
    )
    assert generation_id == int(target.id)
    assert state.by_warehouse_item[("WH", int(custody.component_item_id))] == 2.0
    repeated = handoff_current_material_custody_provenance(
        db_session, parent_generation_id=parent.id, target_generation_id=target.id,
    )
    assert repeated.custody_idempotent is True


def test_handoff_rejects_event_tail_and_mixed_future_owner(db_session):
    parent, target, current, _custody = _world(db_session)
    current.source_generation_id = target.id
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable, match="mixed|stale"):
        handoff_current_future_supply_provenance(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        )


def test_custody_handoff_rejects_unpublished_event_tail(db_session):
    parent, target, _current, custody = _world(db_session)
    db_session.add(models.ProductionMaterialCustodyEvent(
        product_id=int(custody.product_id),
        component_item_id=int(custody.component_item_id),
        source_kind="issue_created", effective_at=parent.cutoff,
        location_kind="workshop", warehouse_ref1c="WH", delta_qty=Decimal("1"),
        idempotency_key="handoff-tail",
    ))
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable, match="event tail"):
        handoff_current_material_custody_provenance(
            db_session, parent_generation_id=parent.id, target_generation_id=target.id,
        )


def test_bounded_custody_tail_folds_only_explicit_sle_and_handoff_reuses_it(db_session):
    parent, target, _current, custody = _world(db_session)
    sle = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="custody-tail-sle",
        business_identity="custody-tail-sle",
        item_id=int(custody.component_item_id),
        characteristic_ref="",
        organization_ref="org",
        warehouse_ref1c="WH",
        qty=Decimal("1"),
        posting_at=target.cutoff,
        record_type="Receipt",
        movement_kind="transfer_in",
        recorder_type="Document_Transfer",
        recorder_ref="custody-tail",
        line_no="1",
        ingest_source="test",
    )
    db_session.add(sle)
    db_session.flush()
    event = models.ProductionMaterialCustodyEvent(
        product_id=int(custody.product_id),
        component_item_id=int(custody.component_item_id),
        source_kind="transfer_posted",
        source_sle_id=int(sle.id),
        effective_at=target.cutoff,
        location_kind="workshop",
        warehouse_ref1c="WH",
        delta_qty=Decimal("1"),
        idempotency_key="bounded-custody-tail",
    )
    db_session.add(event)
    db_session.flush()

    folded = apply_bounded_current_material_custody_events(
        db_session,
        parent_generation_id=parent.id,
        target_generation_id=target.id,
        source_sle_ids=(int(sle.id),),
    )
    assert folded == 1
    assert custody.reserved_qty == Decimal("3")
    assert custody.source_event_high_watermark_id == event.id
    result = handoff_current_material_custody_provenance(
        db_session,
        parent_generation_id=parent.id,
        target_generation_id=target.id,
    )
    assert result.custody_event_watermark == int(event.id)
    assert db_session.get(
        models.ProductionMaterialCustodyProjectionManifest, target.id
    ).source_event_high_watermark_id == event.id


def test_bounded_custody_tail_rejects_event_outside_explicit_sle_manifest(db_session):
    parent, target, _current, custody = _world(db_session)
    db_session.add(models.ProductionMaterialCustodyEvent(
        product_id=int(custody.product_id),
        component_item_id=int(custody.component_item_id),
        source_kind="transfer_posted",
        source_sle_id=999999,
        effective_at=target.cutoff,
        location_kind="workshop",
        warehouse_ref1c="WH",
        delta_qty=Decimal("1"),
        idempotency_key="bounded-custody-foreign-tail",
    ))
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable, match="not covered"):
        apply_bounded_current_material_custody_events(
            db_session,
            parent_generation_id=parent.id,
            target_generation_id=target.id,
            source_sle_ids=(),
        )


def _canonical_backfill_world(db):
    parent, target, _current, custody = _world(db)
    target.cutoff = parent.cutoff + timedelta(days=1)
    target.physical_import_batch.cutoff = target.cutoff
    target.physical_import_batch.source_watermarks = {
        "source": "AccumulationRegister_ЗапасыНаСкладах",
        "recorder_type": "Document_ПеремещениеЗапасов",
        "recorder_ref": "transfer-backfill",
        # This is the recorder's old revision, not the global parent boundary.
        "previous_import_batch_id": 0,
    }
    product = db.get(models.ProductionProduct, int(custody.product_id))
    issue = models.ProductionMaterialIssue(
        document_number="ISSUE-BACKFILL", product_id=product.product_id,
        order_id=product.order_id, status="requested", direction="issue",
        source_warehouse_ref1c="ISSUE-SOURCE",
        warehouse_ref1c="WORKSHOP",
    )
    db.add(issue)
    db.flush()
    line = models.ProductionMaterialIssueLine(
        issue_id=issue.issue_id, component_item_id=custody.component_item_id,
        required_qty=Decimal("4"), issued_qty=Decimal("4"),
        custody_event_revision=2,
    )
    db.add(line)
    db.flush()
    db.add(models.SyncLink(
        source_system="PRODPLAN", source_doctype="material_issue",
        source_id=issue.issue_id, target_entity="Document_ПеремещениеЗапасов",
        target_ref_key="transfer-backfill",
    ))
    db.add(models.LedgerBuildBatch(
        ledger_generation_id=target.id, stage="physical_import",
        batch_key="backfill-audit", status="completed", algorithm_version="test",
        metrics={"parent_physical_import_batch_id": parent.physical_import_batch_id,
                 "physical_import_batch_id": target.physical_import_batch_id,
                 "recorders": [{"recorder_type": "Document_ПеремещениеЗапасов",
                                "recorder_ref": "transfer-backfill"}]},
    ))
    sle = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="backfill-physical", business_identity="backfill-physical",
        item_id=custody.component_item_id, characteristic_ref="",
        organization_ref="org", warehouse_ref1c="ACTUAL-SOURCE",
        qty=Decimal("-4"), posting_at=target.cutoff,
        record_type="Expense", movement_kind="transfer_out",
        recorder_type="Document_ПеремещениеЗапасов",
        recorder_ref="transfer-backfill", line_no="1", ingest_source="pull",
        active=True,
    )
    db.add(sle)
    db.flush()
    opening = models.ProductionMaterialCustodyEvent(
        issue_id=issue.issue_id, product_id=product.product_id,
        component_item_id=line.component_item_id, source_kind="issue_created",
        source_sle_id=None, effective_at=target.cutoff,
        location_kind="transit", warehouse_ref1c="ACTUAL-SOURCE",
        source_ref1c="ISSUE-SOURCE", source_ref2c="transfer-backfill",
        delta_qty=Decimal("4"), document_number=issue.document_number,
        document_line_no=str(line.line_id),
        idempotency_key=_custody_event_idempotency_key(
            issue_id=issue.issue_id, line_id=line.line_id, revision=1,
            source_kind="issue_created", location_kind="transit",
            warehouse_ref1c="ACTUAL-SOURCE", delta_qty=4,
            source_sle_id=None,
        ),
    )
    posted = models.ProductionMaterialCustodyEvent(
        issue_id=issue.issue_id, product_id=product.product_id,
        component_item_id=line.component_item_id, source_kind="transfer_posted",
        source_sle_id=sle.id, effective_at=target.cutoff,
        location_kind="transit", warehouse_ref1c="ACTUAL-SOURCE",
        source_ref1c="ISSUE-SOURCE", source_ref2c="transfer-backfill",
        delta_qty=Decimal("-4"), document_number=issue.document_number,
        document_line_no=str(line.line_id), idempotency_key="backfill-posted",
    )
    db.add_all([opening, posted])
    db.flush()
    return parent, target, sle, opening, posted, custody


@pytest.mark.parametrize("shared_batch", [False, True])
def test_canonical_issue_backfill_passes_both_custody_gates_and_publisher(db_session, shared_batch):
    parent, target, sle, opening, posted, custody = _canonical_backfill_world(db_session)
    if shared_batch:
        target.physical_import_batch.source_watermarks = {
            **target.physical_import_batch.source_watermarks,
            "recorder_ref": "last-unrelated-recorder",
            "recorders": [{"recorder_type": sle.recorder_type,
                           "recorder_ref": sle.recorder_ref, "status": "done"}],
        }
        db_session.flush()
    assert workflow._bounded_custody_tail_sle_ids(
        db_session, after_event_id=0, parent_generation_id=parent.id,
        target_generation_id=target.id, target_cutoff=target.cutoff,
    ) == (sle.id,)
    assert apply_bounded_current_material_custody_events(
        db_session, parent_generation_id=parent.id,
        target_generation_id=target.id, source_sle_ids=(sle.id,),
    ) == 2
    assert custody.reserved_qty == Decimal("2")
    assert custody.source_event_high_watermark_id == posted.id
    assert handoff_current_material_custody_provenance(
        db_session, parent_generation_id=parent.id,
        target_generation_id=target.id,
    ).custody_event_watermark == posted.id


@pytest.mark.parametrize("mutation", [
    "foreign_link", "wrong_qty", "wrong_bucket", "wrong_product",
    "reversal", "future", "incomplete_batch", "bad_key", "foreign_batch",
    "missing_recorder", "incomplete_recorder", "malformed_manifest",
])
def test_canonical_issue_backfill_rejects_forged_or_unbounded_tail(db_session, mutation):
    parent, target, sle, opening, posted, _custody = _canonical_backfill_world(db_session)
    if mutation == "foreign_link":
        opening.source_ref2c = "other-transfer"
    elif mutation == "wrong_qty":
        opening.delta_qty = Decimal("5")
    elif mutation == "wrong_bucket":
        opening.warehouse_ref1c = "OTHER-SOURCE"
    elif mutation == "wrong_product":
        opening.product_id += 1
    elif mutation == "reversal":
        opening.source_kind = "transfer_returned"
    elif mutation == "future":
        sle.posting_at = target.cutoff + timedelta(seconds=1)
    elif mutation == "incomplete_batch":
        target.physical_import_batch.source_complete = False
    elif mutation == "bad_key":
        opening.idempotency_key = "forged"
    elif mutation == "foreign_batch":
        target.physical_import_batch.source_watermarks = {"source": "foreign"}
    elif mutation in {"missing_recorder", "incomplete_recorder", "malformed_manifest"}:
        target.physical_import_batch.source_watermarks = {
            **target.physical_import_batch.source_watermarks,
            "recorders": None if mutation == "malformed_manifest" else [
                {"recorder_type": sle.recorder_type,
                 "recorder_ref": "foreign" if mutation == "missing_recorder" else sle.recorder_ref,
                 "status": "failed" if mutation == "incomplete_recorder" else "done"},
            ],
        }
    db_session.flush()
    with pytest.raises((workflow.PhysicalRefreshOrchestratorError,
                        PhysicalRefreshProvenanceUnavailable)):
        workflow._bounded_custody_tail_sle_ids(
            db_session, after_event_id=0, parent_generation_id=parent.id,
            target_generation_id=target.id, target_cutoff=target.cutoff,
        )


def test_discard_removes_only_candidate_proven_backfill_opening(db_session):
    from app.services.item_ledger.physical_refresh_discard import discard_physical_refresh_candidate

    parent, target, sle, opening, posted, custody = _canonical_backfill_world(db_session)
    local = models.ProductionMaterialCustodyEvent(
        issue_id=opening.issue_id, product_id=opening.product_id,
        component_item_id=opening.component_item_id, source_kind="issue_created",
        effective_at=target.cutoff, location_kind="transit",
        warehouse_ref1c=opening.warehouse_ref1c, delta_qty=Decimal("7"),
        idempotency_key="operator-opening", source_ref2c=None,
    )
    db_session.add(local)
    db_session.commit()
    retained_qty = custody.reserved_qty
    result = discard_physical_refresh_candidate(
        db_session, ledger_generation_id=target.id, reason="failed publication",
    )
    db_session.flush()
    assert result.deleted_custody_events == 2
    assert [row.id for row in db_session.query(models.ProductionMaterialCustodyEvent)] == [local.id]
    assert custody.reserved_qty == retained_qty
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id


def test_discard_retains_backfill_proven_by_accepted_physical_fact(db_session):
    from app.services.item_ledger.physical_refresh_discard import discard_physical_refresh_candidate

    parent, target, sle, opening, posted, custody = _canonical_backfill_world(db_session)
    sle.ingest_batch_id = parent.physical_import_batch_id
    sle.posting_at = parent.cutoff
    opening.effective_at = parent.cutoff
    posted.effective_at = parent.cutoff
    parent.physical_import_batch.source_watermarks = target.physical_import_batch.source_watermarks
    db_session.commit()
    result = discard_physical_refresh_candidate(
        db_session, ledger_generation_id=target.id, reason="failed publication",
    )
    assert result.deleted_custody_events == 0
    assert db_session.query(models.ProductionMaterialCustodyEvent).count() == 2

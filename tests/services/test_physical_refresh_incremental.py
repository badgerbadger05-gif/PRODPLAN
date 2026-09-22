from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import models
from app.services.item_ledger import physical_refresh_current_publish as publisher
from app.services.item_ledger import physical_refresh_orchestrator as workflow
from app.services.item_ledger import physical_refresh_supplier_evidence as evidence_adapter
from app.services.item_ledger.supplier_receipt_allocation import (
    RECEIPT_OPERATION,
    SupplierDocumentEvidence,
)
from app.services.item_ledger.supplier_receipt_odata import (
    SupplierEvidenceExtractionResult,
)


def _parent(db_session):
    cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key="incremental-parent-batch",
        status="completed",
        source_complete=True,
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    parent = models.LedgerGeneration(
        generation_key="incremental-parent",
        status="accepted",
        cutoff=cutoff,
        source_watermarks={"replay_from": "2026-08-01T00:00:00+00:00"},
        capabilities={"physical_ledger": True},
        physical_import_batch=batch,
        algorithm_version="accepted/1",
        accepted_at=cutoff,
    )
    db_session.add_all([batch, parent])
    db_session.flush()
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    db_session.commit()
    return parent, batch


def test_import_checkpoint_evidence_is_zero_for_a_true_noop():
    physical_import = type("Import", (), {"movements_inserted": 0})()
    audit = type("Audit", (), {"changed_recorders": 0})()
    evidence = workflow._physical_refresh_evidence(
        physical_import, audit, database_ledger_rows=100_000
    )
    assert evidence["input_delta_rows"] == 0
    assert evidence["affected_scopes"] == ()


def test_import_checkpoint_evidence_marks_changed_and_backdated_refresh():
    physical_import = type("Import", (), {"movements_inserted": 2})()
    audit = type(
        "Audit", (), {"changed_recorders": 1, "backdated_recorders": 1}
    )()
    evidence = workflow._physical_refresh_evidence(
        physical_import, audit, database_ledger_rows=100_001
    )
    assert evidence["input_delta_rows"] == 2
    assert evidence["backdated"] is True


def test_delta_reader_reports_stable_current_scope_without_mutating_owner(db_session):
    parent, _parent_batch = _parent(db_session)
    child_batch = models.PhysicalImportBatch(
        batch_key="incremental-child-batch", status="completed",
        cutoff=parent.cutoff + timedelta(days=1), source_watermarks={},
        completed_at=parent.cutoff + timedelta(days=1),
    )
    child = models.LedgerGeneration(
        generation_key="incremental-child", status="building",
        cutoff=parent.cutoff + timedelta(days=1),
        source_watermarks={}, capabilities={}, physical_import_batch=child_batch,
        algorithm_version="test", replay_version="test",
    )
    item = models.Item(item_code="DELTA-ITEM", item_name="Delta item")
    run = models.PlanningRun(status="FIXED_SNAPSHOT", ledger_generation_id=parent.id)
    db_session.add_all([child_batch, child, item, run])
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id, item_id=item.item_id,
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
    )
    db_session.add(requirement)
    db_session.flush()
    owner = models.ReservationEntry(
        ledger_generation_id=parent.id, item_id=item.item_id,
        run_id=run.run_id, requirement_id=requirement.id,
        priority_period_from=date(2026, 9, 1), priority_period_to=date(2026, 9, 30),
        realization_mode="make", reserved_qty=Decimal("5"),
        replenishment_required_qty=Decimal("5"), current_identity="delta-owner",
        lifecycle_status="active", owner_kind="current", is_current=True,
    )
    db_session.add(owner)
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=child_batch.id, source_content_hash="delta-sle",
        business_identity="delta-business", item_id=item.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("3"), posting_at=parent.cutoff + timedelta(hours=1),
        record_type="Receipt", movement_kind="assembly_in",
        recorder_type="Production", recorder_ref="delta-rec", line_no="1",
        ingest_source="pull",
    ))
    db_session.commit()
    sle = db_session.query(models.StockLedgerEntry).filter_by(
        source_content_hash="delta-sle"
    ).one()
    delta = workflow._physical_refresh_delta_rows(
        db_session,
        parent=parent,
        target=child,
        physical_import=type("Import", (), {"physical_import_batch_id": child_batch.id})(),
        recorder_audit=type("Audit", (), {})(),
    )
    assert delta["input_delta_rows"] == 1
    assert delta["affected_scopes"] == (f"{item.item_id}:::default:make",)
    assert owner.replenishment_received_qty == Decimal("0.000")
    assert owner.ledger_generation_id == parent.id
    assert db_session.query(models.ReservationEntry).filter(
        models.ReservationEntry.ledger_generation_id == child.id
    ).count() == 0
    assert db_session.query(models.ReservationEvent).filter_by(sle_id=sle.id).count() == 0


def test_delta_reader_uses_batch_interval_and_rejects_backdated_supersession(db_session):
    parent, parent_batch = _parent(db_session)
    child_batch = models.PhysicalImportBatch(
        batch_key="incremental-supersession-batch", status="completed",
        cutoff=parent.cutoff + timedelta(days=1), source_watermarks={},
        completed_at=parent.cutoff + timedelta(days=1),
    )
    item = models.Item(item_code="REVISION-ITEM", item_name="Revision item")
    db_session.add_all([child_batch, item])
    db_session.flush()
    old = models.StockLedgerEntry(
        ingest_batch_id=parent_batch.id, source_content_hash="old-row",
        business_identity="revision-business", item_id=item.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=parent.cutoff,
        record_type="Receipt", movement_kind="assembly_in",
        recorder_type="Production", recorder_ref="revision-rec", line_no="1",
        ingest_source="pull",
    )
    new = models.StockLedgerEntry(
        ingest_batch_id=child_batch.id, source_content_hash="new-row",
        business_identity="revision-business", item_id=item.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("2"), posting_at=parent.cutoff - timedelta(hours=1),
        record_type="Receipt", movement_kind="assembly_in",
        recorder_type="Production", recorder_ref="revision-rec", line_no="1",
        ingest_source="pull",
    )
    db_session.add_all([old, new])
    db_session.flush()
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=old.id, new_sle_id=new.id, import_batch_id=child_batch.id,
    ))
    db_session.commit()
    delta = workflow._physical_refresh_delta_rows(
        db_session,
        parent=parent,
        target=parent,
        physical_import=type("Import", (), {"physical_import_batch_id": child_batch.id})(),
        recorder_audit=type("Audit", (), {})(),
    )
    assert delta["input_delta_rows"] == 2
    assert delta["backdated"] is True
    assert len(delta["supersessions"]) == 1


def test_routine_refresh_defaults_disable_full_recorder_audit_and_lookback():
    import inspect

    refresh_parameters = inspect.signature(workflow.run_physical_refresh).parameters
    audit_parameters = inspect.signature(workflow.run_physical_recorder_audit).parameters
    assert refresh_parameters["discovery_lookback"].default == timedelta(0)
    assert refresh_parameters["audit_all_known_recorders"].default is False
    assert audit_parameters["discovery_lookback"].default == timedelta(0)
    assert audit_parameters["audit_all_known_recorders"].default is False


# ---------------------------------------------------------------------------
# Bounded backdate / supersession replay (CANON "Объём вычислений штатного
# физического refresh", decisions log §37).
#
# All postings below are placed at day scale around the generation cutoffs so
# the assertions hold under both timestamp conventions in play: the raw
# Moscow-local ``posting_at`` axis used by the bounded writers and the UTC axis
# used by the orchestrator boundary detection in ``_posting_at_utc``.
# ---------------------------------------------------------------------------


PARENT_CUTOFF = datetime(2026, 9, 10, tzinfo=timezone.utc)
TARGET_CUTOFF = PARENT_CUTOFF + timedelta(days=1)
BACKDATED_AT = PARENT_CUTOFF - timedelta(days=3)
FORWARD_AT = PARENT_CUTOFF + timedelta(hours=12)
POOL_BY_WAREHOUSE = {"WH-BUY": "default", "WH-MOVE": "default"}


def _generations(db_session):
    parent_batch = models.PhysicalImportBatch(
        batch_key="bounded-replay-parent", status="completed", source_complete=True,
        cutoff=PARENT_CUTOFF, source_watermarks={}, completed_at=PARENT_CUTOFF,
    )
    target_batch = models.PhysicalImportBatch(
        batch_key="bounded-replay-target", status="completed", source_complete=True,
        cutoff=TARGET_CUTOFF, source_watermarks={}, completed_at=TARGET_CUTOFF,
    )
    db_session.add_all([parent_batch, target_batch])
    db_session.flush()
    parent = models.LedgerGeneration(
        generation_key="bounded-replay-parent", status="accepted", cutoff=PARENT_CUTOFF,
        accepted_at=PARENT_CUTOFF, source_watermarks={},
        capabilities={"physical_ledger": True}, physical_import_batch=parent_batch,
        algorithm_version="bounded-replay-tests",
    )
    target = models.LedgerGeneration(
        generation_key="bounded-replay-target", status="building", cutoff=TARGET_CUTOFF,
        source_watermarks={}, capabilities={}, physical_import_batch=target_batch,
        algorithm_version="bounded-replay-tests",
    )
    db_session.add_all([parent, target])
    db_session.flush()
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    db_session.commit()
    return parent, target, parent_batch, target_batch


def _item(db_session, code):
    item = models.Item(item_code=code, item_name=code)
    db_session.add(item)
    db_session.flush()
    return item


def _sle(
    db_session, batch, item, *, qty, at, kind="transfer_in", warehouse="WH-MOVE",
    recorder_type="Document_Transfer", ref=None, line_no="1",
):
    ref = ref or "rec-" + str(item.item_code)
    row = models.StockLedgerEntry(
        ingest_batch_id=batch.id,
        source_content_hash="{0}:{1}:{2}".format(ref, line_no, qty).ljust(64, "0"),
        business_identity="{0}:{1}:{2}".format(recorder_type, ref, line_no),
        item_id=item.item_id, characteristic_ref="", organization_ref="",
        warehouse_ref1c=warehouse, qty=Decimal(str(qty)), qty_after=Decimal(str(qty)),
        posting_at=at, known_at=batch.cutoff,
        record_type="Receipt" if Decimal(str(qty)) >= 0 else "Expense",
        movement_kind=kind, recorder_type=recorder_type, recorder_ref=ref,
        line_no=line_no, ingest_source="pull", active=True,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _bin(db_session, generation, item, *, on_hand, warehouse="WH-MOVE", last_entry=None):
    row = models.StockBin(
        ledger_generation_id=generation.id, item_id=item.item_id,
        characteristic_ref="", organization_ref="", warehouse_ref1c=warehouse,
        on_hand=Decimal(str(on_hand)), is_current=True,
        last_entry_id=None if last_entry is None else int(last_entry.id),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _buy_owner(db_session, parent, item, *, required="10"):
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT", config_snapshot={}, ledger_generation_id=parent.id,
        ledger_cutoff=parent.cutoff, period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30), active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id, item_id=item.item_id, total_required_qty=Decimal(required),
        net_required_qty=Decimal(required), period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30), bom_level=0, planning_stock_pool="default",
        characteristic_ref="", organization_ref="", freeze_version=1,
    )
    db_session.add(requirement)
    db_session.flush()
    owner = models.ReservationEntry(
        ledger_generation_id=parent.id, item_id=item.item_id, run_id=run.run_id,
        freeze_version=1, requirement_id=requirement.id,
        priority_period_from=date(2026, 9, 1), priority_period_to=date(2026, 9, 30),
        realization_mode="buy", planning_stock_pool="default",
        reserved_qty=Decimal(required), replenishment_required_qty=Decimal(required),
        lifecycle_status="active",
        current_identity="reservation:req:{0}:mode:buy".format(requirement.id),
        owner_kind="current", is_current=True,
    )
    db_session.add(owner)
    db_session.flush()
    return owner, requirement


def _provenance(db_session, generation, row, *, ref, line_no="1"):
    db_session.add(models.StockLedgerSupplierReceiptProvenance(
        ledger_generation_id=generation.id, stock_ledger_entry_id=row.id,
        receipt_doc_type=row.recorder_type, receipt_doc_ref=ref,
        receipt_doc_line_no=line_no, supplier_order_ref=None,
        supplier_order_line_no=None, operation_kind="supplier_receipt",
        operation_key="test",
        operation_name="Приобретение у поставщика",
        evidence_hash="hash:{0}".format(row.id).ljust(64, "0"),
        evidence_payload={"signed_qty": str(row.qty)}, match_rule="bounded-typed",
        match_status="unmatched", ambiguity_count=0,
        reason="no exact typed supplier order line",
    ))
    db_session.flush()


def _allocation(db_session, generation, owner, requirement, row, *, qty):
    allocation = models.ReservationConsumptionAllocation(
        ledger_generation_id=generation.id, reservation_id=owner.id, sle_id=row.id,
        requirement_id=requirement.id, allocated_qty=Decimal(str(qty)),
        match_rule="fifo", item_id=row.item_id, characteristic_ref="",
        organization_ref="", planning_stock_pool="default",
        idempotency_key="alloc:{0}:{1}".format(owner.id, row.id),
        allocation_role="replenishment_receipt", is_current=True,
    )
    db_session.add(allocation)
    owner.replenishment_received_qty = Decimal(str(qty))
    db_session.flush()
    return allocation


def _receipt_evidence(row, *, ref, qty):
    return SupplierDocumentEvidence(
        receipt_doc_type=row.recorder_type, receipt_doc_ref=ref,
        receipt_doc_line_no=row.line_no, operation_key=RECEIPT_OPERATION,
        operation_name="Приобретение у поставщика",
        supplier_order_type="",
        supplier_order_ref="", supplier_order_line_no="0", item_id=row.item_id,
        characteristic_ref="", warehouse_ref1c=row.warehouse_ref1c,
        signed_qty=Decimal(str(qty)),
    )


def _patch_payloads(monkeypatch, *, evidence=()):
    """Patch only the compact payload/publication seams, never the writers.

    The bounded stock, replenishment and supplier writers under test stay real
    so the assertions prove the scoped replay rather than a stub.
    """

    class Result:
        changed_rows = 0
        idempotent = True

    monkeypatch.setattr(
        publisher, "apply_bounded_assembly_output_plan_execution",
        lambda *a, **kw: SimpleNamespace(metrics={"replayed_fact_rows": 0}),
    )
    monkeypatch.setattr(
        publisher, "apply_bounded_current_material_custody_events", lambda *a, **kw: 0,
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_assembly_payload",
        lambda *a, **kw: SimpleNamespace(
            queue_rows=(), readiness_rows=(), readiness_metrics={},
        ),
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_drum_payload",
        lambda *a, **kw: SimpleNamespace(rows=(), metrics={}),
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_shelf_payload",
        lambda *a, **kw: SimpleNamespace(rows=(), metrics={}),
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_production_control_payload",
        lambda *a, **kw: {"rows": [], "meta": {}},
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_purchase_control_payload",
        lambda *a, **kw: {"rows": [], "meta": {}},
    )
    monkeypatch.setattr(
        publisher, "handoff_current_physical_refresh_provenance", lambda *a, **kw: None,
    )
    monkeypatch.setattr(publisher, "publish_current_execution_scope", lambda *a, **kw: Result())
    monkeypatch.setattr(
        publisher, "resolve_compact_queue_owner_ids", lambda db, rows, **kw: tuple(rows),
    )
    monkeypatch.setattr(
        publisher, "_build_obligation_view_payloads", lambda *a, **kw: ({}, {}),
    )
    monkeypatch.setattr(
        publisher, "publish_current_obligation_views_from_generation",
        lambda *a, **kw: {
            "production_control_journal": Result(),
            "purchase_control_journal": Result(),
            "mrp_result": Result(),
            "period_plan_execution": Result(),
        },
    )
    monkeypatch.setattr(publisher, "publish_generation", lambda db, target, **kw: None)
    monkeypatch.setattr(publisher, "_fixed_run_ids", lambda db: ())
    monkeypatch.setattr(
        evidence_adapter, "extract_supplier_document_evidence",
        lambda db, client, rows: SupplierEvidenceExtractionResult(
            evidence=tuple(evidence), diagnostics=(),
            fetched_document_count=len(tuple(evidence)),
        ),
    )


def test_backdated_fact_refolds_only_its_own_scope(db_session, monkeypatch):
    parent, target, parent_batch, target_batch = _generations(db_session)
    touched = _item(db_session, "BOUNDED-BACKDATE")
    untouched = _item(db_session, "BOUNDED-UNTOUCHED")
    seed = _sle(
        db_session, parent_batch, touched, qty="5",
        at=PARENT_CUTOFF - timedelta(days=1),
    )
    touched_bin = _bin(db_session, parent, touched, on_hand="5", last_entry=seed)
    untouched_bin = _bin(db_session, parent, untouched, on_hand="7")
    backdated = _sle(
        db_session, target_batch, touched, qty="3", at=BACKDATED_AT, ref="late-doc",
    )
    db_session.commit()
    untouched_owner_before = int(untouched_bin.ledger_generation_id)

    _patch_payloads(monkeypatch)
    result = publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        delta_manifest={
            "rows": (backdated,), "basis_rows": (), "supersessions": (),
            "backdate_from": BACKDATED_AT,
        },
        odata_client=None,
        source_revision=target_batch.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )

    assert result.input_delta_rows == 1
    assert db_session.get(models.LedgerGeneration, target.id).status == "accepted"
    db_session.refresh(touched_bin)
    db_session.refresh(untouched_bin)
    assert touched_bin.on_hand == Decimal("8.000")
    assert int(touched_bin.ledger_generation_id) == int(target.id)
    # A backdated fact never becomes the compact owner's last visible entry.
    assert int(touched_bin.last_entry_id) == int(seed.id)
    # The untouched scope keeps its accepted owner: no rewrite, no audit churn.
    assert untouched_bin.on_hand == Decimal("7.000")
    assert int(untouched_bin.ledger_generation_id) == untouched_owner_before
    assert db_session.query(models.CurrentReplenishmentAudit).count() == 0
    db_session.rollback()


def test_backdated_fact_without_declared_boundary_stays_fail_closed(
    db_session, monkeypatch,
):
    parent, target, _parent_batch, target_batch = _generations(db_session)
    item = _item(db_session, "BOUNDED-NO-BOUNDARY")
    _bin(db_session, parent, item, on_hand="5")
    backdated = _sle(db_session, target_batch, item, qty="3", at=BACKDATED_AT)
    db_session.commit()
    _patch_payloads(monkeypatch)
    with pytest.raises(
        publisher.ForwardPhysicalRefreshUnavailable,
        match="backdate requires maintenance",
    ):
        publisher.publish_forward_physical_refresh_current(
            db_session, target_generation_id=target.id, parent_generation_id=parent.id,
            delta_manifest={"rows": (backdated,), "supersessions": ()},
            odata_client=None, source_revision=target_batch.id,
            planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
        )
    db_session.rollback()
    assert db_session.get(models.LedgerGeneration, target.id).status == "building"


def _superseded_receipt_world(db_session, *, warehouse="WH-BUY"):
    parent, target, parent_batch, target_batch = _generations(db_session)
    item = _item(db_session, "BOUNDED-SUPERSEDED")
    other = _item(db_session, "BOUNDED-OTHER-BUY")
    owner, requirement = _buy_owner(db_session, parent, item)
    other_owner, other_requirement = _buy_owner(db_session, parent, other)
    old = _sle(
        db_session, parent_batch, item, qty="6", at=BACKDATED_AT, kind="receipt",
        warehouse=warehouse, recorder_type="Document_ПриходнаяНакладная",
        ref="receipt-1",
    )
    _provenance(db_session, parent, old, ref="receipt-1")
    _allocation(db_session, parent, owner, requirement, old, qty="6")
    other_row = _sle(
        db_session, parent_batch, other, qty="2", at=BACKDATED_AT, kind="receipt",
        warehouse=warehouse, recorder_type="Document_ПриходнаяНакладная",
        ref="receipt-other",
    )
    _provenance(db_session, parent, other_row, ref="receipt-other")
    # The fork carries the parent's typed evidence onto the candidate, so the
    # generation that is about to become the pointer owns it.  Without this the
    # world is the very defect the acceptance gate now refuses to publish.
    _provenance(db_session, target, old, ref="receipt-1")
    _provenance(db_session, target, other_row, ref="receipt-other")
    other_allocation = _allocation(
        db_session, parent, other_owner, other_requirement, other_row, qty="2",
    )
    item_bin = _bin(
        db_session, parent, item, on_hand="6", warehouse=warehouse, last_entry=old,
    )
    other_bin = _bin(
        db_session, parent, other, on_hand="2", warehouse=warehouse,
        last_entry=other_row,
    )
    # Ingest marks the superseded revision inactive before its replacement is
    # written; visibility itself is decided by the supersession edge.
    old.active = False
    db_session.flush()
    new = _sle(
        db_session, target_batch, item, qty="4", at=BACKDATED_AT, kind="receipt",
        warehouse=warehouse, recorder_type="Document_ПриходнаяНакладная",
        ref="receipt-1",
    )
    edge = models.StockLedgerFactSupersession(
        old_sle_id=old.id, new_sle_id=new.id, import_batch_id=target_batch.id,
    )
    db_session.add(edge)
    db_session.flush()
    db_session.commit()
    return SimpleNamespace(
        parent=parent, target=target, target_batch=target_batch, item=item,
        owner=owner, old=old, new=new, edge=edge, item_bin=item_bin,
        other=other, other_owner=other_owner, other_bin=other_bin,
        other_allocation=other_allocation,
    )


def test_supersession_of_allocated_receipt_corrects_only_its_scope(
    db_session, monkeypatch,
):
    world = _superseded_receipt_world(db_session)
    _patch_payloads(
        monkeypatch, evidence=(_receipt_evidence(world.new, ref="receipt-1", qty="4"),),
    )
    publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=world.target.id,
        parent_generation_id=world.parent.id,
        delta_manifest={
            "rows": (world.new,), "basis_rows": (world.old,),
            "supersessions": (world.edge,), "backdate_from": BACKDATED_AT,
        },
        odata_client=object(),
        source_revision=world.target_batch.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )

    current = db_session.query(models.ReservationConsumptionAllocation).filter(
        models.ReservationConsumptionAllocation.is_current.is_(True),
        models.ReservationConsumptionAllocation.reservation_id == world.owner.id,
    ).all()
    assert [(int(row.sle_id), row.allocated_qty) for row in current] == [
        (int(world.new.id), Decimal("4.000")),
    ]
    db_session.refresh(world.owner)
    assert world.owner.replenishment_received_qty == Decimal("4.000")
    db_session.refresh(world.item_bin)
    assert world.item_bin.on_hand == Decimal("4.000")
    assert int(world.item_bin.last_entry_id) == int(world.new.id)
    # The foreign BUY scope is not replayed and not rewritten.
    db_session.refresh(world.other_owner)
    db_session.refresh(world.other_bin)
    db_session.refresh(world.other_allocation)
    assert world.other_owner.replenishment_received_qty == Decimal("2.000")
    assert world.other_bin.on_hand == Decimal("2.000")
    assert int(world.other_bin.ledger_generation_id) == int(world.parent.id)
    assert bool(world.other_allocation.is_current) is True
    db_session.rollback()


def test_mixed_forward_backdated_and_superseded_delta_is_published(
    db_session, monkeypatch,
):
    world = _superseded_receipt_world(db_session)
    forward_item = _item(db_session, "BOUNDED-FORWARD")
    late_item = _item(db_session, "BOUNDED-LATE")
    forward_bin = _bin(db_session, world.parent, forward_item, on_hand="1")
    late_bin = _bin(db_session, world.parent, late_item, on_hand="2")
    forward = _sle(
        db_session, world.target_batch, forward_item, qty="3", at=FORWARD_AT, ref="fwd",
    )
    late = _sle(
        db_session, world.target_batch, late_item, qty="5", at=BACKDATED_AT, ref="late",
    )
    db_session.commit()

    _patch_payloads(
        monkeypatch, evidence=(_receipt_evidence(world.new, ref="receipt-1", qty="4"),),
    )
    result = publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=world.target.id,
        parent_generation_id=world.parent.id,
        delta_manifest={
            "rows": (world.new, forward, late), "basis_rows": (world.old,),
            "supersessions": (world.edge,), "backdate_from": BACKDATED_AT,
        },
        odata_client=object(),
        source_revision=world.target_batch.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )

    assert result.input_delta_rows == 3
    assert db_session.get(models.LedgerGeneration, world.target.id).status == "accepted"
    db_session.refresh(forward_bin)
    db_session.refresh(late_bin)
    db_session.refresh(world.item_bin)
    assert forward_bin.on_hand == Decimal("4.000")
    assert late_bin.on_hand == Decimal("7.000")
    assert world.item_bin.on_hand == Decimal("4.000")
    current = db_session.query(models.ReservationConsumptionAllocation).filter(
        models.ReservationConsumptionAllocation.is_current.is_(True),
        models.ReservationConsumptionAllocation.reservation_id == world.owner.id,
    ).all()
    assert [int(row.sle_id) for row in current] == [int(world.new.id)]
    db_session.refresh(world.other_bin)
    assert int(world.other_bin.ledger_generation_id) == int(world.parent.id)
    db_session.rollback()


def test_unscopable_supersession_fails_closed_with_named_cause(db_session, monkeypatch):
    # The superseded receipt sits on a warehouse outside the planning contour,
    # so no BUY distribution scope can be derived for its live assignment.
    world = _superseded_receipt_world(db_session, warehouse="WH-OUTSIDE")
    _patch_payloads(
        monkeypatch, evidence=(_receipt_evidence(world.new, ref="receipt-1", qty="4"),),
    )
    with pytest.raises(
        publisher.ForwardPhysicalRefreshUnavailable,
        match="has no resolvable distribution scope",
    ):
        publisher.publish_forward_physical_refresh_current(
            db_session,
            target_generation_id=world.target.id,
            parent_generation_id=world.parent.id,
            delta_manifest={
                "rows": (world.new,), "basis_rows": (world.old,),
                "supersessions": (world.edge,), "backdate_from": BACKDATED_AT,
            },
            odata_client=object(),
            source_revision=world.target_batch.id,
            planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
        )
    db_session.rollback()
    assert db_session.get(models.LedgerGeneration, world.target.id).status == "building"


def test_delta_reader_names_the_earliest_changed_boundary(db_session):
    parent, parent_batch = _parent(db_session)
    child_batch = models.PhysicalImportBatch(
        batch_key="incremental-boundary-batch", status="completed",
        cutoff=parent.cutoff + timedelta(days=1), source_watermarks={},
        completed_at=parent.cutoff + timedelta(days=1),
    )
    item = models.Item(item_code="BOUNDARY-ITEM", item_name="Boundary item")
    db_session.add_all([child_batch, item])
    db_session.flush()
    superseded_at = parent.cutoff - timedelta(days=4)
    backdated_at = parent.cutoff - timedelta(days=2)
    old = models.StockLedgerEntry(
        ingest_batch_id=parent_batch.id, source_content_hash="boundary-old",
        business_identity="boundary-superseded", item_id=item.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=superseded_at, record_type="Receipt",
        movement_kind="assembly_in", recorder_type="Production",
        recorder_ref="boundary-old-rec", line_no="1", ingest_source="pull",
    )
    db_session.add(old)
    db_session.flush()
    old.active = False
    db_session.flush()
    replacement = models.StockLedgerEntry(
        ingest_batch_id=child_batch.id, source_content_hash="boundary-new",
        business_identity="boundary-superseded", item_id=item.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("2"), posting_at=superseded_at, record_type="Receipt",
        movement_kind="assembly_in", recorder_type="Production",
        recorder_ref="boundary-old-rec", line_no="1", ingest_source="pull",
    )
    backdated = models.StockLedgerEntry(
        ingest_batch_id=child_batch.id, source_content_hash="boundary-backdated",
        business_identity="boundary-backdated", item_id=item.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("3"), posting_at=backdated_at, record_type="Receipt",
        movement_kind="assembly_in", recorder_type="Production",
        recorder_ref="boundary-late-rec", line_no="1", ingest_source="pull",
    )
    forward = models.StockLedgerEntry(
        ingest_batch_id=child_batch.id, source_content_hash="boundary-forward",
        business_identity="boundary-forward", item_id=item.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("4"), posting_at=parent.cutoff + timedelta(hours=6),
        record_type="Receipt", movement_kind="assembly_in",
        recorder_type="Production", recorder_ref="boundary-fwd-rec", line_no="1",
        ingest_source="pull",
    )
    db_session.add_all([replacement, backdated, forward])
    db_session.flush()
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=old.id, new_sle_id=replacement.id, import_batch_id=child_batch.id,
    ))
    db_session.commit()

    delta = workflow._physical_refresh_delta_rows(
        db_session,
        parent=parent,
        target=parent,
        physical_import=type(
            "Import", (), {"physical_import_batch_id": child_batch.id},
        )(),
        recorder_audit=type("Audit", (), {})(),
    )

    # The boundary is the oldest changed posting: the superseded fact, not the
    # oldest newly imported row and not the parent cutoff.
    assert workflow._naive(delta["backdate_from"]) == workflow._naive(superseded_at)
    assert delta["backdated"] is True
    assert set(delta["new_row_ids"]) == {
        int(replacement.id), int(backdated.id), int(forward.id),
    }
    assert len(delta["rows"]) == 4


def test_forward_only_delta_declares_no_backdate_boundary(db_session):
    parent, _parent_batch = _parent(db_session)
    child_batch = models.PhysicalImportBatch(
        batch_key="incremental-forward-batch", status="completed",
        cutoff=parent.cutoff + timedelta(days=1), source_watermarks={},
        completed_at=parent.cutoff + timedelta(days=1),
    )
    item = models.Item(item_code="FORWARD-ITEM", item_name="Forward item")
    db_session.add_all([child_batch, item])
    db_session.flush()
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=child_batch.id, source_content_hash="forward-only",
        business_identity="forward-only", item_id=item.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("2"), posting_at=parent.cutoff + timedelta(hours=6),
        record_type="Receipt", movement_kind="assembly_in",
        recorder_type="Production", recorder_ref="forward-rec", line_no="1",
        ingest_source="pull",
    ))
    db_session.commit()
    delta = workflow._physical_refresh_delta_rows(
        db_session,
        parent=parent,
        target=parent,
        physical_import=type(
            "Import", (), {"physical_import_batch_id": child_batch.id},
        )(),
        recorder_audit=type("Audit", (), {})(),
    )
    assert delta["backdate_from"] is None
    assert delta["backdated"] is False


def test_delta_receipt_is_typed_and_allocated_when_the_fact_carries_the_1c_organization(
    db_session, monkeypatch,
):
    """The shape the stand actually has: owner org empty, fact org a 1C GUID.

    The bounded scope resolver compared the two and never matched, so five
    consecutive accepted refreshes produced no BUY scope, typed no supplier
    receipt and allocated nothing - while the generation-wide writer, which
    attaches receipts to reservations by item, had typed every one of them.
    Organization now comes from the owner instead of being compared; item,
    characteristic and pool are still matched.
    """
    parent, target, _parent_batch, target_batch = _generations(db_session)
    item = _item(db_session, "BOUNDED-ORG-MISMATCH")
    owner, _requirement = _buy_owner(db_session, parent, item)
    # The frozen obligation owner carries no organization at all.
    owner.organization_ref = ""
    db_session.flush()
    receipt = _sle(
        db_session, target_batch, item, qty="4", at=FORWARD_AT, kind="receipt",
        warehouse="WH-BUY", recorder_type="Document_ПриходнаяНакладная",
        ref="receipt-org",
    )
    # ...while the physical fact carries the 1C organization that posted it.
    receipt.organization_ref = "c78bcd0e-81f0-11ee-9ce5-9ee51454587f"
    db_session.flush()
    db_session.commit()

    _patch_payloads(
        monkeypatch, evidence=(_receipt_evidence(receipt, ref="receipt-org", qty="4"),),
    )
    result = publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        delta_manifest={"rows": (receipt,), "supersessions": ()},
        odata_client=object(),
        source_revision=target_batch.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )

    # The receipt reached a BUY scope, and the scope carries the owner's
    # organization rather than the document's.
    assert result.affected_scopes
    provenance = db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).filter_by(
        ledger_generation_id=target.id, stock_ledger_entry_id=receipt.id
    ).one()
    assert str(provenance.operation_kind) == "supplier_receipt"
    allocations = db_session.query(models.ReservationConsumptionAllocation).filter_by(
        is_current=True,
        allocation_role="replenishment_receipt",
        reservation_id=owner.id,
        sle_id=receipt.id,
    ).all()
    assert [row.allocated_qty for row in allocations] == [Decimal("4.000")]
    db_session.refresh(owner)
    assert owner.replenishment_received_qty == Decimal("4.000")
    db_session.rollback()


def test_untyped_delta_receipt_owed_to_a_buy_owner_is_refused(db_session, monkeypatch):
    """The gate: a receipt against a live order may not be published untyped."""
    parent, target, _parent_batch, target_batch = _generations(db_session)
    item = _item(db_session, "BOUNDED-UNTYPED-BUY")
    owner, _requirement = _buy_owner(db_session, parent, item)
    owner.organization_ref = ""
    db_session.flush()
    receipt = _sle(
        db_session, target_batch, item, qty="4", at=FORWARD_AT, kind="receipt",
        warehouse="WH-BUY", recorder_type="Document_ПриходнаяНакладная",
        ref="receipt-untyped",
    )
    db_session.commit()

    _patch_payloads(monkeypatch, evidence=())
    # The typing step produces nothing for this receipt.
    monkeypatch.setattr(
        publisher, "build_bounded_supplier_receipt_manifest",
        lambda *a, **kw: publisher.BoundedBuyReceiptDeltaManifest(),
    )
    monkeypatch.setattr(
        publisher, "apply_current_replenishment_for_bounded_buy_scopes",
        lambda *a, **kw: SimpleNamespace(replayed_rows=0),
    )

    with pytest.raises(
        publisher.ForwardPhysicalRefreshUnavailable,
        match="untyped although their items have an active current BUY owner",
    ):
        publisher.publish_forward_physical_refresh_current(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            delta_manifest={"rows": (receipt,), "supersessions": ()},
            odata_client=object(),
            source_revision=target_batch.id,
            planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
        )
    db_session.rollback()


def _make_owner(db_session, parent, item, *, required="10"):
    """A live MAKE owner whose demand an assembly output must extinguish."""
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT", config_snapshot={}, ledger_generation_id=parent.id,
        ledger_cutoff=parent.cutoff, period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30), active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id, item_id=item.item_id, total_required_qty=Decimal(required),
        net_required_qty=Decimal(required), period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30), bom_level=0, planning_stock_pool="default",
        characteristic_ref="", organization_ref="", freeze_version=1,
    )
    db_session.add(requirement)
    db_session.flush()
    owner = models.ReservationEntry(
        ledger_generation_id=parent.id, item_id=item.item_id, run_id=run.run_id,
        freeze_version=1, requirement_id=requirement.id,
        priority_period_from=date(2026, 9, 1), priority_period_to=date(2026, 9, 30),
        realization_mode="make", planning_stock_pool="default",
        reserved_qty=Decimal(required), replenishment_required_qty=Decimal(required),
        lifecycle_status="active",
        current_identity="reservation:req:{0}:mode:make".format(requirement.id),
        owner_kind="current", is_current=True,
    )
    db_session.add(owner)
    db_session.flush()
    return owner, requirement


def test_delta_assembly_output_reaches_its_make_owner_despite_the_1c_organization(
    db_session, monkeypatch,
):
    """The MAKE mirror of the supplier-receipt defect.

    ``_current_scopes`` compared the owner's organization with the fact's, so
    a bounded refresh produced no MAKE scope at all and the assembly outputs
    were never offered to the R4 writer: their demand stayed open.  On the
    stand that was 492 visible ``assembly_in`` facts for items with a live
    MAKE owner.
    """
    parent, target, _parent_batch, target_batch = _generations(db_session)
    item = _item(db_session, "BOUNDED-MAKE-ORG")
    owner, _requirement = _make_owner(db_session, parent, item, required="10")
    owner.organization_ref = ""
    db_session.flush()
    output = _sle(
        db_session, target_batch, item, qty="4", at=FORWARD_AT, kind="assembly_in",
        warehouse="WH-BUY", recorder_type="Document_СборкаЗапасов", ref="assembly-org",
    )
    output.organization_ref = "c78bcd0e-81f0-11ee-9ce5-9ee51454587f"
    db_session.flush()
    db_session.commit()

    _patch_payloads(monkeypatch, evidence=())
    result = publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        delta_manifest={"rows": (output,), "supersessions": ()},
        odata_client=object(),
        source_revision=target_batch.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )

    # The output reached a MAKE scope, and the scope carries the owner's
    # (empty) organization rather than the document's 1C GUID.
    assert result.affected_scopes == (f"{item.item_id}:::default:make",)
    # The current-owner representation of a realized MAKE fact on this line is
    # the R4 allocation plus the owner's received quantity; there is no
    # separate MAKE allocation role.
    allocations = db_session.query(models.ReservationConsumptionAllocation).filter_by(
        is_current=True, reservation_id=owner.id, sle_id=output.id,
    ).all()
    assert [row.allocated_qty for row in allocations] == [Decimal("4.000")]
    assert {str(row.allocation_role) for row in allocations} == {"replenishment_receipt"}
    db_session.refresh(owner)
    assert owner.replenishment_received_qty == Decimal("4.000")
    db_session.rollback()


def test_assembly_output_outside_every_make_scope_is_refused(db_session, monkeypatch):
    """The gate: an output owed to a live MAKE owner may not be published unscoped."""
    parent, target, _parent_batch, target_batch = _generations(db_session)
    item = _item(db_session, "BOUNDED-MAKE-UNSCOPED")
    owner, _requirement = _make_owner(db_session, parent, item, required="10")
    owner.organization_ref = ""
    db_session.flush()
    output = _sle(
        db_session, target_batch, item, qty="4", at=FORWARD_AT, kind="assembly_in",
        warehouse="WH-BUY", recorder_type="Document_СборкаЗапасов",
        ref="assembly-unscoped",
    )
    db_session.commit()

    _patch_payloads(monkeypatch, evidence=())
    # The fact resolves to its owner's scope, but the scope plumbing drops it
    # before the writer is called.  That is the regression the gate exists for:
    # an output owed to a live MAKE owner must never be published unrealized.
    monkeypatch.setattr(publisher, "_current_scopes", lambda *a, **kw: ())
    with pytest.raises(
        publisher.ForwardPhysicalRefreshUnavailable,
        match="outside every MAKE scope",
    ):
        publisher.publish_forward_physical_refresh_current(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            delta_manifest={"rows": (output,), "supersessions": ()},
            odata_client=object(),
            source_revision=target_batch.id,
            planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
        )
    db_session.rollback()


def _fixed_plan_line(db, parent, item, *, qty="5"):
    """A live fixed plan line with open remaining output."""
    plan = models.ProductionPlanHeader(
        name=f"plan-{item.item_code}", period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30), status="fixed",
        fixed_at=parent.cutoff,
    )
    db.add(plan)
    db.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT", config_snapshot={},
        ledger_generation_id=int(parent.id), ledger_cutoff=parent.cutoff,
        active_freeze_version=1, source_plan_id=int(plan.id),
        period_from=plan.period_from, period_to=plan.period_to,
        fixed_at=parent.cutoff,
    )
    db.add(run)
    db.flush()
    line = models.ProductionPlanLine(
        plan_id=int(plan.id), item_id=int(item.item_id),
        bucket_date=plan.period_from, qty=Decimal(qty),
        accepted_output_qty=Decimal("0"), remaining_output_qty=Decimal(qty),
        locked_by_run_id=int(run.run_id),
    )
    db.add(line)
    db.flush()
    db.add(models.MrpRunRoot(
        run_id=int(run.run_id), plan_line_id=int(line.id),
        planned_qty=Decimal(qty), accepted_qty=Decimal("0"),
        remaining_qty=Decimal(qty),
    ))
    db.flush()
    return plan, run, line


def test_bounded_refresh_moves_the_plan_line_when_its_output_is_received(
    db_session, monkeypatch,
):
    """A received assembly output must move "выполнено" in the same refresh.

    Canon: the fact of production is the receipt into stock, and it
    extinguishes the demand.  This pins the whole bounded publication path -
    not just the allocator - so an output for a live fixed plan line cannot
    stop producing its execution fact unnoticed.
    """
    parent, target, _parent_batch, target_batch = _generations(db_session)
    item = _item(db_session, "BOUNDED-PLAN-OUTPUT")
    _plan, _run, line = _fixed_plan_line(db_session, parent, item, qty="5")
    output = _sle(
        db_session, target_batch, item, qty="3", at=FORWARD_AT, kind="assembly_in",
        warehouse="WH-BUY", recorder_type="Document_СборкаЗапасов",
        ref="assembly-plan-output",
    )
    db_session.commit()

    # Everything is stubbed except the writer under test.
    real_output = publisher.apply_bounded_assembly_output_plan_execution
    _patch_payloads(monkeypatch, evidence=())
    monkeypatch.setattr(
        publisher, "apply_bounded_assembly_output_plan_execution", real_output,
    )
    publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        delta_manifest={"rows": (output,), "supersessions": ()},
        odata_client=object(),
        source_revision=target_batch.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )

    facts = db_session.query(models.ProductionPlanExecutionFact).filter_by(
        stock_ledger_entry_id=int(output.id), plan_line_id=int(line.id),
    ).all()
    assert [row.allocated_qty for row in facts] == [Decimal("3.000")]
    db_session.refresh(line)
    assert line.accepted_output_qty == Decimal("3.000")
    assert line.remaining_output_qty == Decimal("2.000")
    db_session.rollback()


def test_bounded_refresh_counts_one_document_output_once(db_session, monkeypatch):
    """The two legs of one assembly document are one output, not two.

    A document that produces an item at one warehouse and re-produces it at
    another carries two ``assembly_in`` rows for the same item; canon nets
    them inside the document, so only the net reaches the plan line.  This is
    why an ``assembly_in`` row without an execution fact is not by itself
    evidence of a defect - which is what kept the MAKE gate from asserting
    on execution facts.
    """
    parent, target, _parent_batch, target_batch = _generations(db_session)
    item = _item(db_session, "BOUNDED-PLAN-INTERNAL")
    _plan, _run, line = _fixed_plan_line(db_session, parent, item, qty="10")
    first_leg = _sle(
        db_session, target_batch, item, qty="2", at=FORWARD_AT, kind="assembly_in",
        warehouse="WH-MOVE", recorder_type="Document_СборкаЗапасов",
        ref="assembly-two-legs", line_no="1",
    )
    consumed = _sle(
        db_session, target_batch, item, qty="-2", at=FORWARD_AT,
        kind="assembly_out", warehouse="WH-MOVE",
        recorder_type="Document_СборкаЗапасов", ref="assembly-two-legs",
        line_no="2",
    )
    second_leg = _sle(
        db_session, target_batch, item, qty="2", at=FORWARD_AT, kind="assembly_in",
        warehouse="WH-BUY", recorder_type="Document_СборкаЗапасов",
        ref="assembly-two-legs", line_no="3",
    )
    db_session.commit()

    real_output = publisher.apply_bounded_assembly_output_plan_execution
    _patch_payloads(monkeypatch, evidence=())
    monkeypatch.setattr(
        publisher, "apply_bounded_assembly_output_plan_execution", real_output,
    )
    publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        delta_manifest={
            "rows": (first_leg, consumed, second_leg), "supersessions": (),
        },
        odata_client=object(),
        source_revision=target_batch.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )

    facts = db_session.query(models.ProductionPlanExecutionFact).filter_by(
        plan_line_id=int(line.id),
    ).all()
    assert sum((row.allocated_qty for row in facts), Decimal("0")) == Decimal("2.000")
    db_session.refresh(line)
    assert line.accepted_output_qty == Decimal("2.000")
    db_session.rollback()


def test_delta_output_reaches_a_rework_owner(db_session, monkeypatch):
    """Canon §18: an accepted ``assembly_in`` closes a rework reserve too.

    The bounded MAKE writer used to hard-code ``realization_mode='make'`` in
    its owner query, so a rework obligation was resolved into a scope the
    writer then found no owner for.
    """
    parent, target, _parent_batch, target_batch = _generations(db_session)
    item = _item(db_session, "BOUNDED-REWORK")
    owner, _requirement = _make_owner(db_session, parent, item, required="10")
    owner.realization_mode = "rework"
    owner.current_identity = (
        f"reservation:req:{int(owner.requirement_id)}:mode:rework"
    )
    owner.organization_ref = ""
    db_session.flush()
    output = _sle(
        db_session, target_batch, item, qty="4", at=FORWARD_AT, kind="assembly_in",
        warehouse="WH-BUY", recorder_type="Document_СборкаЗапасов", ref="rework-out",
    )
    db_session.commit()

    _patch_payloads(monkeypatch, evidence=())
    result = publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        delta_manifest={"rows": (output,), "supersessions": ()},
        odata_client=object(),
        source_revision=target_batch.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )

    # Rework is realized inside the MAKE scope, not a scope of its own.
    assert result.affected_scopes == (f"{item.item_id}:::default:make",)
    allocations = db_session.query(models.ReservationConsumptionAllocation).filter_by(
        is_current=True, reservation_id=owner.id, sle_id=output.id,
    ).all()
    assert [row.allocated_qty for row in allocations] == [Decimal("4.000")]
    db_session.rollback()


def test_delta_output_with_a_real_characteristic_still_reaches_its_owner(
    db_session, monkeypatch,
):
    """The canonical collapse keys both sides; raw columns never matched.

    A frozen reservation is written through ``mrp_freeze.pool_key_for``,
    which collapses characteristic and organization, so a fact that carries
    either would never have matched an owner by column comparison.
    """
    parent, target, _parent_batch, target_batch = _generations(db_session)
    item = _item(db_session, "BOUNDED-CHARACTERISTIC")
    owner, _requirement = _make_owner(db_session, parent, item, required="10")
    owner.organization_ref = ""
    owner.characteristic_ref = ""
    db_session.flush()
    output = _sle(
        db_session, target_batch, item, qty="4", at=FORWARD_AT, kind="assembly_in",
        warehouse="WH-BUY", recorder_type="Document_СборкаЗапасов", ref="char-out",
    )
    output.characteristic_ref = "a-real-characteristic"
    output.organization_ref = "c78bcd0e-81f0-11ee-9ce5-9ee51454587f"
    db_session.flush()
    db_session.commit()

    _patch_payloads(monkeypatch, evidence=())
    result = publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        delta_manifest={"rows": (output,), "supersessions": ()},
        odata_client=object(),
        source_revision=target_batch.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )

    assert result.affected_scopes == (f"{item.item_id}:::default:make",)
    assert db_session.query(models.ReservationConsumptionAllocation).filter_by(
        is_current=True, reservation_id=owner.id, sle_id=output.id,
    ).count() == 1
    db_session.rollback()


def test_two_distribution_pools_for_one_item_fail_closed_with_one_verdict(
    db_session,
):
    """Ambiguity has one reaction and one message, on both sides."""
    from app.services.item_ledger import physical_refresh_current_publish as pub

    parent, _target, _parent_batch, target_batch = _generations(db_session)
    item = _item(db_session, "BOUNDED-AMBIGUOUS")
    first, _req_a = _buy_owner(db_session, parent, item)
    second, _req_b = _buy_owner(db_session, parent, item)
    # Two live obligations of the same item in two realization modes: one
    # physical fact cannot be attributed to both, and canon assigns a fact
    # exactly once.
    second.realization_mode = "rework"
    second.current_identity = (
        f"reservation:req:{int(second.requirement_id)}:mode:rework"
    )
    db_session.flush()
    receipt = _sle(
        db_session, target_batch, item, qty="4", at=FORWARD_AT, kind="receipt",
        warehouse="WH-BUY", recorder_type="Document_ПриходнаяНакладная",
        ref="ambiguous",
    )
    db_session.flush()

    owners = (first, second)
    # Both owners realize inside one MAKE scope: one fact, one replay.
    assembly = _sle(
        db_session, target_batch, item, qty="4", at=FORWARD_AT, kind="assembly_in",
        warehouse="WH-BUY", recorder_type="Document_СборкаЗапасов",
        ref="ambiguous-make",
    )
    first.realization_mode = "make"
    db_session.flush()
    assert len(pub._make_scopes_for_assembly_row(assembly, owners)) == 1

    # Two genuinely different distribution pools for one item is the one
    # ambiguity this module refuses, with one message on both sides.
    with pytest.raises(
        pub.ForwardPhysicalRefreshUnavailable, match="ambiguous distribution pools"
    ):
        pub._single_scope_or_fail(
            receipt,
            (
                (item.item_id, "", "", "default", "buy"),
                (item.item_id, "", "", "other", "buy"),
            ),
        )
    db_session.rollback()


def test_historical_untyped_receipt_does_not_block_a_later_refresh(
    db_session, monkeypatch,
):
    """The BUY gate judges this refresh's delta, not the whole prefix.

    A receipt that predates its item's BUY owner was covered at freeze time,
    not by replenishment.  Scanning the visible prefix made such history
    block every future refresh for ever, against CANON "Объём вычислений
    штатного физического refresh".
    """
    parent, target, parent_batch, target_batch = _generations(db_session)
    item = _item(db_session, "BOUNDED-HISTORICAL")
    owner, _requirement = _buy_owner(db_session, parent, item)
    owner.organization_ref = ""
    db_session.flush()
    # Untyped, visible, inside the contour - and none of this refresh's business.
    _sle(
        db_session, parent_batch, item, qty="7", at=PARENT_CUTOFF - timedelta(days=2),
        kind="receipt", warehouse="WH-BUY",
        recorder_type="Document_ПриходнаяНакладная", ref="historical-receipt",
    )
    unrelated = _sle(
        db_session, target_batch, item, qty="1", at=FORWARD_AT, kind="transfer_in",
        warehouse="WH-MOVE", recorder_type="Document_ПеремещениеЗапасов",
        ref="unrelated-move",
    )
    db_session.commit()

    _patch_payloads(monkeypatch, evidence=())
    result = publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        delta_manifest={"rows": (unrelated,), "supersessions": ()},
        odata_client=object(),
        source_revision=target_batch.id,
        planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
    )

    assert result.target_generation_id == target.id
    db_session.rollback()


def test_second_refresh_over_a_rework_owner_keeps_one_allocation(
    db_session, monkeypatch,
):
    """A rework owner's allocation must be visible to the next replay.

    Rework owners are realized inside the MAKE scope, but the existing
    allocations were selected by ``realization_mode == scope[4]`` - so a
    rework owner's allocation was invisible to every later refresh, which
    re-inserted it each time: duplicated current basis, coverage counted
    twice, and an IntegrityError on the partial current index.
    """
    parent, target, _parent_batch, target_batch = _generations(db_session)
    item = _item(db_session, "BOUNDED-REWORK-TWICE")
    owner, requirement = _make_owner(db_session, parent, item, required="10")
    owner.realization_mode = "rework"
    owner.current_identity = f"reservation:req:{int(requirement.id)}:mode:rework"
    owner.organization_ref = ""
    db_session.flush()
    output = _sle(
        db_session, target_batch, item, qty="4", at=FORWARD_AT, kind="assembly_in",
        warehouse="WH-BUY", recorder_type="Document_СборкаЗапасов",
        ref="rework-twice",
    )
    db_session.commit()

    def _publish(revision):
        _patch_payloads(monkeypatch, evidence=())
        return publisher.publish_forward_physical_refresh_current(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            delta_manifest={"rows": (output,), "supersessions": ()},
            odata_client=object(),
            source_revision=revision,
            planning_pool_by_warehouse=POOL_BY_WAREHOUSE,
        )

    _publish(target_batch.id)
    # The pointer moved on the first publication; put it back so the same
    # bounded refresh can be replayed, which is what a retry does.
    db_session.get(models.PlanningTruthState, 1).current_generation_id = parent.id
    target.status = "building"
    db_session.flush()
    _publish(target_batch.id + 1)

    allocations = db_session.query(models.ReservationConsumptionAllocation).filter_by(
        is_current=True, sle_id=output.id,
    ).all()
    assert [row.allocated_qty for row in allocations] == [Decimal("4.000")]
    assert {int(row.reservation_id) for row in allocations} == {int(owner.id)}
    db_session.refresh(owner)
    # Coverage counted once, not twice.
    assert owner.replenishment_received_qty == Decimal("4.000")
    db_session.rollback()

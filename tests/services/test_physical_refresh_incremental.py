from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app import models
from app.services.item_ledger import physical_refresh_orchestrator as workflow


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

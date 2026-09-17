from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
from types import SimpleNamespace
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger import assembly_output_persistence as output_persistence
from app.services.item_ledger import physical_refresh_current_publish as publisher
from app.services.item_ledger.physical import CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE


def _generations(db_session):
    parent_cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    target_cutoff = parent_cutoff + timedelta(days=1)
    parent_batch = models.PhysicalImportBatch(
        batch_key="current-publish-parent-batch", status="completed",
        source_complete=True, cutoff=parent_cutoff,
        source_watermarks={}, completed_at=parent_cutoff,
    )
    target_batch = models.PhysicalImportBatch(
        batch_key="current-publish-target-batch", status="completed",
        source_complete=True, cutoff=target_cutoff,
        source_watermarks={}, completed_at=target_cutoff,
    )
    parent = models.LedgerGeneration(
        generation_key="current-publish-parent", status="accepted",
        cutoff=parent_cutoff, source_watermarks={}, capabilities={"physical_ledger": True},
        physical_import_batch=parent_batch, algorithm_version="test", accepted_at=parent_cutoff,
    )
    target = models.LedgerGeneration(
        generation_key="current-publish-target", status="building",
        cutoff=target_cutoff, source_watermarks={}, capabilities={},
        physical_import_batch=target_batch, algorithm_version="test",
    )
    db_session.add_all([parent_batch, target_batch, parent])
    db_session.flush()
    # The target is attached after both batches have concrete identities.
    target.physical_import_batch_id = target_batch.id
    db_session.add(target)
    db_session.flush()
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    db_session.commit()
    return parent, target


def _patch_safe_pipeline(monkeypatch, phases):
    class Result:
        changed_rows = 0
        idempotent = True

    monkeypatch.setattr(
        publisher, "apply_bounded_current_stock_bins",
        lambda *a, **kw: phases.append("stock") or SimpleNamespace(changed_keys=1),
    )
    monkeypatch.setattr(
        publisher, "apply_current_replenishment_for_bounded_make_scopes",
        lambda *a, **kw: phases.append("make") or SimpleNamespace(fact_rows=1),
    )
    monkeypatch.setattr(
        publisher, "apply_bounded_assembly_output_plan_execution",
        lambda *a, **kw: phases.append("assembly_output") or SimpleNamespace(metrics={"replayed_fact_rows": 1}),
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_assembly_payload",
        lambda *a, **kw: phases.append("build_assembly") or SimpleNamespace(
            queue_rows=({
                "entity_kind": "assembly_queue", "business_identity": "plan-line:1",
                "scope_key": "assembly:all-live-plans", "payload": {"plan_line_id": 1},
            },),
            readiness_rows=(), readiness_metrics={},
        ),
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_drum_payload",
        lambda *a, **kw: phases.append("build_drum") or SimpleNamespace(rows=(), metrics={}),
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_shelf_payload",
        lambda *a, **kw: phases.append("build_shelf") or SimpleNamespace(rows=(), metrics={}),
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_production_control_payload",
        lambda *a, **kw: phases.append("build_production") or {"rows": [], "meta": {}},
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_purchase_control_payload",
        lambda *a, **kw: phases.append("build_purchase") or {"rows": [], "meta": {}},
    )
    monkeypatch.setattr(
        publisher, "handoff_current_physical_refresh_provenance",
        lambda *a, **kw: phases.append("provenance") or SimpleNamespace(),
    )
    monkeypatch.setattr(
        publisher, "apply_bounded_current_material_custody_events",
        lambda *a, **kw: 0,
    )
    monkeypatch.setattr(
        publisher, "publish_current_execution_scope",
        lambda *a, **kw: phases.append(str(kw["scope_key"])) or Result(),
    )
    monkeypatch.setattr(
        publisher, "resolve_compact_queue_owner_ids",
        lambda db, rows, **kw: tuple(rows),
    )
    monkeypatch.setattr(publisher, "publish_current_production_control_from_payload", lambda *a, **kw: Result())
    monkeypatch.setattr(publisher, "publish_current_purchase_control_from_payload", lambda *a, **kw: Result())
    monkeypatch.setattr(publisher, "publish_generation", lambda db, target, **kw: phases.append("pointer"))
    monkeypatch.setattr(publisher, "_fixed_run_ids", lambda db: (1,))


def test_forward_publish_is_atomic_and_pointer_is_last(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-1", item_name="Current publish item")
    db_session.add(item)
    db_session.flush()
    sle = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-sle", business_identity="current-publish-sle",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("2"), posting_at=target.cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="transfer_out", recorder_type="Document_Transfer", recorder_ref="cp", line_no="1",
        ingest_source="test",
    )
    db_session.add(sle)
    db_session.commit()

    phases = []
    _patch_safe_pipeline(monkeypatch, phases)
    result = publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        delta_manifest={"rows": (sle,), "supersessions": ()},
        odata_client=None,
        source_revision=target.physical_import_batch_id,
        planning_pool_by_warehouse={"wh": "pool"},
        phase_hook=phases.append,
    )
    assert result.input_delta_rows == 1
    assert result.target_generation_id == target.id
    assert phases[-1] == "pointer"
    assert "make" not in phases
    assert "supplier_manifest" not in phases
    assert db_session.get(models.LedgerGeneration, target.id).status == "accepted"
    # The service owns no transaction boundary: caller can still roll back.
    db_session.rollback()


def test_backdated_and_superseded_delta_fail_before_current_writers(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-2", item_name="Current publish item")
    db_session.add(item)
    db_session.flush()
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-old", business_identity="current-publish-old",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=parent.cutoff, record_type="Receipt",
        movement_kind="assembly_in", recorder_type="Production", recorder_ref="cp2", line_no="1",
        ingest_source="test",
    )
    db_session.add(row)
    db_session.commit()
    called = []
    monkeypatch.setattr(publisher, "apply_bounded_current_stock_bins", lambda *a, **k: called.append(True))
    with pytest.raises(publisher.ForwardPhysicalRefreshUnavailable, match="forward facts only"):
        publisher.publish_forward_physical_refresh_current(
            db_session, target_generation_id=target.id, parent_generation_id=parent.id,
            delta_manifest={"rows": (row,), "supersessions": ()}, odata_client=None,
            source_revision=1,
            planning_pool_by_warehouse={"wh": "pool"},
        )
    assert called == []
    assert db_session.get(models.LedgerGeneration, target.id).status == "building"


def test_phase_failure_is_caller_rollback_boundary(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-3", item_name="Current publish item")
    db_session.add(item)
    db_session.flush()
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-failure", business_identity="current-publish-failure",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=target.cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="transfer_out", recorder_type="Document_Transfer", recorder_ref="cp3", line_no="1",
        ingest_source="test",
    )
    db_session.add(row)
    db_session.commit()
    _patch_safe_pipeline(monkeypatch, [])
    monkeypatch.setattr(publisher, "apply_bounded_current_stock_bins", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        publisher.publish_forward_physical_refresh_current(
            db_session, target_generation_id=target.id, parent_generation_id=parent.id,
            delta_manifest={"rows": (row,), "supersessions": ()}, odata_client=None,
            source_revision=1, planning_pool_by_warehouse={"wh": "pool"},
        )
    db_session.rollback()
    assert db_session.get(models.LedgerGeneration, target.id).status == "building"


def test_late_phase_failure_rolls_back_accepted_status_and_pointer(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-4", item_name="Current publish item")
    db_session.add(item)
    db_session.flush()
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-late-failure", business_identity="current-publish-late-failure",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=target.cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="transfer_out", recorder_type="Document_Transfer", recorder_ref="cp4", line_no="1",
        ingest_source="test",
    )
    db_session.add(row)
    db_session.commit()
    _patch_safe_pipeline(monkeypatch, [])

    def fail_after_acceptance(name):
        if name == "accepted":
            raise RuntimeError("late publish failure")

    with pytest.raises(RuntimeError, match="late publish failure"):
        publisher.publish_forward_physical_refresh_current(
            db_session, target_generation_id=target.id, parent_generation_id=parent.id,
            delta_manifest={"rows": (row,), "supersessions": ()}, odata_client=None,
            source_revision=1, planning_pool_by_warehouse={"wh": "pool"},
            phase_hook=fail_after_acceptance,
        )
    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(models.LedgerGeneration, target.id).status == "building"
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id


def test_boundary_and_cutoff_are_rejected_before_stock_writer(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-5", item_name="Current publish item")
    db_session.add(item)
    db_session.flush()
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-future", business_identity="current-publish-future",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=target.cutoff + timedelta(minutes=1), record_type="Receipt",
        movement_kind="expense", recorder_type="Document_Transfer", recorder_ref="cp5", line_no="1",
        ingest_source="test",
    )
    db_session.add(row)
    db_session.commit()
    called = []
    monkeypatch.setattr(publisher, "apply_bounded_current_stock_bins", lambda *a, **k: called.append(True))
    with pytest.raises(publisher.ForwardPhysicalRefreshUnavailable, match="after target cutoff"):
        publisher.publish_forward_physical_refresh_current(
            db_session, target_generation_id=target.id, parent_generation_id=parent.id,
            delta_manifest={"rows": (row,), "supersessions": ()}, odata_client=None,
            source_revision=1, planning_pool_by_warehouse={"wh": "pool"},
        )
    assert called == []


def test_unmatched_r4_fact_uses_explicit_pool_mapping(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-6", item_name="Current publish item")
    db_session.add(item)
    db_session.flush()
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-no-owner", business_identity="current-publish-no-owner",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=target.cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="assembly_in", recorder_type="Production", recorder_ref="cp6", line_no="1",
        ingest_source="test",
    )
    db_session.add(row)
    db_session.commit()
    _patch_safe_pipeline(monkeypatch, [])
    result = publisher.publish_forward_physical_refresh_current(
        db_session, target_generation_id=target.id, parent_generation_id=parent.id,
        delta_manifest={"rows": (row,), "supersessions": ()}, odata_client=None,
        source_revision=1, planning_pool_by_warehouse={"wh": "pool"},
    )
    assert result.affected_scopes == ()


def test_unmatched_supplier_receipt_uses_explicit_pool_mapping(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-10", item_name="Current publish item")
    db_session.add(item)
    db_session.flush()
    run = models.PlanningRun(status="FIXED_SNAPSHOT", ledger_generation_id=parent.id)
    db_session.add(run)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id, item_id=item.item_id,
        period_from=parent.cutoff.date(), period_to=target.cutoff.date(),
    )
    db_session.add(requirement)
    db_session.flush()
    db_session.add(models.ReservationEntry(
        ledger_generation_id=parent.id, item_id=item.item_id,
        characteristic_ref="", organization_ref="org", planning_stock_pool="other-pool",
        run_id=run.run_id, requirement_id=requirement.id,
        priority_period_from=parent.cutoff.date(), priority_period_to=target.cutoff.date(),
        realization_mode="buy", reserved_qty=Decimal("1"),
        current_identity="cp10-other-pool-owner", owner_kind="current", is_current=True,
    ))
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-supplier", business_identity="current-publish-supplier",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=target.cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="receipt", recorder_type="Document_ПриходнаяНакладная",
        recorder_ref="cp10", line_no="1", ingest_source="test",
    )
    db_session.add(row)
    db_session.commit()
    phases = []
    _patch_safe_pipeline(monkeypatch, phases)
    supplier_calls = []
    monkeypatch.setattr(
        publisher, "build_bounded_supplier_receipt_manifest",
        lambda *a, **k: supplier_calls.append("manifest"),
    )
    result = publisher.publish_forward_physical_refresh_current(
        db_session, target_generation_id=target.id, parent_generation_id=parent.id,
        delta_manifest={"rows": (row,), "supersessions": ()}, odata_client=None,
        source_revision=1, planning_pool_by_warehouse={"wh": "pool"},
    )
    assert result.affected_scopes == ()
    assert supplier_calls == []
    assert "supplier_manifest" not in phases


def test_mapped_supplier_receipt_uses_exact_current_buy_owner(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-13", item_name="Current buy owner")
    db_session.add(item)
    db_session.flush()
    run = models.PlanningRun(status="FIXED_SNAPSHOT", ledger_generation_id=parent.id)
    db_session.add(run)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id, item_id=item.item_id,
        period_from=parent.cutoff.date(), period_to=target.cutoff.date(),
    )
    db_session.add(requirement)
    db_session.flush()
    db_session.add(models.ReservationEntry(
        ledger_generation_id=parent.id, item_id=item.item_id,
        characteristic_ref="", organization_ref="org", planning_stock_pool="pool",
        run_id=run.run_id, requirement_id=requirement.id,
        priority_period_from=parent.cutoff.date(), priority_period_to=target.cutoff.date(),
        realization_mode="buy", reserved_qty=Decimal("1"),
        current_identity="cp13-buy-owner", owner_kind="current", is_current=True,
    ))
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-buy-owner", business_identity="current-publish-buy-owner",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=target.cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="receipt", recorder_type="Document_ПриходнаяНакладная",
        recorder_ref="cp13", line_no="1", ingest_source="test",
    )
    db_session.add(row)
    db_session.commit()
    phases = []
    _patch_safe_pipeline(monkeypatch, phases)
    buy_scope_calls = []
    monkeypatch.setattr(
        publisher, "build_bounded_supplier_receipt_manifest",
        lambda *a, **kw: buy_scope_calls.append(("manifest", tuple(kw["affected_scopes"])))
        or publisher.BoundedBuyReceiptDeltaManifest(new_sle_ids=(row.id,)),
    )
    monkeypatch.setattr(
        publisher, "apply_current_replenishment_for_bounded_buy_scopes",
        lambda *a, **kw: buy_scope_calls.append(("apply", tuple(kw["affected_scopes"])))
        or SimpleNamespace(replayed_rows=1),
    )
    result = publisher.publish_forward_physical_refresh_current(
        db_session, target_generation_id=target.id, parent_generation_id=parent.id,
        delta_manifest={"rows": (row,), "supersessions": ()}, odata_client=None,
        source_revision=1, planning_pool_by_warehouse={"wh": "pool"},
    )
    expected_scope = (item.item_id, "", "org", "pool", "buy")
    assert buy_scope_calls == [("manifest", (expected_scope,)), ("apply", (expected_scope,))]
    assert result.affected_scopes == (f"{item.item_id}::org:pool:buy",)


def test_assembly_without_owner_or_pool_is_stock_output_only(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-7", item_name="Current publish item")
    db_session.add(item)
    db_session.flush()
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-no-pool", business_identity="current-publish-no-pool",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=target.cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="assembly_in", recorder_type="Production", recorder_ref="cp7", line_no="1",
        ingest_source="test",
    )
    db_session.add(row)
    db_session.commit()
    phases = []
    _patch_safe_pipeline(monkeypatch, phases)
    result = publisher.publish_forward_physical_refresh_current(
        db_session, target_generation_id=target.id, parent_generation_id=parent.id,
        delta_manifest={"rows": (row,), "supersessions": ()}, odata_client=None,
        source_revision=1, planning_pool_by_warehouse={}, phase_hook=phases.append,
    )
    assert result.affected_scopes == ()
    assert "make" not in phases


def test_assembly_uses_current_owner_pool_when_warehouse_is_unmapped(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-11", item_name="Current make owner")
    db_session.add(item)
    db_session.flush()
    run = models.PlanningRun(status="FIXED_SNAPSHOT", ledger_generation_id=parent.id)
    db_session.add(run)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id, item_id=item.item_id,
        period_from=parent.cutoff.date(), period_to=target.cutoff.date(),
    )
    db_session.add(requirement)
    db_session.flush()
    db_session.add(models.ReservationEntry(
        ledger_generation_id=parent.id, item_id=item.item_id,
        characteristic_ref="", organization_ref="org", planning_stock_pool="owner-pool",
        run_id=run.run_id, requirement_id=requirement.id,
        priority_period_from=parent.cutoff.date(), priority_period_to=target.cutoff.date(),
        realization_mode="make", reserved_qty=Decimal("1"),
        current_identity="cp11-owner", owner_kind="current", is_current=True,
    ))
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-owner", business_identity="current-publish-owner",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="outside",
        qty=Decimal("1"), posting_at=target.cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="assembly_in", recorder_type="Production", recorder_ref="cp11", line_no="1",
        ingest_source="test",
    )
    db_session.add(row)
    db_session.commit()
    phases = []
    _patch_safe_pipeline(monkeypatch, phases)
    make_scopes = []
    monkeypatch.setattr(
        publisher, "apply_current_replenishment_for_bounded_make_scopes",
        lambda *a, **kw: make_scopes.append(tuple(kw["affected_scopes"]))
        or SimpleNamespace(fact_rows=1),
    )
    result = publisher.publish_forward_physical_refresh_current(
        db_session, target_generation_id=target.id, parent_generation_id=parent.id,
        delta_manifest={"rows": (row,), "supersessions": ()}, odata_client=None,
        source_revision=1, planning_pool_by_warehouse={},
    )
    assert make_scopes == [((item.item_id, "", "org", "owner-pool", "make"),)]
    assert result.affected_scopes == (f"{item.item_id}::org:owner-pool:make",)


def test_unmapped_supplier_receipt_is_stock_only(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-12", item_name="Outside supplier")
    db_session.add(item)
    db_session.flush()
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-outside-supplier", business_identity="current-publish-outside-supplier",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="outside",
        qty=Decimal("1"), posting_at=target.cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="receipt", recorder_type="Document_ПриходнаяНакладная",
        recorder_ref="cp12", line_no="1", ingest_source="test",
    )
    db_session.add(row)
    db_session.commit()
    phases = []
    _patch_safe_pipeline(monkeypatch, phases)
    supplier_calls = []
    monkeypatch.setattr(
        publisher, "build_bounded_supplier_receipt_manifest",
        lambda *a, **k: supplier_calls.append("manifest"),
    )
    result = publisher.publish_forward_physical_refresh_current(
        db_session, target_generation_id=target.id, parent_generation_id=parent.id,
        delta_manifest={"rows": (row,), "supersessions": ()}, odata_client=None,
        source_revision=1, planning_pool_by_warehouse={},
    )
    assert result.affected_scopes == ()
    assert supplier_calls == []
    assert "supplier_manifest" not in phases


def test_canonical_cutoff_adjustment_is_allowed_as_stock_only(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-8", item_name="Current publish item")
    db_session.add(item)
    db_session.flush()
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-adjustment", business_identity="current-publish-adjustment",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=target.cutoff, record_type="Adjustment",
        movement_kind=CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE,
        recorder_type=CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE,
        recorder_ref="cutoff", line_no="1", ingest_source=CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE,
    )
    db_session.add(row)
    db_session.commit()
    _patch_safe_pipeline(monkeypatch, [])
    result = publisher.publish_forward_physical_refresh_current(
        db_session, target_generation_id=target.id, parent_generation_id=parent.id,
        delta_manifest={"rows": (row,), "supersessions": ()}, odata_client=None,
        source_revision=1, planning_pool_by_warehouse={},
    )
    assert result.input_delta_rows == 1
    assert result.affected_scopes == ()


def test_cutoff_adjustment_spoof_is_rejected_before_stock_writer(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-9", item_name="Current publish item")
    db_session.add(item)
    db_session.flush()
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-adjustment-spoof", business_identity="current-publish-adjustment-spoof",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=target.cutoff, record_type="Adjustment",
        movement_kind=CUTOFF_BALANCE_ADJUSTMENT_RECORDER_TYPE,
        recorder_type="fake", recorder_ref="cutoff", line_no="1", ingest_source="fake",
    )
    db_session.add(row)
    db_session.commit()
    called = []
    monkeypatch.setattr(publisher, "apply_bounded_current_stock_bins", lambda *a, **k: called.append(True))
    with pytest.raises(publisher.ForwardPhysicalRefreshUnavailable, match="canonical source"):
        publisher.publish_forward_physical_refresh_current(
            db_session, target_generation_id=target.id, parent_generation_id=parent.id,
            delta_manifest={"rows": (row,), "supersessions": ()}, odata_client=None,
            source_revision=1, planning_pool_by_warehouse={},
        )
    assert called == []


def test_publisher_has_no_commit_boundary_and_empty_delta_is_noop(db_session):
    source = inspect.getsource(publisher.publish_forward_physical_refresh_current)
    assert "db.commit" not in source
    parent, target = _generations(db_session)
    with pytest.raises(publisher.ForwardPhysicalRefreshUnavailable, match="no-op"):
        publisher.publish_forward_physical_refresh_current(
            db_session, target_generation_id=target.id, parent_generation_id=parent.id,
            delta_manifest={"rows": (), "supersessions": ()}, odata_client=None,
            source_revision=1, planning_pool_by_warehouse={"wh": "pool"},
        )


def test_phase_status_is_live_and_cleared_with_bounded_timings(db_session, monkeypatch):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-PHASE", item_name="Phase status item")
    db_session.add(item)
    db_session.flush()
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-phase-status", business_identity="current-publish-phase-status",
        item_id=item.item_id, characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=target.cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="expense", recorder_type="Document_Transfer", recorder_ref="phase", line_no="1",
        ingest_source="test",
    )
    db_session.add(row)
    db_session.commit()
    phases = []
    _patch_safe_pipeline(monkeypatch, phases)
    observed = []

    def stock(*args, **kwargs):
        observed.append(publisher.physical_refresh_phase_status(target.id))
        return SimpleNamespace(changed_keys=1)

    monkeypatch.setattr(publisher, "apply_bounded_current_stock_bins", stock)
    result = publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        delta_manifest={"rows": (row,), "supersessions": ()},
        odata_client=None,
        source_revision=target.physical_import_batch_id,
        planning_pool_by_warehouse={"wh": "pool"},
    )
    assert observed and observed[0]["current_phase"] == "stock"
    timings = dict(result.phase_timings)
    assert "stock" in timings and timings["stock"] >= 0
    assert "assembly_payload" in timings
    assert publisher.physical_refresh_phase_status(target.id) == {}


def test_phase_failure_snapshot_is_attached_and_retained_after_live_cleanup(
    db_session, monkeypatch
):
    parent, target = _generations(db_session)
    item = models.Item(item_code="CP-PHASE-FAIL", item_name="Phase failure item")
    db_session.add(item)
    db_session.flush()
    row = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="current-publish-phase-failure",
        business_identity="current-publish-phase-failure",
        item_id=item.item_id, characteristic_ref="", organization_ref="org",
        warehouse_ref1c="wh", qty=Decimal("1"),
        posting_at=target.cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="expense", recorder_type="Document_Transfer",
        recorder_ref="phase-failure", line_no="1", ingest_source="test",
    )
    db_session.add(row)
    db_session.commit()
    phases = []
    _patch_safe_pipeline(monkeypatch, phases)

    def fail_output(*args, **kwargs):
        raise RuntimeError("nullable output owner id")

    monkeypatch.setattr(publisher, "apply_bounded_assembly_output_plan_execution", fail_output)
    with pytest.raises(RuntimeError, match="nullable output owner id") as caught:
        publisher.publish_forward_physical_refresh_current(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            delta_manifest={"rows": (row,), "supersessions": ()},
            odata_client=None,
            source_revision=target.physical_import_batch_id,
            planning_pool_by_warehouse={"wh": "pool"},
        )

    failure = getattr(caught.value, "physical_refresh_phase_status")
    assert failure["failed"] is True
    assert failure["current_phase"] == "assembly_output"
    assert "assembly_output" in failure["phase_timings"]
    assert publisher.physical_refresh_phase_status(target.id) == failure


def test_bounded_owner_missing_required_identity_has_diagnostic():
    with pytest.raises(ValueError, match="missing run_id"):
        output_persistence._persist_bounded_owner_rows(
            None,
            SimpleNamespace(cutoff=datetime.now(timezone.utc)),
            [{
                "stock_ledger_entry_id": 11,
                "run_id": None,
                "plan_id": 3,
                "plan_line_id": 4,
                "allocated_qty": "1",
                "match_rule": "fifo",
            }],
        )

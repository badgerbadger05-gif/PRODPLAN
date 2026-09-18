"""The bounded hourly refresh owns every current scope of the accepted pointer.

Two production defects are covered here:

* a bounded publication advanced only the execution scopes and the two
  journals, so ``mrp_result`` and ``period_plan_execution`` stayed pinned to
  the previous generation and their exact-pointer readers failed closed;
* a refresh with an empty delta published nothing at all, so a scope
  invalidated between refreshes by a reference writer (specification import,
  calendar, rates, resources, custody) stayed unavailable until a real delta
  happened to arrive.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import models
from app.services.item_ledger import historical_bootstrap_phase0 as bootstrap
from app.services.item_ledger import historical_import_orchestration as importer
from app.services.item_ledger import physical_refresh_current_publish as publisher
from app.services.item_ledger import physical_refresh_generation
from app.services.item_ledger import physical_refresh_orchestrator as workflow
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    publish_current_execution_from_generation,
    publish_current_execution_scope,
    publish_current_obligation_views_from_generation,
    require_current_execution_scope,
)
from app.services.item_ledger.physical import LedgerKey
from app.services.specification_revision import record_specification_revisions


_EMPTY_JOURNAL_PAYLOAD = {"rows": [], "meta": {"read_only": True, "fact_source": "ledger"}}

_QUEUE_ROWS = (
    {
        "entity_kind": "assembly_queue",
        "business_identity": "plan-line:1",
        "scope_key": "assembly:all-live-plans",
        "payload": {"plan_line_id": 1, "assembly_remaining_qty": "3"},
    },
)


def _generations(db_session, *, key="scope-repair", with_target=True):
    parent_cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    target_cutoff = parent_cutoff + timedelta(days=1)
    parent_batch = models.PhysicalImportBatch(
        batch_key=f"{key}-parent-batch", status="completed", source_complete=True,
        cutoff=parent_cutoff, source_watermarks={}, completed_at=parent_cutoff,
    )
    target_batch = models.PhysicalImportBatch(
        batch_key=f"{key}-target-batch", status="completed", source_complete=True,
        cutoff=target_cutoff, source_watermarks={}, completed_at=target_cutoff,
    )
    parent = models.LedgerGeneration(
        generation_key=f"{key}-parent", status="accepted", cutoff=parent_cutoff,
        source_watermarks={}, capabilities={"physical_ledger": True},
        physical_import_batch=parent_batch, algorithm_version="test",
        accepted_at=parent_cutoff,
    )
    target = models.LedgerGeneration(
        generation_key=f"{key}-target", status="building", cutoff=target_cutoff,
        source_watermarks={"parent_generation_id": None}, capabilities={},
        physical_import_batch=target_batch, algorithm_version="test",
    )
    warehouse = models.StockWarehouse(
        warehouse_ref1c=f"WH-{key}", warehouse_name="Planning contour",
        is_selected=True, is_finished_goods=False,
    )
    db_session.add_all([parent_batch, parent, warehouse])
    if with_target:
        db_session.add(target_batch)
    db_session.flush()
    if with_target:
        target.physical_import_batch_id = target_batch.id
        target.source_watermarks = {"parent_generation_id": int(parent.id)}
        db_session.add(target)
        db_session.flush()
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    db_session.commit()
    return parent, (target if with_target else None)


def _publish_all_current_scopes(db_session, generation_id):
    """Seed the complete set of 11 current scopes for one accepted pointer."""
    publish_current_execution_scope(
        db_session,
        source_revision=f"seed:g{int(generation_id)}",
        source_generation_id=int(generation_id),
        scope_key="assembly:all-live-plans",
        rows=_QUEUE_ROWS,
        entity_kinds=("assembly_queue",),
        summary={"total_rows": len(_QUEUE_ROWS)},
    )
    for scope_key, kinds in (
        ("assembly:all-live-plans", ("assembly_readiness",)),
        ("drum:all-live-plans", ("drum_schedule", "drum_slot", "drum_gap", "drum_excluded")),
        ("shelf:all-live-mrps", ("shelf_projection",)),
    ):
        publish_current_execution_scope(
            db_session,
            source_revision=f"seed:g{int(generation_id)}",
            source_generation_id=int(generation_id),
            scope_key=scope_key,
            rows=(),
            entity_kinds=kinds,
            summary={"total_rows": 0},
        )
    publish_current_obligation_views_from_generation(
        db_session,
        int(generation_id),
        purchase_payload=dict(_EMPTY_JOURNAL_PAYLOAD),
        production_payload=dict(_EMPTY_JOURNAL_PAYLOAD),
        mrp_payloads={},
        period_payloads={},
        period_run_ids=(),
    )
    db_session.flush()


def _manifests(db_session):
    return {
        (str(row.entity_kind), str(row.scope_key)): (
            int(row.source_generation_id or 0), bool(row.result_ready)
        )
        for row in db_session.query(models.CurrentExecutionScope).all()
    }


def _row_state(db_session):
    return {
        int(row.id): (
            str(row.content_hash), dict(row.payload or {}),
            str(row.result_status), bool(row.result_ready),
        )
        for row in db_session.query(models.CurrentExecutionRow).all()
    }


def _patch_compute_only(monkeypatch):
    """Stub only the heavy compute; every canonical publisher stays real."""
    monkeypatch.setattr(
        publisher, "apply_bounded_current_stock_bins",
        lambda *a, **kw: SimpleNamespace(changed_keys=0),
    )
    monkeypatch.setattr(
        publisher, "apply_bounded_assembly_output_plan_execution",
        lambda *a, **kw: SimpleNamespace(metrics={}),
    )
    monkeypatch.setattr(
        publisher, "apply_bounded_current_material_custody_events", lambda *a, **kw: 0,
    )
    monkeypatch.setattr(
        publisher, "handoff_current_physical_refresh_provenance",
        lambda *a, **kw: SimpleNamespace(),
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_assembly_payload",
        lambda *a, **kw: SimpleNamespace(
            queue_rows=_QUEUE_ROWS, readiness_rows=(), readiness_metrics={},
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
        lambda *a, **kw: dict(_EMPTY_JOURNAL_PAYLOAD),
    )
    monkeypatch.setattr(
        publisher, "build_compact_current_purchase_control_payload",
        lambda *a, **kw: dict(_EMPTY_JOURNAL_PAYLOAD),
    )
    monkeypatch.setattr(publisher, "_fixed_run_ids", lambda db: ())
    monkeypatch.setattr(
        publisher, "_build_obligation_view_payloads", lambda *a, **kw: ({}, {}),
    )


def test_bounded_publish_advances_every_current_manifest_to_the_new_pointer(
    db_session, monkeypatch,
):
    parent, target = _generations(db_session, key="advance")
    _publish_all_current_scopes(db_session, parent.id)
    db_session.commit()
    assert set(_manifests(db_session).values()) == {(parent.id, True)}

    item = models.Item(item_code="CSR-1", item_name="Manifest advance item")
    db_session.add(item)
    db_session.flush()
    sle = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash="advance-sle", business_identity="advance-sle",
        item_id=item.item_id, characteristic_ref="", organization_ref="org",
        warehouse_ref1c="wh", qty=Decimal("2"),
        posting_at=target.cutoff - timedelta(hours=1), record_type="Receipt",
        movement_kind="transfer_out", recorder_type="Document_Transfer",
        recorder_ref="csr-1", line_no="1", ingest_source="test",
    )
    db_session.add(sle)
    db_session.commit()

    _patch_compute_only(monkeypatch)
    publisher.publish_forward_physical_refresh_current(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        delta_manifest={"rows": (sle,), "supersessions": ()},
        odata_client=None,
        source_revision=target.physical_import_batch_id,
        planning_pool_by_warehouse={"wh": "pool"},
    )
    db_session.flush()

    manifests = _manifests(db_session)
    assert len(manifests) == len(publisher.CURRENT_EXECUTION_SCOPE_KEYS)
    assert set(manifests.values()) == {(target.id, True)}
    assert int(db_session.get(models.PlanningTruthState, 1).current_generation_id) == target.id
    # Every current reader resolves against the advanced pointer, including the
    # two scopes the bounded path used to leave behind.
    for entity_kind, scope_key in publisher.CURRENT_EXECUTION_SCOPE_KEYS:
        require_current_execution_scope(
            db_session, entity_kind=entity_kind, scope_key=scope_key,
        )
    db_session.rollback()


def test_legacy_generation_publisher_refuses_to_close_a_live_scope(db_session):
    """A generation that staged nothing must not empty the live R8 scope."""
    parent, target = _generations(db_session, key="legacy-guard")
    _publish_all_current_scopes(db_session, parent.id)
    db_session.flush()
    before = db_session.query(models.CurrentExecutionRow).filter_by(
        result_status="accepted", result_ready=True,
    ).count()
    assert before > 0

    target.status = "accepted"
    target.accepted_at = target.cutoff
    db_session.flush()
    with pytest.raises(CurrentExecutionUnavailable, match="staged no assembly queue"):
        publish_current_execution_from_generation(db_session, generation_id=target.id)
    assert db_session.query(models.CurrentExecutionRow).filter_by(
        result_status="accepted", result_ready=True,
    ).count() == before
    db_session.rollback()


def _invalidate_via_specification_import(db_session):
    """Run the canonical specification-import hook, not a hand-made update."""
    spec = models.Specification(
        spec_code="SPEC-REPAIR", spec_name="Repair spec",
        spec_ref1c="spec-ref-repair", content_hash="stale-hash",
    )
    db_session.add(spec)
    db_session.flush()
    result = record_specification_revisions(
        db_session,
        [int(spec.spec_id)],
        previous_hash_by_id={int(spec.spec_id): "previous-hash"},
    )
    assert result["changed_spec_refs"] == ["spec-ref-repair"]
    db_session.flush()


def test_noop_refresh_republishes_invalidated_scopes_without_row_or_audit_churn(
    db_session, monkeypatch,
):
    parent, _target = _generations(db_session, key="noop-repair", with_target=False)
    _publish_all_current_scopes(db_session, parent.id)
    db_session.commit()

    target_cutoff = parent.cutoff + timedelta(days=1)
    batch = models.PhysicalImportBatch(
        batch_key="noop-repair-batch", status="completed", cutoff=target_cutoff,
        source_watermarks={}, completed_at=target_cutoff,
    )
    candidate = models.LedgerGeneration(
        generation_key="noop-repair-candidate", status="building", cutoff=target_cutoff,
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": parent.id,
            "replay_from": "2026-07-01T00:00:00+00:00",
        },
        capabilities={}, physical_import_batch=batch,
        algorithm_version="ledger-physical-refresh-generation/1",
        replay_version="ledger-physical-refresh-replay/1",
    )
    item = models.Item(item_code="CSR-NOOP", item_name="No-op repair item")
    db_session.add_all([batch, candidate, item])
    db_session.flush()
    db_session.add(models.StockBin(
        ledger_generation_id=parent.id, item_id=item.item_id,
        characteristic_ref="", organization_ref="", warehouse_ref1c="",
        on_hand=Decimal("4"), is_current=True,
    ))
    db_session.flush()

    _invalidate_via_specification_import(db_session)
    db_session.commit()
    invalidated = {
        key for key, (_generation, ready) in _manifests(db_session).items() if not ready
    }
    assert invalidated == {
        ("assembly_queue", "assembly:all-live-plans"),
        ("assembly_readiness", "assembly:all-live-plans"),
        ("drum_schedule", "drum:all-live-plans"),
        ("shelf_projection", "shelf:all-live-mrps"),
    }
    rows_before = _row_state(db_session)
    audit_before = db_session.query(models.CurrentExecutionChange).count()

    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=candidate.id, generation_key="noop-repair-candidate",
        physical_import_batch_id=batch.id, cutoff=target_cutoff,
        from_cutoff=parent.cutoff, created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=candidate.id, from_exclusive=parent.cutoff,
        cutoff=target_cutoff, completed_through=target_cutoff, windows_completed=1,
        windows_resumed=0, recorders_pulled=0, movements_inserted=0,
        complete=True, physical_import_batch_id=batch.id,
    )
    convergence = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=candidate.id, cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(), valid=True, content_hash="noop",
        compared=0, mismatched=0, matched=0, terminal_batch_id=batch.id, deltas=(),
    )
    monkeypatch.setattr(workflow, "fork_physical_refresh_generation", lambda *a, **k: fork_result)
    monkeypatch.setattr(workflow, "run_physical_recorder_audit", lambda *a, **k: object())
    monkeypatch.setattr(workflow, "run_historical_physical_import", lambda *a, **k: import_result)
    monkeypatch.setattr(
        bootstrap, "_aggregate_sles_for_convergence",
        lambda *a, **k: pytest.fail("no-op refresh scanned the historical SLE prefix"),
    )
    _patch_compute_only(monkeypatch)

    result = workflow.run_physical_refresh(
        db_session, generation_key="noop-repair-candidate", target_cutoff=target_cutoff,
        client=object(),
        balance_snapshot={LedgerKey(item.item_id, "", "", ""): Decimal("4")},
    )

    assert result.published is False
    assert result.input_delta_rows == 0
    # No new generation and no pointer move: the repair republishes the very
    # generation the pointer already names.
    assert int(db_session.get(models.PlanningTruthState, 1).current_generation_id) == parent.id
    assert db_session.get(models.LedgerGeneration, candidate.id).status == "rejected"
    assert set(result.repaired_scopes) == {
        "assembly_queue:assembly:all-live-plans",
        "assembly_readiness:assembly:all-live-plans",
        "drum_schedule:drum:all-live-plans",
        "shelf_projection:shelf:all-live-mrps",
    }
    manifests = _manifests(db_session)
    assert len(manifests) == len(publisher.CURRENT_EXECUTION_SCOPE_KEYS)
    assert set(manifests.values()) == {(parent.id, True)}
    for entity_kind, scope_key in publisher.CURRENT_EXECUTION_SCOPE_KEYS:
        require_current_execution_scope(
            db_session, entity_kind=entity_kind, scope_key=scope_key,
        )
    # Restoring an unchanged business payload is a readiness repair only.
    rows_after = _row_state(db_session)
    assert set(rows_after) == set(rows_before)
    for row_id, after in rows_after.items():
        before = rows_before[row_id]
        assert after[0] == before[0]
        assert after[1] == before[1]
        assert after[2] == before[2]
        assert after[3] is True
    assert db_session.query(models.CurrentExecutionChange).count() == audit_before


def test_noop_refresh_leaves_ready_scopes_completely_untouched(db_session, monkeypatch):
    parent, _target = _generations(db_session, key="noop-clean", with_target=False)
    _publish_all_current_scopes(db_session, parent.id)
    db_session.commit()

    target_cutoff = parent.cutoff + timedelta(days=1)
    batch = models.PhysicalImportBatch(
        batch_key="noop-clean-batch", status="completed", cutoff=target_cutoff,
        source_watermarks={}, completed_at=target_cutoff,
    )
    candidate = models.LedgerGeneration(
        generation_key="noop-clean-candidate", status="building", cutoff=target_cutoff,
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": parent.id,
            "replay_from": "2026-07-01T00:00:00+00:00",
        },
        capabilities={}, physical_import_batch=batch,
        algorithm_version="ledger-physical-refresh-generation/1",
        replay_version="ledger-physical-refresh-replay/1",
    )
    item = models.Item(item_code="CSR-CLEAN", item_name="Clean no-op item")
    db_session.add_all([batch, candidate, item])
    db_session.flush()
    db_session.add(models.StockBin(
        ledger_generation_id=parent.id, item_id=item.item_id,
        characteristic_ref="", organization_ref="", warehouse_ref1c="",
        on_hand=Decimal("4"), is_current=True,
    ))
    db_session.commit()
    rows_before = _row_state(db_session)
    audit_before = db_session.query(models.CurrentExecutionChange).count()

    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=candidate.id, generation_key="noop-clean-candidate",
        physical_import_batch_id=batch.id, cutoff=target_cutoff,
        from_cutoff=parent.cutoff, created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=candidate.id, from_exclusive=parent.cutoff,
        cutoff=target_cutoff, completed_through=target_cutoff, windows_completed=1,
        windows_resumed=0, recorders_pulled=0, movements_inserted=0,
        complete=True, physical_import_batch_id=batch.id,
    )
    monkeypatch.setattr(workflow, "fork_physical_refresh_generation", lambda *a, **k: fork_result)
    monkeypatch.setattr(workflow, "run_physical_recorder_audit", lambda *a, **k: object())
    monkeypatch.setattr(workflow, "run_historical_physical_import", lambda *a, **k: import_result)
    monkeypatch.setattr(
        publisher, "build_compact_current_assembly_payload",
        lambda *a, **kw: pytest.fail("a ready scope must not be recomputed"),
    )

    result = workflow.run_physical_refresh(
        db_session, generation_key="noop-clean-candidate", target_cutoff=target_cutoff,
        client=object(),
        balance_snapshot={LedgerKey(item.item_id, "", "", ""): Decimal("4")},
    )

    assert result.published is False
    assert result.repaired_scopes == ()
    assert _row_state(db_session) == rows_before
    assert db_session.query(models.CurrentExecutionChange).count() == audit_before
    assert set(_manifests(db_session).values()) == {(parent.id, True)}

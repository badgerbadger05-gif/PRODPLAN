"""Targeted contract tests for the physical refresh orchestrator."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event

from app import models
from app.services import planning_truth
from app.services.item_ledger import historical_bootstrap_phase0 as bootstrap
from app.services.item_ledger import historical_import_orchestration as importer
from app.services.item_ledger import physical_refresh_generation
from app.services.item_ledger import physical_refresh_orchestrator as workflow
from app.services.item_ledger import physical_refresh_stock_bin as stock_bin
from app.services.item_ledger.physical import (
    LedgerKey,
    guard_physical_batch_writer,
    physical_sequence_lock_context,
)
from app.services.item_ledger.ingest import (
    HistoricalPullBeyondCutoffError,
    PullResult,
)
from app.services.item_ledger.r3_contract import (
    business_identity_for_cutoff_balance_adjustment,
)


def test_physical_lifecycle_lock_uses_dedicated_connection_across_commits():
    events = []

    class Connection:
        def execute(self, *_args, **_kwargs):
            events.append("execute")
            return type("Result", (), {"fetchone": lambda self: (True,)})()

        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

        def close(self):
            events.append("close")

    connection = Connection()

    class Bind:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def connect(self):
            events.append("connect")
            return connection

    class DB:
        def get_bind(self):
            return Bind()

        def commit(self):
            events.append("worker-commit")

    lock = workflow._acquire_lifecycle_lock(DB())
    assert lock is connection
    DB().commit()
    workflow._release_lifecycle_lock(lock)
    assert events == ["connect", "execute", "commit", "worker-commit", "execute", "commit", "close"]


def test_lifecycle_guard_skips_nested_sequence_advisory_lock_only_in_context():
    events = []

    class DB:
        def get_bind(self):
            return type(
                "Bind",
                (),
                {"dialect": type("Dialect", (), {"name": "postgresql"})()},
            )()

        def execute(self, *_args, **_kwargs):
            events.append("execute")

    db = DB()
    guard_physical_batch_writer(db)
    assert events == ["execute"]
    with physical_sequence_lock_context():
        guard_physical_batch_writer(db)
    assert events == ["execute"]


def _moscow_naive(instant):
    """Convert a UTC instant into the ledger's canonical naive Europe/Moscow
    local wall-clock form (see ingest._posting_at_local /
    physical_refresh_orchestrator._posting_at_utc).

    ``instant`` may itself be naive: after a commit, SQLAlchemy expires and
    reloads DateTime(timezone=True) attributes from SQLite without tzinfo,
    but the reloaded wall-clock digits are still the original UTC ones (e.g.
    ``LedgerGeneration.cutoff``). Naive input is therefore treated as UTC
    before converting to Moscow local time.
    """
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)


def _accepted_parent(db_session, *, generation_key="accepted-parent"):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    parent_batch = models.PhysicalImportBatch(
        batch_key=f"accepted-physical:{generation_key}",
        status="completed",
        cutoff=cutoff,
        source_watermarks={"rows_read": 123},
        completed_at=cutoff,
    )
    parent = models.LedgerGeneration(
        generation_key=generation_key,
        status="accepted",
        cutoff=cutoff,
        source_watermarks={
            "replay_from": "2026-07-01T00:00:00+00:00",
        },
        capabilities={"physical_ledger": True},
        physical_import_batch=parent_batch,
        algorithm_version="accepted/1",
        accepted_at=cutoff,
    )
    warehouse = models.StockWarehouse(
        warehouse_ref1c="WH-PHYSICAL-PLAN",
        warehouse_name="Physical planning contour",
        is_selected=True,
        is_finished_goods=False,
    )
    db_session.add_all([parent_batch, parent, warehouse])
    db_session.flush()
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    db_session.commit()
    return parent, parent_batch


def _terminal_boundary_world(db_session, *, key: str, exact_cutoff: bool = False):
    parent, _ = _accepted_parent(db_session, generation_key=f"{key}-parent")
    target_cutoff = parent.cutoff + timedelta(days=1)
    auxiliary = models.PhysicalImportBatch(
        batch_key=f"{key}-auxiliary",
        status="completed",
        source_complete=True,
        cutoff=target_cutoff if exact_cutoff else parent.cutoff + timedelta(hours=2),
        completed_at=target_cutoff,
        source_watermarks={"source": "targeted_recorder_repair"},
    )
    generation = models.LedgerGeneration(
        generation_key=f"{key}-generation",
        status="building",
        cutoff=target_cutoff,
        physical_import_batch=auxiliary,
        source_watermarks={"parent_generation_id": int(parent.id)},
        algorithm_version="physical-refresh/test",
        replay_version="physical-refresh/test",
    )
    db_session.add_all([auxiliary, generation])
    db_session.flush()
    convergence = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=int(generation.id),
        cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(),
        valid=True,
        content_hash=f"{key}-balance",
        compared=17,
        mismatched=0,
        matched=17,
        terminal_batch_id=int(auxiliary.id),
        deltas=(),
    )
    return generation, auxiliary, convergence


def test_terminal_boundary_seals_auxiliary_prefix_at_generation_cutoff(db_session):
    generation, auxiliary, convergence = _terminal_boundary_world(
        db_session, key="terminal-seal"
    )
    auxiliary_cutoff = auxiliary.cutoff

    boundary = workflow._ensure_terminal_physical_boundary(
        db_session, generation=generation, convergence=convergence
    )

    assert int(boundary.id) > int(auxiliary.id)
    assert int(generation.physical_import_batch_id) == int(boundary.id)
    assert workflow._utc(boundary.cutoff, "boundary") == workflow._utc(
        generation.cutoff, "generation"
    )
    assert boundary.status == "completed"
    assert boundary.source_complete is True
    assert boundary.source_watermarks["source"] == workflow._TERMINAL_BOUNDARY_SOURCE
    assert boundary.source_watermarks["previous_import_batch_id"] == auxiliary.id
    assert boundary.source_watermarks["convergence"]["content_hash"] == "terminal-seal-balance"
    assert db_session.query(models.StockLedgerEntry).filter_by(
        ingest_batch_id=boundary.id
    ).count() == 0
    assert auxiliary.cutoff == auxiliary_cutoff


def test_terminal_boundary_reuses_existing_exact_boundary(db_session):
    generation, exact, convergence = _terminal_boundary_world(
        db_session, key="terminal-exact", exact_cutoff=True
    )
    before = db_session.query(models.PhysicalImportBatch).count()

    boundary = workflow._ensure_terminal_physical_boundary(
        db_session, generation=generation, convergence=convergence
    )

    assert boundary.id == exact.id
    assert db_session.query(models.PhysicalImportBatch).count() == before


def test_terminal_boundary_retry_reuses_same_zero_row_boundary(db_session):
    generation, auxiliary, convergence = _terminal_boundary_world(
        db_session, key="terminal-retry"
    )
    first = workflow._ensure_terminal_physical_boundary(
        db_session, generation=generation, convergence=convergence
    )
    generation.physical_import_batch_id = auxiliary.id
    db_session.flush()

    second = workflow._ensure_terminal_physical_boundary(
        db_session, generation=generation, convergence=convergence
    )

    assert second.id == first.id
    assert generation.physical_import_batch_id == first.id
    assert db_session.query(models.PhysicalImportBatch).filter_by(
        batch_key=first.batch_key
    ).count() == 1


def test_terminal_boundary_retry_with_changed_evidence_fails_closed(db_session):
    generation, auxiliary, convergence = _terminal_boundary_world(
        db_session, key="terminal-mismatch"
    )
    boundary = workflow._ensure_terminal_physical_boundary(
        db_session, generation=generation, convergence=convergence
    )
    boundary.source_watermarks = {
        **dict(boundary.source_watermarks),
        "evidence_hash": "changed",
    }
    generation.physical_import_batch_id = auxiliary.id
    db_session.flush()

    with pytest.raises(
        workflow.PhysicalRefreshOrchestratorError,
        match="retry evidence changed",
    ):
        workflow._ensure_terminal_physical_boundary(
            db_session, generation=generation, convergence=convergence
        )

    assert generation.physical_import_batch_id == auxiliary.id


@pytest.mark.parametrize("batch_shape", [
    "single", "shared", "shared-missing", "shared-incomplete", "no-checkpoint",
])
def test_crash_recovery_discovers_completed_custody_tail_with_sequence_gaps(
    db_session, batch_shape,
):
    parent, parent_batch = _accepted_parent(
        db_session, generation_key="custody-crash-recovery"
    )
    target_cutoff = parent.cutoff + timedelta(days=1)
    item = models.Item(
        item_code="CUSTODY-CRASH-ITEM", item_name="custody crash item",
    )
    order = models.ProductionOrder(
        order_number="CUSTODY-CRASH-ORDER",
        order_date=parent.cutoff,
        order_ref1c="custody-crash-order",
    )
    db_session.add_all([item, order])
    db_session.flush()
    product = models.ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        quantity=Decimal("1"),
        remaining_qty=Decimal("1"),
        produced_qty=Decimal("0"),
    )
    batch = models.PhysicalImportBatch(
        batch_key="custody-crash-retry-batch",
        status="completed",
        cutoff=target_cutoff,
        source_complete=True,
        source_watermarks={
            "source": "AccumulationRegister_ЗапасыНаСкладах",
            "recorder_type": "Document_Transfer",
            "recorder_ref": "custody-crash-recorder",
            "previous_import_batch_id": int(parent_batch.id),
        },
    )
    db_session.add_all([product, batch])
    db_session.flush()
    candidate = models.LedgerGeneration(
        generation_key="custody-crash-retry-candidate", status="building",
        cutoff=target_cutoff, physical_import_batch_id=batch.id,
        source_watermarks={"generation_kind": "physical_refresh",
                           "parent_generation_id": parent.id},
        algorithm_version="physical-refresh/test", replay_version="physical-refresh/test",
    )
    db_session.add(candidate)
    db_session.flush()
    db_session.add(models.LedgerBuildBatch(
        ledger_generation_id=candidate.id, stage="physical_import",
        batch_key="custody-crash-audit", status="completed",
        algorithm_version="physical-refresh/test",
        metrics={"parent_physical_import_batch_id": parent_batch.id,
                 "physical_import_batch_id": batch.id,
                 "recorders": [{"recorder_type": "Document_Transfer",
                                "recorder_ref": "custody-crash-recorder"}]},
    ))
    db_session.flush()
    entries = [
        models.StockLedgerEntry(
            ingest_batch_id=batch.id,
            source_content_hash=f"custody-crash-{line}",
            business_identity=f"custody-crash-{line}",
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="org",
            warehouse_ref1c="WH-CUSTODY-CRASH",
            qty=Decimal("1"),
            posting_at=target_cutoff,
            record_type="Receipt",
            movement_kind="transfer_in",
            recorder_type="Document_Transfer",
            recorder_ref="custody-crash-recorder",
            line_no=str(line),
            ingest_source="pull",
            active=True,
        )
        for line in ("1", "2")
    ]
    db_session.add_all(entries)
    db_session.flush()
    if batch_shape.startswith("shared"):
        # Window imports persist all recorder membership while scalar fields
        # retain only the last recorder, as in staged batch 15855.
        batch.source_watermarks = {
            **batch.source_watermarks,
            "recorder_ref": "last-unrelated-recorder",
            "recorders": [
                {"recorder_type": "Document_Transfer",
                 "recorder_ref": "custody-crash-recorder",
                 "status": "failed" if batch_shape == "shared-incomplete" else "done"},
                {"recorder_type": "Document_Transfer",
                 "recorder_ref": "last-unrelated-recorder", "status": "done"},
            ] if batch_shape != "shared-missing" else [],
        }
    if batch_shape == "no-checkpoint":
        checkpoint = db_session.query(models.LedgerBuildBatch).filter_by(
            ledger_generation_id=candidate.id, stage="physical_import",
        ).one()
        checkpoint.status = "building"
    db_session.add_all([
        models.ProductionMaterialCustodyEvent(
            id=4588,
            product_id=product.product_id,
            component_item_id=item.item_id,
            source_kind="transfer_posted",
            source_sle_id=entries[0].id,
            effective_at=target_cutoff,
            location_kind="workshop",
            warehouse_ref1c="WH-CUSTODY-CRASH",
            delta_qty=Decimal("1"),
            idempotency_key="custody-crash-event-1",
        ),
        models.ProductionMaterialCustodyEvent(
            id=4641,
            product_id=product.product_id,
            component_item_id=item.item_id,
            source_kind="transfer_posted",
            source_sle_id=entries[1].id,
            effective_at=target_cutoff,
            location_kind="workshop",
            warehouse_ref1c="WH-CUSTODY-CRASH",
            delta_qty=Decimal("1"),
            idempotency_key="custody-crash-event-2",
        ),
    ])
    db_session.flush()

    if batch_shape in {"shared-missing", "shared-incomplete", "no-checkpoint"}:
        with pytest.raises(workflow.PhysicalRefreshOrchestratorError, match="foreign batch lineage"):
            workflow._bounded_custody_tail_sle_ids(
                db_session, after_event_id=4263,
                parent_generation_id=parent.id, target_cutoff=target_cutoff,
            )
        return

    sle_statements = []
    connection = db_session.connection()

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "stock_ledger_entry" in statement.lower():
            sle_statements.append(statement.lower())

    event.listen(connection, "before_cursor_execute", capture)
    try:
        first = workflow._bounded_custody_tail_sle_ids(
            db_session,
            after_event_id=4263,
            parent_generation_id=parent.id,
            target_cutoff=target_cutoff,
        )
        second = workflow._bounded_custody_tail_sle_ids(
            db_session,
            after_event_id=4263,
            parent_generation_id=parent.id,
            target_cutoff=target_cutoff,
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)

    assert first == second == (int(entries[0].id), int(entries[1].id))
    assert len(sle_statements) == 4
    assert all(" in (" in statement for statement in sle_statements)
    assert db_session.query(models.ProductionMaterialCustodyEvent).count() == 2


def test_retry_backfill_is_proved_by_corrected_candidate_not_restored_parent(db_session):
    from app.services.item_ledger.physical_refresh_provenance import canonical_issue_backfill_source_ids, PhysicalRefreshProvenanceUnavailable
    from app.services.production_material_custody_events import _custody_event_idempotency_key
    from tests.services.test_physical_refresh_provenance import _canonical_backfill_world

    parent, target, corrected, opening, posted, _ = _canonical_backfill_world(db_session)
    at = parent.cutoff - timedelta(days=1)
    old = models.StockLedgerEntry(
        ingest_batch_id=parent.physical_import_batch_id,
        source_content_hash="restored-64", business_identity=corrected.business_identity,
        item_id=corrected.item_id, characteristic_ref="", organization_ref="org",
        warehouse_ref1c=corrected.warehouse_ref1c, qty=Decimal("-64"),
        posting_at=at, record_type="Expense", movement_kind="transfer_out",
        recorder_type=corrected.recorder_type, recorder_ref=corrected.recorder_ref,
        line_no=corrected.line_no, ingest_source="test", active=False,
    )
    db_session.add(old)
    db_session.flush()
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=old.id, new_sle_id=corrected.id,
        import_batch_id=target.physical_import_batch_id,
    ))
    corrected.qty = Decimal("-56")
    corrected.posting_at = at
    opening.delta_qty = Decimal("56")
    opening.effective_at = at
    posted.delta_qty = Decimal("-56")
    posted.effective_at = at
    opening.idempotency_key = _custody_event_idempotency_key(
        issue_id=opening.issue_id, line_id=int(opening.document_line_no), revision=1,
        source_kind="issue_created", location_kind="transit",
        warehouse_ref1c=opening.warehouse_ref1c, delta_qty=56, source_sle_id=None,
    )
    db_session.flush()
    with pytest.raises(PhysicalRefreshProvenanceUnavailable, match="no exact bounded transfer-out"):
        canonical_issue_backfill_source_ids(
            db_session, events=[opening], physical_import_batch_id=parent.physical_import_batch_id,
            target_cutoff=target.cutoff,
        )
    assert canonical_issue_backfill_source_ids(
        db_session, events=[opening], physical_import_batch_id=target.physical_import_batch_id,
        target_cutoff=target.cutoff, allowed_sle_ids={corrected.id},
    ) == (corrected.id,)


@pytest.mark.parametrize("missing_edge", [False, True])
def test_custody_tail_carries_exact_reimport_chain_into_publication(db_session, missing_edge):
    from tests.services.test_physical_refresh_provenance import _canonical_backfill_world

    parent, target, old, opening, posted, _ = _canonical_backfill_world(db_session)
    old.ingest_batch_id = parent.physical_import_batch_id
    old.posting_at = parent.cutoff
    old.active = False
    opening.effective_at = parent.cutoff
    posted.effective_at = parent.cutoff
    parent.physical_import_batch.source_watermarks = target.physical_import_batch.source_watermarks
    replacement = models.StockLedgerEntry(
        ingest_batch_id=target.physical_import_batch_id,
        source_content_hash=old.source_content_hash, business_identity=old.business_identity,
        item_id=old.item_id, characteristic_ref=old.characteristic_ref,
        organization_ref=old.organization_ref, warehouse_ref1c=old.warehouse_ref1c,
        qty=old.qty, posting_at=old.posting_at, record_type=old.record_type,
        movement_kind=old.movement_kind, recorder_type=old.recorder_type,
        recorder_ref=old.recorder_ref, line_no=old.line_no, ingest_source="test", active=True,
    )
    db_session.add(replacement)
    db_session.flush()
    if not missing_edge:
        db_session.add(models.StockLedgerFactSupersession(
            old_sle_id=old.id, new_sle_id=replacement.id,
            import_batch_id=target.physical_import_batch_id,
        ))
    db_session.flush()
    if missing_edge:
        with pytest.raises(workflow.PhysicalRefreshOrchestratorError):
            workflow._bounded_custody_tail_sle_ids(
                db_session, after_event_id=0, parent_generation_id=parent.id,
                target_generation_id=target.id, target_cutoff=target.cutoff,
            )
    else:
        assert workflow._bounded_custody_tail_sle_ids(
            db_session, after_event_id=0, parent_generation_id=parent.id,
            target_generation_id=target.id, target_cutoff=target.cutoff,
        ) == (old.id, replacement.id)


def test_custody_tail_recovery_rejects_local_or_foreign_lineage(
    db_session,
):
    parent, parent_batch = _accepted_parent(
        db_session, generation_key="custody-crash-reject"
    )
    target_cutoff = parent.cutoff + timedelta(days=1)
    item = models.Item(item_code="CUSTODY-REJECT-ITEM", item_name="reject")
    order = models.ProductionOrder(
        order_number="CUSTODY-REJECT-ORDER",
        order_date=parent.cutoff,
        order_ref1c="custody-reject-order",
    )
    db_session.add_all([item, order])
    db_session.flush()
    product = models.ProductionProduct(
        order_id=order.order_id, item_id=item.item_id,
        quantity=Decimal("1"), remaining_qty=Decimal("1"), produced_qty=Decimal("0"),
    )
    foreign_batch = models.PhysicalImportBatch(
        batch_key="custody-foreign-batch", status="completed", cutoff=target_cutoff,
        source_complete=True, source_watermarks={"source": "foreign"},
    )
    db_session.add_all([product, foreign_batch])
    db_session.flush()
    sle = models.StockLedgerEntry(
        ingest_batch_id=foreign_batch.id,
        source_content_hash="custody-reject-sle", business_identity="custody-reject-sle",
        item_id=item.item_id, characteristic_ref="", organization_ref="org",
        warehouse_ref1c="WH-CUSTODY-REJECT", qty=Decimal("1"), posting_at=target_cutoff,
        record_type="Receipt", movement_kind="transfer_in", recorder_type="Document_Transfer",
        recorder_ref="foreign", line_no="1", ingest_source="pull", active=True,
    )
    db_session.add(sle)
    db_session.flush()
    db_session.add(models.ProductionMaterialCustodyEvent(
        product_id=product.product_id, component_item_id=item.item_id,
        source_kind="transfer_posted", source_sle_id=sle.id, effective_at=target_cutoff,
        location_kind="workshop", warehouse_ref1c="WH-CUSTODY-REJECT",
        delta_qty=Decimal("1"), idempotency_key="custody-foreign-event",
    ))
    db_session.flush()
    with pytest.raises(workflow.PhysicalRefreshOrchestratorError, match="foreign batch lineage"):
        workflow._bounded_custody_tail_sle_ids(
            db_session, after_event_id=0,
            parent_generation_id=parent.id, target_cutoff=target_cutoff,
        )



def test_custody_tail_recovery_rejects_local_event(db_session):
    parent, _ = _accepted_parent(
        db_session, generation_key="custody-local-reject"
    )
    local_item = models.Item(item_code="CUSTODY-LOCAL-ITEM", item_name="local")
    local_order = models.ProductionOrder(
        order_number="CUSTODY-LOCAL-ORDER",
        order_date=parent.cutoff,
        order_ref1c="custody-local-order",
    )
    db_session.add_all([local_item, local_order])
    db_session.flush()
    local_product = models.ProductionProduct(
        order_id=local_order.order_id,
        item_id=local_item.item_id,
        quantity=Decimal("1"),
        remaining_qty=Decimal("1"),
        produced_qty=Decimal("0"),
    )
    db_session.add(local_product)
    db_session.flush()
    db_session.add(models.ProductionMaterialCustodyEvent(
        product_id=local_product.product_id,
        component_item_id=local_item.item_id,
        source_kind="issue_created",
        source_sle_id=None, effective_at=parent.cutoff, location_kind="workshop",
        warehouse_ref1c="WH", delta_qty=Decimal("1"), idempotency_key="custody-local-event",
    ))
    db_session.flush()
    with pytest.raises(workflow.PhysicalRefreshOrchestratorError, match="unproved local command"):
        workflow._bounded_custody_tail_sle_ids(
            db_session, after_event_id=0,
            parent_generation_id=parent.id, target_cutoff=parent.cutoff,
        )


def test_targeted_repair_pulls_only_recorder_touching_mismatch(
    db_session, monkeypatch
):
    parent, parent_batch = _accepted_parent(
        db_session, generation_key="targeted-repair"
    )
    item = models.Item(
        item_code="TARGETED-ITEM",
        item_name="Targeted item",
        item_ref1c="item-ref",
    )
    generation = models.LedgerGeneration(
        generation_key="targeted-repair-child",
        status="building",
        cutoff=parent.cutoff + timedelta(days=1),
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": parent.id,
        },
        physical_import_batch=parent_batch,
        algorithm_version="physical-refresh/test",
        replay_version="physical-refresh/test",
    )
    db_session.add_all([item, generation])
    db_session.commit()
    delta = bootstrap.BalanceConvergenceDelta(
        item_id=item.item_id,
        organization_ref="org-ref",
        warehouse_ref1c="WH-PHYSICAL-PLAN",
        balance_qty="2",
        ledger_qty="1",
        delta_qty="1",
        matched=False,
    )
    convergence = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff.isoformat(),
        checked_at=generation.cutoff.isoformat(),
        valid=False,
        content_hash="mismatch",
        compared=1,
        matched=0,
        mismatched=1,
        terminal_batch_id=parent_batch.id,
        deltas=(delta,),
    )

    class Client:
        def _make_request(self, entity, params):
            assert entity.endswith("_RecordType")
            assert "Номенклатура_Key eq guid'item-ref'" in params["$filter"]
            return {"value": [{
                "Recorder": "changed-recorder",
                "Recorder_Type": "StandardODATA.Document_ПеремещениеЗапасов",
                "Организация_Key": "org-ref",
                "СтруктурнаяЕдиница_Key": "WH-PHYSICAL-PLAN",
            }]}

    pulled = []
    monkeypatch.setattr(
        workflow,
        "opening_boundary",
        lambda db: (parent_batch, datetime(2026, 7, 1, tzinfo=timezone.utc)),
    )
    monkeypatch.setattr(
        workflow,
        "pull_recorder_movements",
        lambda db, recorder_type, recorder_ref, **kwargs: (
            pulled.append((recorder_type, recorder_ref))
            or PullResult(
                status="done",
                inserted=0,
                physical_import_batch_id=parent_batch.id,
            )
        ),
    )

    repaired = workflow._repair_mismatched_recorders(
        db_session,
        generation=generation,
        client=Client(),
        convergence=convergence,
    )

    assert repaired == 1
    assert pulled == [(
        "Document_ПеремещениеЗапасов",
        "changed-recorder",
    )]
    assert generation.source_watermarks["targeted_convergence_repair"][
        "recorder_count"
    ] == 1


def test_targeted_repair_repulls_known_recorder_missing_from_current_register(
    db_session, monkeypatch
):
    parent, parent_batch = _accepted_parent(
        db_session, generation_key="targeted-vanished"
    )
    item = models.Item(
        item_code="VANISHED-ITEM",
        item_name="Vanished item",
        item_ref1c="vanished-item-ref",
    )
    generation = models.LedgerGeneration(
        generation_key="targeted-vanished-child",
        status="building",
        cutoff=parent.cutoff + timedelta(days=1),
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": parent.id,
        },
        physical_import_batch=parent_batch,
        algorithm_version="physical-refresh/test",
        replay_version="physical-refresh/test",
    )
    db_session.add_all([item, generation])
    db_session.flush()
    db_session.add(models.StockLedgerEntry(
        source_content_hash="vanished-recorder-row",
        item_id=item.item_id,
        warehouse_ref1c="WH-PHYSICAL-PLAN",
        organization_ref="org-ref",
        qty=13,
        posting_at=parent.cutoff,
        movement_kind="receipt",
        recorder_type="Document_ПеремещениеЗапасов",
        recorder_ref="vanished-recorder",
        ingest_batch_id=parent_batch.id,
    ))
    db_session.commit()
    delta = bootstrap.BalanceConvergenceDelta(
        item_id=item.item_id,
        organization_ref="org-ref",
        warehouse_ref1c="WH-PHYSICAL-PLAN",
        balance_qty="0",
        ledger_qty="13",
        delta_qty="-13",
        matched=False,
    )
    convergence = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff.isoformat(),
        checked_at=generation.cutoff.isoformat(),
        valid=False,
        content_hash="vanished",
        compared=1,
        matched=0,
        mismatched=1,
        terminal_batch_id=parent_batch.id,
        deltas=(delta,),
    )

    class Client:
        def _make_request(self, _entity, _params):
            return {"value": []}

    pulled = []
    monkeypatch.setattr(
        workflow,
        "opening_boundary",
        lambda db: (parent_batch, datetime(2026, 7, 1, tzinfo=timezone.utc)),
    )
    monkeypatch.setattr(
        workflow,
        "pull_recorder_movements",
        lambda db, recorder_type, recorder_ref, **kwargs: (
            pulled.append((recorder_type, recorder_ref))
            or PullResult(status="empty", inserted=0, physical_import_batch_id=parent_batch.id)
        ),
    )

    repaired = workflow._repair_mismatched_recorders(
        db_session,
        generation=generation,
        client=Client(),
        convergence=convergence,
    )

    assert repaired == 1
    assert pulled == [("Document_ПеремещениеЗапасов", "vanished-recorder")]


def test_targeted_repair_defers_current_revision_beyond_candidate_cutoff(
    db_session, monkeypatch
):
    parent, parent_batch = _accepted_parent(
        db_session, generation_key="targeted-future"
    )
    item = models.Item(
        item_code="FUTURE-ITEM",
        item_name="Future revision item",
        item_ref1c="future-item-ref",
    )
    generation = models.LedgerGeneration(
        generation_key="targeted-future-child",
        status="building",
        cutoff=parent.cutoff + timedelta(days=1),
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": parent.id,
        },
        physical_import_batch=parent_batch,
        algorithm_version="physical-refresh/test",
        replay_version="physical-refresh/test",
    )
    db_session.add_all([item, generation])
    db_session.commit()
    delta = bootstrap.BalanceConvergenceDelta(
        item_id=item.item_id,
        organization_ref="org-ref",
        warehouse_ref1c="WH-PHYSICAL-PLAN",
        balance_qty="0",
        ledger_qty="1",
        delta_qty="-1",
        matched=False,
    )
    convergence = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff.isoformat(),
        checked_at=generation.cutoff.isoformat(),
        valid=False,
        content_hash="future",
        compared=1,
        matched=0,
        mismatched=1,
        terminal_batch_id=parent_batch.id,
        deltas=(delta,),
    )

    class Client:
        def _make_request(self, _entity, _params):
            return {"value": [{
                "Recorder": "future-recorder",
                "Recorder_Type": "StandardODATA.Document_ПеремещениеЗапасов",
                "Организация_Key": "org-ref",
                "СтруктурнаяЕдиница_Key": "WH-PHYSICAL-PLAN",
            }]}

    monkeypatch.setattr(
        workflow,
        "opening_boundary",
        lambda db: (parent_batch, datetime(2026, 7, 1, tzinfo=timezone.utc)),
    )
    monkeypatch.setattr(
        workflow,
        "pull_recorder_movements",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            HistoricalPullBeyondCutoffError("movement exceeds cutoff")
        ),
    )

    repaired = workflow._repair_mismatched_recorders(
        db_session,
        generation=generation,
        client=Client(),
        convergence=convergence,
    )

    assert repaired == 0
    repair = generation.source_watermarks["targeted_convergence_repair"]
    assert repair["recorder_count"] == 0
    assert repair["deferred_beyond_cutoff"] == [{
        "recorder_type": "Document_ПеремещениеЗапасов",
        "recorder_ref": "future-recorder",
    }]


def test_run_physical_refresh_no_work_on_lock_contention(db_session, monkeypatch):
    parent, _ = _accepted_parent(db_session)
    blocked = []

    monkeypatch.setattr(workflow, "_acquire_lifecycle_lock", lambda db: False)
    monkeypatch.setattr(
        workflow,
        "fork_physical_refresh_generation",
        lambda *args, **kwargs: blocked.append("fork") or pytest.fail("should not fork on lock contention"),
    )
    monkeypatch.setattr(
        workflow,
        "run_physical_recorder_audit",
        lambda *args, **kwargs: blocked.append("audit") or pytest.fail("should not audit on lock contention"),
    )
    monkeypatch.setattr(
        workflow,
        "run_historical_physical_import",
        lambda *args, **kwargs: blocked.append("import") or pytest.fail("should not import on lock contention"),
    )
    monkeypatch.setattr(
        workflow,
        "evaluate_physical_refresh_balance_convergence",
        lambda *args, **kwargs: blocked.append("balance") or pytest.fail(
            "should not validate balance on lock contention",
        ),
    )
    with pytest.raises(
        workflow.PhysicalRefreshOrchestratorError,
        match="another physical refresh is running",
    ):
        workflow.run_physical_refresh(
            db_session,
            generation_key="lock-contention",
            target_cutoff=parent.cutoff + timedelta(days=1),
            client=object(),
            balance_snapshot={},
        )

    assert blocked == []
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id


@pytest.mark.parametrize("custody_gate_rejects", [False, True])
def test_run_physical_refresh_nonzero_delta_publishes_bounded_current_state(
    db_session, monkeypatch, custody_gate_rejects
):
    parent, parent_batch = _accepted_parent(db_session)
    forked_batch = models.PhysicalImportBatch(
        batch_key="forked-physical",
        status="completed",
        cutoff=parent.cutoff + timedelta(days=1),
        source_watermarks={},
        completed_at=parent.cutoff + timedelta(days=1),
    )
    physical = models.LedgerGeneration(
        generation_key="physical-fork",
        status="building",
        cutoff=parent.cutoff + timedelta(days=1),
        source_watermarks={
            "replay_from": "2026-07-01T00:00:00+00:00",
            "parent_generation_id": parent.id,
        },
        physical_import_batch=forked_batch,
        algorithm_version="ledger-physical-refresh-generation/1",
        replay_version="ledger-physical-refresh-replay/1",
    )
    db_session.add_all([forked_batch, physical])
    db_session.flush()

    item = models.Item(item_code="PHYSICAL-DELTA", item_name="Physical delta")
    run = models.PlanningRun(status="FIXED_SNAPSHOT", ledger_generation_id=parent.id)
    db_session.add_all([item, run])
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id, item_id=item.item_id,
        period_from=parent.cutoff.date(),
        period_to=(parent.cutoff + timedelta(days=30)).date(),
    )
    db_session.add(requirement)
    db_session.flush()
    db_session.add(models.ReservationEntry(
        ledger_generation_id=parent.id, item_id=item.item_id,
        run_id=run.run_id, requirement_id=requirement.id,
        priority_period_from=parent.cutoff.date(),
        priority_period_to=(parent.cutoff + timedelta(days=30)).date(),
        realization_mode="make", reserved_qty=Decimal("5"),
        replenishment_required_qty=Decimal("5"),
        current_identity="physical-delta-owner", owner_kind="current", is_current=True,
    ))
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=forked_batch.id, source_content_hash="physical-delta-sle",
        business_identity="physical-delta-business", item_id=item.item_id,
        characteristic_ref="", organization_ref="", warehouse_ref1c="",
        qty=Decimal("2"), posting_at=_moscow_naive(parent.cutoff + timedelta(hours=1)),
        record_type="Receipt", movement_kind="assembly_in",
        recorder_type="Production", recorder_ref="physical-delta-rec", line_no="1",
        ingest_source="pull",
    ))
    db_session.commit()

    target_cutoff = parent.cutoff + timedelta(days=1)
    calls = []
    db_session.add(models.ProductionMaterialCustodyProjectionManifest(
        ledger_generation_id=parent.id, cutoff=parent.cutoff, status="complete",
        source_event_high_watermark_id=0,
    ))
    db_session.commit()

    original_custody_gate = workflow._bounded_custody_tail_sle_ids

    def custody_gate(db, **kwargs):
        # A retry opening can only be proved after importing its corrected
        # document into this candidate, never against the restored parent.
        assert calls == ["fork", "audit", "import", "balance"]
        assert kwargs["target_generation_id"] == physical.id
        calls.append("custody-gate")
        if custody_gate_rejects:
            raise workflow.PhysicalRefreshOrchestratorError("unproved custody tail")
        return original_custody_gate(db, **kwargs)

    monkeypatch.setattr(workflow, "_bounded_custody_tail_sle_ids", custody_gate)
    commit_calls = []
    original_commit = db_session.commit

    def _commit():
        commit_calls.append("commit")
        return original_commit()

    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=physical.id,
        generation_key="physical-fork",
        physical_import_batch_id=forked_batch.id,
        cutoff=target_cutoff,
        from_cutoff=parent.cutoff,
        created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=physical.id,
        from_exclusive=parent.cutoff,
        cutoff=target_cutoff,
        completed_through=target_cutoff,
        windows_completed=1,
        windows_resumed=0,
        recorders_pulled=0,
        movements_inserted=1,
        complete=True,
        physical_import_batch_id=forked_batch.id,
    )
    balance_result = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=physical.id,
        cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(),
        valid=True,
        content_hash="hash",
        compared=0,
        mismatched=0,
        matched=0,
        terminal_batch_id=forked_batch.id,
        deltas=(),
    )

    monkeypatch.setattr(workflow, "_acquire_lifecycle_lock", lambda db: True)
    monkeypatch.setattr(
        workflow,
        "fork_physical_refresh_generation",
        lambda *args, **kwargs: calls.append("fork") or fork_result,
    )
    monkeypatch.setattr(
        workflow,
        "run_physical_recorder_audit",
        lambda *args, **kwargs: calls.append("audit") or object(),
    )
    monkeypatch.setattr(
        workflow,
        "run_historical_physical_import",
        lambda *args, **kwargs: calls.append("import") or import_result,
    )
    monkeypatch.setattr(
        workflow,
        "evaluate_physical_refresh_balance_convergence",
        lambda *args, **kwargs: calls.append("balance") or balance_result,
    )
    def _publish(*args, **kwargs):
        calls.append("publish")
        candidate = db_session.get(models.LedgerGeneration, physical.id)
        candidate.status = "accepted"
        db_session.get(models.PlanningTruthState, 1).current_generation_id = physical.id
        return SimpleNamespace(
            input_delta_rows=1, replayed_rows=1,
            affected_scopes=(f"{item.item_id}:::pool:make",),
            phase_timings=(("stock", 2), ("production_payload", 7)),
        )
    monkeypatch.setattr(workflow, "publish_forward_physical_refresh_current", _publish)
    monkeypatch.setattr(db_session, "commit", _commit)

    if custody_gate_rejects:
        with pytest.raises(workflow.PhysicalRefreshOrchestratorError, match="unproved custody tail"):
            workflow.run_physical_refresh(
                db_session, generation_key="happy-path", target_cutoff=target_cutoff,
                client=object(), balance_snapshot={}, started_by="pytest",
            )
        assert "publish" not in calls
        assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id
        assert db_session.get(models.LedgerGeneration, physical.id).status == "rejected"
        return

    result = workflow.run_physical_refresh(
        db_session,
        generation_key="happy-path",
        target_cutoff=target_cutoff,
        client=object(),
        balance_snapshot={},
        started_by="pytest",
    )

    assert calls == ["fork", "audit", "import", "balance", "custody-gate", "publish"]
    assert commit_calls == ["commit", "commit"]
    assert result.published is True
    assert result.published_generation_id == physical.id
    assert result.replayed_rows == 1
    assert dict(result.phase_timings) == {"stock": 2, "production_payload": 7}
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == physical.id
    assert db_session.get(models.LedgerGeneration, physical.id).status == "accepted"
    owner = db_session.query(models.ReservationEntry).filter_by(
        current_identity="physical-delta-owner"
    ).one()
    assert owner.replenishment_received_qty == Decimal("0.000")
    assert db_session.query(models.ReservationEvent).filter(
        models.ReservationEvent.reservation_id == owner.id
    ).count() == 0


def test_balance_mismatch_stops_before_accept_or_obligation(db_session, monkeypatch):
    parent, _ = _accepted_parent(db_session)
    target_cutoff = parent.cutoff + timedelta(days=1)
    calls = []

    forked_batch = models.PhysicalImportBatch(
        batch_key="forked-physical",
        status="completed",
        cutoff=target_cutoff,
        source_watermarks={},
        completed_at=target_cutoff,
    )
    physical = models.LedgerGeneration(
        generation_key="physical-fork",
        status="building",
        cutoff=target_cutoff,
        source_watermarks={
            "replay_from": "2026-07-01T00:00:00+00:00",
            "parent_generation_id": parent.id,
        },
        physical_import_batch=forked_batch,
        algorithm_version="ledger-physical-refresh-generation/1",
        replay_version="ledger-physical-refresh-replay/1",
    )
    db_session.add_all([forked_batch, physical])
    db_session.flush()

    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=physical.id,
        generation_key="physical-fork",
        physical_import_batch_id=forked_batch.id,
        cutoff=target_cutoff,
        from_cutoff=parent.cutoff,
        created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=physical.id,
        from_exclusive=parent.cutoff,
        cutoff=target_cutoff,
        completed_through=target_cutoff,
        windows_completed=1,
        windows_resumed=0,
        recorders_pulled=0,
        movements_inserted=0,
        complete=True,
        physical_import_batch_id=forked_batch.id,
    )
    mismatch = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=physical.id,
        cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(),
        valid=False,
        content_hash="mismatch",
        compared=1,
        mismatched=2,
        matched=0,
        terminal_batch_id=forked_batch.id,
        deltas=(),
    )

    monkeypatch.setattr(workflow, "fork_physical_refresh_generation", lambda *args, **kwargs: calls.append("fork") or fork_result)
    monkeypatch.setattr(workflow, "run_physical_recorder_audit", lambda *args, **kwargs: calls.append("audit") or object())
    monkeypatch.setattr(workflow, "run_historical_physical_import", lambda *args, **kwargs: calls.append("import") or import_result)
    monkeypatch.setattr(workflow, "evaluate_physical_refresh_balance_convergence", lambda *args, **kwargs: calls.append("balance") or mismatch)

    with pytest.raises(
        workflow.PhysicalRefreshOrchestratorError,
        match="Balance convergence failed: 2 mismatches",
    ):
        workflow.run_physical_refresh(
            db_session,
            generation_key="balance-mismatch",
            target_cutoff=target_cutoff,
            client=object(),
            balance_snapshot={},
        )

    assert calls == ["fork", "audit", "import", "balance"]
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id


def test_changed_refresh_with_incomplete_manifest_uses_maintenance_path(db_session, monkeypatch):
    parent, _ = _accepted_parent(db_session)
    target_cutoff = parent.cutoff + timedelta(days=1)
    calls = []
    commit_calls = []
    original_commit = db_session.commit

    def _commit():
        commit_calls.append("commit")
        return original_commit()

    forked_batch = models.PhysicalImportBatch(
        batch_key="forked-physical",
        status="completed",
        cutoff=target_cutoff,
        source_watermarks={},
        completed_at=target_cutoff,
    )
    physical = models.LedgerGeneration(
        generation_key="physical-fork",
        status="building",
        cutoff=target_cutoff,
        source_watermarks={
            "replay_from": "2026-07-01T00:00:00+00:00",
            "parent_generation_id": parent.id,
        },
        physical_import_batch=forked_batch,
        algorithm_version="ledger-physical-refresh-generation/1",
        replay_version="ledger-physical-refresh-replay/1",
    )
    db_session.add_all([forked_batch, physical])
    db_session.flush()

    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=physical.id,
        generation_key="physical-fork",
        physical_import_batch_id=forked_batch.id,
        cutoff=target_cutoff,
        from_cutoff=parent.cutoff,
        created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=physical.id,
        from_exclusive=parent.cutoff,
        cutoff=target_cutoff,
        completed_through=target_cutoff,
        windows_completed=1,
        windows_resumed=0,
        recorders_pulled=0,
        movements_inserted=1,
        complete=True,
        physical_import_batch_id=forked_batch.id,
    )
    balance_result = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=physical.id,
        cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(),
        valid=True,
        content_hash="hash",
        compared=0,
        mismatched=0,
        matched=0,
        terminal_batch_id=forked_batch.id,
        deltas=(),
    )

    monkeypatch.setattr(workflow, "fork_physical_refresh_generation", lambda *args, **kwargs: calls.append("fork") or fork_result)
    monkeypatch.setattr(workflow, "run_physical_recorder_audit", lambda *args, **kwargs: calls.append("audit") or object())
    monkeypatch.setattr(workflow, "run_historical_physical_import", lambda *args, **kwargs: calls.append("import") or import_result)
    monkeypatch.setattr(workflow, "evaluate_physical_refresh_balance_convergence", lambda *args, **kwargs: calls.append("balance") or balance_result)
    publisher_calls = []
    monkeypatch.setattr(
        workflow,
        "publish_forward_physical_refresh_current",
        lambda *args, **kwargs: publisher_calls.append("publish") or pytest.fail(
            "incomplete manifest must not publish"
        ),
    )
    monkeypatch.setattr(db_session, "commit", _commit)

    with pytest.raises(
        workflow.PhysicalRefreshOrchestratorError,
        match="bounded physical delta manifest is incomplete",
    ):
        workflow.run_physical_refresh(
            db_session,
            generation_key="rollback-obligation",
            target_cutoff=target_cutoff,
            client=object(),
            balance_snapshot={},
        )

    assert calls == ["fork", "audit", "import", "balance"]
    assert publisher_calls == []
    assert commit_calls == ["commit", "commit"]
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id
    discarded = db_session.get(models.LedgerGeneration, physical.id)
    assert discarded.status == "rejected"


@pytest.mark.parametrize(
    ("failure_factory", "expected_type", "expected_message"),
    [
        (
            lambda: workflow.ForwardPhysicalRefreshUnavailable("bounded owner failure"),
            workflow.PhysicalRefreshOrchestratorError,
            "bounded owner failure",
        ),
        (
            lambda: RuntimeError("generic publisher failure"),
            RuntimeError,
            "generic publisher failure",
        ),
    ],
    ids=("bounded-unavailable", "generic-error"),
)
def test_publisher_failure_rolls_back_and_discards_candidate(
    db_session, monkeypatch, failure_factory, expected_type, expected_message,
):
    parent, _ = _accepted_parent(db_session, generation_key="publisher-failure-parent")
    target_cutoff = parent.cutoff + timedelta(days=1)
    batch = models.PhysicalImportBatch(
        batch_key="publisher-failure-batch", status="completed", cutoff=target_cutoff,
        source_watermarks={}, completed_at=target_cutoff,
    )
    physical = models.LedgerGeneration(
        generation_key="publisher-failure-target", status="building", cutoff=target_cutoff,
        source_watermarks={"parent_generation_id": parent.id}, capabilities={},
        physical_import_batch=batch, algorithm_version="test", replay_version="test",
    )
    item = models.Item(item_code="PUBLISHER-FAILURE", item_name="Publisher failure")
    db_session.add_all([batch, physical, item])
    db_session.flush()
    sle = models.StockLedgerEntry(
        ingest_batch_id=batch.id, source_content_hash="publisher-failure-sle",
        business_identity="publisher-failure-sle", item_id=item.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=_moscow_naive(parent.cutoff + timedelta(hours=1)),
        record_type="Receipt", movement_kind="transfer_out", recorder_type="Document_Transfer",
        recorder_ref="publisher-failure", line_no="1", ingest_source="pull",
    )
    db_session.add(sle)
    db_session.commit()
    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=physical.id, generation_key=physical.generation_key,
        physical_import_batch_id=batch.id, cutoff=target_cutoff,
        from_cutoff=parent.cutoff, created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=physical.id, from_exclusive=parent.cutoff,
        cutoff=target_cutoff, completed_through=target_cutoff, windows_completed=1,
        windows_resumed=0, recorders_pulled=0, movements_inserted=1,
        complete=True, physical_import_batch_id=batch.id,
    )
    convergence = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=physical.id, cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(), valid=True, content_hash="publisher-failure",
        compared=0, mismatched=0, matched=0, terminal_batch_id=batch.id, deltas=(),
    )
    monkeypatch.setattr(workflow, "_acquire_lifecycle_lock", lambda db: True)
    monkeypatch.setattr(workflow, "fork_physical_refresh_generation", lambda *a, **k: fork_result)
    monkeypatch.setattr(workflow, "run_physical_recorder_audit", lambda *a, **k: object())
    monkeypatch.setattr(workflow, "run_historical_physical_import", lambda *a, **k: import_result)
    monkeypatch.setattr(workflow, "evaluate_physical_refresh_balance_convergence", lambda *a, **k: convergence)
    monkeypatch.setattr(
        workflow, "publish_forward_physical_refresh_current",
        lambda *a, **k: (_ for _ in ()).throw(
            failure_factory()
        ),
    )
    sle_id = int(sle.id)
    with pytest.raises(expected_type, match=expected_message):
        workflow.run_physical_refresh(
            db_session, generation_key=physical.generation_key,
            target_cutoff=target_cutoff, client=object(), balance_snapshot={},
        )
    db_session.expire_all()
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id
    assert db_session.get(models.LedgerGeneration, physical.id).status == "rejected"
    assert db_session.get(models.StockLedgerEntry, sle_id) is None


def test_run_physical_refresh_true_noop_discards_lightweight_candidate(db_session, monkeypatch):
    parent, _ = _accepted_parent(db_session, generation_key="noop-parent")
    target_cutoff = parent.cutoff + timedelta(days=1)
    batch = models.PhysicalImportBatch(
        batch_key="noop-refresh-batch", status="completed", cutoff=target_cutoff,
        source_watermarks={}, completed_at=target_cutoff,
    )
    physical = models.LedgerGeneration(
        generation_key="noop-refresh-generation", status="building", cutoff=target_cutoff,
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": parent.id,
            "replay_from": "2026-07-01T00:00:00+00:00",
        }, capabilities={}, physical_import_batch=batch,
        algorithm_version="ledger-physical-refresh-generation/1",
        replay_version="ledger-physical-refresh-replay/1",
    )
    item = models.Item(item_code="NOOP-ITEM", item_name="No-op item")
    db_session.add_all([batch, physical, item])
    db_session.flush()
    db_session.add(models.StockBin(
        ledger_generation_id=parent.id, item_id=item.item_id,
        characteristic_ref="", organization_ref="", warehouse_ref1c="",
        on_hand=Decimal("4"), is_current=True,
    ))
    db_session.commit()
    before_bins = db_session.query(models.StockBin).count()
    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=physical.id, generation_key="noop-refresh-generation",
        physical_import_batch_id=batch.id, cutoff=target_cutoff,
        from_cutoff=parent.cutoff, created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=physical.id, from_exclusive=parent.cutoff,
        cutoff=target_cutoff, completed_through=target_cutoff, windows_completed=1,
        windows_resumed=0, recorders_pulled=0, movements_inserted=0,
        complete=True, physical_import_batch_id=batch.id,
    )
    convergence = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=physical.id, cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(), valid=True, content_hash="noop",
        compared=0, mismatched=0, matched=0, terminal_batch_id=batch.id, deltas=(),
    )
    monkeypatch.setattr(workflow, "fork_physical_refresh_generation", lambda *a, **k: fork_result)
    monkeypatch.setattr(workflow, "run_physical_recorder_audit", lambda *a, **k: object())
    monkeypatch.setattr(workflow, "run_historical_physical_import", lambda *a, **k: import_result)
    monkeypatch.setattr(
        workflow,
        "_ensure_terminal_physical_boundary",
        lambda *a, **k: pytest.fail("true no-op created a terminal boundary"),
    )
    monkeypatch.setattr(
        bootstrap,
        "_aggregate_sles_for_convergence",
        lambda *a, **k: pytest.fail("normal refresh scanned historical SLE prefix"),
    )
    result = workflow.run_physical_refresh(
        db_session, generation_key="noop-refresh-generation", target_cutoff=target_cutoff,
        client=object(),
        balance_snapshot={LedgerKey(item.item_id, "", "", ""): Decimal("4")},
    )
    assert result.published is False
    assert result.replayed_rows == 0
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id
    assert db_session.get(models.LedgerGeneration, physical.id).status == "rejected"
    assert db_session.query(models.StockBin).count() == before_bins
    # Decision §57: the tick read 1C up to the new cutoff and converged, so the
    # pointer it was computed from is verified that far even though nothing was
    # published.  Without this a weekend without postings put every reader on
    # 503 planning_truth_unavailable.
    target_utc = planning_truth._as_utc(target_cutoff)
    pointer = db_session.get(models.PlanningTruthState, 1)
    assert int(pointer.verified_generation_id) == int(parent.id)
    assert planning_truth._as_utc(pointer.verified_cutoff) == target_utc
    assert result.verified_cutoff == target_utc
    monkeypatch.setenv(planning_truth.TRUTH_MAX_AGE_SECONDS_ENV, str(24 * 3600))
    readiness = planning_truth.get_readiness(
        db_session, now=target_utc + timedelta(hours=1)
    )
    assert readiness.ready is True
    assert readiness.verified_cutoff == target_utc
    # The extension is bounded: it ages from the check, not forever.
    assert planning_truth.get_readiness(
        db_session, now=target_utc + timedelta(hours=25),
    ).truth_status == "stale"


def test_balance_adjustment_row_is_not_classified_as_noop(db_session, monkeypatch):
    parent, _ = _accepted_parent(db_session, generation_key="adjustment-parent")
    target_cutoff = parent.cutoff + timedelta(days=1)
    batch = models.PhysicalImportBatch(
        batch_key="adjustment-refresh-batch", status="completed", cutoff=target_cutoff,
        source_watermarks={}, completed_at=target_cutoff,
    )
    physical = models.LedgerGeneration(
        generation_key="adjustment-refresh-generation", status="building", cutoff=target_cutoff,
        source_watermarks={
            "parent_generation_id": parent.id,
            "replay_from": "2026-07-01T00:00:00+00:00",
        }, capabilities={}, physical_import_batch=batch,
        algorithm_version="ledger-physical-refresh-generation/1",
        replay_version="ledger-physical-refresh-replay/1",
    )
    item = models.Item(item_code="ADJUSTMENT-ITEM", item_name="Adjustment item")
    db_session.add_all([batch, physical, item])
    db_session.flush()
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=batch.id, source_content_hash="adjustment-sle",
        business_identity="adjustment-business", item_id=item.item_id,
        characteristic_ref="", organization_ref="", warehouse_ref1c="",
        qty=Decimal("1"), posting_at=target_cutoff,
        record_type="Receipt", movement_kind="cutoff_balance_adjustment",
        recorder_type="cutoff_balance_adjustment", recorder_ref="adjustment-ref",
        line_no="0", ingest_source="cutoff_balance_adjustment",
    ))
    db_session.commit()
    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=physical.id, generation_key=physical.generation_key,
        physical_import_batch_id=batch.id, cutoff=target_cutoff,
        from_cutoff=parent.cutoff, created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=physical.id, from_exclusive=parent.cutoff,
        cutoff=target_cutoff, completed_through=target_cutoff, windows_completed=1,
        windows_resumed=0, recorders_pulled=0, movements_inserted=0,
        complete=True, physical_import_batch_id=batch.id,
    )
    convergence = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=physical.id, cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(), valid=True, content_hash="adjustment",
        compared=0, mismatched=0, matched=0, terminal_batch_id=batch.id, deltas=(),
    )
    monkeypatch.setattr(workflow, "fork_physical_refresh_generation", lambda *a, **k: fork_result)
    monkeypatch.setattr(workflow, "run_physical_recorder_audit", lambda *a, **k: object())
    monkeypatch.setattr(workflow, "run_historical_physical_import", lambda *a, **k: import_result)
    monkeypatch.setattr(workflow, "evaluate_physical_refresh_balance_convergence", lambda *a, **k: convergence)
    def _publish(*args, **kwargs):
        candidate = db_session.get(models.LedgerGeneration, physical.id)
        candidate.status = "accepted"
        db_session.get(models.PlanningTruthState, 1).current_generation_id = physical.id
        return SimpleNamespace(
            input_delta_rows=1, replayed_rows=1,
            affected_scopes=(),
        )
    monkeypatch.setattr(workflow, "publish_forward_physical_refresh_current", _publish)
    result = workflow.run_physical_refresh(
        db_session, generation_key=physical.generation_key,
        target_cutoff=target_cutoff, client=object(), balance_snapshot={},
    )
    assert result.published is True
    assert result.input_delta_rows == 1
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == physical.id
    assert db_session.get(models.LedgerGeneration, physical.id).status == "accepted"


def _building_physical_generation(db_session, parent, parent_batch):
    generation = models.LedgerGeneration(
        generation_key="balance-snap-child",
        status="building",
        cutoff=parent.cutoff + timedelta(days=1),
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": parent.id,
        },
        physical_import_batch=parent_batch,
        algorithm_version="physical-refresh/test",
        replay_version="physical-refresh/test",
    )
    db_session.add(generation)
    db_session.flush()
    return generation


def _seed_sle(db_session, batch, generation, item, *, qty):
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=int(batch.id),
        source_content_hash=f"seed-{item.item_id}",
        item_id=int(item.item_id),
        characteristic_ref="",
        organization_ref="org-ref",
        warehouse_ref1c="WH-PHYSICAL-PLAN",
        qty=Decimal(str(qty)),
        qty_after=Decimal(str(qty)),
        posting_at=generation.cutoff,
        record_type="Receipt",
        movement_kind="test_seed",
        recorder_type="test_seed",
        recorder_ref=f"seed-{item.item_id}",
        line_no="0",
        ingest_source="test",
    ))
    db_session.flush()


def test_balance_snap_closes_backdated_residual_at_cutoff(db_session):
    parent, parent_batch = _accepted_parent(db_session, generation_key="balance-snap")
    generation = _building_physical_generation(db_session, parent, parent_batch)
    # One cell 1C moved behind the cutoff (ledger 1, 1C 2) plus two agreeing
    # cells so the mismatch is a small fraction of the compared set.
    drift = models.Item(item_code="SNAP-DRIFT", item_name="Drift", item_ref1c="drift")
    ok_a = models.Item(item_code="SNAP-OK-A", item_name="Ok A", item_ref1c="ok-a")
    ok_b = models.Item(item_code="SNAP-OK-B", item_name="Ok B", item_ref1c="ok-b")
    db_session.add_all([drift, ok_a, ok_b])
    db_session.flush()
    parent_stock_bin = models.StockBin(
        ledger_generation_id=parent.id,
        item_id=drift.item_id,
        characteristic_ref="",
        organization_ref="org-ref",
        warehouse_ref1c="WH-PHYSICAL-PLAN",
        on_hand=Decimal("1"),
        is_current=True,
    )
    db_session.add(parent_stock_bin)
    db_session.flush()
    _seed_sle(db_session, parent_batch, generation, drift, qty=1)
    _seed_sle(db_session, parent_batch, generation, ok_a, qty=5)
    _seed_sle(db_session, parent_batch, generation, ok_b, qty=5)
    db_session.commit()

    snapshot = {
        (int(drift.item_id), "", "org-ref", "WH-PHYSICAL-PLAN"): Decimal("2"),
        (int(ok_a.item_id), "", "org-ref", "WH-PHYSICAL-PLAN"): Decimal("5"),
        (int(ok_b.item_id), "", "org-ref", "WH-PHYSICAL-PLAN"): Decimal("5"),
    }

    before = bootstrap.evaluate_physical_refresh_balance_convergence(
        db_session, ledger_generation_id=generation.id, balance_snapshot=snapshot,
    )
    assert before.valid is False
    assert before.mismatched == 1

    snapped = workflow._snap_balance_at_cutoff(
        db_session, generation=generation, convergence=before,
    )
    assert snapped == 1

    # Exactly one synthetic cutoff adjustment, dated at the cutoff, for the
    # drifted cell, carrying the delta that closes it (1C 2 - ledger 1 = 1).
    adjustments = db_session.query(models.StockLedgerEntry).filter_by(
        recorder_type="cutoff_balance_adjustment",
    ).all()
    assert len(adjustments) == 1
    assert int(adjustments[0].item_id) == int(drift.item_id)
    assert Decimal(adjustments[0].qty) == Decimal("1")
    assert adjustments[0].posting_at == generation.cutoff
    snap_hash = db_session.get(
        models.PhysicalImportBatch, int(adjustments[0].ingest_batch_id)
    ).source_watermarks["content_hash"]
    assert adjustments[0].business_identity == business_identity_for_cutoff_balance_adjustment(
        adjustments[0].recorder_ref,
        "0",
        item_id=drift.item_id,
        characteristic_ref="",
        organization_ref="org-ref",
        warehouse_ref1c="WH-PHYSICAL-PLAN",
        snap_content_hash=snap_hash,
    )
    assert len(adjustments[0].business_identity) <= 256
    assert db_session.query(models.StockBin).filter(
        models.StockBin.ledger_generation_id == generation.id,
    ).count() == 0
    assert db_session.get(models.StockBin, parent_stock_bin.id).on_hand == Decimal("1")

    bounded_after = bootstrap.evaluate_physical_refresh_balance_convergence(
        db_session,
        ledger_generation_id=generation.id,
        balance_snapshot={
            (int(drift.item_id), "", "org-ref", "WH-PHYSICAL-PLAN"): Decimal("2"),
        },
        base_generation_id=parent.id,
        delta_rows=(adjustments[0],),
        new_rows=(adjustments[0],),
    )
    assert bounded_after.valid is True

    after = bootstrap.evaluate_physical_refresh_balance_convergence(
        db_session, ledger_generation_id=generation.id, balance_snapshot=snapshot,
    )
    assert after.valid is True
    assert after.mismatched == 0


def test_cutoff_snap_then_bounded_stock_publish_has_one_target_owner(
    db_session, monkeypatch,
):
    parent, parent_batch = _accepted_parent(db_session, generation_key="snap-publish")
    target_cutoff = parent.cutoff + timedelta(days=1)
    target_batch = models.PhysicalImportBatch(
        batch_key="snap-publish-target-batch", status="completed",
        source_complete=True, cutoff=target_cutoff,
        source_watermarks={}, completed_at=target_cutoff,
    )
    target = models.LedgerGeneration(
        generation_key="snap-publish-target", status="building", cutoff=target_cutoff,
        source_watermarks={"parent_generation_id": parent.id}, capabilities={},
        physical_import_batch=target_batch, algorithm_version="test",
        replay_version="test",
    )
    item = models.Item(item_code="SNAP-PUBLISH", item_name="Snap publish")
    parent_bin = models.StockBin(
        ledger_generation_id=parent.id, item_id=1, characteristic_ref="",
        organization_ref="org-ref", warehouse_ref1c="WH-PHYSICAL-PLAN",
        on_hand=Decimal("1"), is_current=True,
    )
    db_session.add_all([target_batch, target, item])
    db_session.flush()
    parent_bin.item_id = item.item_id
    db_session.add(parent_bin)
    db_session.commit()

    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=target.id, generation_key=target.generation_key,
        physical_import_batch_id=target_batch.id, cutoff=target_cutoff,
        from_cutoff=parent.cutoff, created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=target.id, from_exclusive=parent.cutoff,
        cutoff=target_cutoff, completed_through=target_cutoff, windows_completed=1,
        windows_resumed=0, recorders_pulled=0, movements_inserted=0,
        complete=True, physical_import_batch_id=target_batch.id,
    )
    mismatch = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=target.id, cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(), valid=False, content_hash="snap-publish-before",
        compared=2, mismatched=1, matched=1, terminal_batch_id=target_batch.id,
        deltas=(bootstrap.BalanceConvergenceDelta(
            item_id=item.item_id, organization_ref="org-ref",
            warehouse_ref1c="WH-PHYSICAL-PLAN", balance_qty="2", ledger_qty="1",
            delta_qty="1", matched=False,
        ),),
    )
    converged = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=target.id, cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(), valid=True, content_hash="snap-publish-after",
        compared=2, mismatched=0, matched=2, terminal_batch_id=target_batch.id,
        deltas=(),
    )
    convergence_results = iter((mismatch, converged))
    monkeypatch.setattr(workflow, "_acquire_lifecycle_lock", lambda db: True)
    monkeypatch.setattr(workflow, "fork_physical_refresh_generation", lambda *a, **k: fork_result)
    monkeypatch.setattr(workflow, "run_physical_recorder_audit", lambda *a, **k: object())
    monkeypatch.setattr(workflow, "run_historical_physical_import", lambda *a, **k: import_result)
    monkeypatch.setattr(workflow, "_repair_mismatched_recorders", lambda *a, **k: 0)
    monkeypatch.setattr(
        workflow, "evaluate_physical_refresh_balance_convergence",
        lambda *a, **k: next(convergence_results),
    )
    publisher_observations = []

    def _publish(db, **kwargs):
        candidate = db.get(models.LedgerGeneration, target.id)
        rows = tuple(kwargs["delta_manifest"]["rows"])
        publisher_observations.append(
            db.query(models.StockBin).filter(
                models.StockBin.ledger_generation_id == target.id,
            ).count()
        )
        adjustment = rows[0]
        stock_bin.apply_bounded_current_stock_bins(
            db,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            affected_physical_keys=(
                (adjustment.item_id, adjustment.characteristic_ref,
                 adjustment.organization_ref, adjustment.warehouse_ref1c),
            ),
            delta_manifest=stock_bin.BoundedPhysicalDeltaManifest(
                new_sle_ids=(int(adjustment.id),),
            ),
        )
        candidate.status = "accepted"
        db.get(models.PlanningTruthState, 1).current_generation_id = target.id
        return SimpleNamespace(
            input_delta_rows=1, replayed_rows=1,
            affected_scopes=(),
        )

    monkeypatch.setattr(workflow, "publish_forward_physical_refresh_current", _publish)
    result = workflow.run_physical_refresh(
        db_session, generation_key=target.generation_key,
        target_cutoff=target_cutoff, client=object(), balance_snapshot={},
    )
    assert result.published is True
    assert publisher_observations == [0]
    assert db_session.query(models.StockBin).filter(
        models.StockBin.item_id == item.item_id,
        models.StockBin.organization_ref == "org-ref",
        models.StockBin.warehouse_ref1c == "WH-PHYSICAL-PLAN",
    ).count() == 1
    assert db_session.query(models.StockBin).filter(
        models.StockBin.ledger_generation_id == target.id,
        models.StockBin.is_current.is_(True),
    ).count() == 1


def test_balance_snap_refuses_wholesale_divergence(db_session):
    parent, parent_batch = _accepted_parent(db_session, generation_key="snap-catastrophe")
    generation = _building_physical_generation(db_session, parent, parent_batch)
    db_session.commit()
    # 2 of 2 cells diverge (100% > 50%): a broken import, not backdated drift.
    deltas = tuple(
        bootstrap.BalanceConvergenceDelta(
            item_id=1000 + i,
            organization_ref="org-ref",
            warehouse_ref1c="WH-PHYSICAL-PLAN",
            balance_qty="2",
            ledger_qty="1",
            delta_qty="1",
            matched=False,
        )
        for i in range(2)
    )
    convergence = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff.isoformat(),
        checked_at=generation.cutoff.isoformat(),
        valid=False,
        content_hash="catastrophe",
        compared=2,
        matched=0,
        mismatched=2,
        terminal_batch_id=parent_batch.id,
        deltas=deltas,
    )
    with pytest.raises(workflow.PhysicalRefreshOrchestratorError, match="balance snap refused"):
        workflow._snap_balance_at_cutoff(
            db_session, generation=generation, convergence=convergence,
        )


def _seed_cutoff_snap(db_session, batch, item, *, qty, posting_at):
    """One synthetic cutoff adjustment cell, shaped like the snap writer's."""
    row = models.StockLedgerEntry(
        ingest_batch_id=int(batch.id),
        source_content_hash=f"snap-{item.item_id}-{qty}",
        business_identity=business_identity_for_cutoff_balance_adjustment(
            f"snap-{item.item_id}-{qty}", "0",
            item_id=int(item.item_id), characteristic_ref="",
            organization_ref="org-ref", warehouse_ref1c="WH-PHYSICAL-PLAN",
            snap_content_hash=f"hash-{item.item_id}-{qty}",
        ),
        item_id=int(item.item_id),
        characteristic_ref="",
        organization_ref="org-ref",
        warehouse_ref1c="WH-PHYSICAL-PLAN",
        qty=Decimal(str(qty)),
        posting_at=posting_at,
        record_type="Receipt" if Decimal(str(qty)) > 0 else "Expense",
        movement_kind="cutoff_balance_adjustment",
        recorder_type="cutoff_balance_adjustment",
        recorder_ref=f"snap-{item.item_id}-{qty}",
        line_no="0",
        ingest_source="cutoff_balance_adjustment",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _seed_document_fact(db_session, batch, item, *, qty, posting_at, ref="doc-1"):
    row = models.StockLedgerEntry(
        ingest_batch_id=int(batch.id),
        source_content_hash=f"{ref}-{item.item_id}",
        item_id=int(item.item_id),
        characteristic_ref="",
        organization_ref="org-ref",
        warehouse_ref1c="WH-PHYSICAL-PLAN",
        qty=Decimal(str(qty)),
        posting_at=posting_at,
        record_type="Receipt" if Decimal(str(qty)) > 0 else "Expense",
        movement_kind="receipt",
        recorder_type="Document_ПриходнаяНакладная",
        recorder_ref=ref,
        line_no="1",
        ingest_source="document_pull",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _active_cell_total(db_session, item):
    return sum(
        (Decimal(row.qty) for row in db_session.query(models.StockLedgerEntry).filter(
            models.StockLedgerEntry.item_id == int(item.item_id),
            models.StockLedgerEntry.active.is_(True),
        ).all()),
        Decimal("0"),
    )


def test_cutoff_snap_is_retired_and_its_remainder_reissued(db_session):
    """The document the snap stood in for replaces it without moving balance."""
    parent, parent_batch = _accepted_parent(db_session, generation_key="snap-retire")
    item = models.Item(item_code="RET-1", item_name="Ret", item_ref1c="ret-1")
    db_session.add(item)
    db_session.flush()
    posted = _moscow_naive(parent.cutoff) - timedelta(days=8)
    snap = _seed_cutoff_snap(
        db_session, parent_batch, item, qty=10, posting_at=_moscow_naive(parent.cutoff),
    )
    db_session.flush()
    # The truth before the document arrived: the snap alone stood for the cell.
    before = _active_cell_total(db_session, item)
    fact = _seed_document_fact(
        db_session, parent_batch, item, qty=7, posting_at=posted,
    )
    db_session.commit()

    retirement = workflow.retire_cutoff_snaps_absorbing_facts(
        db_session,
        fact_rows=[fact],
        previous_import_batch_id=int(parent_batch.id),
        reason="test",
    )
    db_session.flush()

    assert retirement is not None
    assert retirement.retired_rows == 1
    assert retirement.reissued_rows == 1
    assert Decimal(retirement.absorbed_qty) == Decimal("7")
    assert retirement.unabsorbed == ()
    assert db_session.get(models.StockLedgerEntry, int(snap.id)).active is False
    edge = db_session.query(models.StockLedgerFactSupersession).filter_by(
        old_sle_id=int(snap.id)
    ).one()
    assert edge.new_sle_id is not None
    replacement = db_session.get(models.StockLedgerEntry, int(edge.new_sle_id))
    assert Decimal(replacement.qty) == Decimal("3")
    assert replacement.posting_at == snap.posting_at
    assert replacement.movement_kind == "cutoff_balance_adjustment"
    # The operation is balance-neutral: the snap it retired is exactly the
    # imported quantity plus the remainder it re-issued.
    assert _active_cell_total(db_session, item) == before


def test_fully_explained_cutoff_snap_is_tombstoned(db_session):
    parent, parent_batch = _accepted_parent(db_session, generation_key="snap-tombstone")
    item = models.Item(item_code="RET-2", item_name="Ret2", item_ref1c="ret-2")
    db_session.add(item)
    db_session.flush()
    snap = _seed_cutoff_snap(
        db_session, parent_batch, item, qty=29600,
        posting_at=_moscow_naive(parent.cutoff),
    )
    db_session.flush()
    before = _active_cell_total(db_session, item)
    fact = _seed_document_fact(
        db_session, parent_batch, item, qty=29600,
        posting_at=_moscow_naive(parent.cutoff) - timedelta(days=8),
    )
    db_session.commit()

    retirement = workflow.retire_cutoff_snaps_absorbing_facts(
        db_session, fact_rows=[fact],
        previous_import_batch_id=int(parent_batch.id), reason="test",
    )
    db_session.flush()

    assert retirement.retired_rows == 1
    assert retirement.reissued_rows == 0
    edge = db_session.query(models.StockLedgerFactSupersession).filter_by(
        old_sle_id=int(snap.id)
    ).one()
    assert edge.new_sle_id is None
    assert _active_cell_total(db_session, item) == before


def test_historical_audit_retires_only_new_absorbed_facts_once(db_session):
    parent, parent_batch = _accepted_parent(
        db_session, generation_key="audit-snap-retire"
    )
    fresh = models.Item(item_code="AUDIT-NEW", item_name="Audit new")
    revised = models.Item(item_code="AUDIT-REV", item_name="Audit revision")
    db_session.add_all([fresh, revised])
    db_session.flush()
    parent_posting = _moscow_naive(parent.cutoff)
    fresh_snap = _seed_cutoff_snap(
        db_session, parent_batch, fresh, qty=10, posting_at=parent_posting,
    )
    revised_snap = _seed_cutoff_snap(
        db_session, parent_batch, revised, qty=5, posting_at=parent_posting,
    )
    old_revision = _seed_document_fact(
        db_session, parent_batch, revised, qty=2,
        posting_at=parent_posting - timedelta(days=1), ref="known-revision",
    )
    generation = _building_physical_generation(db_session, parent, parent_batch)
    audit_batch = models.PhysicalImportBatch(
        batch_key="audit-snap-retire-import", status="completed",
        cutoff=generation.cutoff, source_watermarks={},
        completed_at=generation.cutoff,
    )
    db_session.add(audit_batch)
    db_session.flush()
    fresh_fact = _seed_document_fact(
        db_session, audit_batch, fresh, qty=7,
        posting_at=parent_posting - timedelta(days=2), ref="new-backdate",
    )
    vanished_fact = _seed_document_fact(
        db_session, audit_batch, fresh, qty=2,
        posting_at=parent_posting - timedelta(days=2), ref="vanished-in-audit",
    )
    vanished_fact.active = False
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=int(vanished_fact.id), new_sle_id=None,
        import_batch_id=int(audit_batch.id),
    ))
    old_revision.active = False
    db_session.flush()
    new_revision = _seed_document_fact(
        db_session, audit_batch, revised, qty=2,
        posting_at=parent_posting - timedelta(days=1), ref="known-revision",
    )
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=int(old_revision.id), new_sle_id=int(new_revision.id),
        import_batch_id=int(audit_batch.id),
    ))
    generation.physical_import_batch_id = int(audit_batch.id)
    db_session.commit()

    parent_cell_balance = Decimal("10")
    audit = SimpleNamespace(terminal_physical_import_batch_id=audit_batch.id)
    result = workflow._retire_audit_absorbed_cutoff_snaps(
        db_session, parent=parent, generation=generation, recorder_audit=audit,
    )
    db_session.commit()

    assert result is not None
    assert result.retired_rows == 1
    assert result.reissued_rows == 1
    assert Decimal(result.absorbed_qty) == Decimal("7")
    assert result.unabsorbed == ()
    assert _active_cell_total(db_session, fresh) == parent_cell_balance
    assert db_session.get(models.StockLedgerEntry, fresh_snap.id).active is False
    assert db_session.get(models.StockLedgerEntry, revised_snap.id).active is True
    assert generation.physical_import_batch_id > audit_batch.id
    assert db_session.get(models.StockLedgerEntry, fresh_fact.id).active is True
    # The earlier accepted prefix still sees its original synthetic row.
    assert fresh_snap.id in {
        row.id for row in workflow.visible_sle_query(
            db_session, physical_import_batch_id=parent_batch.id,
            cutoff=parent_posting,
        ).all()
    }
    supersessions = db_session.query(models.StockLedgerFactSupersession).count()
    assert workflow._retire_audit_absorbed_cutoff_snaps(
        db_session, parent=parent, generation=generation, recorder_audit=audit,
    ) is None
    assert db_session.query(models.StockLedgerFactSupersession).count() == supersessions


def test_cutoff_snap_older_than_the_fact_is_not_retired(db_session):
    """A snap written before the document was posted absorbed something else."""
    parent, parent_batch = _accepted_parent(db_session, generation_key="snap-older")
    item = models.Item(item_code="RET-3", item_name="Ret3", item_ref1c="ret-3")
    db_session.add(item)
    db_session.flush()
    snap = _seed_cutoff_snap(
        db_session, parent_batch, item, qty=10,
        posting_at=_moscow_naive(parent.cutoff) - timedelta(days=20),
    )
    fact = _seed_document_fact(
        db_session, parent_batch, item, qty=7,
        posting_at=_moscow_naive(parent.cutoff) - timedelta(days=8),
    )
    db_session.commit()

    retirement = workflow.retire_cutoff_snaps_absorbing_facts(
        db_session, fact_rows=[fact],
        previous_import_batch_id=int(parent_batch.id), reason="test",
    )

    assert retirement.retired_rows == 0
    assert len(retirement.unabsorbed) == 1
    assert retirement.unabsorbed[0][:3] == (
        int(item.item_id), "org-ref", "WH-PHYSICAL-PLAN",
    )
    assert Decimal(retirement.unabsorbed[0][3]) == Decimal("7")
    assert db_session.get(models.StockLedgerEntry, int(snap.id)).active is True


def test_opposite_signed_cutoff_snap_is_left_alone(db_session):
    parent, parent_batch = _accepted_parent(db_session, generation_key="snap-sign")
    item = models.Item(item_code="RET-4", item_name="Ret4", item_ref1c="ret-4")
    db_session.add(item)
    db_session.flush()
    snap = _seed_cutoff_snap(
        db_session, parent_batch, item, qty=-10,
        posting_at=_moscow_naive(parent.cutoff),
    )
    fact = _seed_document_fact(
        db_session, parent_batch, item, qty=7,
        posting_at=_moscow_naive(parent.cutoff) - timedelta(days=1),
    )
    db_session.commit()

    retirement = workflow.retire_cutoff_snaps_absorbing_facts(
        db_session, fact_rows=[fact],
        previous_import_batch_id=int(parent_batch.id), reason="test",
    )

    assert retirement.retired_rows == 0
    assert db_session.get(models.StockLedgerEntry, int(snap.id)).active is True


@pytest.mark.parametrize("repair_fails", [False, True])
def test_snap_never_runs_before_the_targeted_recorder_repair(
    db_session, monkeypatch, repair_fails,
):
    """A residual is a missing document until the register says otherwise."""
    parent, _ = _accepted_parent(db_session, generation_key="repair-first")
    target_cutoff = parent.cutoff + timedelta(days=1)
    order = []

    forked_batch = models.PhysicalImportBatch(
        batch_key="repair-first-physical", status="completed", cutoff=target_cutoff,
        source_watermarks={}, completed_at=target_cutoff,
    )
    physical = models.LedgerGeneration(
        generation_key="repair-first-fork", status="building", cutoff=target_cutoff,
        source_watermarks={
            "replay_from": "2026-07-01T00:00:00+00:00",
            "parent_generation_id": parent.id,
        },
        physical_import_batch=forked_batch,
        algorithm_version="ledger-physical-refresh-generation/1",
        replay_version="ledger-physical-refresh-replay/1",
    )
    db_session.add_all([forked_batch, physical])
    db_session.flush()
    db_session.commit()
    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=physical.id, generation_key="repair-first-fork",
        physical_import_batch_id=forked_batch.id, cutoff=target_cutoff,
        from_cutoff=parent.cutoff, created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=physical.id, from_exclusive=parent.cutoff,
        cutoff=target_cutoff, completed_through=target_cutoff, windows_completed=1,
        windows_resumed=0, recorders_pulled=0, movements_inserted=0, complete=True,
        physical_import_batch_id=forked_batch.id,
    )
    mismatch = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=physical.id, cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(), valid=False, content_hash="mismatch",
        compared=4, mismatched=1, matched=3, terminal_batch_id=forked_batch.id,
        deltas=(),
    )

    monkeypatch.setattr(workflow, "fork_physical_refresh_generation", lambda *a, **k: fork_result)
    monkeypatch.setattr(workflow, "run_physical_recorder_audit", lambda *a, **k: object())
    monkeypatch.setattr(workflow, "run_historical_physical_import", lambda *a, **k: import_result)
    monkeypatch.setattr(
        workflow, "_retire_audit_absorbed_cutoff_snaps",
        lambda *a, **k: order.append("retire"),
    )
    monkeypatch.setattr(
        workflow, "evaluate_physical_refresh_balance_convergence",
        lambda *a, **k: mismatch,
    )
    def _repair(*_args, **_kwargs):
        order.append("repair")
        if repair_fails:
            raise RuntimeError("simulated targeted-pull failure")
        return 0

    monkeypatch.setattr(workflow, "_repair_mismatched_recorders", _repair)
    monkeypatch.setattr(
        workflow, "_snap_balance_at_cutoff",
        lambda *a, **k: order.append("snap") or 0,
    )

    with pytest.raises(workflow.PhysicalRefreshOrchestratorError):
        workflow.run_physical_refresh(
            db_session, generation_key="repair-first", target_cutoff=target_cutoff,
            client=object(), balance_snapshot={},
        )

    assert order == (
        ["retire", "repair", "retire", "snap"]
        if repair_fails else ["retire", "repair", "snap"]
    )


def test_backdated_and_superseded_delta_is_replayed_not_rejected(db_session, monkeypatch):
    """A correction no longer stops the hourly refresh.

    92% of the production stand's newly imported rows are posted behind the
    accepted cutoff, so rejecting every such candidate froze physical truth.
    The candidate must instead travel with an explicit bounded replay boundary.
    """
    parent, parent_batch = _accepted_parent(db_session, generation_key="bounded-backdate")
    target_cutoff = parent.cutoff + timedelta(days=1)
    forked_batch = models.PhysicalImportBatch(
        batch_key="bounded-backdate-target", status="completed", source_complete=True,
        cutoff=target_cutoff, source_watermarks={}, completed_at=target_cutoff,
    )
    physical = models.LedgerGeneration(
        generation_key="bounded-backdate-fork", status="building", cutoff=target_cutoff,
        source_watermarks={"parent_generation_id": parent.id}, capabilities={},
        physical_import_batch=forked_batch, algorithm_version="test",
        replay_version="test",
    )
    item = models.Item(item_code="BOUNDED-BACKDATE-ORCH", item_name="Backdate")
    db_session.add_all([forked_batch, physical, item])
    db_session.flush()
    superseded_at = _moscow_naive(parent.cutoff - timedelta(days=2))
    old = models.StockLedgerEntry(
        ingest_batch_id=parent_batch.id, source_content_hash="orch-old",
        business_identity="orch-superseded", item_id=item.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("1"), posting_at=superseded_at, record_type="Receipt",
        movement_kind="assembly_in", recorder_type="Production",
        recorder_ref="orch-old-rec", line_no="1", ingest_source="pull",
    )
    db_session.add(old)
    db_session.flush()
    old.active = False
    db_session.flush()
    replacement = models.StockLedgerEntry(
        ingest_batch_id=forked_batch.id, source_content_hash="orch-new",
        business_identity="orch-superseded", item_id=item.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("2"), posting_at=superseded_at, record_type="Receipt",
        movement_kind="assembly_in", recorder_type="Production",
        recorder_ref="orch-old-rec", line_no="1", ingest_source="pull",
    )
    forward = models.StockLedgerEntry(
        ingest_batch_id=forked_batch.id, source_content_hash="orch-forward",
        business_identity="orch-forward", item_id=item.item_id,
        characteristic_ref="", organization_ref="org", warehouse_ref1c="wh",
        qty=Decimal("4"), posting_at=_moscow_naive(parent.cutoff + timedelta(hours=6)),
        record_type="Receipt", movement_kind="assembly_in",
        recorder_type="Production", recorder_ref="orch-fwd-rec", line_no="1",
        ingest_source="pull",
    )
    db_session.add_all([replacement, forward])
    db_session.flush()
    db_session.add(models.StockLedgerFactSupersession(
        old_sle_id=old.id, new_sle_id=replacement.id, import_batch_id=forked_batch.id,
    ))
    db_session.commit()

    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=physical.id, generation_key=physical.generation_key,
        physical_import_batch_id=forked_batch.id, cutoff=target_cutoff,
        from_cutoff=parent.cutoff, created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=physical.id, from_exclusive=parent.cutoff,
        cutoff=target_cutoff, completed_through=target_cutoff, windows_completed=1,
        windows_resumed=0, recorders_pulled=0, movements_inserted=2, complete=True,
        physical_import_batch_id=forked_batch.id,
    )
    balance_result = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=physical.id, cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(), valid=True, content_hash="hash",
        compared=0, mismatched=0, matched=0, terminal_batch_id=forked_batch.id,
        deltas=(),
    )
    monkeypatch.setattr(workflow, "_acquire_lifecycle_lock", lambda db: True)
    monkeypatch.setattr(workflow, "fork_physical_refresh_generation", lambda *a, **k: fork_result)
    monkeypatch.setattr(workflow, "run_physical_recorder_audit", lambda *a, **k: object())
    monkeypatch.setattr(workflow, "run_historical_physical_import", lambda *a, **k: import_result)
    monkeypatch.setattr(
        workflow, "evaluate_physical_refresh_balance_convergence",
        lambda *a, **k: balance_result,
    )
    observed = {}

    def _publish(db, **kwargs):
        observed.update(kwargs["delta_manifest"])
        candidate = db.get(models.LedgerGeneration, physical.id)
        candidate.status = "accepted"
        db.get(models.PlanningTruthState, 1).current_generation_id = physical.id
        return SimpleNamespace(
            input_delta_rows=2, replayed_rows=3, affected_scopes=(), phase_timings=(),
        )

    monkeypatch.setattr(workflow, "publish_forward_physical_refresh_current", _publish)

    result = workflow.run_physical_refresh(
        db_session, generation_key=physical.generation_key,
        target_cutoff=target_cutoff, client=object(), balance_snapshot={},
    )

    assert result.published is True
    assert result.published_generation_id == physical.id
    # Only the target-window rows enter the current writers; the superseded
    # accepted fact travels separately as replay basis.
    assert {int(row.id) for row in observed["rows"]} == {
        int(replacement.id), int(forward.id),
    }
    assert [int(row.id) for row in observed["basis_rows"]] == [int(old.id)]
    assert len(observed["supersessions"]) == 1
    assert workflow._naive(observed["backdate_from"]) == workflow._naive(superseded_at)
    watermark = db_session.get(
        models.LedgerGeneration, physical.id
    ).source_watermarks["physical_refresh_delta"]
    assert watermark["backdated"] is True
    assert watermark["backdate_from"] == workflow._naive(superseded_at).isoformat()

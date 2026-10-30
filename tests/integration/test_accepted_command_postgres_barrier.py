from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import os
from threading import Barrier, Thread
from time import monotonic

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.database import Base
from app.services.accepted_command_mutation import (
    accepted_mutation_tail,
    consume_locked_publication_epoch,
    lock_publication_epoch,
)
from app.services.planning_truth import move_pointer_to_generation
from app.services.planning_truth import require_accepted_truth
from app.services.item_ledger.current_execution import (
    load_current_execution_rows,
    publish_current_production_control_from_payload,
    require_current_execution_scope,
)
from app.services.production_control_journal_projection import _publish_local_payloads
from app.services.production_control_common import DONE_STATE_KEY
from app.services.production_material_custody_events import (
    append_material_issue_custody_event,
    build_transfer_custody_event_specs_for_recorder,
    materialize_candidate_custody_event_specs,
)
from app.services.item_ledger.physical_refresh_provenance import (
    apply_bounded_current_material_custody_events,
)
from app.services.production_material_custody_projection import (
    build_terminal_custody_event_specs,
    fold_candidate_custody_event_specs,
)
from app.services.item_ledger.physical_visibility import visible_sle_query


PG_URL = os.getenv("PRODPLAN_BARRIER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="isolated PostgreSQL URL required")


def test_command_commits_during_build_and_final_pointer_cas_consumes_epoch():
    engine = create_engine(PG_URL, pool_size=4, max_overflow=0)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    cutoff = datetime(2026, 10, 30, tzinfo=timezone.utc)
    try:
        with Session() as seed:
            batch = models.PhysicalImportBatch(
                batch_key="pg-barrier", status="completed", source_complete=True,
                cutoff=cutoff, completed_at=cutoff, source_watermarks={},
            )
            parent = models.LedgerGeneration(
                generation_key="pg-parent", status="accepted", cutoff=cutoff,
                accepted_at=cutoff, physical_import_batch=batch,
                source_watermarks={}, capabilities={}, algorithm_version="test",
            )
            seed.add_all((batch, parent))
            seed.flush()
            target = models.LedgerGeneration(
                generation_key="pg-target", status="building", cutoff=cutoff,
                physical_import_batch_id=int(batch.id),
                source_watermarks={
                    "parent_generation_id": int(parent.id),
                    "generation_kind": "obligation_refresh",
                    "accepted_production_mutation_epoch": 0,
                },
                capabilities={}, algorithm_version="test",
            )
            seed.add(target)
            seed.flush()
            seed.add(models.PlanningTruthState(
                id=1, current_generation_id=int(parent.id),
            ))
            seed.add(models.AcceptedProductionMutationState(id=1))
            publish_current_production_control_from_payload(
                seed, int(parent.id), {
                    "meta": {},
                    "rows": [{
                        "current_identity": "production:product:9",
                        "product_id": 9, "order_id": 90, "item_id": 900,
                        "quantity": 1, "produced_qty": 0,
                        "remaining_qty": 1, "comment": "before",
                    }],
                },
            )
            seed.commit()
            parent_id, target_id = int(parent.id), int(target.id)

        barrier = Barrier(2)
        timings: dict[str, float] = {}
        errors: list[BaseException] = []

        def building_publisher() -> None:
            try:
                with Session() as db:
                    target = db.get(models.LedgerGeneration, target_id)
                    assert target.status == "building"
                    # Heavy BUILDING work has no pointer/epoch/current-row lock.
                    barrier.wait(timeout=10)
                    barrier.wait(timeout=10)
                    tail = accepted_mutation_tail(db, after_epoch=0)
                    assert tail.through_epoch == 1
                    marks = dict(target.source_watermarks or {})
                    marks["accepted_production_mutation_epoch"] = tail.through_epoch
                    target.source_watermarks = marks
                    target.status = "accepted"
                    target.accepted_at = cutoff
                    pointer, state, epoch = lock_publication_epoch(
                        db, generation=target, expected_parent_id=parent_id,
                    )
                    move_pointer_to_generation(db, pointer, target)
                    consume_locked_publication_epoch(
                        db, state=state, through_epoch=epoch,
                    )
                    db.commit()
            except BaseException as exc:  # pragma: no cover - thread handoff
                errors.append(exc)

        thread = Thread(target=building_publisher, daemon=True)
        thread.start()
        barrier.wait(timeout=10)
        started = monotonic()
        with Session() as command:
            truth = require_accepted_truth(command, "pg.command")
            manifest = require_current_execution_scope(
                command, entity_kind="production_control_journal",
                scope_key="production:all-live-orders",
            )
            records = load_current_execution_rows(
                command, entity_kind="production_control_journal",
                scope_key="production:all-live-orders",
            )
            payloads = {
                str(row.business_identity): {
                    **dict(row.payload), "comment": "during-building",
                    "current_identity": str(row.business_identity),
                }
                for row in records
            }
            _publish_local_payloads(command, truth, manifest, payloads)
            command.commit()
        timings["command"] = monotonic() - started
        barrier.wait(timeout=10)
        thread.join(timeout=10)
        assert not thread.is_alive()
        if errors:
            raise errors[0]
        assert timings["command"] < 2.0
        with Session() as verify:
            pointer = verify.get(models.PlanningTruthState, 1)
            state = verify.get(models.AcceptedProductionMutationState, 1)
            assert int(pointer.current_generation_id) == target_id
            assert state.last_issued_epoch == state.last_consumed_epoch == 1
            assert verify.query(models.AcceptedProductionMutation).count() == 0
            row = verify.query(models.CurrentExecutionRow).filter_by(
                entity_kind="production_control_journal",
                business_identity="production:product:9",
            ).one()
            assert row.payload["comment"] == "during-building"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize("close_during_build", [False, True])
def test_same_issue_reserve_commits_while_physical_specs_have_no_ids(
    close_during_build,
):
    engine = create_engine(PG_URL, pool_size=4, max_overflow=0)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    parent_cutoff = datetime(2026, 10, 30, 8, tzinfo=timezone.utc)
    target_cutoff = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    recorder_ref = "00000000-0000-0000-0000-000000000901"
    try:
        with Session() as seed:
            parent_batch = models.PhysicalImportBatch(
                batch_key="custody-parent", status="completed", source_complete=True,
                cutoff=parent_cutoff, completed_at=parent_cutoff, source_watermarks={},
            )
            target_batch = models.PhysicalImportBatch(
                batch_key="custody-target", status="completed", source_complete=True,
                cutoff=target_cutoff, completed_at=target_cutoff, source_watermarks={},
            )
            parent = models.LedgerGeneration(
                generation_key="custody-parent", status="accepted", cutoff=parent_cutoff,
                accepted_at=parent_cutoff, physical_import_batch=parent_batch,
                source_watermarks={}, capabilities={}, algorithm_version="test",
            )
            seed.add_all((parent_batch, target_batch, parent))
            seed.flush()
            target = models.LedgerGeneration(
                generation_key="custody-target", status="building", cutoff=target_cutoff,
                physical_import_batch_id=int(target_batch.id),
                source_watermarks={
                    "parent_generation_id": int(parent.id),
                    "generation_kind": "physical_refresh",
                    "accepted_production_mutation_epoch": 0,
                }, capabilities={}, algorithm_version="test",
            )
            item = models.Item(item_code="CUSTODY", item_name="Custody")
            order = models.ProductionOrder(order_number="CUSTODY", order_date=parent_cutoff)
            seed.add_all((target, item, order))
            seed.flush()
            product = models.ProductionProduct(
                order_id=int(order.order_id), item_id=int(item.item_id),
                quantity=1, produced_qty=0, remaining_qty=1,
            )
            seed.add(product)
            seed.flush()
            issue = models.ProductionMaterialIssue(
                document_number="MI-CUSTODY", product_id=int(product.product_id),
                order_id=int(order.order_id), direction="issue", status="exported",
                source_warehouse_ref1c="SRC", warehouse_ref1c="SHOP",
            )
            seed.add(issue)
            seed.flush()
            line = models.ProductionMaterialIssueLine(
                issue_id=int(issue.issue_id), component_item_id=int(item.item_id),
                required_qty=3, issued_qty=0, line_status="planned",
            )
            seed.add(line)
            seed.flush()
            seed.add(models.SyncLink(
                source_system="PRODPLAN", source_doctype="material_issue",
                source_id=int(issue.issue_id), target_system="1C",
                target_entity="Document_ПеремещениеЗапасов",
                target_ref_key=recorder_ref, status="success",
            ))
            for line_no, movement, warehouse, qty in (
                ("1", "transfer_out", "SRC", -3),
                ("2", "transfer_in", "SHOP", 3),
            ):
                seed.add(models.StockLedgerEntry(
                    ingest_batch_id=int(target_batch.id),
                    source_content_hash=("a" if line_no == "1" else "b") * 64,
                    business_identity=f"custody:{line_no}", item_id=int(item.item_id),
                    characteristic_ref="", organization_ref="", warehouse_ref1c=warehouse,
                    qty=qty, posting_at=target_cutoff, record_type=("Expense" if qty < 0 else "Receipt"),
                    movement_kind=movement, recorder_type="Document_ПеремещениеЗапасов",
                    recorder_ref=recorder_ref, line_no=line_no, ingest_source="document_pull",
                    active=True,
                ))
            seed.add(models.PlanningTruthState(id=1, current_generation_id=int(parent.id)))
            seed.add(models.AcceptedProductionMutationState(id=1))
            seed.add(models.ProductionMaterialCustodyProjectionManifest(
                ledger_generation_id=int(parent.id), baseline_generation_id=int(parent.id),
                cutoff=parent_cutoff, status="complete", source_event_high_watermark_id=0,
            ))
            seed.commit()
            ids = (int(parent.id), int(target.id), int(issue.issue_id), int(line.line_id))

        parent_id, target_id, issue_id, line_id = ids
        barrier = Barrier(2)
        errors: list[BaseException] = []
        timings: dict[str, float] = {}

        def publisher() -> None:
            try:
                with Session() as db:
                    target = db.get(models.LedgerGeneration, target_id)
                    rows = visible_sle_query(
                        db, physical_import_batch_id=int(target.physical_import_batch_id),
                        cutoff=target.cutoff,
                    ).filter(models.StockLedgerEntry.recorder_ref == recorder_ref).all()
                    first = build_transfer_custody_event_specs_for_recorder(
                        db, recorder_type="Document_ПеремещениеЗапасов",
                        recorder_ref=recorder_ref, stock_ledger_entries=rows,
                    )
                    assert len(first) == 3
                    assert db.query(models.ProductionMaterialCustodyEvent).count() == 0
                    barrier.wait(timeout=10)
                    barrier.wait(timeout=10)
                    db.expire_all()
                    target = db.get(models.LedgerGeneration, target_id)
                    rows = visible_sle_query(
                        db, physical_import_batch_id=int(target.physical_import_batch_id),
                        cutoff=target.cutoff,
                    ).filter(models.StockLedgerEntry.recorder_ref == recorder_ref).all()
                    regenerated = build_transfer_custody_event_specs_for_recorder(
                        db, recorder_type="Document_ПеремещениеЗапасов",
                        recorder_ref=recorder_ref, stock_ledger_entries=rows,
                    )
                    assert len(regenerated) == 2
                    target.source_watermarks = {
                        **dict(target.source_watermarks or {}),
                        "accepted_production_mutation_epoch": 1,
                    }
                    lock_publication_epoch(
                        db, generation=target, expected_parent_id=parent_id,
                    )
                    manifest = db.get(
                        models.ProductionMaterialCustodyProjectionManifest,
                        parent_id,
                    )
                    baseline_rows = {
                        (
                            int(row.product_id), int(row.component_item_id),
                            str(row.location_kind), str(row.warehouse_ref1c or ""),
                        ): float(row.reserved_qty)
                        for row in db.query(models.ProductionMaterialCustodyProjection)
                        .filter_by(is_current=True).all()
                    }
                    simulated = fold_candidate_custody_event_specs(
                        db, generation=target,
                        baseline_generation_id=parent_id,
                        baseline_high_watermark_id=int(manifest.source_event_high_watermark_id or 0),
                        baseline_cutoff=target_cutoff,
                        baseline_rows=baseline_rows,
                        candidate_events=regenerated,
                    )
                    terminal = tuple(
                        replace(
                            spec,
                            virtual_ordinal=len(regenerated) + ordinal,
                        )
                        for ordinal, spec in enumerate(
                            build_terminal_custody_event_specs(
                                db, generation=target, cells=simulated,
                            ), start=1,
                        )
                    )
                    if terminal:
                        simulated = fold_candidate_custody_event_specs(
                            db, generation=target,
                            baseline_generation_id=parent_id,
                            baseline_high_watermark_id=int(manifest.source_event_high_watermark_id or 0),
                            baseline_cutoff=target_cutoff,
                            baseline_rows=baseline_rows,
                            candidate_events=tuple(regenerated) + tuple(terminal),
                        )
                    materialize_candidate_custody_event_specs(
                        db, specs=tuple(regenerated) + tuple(terminal),
                        invalidate_current=False,
                        visibility_generation_id=target_id,
                    )
                    apply_bounded_current_material_custody_events(
                        db, parent_generation_id=parent_id,
                        target_generation_id=target_id,
                        source_sle_ids=tuple(
                            int(row.id) for row in rows
                        ),
                    )
                    actual = {
                        (
                            int(row.product_id), int(row.component_item_id),
                            str(row.location_kind), str(row.warehouse_ref1c or ""),
                        ): float(row.reserved_qty)
                        for row in db.query(models.ProductionMaterialCustodyProjection)
                        .filter_by(is_current=True).all()
                    }
                    assert actual == simulated
                    db.commit()
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        thread = Thread(target=publisher, daemon=True)
        thread.start()
        barrier.wait(timeout=10)
        started = monotonic()
        with Session() as command:
            issue = command.get(models.ProductionMaterialIssue, issue_id)
            line = command.get(models.ProductionMaterialIssueLine, line_id)
            assert append_material_issue_custody_event(
                command, issue=issue, line=line, delta_qty=3,
                source_kind="issue_created", location_kind="transit",
                warehouse_ref1c="SRC", source_ref1c="SRC",
                source_ref2c=recorder_ref,
                effective_at=datetime(2026, 10, 30, 9, tzinfo=timezone.utc),
            )
            if close_during_build:
                order = command.get(models.ProductionOrder, int(issue.order_id))
                order.order_state_key = DONE_STATE_KEY
                order.updated_at = datetime(2026, 10, 30, 10, tzinfo=timezone.utc)
            command.commit()
        timings["reserve"] = monotonic() - started
        barrier.wait(timeout=10)
        thread.join(timeout=10)
        assert not thread.is_alive()
        if errors:
            raise errors[0]
        assert timings["reserve"] < 2.0
        with Session() as verify:
            events = verify.query(models.ProductionMaterialCustodyEvent).order_by(
                models.ProductionMaterialCustodyEvent.id
            ).all()
            expected_kinds = [
                "issue_created", "transfer_posted", "transfer_posted",
            ]
            if close_during_build:
                expected_kinds.append("terminal_release")
            assert [event.source_kind for event in events] == expected_kinds
            manifest = verify.get(
                models.ProductionMaterialCustodyProjectionManifest, parent_id,
            )
            assert int(manifest.source_event_high_watermark_id) == int(events[-1].id)
            cells = {
                (row.location_kind, row.warehouse_ref1c): float(row.reserved_qty)
                for row in verify.query(models.ProductionMaterialCustodyProjection)
                .filter_by(is_current=True).all()
            }
            assert cells == (
                {} if close_during_build
                else {("workshop", "SHOP"): 3.0}
            )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_command_worker_prepare_commits_during_building_pause(monkeypatch):
    from app.services import production_control_command_worker as worker
    from tests.services.test_production_partial_materials import _current

    engine = create_engine(PG_URL, pool_size=4, max_overflow=0)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        with Session() as seed:
            work, identity, revision = _current(seed)
            parent_id = int(work.ledger_generation_id)
            parent = seed.get(models.LedgerGeneration, parent_id)
            target = models.LedgerGeneration(
                generation_key="prepare-building", status="building",
                cutoff=parent.cutoff,
                physical_import_batch_id=int(parent.physical_import_batch_id),
                source_watermarks={
                    "parent_generation_id": parent_id,
                    "generation_kind": "obligation_refresh",
                    "accepted_production_mutation_epoch": 0,
                }, capabilities={}, algorithm_version="test",
            )
            seed.add(target)
            if seed.get(models.AcceptedProductionMutationState, 1) is None:
                seed.add(models.AcceptedProductionMutationState(id=1))
            if seed.get(models.ProductionMaterialCustodyProjectionManifest, int(parent.id)) is None:
                seed.add(models.ProductionMaterialCustodyProjectionManifest(
                    ledger_generation_id=int(parent.id),
                    baseline_generation_id=int(parent.id),
                    cutoff=parent.cutoff, status="complete",
                    source_event_high_watermark_id=0,
                ))
            seed.commit()

        monkeypatch.setattr(
            "app.services.production_control_material_availability.preview_make_work_item_materials",
            lambda *args, **kwargs: {
                "qty": kwargs["quantity"],
                "line_quantity": kwargs["quantity"],
                "components": [],
                "coverage_status": "ready",
            },
        )
        entered = Barrier(2)
        release = Barrier(2)
        errors: list[BaseException] = []

        def builder() -> None:
            try:
                from app.services.item_ledger.physical_refresh_orchestrator import (
                    _acquire_lifecycle_lock, _release_lifecycle_lock,
                )
                with Session() as db:
                    assert db.get(models.LedgerGeneration, int(target.id)).status == "building"
                    lifecycle = _acquire_lifecycle_lock(db)
                    try:
                        assert lifecycle is not None
                        entered.wait(timeout=10)
                        release.wait(timeout=10)
                        assert accepted_mutation_tail(db, after_epoch=0).through_epoch == 1
                    finally:
                        _release_lifecycle_lock(lifecycle)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        thread = Thread(target=builder, daemon=True)
        thread.start()
        entered.wait(timeout=10)
        started = monotonic()
        with Session() as command:
            result = worker.execute(command, "materials", {
                "work_item_id": int(work.id), "qty": 3,
                "current_identity": identity,
                "expected_source_revision": revision,
            })
        elapsed = monotonic() - started
        assert result["qty"] == 3
        release.wait(timeout=10)
        thread.join(timeout=10)
        if errors:
            raise errors[0]
        assert elapsed < 2.0
        with Session() as verify:
            row = verify.query(models.CurrentExecutionRow).filter_by(
                business_identity=identity,
            ).one()
            assert "3" in dict(row.payload.get("_material_quantity_snapshots") or {})
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_command_worker_materialize_commits_once_during_building_pause():
    from app.services import production_control_command_worker as worker
    from app.routers.production_control import OrdersFromWorkItemsPayload
    from tests.services.test_production_partial_materials import _current

    engine = create_engine(PG_URL, pool_size=4, max_overflow=0)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        with Session() as seed:
            work, identity, revision = _current(seed)
            parent = seed.get(models.LedgerGeneration, int(work.ledger_generation_id))
            target = models.LedgerGeneration(
                generation_key="materialize-building", status="building",
                cutoff=parent.cutoff,
                physical_import_batch_id=int(parent.physical_import_batch_id),
                source_watermarks={
                    "parent_generation_id": int(parent.id),
                    "generation_kind": "physical_refresh",
                    "accepted_production_mutation_epoch": 0,
                }, capabilities={}, algorithm_version="test",
            )
            seed.add(target)
            if seed.get(models.AcceptedProductionMutationState, 1) is None:
                seed.add(models.AcceptedProductionMutationState(id=1))
            seed.add(models.ProductionMaterialCustodyProjectionManifest(
                ledger_generation_id=int(parent.id),
                baseline_generation_id=int(parent.id),
                cutoff=parent.cutoff, status="complete",
                source_event_high_watermark_id=0,
            ))
            seed.commit()

        entered, release = Barrier(2), Barrier(2)
        errors: list[BaseException] = []
        def builder() -> None:
            try:
                from sqlalchemy import text
                from app.services.mrp_freeze import MRP_LEDGER_LOCK_KEY
                with Session() as db:
                    assert db.get(models.LedgerGeneration, int(target.id)).status == "building"
                    db.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"),
                        {"key": MRP_LEDGER_LOCK_KEY},
                    )
                    entered.wait(timeout=10)
                    release.wait(timeout=10)
                    assert accepted_mutation_tail(db, after_epoch=0).through_epoch >= 1
                    db.rollback()
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
        thread = Thread(target=builder, daemon=True)
        thread.start()
        entered.wait(timeout=10)
        payload = OrdersFromWorkItemsPayload(
            work_items=[{
                "work_item_id": int(work.id), "launch_qty": 3,
                "expected_materialized_qty": 0,
            }],
            current_identities=[identity],
            expected_source_revision=revision,
            require_workshop_routing=False,
        )
        started = monotonic()
        with Session() as command:
            result = worker.execute(
                command, "materialize", payload.model_dump(),
            )
        elapsed = monotonic() - started
        release.wait(timeout=10)
        thread.join(timeout=10)
        if errors:
            raise errors[0]
        assert elapsed < 2.0
        assert len(result["created"]) == 1
        with Session() as verify:
            assert verify.query(models.ProductionProduct).count() == 1
            assert verify.query(models.ProductionOrder).count() == 1
            scope = verify.query(models.CurrentExecutionScope).filter_by(
                entity_kind="production_control_journal",
                scope_key="production:all-live-orders",
            ).one()
            assert scope.result_ready is True
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_full_physical_publisher_rebases_new_materialized_executor_at_final_cas(
    monkeypatch,
):
    from types import SimpleNamespace
    from app.services import production_control_command_worker as worker
    from app.routers.production_control import OrdersFromWorkItemsPayload
    from app.services.item_ledger import physical_refresh_current_publish as publisher
    from tests.services.test_production_partial_materials import _current

    engine = create_engine(PG_URL, pool_size=4, max_overflow=0)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        with Session() as seed:
            work, identity, revision = _current(seed)
            parent = seed.get(models.LedgerGeneration, int(work.ledger_generation_id))
            target_cutoff = parent.cutoff.replace(tzinfo=timezone.utc)
            target_cutoff = target_cutoff.replace(hour=(target_cutoff.hour + 1) % 24)
            target_batch = models.PhysicalImportBatch(
                batch_key="full-publisher-target", status="completed",
                source_complete=True, cutoff=target_cutoff,
                completed_at=target_cutoff, source_watermarks={},
            )
            seed.add(target_batch)
            seed.flush()
            target = models.LedgerGeneration(
                generation_key="full-publisher-target", status="building",
                cutoff=target_cutoff, physical_import_batch_id=int(target_batch.id),
                source_watermarks={
                    "parent_generation_id": int(parent.id),
                    "generation_kind": "physical_refresh",
                    "accepted_production_mutation_epoch": 0,
                }, capabilities={}, algorithm_version="test",
            )
            seed.add(target)
            seed.flush()
            accepted_owner = seed.query(models.ReservationEntry).filter_by(
                requirement_id=int(work.requirement_id), is_current=True,
            ).one()
            seed.get(models.Item, int(work.item_id)).optimal_batch = 5
            accepted_owner.replenishment_received_qty = 0
            accepted_owner.realized_qty = 0
            work.replenishment_fulfilled_qty = 0
            work.replenishment_remaining_qty = 10
            proposal_row = seed.query(models.CurrentExecutionRow).filter_by(
                business_identity=identity,
            ).one()
            proposal_row.payload = {
                **dict(proposal_row.payload or {}),
                "launchable_qty": 10,
                "replenishment_fulfilled_qty": 0,
                "replenishment_remaining_qty": 10,
            }
            stage_values = {
                column.name: getattr(accepted_owner, column.name)
                for column in models.ReservationEntry.__table__.columns
                if column.name not in {"id", "created_at", "updated_at"}
            }
            stage_values.update(
                ledger_generation_id=int(target.id), owner_kind="building",
                is_current=False, replenishment_received_qty=3, realized_qty=3,
            )
            stage_owner = models.ReservationEntry(**stage_values)
            seed.add(stage_owner)
            seed.flush()
            seed.add(models.ReservationEvent(
                ledger_generation_id=int(target.id),
                reservation_id=int(stage_owner.id), item_id=int(stage_owner.item_id),
                characteristic_ref=str(stage_owner.characteristic_ref or ""),
                organization_ref=str(stage_owner.organization_ref or ""),
                planning_stock_pool=str(stage_owner.planning_stock_pool or "default"),
                event_kind="open", reserved_delta=stage_owner.reserved_qty,
                realized_delta=0, fact_ref="", fact_line_ref="",
                match_rule="", cycle_id="target-net7",
                idempotency_key="target-net7-open", origin_kind="obligation",
                is_current=False,
            ))
            target_fact = models.StockLedgerEntry(
                ingest_batch_id=int(target_batch.id),
                source_content_hash="target-net7-assembly-in",
                business_identity="target-net7-assembly-in",
                item_id=int(work.item_id), characteristic_ref="",
                organization_ref="", warehouse_ref1c="WH",
                qty=3, posting_at=target_cutoff, record_type="Receipt",
                movement_kind="assembly_in", recorder_type="Document_СборкаЗапасов",
                recorder_ref="target-net7", line_no="1",
                ingest_source="test", active=True,
            )
            seed.add(target_fact)
            if seed.get(models.AcceptedProductionMutationState, 1) is None:
                seed.add(models.AcceptedProductionMutationState(id=1))
            if seed.get(models.ProductionMaterialCustodyProjectionManifest, int(parent.id)) is None:
                seed.add(models.ProductionMaterialCustodyProjectionManifest(
                    ledger_generation_id=int(parent.id),
                    baseline_generation_id=int(parent.id), cutoff=parent.cutoff,
                    status="complete", source_event_high_watermark_id=0,
                ))
            seed.commit()
            parent_id, target_id, target_fact_id = (
                int(parent.id), int(target.id), int(target_fact.id),
            )

        class Result:
            changed_rows = 0
            closed_rows = 0
            idempotent = True

        empty_assembly = SimpleNamespace(
            queue_rows=(), readiness_rows=(), readiness_metrics={},
        )
        monkeypatch.setattr(
            publisher, "apply_bounded_current_stock_bins",
            lambda *a, **k: SimpleNamespace(changed_keys=0),
        )
        monkeypatch.setattr(
            publisher, "supplier_future_supply_delta",
            lambda *a, **k: SimpleNamespace(changed_scopes=(), evidence=()),
        )
        monkeypatch.setattr(publisher, "_capture_bounded_future_supply", lambda *a, **k: None)
        monkeypatch.setattr(
            publisher, "build_compact_current_assembly_payload",
            lambda *a, **k: empty_assembly,
        )
        monkeypatch.setattr(
            publisher, "build_compact_current_drum_payload",
            lambda *a, **k: SimpleNamespace(rows=(), metrics={}),
        )
        monkeypatch.setattr(
            publisher, "build_compact_current_shelf_payload",
            lambda *a, **k: SimpleNamespace(rows=(), metrics={}),
        )
        monkeypatch.setattr(
            publisher, "build_compact_current_purchase_control_payload",
            lambda *a, **k: {"rows": [], "meta": {}, "summary": {}},
        )
        monkeypatch.setattr(
            publisher, "_build_obligation_view_payloads", lambda *a, **k: ({}, {}),
        )
        monkeypatch.setattr(
            publisher, "prepare_current_obligation_views_from_generation",
            lambda *a, **k: SimpleNamespace(),
        )
        monkeypatch.setattr(
            publisher, "install_prepared_current_obligation_views",
            lambda *a, **k: {
                "purchase_control_journal": Result(),
                "mrp_result": Result(), "period_plan_execution": Result(),
            },
        )
        monkeypatch.setattr(
            publisher, "prepare_compact_current_execution_views",
            lambda *a, **k: SimpleNamespace(),
        )
        monkeypatch.setattr(
            publisher, "install_prepared_compact_current_execution_views",
            lambda *a, **k: {
                "assembly_queue": Result(), "assembly_readiness": Result(),
                "drum": Result(), "shelf": Result(),
            },
        )
        monkeypatch.setattr(publisher, "publish_current_future_supply", lambda *a, **k: {})
        monkeypatch.setattr(publisher, "lost_supplier_receipt_provenance_sle_ids", lambda *a, **k: ())
        monkeypatch.setattr(publisher, "_untyped_buy_owned_receipt_ids", lambda *a, **k: ())

        payload = OrdersFromWorkItemsPayload(
            work_items=[{
                "work_item_id": int(work.id), "launch_qty": 5,
                "expected_materialized_qty": 0,
            }], current_identities=[identity],
            expected_source_revision=revision, require_workshop_routing=False,
        )
        original_lock = publisher.lock_publication_epoch
        lock_calls = []
        command_result = {}
        def inject_then_lock(db, **kwargs):
            lock_calls.append(len(lock_calls) + 1)
            if len(lock_calls) == 1:
                with Session() as command:
                    command_result.update(worker.execute(
                        command, "materialize", payload.model_dump(),
                    ))
            return original_lock(db, **kwargs)
        monkeypatch.setattr(publisher, "lock_publication_epoch", inject_then_lock)

        with Session() as publish:
            target_fact = publish.get(models.StockLedgerEntry, target_fact_id)
            result = publisher.publish_forward_physical_refresh_current(
                publish, target_generation_id=target_id,
                parent_generation_id=parent_id,
                delta_manifest={
                    "rows": (target_fact,), "supersessions": (),
                }, odata_client=None,
                source_revision=int(
                    publish.get(models.LedgerGeneration, target_id).physical_import_batch_id
                ), planning_pool_by_warehouse={"WH": "default"},
            )
            publish.commit()
        assert result.target_generation_id == target_id
        assert lock_calls == [1, 2]
        assert len(command_result["created"]) == 1
        with Session() as verify:
            pointer = verify.get(models.PlanningTruthState, 1)
            assert int(pointer.current_generation_id) == target_id
            products = verify.query(models.ProductionProduct).all()
            assert len(products) == 1
            rows = verify.query(models.CurrentExecutionRow).filter_by(
                entity_kind="production_control_journal",
                result_status="accepted", result_ready=True,
            ).all()
            payloads = [dict(row.payload or {}) for row in rows]
            assert any(int(row.get("product_id") or 0) == int(products[0].product_id) for row in payloads)
            residual = next(
                dict(row.payload or {}) for row in rows
                if str(row.business_identity).startswith("mrp-reservation:")
            )
            assert float(residual["launchable_qty"]) == 2.0
            assert float(products[0].quantity) == 5.0
            owner = verify.query(models.ReservationEntry).filter_by(
                current_identity=identity.replace("mrp-reservation:", ""),
                is_current=True,
            ).one()
            assert float(owner.replenishment_required_qty) == 10.0
            assert float(owner.replenishment_received_qty) == 3.0
            assert verify.query(models.ProductionOrder).count() == 1
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_full_obligation_publisher_rebases_materialize_into_replacement_requirement(
    monkeypatch,
):
    from app.routers.production_control import OrdersFromWorkItemsPayload
    from app.services import accepted_command_mutation, production_control_command_worker
    from app.services.item_ledger.current_execution import (
        publish_current_execution_scope,
        publish_current_production_control_from_payload,
    )
    from app.services.item_ledger.reservation import reservation_event_identity
    from tests.services.test_obligation_refresh_publish import _batch, _publish
    from app.services import obligation_refresh_publish as obligation_publisher

    engine = create_engine(PG_URL, pool_size=4, max_overflow=0)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        with Session() as publish:
            cutoff, parent, target, parents, candidates = _batch(
                publish, count=1, replace_count=1,
            )
            parent_run, target_run = parents[0], candidates[0]
            parent_run.ledger_cutoff = parent.cutoff
            target_run.ledger_cutoff = target.cutoff
            parent_run.active_freeze_version = 1
            target_run.active_freeze_version = 1
            parent.capabilities = {
                "physical_ledger": True,
                "reservation_replay": True,
                "execution_allocations": True,
            }
            line = publish.query(models.ProductionPlanLine).filter_by(
                plan_id=int(parent_run.source_plan_id)
            ).one()
            item_id = int(line.item_id)
            if publish.get(models.PlanningLivePointer, int(parent_run.source_plan_id)) is None:
                publish.add(models.PlanningLivePointer(
                    plan_id=int(parent_run.source_plan_id), run_id=int(parent_run.run_id),
                ))
            req_a = models.MrpRequirement(
                run_id=int(parent_run.run_id), item_id=item_id,
                total_required_qty=10, net_required_qty=10,
                period_from=parent_run.period_from, period_to=parent_run.period_to,
                bom_level=0, freeze_version=1,
            )
            req_b = models.MrpRequirement(
                run_id=int(target_run.run_id), item_id=item_id,
                total_required_qty=7, net_required_qty=7,
                period_from=target_run.period_from, period_to=target_run.period_to,
                bom_level=0, freeze_version=1,
            )
            publish.add_all((req_a, req_b))
            publish.flush()
            identity_a = f"reservation:req:{int(req_a.id)}:mode:make"
            identity_b = f"reservation:req:{int(req_b.id)}:mode:make"
            common = dict(
                item_id=item_id, characteristic_ref="", organization_ref="",
                planning_stock_pool="default", freeze_version=1,
                priority_period_from=parent_run.period_from,
                priority_period_to=parent_run.period_to, realization_mode="make",
                reserved_qty=10, covered_from_stock_at_freeze_qty=0,
                replenishment_received_qty=0, realized_qty=0,
                lifecycle_status="active",
            )
            owner_a = models.ReservationEntry(
                **common, ledger_generation_id=int(parent.id), run_id=int(parent_run.run_id),
                requirement_id=int(req_a.id), replenishment_required_qty=10,
                owner_kind="current", is_current=True, current_identity=identity_a,
            )
            stage_b = models.ReservationEntry(
                **{**common, "reserved_qty": 7}, ledger_generation_id=int(target.id),
                run_id=int(target_run.run_id), requirement_id=int(req_b.id),
                replenishment_required_qty=7, owner_kind="building",
                is_current=False, current_identity=identity_b,
            )
            publish.add_all((owner_a, stage_b))
            publish.flush()
            publish.add(models.ReservationEvent(
                ledger_generation_id=int(target.id), reservation_id=int(stage_b.id),
                item_id=item_id, characteristic_ref="", organization_ref="",
                planning_stock_pool="default", event_kind="open",
                reserved_delta=7, realized_delta=0, fact_ref="", fact_line_ref="",
                match_rule="", cycle_id="obligation-pg", idempotency_key="obligation-pg-open",
                event_identity=reservation_event_identity(
                    stage_b, event_kind="open", sle_id=None,
                    fact_ref="", fact_line_ref="", reserved_delta=7,
                    realized_delta=0, match_rule="",
                ), origin_kind="obligation", is_current=False,
            ))
            work = models.ReplenishmentWorkItem(
                ledger_generation_id=int(parent.id), reservation_id=int(owner_a.id),
                plan_id=int(parent_run.source_plan_id), run_id=int(parent_run.run_id),
                requirement_id=int(req_a.id), item_id=item_id,
                replenishment_method="make", replenishment_required_qty=10,
                replenishment_fulfilled_qty=0, replenishment_remaining_qty=10,
            )
            publish.add(work)
            publish.flush()
            current_identity = f"mrp-reservation:{identity_a}"
            publish_current_production_control_from_payload(publish, int(parent.id), {
                "meta": {}, "rows": [{
                    "current_identity": current_identity, "item_id": item_id,
                    "source_mrp_requirement_id": int(req_a.id),
                    "source_run_id": int(parent_run.run_id), "launchable_qty": 10,
                    "materialized_order_qty": 0, "order_number": "MRP-A",
                    "root_item_ids": [],
                    "material_coverage_snapshot": {
                        "qty": 10, "line_quantity": 10, "components": [],
                        "coverage_status": "ready",
                    },
                }],
            })
            for scope, kinds in (
                ("assembly:all-live-plans", ("assembly_queue", "assembly_readiness")),
                ("drum:all-live-plans", ("drum_schedule", "drum_slot", "drum_gap", "drum_excluded")),
                ("shelf:all-live-mrps", ("shelf_projection",)),
            ):
                publish_current_execution_scope(
                    publish, source_revision=f"accepted:g{int(parent.id)}:{scope}",
                    source_generation_id=int(parent.id), scope_key=scope,
                    rows=[], entity_kinds=kinds,
                )
            publish.commit()

            command_payload = OrdersFromWorkItemsPayload(
                work_items=[{"work_item_id": int(work.id), "launch_qty": 5,
                             "expected_materialized_qty": 0}],
                current_identities=[current_identity],
                expected_source_revision=f"accepted:g{int(parent.id)}:production_control_journal",
                require_workshop_routing=False,
            )
            original_lock = accepted_command_mutation.lock_publication_epoch
            calls = []
            command_elapsed = []
            pre_final_owner = {}
            def inject(db, **kwargs):
                calls.append(len(calls) + 1)
                if len(calls) == 1:
                    with Session() as command:
                        started = monotonic()
                        production_control_command_worker.execute(
                            command, "materialize", command_payload.model_dump(),
                        )
                        command_elapsed.append(monotonic() - started)
                        owner = command.query(models.ReservationEntry).filter_by(
                            current_identity=identity_a, is_current=True,
                        ).one()
                        pre_final_owner.update(
                            received=float(owner.replenishment_received_qty),
                            owner_kind=str(owner.owner_kind),
                        )
                return original_lock(db, **kwargs)
            monkeypatch.setattr(accepted_command_mutation, "lock_publication_epoch", inject)
            monkeypatch.setattr(
                obligation_publisher,
                "_require_period_rows_for_remaining_basis",
                lambda *args, **kwargs: None,
            )
            result = _publish(publish, parent, target, cutoff)
            publish.commit()
            assert result.published is True
            assert calls == [1, 2]
            assert command_elapsed[0] < 2.0
            assert pre_final_owner == {"received": 0.0, "owner_kind": "current"}

        with Session() as verify:
            assert int(verify.get(models.PlanningTruthState, 1).current_generation_id) == int(target.id)
            products = verify.query(models.ProductionProduct).all()
            assert len(products) == 1 and float(products[0].quantity) == 5.0
            rows = verify.query(models.CurrentExecutionRow).filter_by(
                entity_kind="production_control_journal", result_ready=True,
            ).all()
            residual = next(
                dict(row.payload or {}) for row in rows
                if str(row.business_identity) == f"mrp-reservation:{identity_b}"
            )
            assert float(residual["launchable_qty"]) == 2.0
            target_owner = verify.query(models.ReservationEntry).filter_by(
                current_identity=identity_b, is_current=True,
                owner_kind="current",
            ).one()
            assert float(target_owner.replenishment_required_qty) == 7.0
            assert verify.query(models.ProductionOrder).count() == 1
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()

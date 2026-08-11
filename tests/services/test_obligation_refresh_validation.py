"""An obligation refresh must pass a structural gate before it becomes truth.

``accept_generation_build`` has always validated its build; the obligation
refresh publisher had no structural gate at all, so a candidate with a StockBin
fold that disagreed with the immutable physical prefix, or a reservation cache
that disagreed with its own event tape, could be published unchecked.
"""

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json

import pytest

from app import models
from app.services import obligation_refresh_orchestrator as workflow
from app.services.item_ledger.generation_lifecycle import (
    GenerationValidationError,
    RESERVATION_CONSUMPTION_ALGORITHM_VERSION,
    validate_obligation_refresh_build,
)
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C

from tests.services.test_obligation_refresh_orchestrator import _run, _world


CUTOFF = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)


def _lineage(db):
    """One accepted parent and one BUILDING obligation-refresh child."""
    physical = models.PhysicalImportBatch(
        batch_key="validation-physical",
        status="completed",
        cutoff=CUTOFF,
        source_watermarks={},
        completed_at=CUTOFF,
    )
    parent = models.LedgerGeneration(
        generation_key="validation-parent",
        status="accepted",
        cutoff=CUTOFF,
        accepted_at=CUTOFF,
        physical_import_batch=physical,
        algorithm_version="test",
        replay_version="test",
        source_watermarks={"replay_from": "2026-07-01T00:00:00+00:00"},
        capabilities={"physical_ledger": True},
    )
    db.add(parent)
    db.flush()
    target = models.LedgerGeneration(
        generation_key="validation-target",
        status="building",
        cutoff=CUTOFF,
        physical_import_batch_id=int(physical.id),
        algorithm_version="test",
        replay_version="test",
        source_watermarks={
            "generation_kind": "obligation_refresh",
            "parent_generation_id": int(parent.id),
        },
        capabilities={},
    )
    db.add(target)
    db.flush()
    return parent, target


def _retained_reservation(db, generation, run, requirement, item, *, cycle_id: str):
    entry = models.ReservationEntry(
        ledger_generation_id=int(generation.id),
        item_id=item.item_id,
        run_id=run.run_id,
        requirement_id=requirement.id,
        priority_period_from=CUTOFF.date(),
        priority_period_to=CUTOFF.date(),
        realization_mode="buy",
        reserved_qty=Decimal("7"),
        realized_qty=Decimal("0"),
        replenishment_required_qty=Decimal("7"),
        replenishment_received_qty=Decimal("0"),
        lifecycle_status="active",
    )
    db.add(entry)
    db.flush()
    db.add(models.ReservationEvent(
        ledger_generation_id=int(generation.id),
        reservation_id=int(entry.id),
        item_id=item.item_id,
        event_kind="open",
        reserved_delta=Decimal("7"),
        realized_delta=Decimal("0"),
        cycle_id=cycle_id,
        idempotency_key=f"open:{requirement.id}:buy:1",
        event_at=CUTOFF,
    ))
    db.flush()
    return entry


def _obligation_world(db):
    parent, target = _lineage(db)
    item = models.Item(item_code="VALIDATION", item_name="validation")
    run = models.PlanningRun(config_snapshot={})
    db.add_all([item, run])
    db.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        period_from=CUTOFF.date(),
        period_to=CUTOFF.date(),
    )
    db.add(requirement)
    db.flush()
    return parent, target, item, run, requirement


def _execution_allocation_checksum(rows: list[dict[str, object]]) -> str:
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(raw.encode("utf-8")).hexdigest()


def _add_obligation_execution_allocation_batch(
    db_session,
    generation: models.LedgerGeneration,
    *,
    algorithm_version: str = RESERVATION_CONSUMPTION_ALGORITHM_VERSION,
    allocation_rows: list[dict[str, object]] | None = None,
) -> models.LedgerBuildBatch:
    allocation_rows = list(allocation_rows or [])
    batch = models.LedgerBuildBatch(
        ledger_generation_id=int(generation.id),
        stage="execution_allocation",
        batch_key=f"execution-allocation-{generation.id}",
        status="completed",
        algorithm_version=algorithm_version,
        completed_at=CUTOFF,
        metrics={
            "facts": "0",
            "allocations": str(len(allocation_rows)),
            "fact_qty": "0",
            "allocated_qty": "0",
            "surplus_qty": "0",
            "allocation_checksum": _execution_allocation_checksum(allocation_rows),
        },
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def _stock_ledger_entry_for_allocation_drift(
    db_session,
    generation: models.LedgerGeneration,
    item: models.Item,
    *,
    qty: Decimal = Decimal("1"),
) -> models.StockLedgerEntry:
    other_batch = models.PhysicalImportBatch(
        batch_key=f"allocation-drift-batch-{generation.id}",
        status="completed",
        cutoff=CUTOFF,
        completed_at=CUTOFF,
        source_watermarks={},
    )
    db_session.add(other_batch)
    db_session.flush()
    row = models.StockLedgerEntry(
        ingest_batch_id=int(other_batch.id),
        source_content_hash=sha256(
            f"allocation-drift-{generation.id}-{item.item_id}".encode()
        ).hexdigest(),
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c="WH-OUT",
        qty=qty,
        qty_after=qty,
        posting_at=CUTOFF,
        record_type="Receipt",
        movement_kind="assembly_in",
        recorder_type="Production",
        recorder_ref="allocation-drift",
        line_no="1",
        ingest_source="test",
        active=False,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_carried_forward_events_keep_the_parent_cycle_and_stay_valid(db_session):
    """The retained projection is copied verbatim, parent cycle included.

    ``carry_forward_retained_reservations`` is a registered ReservationEvent
    writer precisely so the copy keeps the provenance of the generation that
    first recorded the fact.  The validator must read that as lineage, not as a
    legacy row.
    """
    parent, target, item, run, requirement = _obligation_world(db_session)
    _retained_reservation(
        db_session,
        target,
        run,
        requirement,
        item,
        cycle_id=f"historical-obligations:g{parent.id}",
    )
    _add_obligation_execution_allocation_batch(db_session, target)

    result = validate_obligation_refresh_build(db_session, int(target.id))

    assert result["valid"] is True
    assert result["reservation_events"] == 1
    assert result["carried_forward_generations"] == [int(parent.id)]


def test_event_from_a_generation_outside_the_lineage_is_rejected(db_session):
    parent, target, item, run, requirement = _obligation_world(db_session)
    foreign_id = int(parent.id) + int(target.id) + 1000
    _retained_reservation(
        db_session,
        target,
        run,
        requirement,
        item,
        cycle_id=f"historical-obligations:g{foreign_id}",
    )
    _add_obligation_execution_allocation_batch(db_session, target)

    with pytest.raises(GenerationValidationError, match="legacy reservation event"):
        validate_obligation_refresh_build(db_session, int(target.id))


def test_reservation_cache_that_disagrees_with_its_events_is_rejected(db_session):
    parent, target, item, run, requirement = _obligation_world(db_session)
    entry = _retained_reservation(
        db_session,
        target,
        run,
        requirement,
        item,
        cycle_id=f"historical-obligations:g{parent.id}",
    )
    entry.reserved_qty = Decimal("9")
    db_session.flush()
    _add_obligation_execution_allocation_batch(db_session, target)

    with pytest.raises(GenerationValidationError, match="differs from event fold"):
        validate_obligation_refresh_build(db_session, int(target.id))


def test_corrupt_frozen_reservation_amounts_are_rejected(db_session):
    parent, target, item, run, requirement = _obligation_world(db_session)
    entry = _retained_reservation(
        db_session,
        target,
        run,
        requirement,
        item,
        cycle_id=f"historical-obligations:g{parent.id}",
    )
    entry.replenishment_required_qty = Decimal("8")
    db_session.flush()
    _add_obligation_execution_allocation_batch(db_session, target)

    with pytest.raises(
        GenerationValidationError,
        match=r"covered \+ replenishment_required == reserved",
    ):
        validate_obligation_refresh_build(db_session, int(target.id))


def test_generation_with_net_above_gross_is_rejected(db_session):
    _parent, target, _item, run, requirement = _obligation_world(db_session)
    run.ledger_generation_id = int(target.id)
    requirement.total_required_qty = Decimal("5")
    requirement.net_required_qty = Decimal("6")
    db_session.flush()
    _add_obligation_execution_allocation_batch(db_session, target)

    with pytest.raises(
        GenerationValidationError,
        match="0 <= net <= gross",
    ):
        validate_obligation_refresh_build(db_session, int(target.id))


def test_validation_requires_an_obligation_refresh_generation(db_session):
    _parent, target = _lineage(db_session)
    target.source_watermarks = {
        **dict(target.source_watermarks or {}),
        "generation_kind": "physical_refresh",
    }
    db_session.flush()

    with pytest.raises(GenerationValidationError, match="obligation_refresh generation"):
        validate_obligation_refresh_build(db_session, int(target.id))


def test_orchestrator_refuses_to_publish_a_structurally_broken_candidate(
    db_session, monkeypatch
):
    """A corrupted StockBin fold must stop the refresh, not reach the pointer."""
    accepted, _plan, _line, item, _parent, _cutoff = _world(db_session)
    real_builder = workflow.build_assembly_queue_snapshot

    def corrupt_then_build(db, generation_id, *args, **kwargs):
        db.add(models.StockBin(
            ledger_generation_id=int(generation_id),
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c="GHOST",
            on_hand=Decimal("42"),
        ))
        db.flush()
        return real_builder(db, generation_id, *args, **kwargs)

    monkeypatch.setattr(workflow, "build_assembly_queue_snapshot", corrupt_then_build)

    with pytest.raises(
        workflow.ObligationRefreshOrchestratorError, match="structurally invalid"
    ):
        _run(db_session, accepted, "orch-corrupt")

    db_session.rollback()
    assert db_session.get(
        models.PlanningTruthState, 1
    ).current_generation_id == accepted.id


def test_orchestrator_refuses_stale_work_items_after_replay(
    db_session, monkeypatch
):
    accepted, plan, _line, _item, _parent, _cutoff = _world(
        db_session,
        with_parent=False,
    )
    real_builder = workflow.materialize_replenishment_work_items

    def corrupt_after_build(db, generation_id, batch_id):
        result = real_builder(db, generation_id, batch_id)
        row = (
            db.query(models.ReplenishmentWorkItem)
            .filter_by(ledger_generation_id=int(generation_id))
            .first()
        )
        assert row is not None
        row.replenishment_fulfilled_qty = Decimal("1")
        row.replenishment_remaining_qty = (
            Decimal(row.replenishment_required_qty) - Decimal("1")
        )
        db.flush()
        return result

    monkeypatch.setattr(
        workflow,
        "materialize_replenishment_work_items",
        corrupt_after_build,
    )

    with pytest.raises(
        workflow.ObligationRefreshOrchestratorError,
        match="work item .* differs from reservation fold",
    ):
        _run(
            db_session,
            accepted,
            "orch-stale-work-items",
            add=[plan.id],
        )

    db_session.rollback()
    assert db_session.get(
        models.PlanningTruthState, 1
    ).current_generation_id == accepted.id


def test_obligation_refresh_validation_rejects_missing_execution_allocation_batch(
    db_session,
):
    _parent, target, _item, _run, _requirement = _obligation_world(db_session)

    with pytest.raises(GenerationValidationError, match="execution_allocation"):
        validate_obligation_refresh_build(db_session, int(target.id))


def test_obligation_refresh_validation_rejects_wrong_execution_allocation_algorithm(
    db_session,
):
    _parent, target, _item, _run, _requirement = _obligation_world(db_session)
    _add_obligation_execution_allocation_batch(
        db_session,
        target,
        algorithm_version="wrong/version",
    )

    with pytest.raises(
        GenerationValidationError,
        match="unexpected reservation consumption allocation algorithm",
    ):
        validate_obligation_refresh_build(db_session, int(target.id))


def test_obligation_refresh_validation_allows_zero_execution_allocation_rows(db_session):
    _parent, target, _item, _run, _requirement = _obligation_world(db_session)
    _add_obligation_execution_allocation_batch(db_session, target)

    result = validate_obligation_refresh_build(db_session, int(target.id))

    assert result["valid"] is True


def test_obligation_refresh_validation_rejects_execution_allocation_metric_row_drift(
    db_session,
):
    _parent, target, item, run, requirement = _obligation_world(db_session)
    reservation = _retained_reservation(
        db_session,
        target,
        run,
        requirement,
        item,
        cycle_id=f"historical-obligations:g{_parent.id}",
    )
    fact = _stock_ledger_entry_for_allocation_drift(db_session, target, item)
    db_session.add(
        models.ReservationConsumptionAllocation(
            ledger_generation_id=int(target.id),
            reservation_id=int(reservation.id),
            sle_id=int(fact.id),
            requirement_id=int(requirement.id),
            allocated_qty=Decimal("1"),
            match_rule="fifo",
            fact_ref="allocation-drift",
            fact_line_ref="1",
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            planning_stock_pool="selected",
            idempotency_key=f"drift:g{target.id}:r{reservation.id}:sle{fact.id}",
            event_at=CUTOFF,
        )
    )
    db_session.flush()
    batch = _add_obligation_execution_allocation_batch(
        db_session,
        target,
        allocation_rows=[
            {
                "sle_id": int(fact.id),
                "reservation_id": int(reservation.id),
                "requirement_id": int(requirement.id),
                "qty": "1",
                "match_rule": "fifo",
                "idempotency_key": "allocation-drift",
            },
        ],
    )
    batch.metrics["allocations"] = "0"
    db_session.flush()
    with pytest.raises(
        GenerationValidationError,
        match="execution allocation metric does not match allocation row count",
    ):
        validate_obligation_refresh_build(db_session, int(target.id))


def test_obligation_refresh_validation_rejects_execution_allocation_checksum_drift(
    db_session,
):
    _parent, target, _item, _run, _requirement = _obligation_world(db_session)
    batch = _add_obligation_execution_allocation_batch(db_session, target)
    batch.metrics["allocation_checksum"] = (
        "00000000000000000000000000000000000000000000000000000000000000000000"
    )
    db_session.flush()

    with pytest.raises(
        GenerationValidationError,
        match="execution allocation checksum mismatch",
    ):
        validate_obligation_refresh_build(db_session, int(target.id))

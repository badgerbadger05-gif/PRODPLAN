"""Contract of ``--phase retire-closed-owner-allocations``.

A closed owner used to keep its current R4 allocations, so a fact it held was
counted again by the live owner the successor replay handed it to.  The
runtime now retires those claims at every obligation-refresh publication;
this phase heals a database already in that state without waiting for one.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models
from app.services.item_ledger.current_replenishment import (
    SUPPLIER_RECEIPT_SOURCE_KEY,
    _scope_key,
)
from tools.current_execution_migration import (
    PreflightBlocked,
    apply_retire_closed_owner_allocations,
)

CUTOFF = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


def _engine():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    return engine


def _world(engine):
    """One receipt of 2 held by a closed owner and by a live one: 4 > 2."""
    with Session(engine) as session:
        batch = models.PhysicalImportBatch(
            batch_key="retire-closed", status="completed", cutoff=CUTOFF,
            source_watermarks={}, source_complete=True, completed_at=CUTOFF,
        )
        session.add(batch)
        session.flush()
        generation = models.LedgerGeneration(
            generation_key="retire-closed-pointer", status="accepted", cutoff=CUTOFF,
            accepted_at=CUTOFF, source_watermarks={}, capabilities={},
            physical_import_batch_id=int(batch.id), algorithm_version="retire-tests",
        )
        item = models.Item(item_code="RETIRE-1", item_name="Retire item")
        session.add_all([generation, item])
        session.flush()
        session.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
        receipt = models.StockLedgerEntry(
            ingest_batch_id=int(batch.id), source_content_hash="retire".ljust(64, "0"),
            item_id=int(item.item_id), characteristic_ref="", organization_ref="",
            warehouse_ref1c="WH", qty=Decimal("2"), posting_at=CUTOFF,
            record_type="Receipt", recorder_type="Doc", recorder_ref="retire",
            line_no="1", ingest_source="seed",
        )
        session.add(receipt)
        session.flush()
        scope = (int(item.item_id), "", "", "default", "buy")
        session.add(models.CurrentReplenishmentState(
            scope_key=_scope_key(scope), source_key=SUPPLIER_RECEIPT_SOURCE_KEY,
            ledger_generation_id=int(generation.id), source_revision=int(generation.id),
            scope_checksum="seed".ljust(64, "0"), writer_key="current_replenishment",
            status="completed",
        ))
        owners = []
        for index, status in enumerate(("closed", "active"), start=1):
            run = models.PlanningRun(
                status="FIXED_SNAPSHOT", ledger_generation_id=generation.id,
                config_snapshot={}, active_freeze_version=1, ledger_cutoff=CUTOFF,
            )
            session.add(run)
            session.flush()
            requirement = models.MrpRequirement(
                run_id=run.run_id, item_id=item.item_id, total_required_qty=Decimal("5"),
                net_required_qty=Decimal("5"), period_from=date(2026, 9, 1),
                period_to=date(2026, 9, 30), bom_level=0, planning_stock_pool="default",
                characteristic_ref="", organization_ref="", freeze_version=1,
            )
            session.add(requirement)
            session.flush()
            owner = models.ReservationEntry(
                ledger_generation_id=generation.id, item_id=item.item_id,
                run_id=run.run_id, freeze_version=1, requirement_id=requirement.id,
                priority_period_from=date(2026, 9, 1), priority_period_to=date(2026, 9, 30),
                realization_mode="buy", reserved_qty=Decimal("5"),
                replenishment_required_qty=Decimal("5"), lifecycle_status=status,
                owner_kind="current", is_current=True,
                current_identity=f"reservation:req:{int(requirement.id)}:mode:buy",
            )
            session.add(owner)
            session.flush()
            session.add(models.ReservationConsumptionAllocation(
                ledger_generation_id=generation.id, reservation_id=owner.id,
                sle_id=receipt.id, requirement_id=requirement.id,
                allocated_qty=Decimal("2"), match_rule="fifo", fact_ref="retire",
                fact_line_ref="1", item_id=item.item_id, characteristic_ref="",
                organization_ref="", planning_stock_pool="default",
                idempotency_key=f"retire-{index}", allocation_role="replenishment_receipt",
                is_current=True, event_at=CUTOFF,
            ))
            owners.append(int(owner.id))
        session.commit()
        return int(generation.id), owners, int(receipt.id)


def test_retirement_requires_writers_stopped():
    engine = _engine()
    _world(engine)
    with pytest.raises(PreflightBlocked, match="writers-stopped"):
        apply_retire_closed_owner_allocations(engine, writers_stopped=False)


def test_retirement_heals_the_double_count_and_is_idempotent():
    engine = _engine()
    generation_id, (closed_id, live_id), receipt_id = _world(engine)

    first = apply_retire_closed_owner_allocations(engine, writers_stopped=True)
    second = apply_retire_closed_owner_allocations(engine, writers_stopped=True)

    assert first["status"] == "ready"
    assert first["generation_id"] == generation_id
    assert first["closed_owner_allocations_before"] == 1
    assert first["closed_owner_allocation_units_before"] == 2
    assert first["retired_allocations"] == 1
    assert first["closed_owner_allocations_after"] == 0
    assert first["over_allocated_facts_before"] == 1
    assert first["over_allocated_facts_after"] == 0
    assert first["idempotent"] is False
    assert second["retired_allocations"] == 0
    assert second["idempotent"] is True
    with Session(engine) as session:
        current = session.query(models.ReservationConsumptionAllocation).filter_by(
            sle_id=receipt_id, is_current=True,
        ).all()
        assert [int(row.reservation_id) for row in current] == [live_id]
        audit = session.query(models.CurrentReplenishmentAudit).filter_by(
            reservation_id=closed_id, reason="owner_retired",
        ).one()
        assert audit.operation == "retire"
        assert int(audit.source_revision) == generation_id

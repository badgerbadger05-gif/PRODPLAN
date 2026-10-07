"""Contract of ``--phase replenishment-rebase``.

The bounded physical refresh replays a MAKE distribution scope only when its
delta carries an ``assembly_in`` of that item, so a stand migrated from a live
copy starts with no current MAKE replenishment basis at all: on the 28.09 copy
1586 current allocations on ``ПриходнаяНакладная`` against 79 on
``СборкаЗапасов``.  This phase hands every MAKE scope of the live owners to the
one canonical MAKE writer, at the accepted pointer.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C
from app.services.item_ledger.current_replenishment import (
    ASSEMBLY_OUTPUT_SOURCE_KEY,
    BASIS_CORRECTED_REASON,
    CurrentReplenishmentError,
    apply_current_replenishment_for_bounded_make_scopes,
    _scope_key,
)
from app.services.item_ledger.physical_refresh_current_publish import (
    all_current_make_scopes,
)
from tools.current_execution_migration import (
    PreflightBlocked,
    apply_replenishment_rebase,
)


FREEZE_AT = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


def _engine():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    return engine


def _batch(session, key: str, cutoff: datetime) -> models.PhysicalImportBatch:
    batch = models.PhysicalImportBatch(
        batch_key=key, status="completed", cutoff=cutoff,
        source_watermarks={}, source_complete=True, completed_at=cutoff,
    )
    session.add(batch)
    session.flush()
    return batch


def _sle(
    session,
    *,
    batch: models.PhysicalImportBatch,
    item_id: int,
    qty: Decimal,
    posting_at: datetime,
    movement_kind: str,
    recorder: str,
    line_no: str,
    warehouse: str = "WH",
) -> models.StockLedgerEntry:
    row = models.StockLedgerEntry(
        ingest_batch_id=int(batch.id),
        source_content_hash=f"{recorder}-{line_no}".ljust(64, "0")[:64],
        item_id=int(item_id), characteristic_ref="", organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c=warehouse, qty=qty, posting_at=posting_at,
        record_type="Receipt" if qty > 0 else "Expense",
        movement_kind=movement_kind,
        recorder_type="Document_СборкаЗапасов", recorder_ref=recorder,
        line_no=line_no, ingest_source="seed",
    )
    session.add(row)
    session.flush()
    return row


def _owner(
    session,
    *,
    generation: models.LedgerGeneration,
    freeze_batch: models.PhysicalImportBatch,
    item: models.Item,
    required: Decimal,
    covered: Decimal = Decimal("0"),
    mode: str = "make",
    lifecycle: str = "active",
    period_from: date = date(2026, 8, 1),
    period_to: date = date(2026, 8, 31),
) -> models.ReservationEntry:
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT", ledger_generation_id=generation.id,
        config_snapshot={}, active_freeze_version=1, ledger_cutoff=CUTOFF,
    )
    session.add(run)
    session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id, item_id=item.item_id,
        total_required_qty=required, net_required_qty=required,
        period_from=period_from, period_to=period_to, bom_level=0,
        planning_stock_pool="default", characteristic_ref="",
        organization_ref="", freeze_version=1,
    )
    session.add(requirement)
    session.flush()
    session.add(models.MrpFreezeBaseline(
        run_id=run.run_id, freeze_version=1, item_id=item.item_id,
        characteristic_ref="", organization_ref="", planning_stock_pool="default",
        stock_qty=Decimal("0"),
        physical_import_batch_id=int(freeze_batch.id), baseline_at=FREEZE_AT,
    ))
    owner = models.ReservationEntry(
        ledger_generation_id=generation.id, item_id=item.item_id,
        run_id=run.run_id, freeze_version=1, requirement_id=requirement.id,
        priority_period_from=period_from, priority_period_to=period_to,
        realization_mode=mode, reserved_qty=required + covered,
        replenishment_required_qty=required,
        covered_from_stock_at_freeze_qty=covered,
        lifecycle_status=lifecycle, owner_kind="current", is_current=True,
        current_identity=f"reservation:req:{int(requirement.id)}:mode:{mode}",
    )
    session.add(owner)
    session.flush()
    return owner


def _world(engine):
    """One MAKE item: a pre-freeze output, a netted transfer and a real output.

    ``required`` is 10; only the 6 units produced after the freeze boundary by a
    document whose net output is positive may be credited.
    """
    with Session(engine) as session:
        freeze_batch = _batch(session, "make-bootstrap-freeze", FREEZE_AT)
        current_batch = _batch(session, "make-bootstrap-current", CUTOFF)
        generation = models.LedgerGeneration(
            generation_key="make-bootstrap-pointer", status="accepted", cutoff=CUTOFF,
            accepted_at=CUTOFF, source_watermarks={}, capabilities={},
            physical_import_batch_id=int(current_batch.id),
            algorithm_version="make-bootstrap-tests",
        )
        item = models.Item(item_code="MAKE-1", item_name="Assembled node")
        session.add_all([generation, item])
        session.flush()
        session.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
        session.flush()

        # Known at freeze (§49/§51): imported by the freeze batch and dated
        # before its instant.  Decision §58: it is this owner's frozen stock
        # only up to what the owner actually covered from stock.
        pre_freeze = _sle(
            session, batch=freeze_batch, item_id=item.item_id, qty=Decimal("4"),
            posting_at=FREEZE_AT - timedelta(days=2), movement_kind="assembly_in",
            recorder="doc-before-freeze", line_no="1",
        )
        # A document whose own issue cancels its receipt: internal transport of
        # one 1C document, physical truth but no production output.
        _sle(
            session, batch=current_batch, item_id=item.item_id, qty=Decimal("5"),
            posting_at=CUTOFF - timedelta(days=3), movement_kind="assembly_in",
            recorder="doc-internal", line_no="1",
        )
        _sle(
            session, batch=current_batch, item_id=item.item_id, qty=Decimal("-5"),
            posting_at=CUTOFF - timedelta(days=3), movement_kind="assembly_out",
            recorder="doc-internal", line_no="2",
        )
        # The real output: 6 units produced after the freeze boundary.
        output = _sle(
            session, batch=current_batch, item_id=item.item_id, qty=Decimal("6"),
            posting_at=CUTOFF - timedelta(days=1), movement_kind="assembly_in",
            recorder="doc-output", line_no="1",
        )
        owner = _owner(
            session, generation=generation, freeze_batch=freeze_batch, item=item,
            required=Decimal("10"),
        )
        session.commit()
        return {
            "generation_id": int(generation.id),
            "item_id": int(item.item_id),
            "owner_id": int(owner.id),
            "output_sle_id": int(output.id),
            "pre_freeze_sle_id": int(pre_freeze.id),
        }


def _owner_state(engine, owner_id: int) -> tuple[Decimal, list[tuple[int, Decimal]]]:
    with Session(engine) as session:
        owner = session.get(models.ReservationEntry, int(owner_id))
        rows = session.query(models.ReservationConsumptionAllocation).filter(
            models.ReservationConsumptionAllocation.reservation_id == int(owner_id),
            models.ReservationConsumptionAllocation.is_current.is_(True),
            models.ReservationConsumptionAllocation.allocation_role
            == "replenishment_receipt",
        ).order_by(models.ReservationConsumptionAllocation.sle_id).all()
        return (
            Decimal(str(owner.replenishment_received_qty)),
            [(int(row.sle_id), Decimal(str(row.allocated_qty))) for row in rows],
        )


def test_phase_requires_writers_stopped():
    engine = _engine()
    _world(engine)
    with pytest.raises(PreflightBlocked, match="writers-stopped"):
        apply_replenishment_rebase(engine, writers_stopped=False)


def _mixed_zero_net_and_freeze_world(engine, *, foreign_allocation: bool = False):
    """Old gross credits: one transport leg and one frozen real output."""
    with Session(engine) as session:
        freeze_batch = _batch(session, "mixed-basis-freeze", FREEZE_AT)
        current_batch = _batch(session, "mixed-basis-current", CUTOFF)
        generation = models.LedgerGeneration(
            generation_key="mixed-basis-pointer", status="accepted", cutoff=CUTOFF,
            accepted_at=CUTOFF, source_watermarks={}, capabilities={},
            physical_import_batch_id=int(current_batch.id),
            algorithm_version="mixed-basis-tests",
        )
        item = models.Item(item_code="MIXED-BASIS", item_name="Mixed output")
        session.add_all((generation, item))
        session.flush()
        session.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
        posted = FREEZE_AT - timedelta(days=2)
        transport = _sle(
            session, batch=freeze_batch, item_id=item.item_id,
            qty=Decimal("1"), posting_at=posted, movement_kind="assembly_in",
            recorder="mixed-transport", line_no="1",
        )
        _sle(
            session, batch=freeze_batch, item_id=item.item_id,
            qty=Decimal("-1"), posting_at=posted, movement_kind="assembly_out",
            recorder="mixed-transport", line_no="2",
        )
        real = _sle(
            session, batch=freeze_batch, item_id=item.item_id,
            qty=Decimal("1"), posting_at=posted, movement_kind="assembly_in",
            recorder="mixed-real", line_no="1",
        )
        owner = _owner(
            session, generation=generation, freeze_batch=freeze_batch,
            item=item, required=Decimal("10"), covered=Decimal("1"), mode="rework",
        )
        facts = [transport, real]
        if foreign_allocation:
            facts.append(_sle(
                session, batch=current_batch, item_id=item.item_id,
                qty=Decimal("1"), posting_at=CUTOFF + timedelta(days=1),
                movement_kind="assembly_in", recorder="mixed-future", line_no="1",
            ))
        for index, fact in enumerate(facts):
            session.add(models.ReservationConsumptionAllocation(
                ledger_generation_id=generation.id, reservation_id=owner.id,
                sle_id=fact.id, requirement_id=owner.requirement_id,
                allocated_qty=Decimal("1"), match_rule="fifo",
                fact_ref=fact.recorder_ref, fact_line_ref=fact.line_no,
                item_id=item.item_id, characteristic_ref="", organization_ref="",
                planning_stock_pool="default", idempotency_key=f"mixed-old-{index}",
                allocation_role="replenishment_receipt", is_current=True,
                event_at=posted,
            ))
        owner.replenishment_received_qty = Decimal(len(facts))
        session.commit()
        return generation.id, item.item_id, owner.id


def test_rebase_can_clear_mixed_zero_net_and_frozen_output_without_wiping_foreign_facts():
    engine = _engine()
    generation_id, item_id, owner_id = _mixed_zero_net_and_freeze_world(engine)
    scope = (item_id, "", "", "default", "make")
    with Session(engine) as session:
        with pytest.raises(CurrentReplenishmentError, match="complete-scope replay would delete all"):
            apply_current_replenishment_for_bounded_make_scopes(
                session, target_generation_id=generation_id, target_cutoff=CUTOFF,
                affected_scopes=(scope,), at_accepted_pointer=True,
            )
        session.rollback()
    with Session(engine) as session:
        result = apply_current_replenishment_for_bounded_make_scopes(
            session, target_generation_id=generation_id, target_cutoff=CUTOFF,
            affected_scopes=(scope,), at_accepted_pointer=True,
            basis_correction_reason="test rebase basis",
        )
        session.commit()
        assert result.results[0].deleted == 2
        assert "zero-net document output" in result.results[0].confirmed_empty_reason
    assert _owner_state(engine, owner_id) == (Decimal("0"), [])

    foreign_engine = _engine()
    generation_id, item_id, _ = _mixed_zero_net_and_freeze_world(
        foreign_engine, foreign_allocation=True,
    )
    with Session(foreign_engine) as session:
        with pytest.raises(CurrentReplenishmentError, match="complete-scope replay would delete all"):
            apply_current_replenishment_for_bounded_make_scopes(
                session, target_generation_id=generation_id, target_cutoff=CUTOFF,
                affected_scopes=((item_id, "", "", "default", "make"),),
                at_accepted_pointer=True,
                basis_correction_reason="test rebase basis",
            )


def test_bootstrap_credits_only_the_net_output_after_the_freeze_boundary():
    engine = _engine()
    world = _world(engine)
    report = apply_replenishment_rebase(
        engine, writers_stopped=True, republish=False
    )
    assert report["status"] == "ready"
    assert report["generation_id"] == world["generation_id"]
    assert report["make"]["make_scopes"] == 1
    # Three ``assembly_in`` rows are visible; one is an internal transport leg
    # of its own document and is not output at all.  The pre-freeze one is a
    # fact: decision §58 excludes it only within what the owner covered from
    # stock, and this owner covered nothing.
    assert report["make"]["assembly_output_facts"] == 2
    assert report["make"]["netted_internal_transfer_rows"] == 1
    assert report["make"]["inserted"] == 2
    assert report["over_allocated_facts_after"] == 0

    received, allocations = _owner_state(engine, world["owner_id"])
    assert received == Decimal("10")
    assert dict(allocations) == {
        world["pre_freeze_sle_id"]: Decimal("4"),
        world["output_sle_id"]: Decimal("6"),
    }

    with Session(engine) as session:
        scope = (world["item_id"], "", "", "default", "make")
        marker = session.query(models.CurrentReplenishmentState).filter(
            models.CurrentReplenishmentState.scope_key == _scope_key(scope)
        ).one()
        # §46: the state revision is the id of the publishing generation, which
        # for a one-off at the pointer is the pointer's own id - so the next
        # bounded refresh still publishes a strictly higher revision.
        assert marker.source_key == ASSEMBLY_OUTPUT_SOURCE_KEY
        assert int(marker.source_revision) == world["generation_id"]
        assert int(marker.ledger_generation_id) == world["generation_id"]
        assert marker.status == "completed"


def test_pre_freeze_fact_is_excluded_only_within_what_the_owner_covered():
    """Decision §58, both directions, on the same physical history."""
    engine = _engine()
    world = _world(engine)
    with Session(engine) as session:
        owner = session.get(models.ReservationEntry, world["owner_id"])
        # The plan took 3 of the 4 pre-freeze units off the shelf at its
        # freeze; only the remaining 1 may replenish it.
        owner.covered_from_stock_at_freeze_qty = Decimal("3")
        session.commit()
    apply_replenishment_rebase(engine, writers_stopped=True, republish=False)
    received, allocations = _owner_state(engine, world["owner_id"])
    assert received == Decimal("7")
    assert dict(allocations) == {
        world["pre_freeze_sle_id"]: Decimal("1"),
        world["output_sle_id"]: Decimal("6"),
    }

    with Session(engine) as session:
        owner = session.get(models.ReservationEntry, world["owner_id"])
        # Covering the whole pre-freeze fact from stock excludes all of it.
        owner.covered_from_stock_at_freeze_qty = Decimal("4")
        session.commit()
    apply_replenishment_rebase(engine, writers_stopped=True, republish=False)
    received, allocations = _owner_state(engine, world["owner_id"])
    assert received == Decimal("6")
    assert dict(allocations) == {world["output_sle_id"]: Decimal("6")}


def test_bootstrap_is_idempotent():
    engine = _engine()
    world = _world(engine)
    apply_replenishment_rebase(engine, writers_stopped=True, republish=False)
    with Session(engine) as session:
        marker_before = [
            (row.scope_key, row.scope_checksum, int(row.source_revision), row.updated_at)
            for row in session.query(models.CurrentReplenishmentState).all()
        ]
        audit_before = session.query(models.CurrentReplenishmentAudit).count()
    second = apply_replenishment_rebase(
        engine, writers_stopped=True, republish=False
    )
    assert second["changed_pairs"] == 0
    assert second["make"]["inserted"] == second["make"]["updated"] == second["make"]["deleted"] == 0
    assert second["idempotent"] is True
    assert second["republished_current_execution"] is None
    with Session(engine) as session:
        marker_after = [
            (row.scope_key, row.scope_checksum, int(row.source_revision), row.updated_at)
            for row in session.query(models.CurrentReplenishmentState).all()
        ]
        assert marker_after == marker_before
        assert session.query(models.CurrentReplenishmentAudit).count() == audit_before
    received, allocations = _owner_state(engine, world["owner_id"])
    assert received == Decimal("10")
    assert dict(allocations) == {
        world["pre_freeze_sle_id"]: Decimal("4"),
        world["output_sle_id"]: Decimal("6"),
    }


def test_bootstrap_settles_rework_inside_the_make_scope_and_skips_closed_owners():
    """§44: a rework owner is realized by the same fact; §42: closed owners are not."""
    engine = _engine()
    world = _world(engine)
    with Session(engine) as session:
        generation = session.get(models.LedgerGeneration, world["generation_id"])
        item = session.get(models.Item, world["item_id"])
        freeze_batch = session.query(models.PhysicalImportBatch).filter(
            models.PhysicalImportBatch.batch_key == "make-bootstrap-freeze"
        ).one()
        rework = _owner(
            session, generation=generation, freeze_batch=freeze_batch, item=item,
            required=Decimal("4"), mode="rework",
            period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
        )
        closed = _owner(
            session, generation=generation, freeze_batch=freeze_batch, item=item,
            required=Decimal("8"), lifecycle="closed",
            period_from=date(2026, 7, 1), period_to=date(2026, 7, 31),
        )
        session.commit()
        rework_id, closed_id = int(rework.id), int(closed.id)

    with Session(engine) as session:
        # One MAKE scope for the item, even with a rework owner beside it.
        assert all_current_make_scopes(session) == (
            (world["item_id"], "", "", "default", "make"),
        )

    apply_replenishment_rebase(engine, writers_stopped=True, republish=False)
    # The oldest live owner takes all 10 creditable units FIFO (§58 lets the
    # pre-freeze fact in: it covered nothing from stock); the closed owner
    # takes none, neither as coverage nor as replenishment (§42).
    assert _owner_state(engine, world["owner_id"])[0] == Decimal("10")
    assert _owner_state(engine, rework_id) == (Decimal("0"), [])
    assert _owner_state(engine, closed_id) == (Decimal("0"), [])

    # With the live make owner's demand satisfied, the surplus reaches the
    # rework owner in the same scope instead of staying unrealized.
    with Session(engine) as session:
        batch = session.query(models.PhysicalImportBatch).filter(
            models.PhysicalImportBatch.batch_key == "make-bootstrap-current"
        ).one()
        _sle(
            session, batch=batch, item_id=world["item_id"], qty=Decimal("9"),
            posting_at=CUTOFF - timedelta(hours=1), movement_kind="assembly_in",
            recorder="doc-output-2", line_no="1",
        )
        session.commit()
    apply_replenishment_rebase(engine, writers_stopped=True, republish=False)
    assert _owner_state(engine, world["owner_id"])[0] == Decimal("10")
    assert _owner_state(engine, rework_id)[0] == Decimal("4")
    assert _owner_state(engine, closed_id) == (Decimal("0"), [])


def test_bootstrap_refuses_a_pointer_that_is_not_the_accepted_generation():
    engine = _engine()
    _world(engine)
    with Session(engine) as session:
        pointer = session.get(models.PlanningTruthState, 1)
        pointer.current_generation_id = None
        session.commit()
    with pytest.raises((PreflightBlocked, RuntimeError)):
        apply_replenishment_rebase(
            engine, writers_stopped=True, republish=False
        )


def test_make_writer_at_the_pointer_refuses_a_generation_that_is_not_the_pointer():
    engine = _engine()
    world = _world(engine)
    with Session(engine) as session:
        other = models.LedgerGeneration(
            generation_key="make-bootstrap-other", status="accepted", cutoff=CUTOFF,
            accepted_at=CUTOFF, source_watermarks={}, capabilities={},
            physical_import_batch_id=session.query(
                models.PhysicalImportBatch.id
            ).filter(
                models.PhysicalImportBatch.batch_key == "make-bootstrap-current"
            ).scalar(),
            algorithm_version="make-bootstrap-tests",
        )
        session.add(other)
        session.commit()
        with pytest.raises(CurrentReplenishmentError, match="exact planning truth pointer"):
            apply_current_replenishment_for_bounded_make_scopes(
                session,
                target_generation_id=int(other.id),
                target_cutoff=CUTOFF,
                affected_scopes=((world["item_id"], "", "", "default", "make"),),
                at_accepted_pointer=True,
            )


def test_same_revision_payload_drift_needs_an_explicit_basis_correction():
    """The runtime guard stays; only an acknowledged repair may rewrite a scope."""
    engine = _engine()
    world = _world(engine)
    apply_replenishment_rebase(engine, writers_stopped=True, republish=False)
    with Session(engine) as session:
        batch = session.query(models.PhysicalImportBatch).filter(
            models.PhysicalImportBatch.batch_key == "make-bootstrap-current"
        ).one()
        _sle(
            session, batch=batch, item_id=world["item_id"], qty=Decimal("2"),
            posting_at=CUTOFF - timedelta(hours=2), movement_kind="assembly_in",
            recorder="doc-output-3", line_no="1",
        )
        owner = session.get(models.ReservationEntry, world["owner_id"])
        owner.replenishment_required_qty = Decimal("12")
        session.commit()
    scope = (world["item_id"], "", "", "default", "make")
    with Session(engine) as session:
        with pytest.raises(CurrentReplenishmentError, match="payload drift"):
            apply_current_replenishment_for_bounded_make_scopes(
                session,
                target_generation_id=world["generation_id"],
                target_cutoff=CUTOFF,
                affected_scopes=(scope,),
                at_accepted_pointer=True,
            )
        session.rollback()
    with Session(engine) as session:
        apply_current_replenishment_for_bounded_make_scopes(
            session,
            target_generation_id=world["generation_id"],
            target_cutoff=CUTOFF,
            affected_scopes=(scope,),
            at_accepted_pointer=True,
            basis_correction_reason="test basis correction",
        )
        session.commit()
        reasons = {
            str(row.reason)
            for row in session.query(models.CurrentReplenishmentAudit).all()
        }
        assert BASIS_CORRECTED_REASON in reasons
    assert _owner_state(engine, world["owner_id"])[0] == Decimal("12")

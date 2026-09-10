"""R4 acceptance contract for the transactional current replenishment writer.

These tests deliberately exercise the existing reservation allocation tables.  A
current application is not allowed to create a snapshot or a new ledger
generation just to make a changed assignment visible.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app import models
from app.services.item_ledger.historical_replay_core import Fact, Reserve
from app.services.item_ledger.current_replenishment import (
    CurrentReplenishmentError,
    apply_current_replenishment,
    read_current_replenishment,
)


def _world(db, *, prefix=None):
    token = f"{prefix}-{uuid4().hex[:12]}" if prefix else uuid4().hex[:12]
    physical = models.PhysicalImportBatch(
        batch_key=f"r4-current-physical-{token}",
        status="completed",
        cutoff=datetime(2026, 9, 10, tzinfo=timezone.utc),
        source_watermarks={},
        completed_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
    )
    db.add(physical)
    db.flush()
    generation = models.LedgerGeneration(
        generation_key=f"r4-current-generation-{token}",
        status="accepted",
        cutoff=physical.cutoff,
        accepted_at=physical.cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True, "reservation_replay": True},
        physical_import_batch=physical,
        algorithm_version="r4-tests",
    )
    item = models.Item(item_code=f"R4-ITEM-{token}", item_name="R4 item")
    db.add_all([generation, item])
    db.flush()
    requirements = []
    reservations = []
    for quantity in ("8", "7"):
        run = models.PlanningRun(
            status="BUILDING_SNAPSHOT",
            config_snapshot={},
            ledger_generation_id=generation.id,
            ledger_cutoff=generation.cutoff,
            source_plan_id=None,
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 30),
            active_freeze_version=1,
        )
        db.add(run)
        db.flush()
        requirement = models.MrpRequirement(
            run_id=run.run_id,
            item_id=item.item_id,
            total_required_qty=Decimal(quantity),
            net_required_qty=Decimal(quantity),
            period_from=date(2026, 9, 1),
            period_to=date(2026, 9, 30),
            bom_level=0,
            planning_stock_pool="default",
            characteristic_ref="",
            organization_ref="",
            freeze_version=1,
        )
        db.add(requirement)
        db.flush()
        entry = models.ReservationEntry(
            ledger_generation_id=generation.id,
            item_id=item.item_id,
            run_id=run.run_id,
            freeze_version=1,
            requirement_id=requirement.id,
            priority_period_from=date(2026, 9, 1),
            priority_period_to=date(2026, 9, 30),
            realization_mode="buy",
            reserved_qty=Decimal(quantity),
            replenishment_required_qty=Decimal(quantity),
            lifecycle_status="active",
        )
        db.add(entry)
        db.flush()
        requirements.append(requirement)
        reservations.append(entry)
    facts = []
    for ref, quantity, requirement_id in (("A", "8", None), ("B", "2", requirements[1].id)):
        sle = models.StockLedgerEntry(
            ingest_batch_id=physical.id,
            source_content_hash=("r4-" + ref).ljust(64, "0"),
            business_identity=f"r4:{ref}",
            item_id=item.item_id,
            qty=Decimal(quantity),
            qty_after=Decimal(quantity),
            posting_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
            record_type="Receipt",
            movement_kind="receipt",
            recorder_type="Doc",
            recorder_ref=ref,
            line_no="1",
            ingest_source="seed",
        )
        db.add(sle)
        db.flush()
        facts.append(
            Fact(
                fact_id=str(sle.id),
                item_id=item.item_id,
                mode="buy",
                qty=Decimal(quantity),
                posting_at=sle.posting_at,
                requirement_id=requirement_id,
            )
        )
    db.commit()
    return generation.id, item.item_id, reservations, facts


def _reserves(reservations):
    return tuple(
        Reserve(
            reserve_id=str(row.id),
            item_id=int(row.item_id),
            mode="buy",
            reserved_qty=Decimal(str(row.reserved_qty)),
            due_date=row.priority_period_to,
            plan_period_from=row.priority_period_from,
            plan_period_to=row.priority_period_to,
            run_id=int(row.run_id),
            requirement_id=int(row.requirement_id),
        )
        for row in reservations
    )


def test_current_application_requires_explicit_complete_scope(db_session):
    generation_id, _item_id, reservations, facts = _world(db_session)
    with pytest.raises(CurrentReplenishmentError, match="complete scope"):
        apply_current_replenishment(
            db_session,
            generation_id=generation_id,
            source_key="physical:r4",
            source_revision=1,
            facts=facts,
            reserves=_reserves(reservations),
            complete_scope=False,
        )
    assert db_session.query(models.ReservationConsumptionAllocation).count() == 0


def test_current_application_is_idempotent_and_keeps_assignment_ids(db_session):
    generation_id, item_id, reservations, facts = _world(db_session)
    reserves = _reserves(reservations)
    first = apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:r4",
        source_revision=1,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()
    before = {
        (row.sle_id, row.reservation_id): row.id
        for row in db_session.query(models.ReservationConsumptionAllocation).all()
    }
    second = apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:r4",
        source_revision=1,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()
    after = {
        (row.sle_id, row.reservation_id): row.id
        for row in db_session.query(models.ReservationConsumptionAllocation).all()
    }
    assert first.changed_pairs == 2
    assert second.changed_pairs == 0
    assert after == before
    assert db_session.query(models.LedgerGeneration).count() == 1
    assert read_current_replenishment(db_session, generation_id=generation_id, item_id=item_id)


def test_incomplete_failure_does_not_clear_foreign_pool_or_publish_partial_rows(db_session):
    generation_id, _item_id, reservations, facts = _world(db_session)
    reserves = _reserves(reservations)
    with pytest.raises(CurrentReplenishmentError, match="complete scope"):
        apply_current_replenishment(
            db_session,
            generation_id=generation_id,
            source_key="physical:r4",
            source_revision=2,
            facts=facts[:1],
            reserves=reserves[:1],
            complete_scope=False,
        )
    db_session.rollback()
    assert db_session.query(models.ReservationConsumptionAllocation).count() == 0


def test_failure_after_assignment_rolls_back_marker_execution_and_rows(db_session):
    generation_id, _item_id, reservations, facts = _world(db_session)
    with pytest.raises(CurrentReplenishmentError, match="injected"):
        apply_current_replenishment(
            db_session,
            generation_id=generation_id,
            source_key="physical:r4",
            source_revision=1,
            facts=facts,
            reserves=_reserves(reservations),
            complete_scope=True,
            fail_after="assignments",
        )
    db_session.rollback()
    generation = db_session.get(models.LedgerGeneration, generation_id)
    assert db_session.query(models.ReservationConsumptionAllocation).count() == 0
    assert "current_replenishment" not in (generation.source_watermarks or {})
    assert db_session.query(models.CurrentReplenishmentState).count() == 0


def test_new_revision_updates_only_changed_pair_and_old_revision_is_rejected(db_session):
    generation_id, _item_id, reservations, facts = _world(db_session)
    reserves = _reserves(reservations)
    apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:r4",
        source_revision=1,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()
    before = {
        (row.sle_id, row.reservation_id): (row.id, Decimal(str(row.allocated_qty)))
        for row in db_session.query(models.ReservationConsumptionAllocation).all()
    }
    changed = [
        Fact(
            fact_id=facts[0].fact_id,
            item_id=facts[0].item_id,
            mode=facts[0].mode,
            qty=Decimal("6"),
            posting_at=facts[0].posting_at,
            requirement_id=facts[0].requirement_id,
        ),
        facts[1],
    ]
    result = apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:r4",
        source_revision=2,
        facts=changed,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()
    after = {
        (row.sle_id, row.reservation_id): (row.id, Decimal(str(row.allocated_qty)))
        for row in db_session.query(models.ReservationConsumptionAllocation).all()
    }
    assert result.updated == 1
    assert result.inserted == result.deleted == 0
    assert after[(int(facts[0].fact_id), reservations[0].id)][0] == before[(int(facts[0].fact_id), reservations[0].id)][0]
    assert after[(int(facts[0].fact_id), reservations[0].id)][1] == Decimal("6")
    assert after[(int(facts[1].fact_id), reservations[1].id)] == before[(int(facts[1].fact_id), reservations[1].id)]
    with pytest.raises(CurrentReplenishmentError, match="stale"):
        apply_current_replenishment(
            db_session,
            generation_id=generation_id,
            source_key="physical:r4",
            source_revision=1,
            facts=changed,
            reserves=reserves,
            complete_scope=True,
        )


@pytest.mark.parametrize("failure", ["execution", "marker"])
def test_each_late_boundary_rolls_back_all_current_state(db_session, failure):
    generation_id, _item_id, reservations, facts = _world(db_session)
    generation_before = dict(db_session.get(models.LedgerGeneration, generation_id).source_watermarks or {})
    with pytest.raises(CurrentReplenishmentError, match="injected"):
        apply_current_replenishment(
            db_session,
            generation_id=generation_id,
            source_key="physical:r4",
            source_revision=1,
            facts=facts,
            reserves=_reserves(reservations),
            complete_scope=True,
            fail_after=failure,
        )
    db_session.rollback()
    assert db_session.query(models.ReservationConsumptionAllocation).count() == 0
    assert db_session.query(models.ReservationEvent).count() == 0
    assert db_session.query(models.CurrentReplenishmentAudit).count() == 0
    assert db_session.query(models.CurrentReplenishmentState).count() == 0
    assert db_session.get(models.LedgerGeneration, generation_id).source_watermarks == generation_before


def test_exact_retry_emits_no_assignment_execution_or_audit_dml(db_session):
    generation_id, _item_id, reservations, facts = _world(db_session)
    reserves = _reserves(reservations)
    apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:r4",
        source_revision=1,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()
    statements = []
    from sqlalchemy import event

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        upper = statement.upper()
        if any(name in upper for name in ("RESERVATION_CONSUMPTION_ALLOCATION", "RESERVATION_EVENT", "RESERVATION_ENTRY")):
            statements.append(upper.split()[0])

    event.listen(db_session.bind, "before_cursor_execute", observe)
    try:
        result = apply_current_replenishment(
            db_session,
            generation_id=generation_id,
            source_key="physical:r4",
            source_revision=1,
            facts=facts,
            reserves=reserves,
            complete_scope=True,
        )
        db_session.commit()
    finally:
        event.remove(db_session.bind, "before_cursor_execute", observe)
    assert result.idempotent is True
    assert not {statement for statement in statements if statement in {"INSERT", "UPDATE", "DELETE"}}


def test_legacy_current_writer_is_rejected_and_separate_reader_sees_one_state(db_session):
    generation_id, item_id, reservations, facts = _world(db_session)
    with pytest.raises(CurrentReplenishmentError, match="single current writer"):
        apply_current_replenishment(
            db_session,
            generation_id=generation_id,
            source_key="physical:r4",
            source_revision=1,
            facts=facts,
            reserves=_reserves(reservations),
            complete_scope=True,
            writer="legacy",
        )
    apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:r4",
        source_revision=1,
        facts=facts,
        reserves=_reserves(reservations),
        complete_scope=True,
    )
    db_session.commit()
    separate = db_session.connection().engine
    from sqlalchemy.orm import Session

    other = Session(separate)
    try:
        assert len(read_current_replenishment(other, generation_id=generation_id, item_id=item_id)) == 2
        assert other.query(models.CurrentReplenishmentState).count() == 1
    finally:
        other.close()

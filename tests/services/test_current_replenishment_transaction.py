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
    apply_current_replenishment_for_accepted_generation,
    apply_current_receipt_replay,
    reject_legacy_supplier_receipt_writer,
    read_current_replenishment,
)
from app.services.item_ledger import physical_visibility
from app.services.item_ledger.supplier_receipt_allocation import ReceiptFact


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
            planning_stock_pool="selected",
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
            business_identity=f"r4:{token}:{ref}",
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


def _add_supplier_provenance(db, generation_id, facts, *, prefix):
    for fact in facts:
        db.add(
            models.StockLedgerSupplierReceiptProvenance(
                ledger_generation_id=int(generation_id),
                stock_ledger_entry_id=int(fact.fact_id),
                receipt_doc_type="Doc",
                receipt_doc_ref=f"{prefix}-{fact.fact_id}",
                receipt_doc_line_no="1",
                supplier_order_ref="",
                supplier_order_line_no="",
                operation_kind="supplier_receipt",
                evidence_hash=(f"{prefix}-{fact.fact_id}").ljust(64, "0")[:64],
                evidence_payload={"item_id": int(fact.item_id)},
                match_rule="fifo",
                match_status="unmatched",
                ambiguity_count=0,
                reason="test FIFO supplier receipt",
            )
        )
    db.flush()


def _mark_reservations_current(db, reservations):
    for row in reservations:
        row.current_identity = (
            f"reservation:req:{int(row.requirement_id)}:mode:buy"
        )
        row.owner_kind = "current"
        row.is_current = True
    db.flush()


def _building_rebind_target(
    db,
    *,
    parent_generation_id,
    reservations,
    quantities,
    physical_batch=None,
    sealed_parent_generation_id=None,
):
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    target_batch = physical_batch or parent.physical_import_batch
    target = models.LedgerGeneration(
        generation_key=f"r4-rebind-target-{uuid4().hex[:12]}",
        status="building",
        cutoff=(parent.cutoff if physical_batch is None else target_batch.cutoff),
        source_watermarks={
            "parent_generation_id": int(
                sealed_parent_generation_id
                if sealed_parent_generation_id is not None
                else parent.id
            ),
            "generation_kind": "obligation_refresh",
        },
        capabilities={"physical_ledger": True, "reservation_replay": True},
        physical_import_batch=target_batch,
        algorithm_version="r4-tests",
    )
    db.add(target)
    db.flush()
    target_reservations = []
    for previous, quantity in zip(reservations, quantities, strict=True):
        row = models.ReservationEntry(
            ledger_generation_id=int(target.id),
            item_id=int(previous.item_id),
            run_id=int(previous.run_id),
            freeze_version=int(previous.freeze_version),
            requirement_id=int(previous.requirement_id),
            priority_period_from=previous.priority_period_from,
            priority_period_to=previous.priority_period_to,
            realization_mode="buy",
            reserved_qty=Decimal(quantity),
            replenishment_required_qty=Decimal(quantity),
            lifecycle_status="active",
            current_identity=(
                f"reservation:req:{int(previous.requirement_id)}:mode:buy"
            ),
            owner_kind="building",
            is_current=False,
        )
        db.add(row)
        target_reservations.append(row)
    db.flush()
    return target, target_reservations


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
    assert db_session.query(models.CurrentExecutionScope).count() == 0
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


def test_same_revision_with_changed_payload_fails_closed(db_session):
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
    changed = list(facts)
    changed[0] = Fact(
        fact_id=facts[0].fact_id,
        item_id=facts[0].item_id,
        mode=facts[0].mode,
        qty=Decimal("6"),
        posting_at=facts[0].posting_at,
        requirement_id=facts[0].requirement_id,
    )
    with pytest.raises(CurrentReplenishmentError, match="payload drift"):
        apply_current_replenishment(
            db_session,
            generation_id=generation_id,
            source_key="physical:r4",
            source_revision=1,
            facts=changed,
            reserves=reserves,
            complete_scope=True,
        )


def test_fact_only_generation_advance_keeps_current_assignment_ids(db_session):
    generation_id, item_id, reservations, facts = _world(db_session)
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
        (row.sle_id, row.reservation_id): row.id
        for row in db_session.query(models.ReservationConsumptionAllocation)
        .filter(models.ReservationConsumptionAllocation.is_current.is_(True))
    }
    physical = models.PhysicalImportBatch(
        batch_key="r4-fact-only-" + uuid4().hex[:12],
        status="completed",
        cutoff=datetime(2026, 9, 11, tzinfo=timezone.utc),
        source_watermarks={"fact_only": True},
    )
    next_generation = models.LedgerGeneration(
        generation_key="r4-fact-only-" + uuid4().hex[:12],
        status="accepted",
        cutoff=physical.cutoff,
        accepted_at=physical.cutoff,
        source_watermarks={"fact_only": True},
        capabilities={"physical_ledger": True, "reservation_replay": True},
        physical_import_batch=physical,
        algorithm_version="r4-tests",
    )
    db_session.add_all([physical, next_generation])
    db_session.flush()
    changed = list(facts)
    changed[0] = Fact(
        fact_id=facts[0].fact_id,
        item_id=facts[0].item_id,
        mode=facts[0].mode,
        qty=Decimal("6"),
        posting_at=facts[0].posting_at,
        requirement_id=facts[0].requirement_id,
    )
    result = apply_current_replenishment(
        db_session,
        generation_id=next_generation.id,
        source_key="physical:r4",
        source_revision=2,
        facts=changed,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()
    after = {
        (row.sle_id, row.reservation_id): row.id
        for row in db_session.query(models.ReservationConsumptionAllocation)
        .filter(models.ReservationConsumptionAllocation.is_current.is_(True))
    }
    assert result.updated == 1
    assert after == before
    assert len(read_current_replenishment(db_session, generation_id=next_generation.id, item_id=item_id)) == 2


def test_complete_scope_locks_only_one_distribution_pool(db_session):
    generation_id, _item_id, reservations, facts = _world(db_session)
    reserves = _reserves(reservations)
    apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:seed",
        source_revision=1,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()
    foreign = db_session.get(models.ReservationEntry, reservations[1].id)
    foreign.planning_stock_pool = "foreign"
    foreign_reserve = Reserve(
        reserve_id=reserves[1].reserve_id,
        item_id=reserves[1].item_id,
        mode=reserves[1].mode,
        reserved_qty=reserves[1].reserved_qty,
        due_date=reserves[1].due_date,
        plan_period_from=reserves[1].plan_period_from,
        plan_period_to=reserves[1].plan_period_to,
        run_id=reserves[1].run_id,
        requirement_id=reserves[1].requirement_id,
        planning_stock_pool="foreign",
    )
    reserves = reserves[:1] + (foreign_reserve,)
    foreign_allocation = db_session.query(models.ReservationConsumptionAllocation).filter_by(
        reservation_id=reservations[1].id, is_current=True
    ).one()
    foreign_allocation.planning_stock_pool = "foreign"
    db_session.commit()
    result = apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:seed",
        source_revision=2,
        facts=facts[:1],
        reserves=reserves[:1],
        complete_scope=True,
    )
    db_session.commit()
    assert result.changed_pairs == 0
    assert db_session.get(models.ReservationConsumptionAllocation, foreign_allocation.id) is not None
    assert db_session.get(models.ReservationConsumptionAllocation, foreign_allocation.id).planning_stock_pool == "foreign"


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


def test_scope_identity_is_canonical_and_source_stream_cannot_bypass_revision(db_session):
    generation_id, _item_id, reservations, facts = _world(db_session)
    reserves = _reserves(reservations)
    apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:stream-a",
        source_revision=4,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()
    with pytest.raises(CurrentReplenishmentError, match="source stream|revision"):
        apply_current_replenishment(
            db_session,
            generation_id=generation_id,
            source_key="physical:stream-b",
            source_revision=1,
            facts=facts,
            reserves=reserves,
            complete_scope=True,
        )


def test_verified_empty_scope_clears_only_that_distribution_scope(db_session):
    generation_id, item_id, reservations, facts = _world(db_session)
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
    foreign = db_session.get(models.ReservationEntry, reservations[1].id)
    foreign.planning_stock_pool = "foreign"
    foreign_row = db_session.query(models.ReservationConsumptionAllocation).filter_by(
        reservation_id=reservations[1].id, is_current=True
    ).one()
    foreign_row.planning_stock_pool = "foreign"
    db_session.commit()
    with pytest.raises(CurrentReplenishmentError, match="distribution_scope"):
        apply_current_replenishment(
            db_session,
            generation_id=generation_id,
            source_key="physical:r4",
            source_revision=2,
            facts=(),
            reserves=(),
            complete_scope=True,
        )
    result = apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:r4",
        source_revision=2,
        facts=(),
        reserves=(),
        distribution_scope=(item_id, "", "", "selected", "buy"),
        complete_scope=True,
    )
    db_session.commit()
    assert result.deleted == 1
    assert db_session.get(models.ReservationConsumptionAllocation, foreign_row.id) is not None
    assert db_session.get(models.ReservationConsumptionAllocation, foreign_row.id).is_current is True


def test_postgresql_visibility_is_atomic_across_current_state_and_execution():
    dsn = __import__("os").environ.get("PRODPLAN_R2_TEST_DSN")
    if not dsn:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    pytest.importorskip("psycopg2")
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from app.r2_local_contract import validate_r2_dsn

    validate_r2_dsn(dsn)
    engine = sa.create_engine(dsn, poolclass=sa.pool.NullPool)
    writer = Session(engine)
    reader = Session(engine)
    try:
        generation_id, item_id, reservations, facts = _world(writer, prefix="pg-visible")
        reserves = _reserves(reservations)
        apply_current_replenishment(
            writer,
            generation_id=generation_id,
            source_key="physical:visible",
            source_revision=1,
            facts=facts,
            reserves=reserves,
            complete_scope=True,
        )
        writer.commit()
        reader.expire_all()
        before = read_current_replenishment(reader, generation_id=generation_id, item_id=item_id)
        changed = list(facts)
        changed[0] = Fact(**{**facts[0].__dict__, "qty": Decimal("6")})
        apply_current_replenishment(
            writer,
            generation_id=generation_id,
            source_key="physical:visible",
            source_revision=2,
            facts=changed,
            reserves=reserves,
            complete_scope=True,
        )
        still_old = read_current_replenishment(reader, generation_id=generation_id, item_id=item_id)
        assert [(r["allocated_qty"], r["id"]) for r in still_old] == [
            (r["allocated_qty"], r["id"]) for r in before
        ]
        writer.commit()
        reader.expire_all()
        after = read_current_replenishment(reader, generation_id=generation_id, item_id=item_id)
        assert after[0]["allocated_qty"] == Decimal("6")
        assert (
            reader.query(models.CurrentReplenishmentState)
            .filter_by(ledger_generation_id=generation_id, source_revision=2)
            .count()
            == 1
        )
    finally:
        if "facts" in locals():
            writer.execute(
                sa.text(
                    "UPDATE stock_ledger_entry SET active = false WHERE id = ANY(:ids)"
                ),
                {"ids": [int(fact.fact_id) for fact in facts]},
            )
            writer.commit()
        writer.rollback()
        writer.close()
        reader.close()
        engine.dispose()


def test_accepted_physical_publication_uses_current_writer_and_retires_supplier_events(
    db_session,
):
    generation_id, item_id, reservations, _facts = _world(db_session, prefix="adapter")
    for fact in _facts:
        db_session.add(
            models.StockLedgerSupplierReceiptProvenance(
                ledger_generation_id=generation_id,
                stock_ledger_entry_id=int(fact.fact_id),
                receipt_doc_type="Doc",
                receipt_doc_ref=f"receipt-{fact.fact_id}",
                receipt_doc_line_no="1",
                supplier_order_ref="order-r4",
                supplier_order_line_no="1",
                operation_kind="supplier_receipt",
                evidence_hash=("a" * 64),
                evidence_payload={"supplier_order_ref": "order-r4"},
                match_rule="exact",
                match_status="exact",
                ambiguity_count=0,
            )
        )
    db_session.flush()
    physical_revision = int(
        db_session.get(models.LedgerGeneration, generation_id).physical_import_batch_id
    )
    results = apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=generation_id, source_revision=physical_revision
    )
    db_session.commit()
    assert results and results[0].inserted == 2
    assert {
        row.allocation_role
        for row in db_session.query(models.ReservationConsumptionAllocation).all()
    } == {"replenishment_receipt"}
    assert db_session.query(models.ReservationEvent).count() == 0
    with pytest.raises(CurrentReplenishmentError, match="ReservationEvent writer"):
        reject_legacy_supplier_receipt_writer(
            db_session, db_session.get(models.ReservationEntry, reservations[0].id)
        )
    assert read_current_replenishment(
        db_session, generation_id=generation_id, item_id=item_id
    )


def test_accepted_adapter_reuses_one_validated_visible_prefix_for_all_scopes(
    db_session,
    monkeypatch,
):
    generation_id, item_id, reservations, facts = _world(
        db_session, prefix="adapter-prefix-once"
    )
    generation = db_session.get(models.LedgerGeneration, generation_id)
    _add_supplier_provenance(
        db_session, generation_id, facts, prefix="adapter-prefix-first"
    )

    second_item = models.Item(
        item_code=f"R4-SECOND-{uuid4().hex[:12]}",
        item_name="R4 second item",
    )
    second_run = models.PlanningRun(
        status="BUILDING_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation_id,
        ledger_cutoff=generation.cutoff,
        source_plan_id=None,
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        active_freeze_version=1,
    )
    db_session.add_all([second_item, second_run])
    db_session.flush()
    second_requirement = models.MrpRequirement(
        run_id=int(second_run.run_id),
        item_id=int(second_item.item_id),
        total_required_qty=Decimal("3"),
        net_required_qty=Decimal("3"),
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        bom_level=0,
        planning_stock_pool="selected",
        characteristic_ref="",
        organization_ref="",
        freeze_version=1,
    )
    db_session.add(second_requirement)
    db_session.flush()
    second_reservation = models.ReservationEntry(
        ledger_generation_id=generation_id,
        item_id=int(second_item.item_id),
        run_id=int(second_run.run_id),
        freeze_version=1,
        requirement_id=int(second_requirement.id),
        priority_period_from=date(2026, 9, 1),
        priority_period_to=date(2026, 9, 30),
        realization_mode="buy",
        reserved_qty=Decimal("3"),
        replenishment_required_qty=Decimal("3"),
        lifecycle_status="active",
    )
    second_sle = models.StockLedgerEntry(
        ingest_batch_id=int(generation.physical_import_batch_id),
        source_content_hash="adapter-prefix-second".ljust(64, "0"),
        business_identity=f"r4:{uuid4().hex}:second",
        item_id=int(second_item.item_id),
        qty=Decimal("3"),
        qty_after=Decimal("3"),
        posting_at=generation.cutoff,
        record_type="Receipt",
        movement_kind="receipt",
        recorder_type="Doc",
        recorder_ref="SECOND",
        line_no="1",
        ingest_source="seed",
    )
    db_session.add_all([second_reservation, second_sle])
    db_session.flush()
    second_fact = Fact(
        fact_id=str(second_sle.id),
        item_id=int(second_item.item_id),
        mode="buy",
        qty=Decimal("3"),
        posting_at=second_sle.posting_at,
        requirement_id=int(second_requirement.id),
    )
    _add_supplier_provenance(
        db_session,
        generation_id,
        (second_fact,),
        prefix="adapter-prefix-second",
    )

    original_visible = physical_visibility.visible_sles_for_generation
    visibility_calls = []

    def count_visible(db, requested_generation_id):
        visibility_calls.append(int(requested_generation_id))
        return original_visible(db, requested_generation_id)

    monkeypatch.setattr(
        physical_visibility,
        "visible_sles_for_generation",
        count_visible,
    )

    results = apply_current_replenishment_for_accepted_generation(
        db_session,
        generation_id=generation_id,
    )

    allocated_by_item: dict[int, Decimal] = {}
    for row in (
        db_session.query(models.ReservationConsumptionAllocation)
        .filter(models.ReservationConsumptionAllocation.is_current.is_(True))
        .all()
    ):
        row_item_id = int(row.item_id)
        allocated_by_item[row_item_id] = allocated_by_item.get(
            row_item_id, Decimal("0")
        ) + Decimal(str(row.allocated_qty))
    assert len(results) == 2
    assert visibility_calls == [generation_id]
    assert allocated_by_item[item_id] == Decimal("10")
    assert allocated_by_item[int(second_item.item_id)] == Decimal("3")
    assert reservations[0].replenishment_received_qty == Decimal("8")
    assert second_reservation.replenishment_received_qty == Decimal("3")


def test_validated_visible_prefix_still_rejects_missing_receipt(db_session):
    generation_id, item_id, _reservations, facts = _world(
        db_session, prefix="adapter-prefix-missing"
    )
    fact = facts[0]
    with pytest.raises(
        CurrentReplenishmentError,
        match="receipt correction source is unavailable in accepted Ledger",
    ):
        apply_current_receipt_replay(
            db_session,
            generation_id=generation_id,
            source_key="accepted-physical-receipts",
            source_revision=int(
                db_session.get(
                    models.LedgerGeneration, generation_id
                ).physical_import_batch_id
            ),
            receipt_facts=(
                ReceiptFact(
                    sle_id=int(fact.fact_id),
                    posting_at=fact.posting_at,
                    signed_qty=Decimal(str(fact.qty)),
                    item_id=item_id,
                    supplier_order_ref="",
                    supplier_order_line_no="",
                    receipt_ref="missing",
                    receipt_line_no="1",
                ),
            ),
            reserves=(),
            complete_scope=True,
            history_mode="as_occurred",
            validated_visible_ids=(),
        )


def test_accepted_adapter_rebinds_same_physical_revision_and_recomputes_allocations(
    db_session,
):
    generation_id, item_id, reservations, facts = _world(
        db_session, prefix="same-physical-rebind"
    )
    _add_supplier_provenance(
        db_session, generation_id, facts, prefix="parent-provenance"
    )
    physical_revision = int(
        db_session.get(models.LedgerGeneration, generation_id).physical_import_batch_id
    )
    apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=generation_id
    )
    _mark_reservations_current(db_session, reservations)
    state = db_session.query(models.CurrentReplenishmentState).one()
    previous_checksum = str(state.scope_checksum)
    target, _target_reservations = _building_rebind_target(
        db_session,
        parent_generation_id=generation_id,
        reservations=reservations,
        quantities=("5", "10"),
    )
    _add_supplier_provenance(
        db_session, target.id, facts, prefix="target-provenance"
    )

    result = apply_current_replenishment_for_accepted_generation(
        db_session,
        generation_id=int(target.id),
        allow_building=True,
    )
    db_session.flush()

    totals = {}
    for reservation_id, quantity in (
        db_session.query(
            models.ReservationConsumptionAllocation.reservation_id,
            models.ReservationConsumptionAllocation.allocated_qty,
        )
        .filter(
            models.ReservationConsumptionAllocation.is_current.is_(True),
            models.ReservationConsumptionAllocation.item_id == item_id,
        )
        .all()
    ):
        reservation_id = int(reservation_id)
        totals[reservation_id] = totals.get(reservation_id, Decimal("0")) + Decimal(
            str(quantity)
        )
    assert result[0].changed_pairs > 0
    assert totals == {
        int(reservations[0].id): Decimal("5"),
        int(reservations[1].id): Decimal("5"),
    }
    state = db_session.query(models.CurrentReplenishmentState).one()
    assert int(state.source_revision) == physical_revision
    assert int(state.ledger_generation_id) == int(target.id)
    assert str(state.scope_checksum) != previous_checksum

    audit_count = db_session.query(models.CurrentReplenishmentAudit).count()
    retry = apply_current_replenishment_for_accepted_generation(
        db_session,
        generation_id=int(target.id),
        allow_building=True,
    )
    assert retry[0].idempotent is True
    assert retry[0].changed_pairs == 0
    assert db_session.query(models.CurrentReplenishmentAudit).count() == audit_count
    _target_reservations[0].reserved_qty = Decimal("6")
    _target_reservations[0].replenishment_required_qty = Decimal("6")
    db_session.flush()
    with pytest.raises(CurrentReplenishmentError, match="payload drift"):
        apply_current_replenishment_for_accepted_generation(
            db_session,
            generation_id=int(target.id),
            allow_building=True,
        )


def test_generic_raw_facts_cannot_opt_into_same_revision_rebind(db_session):
    generation_id, item_id, reservations, facts = _world(
        db_session, prefix="raw-rebind-rejected"
    )
    _add_supplier_provenance(db_session, generation_id, facts, prefix="raw-parent")
    apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=generation_id
    )
    _mark_reservations_current(db_session, reservations)
    target, target_reservations = _building_rebind_target(
        db_session,
        parent_generation_id=generation_id,
        reservations=reservations,
        quantities=("5", "10"),
    )
    revision = int(target.physical_import_batch_id)
    pool = str(target_reservations[0].planning_stock_pool)
    raw_facts = tuple(
        Fact(
            **{
                **fact.__dict__,
                "planning_stock_pool": pool,
                "characteristic_ref": "",
                "organization_ref": "",
            }
        )
        for fact in facts
    )
    raw_reserves = tuple(
        Reserve(**{**reserve.__dict__, "planning_stock_pool": pool})
        for reserve in _reserves(target_reservations)
    )
    with pytest.raises(CurrentReplenishmentError, match="payload drift"):
        apply_current_replenishment(
            db_session,
            generation_id=int(target.id),
            source_key="accepted-physical-receipts",
            source_revision=revision,
            facts=raw_facts,
            reserves=raw_reserves,
            complete_scope=True,
            distribution_scope=(item_id, "", "", pool, "buy"),
            allow_building=True,
        )
    with pytest.raises(TypeError, match="unexpected keyword"):
        apply_current_replenishment(
            db_session,
            generation_id=int(target.id),
            source_key="accepted-physical-receipts",
            source_revision=revision,
            facts=raw_facts,
            reserves=raw_reserves,
            complete_scope=True,
            distribution_scope=(item_id, "", "", pool, "buy"),
            allow_building=True,
            allow_same_revision_rebind=True,
        )


def test_identical_same_revision_cross_generation_rebind_keeps_assignments(
    db_session,
):
    generation_id, _item_id, reservations, facts = _world(
        db_session, prefix="identical-cross-generation"
    )
    reserves = _reserves(reservations)
    apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:identical-cross-generation",
        source_revision=1,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
    )
    _mark_reservations_current(db_session, reservations)
    before_ids = {
        (int(row.sle_id), int(row.reservation_id)): int(row.id)
        for row in db_session.query(models.ReservationConsumptionAllocation).all()
    }
    state = db_session.query(models.CurrentReplenishmentState).one()
    before_checksum = str(state.scope_checksum)
    parent = db_session.get(models.LedgerGeneration, generation_id)
    target = models.LedgerGeneration(
        generation_key=f"r4-identical-target-{uuid4().hex[:12]}",
        status="building",
        cutoff=parent.cutoff,
        source_watermarks={"parent_generation_id": int(parent.id)},
        capabilities=dict(parent.capabilities or {}),
        physical_import_batch_id=int(parent.physical_import_batch_id),
        algorithm_version="r4-tests",
    )
    db_session.add(target)
    db_session.flush()

    result = apply_current_replenishment(
        db_session,
        generation_id=int(target.id),
        source_key="physical:identical-cross-generation",
        source_revision=1,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
        allow_building=True,
    )

    after_ids = {
        (int(row.sle_id), int(row.reservation_id)): int(row.id)
        for row in db_session.query(models.ReservationConsumptionAllocation).all()
    }
    state = db_session.query(models.CurrentReplenishmentState).one()
    assert result.changed_pairs == 0
    assert result.idempotent is False
    assert after_ids == before_ids
    assert str(state.scope_checksum) == before_checksum
    assert int(state.ledger_generation_id) == int(target.id)


def test_same_batch_rebind_rejects_generation_outside_sealed_parent_lineage(
    db_session,
):
    generation_id, _item_id, reservations, facts = _world(
        db_session, prefix="unrelated-lineage"
    )
    _add_supplier_provenance(db_session, generation_id, facts, prefix="lineage-parent")
    apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=generation_id
    )
    _mark_reservations_current(db_session, reservations)
    parent = db_session.get(models.LedgerGeneration, generation_id)
    unrelated = models.LedgerGeneration(
        generation_key=f"r4-unrelated-{uuid4().hex[:12]}",
        status="accepted",
        cutoff=parent.cutoff,
        accepted_at=parent.cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch_id=int(parent.physical_import_batch_id),
        algorithm_version="r4-tests",
    )
    db_session.add(unrelated)
    db_session.flush()
    target, _target_reservations = _building_rebind_target(
        db_session,
        parent_generation_id=generation_id,
        sealed_parent_generation_id=int(unrelated.id),
        reservations=reservations,
        quantities=("5", "10"),
    )
    _add_supplier_provenance(db_session, target.id, facts, prefix="lineage-target")
    with pytest.raises(CurrentReplenishmentError, match="outside.*sealed parent lineage"):
        apply_current_replenishment_for_accepted_generation(
            db_session, generation_id=int(target.id), allow_building=True
        )


def test_accepted_adapter_rejects_incomplete_physical_pages(db_session):
    generation_id, _item_id, _reservations, facts = _world(
        db_session, prefix="incomplete-pages"
    )
    generation = db_session.get(models.LedgerGeneration, generation_id)
    batch = generation.physical_import_batch
    batch.expected_page_count = 2
    batch.received_page_count = 1
    db_session.add(
        models.PhysicalImportPage(
            import_batch_id=int(batch.id),
            page_no=1,
            page_token="r4-page-1",
            content_hash="f" * 64,
        )
    )
    _add_supplier_provenance(db_session, generation_id, facts, prefix="incomplete")
    with pytest.raises(CurrentReplenishmentError, match="incomplete or reordered pages"):
        apply_current_replenishment_for_accepted_generation(
            db_session, generation_id=generation_id
        )


def test_accepted_adapter_uses_unsuperseded_fact_despite_mutable_active_flag(
    db_session,
):
    generation_id, _item_id, _reservations, facts = _world(
        db_session, prefix="inactive-visible"
    )
    sle_id = int(facts[0].fact_id)
    generation = db_session.get(models.LedgerGeneration, generation_id)
    generation.physical_import_batch.cutoff = None
    db_session.get(models.StockLedgerEntry, sle_id).active = False
    _add_supplier_provenance(db_session, generation_id, facts, prefix="inactive")
    results = apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=generation_id
    )
    assert results[0].changed_pairs > 0
    assert db_session.query(models.ReservationConsumptionAllocation).filter(
        models.ReservationConsumptionAllocation.sle_id == sle_id,
        models.ReservationConsumptionAllocation.is_current.is_(True),
    ).count() > 0


def test_next_physical_batch_uses_normal_revision_advance(db_session):
    generation_id, _item_id, reservations, facts = _world(
        db_session, prefix="next-physical"
    )
    _add_supplier_provenance(db_session, generation_id, facts, prefix="next-parent")
    apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=generation_id
    )
    _mark_reservations_current(db_session, reservations)
    next_batch = models.PhysicalImportBatch(
        batch_key=f"r4-next-physical-{uuid4().hex[:12]}",
        status="completed",
        cutoff=datetime(2026, 9, 11, tzinfo=timezone.utc),
        source_watermarks={"next": True},
        completed_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )
    db_session.add(next_batch)
    db_session.flush()
    target, _target_reservations = _building_rebind_target(
        db_session,
        parent_generation_id=generation_id,
        reservations=reservations,
        quantities=("5", "10"),
        physical_batch=next_batch,
    )
    _add_supplier_provenance(db_session, target.id, facts, prefix="next-target")

    results = apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=int(target.id), allow_building=True
    )

    state = db_session.query(models.CurrentReplenishmentState).one()
    assert results[0].changed_pairs > 0
    assert int(state.source_revision) == int(next_batch.id)
    assert int(state.ledger_generation_id) == int(target.id)


def test_material_consumption_role_is_excluded_from_current_replenishment_reader(
    db_session,
):
    generation_id, item_id, reservations, facts = _world(db_session, prefix="roles")
    apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:roles",
        source_revision=1,
        facts=facts,
        reserves=_reserves(reservations),
        complete_scope=True,
    )
    db_session.flush()
    material = db_session.query(models.ReservationConsumptionAllocation).first()
    material.allocation_role = "material_consumption"
    db_session.commit()
    rows = read_current_replenishment(db_session, generation_id=generation_id, item_id=item_id)
    assert all(row["id"] != material.id for row in rows)

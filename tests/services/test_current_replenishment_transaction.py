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
    reject_legacy_supplier_receipt_writer,
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
    # An unproven empty input never publishes as emptiness: clearing a
    # populated scope has to name the reason the fact set is empty.
    with pytest.raises(CurrentReplenishmentError, match="confirmed_empty_reason"):
        apply_current_replenishment(
            db_session,
            generation_id=generation_id,
            source_key="physical:r4",
            source_revision=2,
            facts=(),
            reserves=(),
            distribution_scope=(item_id, "", "", "selected", "buy"),
            complete_scope=True,
        )
    db_session.rollback()
    result = apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:r4",
        source_revision=2,
        facts=(),
        reserves=(),
        distribution_scope=(item_id, "", "", "selected", "buy"),
        complete_scope=True,
        confirmed_empty_reason="verified empty supplier stream for the scope",
    )
    db_session.commit()
    assert result.deleted == 1
    cleared_audit = db_session.query(models.CurrentReplenishmentAudit).filter_by(
        operation="delete", source_revision=2
    ).all()
    assert cleared_audit
    # The canonical enum stays in ``reason``; the operator text is echoed to
    # the caller instead of replacing it.
    assert all(row.reason == "confirmed_empty_scope" for row in cleared_audit)
    assert result.confirmed_empty_reason == (
        "verified empty supplier stream for the scope"
    )
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
    results = apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=generation_id, source_revision=9
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


SUPPLIER_RECORDER_TYPE = "Document_ПриходнаяНакладная"


def _promote_to_current_owners(db, reservations):
    """Mark the frozen reservations as the stable current owners."""
    for row in reservations:
        row.owner_kind = "current"
        row.is_current = True
        row.current_identity = (
            f"reservation:req:{int(row.requirement_id)}:mode:{row.realization_mode}"
        )
    db.flush()


def _building_staging_reserve(db, *, generation_id, item_id, quantity="4"):
    """One BUILDING staging reservation of a brand-new obligation refresh.

    An obligation refresh renumbers requirements, so the staging reserve never
    shares an identity with the accepted owners it runs beside.
    """
    accepted = db.get(models.LedgerGeneration, int(generation_id))
    staging_generation = models.LedgerGeneration(
        generation_key=f"r4-staging-{uuid4().hex[:12]}",
        status="building",
        cutoff=accepted.cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True, "reservation_replay": True},
        physical_import_batch_id=int(accepted.physical_import_batch_id),
        algorithm_version="r4-tests",
    )
    db.add(staging_generation)
    db.flush()
    run = models.PlanningRun(
        status="BUILDING_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=staging_generation.id,
        ledger_cutoff=staging_generation.cutoff,
        source_plan_id=None,
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        active_freeze_version=1,
    )
    db.add(run)
    db.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=int(item_id),
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
        ledger_generation_id=staging_generation.id,
        item_id=int(item_id),
        run_id=run.run_id,
        freeze_version=1,
        requirement_id=requirement.id,
        priority_period_from=date(2026, 9, 1),
        priority_period_to=date(2026, 9, 30),
        realization_mode="buy",
        reserved_qty=Decimal(quantity),
        replenishment_required_qty=Decimal(quantity),
        lifecycle_status="active",
        owner_kind="building",
        is_current=False,
        current_identity=f"reservation:req:{requirement.id}:mode:buy",
    )
    db.add(entry)
    db.flush()
    return staging_generation, entry


def test_building_staging_replay_keeps_the_stable_current_owners_allocations(db_session):
    """A BUILDING replay owns its staging reserves, not the whole pool.

    Selecting the "before" set by distribution scope alone made this replay
    plan a deletion for every allocation of the accepted current owners it had
    never been handed - inside a transaction that had not published anything
    yet.  Those owners are closed by the reservation publisher when the
    refresh lands; retiring their basis is not this writer's job.
    """
    generation_id, item_id, reservations, facts = _world(db_session, prefix="staging")
    apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:r4",
        source_revision=1,
        facts=facts,
        reserves=_reserves(reservations),
        complete_scope=True,
    )
    _promote_to_current_owners(db_session, reservations)
    db_session.commit()
    accepted_allocations = {
        int(row.id)
        for row in db_session.query(models.ReservationConsumptionAllocation).filter_by(
            is_current=True
        )
    }
    assert len(accepted_allocations) == 2

    staging_generation, staging_reserve = _building_staging_reserve(
        db_session, generation_id=generation_id, item_id=item_id
    )
    staging_fact = Fact(
        fact_id=str(facts[0].fact_id),
        item_id=int(item_id),
        mode="buy",
        qty=Decimal("4"),
        posting_at=facts[0].posting_at,
    )
    result = apply_current_replenishment(
        db_session,
        generation_id=int(staging_generation.id),
        source_key="physical:r4",
        source_revision=2,
        facts=(staging_fact,),
        reserves=(
            Reserve(
                reserve_id=str(staging_reserve.id),
                item_id=int(item_id),
                mode="buy",
                reserved_qty=Decimal("4"),
                due_date=staging_reserve.priority_period_to,
                plan_period_from=staging_reserve.priority_period_from,
                plan_period_to=staging_reserve.priority_period_to,
                run_id=int(staging_reserve.run_id),
                requirement_id=int(staging_reserve.requirement_id),
            ),
        ),
        complete_scope=True,
        allow_building=True,
    )
    db_session.commit()

    assert result.deleted == 0
    surviving = {
        int(row.id)
        for row in db_session.query(models.ReservationConsumptionAllocation).filter_by(
            is_current=True
        )
    }
    assert accepted_allocations <= surviving


def test_supplier_facts_without_any_provenance_fail_closed(db_session):
    """Zero provenance rows is only "no receipts" when there are no supplier facts.

    With supplier documents in the visible prefix and nothing typed, every BUY
    scope was published empty: the receipt facts disappeared, coverage dropped
    to zero and the emptiness had no recorded cause anywhere.
    """
    generation_id, _item_id, _reservations, facts = _world(db_session, prefix="untyped")
    for fact in facts:
        sle = db_session.get(models.StockLedgerEntry, int(fact.fact_id))
        sle.recorder_type = SUPPLIER_RECORDER_TYPE
        sle.active = True
    db_session.flush()

    with pytest.raises(
        CurrentReplenishmentError, match="no supplier receipt provenance"
    ) as excinfo:
        apply_current_replenishment_for_accepted_generation(
            db_session, generation_id=generation_id, source_revision=9
        )
    assert str(generation_id) in str(excinfo.value)
    assert db_session.query(models.ReservationConsumptionAllocation).count() == 0


def test_no_supplier_documents_and_no_provenance_publishes_an_empty_scope(db_session):
    """The control: a generation with no supplier document is legitimately empty."""
    generation_id, _item_id, _reservations, _facts = _world(db_session, prefix="nosup")

    results = apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=generation_id, source_revision=9
    )
    db_session.commit()

    assert results
    assert all(result.inserted == 0 for result in results)


def _supplier_typed_world(db, *, prefix):
    """An accepted generation that owns typed evidence for its supplier facts."""
    generation_id, item_id, reservations, facts = _world(db, prefix=prefix)
    for fact in facts:
        sle = db.get(models.StockLedgerEntry, int(fact.fact_id))
        sle.recorder_type = SUPPLIER_RECORDER_TYPE
        sle.active = True
        db.add(models.StockLedgerSupplierReceiptProvenance(
            ledger_generation_id=int(generation_id),
            stock_ledger_entry_id=int(sle.id),
            receipt_doc_type=SUPPLIER_RECORDER_TYPE,
            receipt_doc_ref=f"receipt-{int(sle.id)}",
            receipt_doc_line_no="1",
            supplier_order_ref=None,
            supplier_order_line_no=None,
            operation_kind="supplier_receipt",
            operation_key="test",
            operation_name="test supplier receipt",
            evidence_hash=f"hash:{int(sle.id)}".ljust(64, "0"),
            evidence_payload={"signed_qty": str(sle.qty), "item_id": int(sle.item_id)},
            match_rule="bounded-typed",
            match_status="unmatched",
            ambiguity_count=0,
            reason="no exact typed supplier order line",
        ))
    for row in reservations:
        row.owner_kind = "current"
        row.is_current = True
        row.current_identity = (
            f"reservation:req:{int(row.requirement_id)}:mode:{row.realization_mode}"
        )
    db.flush()
    return generation_id, item_id, reservations, facts


def _successor_over_the_same_prefix(db, parent, *, key):
    """A later accepted generation over the parent's own physical prefix."""
    successor = models.LedgerGeneration(
        generation_key=f"{key}-{int(parent.id)}",
        status="accepted",
        cutoff=parent.cutoff,
        accepted_at=parent.cutoff,
        source_watermarks={"parent_generation_id": int(parent.id)},
        capabilities=dict(parent.capabilities or {}),
        physical_import_batch_id=int(parent.physical_import_batch_id),
        algorithm_version="r4-tests",
    )
    db.add(successor)
    db.flush()
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None:
        db.add(models.PlanningTruthState(id=1, current_generation_id=int(successor.id)))
    else:
        pointer.current_generation_id = int(successor.id)
    db.flush()
    return successor


def test_a_generation_that_lost_its_typed_evidence_is_rejected(db_session):
    """The defect the lightweight fork used to create, seen from the reader.

    The facts are accepted and visible, the typing exists - at another
    generation.  Reading the pointer then sees no receipts at all and would
    publish every BUY scope empty, which is an accepted physical quantity made
    uncountable.
    """
    generation_id, _item_id, _reservations, facts = _supplier_typed_world(
        db_session, prefix="lost"
    )
    # A later generation over the same prefix that did not carry the evidence.
    parent = db_session.get(models.LedgerGeneration, int(generation_id))
    successor = _successor_over_the_same_prefix(
        db_session, parent, key="lost-successor"
    )

    with pytest.raises(
        CurrentReplenishmentError, match="lost supplier receipt provenance"
    ) as excinfo:
        apply_current_replenishment_for_accepted_generation(
            db_session, generation_id=int(successor.id), source_revision=11
        )
    assert str(len(facts)) in str(excinfo.value)


def test_a_generation_that_carried_its_typed_evidence_is_accepted(db_session):
    """The same successor, forked the way the fix forks: evidence carried."""
    from app.services.item_ledger.physical_refresh_generation import (
        _clone_supplier_receipt_provenance,
    )

    generation_id, item_id, _reservations, facts = _supplier_typed_world(
        db_session, prefix="carried"
    )
    parent = db_session.get(models.LedgerGeneration, int(generation_id))
    successor = _successor_over_the_same_prefix(
        db_session, parent, key="carried-successor"
    )
    _clone_supplier_receipt_provenance(
        db_session,
        parent_generation_id=int(parent.id),
        target_generation_id=int(successor.id),
    )

    results = apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=int(successor.id), source_revision=11
    )
    db_session.commit()

    assert results
    carried = db_session.query(models.StockLedgerSupplierReceiptProvenance).filter_by(
        ledger_generation_id=int(successor.id)
    ).count()
    assert carried == len(facts)
    # The receipts are countable again: the replay assigned them.
    assert db_session.query(models.ReservationConsumptionAllocation).filter_by(
        is_current=True, allocation_role="replenishment_receipt",
    ).count() > 0
    assert read_current_replenishment(
        db_session, generation_id=int(successor.id), item_id=item_id
    )


def _state_rows(db):
    return db.query(models.CurrentReplenishmentState).all()


def _key(row):
    return str(row.source_key or "").strip()


def test_bootstrapped_buy_scope_accepts_the_next_bounded_replay(db_session):
    """Entering a scope through another door is not a stream change.

    The one-off bootstrap and the full accept wrote
    ``accepted-physical-receipts`` while the bounded BUY replay wrote
    ``physical-refresh:buy``, so the first bounded refresh after the
    migration was rejected for all 547 scopes with "source stream changed for
    canonical distribution scope".  Canon R4/R5: the stream belongs to the
    distribution scope, not to the caller.
    """
    from app.services.item_ledger.current_replenishment import (
        SUPPLIER_RECEIPT_SOURCE_KEY,
    )

    generation_id, item_id, reservations, facts = _world(db_session, prefix="stream")
    reserves = _reserves(reservations)
    # What the bootstrap writes.
    apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="accepted-physical-receipts",
        source_revision=1,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()
    # A database migrated before this fix carries the old spelling on the row.
    for row in _state_rows(db_session):
        row.source_key = "accepted-physical-receipts"
    db_session.commit()
    assert {_key(row) for row in _state_rows(db_session)} == {
        "accepted-physical-receipts"
    }

    # What the next bounded BUY refresh writes for the same scope.
    result = apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key=SUPPLIER_RECEIPT_SOURCE_KEY,
        source_revision=2,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()

    assert result.source_key == SUPPLIER_RECEIPT_SOURCE_KEY
    states = _state_rows(db_session)
    assert len(states) == 1
    # The legacy spelling is rewritten in place; no data migration needed.
    assert _key(states[0]) == SUPPLIER_RECEIPT_SOURCE_KEY


def test_bootstrapped_make_scope_accepts_the_next_bounded_replay(db_session):
    """Same for the assembly-output stream."""
    from app.services.item_ledger.current_replenishment import (
        ASSEMBLY_OUTPUT_SOURCE_KEY,
    )

    generation_id, _item_id, reservations, facts = _world(db_session, prefix="make")
    for row in reservations:
        row.realization_mode = "make"
        row.current_identity = (
            f"reservation:req:{int(row.requirement_id)}:mode:make"
        )
    db_session.flush()
    make_facts = tuple(
        Fact(**{**fact.__dict__, "mode": "make"}) for fact in facts
    )
    make_reserves = tuple(
        Reserve(**{**reserve.__dict__, "mode": "make"})
        for reserve in _reserves(reservations)
    )
    apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical-refresh:make",
        source_revision=1,
        facts=make_facts,
        reserves=make_reserves,
        complete_scope=True,
    )
    db_session.commit()
    for row in _state_rows(db_session):
        row.source_key = "physical-refresh:make"
    db_session.commit()

    result = apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key=ASSEMBLY_OUTPUT_SOURCE_KEY,
        source_revision=2,
        facts=make_facts,
        reserves=make_reserves,
        complete_scope=True,
    )
    db_session.commit()

    assert result.source_key == ASSEMBLY_OUTPUT_SOURCE_KEY
    states = _state_rows(db_session)
    assert len(states) == 1
    assert _key(states[0]) == ASSEMBLY_OUTPUT_SOURCE_KEY


def test_a_genuinely_foreign_source_stream_still_fails_closed(db_session):
    """The guard still does its job for a stream nobody documented."""
    from app.services.item_ledger.current_replenishment import (
        SUPPLIER_RECEIPT_SOURCE_KEY,
    )

    generation_id, _item_id, reservations, facts = _world(db_session, prefix="foreign")
    reserves = _reserves(reservations)
    apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key=SUPPLIER_RECEIPT_SOURCE_KEY,
        source_revision=1,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()

    with pytest.raises(CurrentReplenishmentError, match="source stream changed"):
        apply_current_replenishment(
            db_session,
            generation_id=generation_id,
            source_key="some-other-importer",
            source_revision=2,
            facts=facts,
            reserves=reserves,
            complete_scope=True,
        )


def test_bootstrap_revision_cannot_collide_with_the_next_bounded_refresh(db_session):
    """The drift guard must not fire on the first bounded refresh.

    Exercised through the explicit-revision seam: a strictly greater revision
    with a different payload is a new revision, never drift.  Production
    writers stamp the publishing generation instead (``GENERATION_REVISION``;
    see the item-23 tests below), because an obligation refresh inherits its
    parent's batch and the batch id cannot tell two publications apart.
    """
    from app.services.item_ledger.current_replenishment import (
        SUPPLIER_RECEIPT_SOURCE_KEY,
    )

    generation_id, _item_id, reservations, facts = _world(db_session, prefix="revision")
    reserves = _reserves(reservations)
    pointer_batch = int(
        db_session.get(models.LedgerGeneration, generation_id).physical_import_batch_id
    )
    apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="accepted-physical-receipts",
        source_revision=pointer_batch,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()
    for row in _state_rows(db_session):
        row.source_key = "accepted-physical-receipts"
    db_session.commit()

    # A later batch id, with a different payload: accepted as a new revision,
    # never judged as drift.
    changed = list(facts)
    changed[0] = Fact(**{**facts[0].__dict__, "qty": Decimal("5")})
    result = apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key=SUPPLIER_RECEIPT_SOURCE_KEY,
        source_revision=pointer_batch + 1,
        facts=tuple(changed),
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()

    assert result.source_revision == pointer_batch + 1
    assert result.idempotent is False


def test_the_two_entry_paths_are_one_stream_for_the_same_scope(db_session):
    """The exact production failure, written with the literal keys.

    This is what the stand hit on its first bounded refresh after the
    bootstrap: 547 scopes rejected with "source stream changed for canonical
    distribution scope" because the bootstrap had written
    ``accepted-physical-receipts`` and the bounded BUY replay announced
    ``physical-refresh:buy`` for the very same distribution scope.
    """
    generation_id, _item_id, reservations, facts = _world(db_session, prefix="literal")
    reserves = _reserves(reservations)
    apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="accepted-physical-receipts",
        source_revision=1,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()

    result = apply_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical-refresh:buy",
        source_revision=2,
        facts=facts,
        reserves=reserves,
        complete_scope=True,
    )
    db_session.commit()

    assert result.source_key == "supplier-receipts"
    assert len(_state_rows(db_session)) == 1


# --- R4 revision identifies the publishing generation (item 23) -------------


def _obligation_refresh_over(db, parent, *, key):
    """An obligation-refresh generation: new id, the parent's own batch."""
    from app.services.item_ledger.physical_refresh_generation import (
        _clone_supplier_receipt_provenance,
    )

    successor = _successor_over_the_same_prefix(db, parent, key=key)
    _clone_supplier_receipt_provenance(
        db,
        parent_generation_id=int(parent.id),
        target_generation_id=int(successor.id),
    )
    return successor


def _point_at(db, generation):
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None:
        db.add(models.PlanningTruthState(id=1, current_generation_id=int(generation.id)))
    else:
        pointer.current_generation_id = int(generation.id)
    db.flush()


def _replace_run(reservations, quantity):
    """What a run replacement does to the scope input: new reserve quantities."""
    for row in reservations:
        row.reserved_qty = Decimal(quantity)
        row.replenishment_required_qty = Decimal(quantity)


def test_two_consecutive_obligation_refreshes_over_one_batch_are_both_accepted(
    db_session,
):
    """The stand's run 507: the second refresh failed "payload drift".

    Both refreshes inherit the parent's import batch, so the batch id could
    not tell their publications apart; their reserve sets differ, which is
    exactly a different checksum under "the same revision".
    """
    generation_id, _item_id, reservations, _facts = _supplier_typed_world(
        db_session, prefix="two-refreshes"
    )
    parent = db_session.get(models.LedgerGeneration, int(generation_id))
    first = _obligation_refresh_over(db_session, parent, key="refresh-1")
    _replace_run(reservations, "6")
    apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=int(first.id)
    )
    db_session.commit()

    second = _obligation_refresh_over(db_session, first, key="refresh-2")
    assert second.physical_import_batch_id == first.physical_import_batch_id
    _replace_run(reservations, "5")
    results = apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=int(second.id)
    )
    db_session.commit()

    assert results and all(int(r.source_revision) == int(second.id) for r in results)
    assert {
        (int(row.source_revision), int(row.ledger_generation_id))
        for row in _state_rows(db_session)
    } == {(int(second.id), int(second.id))}


def test_obligation_and_bounded_writes_interleave_on_one_axis(db_session):
    """Bootstrap -> bounded -> obligation -> bounded: strictly increasing.

    Every production writer stamps the generation it publishes, whatever its
    kind, so the order of publication is the order of revisions.
    """
    from app.services.item_ledger.current_replenishment import (
        GENERATION_REVISION,
        SUPPLIER_RECEIPT_SOURCE_KEY,
    )

    generation_id, _item_id, reservations, facts = _world(db_session, prefix="axis")
    reserves = _reserves(reservations)
    pointer = db_session.get(models.LedgerGeneration, int(generation_id))

    def bounded(generation, qty):
        changed = (Fact(**{**facts[0].__dict__, "qty": Decimal(qty)}),) + tuple(facts[1:])
        return apply_current_replenishment(
            db_session,
            generation_id=int(generation.id),
            source_key=SUPPLIER_RECEIPT_SOURCE_KEY,
            source_revision=int(generation.id),
            facts=changed,
            reserves=reserves,
            complete_scope=True,
            revision_basis=GENERATION_REVISION,
        )

    bootstrap = bounded(pointer, "8")
    physical = _successor_over_the_same_prefix(db_session, pointer, key="axis-bounded")
    after_bounded = bounded(physical, "5")
    obligation = _successor_over_the_same_prefix(db_session, physical, key="axis-obl")
    after_obligation = bounded(obligation, "4")
    physical_again = _successor_over_the_same_prefix(
        db_session, obligation, key="axis-bounded-2"
    )
    after_again = bounded(physical_again, "3")
    db_session.commit()

    revisions = [
        int(r.source_revision)
        for r in (bootstrap, after_bounded, after_obligation, after_again)
    ]
    assert revisions == sorted(set(revisions))
    assert revisions == [
        int(pointer.id), int(physical.id), int(obligation.id), int(physical_again.id)
    ]


def test_a_marker_stamped_with_a_batch_id_is_accepted_by_the_first_generation_write(
    db_session,
):
    """Existing databases: ``14440`` against generation ids near ``1450``.

    As stored the batch revision looks newer than every generation and would
    refuse every write; its own generation column is its revision on the
    generation axis, and the first write that passes rewrites it.
    """
    generation_id, _item_id, reservations, _facts = _supplier_typed_world(
        db_session, prefix="legacy-batch"
    )
    parent = db_session.get(models.LedgerGeneration, int(generation_id))
    _point_at(db_session, parent)
    apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=int(parent.id)
    )
    db_session.commit()
    for row in _state_rows(db_session):
        row.source_revision = 14440
    db_session.commit()

    successor = _obligation_refresh_over(db_session, parent, key="legacy-next")
    _replace_run(reservations, "6")
    results = apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=int(successor.id)
    )
    db_session.commit()

    assert results
    assert {int(row.source_revision) for row in _state_rows(db_session)} == {
        int(successor.id)
    }


def test_a_batch_id_marker_republished_by_its_own_generation_is_rewritten(db_session):
    generation_id, _item_id, _reservations, _facts = _supplier_typed_world(
        db_session, prefix="legacy-same"
    )
    _point_at(db_session, db_session.get(models.LedgerGeneration, int(generation_id)))
    apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=int(generation_id)
    )
    db_session.commit()
    for row in _state_rows(db_session):
        row.source_revision = 14440
    db_session.commit()

    results = apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=int(generation_id)
    )
    db_session.commit()

    assert all(int(r.changed_pairs) == 0 for r in results)
    assert {int(row.source_revision) for row in _state_rows(db_session)} == {
        int(generation_id)
    }


def test_genuine_drift_under_one_generation_still_fails_closed(db_session):
    """Same publication, different input: that is drift, not a new revision."""
    generation_id, _item_id, reservations, _facts = _supplier_typed_world(
        db_session, prefix="drift"
    )
    _point_at(db_session, db_session.get(models.LedgerGeneration, int(generation_id)))
    apply_current_replenishment_for_accepted_generation(
        db_session, generation_id=int(generation_id)
    )
    db_session.commit()
    _replace_run(reservations, "6")

    with pytest.raises(CurrentReplenishmentError, match="payload drift"):
        apply_current_replenishment_for_accepted_generation(
            db_session, generation_id=int(generation_id)
        )


def test_an_older_generation_cannot_overwrite_a_newer_publication(db_session):
    from app.services.item_ledger.current_replenishment import (
        GENERATION_REVISION,
        SUPPLIER_RECEIPT_SOURCE_KEY,
    )

    generation_id, _item_id, reservations, facts = _world(db_session, prefix="stale")
    parent = db_session.get(models.LedgerGeneration, int(generation_id))
    successor = _successor_over_the_same_prefix(db_session, parent, key="stale-next")

    def write(generation):
        return apply_current_replenishment(
            db_session,
            generation_id=int(generation.id),
            source_key=SUPPLIER_RECEIPT_SOURCE_KEY,
            source_revision=int(generation.id),
            facts=facts,
            reserves=_reserves(reservations),
            complete_scope=True,
            revision_basis=GENERATION_REVISION,
        )

    write(successor)
    db_session.commit()
    _replace_run(reservations, "6")
    with pytest.raises(CurrentReplenishmentError, match="stale source revision"):
        write(parent)


def test_a_generation_revision_must_name_the_publishing_generation(db_session):
    from app.services.item_ledger.current_replenishment import GENERATION_REVISION

    generation_id, _item_id, reservations, facts = _world(db_session, prefix="name")
    with pytest.raises(CurrentReplenishmentError, match="does not identify"):
        apply_current_replenishment(
            db_session,
            generation_id=int(generation_id),
            source_key="supplier-receipts",
            source_revision=int(generation_id) + 1,
            facts=facts,
            reserves=_reserves(reservations),
            complete_scope=True,
            revision_basis=GENERATION_REVISION,
        )

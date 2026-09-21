"""Regression: incremental carry-forward must preserve realization physically.

An incremental obligation refresh (specification rebase) reuses the parent's
physical prefix and deliberately SKIPS the historical replay, so a retained
run's realization cannot be rebuilt downstream — it is carried forward as
persisted state.  The carry step therefore must copy the parent's real
realization events (each keeping its visible ``sle_id``) rather than synthesize
a single ``open`` event that stamps ``realized_delta`` with ``sle_id=None``.  The
latter fails the fact-conservation checkpoint with "reservation realization
references a non-visible physical fact" and deadlocked every incremental rebase
whose retained runs had already received goods.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from app import models
from app.services.item_ledger.obligation_generation import (
    carry_forward_retained_reservations,
    _current_reservation_owner_fingerprint,
)
from app.services.item_ledger.generation_lifecycle import (
    _d,
    _reservation_fact_conservation_checkpoint,
    _reservation_fold_checkpoint,
)
from app.services.item_ledger.physical_visibility import visible_sles_for_generation


CUTOFF = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)


def test_incremental_carry_forward_preserves_realization_against_visible_sle(
    db_session,
):
    db = db_session

    physical = models.PhysicalImportBatch(
        batch_key="cf-physical", status="completed", cutoff=CUTOFF,
        source_watermarks={}, completed_at=CUTOFF,
    )
    db.add(physical)
    db.flush()
    parent = models.LedgerGeneration(
        generation_key="cf-parent", status="accepted", cutoff=CUTOFF,
        source_watermarks={}, capabilities={},
        physical_import_batch_id=int(physical.id), algorithm_version="test",
        accepted_at=CUTOFF,
    )
    item = models.Item(item_code="CF-ITEM", item_name="carry item", status="active")
    db.add_all([parent, item])
    db.flush()

    # A physical fact in the parent's import batch that backs the realization.
    sle = models.StockLedgerEntry(
        ingest_batch_id=int(physical.id), source_content_hash="cf-sle",
        item_id=int(item.item_id), qty=Decimal("4"), qty_after=Decimal("4"),
        posting_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        movement_kind="assembly_in", record_type="Receipt", recorder_type="test",
        recorder_ref="cf-sle", line_no="1", ingest_source="test",
    )
    plan = models.ProductionPlanHeader(
        name="retained", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        status="fixed", fixed_at=CUTOFF,
    )
    db.add_all([sle, plan])
    db.flush()
    run = models.PlanningRun(
        source_plan_id=int(plan.id), status="FIXED_SNAPSHOT",
        ledger_generation_id=int(parent.id), period_from=plan.period_from,
        period_to=plan.period_to, fixed_at=CUTOFF, pinned=True,
        active_freeze_version=1, config_snapshot={},
    )
    db.add(run)
    db.flush()
    requirement = models.MrpRequirement(
        run_id=int(run.run_id), item_id=int(item.item_id),
        total_required_qty=Decimal("10"), net_required_qty=Decimal("10"),
        period_from=plan.period_from, period_to=plan.period_to, bom_level=0,
    )
    db.add(requirement)
    db.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=int(parent.id), item_id=int(item.item_id),
        run_id=int(run.run_id), freeze_version=1, requirement_id=int(requirement.id),
        priority_period_from=plan.period_from, priority_period_to=plan.period_to,
        reserved_qty=Decimal("10"), covered_from_stock_at_freeze_qty=Decimal("0"),
        replenishment_required_qty=Decimal("10"),
        replenishment_received_qty=Decimal("4"), realized_qty=Decimal("4"),
        lifecycle_status="active", opened_at=CUTOFF,
    )
    db.add(reservation)
    db.flush()
    # Parent event stream: open (reserve) + realize (against the real SLE).
    db.add_all([
        models.ReservationEvent(
            ledger_generation_id=int(parent.id), reservation_id=int(reservation.id),
            item_id=int(item.item_id), event_kind="open",
            reserved_delta=Decimal("10"), realized_delta=Decimal("0"), sle_id=None,
            cycle_id=f"parent-open:g{int(parent.id)}",
            idempotency_key=f"p-open-{int(reservation.id)}", event_at=CUTOFF,
        ),
        models.ReservationEvent(
            ledger_generation_id=int(parent.id), reservation_id=int(reservation.id),
            item_id=int(item.item_id), event_kind="realize",
            reserved_delta=Decimal("0"), realized_delta=Decimal("4"),
            sle_id=int(sle.id), fact_ref="cf-sle", match_rule="fifo",
            cycle_id=f"parent-realize:g{int(parent.id)}",
            idempotency_key=f"p-realize-{int(reservation.id)}", event_at=CUTOFF,
        ),
    ])
    db.commit()

    # Target: an obligation-refresh fork reusing the parent's physical prefix.
    target = models.LedgerGeneration(
        generation_key="cf-target", status="building", cutoff=CUTOFF,
        source_watermarks={}, capabilities={},
        physical_import_batch_id=int(physical.id), algorithm_version="test",
    )
    db.add(target)
    db.flush()

    carried = carry_forward_retained_reservations(
        db, parent_generation_id=int(parent.id),
        target_generation_id=int(target.id),
        retained_run_ids=(int(run.run_id),), preserve_realization=True,
    )
    db.flush()
    assert carried == 1

    entries, events, _ = _reservation_fold_checkpoint(
        db, target, is_allowed_cycle=lambda cycle: True,
    )
    # The defect was a realized delta with no physical fact behind it.
    assert not any(_d(e.realized_delta) != 0 and e.sle_id is None for e in events)
    # The fold reconstructs the preserved caches exactly.
    (entry,) = entries
    assert _d(entry.reserved_qty) == Decimal("10")
    assert _d(entry.realized_qty) == Decimal("4")
    assert _d(entry.replenishment_required_qty) - _d(entry.replenishment_received_qty) == Decimal("6")
    realized = [e for e in events if e.sle_id is not None]
    assert len(realized) == 1
    assert sum((_d(e.realized_delta) for e in realized), Decimal("0")) == Decimal("4")
    # The exact checkpoint that used to raise must now pass, counting one fact.
    visible = visible_sles_for_generation(db, int(target.id))
    assert _reservation_fact_conservation_checkpoint(events, visible) == 1
    # Carried events are re-stamped under the target's carry cycle.
    assert all(
        e.cycle_id == f"obligation-carry:g{int(target.id)}" for e in events
    )

    # Explicit recovery may select the stable current owner even when its
    # provenance generation is older than the accepted parent.  The source
    # map and fingerprint are mandatory, so this cannot widen normal refresh.
    sealed_source = models.LedgerGeneration(
        generation_key="cf-recovery-source", status="accepted", cutoff=CUTOFF,
        source_watermarks={}, capabilities={},
        physical_import_batch_id=int(physical.id), algorithm_version="test",
        accepted_at=CUTOFF,
    )
    db.add(sealed_source)
    db.flush()
    reservation.ledger_generation_id = int(sealed_source.id)
    second_item = models.Item(item_code="CF-ITEM-2", item_name="carry item 2", status="active")
    db.add(second_item)
    db.flush()
    second_requirement = models.MrpRequirement(
        run_id=int(run.run_id), item_id=int(second_item.item_id),
        total_required_qty=Decimal("3"), net_required_qty=Decimal("3"),
        period_from=plan.period_from, period_to=plan.period_to, bom_level=0,
    )
    db.add(second_requirement)
    db.flush()
    second_reservation = models.ReservationEntry(
        ledger_generation_id=int(sealed_source.id), item_id=int(second_item.item_id),
        run_id=int(run.run_id), freeze_version=1,
        requirement_id=int(second_requirement.id),
        priority_period_from=plan.period_from, priority_period_to=plan.period_to,
        reserved_qty=Decimal("3"), covered_from_stock_at_freeze_qty=Decimal("0"),
        replenishment_required_qty=Decimal("3"), replenishment_received_qty=Decimal("0"),
        realized_qty=Decimal("0"), lifecycle_status="active", opened_at=CUTOFF,
    )
    db.add(second_reservation)
    db.flush()
    recovery_target = models.LedgerGeneration(
        generation_key="cf-recovery-target", status="building", cutoff=CUTOFF,
        source_watermarks={}, capabilities={},
        physical_import_batch_id=int(physical.id), algorithm_version="test",
    )
    db.add(recovery_target)
    db.flush()
    sealed_rows = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.ledger_generation_id == int(sealed_source.id),
        models.ReservationEntry.run_id == int(run.run_id),
    ).populate_existing().order_by(models.ReservationEntry.id.asc()).all()
    carried_recovery = carry_forward_retained_reservations(
        db, parent_generation_id=int(parent.id),
        target_generation_id=int(recovery_target.id),
        retained_run_ids=(int(run.run_id),), preserve_realization=False,
        source_current=True,
        recovery_source_generations={int(run.run_id): int(sealed_source.id)},
        expected_source_fingerprints={
            int(run.run_id): (
                *(_current_reservation_owner_fingerprint(row) for row in sealed_rows),
            )
        },
    )
    assert carried_recovery == 2

"""Contract of ``--phase freeze-basis-impact`` (decision §49 impact report)."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models
from tools.current_execution_migration import freeze_basis_impact

CUTOFF = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


def _engine():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    return engine


def _world(engine):
    """Two live owners frozen at the first batch: one credited with a receipt
    that batch already knew (stock at freeze), one with a receipt imported
    later although dated earlier (a backdated document - replenishment)."""
    with Session(engine) as session:
        batch = models.PhysicalImportBatch(
            batch_key="impact", status="completed", cutoff=CUTOFF,
            source_watermarks={}, source_complete=True, completed_at=CUTOFF,
        )
        later_batch = models.PhysicalImportBatch(
            batch_key="impact-later", status="completed", cutoff=CUTOFF,
            source_watermarks={}, source_complete=True, completed_at=CUTOFF,
        )
        session.add(batch)
        session.flush()
        session.add(later_batch)
        session.flush()
        generation = models.LedgerGeneration(
            generation_key="impact-pointer", status="accepted", cutoff=CUTOFF,
            accepted_at=CUTOFF, source_watermarks={}, capabilities={},
            physical_import_batch_id=int(batch.id), algorithm_version="impact-tests",
        )
        item = models.Item(item_code="IMPACT-1", item_name="Impact item")
        session.add_all([generation, item])
        session.flush()
        session.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
        result = {}
        for label, posted, imported in (
            ("before", CUTOFF - timedelta(days=1), batch),
            ("after", CUTOFF - timedelta(days=90), later_batch),
        ):
            run = models.PlanningRun(
                status="FIXED_SNAPSHOT", ledger_generation_id=generation.id,
                config_snapshot={}, active_freeze_version=1, ledger_cutoff=CUTOFF,
            )
            session.add(run)
            session.flush()
            requirement = models.MrpRequirement(
                run_id=run.run_id, item_id=item.item_id, total_required_qty=Decimal("5"),
                net_required_qty=Decimal("5"), period_from=date(2026, 10, 1),
                period_to=date(2026, 10, 31), bom_level=0, planning_stock_pool="default",
                characteristic_ref="", organization_ref="", freeze_version=1,
            )
            session.add(requirement)
            session.flush()
            owner = models.ReservationEntry(
                ledger_generation_id=generation.id, item_id=item.item_id, run_id=run.run_id,
                freeze_version=1, requirement_id=requirement.id,
                priority_period_from=date(2026, 10, 1), priority_period_to=date(2026, 10, 31),
                realization_mode="buy", reserved_qty=Decimal("5"),
                replenishment_required_qty=Decimal("5"), replenishment_received_qty=Decimal("2"),
                lifecycle_status="active", owner_kind="current", is_current=True,
                planning_stock_pool="default", characteristic_ref="", organization_ref="",
                current_identity=f"reservation:req:{int(requirement.id)}:mode:buy",
            )
            receipt = models.StockLedgerEntry(
                ingest_batch_id=int(imported.id), source_content_hash=f"impact-{label}".ljust(64, "0"),
                item_id=int(item.item_id), characteristic_ref="", organization_ref="",
                warehouse_ref1c="WH", qty=Decimal("2"), posting_at=posted,
                record_type="Receipt", recorder_type="Doc", recorder_ref=f"impact-{label}",
                line_no="1", ingest_source="seed",
            )
            session.add_all([owner, receipt])
            session.flush()
            session.add(models.MrpFreezeBaseline(
                run_id=int(run.run_id), freeze_version=1, item_id=int(item.item_id),
                characteristic_ref="", organization_ref="", planning_stock_pool="default",
                baseline_at=CUTOFF.replace(tzinfo=None), stock_qty=Decimal("7"),
                physical_import_batch_id=int(batch.id),
            ))
            session.add(models.ReservationConsumptionAllocation(
                ledger_generation_id=generation.id, reservation_id=owner.id,
                sle_id=receipt.id, requirement_id=requirement.id,
                allocated_qty=Decimal("2"), match_rule="fifo", fact_ref=f"impact-{label}",
                fact_line_ref="1", item_id=item.item_id, characteristic_ref="",
                organization_ref="", planning_stock_pool="default",
                idempotency_key=f"impact-{label}", allocation_role="replenishment_receipt",
                is_current=True, event_at=posted,
            ))
            result[label] = (int(run.run_id), int(owner.id))
        session.commit()
        return result


def test_the_impact_report_lists_what_the_freeze_baseline_will_retire(tmp_path):
    engine = _engine()
    world = _world(engine)
    csv_path = tmp_path / "impact.csv"

    report = freeze_basis_impact(engine, csv_path=str(csv_path))

    assert report["read_only"] is True
    run_id, owner_id = world["before"]
    assert [row["reservation_id"] for row in report["rows"]] == [owner_id]
    row = report["rows"][0]
    assert row == {
        "run_id": run_id, "item_id": row["item_id"], "reservation_id": owner_id,
        "baseline_at": row["baseline_at"], "freeze_batch_id": row["freeze_batch_id"],
        "boundary_source": "baseline", "revised_after_freeze_identities": 0,
        "allocations_to_retire": 1, "qty_to_retire": "2.000",
        "replenishment_required_qty": "5.000",
        "received_before": "2.000", "received_after": "0.000",
        "outstanding_before": "3.000", "outstanding_after": "5.000",
        "outstanding_delta": "2.000",
        "frozen_stock_qty": "7.000", "frozen_received_total": "0.000",
        "status": "ok",
    }
    assert report["revised_after_freeze_identities"] == 0
    # The retired 2 go back to FIFO.  The other live owner of the item still
    # needs 3, but it was frozen at the same batch and instant, so the
    # released receipt is its stock as well: nothing can land there (32d).
    assert report["increases"] == [{
        "item_id": row["item_id"], "released_qty": "2.000",
        "other_owners_outstanding": "3.000",
        "owners_for_which_the_fact_is_stock": 1,
        "estimated_reallocated_qty": "0.000", "estimate_kind": "upper_bound",
    }]
    assert report["runs"] == [{
        "run_id": run_id, "owners": 1, "allocations_to_retire": 1,
        "qty_to_retire": "2.000", "outstanding_delta": "2.000",
    }]
    with open(csv_path, encoding="utf-8") as handle:
        lines = list(csv.DictReader(handle))
    assert [int(line["reservation_id"]) for line in lines] == [owner_id]
    # Read-only: nothing was retired.
    with Session(engine) as session:
        assert session.query(models.ReservationConsumptionAllocation).filter_by(
            is_current=True
        ).count() == 2


def test_a_retiring_owner_frozen_before_a_released_fact_can_receive_it():
    """33c: receivers include owners that are themselves retiring facts.

    Owner X is frozen at batch 1 and retires the receipt that batch knew.
    Owner Y is frozen at batch 2 and retires the receipt imported by batch 2.
    X was frozen BEFORE Y's receipt became known, so that receipt is not
    X's stock and can land on X; X's receipt is Y's stock (excluded).
    """
    engine = _engine()
    with Session(engine) as session:
        batches = []
        for key in ("x", "y"):
            batch = models.PhysicalImportBatch(
                batch_key=f"retiring-{key}", status="completed", cutoff=CUTOFF,
                source_watermarks={}, source_complete=True, completed_at=CUTOFF,
            )
            session.add(batch)
            session.flush()
            batches.append(batch)
        generation = models.LedgerGeneration(
            generation_key="retiring-pointer", status="accepted", cutoff=CUTOFF,
            accepted_at=CUTOFF, source_watermarks={}, capabilities={},
            physical_import_batch_id=int(batches[-1].id), algorithm_version="impact-tests",
        )
        item = models.Item(item_code="IMPACT-R", item_name="Retiring receivers")
        session.add_all([generation, item])
        session.flush()
        session.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
        owners = {}
        for label, batch in (("x", batches[0]), ("y", batches[1])):
            run = models.PlanningRun(
                status="FIXED_SNAPSHOT", ledger_generation_id=generation.id,
                config_snapshot={}, active_freeze_version=1, ledger_cutoff=CUTOFF,
            )
            session.add(run)
            session.flush()
            requirement = models.MrpRequirement(
                run_id=run.run_id, item_id=item.item_id, total_required_qty=Decimal("5"),
                net_required_qty=Decimal("5"), period_from=date(2026, 10, 1),
                period_to=date(2026, 10, 31), bom_level=0, planning_stock_pool="default",
                characteristic_ref="", organization_ref="", freeze_version=1,
            )
            session.add(requirement)
            session.flush()
            owner = models.ReservationEntry(
                ledger_generation_id=generation.id, item_id=item.item_id, run_id=run.run_id,
                freeze_version=1, requirement_id=requirement.id,
                priority_period_from=date(2026, 10, 1), priority_period_to=date(2026, 10, 31),
                realization_mode="buy", reserved_qty=Decimal("5"),
                replenishment_required_qty=Decimal("5"), replenishment_received_qty=Decimal("2"),
                lifecycle_status="active", owner_kind="current", is_current=True,
                planning_stock_pool="default", characteristic_ref="", organization_ref="",
                current_identity=f"reservation:req:{int(requirement.id)}:mode:buy",
            )
            receipt = models.StockLedgerEntry(
                ingest_batch_id=int(batch.id), source_content_hash=f"retiring-{label}".ljust(64, "0"),
                item_id=int(item.item_id), characteristic_ref="", organization_ref="",
                warehouse_ref1c="WH", qty=Decimal("2"), posting_at=CUTOFF - timedelta(days=1),
                record_type="Receipt", recorder_type="Doc", recorder_ref=f"retiring-{label}",
                line_no="1", ingest_source="seed",
            )
            session.add_all([owner, receipt])
            session.flush()
            session.add(models.MrpFreezeBaseline(
                run_id=int(run.run_id), freeze_version=1, item_id=int(item.item_id),
                characteristic_ref="", organization_ref="", planning_stock_pool="default",
                baseline_at=CUTOFF.replace(tzinfo=None), stock_qty=Decimal("7"),
                physical_import_batch_id=int(batch.id),
            ))
            session.add(models.ReservationConsumptionAllocation(
                ledger_generation_id=generation.id, reservation_id=owner.id,
                sle_id=receipt.id, requirement_id=requirement.id,
                allocated_qty=Decimal("2"), match_rule="fifo", fact_ref=f"retiring-{label}",
                fact_line_ref="1", item_id=item.item_id, characteristic_ref="",
                organization_ref="", planning_stock_pool="default",
                idempotency_key=f"retiring-{label}", allocation_role="replenishment_receipt",
                is_current=True, event_at=CUTOFF - timedelta(days=1),
            ))
            owners[label] = int(owner.id)
        session.commit()

    report = freeze_basis_impact(engine)

    assert sorted(row["reservation_id"] for row in report["rows"]) == sorted(owners.values())
    # Both owners retire 2 and then need 5.  Y's receipt can land on X (X was
    # frozen before it was imported); X's receipt is stock at Y's freeze.
    assert report["increases"] == [{
        "item_id": report["rows"][0]["item_id"], "released_qty": "4.000",
        "other_owners_outstanding": "10.000",
        "owners_for_which_the_fact_is_stock": 1,
        "estimated_reallocated_qty": "2.000", "estimate_kind": "upper_bound",
    }]

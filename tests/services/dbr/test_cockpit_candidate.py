from datetime import date, datetime
from decimal import Decimal

import pytest

from app import models
from app.services.dbr.cockpit_candidate import (
    DbrCockpitCandidateError,
    build_cockpit_candidate_snapshot,
)
from app.services.dbr.policy_snapshot import build_policy_candidate_snapshot
from app.services.obligation_refresh_manifest import MANIFEST_KEY


CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "execution_allocations": True,
    "planning_snapshots": True,
}


def _ready(db):
    batch = models.PhysicalImportBatch(
        batch_key="cockpit-candidate-batch", status="completed", cutoff=datetime(2026, 8, 1),
        source_watermarks={}, completed_at=datetime(2026, 8, 1),
    )
    generation = models.LedgerGeneration(
        generation_key="cockpit-candidate", status="building", cutoff=datetime(2026, 8, 1),
        source_watermarks={"generation_kind": "obligation_refresh"}, capabilities=CAPABILITIES,
        algorithm_version="test", replay_version="test", physical_import_batch=batch,
    )
    category = models.ItemCategory(category_name="PURCHASE")
    db.add(category)
    db.flush()
    item = models.Item(
        item_code="COCKPIT-ITEM",
        item_name="Captured item",
        optimal_batch=Decimal("8"),
        replenishment_method="Покупка",
        category_id=category.category_id,
    )
    db.add_all([
        generation,
        item,
        models.DbrSettings(
            id=1,
            w2_warehouse_ref1c="W2",
            w3_warehouse_ref1c="W3",
            w4_warehouse_ref1c="W4",
        ),
        models.DbrCategorySupplyRisk(
            item_group="PURCHASE",
            receipt_warehouse_ref1c="W3",
            supply_risk_pct=0,
        ),
    ])
    db.flush()
    run = models.PlanningRun(
        status="BUILDING_SNAPSHOT", config_snapshot={}, ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff, active_freeze_version=1,
        period_from=date(2026, 8, 3), period_to=date(2026, 8, 4),
    )
    db.add(run)
    db.flush()
    generation.source_watermarks = {
        "generation_kind": "obligation_refresh",
        MANIFEST_KEY: {"entries": [{"candidate_run_id": run.run_id}], "add_request": {
            "planning_pool_by_warehouse": {
                "W2": "w2",
                "W3": "main",
                "W4": "w4",
            },
        }},
    }
    requirement = models.MrpRequirement(
        run_id=run.run_id, item_id=item.item_id, freeze_version=1, planning_stock_pool="main",
        total_required_qty=Decimal("10"), net_required_qty=Decimal("10"),
        period_from=run.period_from, period_to=run.period_to,
    )
    db.add(requirement)
    db.flush()
    db.add_all([
        models.MrpRequirementBucket(requirement_id=requirement.id, run_id=run.run_id, item_id=item.item_id,
                                    bucket_date=date(2026, 8, 3), gross_qty=Decimal("10"), net_qty=Decimal("10")),
        models.WorkCalendarDay(date=date(2026, 8, 3), is_workday=True),
        models.WorkCalendarDay(date=date(2026, 8, 4), is_workday=False),
        models.StockBin(ledger_generation_id=generation.id, item_id=item.item_id, characteristic_ref="", organization_ref="",
                        warehouse_ref1c="W3", on_hand=Decimal("10"), reconcile_pending_qty=0),
        models.ReservationEntry(ledger_generation_id=generation.id, item_id=item.item_id, characteristic_ref="", organization_ref="",
                                planning_stock_pool="main", run_id=run.run_id, freeze_version=1, requirement_id=requirement.id,
                                priority_period_from=run.period_from, priority_period_to=run.period_to,
                                realization_mode="consume", reserved_qty=Decimal("4"), realized_qty=Decimal("0"),
                                uncovered_qty=Decimal("2"), lifecycle_status="active"),
    ])
    build_batch = models.LedgerBuildBatch(ledger_generation_id=generation.id, stage="snapshot_build",
        batch_key="cockpit-supply", status="completed", algorithm_version="test", metrics={}, completed_at=datetime(2026, 8, 1))
    db.add(build_batch)
    db.flush()
    db.add(models.LedgerFutureSupply(
        ledger_generation_id=generation.id, supply_kind="supplier_order", item_id=item.item_id,
        characteristic_ref="", organization_ref="", planning_stock_pool="main", destination_warehouse_ref1c="W3",
        source_ref="PO-1", source_line_ref="1", ordered_qty_at_cutoff=Decimal("3"), realized_qty_at_cutoff=0,
        open_qty_at_cutoff=Decimal("3"), eta_date=date(2026, 8, 5), source_state_key="open",
        capture_cutoff=generation.cutoff, source_content_hash="supply", capture_batch_id=build_batch.id, evidence_status="exact",
    ))
    db.flush()
    build_policy_candidate_snapshot(db, generation.id)
    return generation, run, item


def test_candidate_uses_only_generation_ledger_values_and_exact_lineage(db_session):
    generation, run, item = _ready(db_session)
    # This legacy mirror must not influence either NFP or a saved signal.
    db_session.add(models.ItemWarehouseStock(item_id=item.item_id, warehouse_ref1c="W3", qty=Decimal("999")))
    db_session.flush()
    snapshot = build_cockpit_candidate_snapshot(db_session, generation.id)
    assert snapshot.truth_status == "building"
    assert snapshot.payload["meta"]["runs"] == [{"run_id": run.run_id, "freeze_version": 1}]
    position = snapshot.payload["positions"][0]
    assert position["live_nfp"]["stock_qty"] == 10.0
    assert position["live_nfp"]["open_supply_qty"] == 3.0
    assert position["live_nfp"]["qualified_demand_qty"] == 4.0
    assert position["live_nfp"]["nfp"] == 9.0
    assert snapshot.payload["deficits"]["items"][0]["deficit_qty"] == 2.0


def test_candidate_declares_sections_without_exact_target_inputs_unavailable(db_session):
    generation, _, _ = _ready(db_session)
    snapshot = build_cockpit_candidate_snapshot(db_session, generation.id)
    assert snapshot.payload["meta"]["unavailable_sections"] == ["under_schedule", "processing_board"]
    assert snapshot.payload["processing_board"]["status"] == "unavailable"


def test_candidate_retry_is_idempotent_and_changed_ledger_conflicts(db_session):
    generation, _, item = _ready(db_session)
    first = build_cockpit_candidate_snapshot(db_session, generation.id)
    assert build_cockpit_candidate_snapshot(db_session, generation.id).id == first.id
    bin_row = db_session.query(models.StockBin).filter_by(ledger_generation_id=generation.id, item_id=item.item_id).one()
    bin_row.on_hand = Decimal("11")
    db_session.flush()
    with pytest.raises(DbrCockpitCandidateError, match="conflicts"):
        build_cockpit_candidate_snapshot(db_session, generation.id)

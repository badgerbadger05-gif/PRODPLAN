from datetime import date, datetime
from decimal import Decimal

import pytest

from app import models
from app.services.dbr.policy_snapshot import (
    DbrPolicySnapshotError,
    build_policy_candidate_snapshot,
)
from app.services.obligation_refresh_manifest import MANIFEST_KEY


def _generation(db):
    batch = models.PhysicalImportBatch(
        batch_key="policy-candidate-batch", status="completed", cutoff=datetime(2026, 8, 1),
        source_watermarks={}, completed_at=datetime(2026, 8, 1),
    )
    generation = models.LedgerGeneration(
        generation_key="policy-candidate", status="building", cutoff=datetime(2026, 8, 1),
        source_watermarks={"generation_kind": "obligation_refresh"}, capabilities={},
        algorithm_version="test", replay_version="test", physical_import_batch=batch,
    )
    db.add(generation)
    db.flush()
    return generation


def _ready(db):
    generation = _generation(db)
    item = models.Item(item_code="POLICY-ITEM", item_name="Policy item", optimal_batch=Decimal("12"))
    db.add_all([
        item,
        models.DbrSettings(
            id=1,
            w2_warehouse_ref1c="W2",
            w3_warehouse_ref1c="W3",
            w4_warehouse_ref1c="W4",
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
                "W3": "w3",
                "W4": "main",
            },
        }},
    }
    db.add(models.MrpRequirement(
        run_id=run.run_id, item_id=item.item_id, freeze_version=1, planning_stock_pool="main",
        total_required_qty=1, net_required_qty=1, period_from=run.period_from, period_to=run.period_to,
    ))
    db.add(models.StockBin(
        ledger_generation_id=generation.id, item_id=item.item_id, characteristic_ref="", organization_ref="",
        warehouse_ref1c="W3", on_hand=Decimal("3"), reconcile_pending_qty=0,
    ))
    db.add_all([
        models.WorkCalendarDay(date=date(2026, 8, 3), is_workday=True),
        models.WorkCalendarDay(date=date(2026, 8, 4), is_workday=False),
    ])
    db.flush()
    return generation, item


def test_policy_snapshot_is_idempotent_and_rejects_changed_input(db_session):
    generation, item = _ready(db_session)
    first = build_policy_candidate_snapshot(db_session, generation.id)
    assert build_policy_candidate_snapshot(db_session, generation.id).id == first.id
    assert first.payload["w3_stock_item_ids"] == [item.item_id]
    assert "stock_qty" not in first.payload["items"][0]
    db_session.get(models.DbrSettings, 1).frozen_days = 9
    with pytest.raises(DbrPolicySnapshotError, match="conflicts"):
        build_policy_candidate_snapshot(db_session, generation.id)


def test_policy_snapshot_uses_generation_stock_not_legacy_item_warehouse_stock(db_session):
    generation, item = _ready(db_session)
    db_session.add(models.ItemWarehouseStock(item_id=item.item_id, warehouse_ref1c="W3", qty=Decimal("999")))
    db_session.flush()
    snapshot = build_policy_candidate_snapshot(db_session, generation.id)
    assert snapshot.payload["w3_stock_item_ids"] == [item.item_id]


@pytest.mark.parametrize("mutation", ["calendar", "mapping"])
def test_policy_snapshot_fails_closed_for_missing_calendar_or_mapping(db_session, mutation):
    generation, _ = _ready(db_session)
    if mutation == "calendar":
        db_session.query(models.WorkCalendarDay).filter(models.WorkCalendarDay.date == date(2026, 8, 4)).delete()
        expected = "calendar"
    else:
        generation.source_watermarks[MANIFEST_KEY]["add_request"]["planning_pool_by_warehouse"] = {}
        expected = "planning_pool"
    db_session.flush()
    with pytest.raises(DbrPolicySnapshotError, match=expected):
        build_policy_candidate_snapshot(db_session, generation.id)


def test_policy_snapshot_fails_closed_for_non_workday_bucket_and_mixed_freeze(db_session):
    generation, item = _ready(db_session)
    run_id = generation.source_watermarks[MANIFEST_KEY]["entries"][0]["candidate_run_id"]
    requirement = db_session.query(models.MrpRequirement).filter_by(run_id=run_id).one()
    db_session.add(models.MrpRequirementBucket(
        requirement_id=requirement.id, run_id=run_id, item_id=item.item_id,
        bucket_date=date(2026, 8, 4), gross_qty=1, net_qty=1,
    ))
    db_session.flush()
    with pytest.raises(DbrPolicySnapshotError, match="work calendar"):
        build_policy_candidate_snapshot(db_session, generation.id)
    db_session.query(models.MrpRequirementBucket).delete()
    requirement.freeze_version = 2
    db_session.flush()
    with pytest.raises(DbrPolicySnapshotError, match="mixed"):
        build_policy_candidate_snapshot(db_session, generation.id)

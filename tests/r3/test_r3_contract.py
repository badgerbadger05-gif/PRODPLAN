from datetime import datetime, timezone

import pytest

from app import models
from app.services.item_ledger.r3_contract import (
    ImportCompletenessError,
    business_identity_for_cutoff_balance_adjustment,
    business_identity_for_movement,
    current_live_run,
    finalize_import,
    record_import_page,
)


def test_business_identity_is_stable_across_repeated_generation_rows():
    key = business_identity_for_movement("Документ", "ref-1", "7")
    assert key == business_identity_for_movement("Документ", "ref-1", "7")
    assert key != business_identity_for_movement("Документ", "ref-1", "8")


def test_cutoff_adjustment_identity_includes_the_physical_cell():
    first = business_identity_for_cutoff_balance_adjustment(
        "snap-ref", "0", item_id=10, characteristic_ref="",
        organization_ref="org-a", warehouse_ref1c="wh-a",
        snap_content_hash="a" * 64,
    )
    second = business_identity_for_cutoff_balance_adjustment(
        "snap-ref", "0", item_id=11, characteristic_ref="",
        organization_ref="org-a", warehouse_ref1c="wh-a",
        snap_content_hash="a" * 64,
    )
    assert first == business_identity_for_cutoff_balance_adjustment(
        "snap-ref", "0", item_id=10, characteristic_ref="",
        organization_ref="org-a", warehouse_ref1c="wh-a",
        snap_content_hash="a" * 64,
    )
    assert first != second
    later_snap = business_identity_for_cutoff_balance_adjustment(
        "snap-ref", "0", item_id=10, characteristic_ref="",
        organization_ref="org-a", warehouse_ref1c="wh-a",
        snap_content_hash="b" * 64,
    )
    assert first != later_snap
    assert first == business_identity_for_cutoff_balance_adjustment(
        "snap-ref", "0", item_id=10, characteristic_ref="",
        organization_ref="org-a", warehouse_ref1c="wh-a",
        snap_content_hash="a" * 64,
    )
    assert len(first) <= 256
    maximal = business_identity_for_cutoff_balance_adjustment(
        "r" * 64,
        "l" * 32,
        item_id=10,
        characteristic_ref="c" * 36,
        organization_ref="o" * 36,
        warehouse_ref1c="w" * 36,
        snap_content_hash="h" * 64,
    )
    assert len(maximal) <= 256


def test_partial_reordered_or_missing_pages_never_complete_import(db_session):
    batch = models.PhysicalImportBatch(
        batch_key="r3-pages", status="building", source_watermarks={}
    )
    db_session.add(batch)
    db_session.flush()
    record_import_page(db_session, batch.id, 1, 3, "page-1")
    record_import_page(db_session, batch.id, 3, 3, "page-3")
    with pytest.raises(ImportCompletenessError):
        finalize_import(db_session, batch.id)
    assert db_session.get(models.PhysicalImportBatch, batch.id).status == "building"


def test_page_reordering_is_rejected_even_when_all_pages_exist(db_session):
    batch = models.PhysicalImportBatch(
        batch_key="r3-reordered", status="building", source_watermarks={}
    )
    db_session.add(batch)
    db_session.flush()
    record_import_page(db_session, batch.id, 2, 2, "page-2")
    record_import_page(db_session, batch.id, 1, 2, "page-1")
    with pytest.raises(ImportCompletenessError, match="order"):
        finalize_import(db_session, batch.id)


def test_live_pointer_read_does_not_walk_prior_run_chain(db_session):
    plan = models.ProductionPlanHeader(
        name="R3", period_from=datetime(2026, 9, 1).date(),
        period_to=datetime(2026, 9, 30).date(), status="fixed",
    )
    db_session.add(plan)
    db_session.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT", config_snapshot={}, source_plan_id=plan.id,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(models.PlanningLivePointer(plan_id=plan.id, run_id=run.run_id))
    db_session.commit()
    assert current_live_run(db_session, plan.id).run_id == run.run_id


def test_frozen_basis_has_explicit_generation_provenance(db_session):
    assert hasattr(models.MrpFreezeBaseline, "frozen_basis_generation_id")

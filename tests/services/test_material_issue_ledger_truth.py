"""P0: material issues may only use the current accepted Ledger generation."""

from datetime import datetime, timezone

import pytest

from app import models
from app.services import planning_truth
from app.services import production_control_material_issues as issues
from app.services.one_c_stock_transfer_export import export_material_issues_to_1c


def _accepted(db, key="one"):
    cutoff = datetime(2026, 7, 23, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(batch_key=f"mi-{key}", status="completed", cutoff=cutoff, source_watermarks={})
    generation = models.LedgerGeneration(
        generation_key=f"mi-{key}", status="accepted", cutoff=cutoff, accepted_at=cutoff,
        physical_import_batch=batch, source_watermarks={}, capabilities={}, algorithm_version="test",
    )
    db.add_all((batch, generation)); db.flush()
    planning_truth.publish_generation(db, generation)
    return generation


def _two_generation_bins(db_session, *, foreign_is_current):
    generation = _accepted(db_session)
    item = models.Item(item_code="MI-LEDGER", item_name="Ledger item")
    db_session.add_all((item, models.StockWarehouse(warehouse_ref1c="BIN", warehouse_name="BIN", is_selected=True),
                        models.StockWarehouse(warehouse_ref1c="LEGACY", warehouse_name="LEGACY", is_selected=True)))
    db_session.flush()
    foreign_batch = models.PhysicalImportBatch(
        batch_key="mi-foreign",
        status="completed",
        cutoff=datetime(2026, 7, 23, tzinfo=timezone.utc),
        source_watermarks={},
    )
    foreign_generation = models.LedgerGeneration(
        generation_key="mi-foreign",
        status="accepted",
        cutoff=datetime(2026, 7, 22, tzinfo=timezone.utc),
        accepted_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        physical_import_batch=foreign_batch,
        source_watermarks={},
        capabilities={},
        algorithm_version="test",
    )
    db_session.add_all((foreign_batch, foreign_generation))
    db_session.flush()
    db_session.add_all([
        models.StockBin(
            ledger_generation_id=generation.id,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c="BIN",
            on_hand=4,
        ),
        models.StockBin(
            ledger_generation_id=foreign_generation.id,
            item_id=item.item_id,
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c="LEGACY",
            on_hand=99,
            is_current=foreign_is_current,
        ),
    ])
    db_session.flush()
    return generation, item


def test_source_selection_uses_the_current_stockbin_set_only(db_session):
    """Canon R6: membership is ``is_current``; the generation is provenance."""
    generation, item = _two_generation_bins(db_session, foreign_is_current=False)
    options = issues._source_warehouse_options(db_session, [item.item_id], ledger_generation_id=generation.id)
    assert options[item.item_id] == [{"ref1c": "BIN", "name": "BIN", "qty": 4.0}]


def test_source_selection_fails_closed_on_a_current_bin_of_an_unrelated_generation(db_session):
    """A current bin outside the pointer's lineage is an ambiguous projection.

    It used to be silently filtered out by ``ledger_generation_id ==
    pointer``, the same filter that dropped every valid bin after an
    obligation refresh.  The canonical current read refuses it instead.
    """
    generation, item = _two_generation_bins(db_session, foreign_is_current=True)
    with pytest.raises(ValueError, match="stale or ambiguous"):
        issues._source_warehouse_options(db_session, [item.item_id], ledger_generation_id=generation.id)


def test_create_fails_closed_without_accepted_truth(db_session):
    with pytest.raises(planning_truth.PlanningTruthUnavailable):
        issues.create_material_issues(db_session, [])


@pytest.mark.parametrize("ledger_generation_id", [None, 999999])
def test_export_rejects_null_or_foreign_issue_lineage_even_dry_run(db_session, ledger_generation_id):
    _accepted(db_session, "export")
    issue = models.ProductionMaterialIssue(
        document_number=f"MI-{ledger_generation_id}", product_id=1, order_id=1,
        ledger_generation_id=ledger_generation_id,
    )
    db_session.add(issue); db_session.flush()
    with pytest.raises(ValueError, match="not current accepted truth"):
        export_material_issues_to_1c(db_session, [issue.issue_id], dry_run=True)


def test_a_building_reader_must_descend_from_the_truth_pointer(db_session):
    """A candidate forked from anything but the pointer cannot read current bins."""
    pointer = _accepted(db_session, "pointer")
    stray_parent = _accepted(db_session, "stray")  # publish_generation moved the pointer
    planning_truth.publish_generation(db_session, pointer)
    cutoff = datetime(2026, 7, 24, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(batch_key="mi-building", status="completed", cutoff=cutoff, source_watermarks={})
    building = models.LedgerGeneration(
        generation_key="mi-building", status="building", cutoff=cutoff,
        physical_import_batch=batch, capabilities={}, algorithm_version="test",
        source_watermarks={"parent_generation_id": int(stray_parent.id)},
    )
    db_session.add_all((batch, building)); db_session.flush()
    with pytest.raises(ValueError, match="not a child of the truth pointer"):
        issues._source_warehouse_options(db_session, [1], ledger_generation_id=building.id)
    building.source_watermarks = {"parent_generation_id": int(pointer.id)}
    db_session.flush()
    assert issues._source_warehouse_options(db_session, [1], ledger_generation_id=building.id) == {}

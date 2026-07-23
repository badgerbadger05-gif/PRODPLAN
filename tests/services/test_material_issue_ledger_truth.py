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


def test_source_selection_ignores_foreign_item_warehouse_stock(db_session):
    generation = _accepted(db_session)
    item = models.Item(item_code="MI-LEDGER", item_name="Ledger item")
    db_session.add_all((item, models.StockWarehouse(warehouse_ref1c="BIN", warehouse_name="BIN", is_selected=True),
                        models.StockWarehouse(warehouse_ref1c="LEGACY", warehouse_name="LEGACY", is_selected=True)))
    db_session.flush()
    db_session.add(models.ItemWarehouseStock(item_id=item.item_id, warehouse_ref1c="LEGACY", qty=99))
    db_session.add(models.StockBin(ledger_generation_id=generation.id, item_id=item.item_id,
                                   characteristic_ref="", organization_ref="", warehouse_ref1c="BIN", on_hand=4))
    db_session.flush()
    options = issues._source_warehouse_options(db_session, [item.item_id], ledger_generation_id=generation.id)
    assert options[item.item_id] == [{"ref1c": "BIN", "name": "BIN", "qty": 4.0}]


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

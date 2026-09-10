from datetime import date, datetime
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger import LedgerKey, seed_from_balance
from app.services.item_ledger.ingest import pull_recorder_movements
from app.services.item_ledger.physical_visibility import (
    PhysicalVisibilityError,
    require_import_batch,
)
from app.services.item_ledger.r3_contract import (
    ImportCompletenessError,
    current_live_run,
    record_successor,
    set_live_pointer,
    validate_legacy_identity_mapping,
)
from app.services.mrp_freeze import _write_freeze_baseline


class _RecorderClient:
    def __init__(self, qty):
        self.qty = qty

    def get_all(self, entity_name, filter_query=None, order_by=None, **kwargs):
        return [{
            "Recorder": "r3-doc",
            "Recorder_Type": "AccumulationRecordType",
            "RecordSet": [{
                "Period": "2026-09-10T10:00:00",
                "LineNumber": "1",
                "Active": True,
                "RecordType": "Receipt",
                "Организация_Key": "org",
                "Номенклатура_Key": "item-r3",
                "Характеристика_Key": "00000000-0000-0000-0000-000000000000",
                "СтруктурнаяЕдиница_Key": "wh-r3",
                "Количество": self.qty,
            }],
        }]


def _lineage(db_session):
    item = models.Item(item_code="R3", item_name="R3", item_ref1c="item-r3")
    warehouse = models.StockWarehouse(warehouse_ref1c="wh-r3", warehouse_name="R3")
    batch = models.PhysicalImportBatch(
        batch_key="r3-runtime-batch", status="completed", source_watermarks={}
    )
    generation = models.LedgerGeneration(
        generation_key="r3-runtime-generation", status="building",
        cutoff=datetime(2026, 9, 10), source_watermarks={}, capabilities={},
        physical_import_batch=batch, algorithm_version="r3-test",
    )
    db_session.add_all([item, warehouse, generation])
    db_session.flush()
    return item, generation


def test_seed_repeat_100_preserves_one_id_and_identity_mapping(db_session):
    item, _generation = _lineage(db_session)
    key = LedgerKey(item.item_id, "", "org", "wh-r3")
    for _ in range(100):
        seed_from_balance(
            db_session, {key: Decimal("5")},
            anchor_period=date(2026, 9, 1),
        )
    rows = db_session.query(models.StockLedgerEntry).all()
    mappings = db_session.query(models.StockLedgerBusinessIdentityMap).all()
    assert len(rows) == len(mappings) == 1
    assert rows[0].id == mappings[0].stock_ledger_entry_id
    assert rows[0].business_identity


def test_new_source_version_creates_one_correction_edge_and_two_mappings(db_session):
    _lineage(db_session)
    first = pull_recorder_movements(
        db_session, "Document_R3", "r3-doc", client=_RecorderClient(5),
    )
    second = pull_recorder_movements(
        db_session, "Document_R3", "r3-doc", client=_RecorderClient(8),
    )
    assert first.inserted == 1 and second.inserted == 1
    assert db_session.query(models.StockLedgerFactSupersession).count() == 1
    assert db_session.query(models.StockLedgerBusinessIdentityMap).count() == 2


def test_incomplete_batch_is_not_visible_until_finalize(db_session):
    batch = models.PhysicalImportBatch(
        batch_key="r3-incomplete", status="completed", source_complete=False,
        expected_page_count=2, received_page_count=1, source_watermarks={},
    )
    db_session.add(batch)
    db_session.commit()
    with pytest.raises(PhysicalVisibilityError, match="complete"):
        require_import_batch(db_session, batch.id)


def test_pointer_and_successor_are_idempotent_and_direct(db_session):
    plan = models.ProductionPlanHeader(
        name="R3 pointer", period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30), status="fixed",
    )
    first = models.PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={}, source_plan_id=1)
    second = models.PlanningRun(status="BUILDING_SNAPSHOT", config_snapshot={}, source_plan_id=1)
    db_session.add(plan)
    db_session.flush()
    first.source_plan_id = plan.id
    second.source_plan_id = plan.id
    db_session.add_all([first, second])
    db_session.flush()
    set_live_pointer(db_session, plan.id, first.run_id)
    record_successor(db_session, plan.id, first.run_id, second.run_id, reason="rebase")
    record_successor(db_session, plan.id, first.run_id, second.run_id, reason="rebase")
    set_live_pointer(db_session, plan.id, second.run_id)
    db_session.commit()
    assert current_live_run(db_session, plan.id).run_id == second.run_id
    assert db_session.query(models.PlanningRunSuccessor).count() == 1


def test_legacy_mapping_preflight_rejects_ambiguous_active_duplicates(db_session):
    with pytest.raises(ValueError, match="active duplicate"):
        validate_legacy_identity_mapping([{"business_identity": "x", "active": True}, {"business_identity": "x", "active": True}])


def test_freeze_writer_stamps_generation_provenance(db_session):
    item, generation = _lineage(db_session)
    run = models.PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={})
    db_session.add(run)
    db_session.flush()
    _write_freeze_baseline(
        db_session, run, 1, [item.item_id], {item.item_id: 2}, datetime(2026, 9, 10),
        baseline_at=datetime(2026, 9, 10), physical_import_batch_id=generation.physical_import_batch_id,
        frozen_basis_generation_id=generation.id,
    )
    db_session.flush()
    row = db_session.query(models.MrpFreezeBaseline).one()
    assert row.frozen_basis_generation_id == generation.id

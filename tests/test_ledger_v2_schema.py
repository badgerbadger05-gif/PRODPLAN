"""Schema test for MRP execution ledger v2 (Increment 1, additive only).

Creates one minimal row in each new ledger table and asserts server-side
defaults apply (drift_adjustment_qty==0, realized/evaporated==0, matured==False,
etc.). No business logic is exercised — only defaults from create_all + insert.
"""

import datetime

from app import models


def _generation(db_session):
    imported = models.PhysicalImportBatch(
        batch_key="ledger-v2-physical",
        status="completed",
        source_watermarks={"fixture": "ledger-v2"},
        completed_at=datetime.datetime(2026, 1, 1),
    )
    generation = models.LedgerGeneration(
        generation_key="ledger-v2-generation",
        status="building",
        source_watermarks={},
        capabilities={},
        physical_import_batch=imported,
        algorithm_version="tests/1",
    )
    db_session.add(generation)
    db_session.flush()
    return generation


def _mk_item_run_req(db_session):
    item = models.Item(item_code="LEDGER-V2", item_name="Ledger V2 Test")
    db_session.add(item)
    db_session.flush()

    run = models.PlanningRun(config_snapshot={})
    db_session.add(run)
    db_session.flush()

    req = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        period_from=datetime.date(2026, 1, 1),
        period_to=datetime.date(2026, 1, 31),
    )
    db_session.add(req)
    db_session.flush()
    return item, run, req


def test_mrp_requirement_v2_column_defaults(db_session):
    _item, run, req = _mk_item_run_req(db_session)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(run)

    # New MrpRequirement pool/freeze columns default correctly.
    assert req.freeze_version is None
    assert float(req.drift_adjustment_qty) == 0
    assert req.characteristic_ref is None
    assert req.organization_ref is None
    assert req.planning_stock_pool is None
    # New PlanningRun column.
    assert run.active_freeze_version is None


def test_freeze_baseline_defaults(db_session):
    item, run, _req = _mk_item_run_req(db_session)
    row = models.MrpFreezeBaseline(run_id=run.run_id, freeze_version=1, item_id=item.item_id)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)

    assert float(row.stock_qty) == 0
    assert float(row.produced_total) == 0
    assert float(row.received_total) == 0
    assert float(row.unit_coef) == 1
    assert row.frozen_at is not None


def test_freeze_allocation_defaults(db_session):
    item, run, req = _mk_item_run_req(db_session)
    row = models.MrpFreezeAllocation(
        run_id=run.run_id, freeze_version=1, requirement_id=req.id, item_id=item.item_id,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)

    assert row.source_type == ""
    assert row.source_ref == ""
    assert row.source_line_ref == ""
    assert float(row.alloc_qty) == 0
    assert float(row.fact_at_freeze) == 0
    assert float(row.realized_qty) == 0
    assert float(row.evaporated_qty) == 0


def test_freeze_component_defaults(db_session):
    item, run, _req = _mk_item_run_req(db_session)
    row = models.MrpFreezeComponent(
        run_id=run.run_id, freeze_version=1,
        parent_item_id=item.item_id, component_item_id=item.item_id,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)

    assert row.spec_ref == ""
    assert row.spec_version is None
    assert float(row.norm_qty_per_unit) == 0
    assert float(row.unit_coef) == 1


def test_execution_allocation_defaults(db_session):
    _item, _run, req = _mk_item_run_req(db_session)
    generation = _generation(db_session)
    row = models.MrpExecutionAllocation(
        ledger_generation_id=generation.id,
        requirement_id=req.id,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)

    assert row.cycle_id == ""
    assert row.fact_type == ""
    assert row.allocation_kind == ""
    assert row.fact_ref == ""
    assert row.fact_line_ref == ""
    assert float(row.allocated_qty) == 0
    assert row.bucket_id is None
    assert row.freeze_allocation_id is None
    assert row.origin_requirement_id is None
    assert row.calculated_at is not None


def test_requirement_carry_defaults(db_session):
    item, _run, req = _mk_item_run_req(db_session)
    row = models.MrpRequirementCarry(
        source_requirement_id=req.id, target_requirement_id=req.id, item_id=item.item_id,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)

    assert float(row.carried_qty) == 0
    assert row.carried_at is None
    assert row.operator is None
    assert row.source_run_id is None
    assert row.target_run_id is None


def test_drift_event_defaults(db_session):
    item, _run, _req = _mk_item_run_req(db_session)
    row = models.MrpDriftEvent(item_id=item.item_id)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)

    assert row.cycle_id == ""
    assert row.kind == ""
    assert float(row.drift_qty) == 0
    assert row.expected_stock is None
    assert row.actual_stock is None
    assert row.matured is False
    assert row.first_seen_cycle_id is None
    assert row.requirement_id is None
    assert row.details is None

from datetime import date, datetime
import importlib.util
from pathlib import Path

import pytest

from app import models
from app.services.mrp_reconciliation import (
    _create_catchup_product,
    _own_open_production_by_item,
    _own_purchase_coverage,
)


def _generation(db, suffix):
    batch = models.PhysicalImportBatch(
        batch_key=f"proposal-batch-{suffix}",
        status="completed",
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=f"proposal-generation-{suffix}",
        status="accepted",
        cutoff=datetime(2026, 7, 23),
        source_watermarks={},
        capabilities={},
        physical_import_batch=batch,
        algorithm_version="test/proposal-lineage",
        accepted_at=datetime(2026, 7, 23),
    )
    db.add(generation)
    db.flush()
    return generation


def _run_item_requirement(db):
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
    )
    item = models.Item(item_code="PROP-LINEAGE", item_name="Proposal lineage")
    db.add_all([run, item])
    db.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=10,
        net_required_qty=10,
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
    )
    db.add(requirement)
    db.flush()
    return run, item, requirement


def _purchase(run, item, requirement, generation, qty):
    return models.PlannedPurchase(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=qty,
        planned_qty=qty,
        qty=qty,
        need_date=date(2026, 7, 31),
        order_date=date(2026, 7, 20),
        lead_time_days=10,
        bucket_date=date(2026, 7, 31),
        source_mrp_requirement_id=requirement.id,
        ledger_generation_id=generation.id,
    )


def test_purchase_coverage_isolated_between_generations(db_session):
    run, item, requirement = _run_item_requirement(db_session)
    generation_a = _generation(db_session, "a")
    generation_b = _generation(db_session, "b")
    db_session.add_all([
        _purchase(run, item, requirement, generation_a, 3),
        _purchase(run, item, requirement, generation_b, 7),
        models.PlannedPurchase(
            run_id=run.run_id,
            item_id=item.item_id,
            requested_qty=99,
            planned_qty=99,
            qty=99,
            need_date=date(2026, 7, 31),
            order_date=date(2026, 7, 20),
            lead_time_days=10,
            bucket_date=date(2026, 7, 31),
            source_mrp_requirement_id=requirement.id,
            ledger_generation_id=None,
        ),
    ])
    db_session.flush()

    _, local_a, _ = _own_purchase_coverage(
        db_session, run, ledger_generation_id=generation_a.id
    )
    _, local_b, _ = _own_purchase_coverage(
        db_session, run, ledger_generation_id=generation_b.id
    )

    assert local_a == {item.item_id: 3.0}
    assert local_b == {item.item_id: 7.0}


def test_production_coverage_and_new_writer_are_generation_scoped(db_session):
    run, item, requirement = _run_item_requirement(db_session)
    generation_a = _generation(db_session, "a")
    generation_b = _generation(db_session, "b")

    _, product_a = _create_catchup_product(
        db_session,
        run=run,
        item_id=item.item_id,
        qty=4,
        req=requirement,
        now=datetime(2026, 7, 23),
        ledger_generation_id=generation_a.id,
    )
    _, product_b = _create_catchup_product(
        db_session,
        run=run,
        item_id=item.item_id,
        qty=9,
        req=requirement,
        now=datetime(2026, 7, 23),
        ledger_generation_id=generation_b.id,
    )

    assert product_a.ledger_generation_id == generation_a.id
    assert product_b.ledger_generation_id == generation_b.id
    assert _own_open_production_by_item(
        db_session,
        run,
        {requirement.id},
        ledger_generation_id=generation_a.id,
    ) == {item.item_id: 4.0}
    assert _own_open_production_by_item(
        db_session,
        run,
        {requirement.id},
        ledger_generation_id=generation_b.id,
    ) == {item.item_id: 9.0}

    with pytest.raises(TypeError):
        _create_catchup_product(
            db_session,
            run=run,
            item_id=item.item_id,
            qty=1,
            req=requirement,
            now=datetime(2026, 7, 23),
        )


def test_proposal_lineage_schema_and_migration_metadata():
    for table in (
        models.PlannedPurchase.__table__,
        models.ProductionProduct.__table__,
    ):
        column = table.c.ledger_generation_id
        assert column.nullable is True
        assert column.index is True
        assert {
            fk.target_fullname for fk in column.foreign_keys
        } == {"ledger_generation.id"}
        assert {fk.ondelete for fk in column.foreign_keys} == {"RESTRICT"}

    path = (
        Path(__file__).resolve().parents[2]
        / "backend/alembic/versions/20260723_07_proposal_generation_lineage.py"
    )
    spec = importlib.util.spec_from_file_location("proposal_lineage_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.revision == "20260723_07"
    assert module.down_revision == "20260723_06"

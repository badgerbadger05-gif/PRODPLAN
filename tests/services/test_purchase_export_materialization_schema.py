"""Schema contract for durable purchase-export materialization tables."""

import datetime
from pathlib import Path
from importlib.util import module_from_spec, spec_from_file_location

from app import models


def _fixture(db_session):
    item = models.Item(item_code="PEM-SCHEMA", item_name="Purchase Export Schema")
    imported = models.PhysicalImportBatch(
        batch_key="schema-physical",
        status="completed",
        source_watermarks={"fixture": "schema"},
        completed_at=datetime.datetime(2026, 7, 1),
    )
    generation = models.LedgerGeneration(
        generation_key="schema-generation",
        status="accepted",
        source_watermarks={},
        capabilities={},
        physical_import_batch=imported,
        algorithm_version="tests/1",
    )
    run = models.PlanningRun(config_snapshot={})
    db_session.add_all([item, imported, generation, run])
    db_session.flush()
    snapshot = models.PlanningReadSnapshot(
        consumer="purchase_control_journal",
        snapshot_key="journal:v1",
        ledger_generation_id=generation.id,
        cutoff=datetime.datetime(2026, 7, 1),
        truth_status="building",
        payload={},
        published_at=datetime.datetime(2026, 7, 1),
    )
    db_session.add(snapshot)
    db_session.flush()

    req = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        period_from=datetime.date(2026, 7, 1),
        period_to=datetime.date(2026, 7, 31),
    )
    db_session.add(req)
    db_session.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        requirement_id=req.id,
        priority_period_from=datetime.date(2026, 7, 1),
        priority_period_to=datetime.date(2026, 7, 31),
        run_id=run.run_id,
    )
    db_session.add(reservation)
    db_session.flush()

    return generation, snapshot, reservation


def _batch_payload(*, db_session):
    generation, snapshot, _ = _fixture(db_session)
    batch = models.PurchaseExportBatch(
        ledger_generation_id=generation.id,
        planning_read_snapshot_id=snapshot.id,
        idempotency_key="batch-schema-key",
        status="building",
        payload_hash="a" * 64,
        request_payload={"request": True},
        result_payload={"result": True},
    )
    return batch


def test_purchase_export_batch_metadata_contract():
    table = models.PurchaseExportBatch.__table__
    assert table.name == "purchase_export_batch"

    assert set(("ledger_generation_id", "planning_read_snapshot_id", "idempotency_key", "status")) <= set(
        col.name
        for col in table.columns
        if not col.nullable
    )
    assert "payload_hash" in table.c
    assert table.c.request_payload is not None and table.c.result_payload is not None

    foreign_keys = {
        (fk.parent.name, fk.target_fullname, fk.ondelete) for fk in table.foreign_keys
    }
    assert ("ledger_generation_id", "ledger_generation.id", "RESTRICT") in foreign_keys
    assert ("planning_read_snapshot_id", "planning_read_snapshot.id", "RESTRICT") in foreign_keys

    unique = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("idempotency_key",) in unique

    checks = {
        constraint.name
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert "ck_purchase_export_batch_status" in checks

    indexes = {index.name for index in table.indexes}
    assert {
        "ix_purchase_export_batch_ledger_generation_id",
        "ix_purchase_export_batch_planning_read_snapshot_id",
    } <= indexes


def test_purchase_export_obligation_allocation_metadata_contract():
    table = models.PurchaseExportObligationAllocation.__table__
    assert table.name == "purchase_export_obligation_allocation"

    assert set(("batch_id", "reservation_id", "supplier_order_ref", "supplier_order_line_no", "allocated_qty")) <= set(
        col.name for col in table.columns if not col.nullable
    )

    foreign_keys = {
        (fk.parent.name, fk.target_fullname, fk.ondelete)
        for fk in table.foreign_keys
    }
    assert ("batch_id", "purchase_export_batch.id", "RESTRICT") in foreign_keys
    assert ("reservation_id", "reservation_entry.id", "RESTRICT") in foreign_keys
    assert ("planned_purchase_id", "planned_purchase.purchase_id", "SET NULL") in foreign_keys

    unique = {
        tuple(constraint.columns.keys())
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert (
        "batch_id",
        "supplier_order_ref",
        "supplier_order_line_no",
        "reservation_id",
    ) in unique

    checks = {
        constraint.name
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert "ck_purchase_export_obligation_allocation_qty_positive" in checks

    indexes = {index.name for index in table.indexes}
    assert {
        "ix_purchase_export_obligation_allocation_batch_reservation",
        "ix_purchase_export_obligation_allocation_batch_supplier_line",
        "ix_purchase_export_obligation_allocation_planned_purchase",
    } <= indexes


def test_purchase_export_batch_can_insert(db_session):
    batch = _batch_payload(db_session=db_session)
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)

    assert batch.id is not None
    assert batch.status == "building"
    assert batch.created_at is not None


def test_purchase_export_obligation_allocation_can_insert(db_session):
    generation, _snapshot, reservation = _fixture(db_session)
    snapshot = models.PlanningReadSnapshot(
        consumer="purchase_control_journal",
        snapshot_key="journal:v1-alt",
        ledger_generation_id=generation.id,
        cutoff=datetime.datetime(2026, 7, 2),
        truth_status="building",
        payload={},
        published_at=datetime.datetime(2026, 7, 2),
    )
    db_session.add(snapshot)
    db_session.flush()
    batch = models.PurchaseExportBatch(
        ledger_generation_id=generation.id,
        planning_read_snapshot_id=snapshot.id,
        idempotency_key="batch-alloc-key",
        status="building",
        request_payload={"request": True},
        result_payload={"result": True},
    )
    db_session.add(batch)
    db_session.flush()
    allocation = models.PurchaseExportObligationAllocation(
        batch_id=batch.id,
        reservation_id=reservation.id,
        supplier_order_ref="SUP-REF",
        supplier_order_line_no="1",
        line_token=123,
        line_hash="h" * 10,
        allocated_qty=1,
    )
    db_session.add_all([batch, allocation])
    db_session.commit()
    db_session.refresh(allocation)
    assert allocation.id is not None
    assert float(allocation.allocated_qty) == 1


def test_purchase_export_batch_migration_follows_current_head():
    path = (
        Path(__file__).resolve().parents[2]
        / "backend/alembic/versions/20260723_19_add_purchase_export_materialization.py"
    )
    spec = spec_from_file_location("purchase_export_batch_migration", path)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.revision == "20260723_19"
    assert module.down_revision == "20260723_18"

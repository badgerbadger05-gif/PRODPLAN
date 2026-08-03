from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from app import models
from app.services.item_ledger import candidate_future_supply as stage
from app.services.item_ledger.future_supply_capture import (
    FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION,
    FutureSupplyEvidence,
    future_supply_evidence_hash,
)


def _scope(db, suffix="one"):
    cutoff = datetime(2026, 7, 31, 23, 59)
    physical = models.PhysicalImportBatch(
        batch_key=f"candidate-physical-{suffix}", status="completed", cutoff=cutoff,
        source_watermarks={},
    )
    source = models.LedgerGeneration(
        generation_key=f"candidate-source-{suffix}", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, source_watermarks={}, capabilities={},
        physical_import_batch=physical, algorithm_version="test",
    )
    target = models.LedgerGeneration(
        generation_key=f"candidate-target-{suffix}", status="building", cutoff=cutoff,
        source_watermarks={"generation_kind": "obligation_refresh", "parent_generation_id": 0},
        capabilities={}, physical_import_batch=physical, algorithm_version="test",
    )
    item = models.Item(item_code=f"CAND-{suffix}", item_name="Candidate")
    db.add_all([source, target, item])
    db.flush()
    target.source_watermarks = {
        "generation_kind": "obligation_refresh", "parent_generation_id": source.id,
    }
    batch = models.LedgerBuildBatch(
        ledger_generation_id=target.id, stage="future_supply_capture", status="building",
        batch_key=f"candidate-snapshot-{suffix}",
        algorithm_version=FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION,
        metrics={},
    )
    db.add(batch)
    db.flush()
    return source, target, batch, item


def _evidence(source, item, *, kind, ref, line, status="exact"):
    values = dict(
        supply_kind=kind, item_id=item.item_id, planning_stock_pool="main",
        destination_warehouse_ref1c="WH-1", source_ref=ref, source_line_ref=line,
        source_local_id=f"{kind}:{ref}:{line}", ordered_qty_at_cutoff=Decimal("10"),
        realized_qty_at_cutoff=Decimal("3"), eta_date=date(2026, 8, 1),
        source_state_key="open", source_updated_at=datetime(2026, 7, 30),
        capture_cutoff=source.cutoff, evidence_status=status,
    )
    unsigned = FutureSupplyEvidence(**values)
    return FutureSupplyEvidence(**{**values, "source_content_hash": future_supply_evidence_hash(unsigned)})


def _adapters(monkeypatch, source, item, *, calls=None):
    wip = _evidence(source, item, kind="wip_order", ref="WO-1", line="1")
    supplier = _evidence(source, item, kind="supplier_order", ref="SO-1", line="1")

    def collect(db, generation_id, **kwargs):
        assert generation_id == source.id
        assert kwargs["planning_pool_by_warehouse"] == {"WH-1": "main"}
        return [wip]

    def suppliers(db, generation_id, **kwargs):
        assert generation_id == source.id
        assert kwargs["planning_pool_by_warehouse"] == {"WH-1": "main"}
        return (supplier,)

    monkeypatch.setattr(stage, "collect_wip_future_supply_evidence", collect)
    monkeypatch.setattr(stage, "supplier_future_supply_evidence", suppliers)
    if calls is not None:
        real = stage.replace_future_supply_capture

        def replace(*args):
            calls.append(tuple(args[3]))
            return real(*args)

        monkeypatch.setattr(stage, "replace_future_supply_capture", replace)


def test_combined_stage_keeps_wip_and_supplier_in_one_replace(db_session, monkeypatch):
    source, target, batch, item = _scope(db_session)
    calls = []
    _adapters(monkeypatch, source, item, calls=calls)

    metrics = stage.capture_candidate_future_supply(
        db_session, source.id, target.id, batch.id,
        planning_pool_by_warehouse={"WH-1": "main"},
    )

    assert len(calls) == 1
    assert {row.supply_kind for row in calls[0]} == {"wip_order", "supplier_order"}
    stored = db_session.query(models.LedgerFutureSupply).order_by(
        models.LedgerFutureSupply.supply_kind
    ).all()
    assert [row.supply_kind for row in stored] == ["supplier_order", "wip_order"]
    assert metrics["rows"] == 2


def test_candidate_captures_direct_1c_supplier_order_without_export_link(db_session):
    source, target, batch, item = _scope(db_session, "direct-supplier")
    history_timestamp = datetime(2026, 7, 30)
    order = models.SupplierOrder(
        order_number="DIRECT-1",
        order_date=datetime(2026, 7, 25),
        order_ref1c="direct-supplier-ref",
        order_state_name="В пути",
        deletion_mark=False,
        created_at=history_timestamp,
        updated_at=history_timestamp,
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(
        models.SupplierOrderItem(
            order_id=order.order_id,
            item_id_ref=item.item_id,
            line_number=1,
            destination_warehouse_ref1c="WH-1",
            quantity=Decimal("9"),
            received_qty=Decimal("0"),
            remaining_qty=Decimal("9"),
            delivery_date=datetime(2026, 8, 5),
            created_at=history_timestamp,
            updated_at=history_timestamp,
        )
    )
    db_session.flush()

    metrics = stage.capture_candidate_future_supply(
        db_session,
        source.id,
        target.id,
        batch.id,
        planning_pool_by_warehouse={"WH-1": "main"},
    )

    row = db_session.query(models.LedgerFutureSupply).one()
    assert metrics["rows"] == 1
    assert metrics["exact_rows"] == 1
    assert row.source_ref == "direct-supplier-ref"
    assert row.open_qty_at_cutoff == Decimal("9")
    assert row.source_state_key == "В пути"


def test_combined_stage_retry_does_not_rewrite_rows(db_session, monkeypatch):
    source, target, batch, item = _scope(db_session, "retry")
    _adapters(monkeypatch, source, item)
    args = (db_session, source.id, target.id, batch.id)
    kwargs = {"planning_pool_by_warehouse": {"WH-1": "main"}}

    first = stage.capture_candidate_future_supply(*args, **kwargs)
    before = [(row.id, row.created_at) for row in db_session.query(
        models.LedgerFutureSupply
    ).order_by(models.LedgerFutureSupply.id)]
    second = stage.capture_candidate_future_supply(*args, **kwargs)
    after = [(row.id, row.created_at) for row in db_session.query(
        models.LedgerFutureSupply
    ).order_by(models.LedgerFutureSupply.id)]

    assert second == first
    assert after == before


def test_combined_stage_obeys_outer_rollback(db_session, monkeypatch):
    source, target, batch, item = _scope(db_session, "rollback")
    _adapters(monkeypatch, source, item)
    db_session.commit()
    outer = db_session.begin()
    db_session.execute(text("UPDATE ledger_generation SET id = id WHERE id = :id"), {"id": target.id})

    stage.capture_candidate_future_supply(
        db_session, source.id, target.id, batch.id,
        planning_pool_by_warehouse={"WH-1": "main"},
    )
    outer.rollback()

    assert db_session.query(models.LedgerFutureSupply).count() == 0


@pytest.mark.parametrize("broken", ["parent", "physical", "cutoff"])
def test_combined_stage_rejects_nonidentical_obligation_lineage(db_session, monkeypatch, broken):
    source, target, batch, item = _scope(db_session, broken)
    _adapters(monkeypatch, source, item)
    if broken == "parent":
        target.source_watermarks = {"generation_kind": "obligation_refresh", "parent_generation_id": 999}
    elif broken == "physical":
        other = models.PhysicalImportBatch(
            batch_key=f"other-{broken}", status="completed", cutoff=source.cutoff, source_watermarks={},
        )
        db_session.add(other)
        db_session.flush()
        target.physical_import_batch_id = other.id
    else:
        target.cutoff = datetime(2026, 8, 1)
    db_session.flush()

    with pytest.raises(stage.CandidateFutureSupplyError):
        stage.capture_candidate_future_supply(
            db_session, source.id, target.id, batch.id,
            planning_pool_by_warehouse={"WH-1": "main"},
        )
    assert db_session.query(models.LedgerFutureSupply).count() == 0

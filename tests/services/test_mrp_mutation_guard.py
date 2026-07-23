from datetime import date, datetime, timezone

import pytest

from app import models
from app.services import one_c_purchase_order_export as purchase_exporter
from app.services.planning_truth import PlanningTruthUnavailable, publish_generation
from app.services.production_control_journal import create_orders_from_mrp


def _accepted_truth(db):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key="mutation-guard-batch",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="mutation-guard-generation",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
        physical_import_batch=batch,
        algorithm_version="test/1",
        replay_version="test/1",
    )
    publish_generation(db, generation)
    db.flush()
    return generation, cutoff


def _proposal(db, *, with_truth: bool):
    generation = cutoff = None
    if with_truth:
        generation, cutoff = _accepted_truth(db)
    item = models.Item(
        item_code=f"guard-item-{with_truth}",
        item_name="Guard item",
        status="active",
    )
    db.add(item)
    db.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        active_freeze_version=1,
        ledger_generation_id=generation.id if generation else None,
        ledger_cutoff=cutoff,
    )
    db.add(run)
    db.flush()
    proposal = models.PlannedOrder(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=2,
        planned_qty=2,
        qty=2,
        need_date=date(2026, 7, 30),
        bucket_date=date(2026, 7, 30),
        ledger_generation_id=generation.id if generation else None,
    )
    db.add(proposal)
    db.commit()
    return generation, run, proposal


def test_materialization_blocks_before_local_writes_when_truth_unavailable(db_session):
    _generation, _run, proposal = _proposal(db_session, with_truth=False)

    with pytest.raises(PlanningTruthUnavailable):
        create_orders_from_mrp(db_session, [proposal.order_id])

    assert db_session.query(models.ProductionOrder).count() == 0
    assert db_session.query(models.ProductionProduct).count() == 0


def test_materialization_copies_exact_accepted_generation_lineage(db_session):
    generation, _run, proposal = _proposal(db_session, with_truth=True)

    result = create_orders_from_mrp(db_session, [proposal.order_id])

    assert result["status"] == "ok"
    assert len(result["created"]) == 1
    product = db_session.query(models.ProductionProduct).one()
    assert product.ledger_generation_id == generation.id


def test_materialization_rejects_stale_proposal_before_local_writes(db_session):
    generation, _run, proposal = _proposal(db_session, with_truth=True)
    proposal.ledger_generation_id = None
    db_session.commit()

    with pytest.raises(ValueError, match="null, mixed or stale"):
        create_orders_from_mrp(db_session, [proposal.order_id])

    assert db_session.query(models.ProductionOrder).count() == 0
    assert generation.id is not None


def test_dry_run_is_blocked_before_network_when_truth_unavailable(
    db_session, monkeypatch
):
    item = models.Item(
        item_code="guard-purchase-unavailable",
        item_name="Guard purchase",
        item_ref1c="ITEM-REF",
        supplier_ref1c="SUPPLIER-REF",
        status="active",
    )
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        active_freeze_version=1,
    )
    db_session.add_all([item, run])
    db_session.flush()
    purchase = models.PlannedPurchase(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=1,
        planned_qty=1,
        qty=1,
        need_date=date(2026, 7, 30),
        order_date=date(2026, 7, 23),
        lead_time_days=7,
        priority_index=1,
        bucket_date=date(2026, 7, 30),
    )
    db_session.add(purchase)
    db_session.commit()

    network_called = False

    def network_bomb(*_args, **_kwargs):
        nonlocal network_called
        network_called = True
        raise AssertionError("network client must not be constructed")

    monkeypatch.setattr(purchase_exporter, "_create_odata_client", network_bomb)
    with pytest.raises(PlanningTruthUnavailable):
        purchase_exporter.export_planned_purchases_to_1c(
            db_session,
            run.run_id,
            purchase_ids=[purchase.purchase_id],
            dry_run=True,
        )

    assert network_called is False
    assert db_session.query(models.SyncLink).count() == 0
    assert db_session.query(models.PurchaseExportLineAllocation).count() == 0


def test_materialization_rejects_mixed_proposal_lineage_atomically(db_session):
    generation, run, proposal = _proposal(db_session, with_truth=True)
    stale = models.PlannedOrder(
        run_id=run.run_id,
        item_id=proposal.item_id,
        requested_qty=3,
        planned_qty=3,
        qty=3,
        need_date=date(2026, 7, 31),
        bucket_date=date(2026, 7, 31),
        ledger_generation_id=None,
    )
    db_session.add(stale)
    db_session.commit()

    with pytest.raises(ValueError, match="null, mixed or stale"):
        create_orders_from_mrp(
            db_session, [proposal.order_id, stale.order_id]
        )

    assert db_session.query(models.ProductionOrder).count() == 0
    assert db_session.query(models.ProductionProduct).count() == 0
    assert generation.id is not None


def test_materialization_rejects_stale_run_cutoff(db_session):
    _generation, run, proposal = _proposal(db_session, with_truth=True)
    run.ledger_cutoff = datetime(2026, 7, 23, 13, tzinfo=timezone.utc)
    db_session.commit()

    with pytest.raises(ValueError, match="cutoff does not match"):
        create_orders_from_mrp(db_session, [proposal.order_id])

    assert db_session.query(models.ProductionOrder).count() == 0

"""Tests for the fixed-MRP execution ledger cycle (increment 3).

``run_ledger_cycle`` rebuilds the explainable ``mrp_execution_allocation`` rows
and the derived ``MrpRequirement.executed_qty`` cache for the canonical scope
(last FIXED_SNAPSHOT per plan, NO period filter, plus CLOSED-with-open-req). It
is a recompute: it DELETEs its scope and re-INSERTs, never reading its own
output, so two cycles over unchanged facts are identical.

The first eight tests are the migrated phase-2 tests: the FIXTURES/input change
(``run_ledger_cycle(db)`` instead of the retired ``populate_executed_qty([run])``;
``_make_run`` now creates a plan + source_plan_id so the run enters scope), the
asserted VALUES are unchanged (no baseline → Δ = full history = phase-2 parity).
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from app import models


@pytest.fixture(autouse=True)
def _accepted_planning_truth(monkeypatch):
    monkeypatch.setattr(
        "app.services.planning_truth.require_accepted_truth",
        lambda db, consumer, **kwargs: SimpleNamespace(
            status="accepted", generation_id=1, cutoff=None, reason=None
        ),
    )

from app.models import (
    Item,
    MrpDriftEvent,
    MrpExecutionAllocation,
    MrpFreezeAllocation,
    MrpFreezeBaseline,
    MrpFreezeComponent,
    MrpRequirement,
    MrpRequirementBucket,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionPlanHeader,
    ProductionProduct,
    SpecComponent,
    Specification,
    SupplierOrder,
    SupplierOrderItem,
)
from app.services.mrp_execution_ledger import (
    _scope_run_ids,
    populate_executed_qty as _public_populate_executed_qty,
    run_ledger_cycle as _public_run_ledger_cycle,
)


def run_ledger_cycle(db):
    return _public_run_ledger_cycle(db, diagnostic_legacy=True)


def populate_executed_qty(db, run_ids=None):
    return _public_populate_executed_qty(
        db, run_ids, diagnostic_legacy=run_ids is None
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_public_ledger_cycle_is_blocked_and_creates_no_allocations(db_session):
    before = db_session.query(MrpExecutionAllocation).count()

    with pytest.raises(NotImplementedError, match="generation-unaware"):
        _public_run_ledger_cycle(db_session)

    assert db_session.query(MrpExecutionAllocation).count() == before


def test_legacy_diagnostic_cycle_never_repoints_accepted_truth(db_session):
    batch = models.PhysicalImportBatch(
        batch_key="accepted-before-diagnostic",
        status="completed",
        cutoff=datetime(2026, 7, 1),
        source_watermarks={"test": True},
    )
    db_session.add(batch)
    db_session.flush()
    accepted = models.LedgerGeneration(
        generation_key="accepted-before-diagnostic",
        status="accepted",
        cutoff=batch.cutoff,
        accepted_at=datetime(2026, 7, 1),
        physical_import_batch_id=batch.id,
        algorithm_version="test",
        replay_version="test",
        source_watermarks={"test": True},
        capabilities={},
    )
    db_session.add(accepted)
    db_session.flush()
    db_session.add(
        models.PlanningTruthState(id=1, current_generation_id=accepted.id)
    )
    db_session.commit()

    _public_run_ledger_cycle(db_session, diagnostic_legacy=True)
    db_session.expire_all()

    pointer = db_session.get(models.PlanningTruthState, 1)
    assert pointer.current_generation_id == accepted.id

def _make_production_item(db, code: str, stock: float = 0.0) -> Item:
    item = Item(
        item_code=code,
        item_name=f"Изделие {code}",
        item_article=code,
        unit="шт",
        stock_qty=stock,
        replenishment_method="Производство",
        replenishment_time=0,
        status="active",
    )
    db.add(item)
    db.flush()
    return item


def _make_purchased_item(db, code: str, stock: float = 0.0) -> Item:
    item = Item(
        item_code=code,
        item_name=f"Деталь {code}",
        item_article=code,
        unit="шт",
        stock_qty=stock,
        replenishment_method="Покупка",
        replenishment_time=3,
        status="active",
    )
    db.add(item)
    db.flush()
    return item


def _make_plan(db, *, period_from: date, period_to: date) -> ProductionPlanHeader:
    plan = ProductionPlanHeader(
        name=f"plan-{period_from}-{period_to}",
        period_from=period_from,
        period_to=period_to,
        status="fixed",
    )
    db.add(plan)
    db.flush()
    return plan


def _make_run(
    db,
    *,
    period_from: date,
    period_to: date,
    plan: ProductionPlanHeader = None,
    freeze_version: int = None,
) -> PlanningRun:
    # A run must be plan-linked to enter the ledger scope (last FIXED_SNAPSHOT
    # per source_plan_id). period_to is intentionally NOT filtered by the scope.
    if plan is None:
        plan = _make_plan(db, period_from=period_from, period_to=period_to)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        pinned=True,
        source_plan_id=plan.id,
        period_from=period_from,
        period_to=period_to,
        active_freeze_version=freeze_version,
    )
    db.add(run)
    db.flush()
    return run


def _make_req(db, run, item, *, net, bom_level=0, status="open") -> MrpRequirement:
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=net,
        net_required_qty=net,
        covered_qty=0.0,
        remaining_qty=net,
        period_from=run.period_from,
        period_to=run.period_to,
        bom_level=bom_level,
        status=status,
    )
    db.add(req)
    db.flush()
    return req


def _make_bucket(db, req, *, bucket_date, net_qty, gross_qty=None) -> MrpRequirementBucket:
    bucket = MrpRequirementBucket(
        requirement_id=req.id,
        run_id=req.run_id,
        item_id=req.item_id,
        bucket_date=bucket_date,
        gross_qty=gross_qty if gross_qty is not None else net_qty,
        net_qty=net_qty,
    )
    db.add(bucket)
    db.flush()
    return bucket


def _make_production_line(
    db,
    item,
    *,
    quantity,
    produced,
    req=None,
    source="mrp",
    status="in_progress",
    order_ref1c=None,
    order_state_key=None,
    deletion_mark=False,
    order_number=None,
) -> ProductionProduct:
    order = ProductionOrder(
        order_number=order_number or f"PO-{item.item_code}-{quantity}-{produced}",
        order_date=datetime(2026, 6, 1),
        is_posted=False,
        deletion_mark=deletion_mark,
        source=source,
        order_ref1c=order_ref1c,
        order_state_key=order_state_key,
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=quantity,
        produced_qty=produced,
        remaining_qty=max(quantity - produced, 0.0),
        source_mrp_requirement_id=req.id if req else None,
    )
    db.add(product)
    db.flush()
    if status is not None:
        db.add(ProductionOrderLineState(product_id=product.product_id, status=status))
        db.flush()
    return product


def _make_receipt(
    db,
    item,
    *,
    received,
    quantity=None,
    state_name="Принят на склад",
    deletion_mark=False,
    order_ref1c=None,
) -> SupplierOrderItem:
    order = SupplierOrder(
        order_number=f"SO-{item.item_code}-{received}",
        order_date=datetime(2026, 6, 1),
        order_ref1c=order_ref1c,
        deletion_mark=deletion_mark,
        order_state_name=state_name,
    )
    db.add(order)
    db.flush()
    qty = quantity if quantity is not None else received
    line = SupplierOrderItem(
        order_id=order.order_id,
        item_id_ref=item.item_id,
        line_number=1,
        quantity=qty,
        received_qty=received,
        remaining_qty=max(qty - received, 0.0),
    )
    db.add(line)
    db.flush()
    return line


def _make_baseline(db, run, item, *, produced_total=0.0, received_total=0.0, stock=0.0, version=1):
    row = MrpFreezeBaseline(
        run_id=run.run_id,
        freeze_version=version,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        frozen_at=datetime(2026, 6, 1),
        stock_qty=stock,
        produced_total=produced_total,
        received_total=received_total,
        unit_coef=1.0,
    )
    db.add(row)
    db.flush()
    return row


def _make_freeze_alloc(
    db, run, req, item, *, source_type, source_ref, source_line_ref, alloc_qty, fact_at_freeze, version=1
):
    alloc = MrpFreezeAllocation(
        run_id=run.run_id,
        freeze_version=version,
        requirement_id=req.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        source_type=source_type,
        source_ref=source_ref,
        source_line_ref=str(source_line_ref),
        alloc_qty=alloc_qty,
        fact_at_freeze=fact_at_freeze,
        realized_qty=0.0,
        evaporated_qty=0.0,
    )
    db.add(alloc)
    db.flush()
    return alloc


def _make_freeze_component(db, run, parent, component, *, norm, version=1):
    row = MrpFreezeComponent(
        run_id=run.run_id,
        freeze_version=version,
        parent_item_id=parent.item_id,
        parent_characteristic_ref="",
        parent_organization_ref="",
        parent_planning_stock_pool="default",
        component_item_id=component.item_id,
        component_characteristic_ref="",
        component_organization_ref="",
        component_planning_stock_pool="default",
        spec_ref=f"spec-{parent.item_code}",
        norm_qty_per_unit=norm,
        unit_coef=1.0,
    )
    db.add(row)
    db.flush()
    return row


def _drift_events(db, item, kind=None):
    q = db.query(MrpDriftEvent).filter(MrpDriftEvent.item_id == item.item_id)
    if kind is not None:
        q = q.filter(MrpDriftEvent.kind == kind)
    return q.all()


def _exec_rows(db, req):
    return (
        db.query(MrpExecutionAllocation)
        .filter(MrpExecutionAllocation.requirement_id == req.id)
        .filter(MrpExecutionAllocation.allocation_kind == "execution")
        .all()
    )


def _coverage_rows(db, req):
    return (
        db.query(MrpExecutionAllocation)
        .filter(MrpExecutionAllocation.requirement_id == req.id)
        .filter(MrpExecutionAllocation.allocation_kind == "coverage_realization")
        .all()
    )


def _payload(db):
    """Stable allocation payload (excludes cycle_id / calculated_at / PK)."""
    rows = db.query(MrpExecutionAllocation).all()
    payload = [
        (
            int(r.requirement_id),
            r.bucket_id,
            r.fact_type,
            r.allocation_kind,
            r.fact_ref,
            r.fact_line_ref,
            float(r.allocated_qty),
            r.freeze_allocation_id,
            r.origin_requirement_id,
        )
        for r in rows
    ]
    return sorted(payload)


# ===========================================================================
# Migrated phase-2 tests (input/fixtures changed; asserted values unchanged)
# ===========================================================================

def test_linked_production_executed_is_min_produced_quantity(db_session):
    item = _make_production_item(db_session, "L-PROD")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req = _make_req(db_session, run, item, net=40)
    _make_production_line(db_session, item, quantity=40, produced=25, req=req)
    db_session.commit()

    summary = run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 25.0
    assert summary["items_touched"] == 1
    assert abs(summary["total_executed"] - 25.0) < 1e-6


def test_linked_receipt_executed_reflects_received_qty(db_session):
    item = _make_purchased_item(db_session, "L-RECV")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req = _make_req(db_session, run, item, net=50)
    _make_receipt(db_session, item, received=30, quantity=50)
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 30.0


def test_fifo_oldest_plan_first_across_two_runs(db_session):
    item = _make_production_item(db_session, "FIFO-ITEM")
    old_run = _make_run(db_session, period_from=date(2026, 5, 1), period_to=date(2026, 5, 31))
    new_run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    old_req = _make_req(db_session, old_run, item, net=40)
    new_req = _make_req(db_session, new_run, item, net=40)
    # Real buckets drive the global bucket-FIFO order (oldest plan first).
    _make_bucket(db_session, old_req, bucket_date=date(2026, 5, 31), net_qty=40)
    _make_bucket(db_session, new_req, bucket_date=date(2026, 6, 30), net_qty=40)
    # Unlinked 1C production pool of 50 for the item.
    _make_production_line(db_session, item, quantity=50, produced=50, req=None, source="1c")
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(old_req)
    db_session.refresh(new_req)
    assert float(old_req.executed_qty) == 40.0
    assert float(new_req.executed_qty) == 10.0


def test_cap_at_net_for_production_and_receipt(db_session):
    prod = _make_production_item(db_session, "CAP-PROD")
    buy = _make_purchased_item(db_session, "CAP-BUY")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    prod_req = _make_req(db_session, run, prod, net=40)
    buy_req = _make_req(db_session, run, buy, net=40)
    _make_production_line(db_session, prod, quantity=100, produced=100, req=prod_req)
    _make_receipt(db_session, buy, received=100, quantity=100)
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(prod_req)
    db_session.refresh(buy_req)
    assert float(prod_req.executed_qty) == 40.0
    assert float(buy_req.executed_qty) == 40.0


def test_idempotent_double_run(db_session):
    item = _make_production_item(db_session, "IDEM-ITEM")
    buy = _make_purchased_item(db_session, "IDEM-BUY")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    prod_req = _make_req(db_session, run, item, net=40)
    buy_req = _make_req(db_session, run, buy, net=50)
    _make_production_line(db_session, item, quantity=40, produced=15, req=prod_req)
    _make_production_line(db_session, item, quantity=100, produced=100, req=None, source="1c")
    _make_receipt(db_session, buy, received=30, quantity=50)
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()
    first = {
        int(r.id): float(r.executed_qty)
        for r in db_session.query(MrpRequirement).filter(MrpRequirement.run_id == run.run_id).all()
    }
    first_payload = _payload(db_session)

    run_ledger_cycle(db_session)
    db_session.commit()
    second = {
        int(r.id): float(r.executed_qty)
        for r in db_session.query(MrpRequirement).filter(MrpRequirement.run_id == run.run_id).all()
    }
    second_payload = _payload(db_session)

    assert first == second
    assert first_payload == second_payload
    assert first[prod_req.id] == 40.0
    assert first[buy_req.id] == 30.0


def test_cancelled_line_excluded(db_session):
    item = _make_production_item(db_session, "CANCEL-ITEM")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req = _make_req(db_session, run, item, net=40)
    _make_production_line(db_session, item, quantity=40, produced=30, req=req, status="cancelled")
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 0.0


def test_unlinked_pool_not_double_counted_across_two_plans(db_session):
    item = _make_production_item(db_session, "POOL-ITEM")
    run_a = _make_run(db_session, period_from=date(2026, 5, 1), period_to=date(2026, 5, 31))
    run_b = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req_a = _make_req(db_session, run_a, item, net=40)
    req_b = _make_req(db_session, run_b, item, net=40)
    _make_production_line(db_session, item, quantity=40, produced=40, req=None, source="1c")
    db_session.commit()

    summary = run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req_a)
    db_session.refresh(req_b)
    assert float(req_a.executed_qty) == 40.0
    assert float(req_b.executed_qty) == 0.0
    assert abs(summary["total_executed"] - 40.0) < 1e-6


def test_closed_requirements_are_not_populated(db_session):
    item = _make_production_item(db_session, "CLOSED-ITEM")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req = _make_req(db_session, run, item, net=40, status="closed")
    _make_production_line(db_session, item, quantity=40, produced=40, req=req)
    db_session.commit()

    summary = run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 0.0
    assert summary["items_touched"] == 0


# ===========================================================================
# Increment-3 tests
# ===========================================================================

def test_i3_delta_from_baseline(db_session):
    """produced_total=100 baseline, current produced=130 → executed = Δ 30."""
    item = _make_production_item(db_session, "D-1")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db_session, run, item, net=100)
    _make_baseline(db_session, run, item, produced_total=100.0, version=1)
    _make_production_line(db_session, item, quantity=130, produced=130, req=req)
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 30.0


def test_i3_coverage_realization_not_executed(db_session):
    """Realising a frozen supplier allocation → coverage row, executed stays 0."""
    item = _make_purchased_item(db_session, "C-1")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db_session, run, item, net=50)
    _make_baseline(db_session, run, item, received_total=0.0, version=1)
    line = _make_receipt(db_session, item, received=30, quantity=50, order_ref1c="SUP-REF-1")
    _make_freeze_alloc(
        db_session, run, req, item,
        source_type="supplier_order", source_ref="SUP-REF-1",
        source_line_ref=line.item_id, alloc_qty=50, fact_at_freeze=50,
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 0.0
    cov = _coverage_rows(db_session, req)
    assert len(cov) == 1
    assert float(cov[0].allocated_qty) == 30.0
    assert cov[0].freeze_allocation_id is not None
    assert _exec_rows(db_session, req) == []


def test_i3_realization_with_surplus_execution(db_session):
    """WIP alloc 50, Δ 70 → realized 50 (coverage) + surplus 20 (execution)."""
    item = _make_production_item(db_session, "R-1")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db_session, run, item, net=100)
    _make_baseline(db_session, run, item, produced_total=0.0, version=1)
    product = _make_production_line(
        db_session, item, quantity=50, produced=70, req=None, source="1c", order_ref1c="WIP-REF-1"
    )
    alloc = _make_freeze_alloc(
        db_session, run, req, item,
        source_type="wip_order", source_ref="WIP-REF-1",
        source_line_ref=product.product_id, alloc_qty=50, fact_at_freeze=50,
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    db_session.refresh(alloc)
    assert float(alloc.realized_qty) == 50.0
    cov = _coverage_rows(db_session, req)
    assert sum(float(r.allocated_qty) for r in cov) == 50.0
    assert sum(float(r.allocated_qty) for r in _exec_rows(db_session, req)) == 20.0
    assert float(req.executed_qty) == 20.0


def test_i3_evaporation_terminal_order(db_session):
    """Cancelled supplier order: realized 10, evaporated 40 → drift_event is still
    written (the audit signal), but a supplier_order pin's evaporation is NO LONGER
    folded into drift_adjustment (single-channel — it resurfaces via
    own_open_coverage in the sizer; folding it here too over-orders by the dead
    pin's alloc). So drift_adjustment == 0, while the matured evaporation event
    still records 40."""
    item = _make_purchased_item(db_session, "E-1")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db_session, run, item, net=100)
    _make_baseline(db_session, run, item, received_total=0.0, version=1)
    line = _make_receipt(
        db_session, item, received=10, quantity=50, state_name="Отменён", order_ref1c="SUP-EVAP-1"
    )
    alloc = _make_freeze_alloc(
        db_session, run, req, item,
        source_type="supplier_order", source_ref="SUP-EVAP-1",
        source_line_ref=line.item_id, alloc_qty=50, fact_at_freeze=50,
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    db_session.refresh(alloc)
    assert float(alloc.realized_qty) == 10.0
    assert float(alloc.evaporated_qty) == 40.0
    assert float(req.executed_qty) == 0.0
    # supplier_order evaporation is single-channel (own_open_coverage), NOT drift
    assert float(req.drift_adjustment_qty) == 0.0
    events = (
        db_session.query(MrpDriftEvent)
        .filter(MrpDriftEvent.kind == "evaporation")
        .filter(MrpDriftEvent.requirement_id == req.id)
        .all()
    )
    assert len(events) == 1
    assert float(events[0].drift_qty) == 40.0
    assert bool(events[0].matured) is True


def test_i3_wip_realization_is_coverage_only(db_session):
    """Realising a frozen WIP allocation is coverage, not execution."""
    item = _make_production_item(db_session, "W-1")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db_session, run, item, net=50)
    _make_baseline(db_session, run, item, produced_total=0.0, version=1)
    product = _make_production_line(
        db_session, item, quantity=50, produced=30, req=None, source="1c", order_ref1c="WIP-W1"
    )
    alloc = _make_freeze_alloc(
        db_session, run, req, item,
        source_type="wip_order", source_ref="WIP-W1",
        source_line_ref=product.product_id, alloc_qty=50, fact_at_freeze=50,
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    db_session.refresh(alloc)
    assert float(alloc.realized_qty) == 30.0
    assert float(req.executed_qty) == 0.0
    assert sum(float(r.allocated_qty) for r in _coverage_rows(db_session, req)) == 30.0


def test_i3_linked_bucket_fifo_with_caps(db_session):
    """A linked production line fills its own req's buckets in date order, capped."""
    item = _make_production_item(db_session, "LB-1")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db_session, run, item, net=100)
    b1 = _make_bucket(db_session, req, bucket_date=date(2026, 6, 10), net_qty=40)
    b2 = _make_bucket(db_session, req, bucket_date=date(2026, 6, 20), net_qty=60)
    _make_baseline(db_session, run, item, produced_total=0.0, version=1)
    _make_production_line(db_session, item, quantity=100, produced=70, req=req)
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 70.0
    by_bucket = {
        r.bucket_id: float(r.allocated_qty) for r in _exec_rows(db_session, req)
    }
    assert by_bucket.get(b1.id) == 40.0
    assert by_bucket.get(b2.id) == 30.0


def test_i3_linked_overproduction_spills_with_origin(db_session):
    """Overproduction beyond the owner's net spills to another req in the pool."""
    item = _make_production_item(db_session, "OP-1")
    run_a = _make_run(db_session, period_from=date(2026, 5, 1), period_to=date(2026, 5, 31), freeze_version=1)
    run_b = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req_a = _make_req(db_session, run_a, item, net=40)
    req_b = _make_req(db_session, run_b, item, net=40)
    _make_production_line(db_session, item, quantity=100, produced=100, req=req_a)
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req_a)
    db_session.refresh(req_b)
    assert float(req_a.executed_qty) == 40.0
    assert float(req_b.executed_qty) == 40.0
    b_rows = _exec_rows(db_session, req_b)
    assert b_rows and all(r.origin_requirement_id == req_a.id for r in b_rows)


def test_i3_global_bucket_fifo_interleaves_by_date(db_session):
    """Buckets of two plans interleave by bucket_date, not by plan age."""
    item = _make_production_item(db_session, "GB-1")
    run_a = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    run_b = _make_run(db_session, period_from=date(2026, 7, 1), period_to=date(2026, 7, 31), freeze_version=1)
    req_a = _make_req(db_session, run_a, item, net=30)
    req_b = _make_req(db_session, run_b, item, net=30)
    _make_bucket(db_session, req_a, bucket_date=date(2026, 6, 15), net_qty=30)
    _make_bucket(db_session, req_b, bucket_date=date(2026, 6, 5), net_qty=30)
    _make_production_line(db_session, item, quantity=40, produced=40, req=None, source="1c")
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req_a)
    db_session.refresh(req_b)
    # req_b's bucket (June 5) is earliest → fills first though its plan is newer.
    assert float(req_b.executed_qty) == 30.0
    assert float(req_a.executed_qty) == 10.0


def test_i3_caps_executed_and_bucket(db_session):
    """Σ execution never exceeds effective net; a bucket never exceeds net_qty."""
    item = _make_production_item(db_session, "CAP-1")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db_session, run, item, net=40)
    bucket = _make_bucket(db_session, req, bucket_date=date(2026, 6, 15), net_qty=40)
    _make_production_line(db_session, item, quantity=1000, produced=1000, req=None, source="1c")
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 40.0
    by_bucket = {r.bucket_id: float(r.allocated_qty) for r in _exec_rows(db_session, req)}
    assert by_bucket.get(bucket.id) <= 40.0 + 1e-9


def test_i3_idempotent_payload_and_executed(db_session):
    """Two cycles over unchanged facts → identical payload and executed_qty."""
    item = _make_production_item(db_session, "IDEM3")
    buy = _make_purchased_item(db_session, "IDEM3-BUY")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    prod_req = _make_req(db_session, run, item, net=60)
    buy_req = _make_req(db_session, run, buy, net=50)
    _make_baseline(db_session, run, item, produced_total=10.0, version=1)
    _make_baseline(db_session, run, buy, received_total=0.0, version=1)
    _make_production_line(db_session, item, quantity=100, produced=100, req=prod_req)
    _make_receipt(db_session, buy, received=30, quantity=50)
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()
    first_payload = _payload(db_session)
    first_exec = {
        int(r.id): float(r.executed_qty)
        for r in db_session.query(MrpRequirement).all()
    }

    run_ledger_cycle(db_session)
    db_session.commit()
    second_payload = _payload(db_session)
    second_exec = {
        int(r.id): float(r.executed_qty)
        for r in db_session.query(MrpRequirement).all()
    }

    assert first_payload == second_payload
    assert first_exec == second_exec


def test_i3_fallback_no_baseline_is_phase2_parity(db_session):
    """No baseline → anchor (0,0) → Δ = full history = phase-2 numbers."""
    item = _make_production_item(db_session, "FB-1")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req = _make_req(db_session, run, item, net=40)
    _make_production_line(db_session, item, quantity=40, produced=25, req=req)
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 25.0


def test_i3_scope_overdue_in_and_stale_run_out(db_session):
    """An overdue FIXED_SNAPSHOT stays in scope; a non-latest run of the same
    plan is excluded."""
    # Overdue plan (period fully in the past) — must still be in scope.
    over_item = _make_production_item(db_session, "OV-1")
    over_run = _make_run(db_session, period_from=date(2020, 1, 1), period_to=date(2020, 2, 1))
    over_req = _make_req(db_session, over_run, over_item, net=40)
    _make_production_line(db_session, over_item, quantity=40, produced=40, req=over_req)

    # Same plan, two runs — only the latest (max run_id) is in scope.
    dup_item = _make_production_item(db_session, "DUP-1")
    plan = _make_plan(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    run_old = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), plan=plan)
    run_new = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), plan=plan)
    req_old = _make_req(db_session, run_old, dup_item, net=40)
    req_new = _make_req(db_session, run_new, dup_item, net=40)
    _make_production_line(db_session, dup_item, quantity=40, produced=40, req=req_new)
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(over_req)
    db_session.refresh(req_old)
    db_session.refresh(req_new)
    assert float(over_req.executed_qty) == 40.0  # overdue → in scope
    assert float(req_new.executed_qty) == 40.0   # latest run of plan → in scope
    assert float(req_old.executed_qty) == 0.0    # stale run → out of scope, untouched


def test_i3_shim_raises_on_run_ids(db_session):
    """The retired partial recompute raises loudly; None delegates to the cycle."""
    with pytest.raises(ValueError):
        populate_executed_qty(db_session, [1, 2])

    result = populate_executed_qty(db_session, None)
    assert "cycle_id" in result


def test_i3_reconcile_tail_runs_cycle_and_respects_dry_run(db_session):
    """reconcile_all_active's tail runs the ledger cycle; dry_run rolls it back."""
    from app.services.mrp_reconciliation import (
        reconcile_all_active as public_reconcile_all_active,
    )

    item = _make_production_item(db_session, "RC-1")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db_session, run, item, net=40)
    _make_production_line(db_session, item, quantity=40, produced=40, req=req)
    db_session.commit()

    dry = public_reconcile_all_active(
        db_session, dry_run=True, diagnostic_legacy=True
    )
    assert "execution_ledger" in dry
    assert "cycle_id" in dry["execution_ledger"]
    db_session.expire_all()
    assert float(db_session.get(MrpRequirement, req.id).executed_qty) == 0.0

    public_reconcile_all_active(
        db_session, dry_run=False, diagnostic_legacy=True
    )
    db_session.expire_all()
    assert float(db_session.get(MrpRequirement, req.id).executed_qty) == 40.0


def test_i3_multi_alloc_one_line_queue_order(db_session):
    """One physical line covering two reqs distributes Δ in freeze-queue order."""
    item = _make_purchased_item(db_session, "MA-1")
    run_a = _make_run(db_session, period_from=date(2026, 5, 1), period_to=date(2026, 5, 31), freeze_version=1)
    run_b = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req_a = _make_req(db_session, run_a, item, net=50)
    req_b = _make_req(db_session, run_b, item, net=50)
    _make_baseline(db_session, run_a, item, received_total=0.0, version=1)
    _make_baseline(db_session, run_b, item, received_total=0.0, version=1)
    line = _make_receipt(db_session, item, received=40, quantity=60, order_ref1c="SUP-MA-1")
    alloc_a = _make_freeze_alloc(
        db_session, run_a, req_a, item,
        source_type="supplier_order", source_ref="SUP-MA-1",
        source_line_ref=line.item_id, alloc_qty=30, fact_at_freeze=60,
    )
    alloc_b = _make_freeze_alloc(
        db_session, run_b, req_b, item,
        source_type="supplier_order", source_ref="SUP-MA-1",
        source_line_ref=line.item_id, alloc_qty=30, fact_at_freeze=60,
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(alloc_a)
    db_session.refresh(alloc_b)
    # Δ_line = 40; earlier plan (run_a) realises first: 30, then run_b: 10.
    assert float(alloc_a.realized_qty) == 30.0
    assert float(alloc_b.realized_qty) == 10.0


def test_i3_reverse_below_baseline_no_negative(db_session):
    """produced < baseline → Δ 0; executed drops to 0 (never negative)."""
    item = _make_production_item(db_session, "RV-1")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db_session, run, item, net=40)
    _make_baseline(db_session, run, item, produced_total=100.0, version=1)
    _make_production_line(db_session, item, quantity=100, produced=80, req=req)
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 0.0


def test_i3_cancelled_line_not_a_candidate(db_session):
    """A cancelled production line is never a candidate."""
    item = _make_production_item(db_session, "CX-1")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db_session, run, item, net=40)
    _make_baseline(db_session, run, item, produced_total=0.0, version=1)
    _make_production_line(db_session, item, quantity=40, produced=30, req=req, status="cancelled")
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.executed_qty) == 0.0
    assert _exec_rows(db_session, req) == []


# ===========================================================================
# Increment-4 tests — stock drift (compute_stock_drift + materialisation)
# ===========================================================================

def _drift_component(db, *, comp_stock, parent_produced, norm, comp_s0, net, initial):
    """A parent→component drift fixture: one frozen run whose component (bom
    level 1) has baseline S0=comp_s0 and a frozen norm ``norm`` from the parent.
    ``comp_stock`` is the component's CURRENT effective stock; the parent's
    produced_now is ``parent_produced``."""
    parent = _make_production_item(db, f"DP-PARENT-{net}-{comp_s0}-{comp_stock}", stock=0.0)
    component = _make_production_item(db, f"DP-COMP-{net}-{comp_s0}-{comp_stock}", stock=comp_stock)
    run = _make_run(db, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    comp_req = _make_req(db, run, component, net=net, bom_level=1)
    comp_req.initial_snapshot_stock = initial
    db.flush()
    _make_baseline(db, run, component, produced_total=0.0, received_total=0.0, stock=comp_s0, version=1)
    _make_baseline(db, run, parent, produced_total=0.0, received_total=0.0, stock=0.0, version=1)
    _make_freeze_component(db, run, parent, component, norm=norm, version=1)
    if parent_produced > 0:
        _make_production_line(db, parent, quantity=parent_produced, produced=parent_produced, req=None, source="1c")
    return run, parent, component, comp_req


def test_i4_planned_parent_production_zero_component_drift(db_session):
    """Producing the parent consumes the component by Δproduced×frozen_norm, so
    the component drift is ≈0 and no adjustment / event is raised."""
    # S0=100, parent produced 10 at norm 2 → expected consumption 20 →
    # expected stock 80; current component stock is exactly 80.
    _run, _p, comp, req = _drift_component(
        db_session, comp_stock=80.0, parent_produced=10, norm=2.0, comp_s0=100.0, net=100, initial=100
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.drift_adjustment_qty) == 0.0
    assert _drift_events(db_session, comp, kind="shortfall") == []
    assert _drift_events(db_session, comp, kind="surplus") == []


def test_i4_offplan_stock_drop_first_cycle_pending(db_session):
    """An off-plan stock decrease on cycle 1 is a pending shortfall (matured
    False), so no drift_adjustment is materialised yet."""
    _run, _p, comp, req = _drift_component(
        db_session, comp_stock=90.0, parent_produced=0, norm=1.0, comp_s0=100.0, net=100, initial=100
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.drift_adjustment_qty) == 0.0
    events = _drift_events(db_session, comp, kind="shortfall")
    assert len(events) == 1
    assert float(events[0].drift_qty) == 10.0
    assert bool(events[0].matured) is False


def test_i4_matured_shortfall_after_second_cycle(db_session, monkeypatch):
    """With W=0, the shortfall matures on cycle 2 (≥2 cycles) and materialises as
    a drift_adjustment, oldest-first, capped by initial_snapshot_stock."""
    monkeypatch.setenv("MRP_DRIFT_MATURITY_HOURS", "0")
    _run, _p, comp, req = _drift_component(
        db_session, comp_stock=90.0, parent_produced=0, norm=1.0, comp_s0=100.0, net=100, initial=100
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()
    db_session.refresh(req)
    assert float(req.drift_adjustment_qty) == 0.0  # cycle 1: pending
    first_event = _drift_events(db_session, comp, kind="shortfall")[0]
    assert first_event.ledger_generation_id is not None
    current_generation_id = first_event.ledger_generation_id

    other_batch = models.PhysicalImportBatch(
        batch_key="drift-lineage-other",
        status="completed",
        source_watermarks={},
        completed_at=datetime.now(timezone.utc),
    )
    other_generation = models.LedgerGeneration(
        generation_key="drift-lineage-other",
        status="rejected",
        cutoff=datetime.now(timezone.utc),
        physical_import_batch=other_batch,
        algorithm_version="tests/other",
        source_watermarks={},
        capabilities={},
    )
    db_session.add(other_generation)
    db_session.flush()
    foreign_event = models.MrpDriftEvent(
        ledger_generation_id=other_generation.id,
        cycle_id="foreign-generation",
        item_id=comp.item_id,
        kind="shortfall",
        drift_qty=777,
        matured=True,
    )
    db_session.add(foreign_event)
    db_session.commit()
    foreign_event_id = foreign_event.id

    run_ledger_cycle(db_session)
    db_session.commit()
    db_session.refresh(req)
    assert float(req.drift_adjustment_qty) == 10.0  # cycle 2: matured
    assert db_session.get(models.MrpDriftEvent, foreign_event_id) is not None
    current_events = (
        db_session.query(models.MrpDriftEvent)
        .filter(
                models.MrpDriftEvent.item_id == comp.item_id,
                models.MrpDriftEvent.ledger_generation_id == current_generation_id,
            models.MrpDriftEvent.kind == "shortfall",
        )
        .all()
    )
    assert len(current_events) == 1
    assert bool(current_events[0].matured) is True


def test_i4_shortfall_over_initial_is_unattributed(db_session, monkeypatch):
    """A shortfall larger than Σ initial_snapshot_stock is capped; the excess is
    unattributed (never materialised)."""
    monkeypatch.setenv("MRP_DRIFT_MATURITY_HOURS", "0")
    _run, _p, comp, req = _drift_component(
        db_session, comp_stock=90.0, parent_produced=0, norm=1.0, comp_s0=100.0, net=100, initial=5
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()
    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.drift_adjustment_qty) == 5.0  # capped at initial
    event = _drift_events(db_session, comp, kind="shortfall")[0]
    assert float((event.details or {}).get("unattributed")) == 5.0


def test_i4_surplus_reduces_adjustment_capped_at_open_deficit(db_session, monkeypatch):
    """A matured surplus becomes a NEGATIVE adjustment, capped by the open
    deficit (net + evap − executed)."""
    monkeypatch.setenv("MRP_DRIFT_MATURITY_HOURS", "0")
    _run, _p, comp, req = _drift_component(
        db_session, comp_stock=130.0, parent_produced=0, norm=1.0, comp_s0=100.0, net=100, initial=100
    )
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()
    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.drift_adjustment_qty) == -30.0


def test_i4_frozen_bom_norm_survives_spec_change(db_session):
    """Drift uses the FROZEN component norm, never the current SpecComponent: a
    changed SpecComponent qty does not move the drift."""
    _run, parent, comp, req = _drift_component(
        db_session, comp_stock=80.0, parent_produced=10, norm=2.0, comp_s0=100.0, net=100, initial=100
    )
    # Current SpecComponent says 5/unit — if drift read it, expected consumption
    # would be 50 and a +30 surplus would appear. It must NOT.
    spec = Specification(spec_name="cur", spec_ref1c="cur-ref")
    db_session.add(spec)
    db_session.flush()
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=5))
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.drift_adjustment_qty) == 0.0
    assert _drift_events(db_session, comp, kind="surplus") == []


def test_i4_three_cycles_adjustment_stable(db_session, monkeypatch):
    """Drift recomputes from scratch each cycle — the adjustment does not
    accumulate across repeated cycles over unchanged facts."""
    monkeypatch.setenv("MRP_DRIFT_MATURITY_HOURS", "0")
    _run, _p, _comp, req = _drift_component(
        db_session, comp_stock=90.0, parent_produced=0, norm=1.0, comp_s0=100.0, net=100, initial=100
    )
    db_session.commit()

    values = []
    for _ in range(3):
        run_ledger_cycle(db_session)
        db_session.commit()
        db_session.refresh(req)
        values.append(float(req.drift_adjustment_qty))
    # cycle1 pending (0), cycles 2 and 3 matured and identical (no accumulation).
    assert values[0] == 0.0
    assert values[1] == 10.0
    assert values[2] == 10.0


def test_i4_bom_level_zero_out_of_drift(db_session, monkeypatch):
    """A pool whose minimum bom_level is 0 (a shipped finished good) is excluded
    from drift even when its stock drops."""
    monkeypatch.setenv("MRP_DRIFT_MATURITY_HOURS", "0")
    item = _make_production_item(db_session, "D0-1", stock=90.0)
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db_session, run, item, net=100, bom_level=0)
    req.initial_snapshot_stock = 100
    _make_baseline(db_session, run, item, stock=100.0, version=1)
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()
    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    assert float(req.drift_adjustment_qty) == 0.0
    assert _drift_events(db_session, item) == []


def test_i4_chain_reset_on_recovery(db_session, monkeypatch):
    """A shortfall chain that recovers (drift returns to 0) resets: a later
    shortfall is pending again, not immediately matured."""
    monkeypatch.setenv("MRP_DRIFT_MATURITY_HOURS", "0")
    _run, _p, comp, req = _drift_component(
        db_session, comp_stock=90.0, parent_produced=0, norm=1.0, comp_s0=100.0, net=100, initial=100
    )
    db_session.commit()

    run_ledger_cycle(db_session)  # cycle 1: shortfall pending
    db_session.commit()
    # Recovery: stock back to expected 100 → drift 0, chain broken.
    comp.stock_qty = 100.0
    db_session.commit()
    run_ledger_cycle(db_session)  # cycle 2: no event
    db_session.commit()
    assert _drift_events(db_session, comp, kind="shortfall") == []
    # New shortfall.
    comp.stock_qty = 90.0
    db_session.commit()
    run_ledger_cycle(db_session)  # cycle 3: fresh chain → pending again
    db_session.commit()

    db_session.refresh(req)
    assert float(req.drift_adjustment_qty) == 0.0
    events = _drift_events(db_session, comp, kind="shortfall")
    assert len(events) == 1
    assert bool(events[0].matured) is False


def test_i4_bucket_cap_extension_lets_drift_topup_execute(db_session):
    """The first bucket cap is widened by a positive drift_adjustment so the
    drift top-up can execute to the full effective_net (§4 bucket-cap)."""
    item = _make_production_item(db_session, "BK-1")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30), freeze_version=1)
    req = _make_req(db_session, run, item, net=40, bom_level=1)
    b1 = _make_bucket(db_session, req, bucket_date=date(2026, 6, 10), net_qty=20)
    b2 = _make_bucket(db_session, req, bucket_date=date(2026, 6, 20), net_qty=20)
    # A prior cycle materialised a +20 drift_adjustment (effective_net = 60).
    req.drift_adjustment_qty = 20
    # Unlinked 1C production of 60 for the item (no baseline → full-history Δ).
    _make_production_line(db_session, item, quantity=60, produced=60, req=None, source="1c")
    db_session.commit()

    run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    # The extended first bucket (20+20=40) plus the second (20) absorb all 60.
    assert float(req.executed_qty) == 60.0
    by_bucket = {r.bucket_id: float(r.allocated_qty) for r in _exec_rows(db_session, req)}
    assert by_bucket.get(b1.id) == 40.0
    assert by_bucket.get(b2.id) == 20.0


# ===========================================================================
# Increment-5 tests — plan closure by execution
# ===========================================================================

def test_i5_satisfied_requirement_and_run_auto_close(db_session):
    """I1/I4: executed ≥ effective_net → req status='closed', closed_at set; a
    run whose every requirement is closed becomes CLOSED with finished_at, and
    drops out of the ledger scope."""
    item = _make_production_item(db_session, "I5-SAT")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req = _make_req(db_session, run, item, net=40)
    _make_production_line(db_session, item, quantity=40, produced=40, req=req)
    db_session.commit()

    summary = run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    db_session.refresh(run)
    assert req.status == "closed"
    assert req.closed_at is not None
    assert float(req.executed_qty) == 40.0
    assert run.status == "CLOSED"
    assert run.finished_at is not None
    assert summary["requirements_closed"] == 1
    assert summary["runs_closed"] == [run.run_id]
    # Closed run (no open req) drops out of the canonical scope next cycle.
    assert run.run_id not in _scope_run_ids(db_session)


def test_i5_short_requirement_stays_open(db_session):
    """I2: executed < effective_net → requirement stays open, run stays
    FIXED_SNAPSHOT. No date closes it."""
    item = _make_production_item(db_session, "I5-SHORT")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req = _make_req(db_session, run, item, net=40)
    _make_production_line(db_session, item, quantity=40, produced=25, req=req)
    db_session.commit()

    summary = run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    db_session.refresh(run)
    assert req.status == "open"
    assert req.closed_at is None
    assert run.status == "FIXED_SNAPSHOT"
    assert summary["requirements_closed"] == 0
    assert summary["runs_closed"] == []


def test_i5_net_zero_alongside_deficit_stays_open(db_session):
    """I3 (owner ruling 21.07): a net=0 requirement (covered by frozen stock, not
    by execution) sitting ALONGSIDE an open real deficit in the SAME run is NOT
    closed individually — it stays open-but-zero, in scope, visible to
    drift/evaporation. It closes only when the whole run closes."""
    zero_item = _make_production_item(db_session, "I5-ZERO")
    deficit_item = _make_production_item(db_session, "I5-DEFICIT")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    zero_req = _make_req(db_session, run, zero_item, net=0)
    deficit_req = _make_req(db_session, run, deficit_item, net=40)
    _make_production_line(db_session, deficit_item, quantity=40, produced=10, req=deficit_req)
    db_session.commit()

    summary = run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(zero_req)
    db_session.refresh(deficit_req)
    db_session.refresh(run)
    # The open deficit keeps the run alive → the net=0 tail is NOT swept.
    assert zero_req.status == "open"
    assert zero_req.closed_at is None
    assert deficit_req.status == "open"
    assert run.status == "FIXED_SNAPSHOT"
    assert summary["requirements_closed"] == 0
    assert summary["runs_closed"] == []
    assert run.run_id in _scope_run_ids(db_session)


def test_i5_run_with_mixed_reqs_stays_open(db_session):
    """A run auto-closes ONLY when every requirement is closed; one still-open
    req keeps the run FIXED_SNAPSHOT."""
    done_item = _make_production_item(db_session, "I5-MIX-DONE")
    open_item = _make_production_item(db_session, "I5-MIX-OPEN")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    done_req = _make_req(db_session, run, done_item, net=40)
    open_req = _make_req(db_session, run, open_item, net=40)
    _make_production_line(db_session, done_item, quantity=40, produced=40, req=done_req)
    _make_production_line(db_session, open_item, quantity=40, produced=10, req=open_req)
    db_session.commit()

    summary = run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(done_req)
    db_session.refresh(open_req)
    db_session.refresh(run)
    assert done_req.status == "closed"
    assert open_req.status == "open"
    assert run.status == "FIXED_SNAPSHOT"
    assert summary["requirements_closed"] == 1
    assert summary["runs_closed"] == []
    assert run.run_id in _scope_run_ids(db_session)


def test_i5_run_closes_and_sweeps_net_zero_tail(db_session):
    """I4 (extended): a run with one real deficit (net>0) + one net=0 req. While
    the deficit is short the run stays FIXED_SNAPSHOT and BOTH reqs open; once the
    deficit is executed to net, the run→CLOSED and BOTH reqs close — the net=0
    tail is swept by the run closure."""
    deficit_item = _make_production_item(db_session, "I5-TAIL-DEF")
    zero_item = _make_production_item(db_session, "I5-TAIL-ZERO")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    deficit_req = _make_req(db_session, run, deficit_item, net=40)
    zero_req = _make_req(db_session, run, zero_item, net=0)
    product = _make_production_line(db_session, deficit_item, quantity=40, produced=10, req=deficit_req)
    db_session.commit()

    # Cycle 1: deficit short → run open, both reqs open.
    s1 = run_ledger_cycle(db_session)
    db_session.commit()
    db_session.refresh(deficit_req)
    db_session.refresh(zero_req)
    db_session.refresh(run)
    assert deficit_req.status == "open"
    assert zero_req.status == "open"
    assert run.status == "FIXED_SNAPSHOT"
    assert s1["runs_closed"] == []

    # Execute the deficit to its full net.
    product.produced_qty = 40
    product.remaining_qty = 0
    db_session.commit()

    # Cycle 2: no open deficit → run CLOSED, both reqs closed (net=0 tail swept).
    s2 = run_ledger_cycle(db_session)
    db_session.commit()
    db_session.refresh(deficit_req)
    db_session.refresh(zero_req)
    db_session.refresh(run)
    assert deficit_req.status == "closed"
    assert zero_req.status == "closed"
    assert zero_req.closed_at is not None
    assert run.status == "CLOSED"
    assert run.finished_at is not None
    assert s2["runs_closed"] == [run.run_id]
    assert run.run_id not in _scope_run_ids(db_session)


def test_i5_overdue_but_short_run_stays_active(db_session):
    """I5: an overdue FIXED_SNAPSHOT with an open under-executed req (june runs
    13/14 with executed=0) is NOT closed by the passed period — it stays
    FIXED_SNAPSHOT and in the scope. Only execution closes a plan."""
    item = _make_production_item(db_session, "I5-OVERDUE")
    # Period fully in the past relative to the test date (2026-07-20).
    run = _make_run(db_session, period_from=date(2020, 1, 1), period_to=date(2020, 2, 1))
    req = _make_req(db_session, run, item, net=40)
    # executed 0 (no production) → deficit stays visible.
    db_session.commit()

    summary = run_ledger_cycle(db_session)
    db_session.commit()

    db_session.refresh(req)
    db_session.refresh(run)
    assert req.status == "open"
    assert run.status == "FIXED_SNAPSHOT"
    assert summary["requirements_closed"] == 0
    assert summary["runs_closed"] == []
    assert run.run_id in _scope_run_ids(db_session)


def test_i5_empty_scope_returns_identical_shape_no_closure(db_session):
    """I10: with no open requirements in scope, run_ledger_cycle returns the
    byte-identical early shape — it never writes a closure key."""
    summary = run_ledger_cycle(db_session)
    assert summary["items_touched"] == 0
    assert summary["runs"] == []
    # The empty-scope early return does not thread the closure keys.
    assert "requirements_closed" not in summary
    assert "runs_closed" not in summary


def test_i5_closure_is_idempotent(db_session):
    """I11: a second cycle over unchanged facts closes nothing more and does not
    thrash the already-closed requirement / run."""
    item = _make_production_item(db_session, "I5-IDEM")
    run = _make_run(db_session, period_from=date(2026, 6, 1), period_to=date(2026, 6, 30))
    req = _make_req(db_session, run, item, net=40)
    _make_production_line(db_session, item, quantity=40, produced=40, req=req)
    db_session.commit()

    first = run_ledger_cycle(db_session)
    db_session.commit()
    db_session.refresh(req)
    first_closed_at = req.closed_at

    second = run_ledger_cycle(db_session)
    db_session.commit()
    db_session.refresh(req)

    assert first["requirements_closed"] == 1
    assert first["runs_closed"] == [run.run_id]
    # The closed run drops out of scope → the second cycle sees no open req at
    # all and returns the byte-identical empty-scope shape (no closure keys).
    assert second["runs"] == []
    assert "requirements_closed" not in second
    assert req.status == "closed"
    assert req.closed_at == first_closed_at  # not re-stamped

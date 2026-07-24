"""Tests for MRP reconciliation and the covered_qty rollback that feeds it."""

from datetime import date, datetime
import pytest

from app.models import (
    DefaultSpecification,
    DbrFeederSignal,
    Item,
    LedgerGeneration,
    MrpFreezeAllocation,
    MrpRequirement,
    PaintWeldPair,
    PlannedPurchase,
    PhysicalImportBatch,
    PlanningTruthState,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionKind,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionProduct,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionResource,
    ResourceProductionKind,
    SpecComponent,
    Specification,
    SupplierOrder,
    SupplierOrderItem,
    SyncLink,
)
from app.services.mrp_execution_ledger import _scope_run_ids
from app.services.mrp_reconciliation import (
    _latest_active_snapshot_run_ids,
    force_close_run,
    reconcile_all_active as _public_reconcile_all_active,
    reconcile_snapshot as _public_reconcile_snapshot,
    reopen_run,
)
from app.services.period_plan_service import create_mrp_snapshot_from_period_plan
from app.services.production_binding_repair import repair_clean_mrp_bindings
from app.services.production_control_journal import (
    create_production_orders_from_mrp_requirements,
    update_line_state,
    update_product_quantity,
)


DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"


def reconcile_snapshot(db, run_id, **kwargs):
    _attach_diagnostic_proposal_lineage(db)
    return _public_reconcile_snapshot(
        db, run_id, diagnostic_legacy=True, **kwargs
    )


def reconcile_all_active(db, **kwargs):
    _attach_diagnostic_proposal_lineage(db)
    return _public_reconcile_all_active(db, diagnostic_legacy=True, **kwargs)


def _publish_plan_snapshot(db, plan_id):
    """Publish through the current atomic Ledger workflow.

    The key is deterministic per isolated test plan, matching the public
    endpoint's required idempotency contract.
    """
    result = create_mrp_snapshot_from_period_plan(
        db,
        plan_id,
        generation_key=f"mrp-reconciliation-{plan_id}",
    )
    # The service intentionally leaves transaction ownership to its caller.
    # These tests emulate the API boundary, which commits the atomic publish.
    db.commit()
    return result


@pytest.fixture(autouse=True)
def _accepted_planning_truth(db_session):
    """Existing sizing scenarios explicitly run under an accepted truth."""
    batch = PhysicalImportBatch(
        batch_key="mrp-reconciliation-diagnostic",
        status="completed",
        cutoff=datetime(2026, 7, 23),
        source_watermarks={"source": "test-diagnostic"},
        completed_at=datetime(2026, 7, 23),
    )
    generation = LedgerGeneration(
        generation_key="mrp-reconciliation-diagnostic",
        status="accepted",
        cutoff=datetime(2026, 7, 23),
        source_watermarks={"replay_from": "2026-06-01T00:00:00+00:00"},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
        physical_import_batch=batch,
        algorithm_version="test/diagnostic",
        accepted_at=datetime(2026, 7, 23),
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(
        PlanningTruthState(id=1, current_generation_id=generation.id)
    )
    db_session.flush()
    db_session.info["diagnostic_ledger_generation_id"] = generation.id
    db_session.info["accepted_ledger_generation_id"] = generation.id


def _attach_diagnostic_proposal_lineage(db):
    """Every unlineaged proposal in this isolated legacy scenario is owned by
    its explicit diagnostic generation. Production code never performs this
    backfill and continues to exclude NULL lineage."""
    generation_id = int(db.info["diagnostic_ledger_generation_id"])
    db.query(ProductionProduct).filter(
        ProductionProduct.ledger_generation_id.is_(None)
    ).update(
        {"ledger_generation_id": generation_id},
        synchronize_session=False,
    )
    db.query(PlannedPurchase).filter(
        PlannedPurchase.ledger_generation_id.is_(None)
    ).update(
        {"ledger_generation_id": generation_id},
        synchronize_session=False,
    )
    db.flush()


def test_reconciliation_selector_uses_only_current_pointer_generation(db_session):
    plan = ProductionPlanHeader(
        name="selector-plan", period_from=date(2026, 6, 1), period_to=date(2026, 6, 30),
        status="fixed",
    )
    db_session.add(plan)
    db_session.flush()
    foreign_batch = PhysicalImportBatch(
        batch_key="reconciliation-selector-foreign", status="completed",
        cutoff=datetime(2026, 7, 22), source_watermarks={"test": True},
    )
    db_session.add(foreign_batch)
    db_session.flush()
    foreign_generation = LedgerGeneration(
        generation_key="reconciliation-selector-foreign", status="accepted",
        cutoff=foreign_batch.cutoff, accepted_at=foreign_batch.cutoff,
        physical_import_batch_id=foreign_batch.id, algorithm_version="test", replay_version="test",
        source_watermarks={"test": True}, capabilities={},
    )
    db_session.add(foreign_generation)
    db_session.flush()
    current = PlanningRun(
        status="FIXED_SNAPSHOT", config_snapshot={}, pinned=True, source_plan_id=plan.id,
        period_from=plan.period_from, period_to=plan.period_to,
        ledger_generation_id=db_session.info["accepted_ledger_generation_id"],
    )
    foreign = PlanningRun(
        status="FIXED_SNAPSHOT", config_snapshot={}, pinned=True, source_plan_id=plan.id,
        period_from=plan.period_from, period_to=plan.period_to, ledger_generation_id=foreign_generation.id,
    )
    legacy = PlanningRun(
        status="FIXED_SNAPSHOT", config_snapshot={}, pinned=True, source_plan_id=plan.id,
        period_from=plan.period_from, period_to=plan.period_to, ledger_generation_id=None,
    )
    db_session.add_all([current, foreign, legacy])
    db_session.flush()

    assert _latest_active_snapshot_run_ids(db_session) == [current.run_id]


def test_reconciliation_selector_fails_closed_without_truth_pointer(db_session):
    from app.services.planning_truth import PlanningTruthUnavailable

    db_session.delete(db_session.get(PlanningTruthState, 1))
    db_session.flush()

    with pytest.raises(PlanningTruthUnavailable, match="No Item Ledger generation"):
        _latest_active_snapshot_run_ids(db_session)


def test_reconcile_all_active_fails_closed_without_accepted_truth(
    db_session, monkeypatch
):
    from app.services import planning_truth

    monkeypatch.setattr(
        planning_truth,
        "require_accepted_truth",
        lambda db, consumer, **kwargs: planning_truth.require_accepted(db),
    )
    db_session.delete(db_session.get(PlanningTruthState, 1))
    db_session.flush()

    result = reconcile_all_active(db_session)

    assert result["status"] == "blocked"
    assert result["truth_status"] == "uninitialized"
    assert result["runs_checked"] == 0
    assert result["execution_ledger"] is None


def test_public_reconcile_uses_accepted_generation_and_writes_nothing_without_runs(
    db_session,
):
    before_products = db_session.query(ProductionProduct).count()
    before_purchases = db_session.query(PlannedPurchase).count()

    result = _public_reconcile_all_active(db_session)

    assert result["status"] == "ok"
    assert result["runs_checked"] == 0
    assert result["execution_ledger"]["source"] == (
        "reservation_event+mrp_execution_allocation"
    )
    assert db_session.query(ProductionProduct).count() == before_products
    assert db_session.query(PlannedPurchase).count() == before_purchases


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
        item_name=f"Закупаемая деталь {code}",
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


def _make_requirement_with_line(db, item, *, net, covered, produced, quantity):
    """A standalone requirement + materialized production line (no snapshot)."""
    run = PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={}, pinned=True)
    db.add(run)
    db.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=net,
        net_required_qty=net,
        covered_qty=covered,
        remaining_qty=max(net - covered, 0.0),
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        bom_level=0,
    )
    db.add(req)
    db.flush()
    order = ProductionOrder(
        order_number=f"O-{item.item_code}",
        order_date=date(2026, 6, 1),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
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
        source_mrp_requirement_id=req.id,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="in_progress",
            issue_status="not_requested",
        )
    )
    db.flush()
    return req, product


def _make_spec(db, item: Item, name: str, *, resource_name: str | None = None) -> Specification:
    kind = None
    if resource_name:
        kind = ProductionKind(ref_1c=f"kind-{name}", name=f"Kind {name}")
        resource = ProductionResource(resource_name=resource_name)
        db.add_all([kind, resource])
        db.flush()
        db.add(ResourceProductionKind(resource_id=resource.resource_id, production_kind_id=kind.id))
    spec = Specification(
        spec_code=name,
        spec_name=name,
        spec_ref1c=f"spec-{name}",
        production_kind_id=kind.id if kind else None,
    )
    db.add(spec)
    db.flush()
    return spec


def test_repair_clean_mrp_line_updates_default_spec_and_clears_auto_workshop(db_session):
    item = _make_production_item(db_session, "P-BIND")
    component = _make_production_item(db_session, "P-BIND-COMP")
    old_spec = _make_spec(db_session, item, "OLD", resource_name="Старый участок")
    new_spec = _make_spec(db_session, item, "NEW", resource_name="Новый участок")
    db_session.add(SpecComponent(spec_id=new_spec.spec_id, item_id=component.item_id, quantity=2))
    db_session.add(DefaultSpecification(item_id=item.item_id, spec_id=old_spec.spec_id))
    run = PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={}, pinned=True)
    db_session.add(run)
    db_session.flush()
    order = ProductionOrder(
        order_number="MRP-RC-TEST",
        order_date=date(2026, 6, 1),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
        spec_id=old_spec.spec_id,
    )
    db_session.add(product)
    db_session.flush()
    state = ProductionOrderLineState(
        product_id=product.product_id,
        status="to_move",
        issue_status="requested",
        workshop_id=999,
        workshop_id_source="auto",
    )
    db_session.add(state)
    issue = ProductionMaterialIssue(
        document_number="MI-DRAFT",
        product_id=product.product_id,
        order_id=order.order_id,
        status="draft",
        direction="issue",
    )
    db_session.add(issue)
    db_session.flush()
    db_session.add(
        ProductionMaterialIssueLine(
            issue_id=issue.issue_id,
            component_item_id=component.item_id,
            required_qty=1,
            issued_qty=0,
            source_spec_id=old_spec.spec_id,
        )
    )
    db_session.flush()

    default = db_session.query(DefaultSpecification).filter_by(item_id=item.item_id).one()
    default.spec_id = new_spec.spec_id
    stats = repair_clean_mrp_bindings(db_session, run_id=run.run_id)
    db_session.commit()

    db_session.refresh(product)
    db_session.refresh(state)
    assert stats["spec_updated"] == 1
    assert stats["workshop_auto_cleared"] == 1
    assert stats["local_issues_deleted"] == 1
    assert product.spec_id == new_spec.spec_id
    assert state.workshop_id is None
    assert state.workshop_id_source is None
    assert state.issue_status == "not_requested"
    assert db_session.query(ProductionMaterialIssue).filter_by(product_id=product.product_id).count() == 0


def test_repair_clean_mrp_line_blocks_after_production_order_export(db_session):
    item = _make_production_item(db_session, "P-BLOCK")
    old_spec = _make_spec(db_session, item, "BLOCK-OLD")
    new_spec = _make_spec(db_session, item, "BLOCK-NEW")
    db_session.add(DefaultSpecification(item_id=item.item_id, spec_id=new_spec.spec_id))
    run = PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={}, pinned=True)
    db_session.add(run)
    db_session.flush()
    order = ProductionOrder(
        order_number="MRP-BLOCK",
        order_date=date(2026, 6, 1),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
        spec_id=old_spec.spec_id,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        SyncLink(
            source_system="PRODPLAN",
            source_doctype="production_order",
            source_id=order.order_id,
            target_system="1C",
            target_entity="Document_ЗаказНаПроизводство",
            target_ref_key="11111111-1111-1111-1111-111111111111",
            status="success",
        )
    )
    db_session.flush()

    stats = repair_clean_mrp_bindings(db_session, run_id=run.run_id)
    db_session.commit()

    db_session.refresh(product)
    assert product.spec_id == old_spec.spec_id
    assert stats["spec_updated"] == 0
    assert stats["blocked"]["order_in_1c"] == 1


def test_cancelling_partial_line_releases_requirement_coverage(db_session):
    item = _make_production_item(db_session, "P-CLOSE")
    req, product = _make_requirement_with_line(
        db_session, item, net=40, covered=40, produced=10, quantity=40
    )

    update_line_state(db_session, product.product_id, {"status": "cancelled"})

    db_session.refresh(req)
    db_session.refresh(product)
    assert float(product.remaining_qty) == 0.0
    # 30 un-produced units released back into the requirement's open demand.
    assert float(req.covered_qty) == 10.0
    assert float(req.remaining_qty) == 30.0


def test_manual_completed_rejects_unproduced_remainder(db_session):
    item = _make_production_item(db_session, "P-COMPLETE-GUARD")
    req, product = _make_requirement_with_line(
        db_session, item, net=40, covered=40, produced=10, quantity=40
    )

    with pytest.raises(ValueError, match="Нельзя вручную завершить"):
        update_line_state(db_session, product.product_id, {"status": "completed"})

    db_session.refresh(req)
    db_session.refresh(product)
    assert float(product.remaining_qty) == 30.0
    assert float(req.covered_qty) == 40.0
    assert float(req.remaining_qty) == 0.0


def test_closing_fully_produced_line_releases_nothing(db_session):
    item = _make_production_item(db_session, "P-FULL")
    req, product = _make_requirement_with_line(
        db_session, item, net=40, covered=40, produced=40, quantity=40
    )

    update_line_state(db_session, product.product_id, {"status": "completed"})

    db_session.refresh(req)
    assert float(req.covered_qty) == 40.0
    assert float(req.remaining_qty) == 0.0


def test_reducing_quantity_releases_coverage(db_session):
    item = _make_production_item(db_session, "P-REDUCE")
    req, product = _make_requirement_with_line(
        db_session, item, net=40, covered=40, produced=10, quantity=40
    )

    update_product_quantity(db_session, product.product_id, 25)

    db_session.refresh(req)
    assert float(req.covered_qty) == 25.0
    assert float(req.remaining_qty) == 15.0


def test_cancel_then_terminal_again_is_idempotent(db_session):
    item = _make_production_item(db_session, "P-IDEM")
    req, product = _make_requirement_with_line(
        db_session, item, net=40, covered=40, produced=10, quantity=40
    )

    update_line_state(db_session, product.product_id, {"status": "cancelled"})
    update_line_state(db_session, product.product_id, {"status": "completed"})

    db_session.refresh(req)
    # Release happens once: a second terminal transition must not double-release.
    assert float(req.covered_qty) == 10.0
    assert float(req.remaining_qty) == 30.0


# ---------------------------------------------------------------------------
# Part 2 — reconciliation top-up
# ---------------------------------------------------------------------------

def test_reconcile_tops_up_after_partial_close(db_session):
    item = _make_production_item(db_session, "P-RECON", stock=0.0)
    plan = ProductionPlanHeader(
        name="План июнь",
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        status="fixed",
        created_by="test",
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(
        ProductionPlanLine(
            plan_id=plan.id, item_id=item.item_id, bucket_date=date(2026, 6, 15), qty=40
        )
    )
    db_session.commit()

    snap = _publish_plan_snapshot(db_session, plan.id)
    run_id = snap["run_id"]
    req = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == item.item_id)
        .one()
    )
    assert float(req.net_required_qty) == 40.0

    # Materialize the production order from the requirement → becomes open WIP.
    create_production_orders_from_mrp_requirements(db_session, [req.id])
    db_session.commit()

    # With the full order open as WIP, reconciliation finds no gap.
    res = reconcile_snapshot(db_session, run_id)
    assert res["production_added"] == []

    # Produce 10, cancel the line: 30 un-produced units are released; the 10
    # produced are returned to stock (simulate the stock effect).
    product = (
        db_session.query(ProductionProduct)
        .filter(ProductionProduct.source_mrp_requirement_id == req.id)
        .one()
    )
    product.produced_qty = 10
    product.remaining_qty = 30
    db_session.flush()
    update_line_state(db_session, product.product_id, {"status": "cancelled"})
    item.stock_qty = 10
    db_session.commit()

    # Finished-goods stock does not replace the approved release programme.
    # With the original order cancelled, reconciliation recreates all 40.
    res = reconcile_snapshot(db_session, run_id)
    added = res["production_added"]
    assert len(added) == 1
    assert added[0]["item_id"] == item.item_id
    assert abs(added[0]["qty"] - 40.0) < 1e-6

    # Running again is idempotent: the new order is open WIP, so no further gap.
    res = reconcile_snapshot(db_session, run_id)
    assert res["production_added"] == []


def test_diagnostic_reconcile_does_not_treat_legacy_dbr_wip_as_ledger_coverage(
    db_session,
):
    item = _make_production_item(db_session, "P-DBR-OWNED", stock=0.0)
    plan = ProductionPlanHeader(
        name="План DBR ownership",
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        status="fixed",
        created_by="test",
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(ProductionPlanLine(
        plan_id=plan.id,
        item_id=item.item_id,
        bucket_date=date(2026, 6, 15),
        qty=20,
    ))
    db_session.commit()
    run_id = _publish_plan_snapshot(db_session, plan.id)["run_id"]
    req = db_session.query(MrpRequirement).filter_by(
        run_id=run_id,
        item_id=item.item_id,
    ).one()
    signal = DbrFeederSignal(
        dedup_key="R:P-DBR-OWNED",
        signal_type="Пополнение",
        item_id=item.item_id,
        warehouse_ref1c="W2",
        status="Open",
        suggested_qty=7,
        priority=1,
    )
    db_session.add(signal)
    db_session.flush()
    order = ProductionOrder(
        order_number=f"DBR-S{signal.id}",
        order_date=date(2026, 6, 1),
        source="dbr",
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=7,
        produced_qty=0,
        remaining_qty=7,
        source_dbr_signal_id=signal.id,
    ))
    db_session.commit()

    result = reconcile_snapshot(db_session, run_id)

    assert len(result["production_added"]) == 1
    # A mutable DBR journal line is not accepted Ledger evidence.  Even the
    # explicit legacy diagnostic must not subtract it from the frozen demand.
    assert result["production_added"][0]["qty"] == 20.0
    assert result["production_trimmed"] == []
    assert result["dbr_owned_skipped"] == []
    db_session.refresh(req)
    assert float(req.covered_qty) == 20.0
    assert float(req.remaining_qty) == 0.0
    assert db_session.query(ProductionOrder).filter(
        ProductionOrder.source == "mrp",
        ProductionOrder.source_run_id == run_id,
    ).count() == 1


def test_reconcile_tops_up_when_stock_drops_after_snapshot(db_session):
    item = _make_production_item(db_session, "P-STOCK-DROP", stock=32.0)
    plan = ProductionPlanHeader(
        name="План июнь",
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        status="fixed",
        created_by="test",
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(
        ProductionPlanLine(
            plan_id=plan.id, item_id=item.item_id, bucket_date=date(2026, 6, 15), qty=34
        )
    )
    db_session.commit()

    snap = _publish_plan_snapshot(db_session, plan.id)
    run_id = snap["run_id"]
    req = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == item.item_id)
        .one()
    )
    assert float(req.total_required_qty) == 34.0
    assert float(req.net_required_qty) == 34.0

    create_production_orders_from_mrp_requirements(db_session, [req.id])
    item.stock_qty = 17.0
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)

    assert res["production_added"] == []
    db_session.refresh(req)
    assert float(req.net_required_qty) == 34.0
    assert float(req.covered_qty) == 34.0
    assert float(req.remaining_qty) == 0.0

    res = reconcile_snapshot(db_session, run_id)
    assert res["production_added"] == []


def test_diagnostic_reconcile_ignores_unpublished_manual_catchup_line(db_session):
    item = _make_production_item(db_session, "P-STOCK-DROP-RC", stock=32.0)
    plan = ProductionPlanHeader(
        name="План июнь",
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        status="fixed",
        created_by="test",
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(
        ProductionPlanLine(
            plan_id=plan.id, item_id=item.item_id, bucket_date=date(2026, 6, 15), qty=34
        )
    )
    db_session.commit()

    snap = _publish_plan_snapshot(db_session, plan.id)
    run_id = snap["run_id"]
    req = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == item.item_id)
        .one()
    )
    order = ProductionOrder(
        order_number=f"MRP-RC-{run_id}-{item.item_id}",
        order_date=date(2026, 6, 15),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=run_id,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=2,
        produced_qty=0,
        remaining_qty=2,
        source_mrp_requirement_id=req.id,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, status="partial"))
    item.stock_qty = 17.0
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)

    added = res["production_added"]
    assert len(added) == 1
    # The hand-written journal line has no accepted execution allocation.  It
    # must not silently become factual coverage of the Ledger obligation.
    assert abs(added[0]["qty"] - 34.0) < 1e-6
    db_session.refresh(product)
    db_session.refresh(req)
    assert float(product.quantity) == 2.0
    # Structural diagnostic repair may close the unrecognised local line, but
    # it still cannot use it to reduce the Ledger-sized proposal.
    assert float(product.remaining_qty) == 0.0
    assert float(req.net_required_qty) == 34.0
    assert float(req.covered_qty) == 34.0
    assert float(req.remaining_qty) == 0.0


def test_reconcile_splits_catchup_order_by_optimal_batch(db_session):
    item = _make_production_item(db_session, "P-STOCK-DROP-BATCH", stock=32.0)
    item.optimal_batch = 15
    plan = ProductionPlanHeader(
        name="План июнь",
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        status="fixed",
        created_by="test",
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(
        ProductionPlanLine(
            plan_id=plan.id, item_id=item.item_id, bucket_date=date(2026, 6, 15), qty=34
        )
    )
    db_session.commit()

    snap = _publish_plan_snapshot(db_session, plan.id)
    run_id = snap["run_id"]
    req = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == item.item_id)
        .one()
    )
    create_production_orders_from_mrp_requirements(db_session, [req.id])
    item.stock_qty = 0.0
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)

    added = res["production_added"]
    assert added == []
    products = (
        db_session.query(ProductionProduct)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .filter(ProductionOrder.source_run_id == run_id, ProductionProduct.item_id == item.item_id)
        .order_by(ProductionProduct.product_id.asc())
        .all()
    )
    assert [float(product.quantity) for product in products] == [15.0, 15.0, 4.0]


def test_diagnostic_reconcile_does_not_accept_legacy_coverage_counters(db_session):
    item = _make_production_item(db_session, "P-BATCH-REPAIR", stock=0.0)
    item.optimal_batch = 15
    plan = ProductionPlanHeader(
        name="План июнь",
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        status="fixed",
        created_by="test",
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(
        ProductionPlanLine(
            plan_id=plan.id, item_id=item.item_id, bucket_date=date(2026, 6, 15), qty=67
        )
    )
    db_session.commit()

    snap = _publish_plan_snapshot(db_session, plan.id)
    run_id = snap["run_id"]
    req = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == item.item_id)
        .one()
    )
    order = ProductionOrder(
        order_number=f"MRP-RC-{run_id}-{item.item_id}",
        order_date=date(2026, 6, 15),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=run_id,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=67,
        produced_qty=0,
        remaining_qty=67,
        source_mrp_requirement_id=req.id,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, status="shortage"))
    req.covered_qty = 67
    req.remaining_qty = 0
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)

    # covered_qty/remaining_qty and a legacy ProductionProduct are mutable
    # counters, not accepted Ledger execution.  A full Ledger-sized proposal is
    # therefore produced instead of accepting those counters as fact.
    assert len(res["production_added"]) == 1
    assert res["production_added"][0]["qty"] == 67.0


def _link_bom(db, parent: Item, child: Item, qty_per_unit: float = 1.0) -> None:
    """Give `parent` a default spec whose single component is `child`."""
    spec = Specification(
        spec_name=f"Spec {parent.item_code}",
        spec_ref1c=f"spec-{parent.item_code}",
    )
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=child.item_id, quantity=qty_per_unit))
    db.flush()



# ---------------------------------------------------------------------------
# Part 3 — increment-4 drift-only reconcile (tests 12-20)
# ---------------------------------------------------------------------------

def _fixed_plan(db) -> ProductionPlanHeader:
    # Period spans the current test date (2026-07-20) so the snapshot stays in
    # reconcile_all_active's active window (period_to >= today).
    plan = ProductionPlanHeader(
        name="План",
        period_from=date(2026, 7, 1),
        period_to=date(2026, 8, 31),
        status="fixed",
        created_by="test",
    )
    db.add(plan)
    db.flush()
    return plan


def _plan_line(db, plan, item, qty, bucket_date=date(2026, 8, 15)) -> None:
    db.add(ProductionPlanLine(plan_id=plan.id, item_id=item.item_id, bucket_date=bucket_date, qty=qty))
    db.flush()


def _make_purchased_component(db, code: str, stock: float = 0.0) -> Item:
    item = Item(
        item_code=code,
        item_name=f"Компонент {code}",
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


def _run_dump(db, run_id):
    prods = sorted(
        (int(p.item_id), float(p.quantity), float(p.remaining_qty))
        for p in db.query(ProductionProduct)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .filter(ProductionOrder.source_run_id == run_id)
        .all()
    )
    purch = sorted(
        (int(pp.item_id), float(pp.qty))
        for pp in db.query(PlannedPurchase).filter_by(run_id=run_id).all()
    )
    return (prods, purch)


def test_reconcile_twice_is_a_noop_KEY(db_session):
    """The anti-reinflation gate: after freeze, a first reconcile materialises a
    gap; a second and third over unchanged facts add/prune/trim NOTHING and the
    DB dump is byte-stable."""
    item = _make_production_item(db_session, "RC-TWICE", stock=0.0)
    plan = _fixed_plan(db_session)
    _plan_line(db_session, plan, item, 40)
    db_session.commit()

    run_id = _publish_plan_snapshot(db_session, plan.id)["run_id"]

    res1 = reconcile_snapshot(db_session, run_id)
    assert len(res1["production_added"]) == 1
    assert abs(res1["production_added"][0]["qty"] - 40.0) < 1e-6

    res2 = reconcile_snapshot(db_session, run_id)
    dump2 = _run_dump(db_session, run_id)
    res3 = reconcile_snapshot(db_session, run_id)
    dump3 = _run_dump(db_session, run_id)

    for res in (res2, res3):
        assert res["production_added"] == []
        assert res["purchase_added"] == []
        assert res["purchase_pruned"] == []
        assert res["production_trimmed"] == []
    assert dump2 == dump3


def test_legacy_stock_drift_requires_explicit_diagnostic_mode(
    db_session, monkeypatch
):
    """Production reconciliation ignores mutable Item stock; the retained
    legacy drift calculator is reachable only through explicit diagnostics."""
    monkeypatch.setenv("MRP_DRIFT_MATURITY_HOURS", "0")
    parent = _make_production_item(db_session, "SF-PARENT", stock=0.0)
    comp = _make_purchased_component(db_session, "SF-COMP", stock=3.0)
    _link_bom(db_session, parent, comp, qty_per_unit=1.0)
    plan = _fixed_plan(db_session)
    _plan_line(db_session, plan, parent, 10)
    db_session.commit()

    run_id = _publish_plan_snapshot(db_session, plan.id)["run_id"]
    comp_req = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == comp.item_id)
        .one()
    )
    # Item.stock_qty is a legacy cache.  The published freeze must not subtract
    # it from demand in place of accepted Ledger stock.
    assert float(comp_req.net_required_qty) == 10.0
    pp = db_session.query(PlannedPurchase).filter_by(run_id=run_id, item_id=comp.item_id).one()
    assert float(pp.qty) == 10.0

    # Off-plan: component stock 3 → 1 (a −2 shortfall).
    comp.stock_qty = 1.0
    db_session.commit()

    production = _public_reconcile_snapshot(db_session, run_id, dry_run=True)
    db_session.refresh(comp_req)
    assert production["purchase_added"] == []
    assert production["purchase_pruned"]
    assert float(comp_req.drift_adjustment_qty) == 0.0
    assert db_session.get(PlannedPurchase, pp.purchase_id) is not None

    # Explicit diagnostic cycle 1 is pending.
    res1 = reconcile_snapshot(db_session, run_id)
    assert res1["purchase_added"] == []
    db_session.refresh(comp_req)
    assert float(comp_req.drift_adjustment_qty) == 0.0

    # The diagnostic-only legacy calculator sees Item.stock_qty=1 against its
    # zero Ledger baseline and records a -1 adjustment.  This is deliberately
    # not the production path.
    res2 = reconcile_snapshot(db_session, run_id)
    db_session.refresh(comp_req)
    assert float(comp_req.drift_adjustment_qty) == -1.0
    assert res2["purchase_added"] == []
    assert res2["purchase_pruned"]


def test_reconcile_trims_only_unexported_purchases_on_surplus(db_session, monkeypatch):
    """A matured off-plan surplus lowers effective_net; the local unexported
    purchase is trimmed while an exported one is left untouched."""
    monkeypatch.setenv("MRP_DRIFT_MATURITY_HOURS", "0")
    parent = _make_production_item(db_session, "SP-PARENT", stock=0.0)
    comp = _make_purchased_component(db_session, "SP-COMP", stock=3.0)
    _link_bom(db_session, parent, comp, qty_per_unit=1.0)
    plan = _fixed_plan(db_session)
    _plan_line(db_session, plan, parent, 10)
    db_session.commit()

    run_id = _publish_plan_snapshot(db_session, plan.id)["run_id"]
    unexp = db_session.query(PlannedPurchase).filter_by(run_id=run_id, item_id=comp.item_id).one()
    # The mutable Item stock cache is deliberately absent from the accepted
    # Ledger pool, so the frozen purchase obligation remains the full ten.
    assert float(unexp.qty) == 10.0

    # A second, EXPORTED purchase (success SyncLink) must survive any trim.
    exported = PlannedPurchase(
        run_id=run_id, item_id=comp.item_id, requested_qty=5, planned_qty=5, qty=5,
        need_date=date(2026, 6, 30), order_date=date(2026, 6, 1), lead_time_days=3,
        bucket_date=date(2026, 6, 30),
    )
    db_session.add(exported)
    db_session.flush()
    db_session.add(
        SyncLink(
            source_system="PRODPLAN", source_doctype="planned_purchase",
            source_id=exported.purchase_id, target_system="1C",
            target_entity="Document_ЗаказПоставщику",
            target_ref_key="EXP-REF-1", status="success",
        )
    )
    # Off-plan surplus: component stock 3 → 13 (+10).
    comp.stock_qty = 13.0
    db_session.commit()

    reconcile_snapshot(db_session, run_id)          # cycle 1 pending
    res2 = reconcile_snapshot(db_session, run_id)    # cycle 2 matured surplus

    assert res2["purchase_pruned"]
    remaining = {
        int(pp.purchase_id): float(pp.qty)
        for pp in db_session.query(PlannedPurchase).filter_by(run_id=run_id, item_id=comp.item_id).all()
    }
    assert exported.purchase_id in remaining and remaining[exported.purchase_id] == 5.0
    assert unexp.purchase_id not in remaining  # unexported trimmed away


def test_reconcile_foreign_wip_is_not_own_coverage(db_session):
    """An open production order NOT owned by the run (foreign 1C WIP, produced 0)
    is not coverage — the run still materialises its own catch-up."""
    item = _make_production_item(db_session, "OWN-COV", stock=0.0)
    plan = _fixed_plan(db_session)
    _plan_line(db_session, plan, item, 40)
    db_session.commit()

    run_id = _publish_plan_snapshot(db_session, plan.id)["run_id"]

    # Foreign open 1C order (not source=mrp/this-run, not linked) — produced 0.
    foreign = ProductionOrder(
        order_number="FOREIGN-WIP", order_date=date(2026, 6, 1), is_posted=True,
        deletion_mark=False, source="1c", order_ref1c="foreign-ref",
    )
    db_session.add(foreign)
    db_session.flush()
    fp = ProductionProduct(
        order_id=foreign.order_id, item_id=item.item_id, line_number=1,
        quantity=40, produced_qty=0, remaining_qty=40,
    )
    db_session.add(fp)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=fp.product_id, status="in_progress"))
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)
    added = [e for e in res["production_added"] if e["item_id"] == item.item_id]
    assert len(added) == 1
    assert abs(added[0]["qty"] - 40.0) < 1e-6


def test_reconcile_needs_freeze_runs_repairs_only(db_session):
    """A run with no active_freeze_version returns status=needs_freeze, sizes
    nothing, but still runs the structural repairs (no freeze_guard key)."""
    item = _make_purchased_item(db_session, "NF-ITEM", stock=0.0)
    plan = _fixed_plan(db_session)
    db_session.flush()
    run = PlanningRun(
        status="FIXED_SNAPSHOT", config_snapshot={}, pinned=True,
        source_plan_id=plan.id, period_from=plan.period_from, period_to=plan.period_to,
        active_freeze_version=None,
    )
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id, item_id=item.item_id, total_required_qty=50,
        net_required_qty=50, covered_qty=0, remaining_qty=50,
        period_from=plan.period_from, period_to=plan.period_to, bom_level=0,
    )
    db_session.add(req)
    # A stale local purchase that MUST NOT be trimmed under needs_freeze.
    db_session.add(
        PlannedPurchase(
            run_id=run.run_id, item_id=item.item_id, requested_qty=99, planned_qty=99,
            qty=99, need_date=plan.period_to, order_date=plan.period_from, lead_time_days=3,
            bucket_date=plan.period_to,
        )
    )
    db_session.commit()

    res = reconcile_snapshot(db_session, run.run_id)
    assert res["status"] == "needs_freeze"
    assert "freeze_guard" not in res
    assert res["production_added"] == []
    assert res["purchase_added"] == []
    assert res["purchase_pruned"] == []
    assert "rescheduled" in res and "mrp_order_repair" in res
    assert db_session.query(PlannedPurchase).filter_by(run_id=run.run_id).count() == 1


def test_reconcile_does_not_touch_welded_pair_item(db_session):
    """A welded predecessor of an active paint↔weld pair is never materialised
    nor trimmed here (it is ordered through the paint chain)."""
    painted = _make_production_item(db_session, "WLD-PAINT", stock=0.0)
    welded = _make_production_item(db_session, "WLD-WELD", stock=0.0)
    plan = _fixed_plan(db_session)
    db_session.flush()
    run = PlanningRun(
        status="FIXED_SNAPSHOT", config_snapshot={}, pinned=True,
        source_plan_id=plan.id, period_from=plan.period_from, period_to=plan.period_to,
        active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        PaintWeldPair(painted_item_id=painted.item_id, welded_item_id=welded.item_id, source="manual", is_active=True)
    )
    req = MrpRequirement(
        run_id=run.run_id, item_id=welded.item_id, total_required_qty=40,
        net_required_qty=40, covered_qty=0, remaining_qty=40,
        period_from=plan.period_from, period_to=plan.period_to, bom_level=0,
    )
    db_session.add(req)
    db_session.commit()

    # The welded item has a full 40 gap and NO coverage, yet reconcile must not
    # materialise a catch-up for it (nor trim), because it is welded-blocked.
    res = reconcile_snapshot(db_session, run.run_id)
    assert [e for e in res["production_added"] if e["item_id"] == welded.item_id] == []
    assert res["production_trimmed"] == []
    # No production order was created for the welded item.
    assert (
        db_session.query(ProductionProduct)
        .filter(ProductionProduct.item_id == welded.item_id)
        .count()
        == 0
    )


def test_reconcile_all_active_runs_ledger_then_sizes_and_dry_is_stable(db_session):
    """reconcile_all_active runs the ledger cycle BEFORE the sizing loop; a
    dry-run leaves the DB unchanged and is repeatable."""
    item = _make_production_item(db_session, "ALL-1", stock=0.0)
    plan = _fixed_plan(db_session)
    _plan_line(db_session, plan, item, 40)
    db_session.commit()

    _publish_plan_snapshot(db_session, plan.id)

    before = _all_dump(db_session)
    dry1 = reconcile_all_active(db_session, dry_run=True)
    db_session.expire_all()
    assert _all_dump(db_session) == before  # dry-run wrote nothing
    assert "execution_ledger" in dry1 and "cycle_id" in dry1["execution_ledger"]

    # A real run materialises the catch-up; a second real run is a near no-op.
    live = reconcile_all_active(db_session, dry_run=False)
    assert live["production_lines_added"] >= 1
    live2 = reconcile_all_active(db_session, dry_run=False)
    assert live2["production_lines_added"] == 0
    assert live2["purchase_lines_added"] == 0


def _all_dump(db):
    prods = sorted(
        (int(p.item_id), float(p.quantity)) for p in db.query(ProductionProduct).all()
    )
    purch = sorted((int(pp.item_id), float(pp.qty)) for pp in db.query(PlannedPurchase).all())
    return (prods, purch)


def test_reconcile_does_not_create_requirements(db_session):
    """Reconcile never creates a requirement — only the freeze does."""
    item = _make_production_item(db_session, "NOREQ-1", stock=0.0)
    comp = _make_production_item(db_session, "NOREQ-COMP", stock=0.0)
    _link_bom(db_session, item, comp, qty_per_unit=2.0)
    plan = _fixed_plan(db_session)
    _plan_line(db_session, plan, item, 40)
    db_session.commit()

    run_id = _publish_plan_snapshot(db_session, plan.id)["run_id"]
    before = db_session.query(MrpRequirement).filter_by(run_id=run_id).count()

    reconcile_snapshot(db_session, run_id)
    after = db_session.query(MrpRequirement).filter_by(run_id=run_id).count()
    assert after == before


def test_reconcile_repairs_still_run(db_session):
    """The repair pipeline (batch/binding/reschedule/dedupe/orphan) is still
    invoked and reported by reconcile."""
    item = _make_production_item(db_session, "REP-1", stock=0.0)
    plan = _fixed_plan(db_session)
    _plan_line(db_session, plan, item, 40)
    db_session.commit()

    run_id = _publish_plan_snapshot(db_session, plan.id)["run_id"]
    res = reconcile_snapshot(db_session, run_id)
    for key in ("rescheduled", "mrp_order_repair", "mrp_batch_repair", "binding_repair", "orphan_link_repair"):
        assert key in res


# ---------------------------------------------------------------------------
# Part 4 — increment-5: manual force-close / reopen + activity-by-status
# ---------------------------------------------------------------------------

def _force_close_fixture(db):
    """A frozen run whose purchased component carries an unexported PlannedPurchase
    plus a second EXPORTED one. Returns (run_id, comp, comp_req, unexp, exported)."""
    parent = _make_production_item(db, "FC-PARENT", stock=0.0)
    comp = _make_purchased_component(db, "FC-COMP", stock=0.0)
    _link_bom(db, parent, comp, qty_per_unit=1.0)
    plan = _fixed_plan(db)
    _plan_line(db, plan, parent, 10)
    db.commit()

    run_id = _publish_plan_snapshot(db, plan.id)["run_id"]
    comp_req = (
        db.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == comp.item_id)
        .one()
    )
    unexp = db.query(PlannedPurchase).filter_by(run_id=run_id, item_id=comp.item_id).one()
    assert float(unexp.qty) > 0.0

    exported = PlannedPurchase(
        run_id=run_id, item_id=comp.item_id, requested_qty=5, planned_qty=5, qty=5,
        need_date=date(2026, 8, 30), order_date=date(2026, 8, 1), lead_time_days=3,
        bucket_date=date(2026, 8, 30),
    )
    db.add(exported)
    db.flush()
    db.add(
        SyncLink(
            source_system="PRODPLAN", source_doctype="planned_purchase",
            source_id=exported.purchase_id, target_system="1C",
            target_entity="Document_ЗаказПоставщику",
            target_ref_key="FC-EXP-REF", status="success",
        )
    )
    db.commit()
    return run_id, comp, comp_req, unexp, exported


def test_i5_force_close_flips_trims_unexported_keeps_exported_and_drops_active(db_session):
    """I6: force-close flips FIXED_SNAPSHOT→CLOSED, closes open reqs, trims the
    run's UNexported purchases to 0 while leaving exported ones intact, and drops
    the run from the active set."""
    run_id, comp, comp_req, unexp, exported = _force_close_fixture(db_session)
    unexp_id = unexp.purchase_id
    exported_id = exported.purchase_id

    assert run_id in _latest_active_snapshot_run_ids(db_session)

    res = force_close_run(db_session, run_id)
    assert res["status"] == "closed"
    assert res["requirements_closed"] >= 1
    assert res["purchases_pruned"]

    db_session.expire_all()
    run = db_session.get(PlanningRun, run_id)
    assert run.status == "CLOSED"
    assert run.finished_at is not None
    reqs = db_session.query(MrpRequirement).filter_by(run_id=run_id).all()
    assert reqs and all(r.status == "closed" for r in reqs)
    assert all(r.closed_at is not None for r in reqs)

    remaining = {
        int(pp.purchase_id): float(pp.qty)
        for pp in db_session.query(PlannedPurchase).filter_by(run_id=run_id).all()
    }
    assert remaining.get(exported_id) == 5.0   # exported survives
    assert unexp_id not in remaining           # unexported abandoned
    assert run_id not in _latest_active_snapshot_run_ids(db_session)


def test_i5_force_close_is_idempotent(db_session):
    """I7: a second force-close returns already_closed and changes nothing."""
    run_id, comp, comp_req, unexp, exported = _force_close_fixture(db_session)
    force_close_run(db_session, run_id)
    db_session.expire_all()
    before = _all_dump(db_session)

    res = force_close_run(db_session, run_id)
    assert res["status"] == "already_closed"
    assert res["requirements_closed"] == 0
    assert res["purchases_pruned"] == []
    db_session.expire_all()
    assert _all_dump(db_session) == before


def test_i5_force_close_dry_run_writes_nothing(db_session):
    """I12: a dry-run force-close reports the effect but persists nothing."""
    run_id, comp, comp_req, unexp, exported = _force_close_fixture(db_session)
    before = _all_dump(db_session)

    res = force_close_run(db_session, run_id, dry_run=True)
    assert res["status"] == "closed"
    assert res["dry_run"] is True

    db_session.expire_all()
    run = db_session.get(PlanningRun, run_id)
    assert run.status == "FIXED_SNAPSHOT"
    assert db_session.get(MrpRequirement, comp_req.id).status == "open"
    assert _all_dump(db_session) == before
    assert run_id in _latest_active_snapshot_run_ids(db_session)


def test_i5_reopen_restores_run_and_requirements(db_session):
    """I8: reopen flips CLOSED→FIXED_SNAPSHOT, reopens the closed reqs and puts
    the run back into the active set and the ledger scope."""
    run_id, comp, comp_req, unexp, exported = _force_close_fixture(db_session)
    force_close_run(db_session, run_id)
    db_session.expire_all()
    assert db_session.get(PlanningRun, run_id).status == "CLOSED"

    res = reopen_run(db_session, run_id)
    assert res["status"] == "reopened"
    assert res["requirements_reopened"] >= 1

    db_session.expire_all()
    run = db_session.get(PlanningRun, run_id)
    assert run.status == "FIXED_SNAPSHOT"
    assert run.finished_at is None
    reqs = db_session.query(MrpRequirement).filter_by(run_id=run_id).all()
    assert reqs and all(r.status == "open" for r in reqs)
    assert all(r.closed_at is None for r in reqs)
    assert run_id in _latest_active_snapshot_run_ids(db_session)
    assert run_id in _scope_run_ids(db_session)


def test_i5_reopen_dry_run_writes_nothing(db_session):
    """I12: a dry-run reopen reports the effect but persists nothing."""
    run_id, comp, comp_req, unexp, exported = _force_close_fixture(db_session)
    force_close_run(db_session, run_id)
    db_session.expire_all()
    before_status = db_session.get(PlanningRun, run_id).status
    assert before_status == "CLOSED"

    res = reopen_run(db_session, run_id, dry_run=True)
    assert res["status"] == "reopened"
    assert res["dry_run"] is True

    db_session.expire_all()
    assert db_session.get(PlanningRun, run_id).status == "CLOSED"
    assert db_session.get(MrpRequirement, comp_req.id).status == "closed"


def test_i5_force_close_rejects_missing_and_non_fixed(db_session):
    """force-close raises on an unknown run (→404 at the router) and on a run
    that is not FIXED_SNAPSHOT (→400)."""
    with pytest.raises(ValueError, match="не найден"):
        force_close_run(db_session, 999999)

    plan = _fixed_plan(db_session)
    run = PlanningRun(
        status="PENDING", config_snapshot={}, pinned=True,
        source_plan_id=plan.id, period_from=plan.period_from, period_to=plan.period_to,
    )
    db_session.add(run)
    db_session.commit()
    with pytest.raises(ValueError, match="FIXED_SNAPSHOT"):
        force_close_run(db_session, run.run_id)


def test_i5_reopen_rejects_non_closed(db_session):
    """reopen raises on a run that is not CLOSED."""
    run_id, comp, comp_req, unexp, exported = _force_close_fixture(db_session)
    with pytest.raises(ValueError, match="CLOSED"):
        reopen_run(db_session, run_id)


def test_i5_activity_is_status_based_overdue_snapshot_stays_active(db_session):
    """I9: with the period filter removed, an overdue FIXED_SNAPSHOT (period_to
    in the past) is still returned by _latest_active_snapshot_run_ids — activity
    is status-based, aligned with the ledger scope."""
    item = _make_purchased_item(db_session, "OVERDUE-ACT", stock=0.0)
    plan = ProductionPlanHeader(
        name="Просроченный план",
        period_from=date(2020, 1, 1),
        period_to=date(2020, 2, 1),
        status="fixed",
        created_by="test",
    )
    db_session.add(plan)
    db_session.flush()
    run = PlanningRun(
        status="FIXED_SNAPSHOT", config_snapshot={}, pinned=True,
        source_plan_id=plan.id, period_from=date(2020, 1, 1), period_to=date(2020, 2, 1),
        active_freeze_version=1,
        ledger_generation_id=db_session.info["accepted_ledger_generation_id"],
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        MrpRequirement(
            run_id=run.run_id, item_id=item.item_id, total_required_qty=40,
            net_required_qty=40, covered_qty=0, remaining_qty=40,
            period_from=date(2020, 1, 1), period_to=date(2020, 2, 1), bom_level=0,
        )
    )
    db_session.commit()

    active = _latest_active_snapshot_run_ids(db_session)
    assert run.run_id in active  # overdue but still active (status-based)

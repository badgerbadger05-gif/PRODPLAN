"""Tests for MRP reconciliation and the covered_qty rollback that feeds it."""

from datetime import date, datetime

import pytest

from app.models import (
    DefaultSpecification,
    Item,
    MrpRequirement,
    PlannedPurchase,
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
    Supplier,
    SupplierOrder,
    SupplierOrderItem,
    SyncLink,
)
from app.services.mrp_reconciliation import (
    SharedPools,
    _active_production_qty_by_item,
    _load_purchase_supplier_remaining,
    reconcile_all_active,
    reconcile_snapshot,
)
from app.services.mrp_stock_helpers import effective_stock_by_item_all
from app.services.one_c_purchase_order_export import PURCHASE_ORDER_ENTITY
from app.services.period_plan_service import create_mrp_snapshot_from_period_plan
from app.services.production_binding_repair import repair_clean_mrp_bindings
from app.services.production_control_journal import (
    create_production_orders_from_mrp_requirements,
    update_line_state,
    update_product_quantity,
)


DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"


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


def test_reconcile_prunes_stale_purchase_when_current_net_is_zero(db_session):
    item = _make_purchased_item(db_session, "BUY-STALE", stock=0.0)
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
            plan_id=plan.id,
            item_id=item.item_id,
            bucket_date=date(2026, 6, 15),
            qty=50,
        )
    )
    db_session.commit()

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
    run_id = snap["run_id"]
    purchase = db_session.query(PlannedPurchase).filter_by(run_id=run_id, item_id=item.item_id).one()
    assert float(purchase.qty) == 50.0

    item.stock_qty = 50.0
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)

    assert len(res["purchase_pruned"]) == 1
    assert res["purchase_pruned"][0]["item_id"] == item.item_id
    assert res["purchase_pruned"][0]["removed_qty"] == 50.0
    assert db_session.query(PlannedPurchase).filter_by(run_id=run_id, item_id=item.item_id).count() == 0
    req = db_session.query(MrpRequirement).filter_by(run_id=run_id, item_id=item.item_id).one()
    assert float(req.net_required_qty) == 0.0
    assert float(req.covered_qty) == 0.0
    assert float(req.remaining_qty) == 0.0


# ---------------------------------------------------------------------------
# Part 1 — covered_qty rollback
# ---------------------------------------------------------------------------

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

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
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


def test_reconcile_creates_catchup_when_done_1c_output_is_not_in_stock(db_session):
    item = _make_production_item(db_session, "P-DONE-1C", stock=0.0)
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
            plan_id=plan.id,
            item_id=item.item_id,
            bucket_date=date(2026, 6, 15),
            qty=40,
        )
    )
    db_session.commit()

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
    run_id = snap["run_id"]
    req = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == item.item_id)
        .one()
    )
    assert float(req.net_required_qty) == 40.0

    order = ProductionOrder(
        order_number="1C-DONE-TECH",
        order_date=date(2026, 6, 15),
        is_posted=True,
        deletion_mark=False,
        source="1c",
        order_ref1c="11111111-1111-1111-1111-111111111111",
        order_state_key=DONE_STATE_KEY,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=40,
        produced_qty=40,
        remaining_qty=0,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="completed",
            issue_status="completed",
        )
    )
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)

    assert len(res["production_added"]) == 1
    assert res["production_added"][0]["qty"] == 40.0
    db_session.refresh(req)
    assert float(req.covered_qty) == 40.0
    assert float(req.remaining_qty) == 0.0


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

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
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


def test_reconcile_grows_existing_catchup_order_when_stock_drops(db_session):
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

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
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
    assert abs(added[0]["qty"] - 32.0) < 1e-6
    db_session.refresh(product)
    db_session.refresh(req)
    assert float(product.quantity) == 34.0
    assert float(product.remaining_qty) == 34.0
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

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
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


def test_reconcile_repairs_oversized_catchup_order_by_optimal_batch(db_session):
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

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
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

    assert res["production_added"] == []
    assert res["mrp_batch_repair"]["created_orders"] == 4
    products = (
        db_session.query(ProductionProduct)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .filter(ProductionOrder.source_run_id == run_id, ProductionProduct.item_id == item.item_id)
        .order_by(ProductionProduct.product_id.asc())
        .all()
    )
    assert [float(product.quantity) for product in products] == [15.0, 15.0, 15.0, 15.0, 7.0]
    assert [float(product.remaining_qty) for product in products] == [15.0, 15.0, 15.0, 15.0, 7.0]


def test_reconcile_propagates_parent_stock_drop_to_component(db_session):
    painted = _make_production_item(db_session, "P-PAINT-DROP", stock=32.0)
    welded = _make_production_item(db_session, "P-WELD-DROP", stock=7.0)
    _link_bom(db_session, painted, welded, qty_per_unit=1.0)
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
            plan_id=plan.id, item_id=painted.item_id, bucket_date=date(2026, 6, 15), qty=34
        )
    )
    db_session.commit()

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
    run_id = snap["run_id"]
    reqs = {
        int(r.item_id): r
        for r in db_session.query(MrpRequirement).filter(MrpRequirement.run_id == run_id).all()
    }
    assert float(reqs[painted.item_id].net_required_qty) == 34.0
    assert float(reqs[welded.item_id].total_required_qty) == 34.0
    assert float(reqs[welded.item_id].net_required_qty) == 27.0

    create_production_orders_from_mrp_requirements(db_session, [reqs[painted.item_id].id])
    painted.stock_qty = 17.0
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)

    added = {entry["item_id"]: entry["qty"] for entry in res["production_added"]}
    assert painted.item_id not in added
    assert abs(added[welded.item_id] - 27.0) < 1e-6
    db_session.refresh(reqs[painted.item_id])
    db_session.refresh(reqs[welded.item_id])
    assert float(reqs[painted.item_id].net_required_qty) == 34.0
    assert float(reqs[welded.item_id].net_required_qty) == 27.0
    assert float(reqs[welded.item_id].covered_qty) == 27.0
    assert float(reqs[welded.item_id].remaining_qty) == 0.0


def test_reconcile_uses_bucket_net_baseline_after_parent_requirement_was_updated(db_session):
    painted = _make_production_item(db_session, "P-PAINT-DROP-2", stock=32.0)
    welded = _make_production_item(db_session, "P-WELD-DROP-2", stock=7.0)
    _link_bom(db_session, painted, welded, qty_per_unit=1.0)
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
            plan_id=plan.id, item_id=painted.item_id, bucket_date=date(2026, 6, 15), qty=34
        )
    )
    db_session.commit()

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
    run_id = snap["run_id"]
    reqs = {
        int(r.item_id): r
        for r in db_session.query(MrpRequirement).filter(MrpRequirement.run_id == run_id).all()
    }
    create_production_orders_from_mrp_requirements(db_session, [reqs[painted.item_id].id])
    product = (
        db_session.query(ProductionProduct)
        .filter(ProductionProduct.source_mrp_requirement_id == reqs[painted.item_id].id)
        .one()
    )
    painted.stock_qty = 17.0
    product.quantity = 17.0
    product.remaining_qty = 17.0
    reqs[painted.item_id].net_required_qty = 17.0
    reqs[painted.item_id].covered_qty = 17.0
    reqs[painted.item_id].remaining_qty = 0.0
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)

    added = {entry["item_id"]: entry["qty"] for entry in res["production_added"]}
    assert abs(added[painted.item_id] - 17.0) < 1e-6
    assert abs(added[welded.item_id] - 27.0) < 1e-6
    db_session.refresh(reqs[welded.item_id])
    assert float(reqs[welded.item_id].net_required_qty) == 27.0
    assert float(reqs[welded.item_id].covered_qty) == 27.0
    assert float(reqs[welded.item_id].remaining_qty) == 0.0


def test_reconcile_adds_component_requirement_missing_from_snapshot(db_session):
    painted = _make_production_item(db_session, "P-MISSING-PAINT", stock=10.0)
    welded = _make_production_item(db_session, "P-MISSING-WELD", stock=0.0)
    welded.optimal_batch = 30
    _link_bom(db_session, painted, welded, qty_per_unit=1.0)
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
            plan_id=plan.id, item_id=painted.item_id, bucket_date=date(2026, 6, 15), qty=10
        )
    )
    db_session.commit()

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
    run_id = snap["run_id"]
    reqs = {
        int(r.item_id): r
        for r in db_session.query(MrpRequirement).filter(MrpRequirement.run_id == run_id).all()
    }
    assert painted.item_id in reqs
    assert welded.item_id in reqs

    order = ProductionOrder(
        order_number=f"MRP-RC-{run_id}-{welded.item_id}",
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
        item_id=welded.item_id,
        line_number=1,
        quantity=40,
        produced_qty=0,
        remaining_qty=40,
        source_mrp_requirement_id=None,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, status="shortage"))
    painted.stock_qty = 0.0
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)

    added = {entry["item_id"]: entry["qty"] for entry in res["production_added"]}
    assert added[painted.item_id] == 10.0
    assert welded.item_id not in added
    assert res["orphan_link_repair"]["linked"] == 1
    req = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == welded.item_id)
        .one()
    )
    db_session.refresh(product)
    assert product.source_mrp_requirement_id == req.id
    assert float(req.total_required_qty) == 10.0
    assert float(req.net_required_qty) == 10.0
    assert float(product.quantity) == 10.0
    assert float(product.remaining_qty) == 10.0


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


def test_reconcile_explodes_through_wip_covered_parent(db_session):
    """Regression: an open production order on a parent must NOT stop the BOM
    explosion to its components.

    BOM: painted → welded → blank, all out of stock. Only the painted line is
    materialised as an open order (WIP). Earlier the parent's WIP zeroed its
    net demand, the explosion stopped, and the welded/blank deficit produced no
    catch-up orders. Reconciliation must now create welded AND blank in a single
    pass, and a second pass must be idempotent.
    """
    painted = _make_production_item(db_session, "P-PAINT", stock=0.0)
    welded = _make_production_item(db_session, "P-WELD", stock=0.0)
    blank = _make_production_item(db_session, "P-BLANK", stock=0.0)
    _link_bom(db_session, painted, welded, qty_per_unit=1.0)
    _link_bom(db_session, welded, blank, qty_per_unit=1.0)

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
            plan_id=plan.id, item_id=painted.item_id, bucket_date=date(2026, 6, 15), qty=32
        )
    )
    db_session.commit()

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
    run_id = snap["run_id"]

    # With painted out of stock, the snapshot explodes the whole tree.
    reqs = {
        int(r.item_id): r
        for r in db_session.query(MrpRequirement).filter(MrpRequirement.run_id == run_id).all()
    }
    assert set(reqs) == {painted.item_id, welded.item_id, blank.item_id}
    assert float(reqs[blank.item_id].net_required_qty) == 32.0

    # Materialise ONLY the painted line → it becomes the parent's open WIP.
    create_production_orders_from_mrp_requirements(db_session, [reqs[painted.item_id].id])
    db_session.commit()

    # Reconcile: painted is fully covered by its open order, but welded and blank
    # are still in deficit and must each get a catch-up order in this single pass.
    res = reconcile_snapshot(db_session, run_id)
    added = {entry["item_id"]: entry["qty"] for entry in res["production_added"]}
    assert welded.item_id in added and abs(added[welded.item_id] - 32.0) < 1e-6
    assert blank.item_id in added and abs(added[blank.item_id] - 32.0) < 1e-6
    assert painted.item_id not in added  # parent already covered by its open order

    # Second pass: the new welded/blank orders are open WIP → no further gap.
    res = reconcile_snapshot(db_session, run_id)
    assert res["production_added"] == []


def test_reconcile_does_not_reinflate_components_covered_by_parent_wip_at_snapshot(db_session):
    """Regression: a parent covered by an open order ALREADY AT SNAPSHOT TIME
    must not inflate its components on reconcile.

    At snapshot the parent's net is WIP-netted to 0, but its gross still
    explodes to the child (the open order will consume components). The old
    drift logic compared the parent's after-stock current net (32) against the
    WIP-netted bucket net (0) and pushed the "drift" (+32) into the child's
    gross on every cycle — the child demand doubled and a spurious catch-up
    order was created.
    """
    painted = _make_production_item(db_session, "P-PAINT-PREWIP", stock=0.0)
    welded = _make_production_item(db_session, "P-WELD-PREWIP", stock=0.0)
    _link_bom(db_session, painted, welded, qty_per_unit=1.0)

    # Open production order for the parent BEFORE the snapshot (mirrors an
    # order already opened in 1C).
    order = ProductionOrder(
        order_number="ЗСНФ-PREWIP",
        order_date=date(2026, 6, 1),
        order_ref1c="ref-prewip",
        is_posted=True,
        deletion_mark=False,
        source="1c",
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=painted.item_id,
        line_number=1,
        quantity=32,
        produced_qty=0,
        remaining_qty=32,
        source_mrp_requirement_id=None,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, status="in_progress"))

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
            plan_id=plan.id, item_id=painted.item_id, bucket_date=date(2026, 6, 15), qty=32
        )
    )
    db_session.commit()

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
    run_id = snap["run_id"]
    reqs = {
        int(r.item_id): r
        for r in db_session.query(MrpRequirement).filter(MrpRequirement.run_id == run_id).all()
    }
    # An open order does not replace the approved top-level release plan.
    assert float(reqs[painted.item_id].net_required_qty) == 32.0
    assert float(reqs[welded.item_id].total_required_qty) == 32.0
    assert float(reqs[welded.item_id].net_required_qty) == 32.0

    # Materialise the child line → it becomes open WIP covering the child.
    create_production_orders_from_mrp_requirements(db_session, [reqs[welded.item_id].id])
    db_session.commit()

    # Nothing drifted since the snapshot → reconcile must add nothing, twice.
    for _ in range(2):
        res = reconcile_snapshot(db_session, run_id)
        assert res["production_added"] == []

    db_session.refresh(reqs[welded.item_id])
    assert float(reqs[welded.item_id].net_required_qty) == 32.0
    assert float(reqs[welded.item_id].covered_qty) == 32.0
    assert float(reqs[welded.item_id].remaining_qty) == 0.0
    # The child's materialised order must survive the dedupe untouched.
    child_product = (
        db_session.query(ProductionProduct)
        .filter(ProductionProduct.source_mrp_requirement_id == reqs[welded.item_id].id)
        .one()
    )
    assert float(child_product.remaining_qty) == 32.0


# ---------------------------------------------------------------------------
# Part 3 — Step A: consume the shared pools ONCE across the active-run queue
# ---------------------------------------------------------------------------

def _make_purchase_plan_snapshot(
    db, item, *, name, period_from, period_to, qty, bucket_date
) -> int:
    """Fixed period plan with a single purchase-item line, snapshotted."""
    plan = ProductionPlanHeader(
        name=name,
        period_from=period_from,
        period_to=period_to,
        status="fixed",
        created_by="test",
    )
    db.add(plan)
    db.flush()
    db.add(
        ProductionPlanLine(
            plan_id=plan.id, item_id=item.item_id, bucket_date=bucket_date, qty=qty
        )
    )
    db.commit()
    snap = create_mrp_snapshot_from_period_plan(db, plan.id)
    return int(snap["run_id"])


def _planned_purchase_sum(db, run_id: int) -> float:
    rows = db.query(PlannedPurchase.qty).filter(PlannedPurchase.run_id == int(run_id)).all()
    return sum(float(q or 0.0) for (q,) in rows)


def _purchase_signature(db):
    rows = db.query(
        PlannedPurchase.run_id, PlannedPurchase.item_id, PlannedPurchase.qty
    ).all()
    return sorted((int(r), int(i), round(float(q or 0.0), 6)) for r, i, q in rows)


def test_two_active_runs_share_stock_once(db_session):
    # One physical pile of 335.144 must be credited across the two active plans
    # exactly once, earliest-need-first.
    item = _make_purchased_item(db_session, "SHARE-STOCK", stock=335.144)
    aug = _make_purchase_plan_snapshot(
        db_session, item, name="Август",
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        qty=300, bucket_date=date(2026, 8, 15),
    )
    sep = _make_purchase_plan_snapshot(
        db_session, item, name="Сентябрь",
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
        qty=226.218, bucket_date=date(2026, 9, 15),
    )

    reconcile_all_active(db_session)

    aug_sum = _planned_purchase_sum(db_session, aug)
    sep_sum = _planned_purchase_sum(db_session, sep)
    # Aug (earliest need) eats 300 of 335.144 → net 0. Sep gets the 35.144 left
    # → 226.218 − 35.144 = 191.074.
    assert aug_sum == pytest.approx(0.0, abs=1e-6)
    assert sep_sum == pytest.approx(191.074, abs=1e-4)
    assert (aug_sum + sep_sum) == pytest.approx(191.074, abs=1e-4)


def test_single_run_no_regression(db_session):
    # Locks the shared_pools=None path: single-run reconcile, stock >= demand.
    item = _make_purchased_item(db_session, "SINGLE-NOREG", stock=100.0)
    run_id = _make_purchase_plan_snapshot(
        db_session, item, name="Единый",
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        qty=50, bucket_date=date(2026, 8, 15),
    )

    res = reconcile_snapshot(db_session, run_id)

    assert res["purchase_added"] == []
    assert _planned_purchase_sum(db_session, run_id) == pytest.approx(0.0, abs=1e-9)


def test_overlapping_stock_zero_no_double_count(db_session):
    # Zero stock: neither run may borrow the other's coverage — each carries its
    # own full gross and the aggregate is the plain sum.
    item = _make_purchased_item(db_session, "OVERLAP-ZERO", stock=0.0)
    aug = _make_purchase_plan_snapshot(
        db_session, item, name="Август",
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        qty=300, bucket_date=date(2026, 8, 15),
    )
    sep = _make_purchase_plan_snapshot(
        db_session, item, name="Сентябрь",
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
        qty=226.218, bucket_date=date(2026, 9, 15),
    )

    reconcile_all_active(db_session)

    aug_sum = _planned_purchase_sum(db_session, aug)
    sep_sum = _planned_purchase_sum(db_session, sep)
    assert aug_sum == pytest.approx(300.0)
    assert sep_sum == pytest.approx(226.218)
    assert (aug_sum + sep_sum) == pytest.approx(526.218)


def test_partial_supplier_coverage_shared(db_session):
    # In-transit supplier supply (counts in MRP, delivery <= Sep period_to) is
    # consumed once, by the run that still has a deficit after stock.
    item = _make_purchased_item(db_session, "SUP-SHARE", stock=335.144)
    supplier = Supplier(supplier_ref1c="sup-1", supplier_name="ООО Поставка")
    db_session.add(supplier)
    db_session.flush()
    order = SupplierOrder(
        order_number="ЗП-SUP",
        order_date=datetime(2026, 7, 1),
        supplier_id=supplier.supplier_id,
        order_state_name="В пути",
        deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(
        SupplierOrderItem(
            order_id=order.order_id,
            item_id_ref=item.item_id,
            quantity=100,
            received_qty=0,
            remaining_qty=100,
            delivery_date=datetime(2026, 9, 20),
        )
    )
    db_session.commit()

    aug = _make_purchase_plan_snapshot(
        db_session, item, name="Август",
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        qty=300, bucket_date=date(2026, 8, 15),
    )
    sep = _make_purchase_plan_snapshot(
        db_session, item, name="Сентябрь",
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
        qty=226.218, bucket_date=date(2026, 9, 15),
    )

    reconcile_all_active(db_session)

    assert _planned_purchase_sum(db_session, aug) == pytest.approx(0.0, abs=1e-6)
    # 226.218 − 35.144 (leftover stock) − 100 (supplier) = 91.074.
    assert _planned_purchase_sum(db_session, sep) == pytest.approx(91.074, abs=1e-4)


def test_idempotent_across_repeated_reconcile(db_session):
    item = _make_purchased_item(db_session, "IDEMPOTENT", stock=335.144)
    _make_purchase_plan_snapshot(
        db_session, item, name="Август",
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        qty=300, bucket_date=date(2026, 8, 15),
    )
    _make_purchase_plan_snapshot(
        db_session, item, name="Сентябрь",
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
        qty=226.218, bucket_date=date(2026, 9, 15),
    )

    reconcile_all_active(db_session)
    sig1 = _purchase_signature(db_session)

    reconcile_all_active(db_session)
    sig2 = _purchase_signature(db_session)

    assert sig1 == sig2
    assert len(sig1) == 1  # only Sep carries a deficit


def test_exported_purchase_not_double_ordered(db_session):
    item = _make_purchased_item(db_session, "EXPORTED", stock=335.144)
    _make_purchase_plan_snapshot(
        db_session, item, name="Август",
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        qty=300, bucket_date=date(2026, 8, 15),
    )
    sep = _make_purchase_plan_snapshot(
        db_session, item, name="Сентябрь",
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
        qty=226.218, bucket_date=date(2026, 9, 15),
    )

    reconcile_all_active(db_session)
    sep_purchase = (
        db_session.query(PlannedPurchase).filter(PlannedPurchase.run_id == sep).one()
    )
    db_session.add(
        SyncLink(
            source_system="PRODPLAN",
            source_doctype="planned_purchase",
            source_id=sep_purchase.purchase_id,
            target_entity=PURCHASE_ORDER_ENTITY,
            target_ref_key="po-ref-1",
            status="success",
        )
    )
    db_session.commit()

    reconcile_all_active(db_session)

    remaining = (
        db_session.query(PlannedPurchase).filter(PlannedPurchase.run_id == sep).all()
    )
    assert len(remaining) == 1
    assert int(remaining[0].purchase_id) == int(sep_purchase.purchase_id)
    assert float(remaining[0].qty) == pytest.approx(191.074, abs=1e-4)


# ---------------------------------------------------------------------------
# Part 4 — Step B: net PRODUCED sub-assembly stock ONCE across the queue,
# at the explosion level, without double-consuming within a run.
# ---------------------------------------------------------------------------

def _make_purchase_plan_snapshot_prod(
    db, item, *, name, period_from, period_to, qty, bucket_date
) -> int:
    """Fixed period plan with a single PRODUCTION top-level line, snapshotted."""
    plan = ProductionPlanHeader(
        name=name,
        period_from=period_from,
        period_to=period_to,
        status="fixed",
        created_by="test",
    )
    db.add(plan)
    db.flush()
    db.add(
        ProductionPlanLine(
            plan_id=plan.id, item_id=item.item_id, bucket_date=bucket_date, qty=qty
        )
    )
    db.commit()
    snap = create_mrp_snapshot_from_period_plan(db, plan.id)
    return int(snap["run_id"])


def test_produced_subassembly_stock_netted_once(db_session):
    # BOM: root R (produced) -> sub-assembly S (produced, HOLDS STOCK) -> leaf L
    # (purchased). qty_per_unit = 1 throughout. Two active runs (Aug, Sep) each
    # release 100 of R, so aggregate demand for S and L across the queue is 200.
    #
    # S carries 100 of stock — exactly one run's worth. Its stock must reduce the
    # explosion for EXACTLY ONE run (earliest-need-first = Aug), so the purchased
    # leaf L's aggregate deficit is 200 - 100 = 100, NOT 0 (which is what a
    # per-run re-credit of S's full stock at the explosion level would give).
    root = _make_production_item(db_session, "STEPB-ROOT", stock=0.0)
    sub = _make_production_item(db_session, "STEPB-SUB", stock=100.0)
    leaf = _make_purchased_item(db_session, "STEPB-LEAF", stock=0.0)
    _link_bom(db_session, root, sub, qty_per_unit=1.0)
    _link_bom(db_session, sub, leaf, qty_per_unit=1.0)

    aug = _make_purchase_plan_snapshot_prod(
        db_session, root, name="Август",
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        qty=100, bucket_date=date(2026, 8, 15),
    )
    sep = _make_purchase_plan_snapshot_prod(
        db_session, root, name="Сентябрь",
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
        qty=100, bucket_date=date(2026, 9, 15),
    )

    reconcile_all_active(db_session)

    aug_leaf = _planned_purchase_sum(db_session, aug)
    sep_leaf = _planned_purchase_sum(db_session, sep)
    # Aug (earliest need) consumes all 100 of S's stock at the explosion level →
    # S_net(Aug)=0 → L_gross(Aug)=0 → no purchase. Sep sees a depleted S ledger →
    # S_net(Sep)=100 → L_gross(Sep)=100 → purchase 100.
    assert aug_leaf == pytest.approx(0.0, abs=1e-6)
    assert sep_leaf == pytest.approx(100.0, abs=1e-6)
    # S's 100 of stock is subtracted from the leaf deficit ONCE, not once per run.
    assert (aug_leaf + sep_leaf) == pytest.approx(100.0, abs=1e-6)


def test_produced_node_single_run_no_regression(db_session):
    # A single active run with a produced sub-assembly holding stock must give a
    # byte-identical result on the shared path and the None (single-run) path.
    # Two mirrored, isolated scenarios: A reconciled via the None path, B via a
    # SharedPools of exactly one run. R->S->L, qty R->S = 2, S->L = 3.
    #   R net = 50 (root). S gross = 100, S stock 30 → S net = 70.
    #   L gross = 70*3 = 210 → L purchase = 210.
    def _build(tag):
        root = _make_production_item(db_session, f"NOREG-ROOT-{tag}", stock=0.0)
        sub = _make_production_item(db_session, f"NOREG-SUB-{tag}", stock=30.0)
        leaf = _make_purchased_item(db_session, f"NOREG-LEAF-{tag}", stock=0.0)
        _link_bom(db_session, root, sub, qty_per_unit=2.0)
        _link_bom(db_session, sub, leaf, qty_per_unit=3.0)
        run_id = _make_purchase_plan_snapshot_prod(
            db_session, root, name=f"План-{tag}",
            period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
            qty=50, bucket_date=date(2026, 8, 15),
        )
        return root, sub, leaf, run_id

    root_a, sub_a, leaf_a, run_a = _build("A")
    root_b, sub_b, leaf_b, run_b = _build("B")

    # Scenario A: the None (single-run) path.
    res_a = reconcile_snapshot(db_session, run_a)
    added_a = {int(e["item_id"]): e["qty"] for e in res_a["production_added"]}
    leaf_a_purchase = _planned_purchase_sum(db_session, run_a)

    # Scenario B: the shared path with a SharedPools scoped to this one run only.
    b_item_ids = [root_b.item_id, sub_b.item_id, leaf_b.item_id]
    pools = SharedPools(
        stock=effective_stock_by_item_all(db_session),
        supplier=_load_purchase_supplier_remaining(
            db_session, [leaf_b.item_id], date(2026, 8, 31)
        ),
        prodsupply=_active_production_qty_by_item(db_session, b_item_ids),
    )
    res_b = reconcile_snapshot(db_session, run_b, shared_pools=pools)
    added_b = {int(e["item_id"]): e["qty"] for e in res_b["production_added"]}
    leaf_b_purchase = _planned_purchase_sum(db_session, run_b)

    # Same numbers on both paths (only the item codes differ).
    assert added_a[root_a.item_id] == pytest.approx(50.0)
    assert added_a[sub_a.item_id] == pytest.approx(70.0)
    assert leaf_a_purchase == pytest.approx(210.0)

    assert added_b[root_b.item_id] == pytest.approx(added_a[root_a.item_id])
    assert added_b[sub_b.item_id] == pytest.approx(added_a[sub_a.item_id])
    assert leaf_b_purchase == pytest.approx(leaf_a_purchase)

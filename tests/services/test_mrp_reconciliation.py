"""Tests for MRP reconciliation and the covered_qty rollback that feeds it."""

from datetime import date

import pytest

from app.models import (
    DefaultSpecification,
    Item,
    MrpRequirement,
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
    SyncLink,
)
from app.services.mrp_reconciliation import reconcile_snapshot
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

    # Now reconciliation must create a catch-up order for the residual 30.
    res = reconcile_snapshot(db_session, run_id)
    added = res["production_added"]
    assert len(added) == 1
    assert added[0]["item_id"] == item.item_id
    assert abs(added[0]["qty"] - 30.0) < 1e-6

    # Running again is idempotent: the new order is open WIP, so no further gap.
    res = reconcile_snapshot(db_session, run_id)
    assert res["production_added"] == []


def test_reconcile_counts_completed_1c_zero_remaining_as_supply(db_session):
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

    assert res["production_added"] == []
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
    assert float(req.net_required_qty) == 2.0

    create_production_orders_from_mrp_requirements(db_session, [req.id])
    item.stock_qty = 17.0
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)

    added = res["production_added"]
    assert len(added) == 1
    assert added[0]["item_id"] == item.item_id
    assert abs(added[0]["qty"] - 15.0) < 1e-6
    db_session.refresh(req)
    assert float(req.net_required_qty) == 17.0
    assert float(req.covered_qty) == 17.0
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
    assert abs(added[0]["qty"] - 15.0) < 1e-6
    db_session.refresh(product)
    db_session.refresh(req)
    assert float(product.quantity) == 17.0
    assert float(product.remaining_qty) == 17.0
    assert float(req.net_required_qty) == 17.0
    assert float(req.covered_qty) == 17.0
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
    assert len(added) == 1
    assert abs(added[0]["qty"] - 32.0) < 1e-6
    assert [entry["qty"] for entry in added[0]["orders"]] == [15.0, 15.0, 2.0]
    products = (
        db_session.query(ProductionProduct)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .filter(ProductionOrder.source_run_id == run_id, ProductionProduct.item_id == item.item_id)
        .order_by(ProductionProduct.product_id.asc())
        .all()
    )
    assert [float(product.quantity) for product in products] == [2.0, 15.0, 15.0, 2.0]


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
    assert float(reqs[painted.item_id].net_required_qty) == 2.0
    assert float(reqs[welded.item_id].total_required_qty) == 2.0
    assert float(reqs[welded.item_id].net_required_qty) == 0.0

    create_production_orders_from_mrp_requirements(db_session, [reqs[painted.item_id].id])
    painted.stock_qty = 17.0
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)

    added = {entry["item_id"]: entry["qty"] for entry in res["production_added"]}
    assert abs(added[painted.item_id] - 15.0) < 1e-6
    assert abs(added[welded.item_id] - 10.0) < 1e-6
    db_session.refresh(reqs[painted.item_id])
    db_session.refresh(reqs[welded.item_id])
    assert float(reqs[painted.item_id].net_required_qty) == 17.0
    assert float(reqs[welded.item_id].net_required_qty) == 10.0
    assert float(reqs[welded.item_id].covered_qty) == 10.0
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
    assert painted.item_id not in added
    assert abs(added[welded.item_id] - 10.0) < 1e-6
    db_session.refresh(reqs[welded.item_id])
    assert float(reqs[welded.item_id].net_required_qty) == 10.0
    assert float(reqs[welded.item_id].covered_qty) == 10.0
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
    assert welded.item_id not in reqs

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
    # Parent's net is WIP-netted to 0; the child's gross exploded through WIP.
    assert float(reqs[painted.item_id].net_required_qty) == 0.0
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

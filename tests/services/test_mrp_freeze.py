"""Increment-2 (freeze v2) tests — refreeze_active_snapshots and the shared pool.

The matrix (spec §13): shared stock once, produced sub-assembly once, WIP &
supplier self-exclusion, id preservation, idempotency, single-plan parity,
freeze-table values, version isolation, dropped-item zeroing, drift reset,
dry_run, reconcile guard, and the create-snapshot wrapper contract.
"""

from datetime import date, datetime
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _accepted_planning_truth(monkeypatch):
    monkeypatch.setattr(
        "app.services.planning_truth.require_accepted_truth",
        lambda db, consumer, **kwargs: SimpleNamespace(
            status="accepted", generation_id=1, cutoff=None, reason=None
        ),
    )

from app.models import (
    DefaultSpecification,
    Item,
    MrpFreezeAllocation,
    MrpFreezeBaseline,
    MrpFreezeComponent,
    MrpRequirement,
    PlannedPurchase,
    PlanningRun,
    ProductionOrder,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionProduct,
    SpecComponent,
    Specification,
    Supplier,
    SupplierOrder,
    SupplierOrderItem,
    SyncLink,
)
from app.services.mrp_freeze import (
    build_shared_pools,
    pool_key_for,
    refreeze_active_snapshots,
)
from app.services.mrp_reconciliation import reconcile_snapshot
from app.services.one_c_purchase_order_export import PURCHASE_ORDER_ENTITY
from app.services.period_plan_service import create_mrp_snapshot_from_period_plan

# Future window so _latest_active_snapshot_run_ids keeps the runs "active".
AUG = (date(2026, 8, 1), date(2026, 8, 31), date(2026, 8, 15))
SEP = (date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 15))


# --------------------------------------------------------------------------- helpers
def _purchased(db, code, stock=0.0):
    item = Item(
        item_code=code, item_name=f"Куп {code}", item_article=code, unit="шт",
        stock_qty=stock, replenishment_method="Покупка", replenishment_time=3, status="active",
    )
    db.add(item)
    db.flush()
    return item


def _produced(db, code, stock=0.0):
    item = Item(
        item_code=code, item_name=f"Изд {code}", item_article=code, unit="шт",
        stock_qty=stock, replenishment_method="Производство", replenishment_time=0, status="active",
    )
    db.add(item)
    db.flush()
    return item


def _link_bom(db, parent, child, qty_per_unit=1.0):
    spec = Specification(spec_name=f"Spec {parent.item_code}", spec_ref1c=f"spec-{parent.item_code}")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=child.item_id, quantity=qty_per_unit))
    db.flush()
    return spec


def _plan(db, name, window, item, qty):
    pf, pt, bucket = window
    plan = ProductionPlanHeader(name=name, period_from=pf, period_to=pt, status="fixed", created_by="t")
    db.add(plan)
    db.flush()
    db.add(ProductionPlanLine(plan_id=plan.id, item_id=item.item_id, bucket_date=bucket, qty=qty))
    db.commit()
    return plan


def _snapshot(db, plan):
    return create_mrp_snapshot_from_period_plan(db, plan.id)


def _req(db, run_id, item_id):
    return db.query(MrpRequirement).filter_by(run_id=int(run_id), item_id=int(item_id)).one()


def _purchase_qty(db, run_id, item_id):
    rows = db.query(PlannedPurchase.qty).filter_by(run_id=int(run_id), item_id=int(item_id)).all()
    return sum(float(q or 0.0) for (q,) in rows)


# --------------------------------------------------------------------------- 1
def test_two_active_runs_share_stock_once(db_session):
    item = _purchased(db_session, "SHARE", stock=100.0)
    pa = _plan(db_session, "Авг", AUG, item, 100)
    pb = _plan(db_session, "Сен", SEP, item, 20)
    run_a = _snapshot(db_session, pa)["run_id"]
    run_b = _snapshot(db_session, pb)["run_id"]  # refreezes both

    # Aug (earliest need) eats the whole 100 → net 0; Sep sees a depleted pool.
    assert float(_req(db_session, run_a, item.item_id).net_required_qty) == pytest.approx(0.0)
    assert float(_req(db_session, run_b, item.item_id).net_required_qty) == pytest.approx(20.0)
    assert _purchase_qty(db_session, run_a, item.item_id) == pytest.approx(0.0)
    assert _purchase_qty(db_session, run_b, item.item_id) == pytest.approx(20.0)

    # baseline S0 == 100 for the pool in BOTH runs (latest version); Σ active
    # stock allocation across the pool == 100 (consumed once).
    def _active_version(run_id):
        return int(db_session.query(PlanningRun).filter_by(run_id=run_id).one().active_freeze_version)

    for run_id in (run_a, run_b):
        base = db_session.query(MrpFreezeBaseline).filter_by(
            run_id=run_id, item_id=item.item_id, freeze_version=_active_version(run_id)
        ).one()
        assert float(base.stock_qty) == pytest.approx(100.0)

    stock_alloc = 0.0
    for run_id in (run_a, run_b):
        stock_alloc += sum(
            float(a.alloc_qty)
            for a in db_session.query(MrpFreezeAllocation).filter_by(
                run_id=run_id, item_id=item.item_id, source_type="stock",
                freeze_version=_active_version(run_id),
            ).all()
        )
    assert stock_alloc == pytest.approx(100.0)


# --------------------------------------------------------------------------- 2
def test_produced_subassembly_stock_netted_once(db_session):
    root = _produced(db_session, "B-ROOT")
    sub = _produced(db_session, "B-SUB", stock=100.0)
    leaf = _purchased(db_session, "B-LEAF")
    _link_bom(db_session, root, sub, 1.0)
    _link_bom(db_session, sub, leaf, 1.0)
    pa = _plan(db_session, "Авг", AUG, root, 100)
    pb = _plan(db_session, "Сен", SEP, root, 100)
    run_a = _snapshot(db_session, pa)["run_id"]
    run_b = _snapshot(db_session, pb)["run_id"]

    # S's 100 stock covers exactly one run's release → leaf deficit 200-100=100.
    assert _purchase_qty(db_session, run_a, leaf.item_id) == pytest.approx(0.0)
    assert _purchase_qty(db_session, run_b, leaf.item_id) == pytest.approx(100.0)


# --------------------------------------------------------------------------- 3
def test_wip_self_exclusion(db_session):
    root = _produced(db_session, "W-ROOT")
    sub = _produced(db_session, "W-SUB")
    leaf = _purchased(db_session, "W-LEAF")
    _link_bom(db_session, root, sub, 1.0)
    _link_bom(db_session, sub, leaf, 1.0)
    plan = _plan(db_session, "Авг", AUG, root, 100)
    run_id = _snapshot(db_session, plan)["run_id"]
    sub_req = _req(db_session, run_id, sub.item_id)
    assert float(sub_req.net_required_qty) == pytest.approx(100.0)

    # OWN materialised WIP on the sub (executes this net) — must NOT cover it.
    own_order = ProductionOrder(
        order_number="MRP-OWN", order_date=datetime(2026, 7, 1), deletion_mark=False,
        source="mrp", source_run_id=run_id,
    )
    db_session.add(own_order)
    db_session.flush()
    db_session.add(ProductionProduct(
        order_id=own_order.order_id, item_id=sub.item_id, line_number=1,
        quantity=50, produced_qty=0, remaining_qty=50, source_mrp_requirement_id=sub_req.id,
    ))
    # FOREIGN 1C WIP on the sub — legit coverage.
    foreign = ProductionOrder(
        order_number="1C-FOREIGN", order_date=datetime(2026, 7, 1), deletion_mark=False,
        source="1c", order_ref1c="po-foreign",
    )
    db_session.add(foreign)
    db_session.flush()
    db_session.add(ProductionProduct(
        order_id=foreign.order_id, item_id=sub.item_id, line_number=1,
        quantity=30, produced_qty=0, remaining_qty=30,
    ))
    db_session.commit()

    refreeze_active_snapshots(db_session)
    net = float(_req(db_session, run_id, sub.item_id).net_required_qty)
    assert net == pytest.approx(70.0)  # 100 - 30 foreign; own 50 excluded

    # Second refreeze must not degrade the net further (own stays excluded).
    refreeze_active_snapshots(db_session)
    assert float(_req(db_session, run_id, sub.item_id).net_required_qty) == pytest.approx(70.0)


# --------------------------------------------------------------------------- 4
def test_supplier_self_exclusion_keeps_exported_purchase(db_session):
    item = _purchased(db_session, "SUP-X")
    plan = _plan(db_session, "Авг", AUG, item, 100)
    run_id = _snapshot(db_session, plan)["run_id"]
    purchase = db_session.query(PlannedPurchase).filter_by(run_id=run_id, item_id=item.item_id).one()
    assert float(purchase.qty) == pytest.approx(100.0)

    # Export the purchase to a 1C supplier order carrying the same 100 in transit.
    supplier = Supplier(supplier_ref1c="s1", supplier_name="ООО")
    db_session.add(supplier)
    db_session.flush()
    order = SupplierOrder(
        order_number="ЗП-1", order_date=datetime(2026, 7, 1), order_ref1c="so-ref-1",
        supplier_id=supplier.supplier_id, order_state_name="В пути", deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(SupplierOrderItem(
        order_id=order.order_id, item_id_ref=item.item_id, quantity=100,
        received_qty=0, remaining_qty=100, delivery_date=datetime(2026, 8, 10),
    ))
    db_session.add(SyncLink(
        source_system="PRODPLAN", source_doctype="planned_purchase", source_id=purchase.purchase_id,
        target_entity=PURCHASE_ORDER_ENTITY, target_ref_key="so-ref-1", status="success",
    ))
    db_session.commit()

    refreeze_active_snapshots(db_session)

    # The exported purchase survives (own coverage), no duplicate is created, and
    # its self-excluded supplier order is NOT written as a freeze allocation.
    remaining = db_session.query(PlannedPurchase).filter_by(run_id=run_id, item_id=item.item_id).all()
    assert len(remaining) == 1
    assert int(remaining[0].purchase_id) == int(purchase.purchase_id)
    assert db_session.query(MrpFreezeAllocation).filter_by(
        run_id=run_id, item_id=item.item_id, source_type="supplier_order"
    ).count() == 0


# --------------------------------------------------------------------------- 5
def test_requirement_ids_preserved_across_refreeze(db_session):
    root = _produced(db_session, "ID-ROOT")
    leaf = _purchased(db_session, "ID-LEAF")
    _link_bom(db_session, root, leaf, 2.0)
    plan = _plan(db_session, "Авг", AUG, root, 10)
    run_id = _snapshot(db_session, plan)["run_id"]
    before = {int(r.item_id): int(r.id) for r in db_session.query(MrpRequirement).filter_by(run_id=run_id).all()}

    refreeze_active_snapshots(db_session)
    after = {int(r.item_id): int(r.id) for r in db_session.query(MrpRequirement).filter_by(run_id=run_id).all()}
    assert before == after

    # A production order bound to the root req stays valid (no orphaned FK).
    root_req_id = before[root.item_id]
    order = ProductionOrder(order_number="X1", order_date=datetime(2026, 7, 1), deletion_mark=False, source="mrp", source_run_id=run_id)
    db_session.add(order)
    db_session.flush()
    db_session.add(ProductionProduct(order_id=order.order_id, item_id=root.item_id, line_number=1, quantity=10, produced_qty=0, remaining_qty=10, source_mrp_requirement_id=root_req_id))
    db_session.commit()
    refreeze_active_snapshots(db_session)
    pp = db_session.query(ProductionProduct).filter_by(order_id=order.order_id).one()
    assert int(pp.source_mrp_requirement_id) == root_req_id
    assert db_session.query(MrpRequirement).filter_by(id=root_req_id).count() == 1


# --------------------------------------------------------------------------- 6
def test_refreeze_is_value_idempotent(db_session):
    item = _purchased(db_session, "IDEM", stock=30.0)
    pa = _plan(db_session, "Авг", AUG, item, 100)
    pb = _plan(db_session, "Сен", SEP, item, 40)
    run_a = _snapshot(db_session, pa)["run_id"]
    run_b = _snapshot(db_session, pb)["run_id"]

    def _sig():
        reqs = {
            int(r.item_id): (round(float(r.net_required_qty), 6), round(float(r.initial_snapshot_stock or 0.0), 6), int(r.freeze_version or 0))
            for r in db_session.query(MrpRequirement).filter(MrpRequirement.run_id.in_([run_a, run_b])).all()
        }
        return reqs

    sig1 = _sig()
    v_before = {b.run_id: b.freeze_version for b in db_session.query(MrpRequirement).filter(MrpRequirement.run_id.in_([run_a, run_b]))}

    refreeze_active_snapshots(db_session)
    sig2 = _sig()

    # net + initial identical; only the version advances by 1.
    assert {k: (v[0], v[1]) for k, v in sig1.items()} == {k: (v[0], v[1]) for k, v in sig2.items()}
    assert all(sig2[k][2] == sig1[k][2] + 1 for k in sig1)


# --------------------------------------------------------------------------- 7
def test_single_plan_parity_arithmetic(db_session):
    # Purchase item, stock 30, demand 100 → net 70 → purchase 70 (legacy formula).
    item = _purchased(db_session, "PAR", stock=30.0)
    plan = _plan(db_session, "Авг", AUG, item, 100)
    run_id = _snapshot(db_session, plan)["run_id"]
    assert float(_req(db_session, run_id, item.item_id).net_required_qty) == pytest.approx(70.0)
    assert _purchase_qty(db_session, run_id, item.item_id) == pytest.approx(70.0)
    # Frozen stock allocation equals the netted 30.
    assert float(_req(db_session, run_id, item.item_id).initial_snapshot_stock) == pytest.approx(30.0)


# --------------------------------------------------------------------------- 8
def test_freeze_table_values_and_pool_columns(db_session):
    root = _produced(db_session, "T-ROOT")
    sub = _produced(db_session, "T-SUB", stock=20.0)
    leaf = _purchased(db_session, "T-LEAF", stock=5.0)
    _link_bom(db_session, root, sub, 1.0)
    _link_bom(db_session, sub, leaf, 2.0)
    plan = _plan(db_session, "Авг", AUG, root, 50)
    run_id = _snapshot(db_session, plan)["run_id"]

    # baseline: unit_coef == 1, stock_qty == S0, pool columns never NULL.
    for b in db_session.query(MrpFreezeBaseline).filter_by(run_id=run_id).all():
        assert float(b.unit_coef) == pytest.approx(1.0)
        assert b.characteristic_ref == "" and b.organization_ref == "" and b.planning_stock_pool == "default"
    sub_base = db_session.query(MrpFreezeBaseline).filter_by(run_id=run_id, item_id=sub.item_id).one()
    assert float(sub_base.stock_qty) == pytest.approx(20.0)

    # component: norm captured, pool cols set, stock-covered parent included.
    comp = db_session.query(MrpFreezeComponent).filter_by(run_id=run_id, parent_item_id=sub.item_id, component_item_id=leaf.item_id).one()
    assert float(comp.norm_qty_per_unit) == pytest.approx(2.0)
    assert comp.parent_planning_stock_pool == "default" and comp.component_planning_stock_pool == "default"
    assert db_session.query(MrpFreezeComponent).filter_by(run_id=run_id, parent_item_id=root.item_id, component_item_id=sub.item_id).count() == 1

    # allocation: stock source rows exist, fact == S0, pool cols never NULL.
    stock_allocs = db_session.query(MrpFreezeAllocation).filter_by(run_id=run_id, source_type="stock").all()
    assert stock_allocs
    for a in db_session.query(MrpFreezeAllocation).filter_by(run_id=run_id).all():
        assert a.characteristic_ref == "" and a.organization_ref == "" and a.planning_stock_pool == "default"

    # per-req initial_snapshot_stock == Σ its stock allocations.
    sub_req = _req(db_session, run_id, sub.item_id)
    sub_stock_alloc = sum(float(a.alloc_qty) for a in stock_allocs if int(a.requirement_id) == int(sub_req.id))
    assert float(sub_req.initial_snapshot_stock) == pytest.approx(sub_stock_alloc)


# --------------------------------------------------------------------------- 9
def test_prior_version_rows_immutable(db_session):
    item = _purchased(db_session, "VER", stock=10.0)
    plan = _plan(db_session, "Авг", AUG, item, 50)
    run_id = _snapshot(db_session, plan)["run_id"]
    v1 = [
        (b.item_id, float(b.stock_qty), int(b.freeze_version))
        for b in db_session.query(MrpFreezeBaseline).filter_by(run_id=run_id, freeze_version=1).all()
    ]
    assert v1  # version 1 exists

    refreeze_active_snapshots(db_session, include_plan_id=plan.id)
    # Version-1 rows are untouched; version 2 is a fresh INSERT.
    v1_after = [
        (b.item_id, float(b.stock_qty), int(b.freeze_version))
        for b in db_session.query(MrpFreezeBaseline).filter_by(run_id=run_id, freeze_version=1).all()
    ]
    assert sorted(v1_after) == sorted(v1)
    assert db_session.query(MrpFreezeBaseline).filter_by(run_id=run_id, freeze_version=2).count() >= 1


# --------------------------------------------------------------------------- 10
def test_dropped_item_zeroed_and_stamped(db_session):
    root = _produced(db_session, "D-ROOT")
    sub = _produced(db_session, "D-SUB")
    spec = _link_bom(db_session, root, sub, 1.0)
    plan = _plan(db_session, "Авг", AUG, root, 40)
    run_id = _snapshot(db_session, plan)["run_id"]
    sub_req = _req(db_session, run_id, sub.item_id)
    assert float(sub_req.net_required_qty) == pytest.approx(40.0)

    # Remove the component so the sub is no longer reached by the explosion.
    db_session.query(SpecComponent).filter_by(spec_id=spec.spec_id, item_id=sub.item_id).delete()
    db_session.commit()

    refreeze_active_snapshots(db_session, include_plan_id=plan.id)
    sub_req = _req(db_session, run_id, sub.item_id)
    assert float(sub_req.total_required_qty) == pytest.approx(0.0)
    assert float(sub_req.net_required_qty) == pytest.approx(0.0)
    assert float(sub_req.initial_snapshot_stock or 0.0) == pytest.approx(0.0)
    assert int(sub_req.freeze_version) == 2
    assert db_session.query(MrpFreezeAllocation).filter_by(run_id=run_id, item_id=sub.item_id).count() == 0


# --------------------------------------------------------------------------- 11
def test_drift_adjustment_reset_on_refreeze(db_session):
    item = _purchased(db_session, "DRIFT", stock=0.0)
    plan = _plan(db_session, "Авг", AUG, item, 10)
    run_id = _snapshot(db_session, plan)["run_id"]
    req = _req(db_session, run_id, item.item_id)
    req.drift_adjustment_qty = 7.0
    db_session.commit()

    refreeze_active_snapshots(db_session)
    assert float(_req(db_session, run_id, item.item_id).drift_adjustment_qty) == pytest.approx(0.0)


# --------------------------------------------------------------------------- 12
def test_dry_run_writes_nothing(db_session):
    item = _purchased(db_session, "DRY", stock=0.0)
    plan = _plan(db_session, "Авг", AUG, item, 10)
    run_id = _snapshot(db_session, plan)["run_id"]
    version_before = int(_req(db_session, run_id, item.item_id).freeze_version)
    baseline_before = db_session.query(MrpFreezeBaseline).filter_by(run_id=run_id).count()

    report = refreeze_active_snapshots(db_session, dry_run=True)
    assert report["dry_run"] is True
    assert report["results"]
    # Nothing persisted: version and freeze rows are unchanged.
    assert int(_req(db_session, run_id, item.item_id).freeze_version) == version_before
    assert db_session.query(MrpFreezeBaseline).filter_by(run_id=run_id).count() == baseline_before


# --------------------------------------------------------------------------- 13
def test_reconcile_frozen_run_does_not_rewrite_net_or_trim(db_session):
    # Increment 4: the temporary freeze_guard is gone; the drift-only reconcile
    # is idempotent by construction. A top-level (bom level 0) purchased item is
    # out of drift, so a post-freeze stock rise does not move its frozen net and
    # its purchase is not trimmed.
    item = _purchased(db_session, "GUARD", stock=0.0)
    plan = _plan(db_session, "Авг", AUG, item, 60)
    run_id = _snapshot(db_session, plan)["run_id"]
    item.stock_qty = 60.0
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)
    assert "freeze_guard" not in res
    assert res["status"] == "ok"
    assert res["purchase_pruned"] == []
    # Frozen net is NOT rewritten; the purchase is NOT trimmed.
    assert float(_req(db_session, run_id, item.item_id).net_required_qty) == pytest.approx(60.0)
    assert db_session.query(PlannedPurchase).filter_by(run_id=run_id, item_id=item.item_id).count() == 1


def test_reconcile_unfrozen_run_is_needs_freeze(db_session):
    # A run without an active freeze version has no authoritative net to size
    # against: reconcile returns needs_freeze and runs only repairs — it does NOT
    # rewrite net nor trim purchases (the old legacy re-explosion trim is gone).
    item = _purchased(db_session, "UNFROZEN", stock=0.0)
    plan = _plan(db_session, "Авг", AUG, item, 60)
    run_id = _snapshot(db_session, plan)["run_id"]
    run = db_session.query(PlanningRun).filter_by(run_id=run_id).one()
    run.active_freeze_version = None  # simulate a pre-freeze legacy run
    item.stock_qty = 60.0
    db_session.commit()

    res = reconcile_snapshot(db_session, run_id)
    assert res["status"] == "needs_freeze"
    assert "freeze_guard" not in res
    assert res["purchase_pruned"] == []
    assert db_session.query(PlannedPurchase).filter_by(run_id=run_id, item_id=item.item_id).count() == 1


# --------------------------------------------------------------------------- 14
def test_wrapper_contract_and_report(db_session):
    item = _purchased(db_session, "WRAP", stock=0.0)
    plan = _plan(db_session, "Авг", AUG, item, 25)
    result = _snapshot(db_session, plan)
    for key in ("status", "run_id", "plan_id", "requirement_count", "bucket_count",
                "production_count", "stage_count", "purchase_count", "rework_count", "freeze_version"):
        assert key in result
    assert result["freeze_version"] >= 1
    assert result["purchase_count"] == 1

    report = refreeze_active_snapshots(db_session, dry_run=True)
    assert report["status"] == "ok" and report["dry_run"] is True
    assert report["order"] and report["totals"]["runs"] >= 1


# --------------------------------------------------------------------------- extra: pool key + build helper
def test_pool_key_normalises_to_default(db_session):
    pk = pool_key_for(123, characteristic_ref="abc", organization_ref="org")
    assert (pk.characteristic_ref, pk.organization_ref, pk.planning_stock_pool) == ("", "", "default")


def test_build_shared_pools_snapshots_initial_stock(db_session):
    item = _purchased(db_session, "POOL", stock=42.0)
    plan = _plan(db_session, "Авг", AUG, item, 10)
    run_id = _snapshot(db_session, plan)["run_id"]
    pools = build_shared_pools(db_session, [run_id])
    assert pools.stock_initial.get(item.item_id) == pytest.approx(42.0)
    assert pools.stock is not pools.stock_initial

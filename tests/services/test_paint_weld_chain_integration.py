"""Integration: paint↔weld pairs greying in the order journal and the MRP
reconciliation catch-up filter.

Rules (see .docs/paint_weld_chain_logic.md, stage 1):
- welded item that is part of an ACTIVE pair is not ordered on its own — the
  journal greys it (skipped) and reconcile does not materialize a catch-up
  order for it, but its net demand / open orders keep counting.
- an ORPHAN welded item (no active pair) behaves exactly as before.
"""
from __future__ import annotations

from datetime import date
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
    Item,
    MrpRequirement,
    PaintWeldPair,
    PlanningRun,
    ProductionPlanHeader,
    ProductionPlanLine,
)
from app.services.mrp_reconciliation import reconcile_snapshot as _public_reconcile_snapshot


def reconcile_snapshot(db, run_id, **kwargs):
    return _public_reconcile_snapshot(
        db, run_id, diagnostic_legacy=True, **kwargs
    )
from app.services.period_plan_service import create_mrp_snapshot_from_period_plan
from app.services.production_control_journal import (
    create_production_orders_from_mrp_requirements,
)


def _welded_item(db, code: str, *, stock: float = 0.0) -> Item:
    item = Item(
        item_code=code,
        item_name=f"{code}, после сварки",
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


def _active_pair(db, welded: Item) -> PaintWeldPair:
    # painted parent identity is irrelevant to the filter; only the active pair
    # marking the welded item matters.
    painted = Item(
        item_code=f"P-{welded.item_code}",
        item_name=f"{welded.item_code}, после покраски",
        item_article=f"P-{welded.item_code}",
        unit="шт",
        stock_qty=0,
        replenishment_method="Производство",
        status="active",
    )
    db.add(painted)
    db.flush()
    pair = PaintWeldPair(
        painted_item_id=painted.item_id,
        welded_item_id=welded.item_id,
        source="auto",
        is_active=True,
    )
    db.add(pair)
    db.flush()
    return pair


def _standalone_requirement(db, item: Item, *, net: float) -> MrpRequirement:
    run = PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={}, pinned=True)
    db.add(run)
    db.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=net,
        net_required_qty=net,
        covered_qty=0.0,
        remaining_qty=net,
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        bom_level=0,
    )
    db.add(req)
    db.flush()
    return req


# ---------------------------------------------------------------------------
# Journal greying
# ---------------------------------------------------------------------------

def test_journal_skips_welded_with_active_pair(db_session):
    welded = _welded_item(db_session, "WLD-A")
    _active_pair(db_session, welded)
    req = _standalone_requirement(db_session, welded, net=10)
    db_session.commit()

    result = create_production_orders_from_mrp_requirements(db_session, [req.id])

    assert result["created"] == []
    reasons = [row["reason"] for row in result["skipped"]]
    assert "заказывается по цепочке от окраски" in reasons


def test_journal_materializes_orphan_welded(db_session):
    orphan = _welded_item(db_session, "WLD-ORPH")  # no pair
    req = _standalone_requirement(db_session, orphan, net=10)
    db_session.commit()

    result = create_production_orders_from_mrp_requirements(db_session, [req.id])

    assert len(result["created"]) == 1
    assert result["created"][0]["item_id"] == orphan.item_id


def test_journal_skip_ignores_inactive_pair(db_session):
    welded = _welded_item(db_session, "WLD-INA")
    pair = _active_pair(db_session, welded)
    pair.is_active = False  # deactivated -> welded is orderable again
    req = _standalone_requirement(db_session, welded, net=10)
    db_session.commit()

    result = create_production_orders_from_mrp_requirements(db_session, [req.id])
    assert len(result["created"]) == 1


# ---------------------------------------------------------------------------
# Reconcile catch-up filter
# ---------------------------------------------------------------------------

def _plan_with_line(db, item: Item, qty: float):
    plan = ProductionPlanHeader(
        name="План июнь",
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        status="fixed",
        created_by="test",
    )
    db.add(plan)
    db.flush()
    db.add(
        ProductionPlanLine(
            plan_id=plan.id, item_id=item.item_id, bucket_date=date(2026, 6, 15), qty=qty
        )
    )
    db.commit()
    return plan


def test_reconcile_does_not_materialize_welded_with_pair(db_session):
    welded = _welded_item(db_session, "WLD-RC", stock=0.0)
    _active_pair(db_session, welded)
    plan = _plan_with_line(db_session, welded, qty=34)

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
    run_id = snap["run_id"]
    req = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == welded.item_id)
        .one()
    )
    assert float(req.net_required_qty) == 34.0

    res = reconcile_snapshot(db_session, run_id)

    # No catch-up production order materialized for the paired welded item...
    assert all(row["item_id"] != welded.item_id for row in res["production_added"])
    # ...but the net demand is untouched (still counts against the chain).
    db_session.refresh(req)
    assert float(req.net_required_qty) == 34.0


def test_reconcile_materializes_orphan_welded_as_before(db_session):
    orphan = _welded_item(db_session, "WLD-RC-ORPH", stock=0.0)  # no pair
    plan = _plan_with_line(db_session, orphan, qty=34)

    snap = create_mrp_snapshot_from_period_plan(db_session, plan.id)
    run_id = snap["run_id"]
    req = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == orphan.item_id)
        .one()
    )

    res = reconcile_snapshot(db_session, run_id)

    added = [row for row in res["production_added"] if row["item_id"] == orphan.item_id]
    assert len(added) == 1
    assert added[0]["qty"] == pytest.approx(34.0)
    db_session.refresh(req)
    assert float(req.net_required_qty) == 34.0


def test_reconcile_net_unchanged_between_pair_and_orphan(db_session):
    """The net side must be identical whether or not the pair filter fires."""
    paired = _welded_item(db_session, "WLD-CMP-A", stock=0.0)
    _active_pair(db_session, paired)
    plan_a = _plan_with_line(db_session, paired, qty=20)
    snap_a = create_mrp_snapshot_from_period_plan(db_session, plan_a.id)
    reconcile_snapshot(db_session, snap_a["run_id"])
    req_a = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == snap_a["run_id"], MrpRequirement.item_id == paired.item_id)
        .one()
    )

    orphan = _welded_item(db_session, "WLD-CMP-B", stock=0.0)
    plan_b = _plan_with_line(db_session, orphan, qty=20)
    snap_b = create_mrp_snapshot_from_period_plan(db_session, plan_b.id)
    reconcile_snapshot(db_session, snap_b["run_id"])
    req_b = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == snap_b["run_id"], MrpRequirement.item_id == orphan.item_id)
        .one()
    )

    assert float(req_a.net_required_qty) == float(req_b.net_required_qty) == 20.0

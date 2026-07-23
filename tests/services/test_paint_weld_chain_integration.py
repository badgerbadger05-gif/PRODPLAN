"""Integration: paint↔weld pairs greying in the order journal and the MRP
reconciliation catch-up filter.

Rules (see .docs/paint_weld_chain_logic.md, stage 1):
- welded item that is part of an ACTIVE pair is not ordered on its own — the
  journal greys it (skipped) and reconcile does not materialize a catch-up
  order for it, but its net demand / open orders keep counting.
- an ORPHAN welded item (no active pair) behaves exactly as before.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.models import (
    DefaultSpecification,
    Item,
    LedgerGeneration,
    MrpRequirement,
    PaintWeldPair,
    PhysicalImportBatch,
    PlanningRun,
    PlanningTruthState,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionProduct,
    ReservationEntry,
    SpecComponent,
    Specification,
    StockBin,
)
from app.services import one_c_production_order_export as production_exporter
from app.services.paint_weld_chain import open_paint_chain
from app.services.planning_truth import publish_generation


from app.services.period_plan_service import create_mrp_snapshot_from_period_plan
from app.services.production_control_journal import (
    create_production_orders_from_mrp_requirements,
)


@pytest.fixture(autouse=True)
def _accepted_planning_truth(db_session):
    cutoff = datetime(2026, 5, 31, 23, 59, tzinfo=timezone.utc)
    batch = PhysicalImportBatch(
        batch_key="paint-weld-integration",
        status="completed",
        cutoff=cutoff,
        completed_at=cutoff,
        source_watermarks={"explicit_empty_prefix": True},
    )
    generation = LedgerGeneration(
        generation_key="paint-weld-integration",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={"explicit_empty_prefix": True},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
        physical_import_batch=batch,
        algorithm_version="test/1",
        replay_version="test/1",
    )
    publish_generation(db_session, generation)
    db_session.flush()
    return generation


def _accepted_generation(db) -> LedgerGeneration:
    state = db.get(PlanningTruthState, 1)
    assert state is not None and state.current_generation_id is not None
    return db.get(LedgerGeneration, int(state.current_generation_id))


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
    if stock:
        generation = _accepted_generation(db)
        db.add(
            StockBin(
                ledger_generation_id=generation.id,
                item_id=item.item_id,
                characteristic_ref="",
                organization_ref="",
                warehouse_ref1c="WH",
                on_hand=stock,
            )
        )
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
    generation = _accepted_generation(db)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        pinned=True,
        active_freeze_version=1,
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
    )
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
        freeze_version=1,
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

    snap = create_mrp_snapshot_from_period_plan(
        db_session, plan.id, generation_key=f"paint-weld-paired-{plan.id}"
    )
    db_session.commit()
    run_id = snap["run_id"]
    req = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == welded.item_id)
        .one()
    )
    assert float(req.net_required_qty) == 34.0

    res = create_production_orders_from_mrp_requirements(
        db_session, [req.id]
    )

    # No catch-up production order materialized for the paired welded item...
    assert res["created"] == []
    assert any(
        row["item_id"] == welded.item_id
        and row["reason"] == "заказывается по цепочке от окраски"
        for row in res["skipped"]
    )
    # ...but the net demand is untouched (still counts against the chain).
    db_session.refresh(req)
    assert float(req.net_required_qty) == 34.0


def test_reconcile_materializes_orphan_welded_as_before(db_session):
    orphan = _welded_item(db_session, "WLD-RC-ORPH", stock=0.0)  # no pair
    plan = _plan_with_line(db_session, orphan, qty=34)

    snap = create_mrp_snapshot_from_period_plan(
        db_session, plan.id, generation_key=f"paint-weld-orphan-{plan.id}"
    )
    db_session.commit()
    run_id = snap["run_id"]
    req = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == run_id, MrpRequirement.item_id == orphan.item_id)
        .one()
    )

    res = create_production_orders_from_mrp_requirements(
        db_session, [req.id]
    )

    added = [row for row in res["created"] if row["item_id"] == orphan.item_id]
    assert len(added) == 1
    assert added[0]["qty"] == pytest.approx(34.0)
    db_session.refresh(req)
    assert float(req.net_required_qty) == 34.0


def test_reconcile_net_unchanged_between_pair_and_orphan(db_session):
    """The net side must be identical whether or not the pair filter fires."""
    paired = _welded_item(db_session, "WLD-CMP-A", stock=0.0)
    _active_pair(db_session, paired)
    plan_a = _plan_with_line(db_session, paired, qty=20)
    snap_a = create_mrp_snapshot_from_period_plan(
        db_session, plan_a.id, generation_key=f"paint-weld-compare-a-{plan_a.id}"
    )
    db_session.commit()
    req_a = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == snap_a["run_id"], MrpRequirement.item_id == paired.item_id)
        .one()
    )

    orphan = _welded_item(db_session, "WLD-CMP-B", stock=0.0)
    plan_b = _plan_with_line(db_session, orphan, qty=20)
    snap_b = create_mrp_snapshot_from_period_plan(
        db_session, plan_b.id, generation_key=f"paint-weld-compare-b-{plan_b.id}"
    )
    db_session.commit()
    req_b = (
        db_session.query(MrpRequirement)
        .filter(MrpRequirement.run_id == snap_b["run_id"], MrpRequirement.item_id == orphan.item_id)
        .one()
    )

    assert float(req_a.net_required_qty) == float(req_b.net_required_qty) == 20.0


def test_chain_materializes_exact_weld_slice_from_published_obligation(
    db_session, monkeypatch
):
    welded = _welded_item(db_session, "WLD-END-TO-END", stock=0)
    welded.item_ref1c = "ref-weld-e2e"
    painted = Item(
        item_code="PNT-END-TO-END",
        item_name="PNT-END-TO-END, после покраски",
        item_article="PNT-END-TO-END",
        item_ref1c="ref-paint-e2e",
        unit="шт",
        stock_qty=999,  # legacy stock must not participate in the decision
        replenishment_method="Производство",
        replenishment_time=0,
        status="active",
    )
    db_session.add(painted)
    db_session.flush()
    spec = Specification(
        spec_name="Paint E2E",
        spec_ref1c="spec-paint-e2e",
    )
    db_session.add(spec)
    db_session.flush()
    db_session.add(
        DefaultSpecification(item_id=painted.item_id, spec_id=spec.spec_id)
    )
    db_session.add(
        SpecComponent(
            spec_id=spec.spec_id,
            item_id=welded.item_id,
            quantity=1,
            component_type="Сборка",
        )
    )
    db_session.add(
        PaintWeldPair(
            painted_item_id=painted.item_id,
            welded_item_id=welded.item_id,
            source="manual",
            is_active=True,
        )
    )
    plan = _plan_with_line(db_session, painted, qty=10)

    snap = create_mrp_snapshot_from_period_plan(
        db_session, plan.id, generation_key=f"paint-weld-e2e-{plan.id}"
    )
    db_session.commit()
    painted_req = db_session.query(MrpRequirement).filter_by(
        run_id=snap["run_id"], item_id=painted.item_id
    ).one()
    welded_req = db_session.query(MrpRequirement).filter_by(
        run_id=snap["run_id"], item_id=welded.item_id
    ).one()
    reservation = db_session.query(ReservationEntry).filter_by(
        ledger_generation_id=snap["ledger_generation_id"],
        requirement_id=welded_req.id,
        realization_mode="make",
    ).one()
    assert float(reservation.reserved_qty) == pytest.approx(10)

    blocked = create_production_orders_from_mrp_requirements(
        db_session, [welded_req.id]
    )
    assert blocked["created"] == []
    painted_created = create_production_orders_from_mrp_requirements(
        db_session, [painted_req.id]
    )
    painted_product_id = painted_created["created"][0]["product_id"]
    db_session.commit()
    monkeypatch.setattr(
        production_exporter,
        "_load_odata_config",
        lambda: {
            "base_url": "http://mtzw7/unf_demo/odata",
            "username": "u",
            "password": "p",
        },
    )

    preview = open_paint_chain(
        db_session,
        painted_product_id=painted_product_id,
        dry_run=True,
    )

    assert preview["guard"]["requirement_id"] == welded_req.id
    assert preview["guard"]["reservation_id"] == reservation.id
    assert preview["welded"]["qty"] == pytest.approx(10)
    assert db_session.query(ProductionProduct).filter_by(
        source_mrp_requirement_id=welded_req.id
    ).count() == 0

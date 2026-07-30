"""What the frozen obligation may and may not do — CANON.md §Неподвижное.

A freeze is written once and never recomputed, so every defect here is
permanent for the life of the plan:

* a convergent BOM must not lose the demand of its second branch;
* demand may not silently fall off the end of the explosion;
* the same generation must freeze identically on any calendar day;
* the WIP pool may not fail open into "no WIP at all";
* `PlannedOrder.demand_ref` must be readable by the code that writes it;
* an execution projection may not invent a zero or a 100%.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.models import (
    AssemblyRate,
    DefaultSpecification,
    Item,
    LedgerGeneration,
    MrpRequirement,
    PhysicalImportBatch,
    PlannedOrder,
    PlanningRun,
    PlanningTruthState,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionResource,
    SpecComponent,
    Specification,
    StockWarehouse,
)
from app.services import period_plan_service
from app.services.period_plan_service import (
    _build_execution_snapshot_rows,
    _execution_row_summary,
    _explode_bom_net_first,
    build_period_plan_execution_snapshot,
    fix_period_plan,
)


CUTOFF = datetime(2026, 7, 23)
FLOOR = date(2026, 7, 23)


@pytest.fixture(autouse=True)
def _accepted_planning_truth(db_session):
    batch = PhysicalImportBatch(
        batch_key="freeze-contract", status="completed", cutoff=CUTOFF,
        source_watermarks={"opening_at": "2026-06-01T00:00:00+00:00"},
        completed_at=CUTOFF,
    )
    generation = LedgerGeneration(
        generation_key="freeze-contract", status="accepted", cutoff=CUTOFF,
        accepted_at=CUTOFF, source_watermarks={"replay_from": "2026-06-01T00:00:00"},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
            "planning_snapshots": True,
        },
        physical_import_batch=batch, algorithm_version="test",
    )
    db_session.add_all([
        generation,
        StockWarehouse(
            warehouse_ref1c="WH-FREEZE-PLAN",
            warehouse_name="Freeze planning contour",
            is_selected=True,
            is_finished_goods=False,
        ),
    ])
    db_session.flush()
    db_session.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    resource = ProductionResource(
        resource_name="Freeze contract assembly",
        planning_range=30,
        capacity=Decimal("100"),
    )
    db_session.add(resource)
    db_session.flush()
    db_session.commit()
    db_session.info["generation_id"] = int(generation.id)
    db_session.info["assembly_resource_id"] = int(resource.resource_id)
    return generation


def _item(db, code, *, method="Производство") -> Item:
    item = Item(
        item_code=code, item_name=code, item_article=code, unit="шт",
        stock_qty=0.0, replenishment_method=method, replenishment_time=3,
        status="active",
    )
    db.add(item)
    db.flush()
    db.add(AssemblyRate(
        resource_id=int(db.info["assembly_resource_id"]),
        item_id=int(item.item_id),
        qty_per_capacity=Decimal("1"),
    ))
    db.flush()
    return item


def _bom(db, parent: Item, children: dict[Item, float]) -> None:
    spec = Specification(spec_name=f"spec {parent.item_code}")
    db.add(spec)
    db.flush()
    for child, qty in children.items():
        db.add(SpecComponent(
            spec_id=spec.spec_id, item_id=child.item_id, quantity=Decimal(str(qty)),
        ))
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.flush()


# ---------------------------------------------------------------------------
# convergent BOM — the children of a re-visited node must still explode
# ---------------------------------------------------------------------------

def test_convergent_bom_keeps_the_children_of_the_second_branch(db_session):
    """A→B→C and A→D→B→C: C must carry the demand of BOTH branches.

    The old global ``exploded_parents`` set gave the second arrival of B (at
    depth 2) its gross/net but never re-exploded it, so C silently kept only
    the A→B branch and every level under it was under-planned forever.
    """
    a = _item(db_session, "CONV-A")
    b = _item(db_session, "CONV-B")
    c = _item(db_session, "CONV-C", method="Покупка")
    d = _item(db_session, "CONV-D")
    _bom(db_session, a, {b: 1.0, d: 1.0})
    _bom(db_session, d, {b: 1.0})
    _bom(db_session, b, {c: 1.0})
    db_session.commit()

    bucket = date(2026, 8, 7)
    gross, _net, levels, warnings = _explode_bom_net_first(
        db_session, {a.item_id: {bucket: 10.0}}, need_date_floor=FLOOR,
    )

    assert warnings == []
    # B is reached directly (10) and through D (10).
    assert sum(gross[b.item_id].values()) == pytest.approx(20.0)
    # …and C must inherit the full 20, not the 10 of the first branch.
    assert sum(gross[c.item_id].values()) == pytest.approx(20.0)
    assert levels[b.item_id] == 1
    assert levels[c.item_id] == 2


def test_bom_cycle_is_reported_instead_of_silently_absorbed(db_session):
    a = _item(db_session, "CYC-A")
    b = _item(db_session, "CYC-B")
    _bom(db_session, a, {b: 1.0})
    _bom(db_session, b, {a: 1.0})
    db_session.commit()

    gross, _net, _levels, warnings = _explode_bom_net_first(
        db_session, {a.item_id: {date(2026, 8, 7): 4.0}}, need_date_floor=FLOOR,
    )

    assert sum(gross[b.item_id].values()) == pytest.approx(4.0)
    assert [w["code"] for w in warnings] == ["BOM_CYCLE_EDGE_SKIPPED"]
    assert warnings[0]["parent_item_id"] == b.item_id
    assert warnings[0]["child_item_id"] == a.item_id


def test_demand_deeper_than_the_explosion_limit_fails_closed(db_session):
    """21 chained levels: the residue may not be dropped without a word."""
    chain = [_item(db_session, f"DEEP-{index:02d}") for index in range(22)]
    for parent, child in zip(chain, chain[1:]):
        _bom(db_session, parent, {child: 1.0})
    db_session.commit()

    with pytest.raises(ValueError, match="предел вложенности"):
        _explode_bom_net_first(
            db_session, {chain[0].item_id: {date(2026, 8, 7): 1.0}},
            need_date_floor=FLOOR,
        )


# ---------------------------------------------------------------------------
# the freeze is dated by the generation cutoff, not by the wall clock
# ---------------------------------------------------------------------------

def test_need_dates_are_clamped_to_the_generation_cutoff_not_to_today(db_session):
    """The child need-date floor is the cutoff, so a rebuild is reproducible."""
    parent = _item(db_session, "FLOOR-P")
    child = _item(db_session, "FLOOR-C", method="Покупка")
    _bom(db_session, parent, {child: 1.0})
    db_session.commit()

    # The parent's bucket sits before the floor, so the child's need-date has
    # to be pulled up to the floor — and to nothing else.
    early, late = date(2026, 5, 1), date(2027, 1, 15)
    gross_early, _n, _l, _w = _explode_bom_net_first(
        db_session, {parent.item_id: {early: 3.0}}, need_date_floor=FLOOR,
    )
    gross_late, _n, _l, _w = _explode_bom_net_first(
        db_session, {parent.item_id: {early: 3.0}}, need_date_floor=late,
    )

    assert list(gross_early[child.item_id]) == [FLOOR]
    assert list(gross_late[child.item_id]) == [late]


def test_wip_pool_failure_fails_closed_instead_of_planning_without_wip(
    db_session, monkeypatch
):
    item = _item(db_session, "WIP-FAILCLOSED", method="Покупка")
    db_session.commit()

    def boom(_db):
        raise RuntimeError("wip pool unreadable")

    monkeypatch.setattr(period_plan_service, "_active_wip_eta_by_item", boom)

    with pytest.raises(RuntimeError, match="wip pool unreadable"):
        _explode_bom_net_first(
            db_session, {item.item_id: {date(2026, 8, 7): 1.0}},
            need_date_floor=FLOOR,
        )


def test_fixation_dates_orders_from_the_cutoff_not_from_today(db_session):
    """`order_date` never precedes the generation cutoff — and never today."""
    item = _item(db_session, "ORDER-DATE", method="Покупка")
    plan = ProductionPlanHeader(
        name="август", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        status="draft", created_by="test",
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(ProductionPlanLine(
        plan_id=plan.id, item_id=item.item_id, bucket_date=date(2026, 8, 7), qty=5,
    ))
    db_session.commit()

    fix_period_plan(db_session, plan.id, fixed_by="tester")

    from app.models import PlannedPurchase

    purchase = db_session.query(PlannedPurchase).one()
    # need_date − lead_time (3) = 2026-08-04, which is after the cutoff.
    assert purchase.order_date == date(2026, 8, 4)
    assert purchase.order_date >= CUTOFF.date()


# ---------------------------------------------------------------------------
# demand_ref: one spelling for the writer and the reader
# ---------------------------------------------------------------------------

def test_planned_production_orders_are_linked_back_to_their_requirement(db_session):
    """`ordered_qty`/`unassigned_qty` were dead: the reader searched `req:<id>`
    while the writer stamped `mrp_requirement:<id>`."""
    item = _item(db_session, "DEMAND-REF")
    _bom(db_session, item, {_item(db_session, "DEMAND-REF-C", method="Покупка"): 1.0})
    plan = ProductionPlanHeader(
        name="август", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        status="draft", created_by="test",
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(ProductionPlanLine(
        plan_id=plan.id, item_id=item.item_id, bucket_date=date(2026, 8, 7), qty=6,
    ))
    db_session.commit()

    fix_period_plan(db_session, plan.id, fixed_by="tester")
    run = db_session.query(PlanningRun).filter_by(
        source_plan_id=plan.id, status="FIXED_SNAPSHOT",
    ).one()
    requirement = db_session.query(MrpRequirement).filter_by(
        run_id=run.run_id, item_id=item.item_id,
    ).one()
    order = db_session.query(PlannedOrder).filter_by(
        run_id=run.run_id, item_id=item.item_id,
    ).one()
    assert order.demand_ref == f"mrp_requirement:{int(requirement.id)}"

    payload = build_period_plan_execution_snapshot(
        db_session, plan.id, run_id=int(run.run_id),
    )
    row = next(r for r in payload["rows"] if r["req_id"] == int(requirement.id))
    assert row["ordered_qty"] == pytest.approx(0.0)  # not opened in 1C yet
    work_item_types = {link["type"] for link in row["work_items"]}
    assert "planned_order" in work_item_types


def test_legacy_demand_ref_spelling_is_still_resolved_on_read(db_session):
    item = _item(db_session, "LEGACY-REF")
    plan = ProductionPlanHeader(
        name="август", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        status="fixed", created_by="test",
    )
    db_session.add(plan)
    db_session.flush()
    run = PlanningRun(
        status="FIXED_SNAPSHOT", source_plan_id=plan.id,
        period_from=plan.period_from, period_to=plan.period_to,
        ledger_generation_id=int(db_session.info["generation_id"]),
    )
    db_session.add(run)
    db_session.flush()
    requirement = MrpRequirement(
        run_id=run.run_id, item_id=item.item_id, total_required_qty=4,
        net_required_qty=4, period_from=plan.period_from, period_to=plan.period_to,
        bom_level=0,
    )
    db_session.add(requirement)
    db_session.flush()
    db_session.add(PlannedOrder(
        run_id=run.run_id, item_id=item.item_id, qty=4, planned_qty=4,
        requested_qty=4, need_date=date(2026, 8, 7), bucket_date=date(2026, 8, 7),
        demand_ref=f"req:{int(requirement.id)}",
        ledger_generation_id=int(db_session.info["generation_id"]),
    ))
    db_session.commit()

    links, _ordered = period_plan_service._execution_obligation_links(
        db_session, run,
        requirement_ids=[int(requirement.id)],
        requirement_id_by_item={int(item.item_id): int(requirement.id)},
    )

    assert [link["type"] for link in links[int(requirement.id)]] == ["planned_order"]


# ---------------------------------------------------------------------------
# execution channels report facts, not constants
# ---------------------------------------------------------------------------

def test_requirement_without_a_reservation_is_unavailable_not_zero(db_session):
    """No reservation in this generation ⇒ the fact is unknown, not 0%."""
    item = _item(db_session, "NO-RESERVATION", method="Покупка")
    plan = ProductionPlanHeader(
        name="август", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        status="fixed", created_by="test",
    )
    db_session.add(plan)
    db_session.flush()
    run = PlanningRun(
        status="FIXED_SNAPSHOT", source_plan_id=plan.id,
        period_from=plan.period_from, period_to=plan.period_to,
        ledger_generation_id=int(db_session.info["generation_id"]),
    )
    db_session.add(run)
    db_session.flush()
    requirement = MrpRequirement(
        run_id=run.run_id, item_id=item.item_id, total_required_qty=9,
        net_required_qty=9, period_from=plan.period_from, period_to=plan.period_to,
        bom_level=0,
    )
    db_session.add(requirement)
    db_session.commit()

    rows, meta = _build_execution_snapshot_rows(
        db_session, run,
        requirement_ids=[int(requirement.id)],
        items_by_requirement={
            int(requirement.id): {
                "item_id": int(item.item_id), "item_code": item.item_code,
                "item_name": item.item_name, "item_article": None,
                "gross_required_qty": 9.0, "net_required_qty": 9.0, "bom_level": 0,
            },
        },
        generation_id=int(db_session.info["generation_id"]),
        root_item_ids_by_item={},
    )

    row = rows[0]
    assert row["execution_available"] is False
    assert row["execution_unavailable_reason"]
    assert row["completed_qty"] is None
    assert row["coverage_pct"] is None
    assert row["status"] == "execution_unavailable"
    # The build metadata reports the real generation, not a hardcoded string.
    assert meta["truth_status"] == "accepted"
    assert meta["allocation_rows"] == 0
    assert meta["execution_by_requirement"]["execution_pct"] is None
    assert meta["execution_by_requirement"]["execution_partial"] is True


def test_empty_selection_reports_no_percent_instead_of_a_full_bar(db_session):
    assert _execution_row_summary([])["execution_pct"] is None
    assert _execution_row_summary([])["execution_confirmed_pct"] is None
    # A single fully stock-covered (net-zero) requirement has no base either.
    net_zero = _execution_row_summary([{
        "flow": "buy", "status": "net_zero", "execution_available": True,
        "completed_qty": 0.0, "progress_base_qty": 0.0, "net_qty": 0.0,
    }])
    assert net_zero["execution_pct"] is None
    assert net_zero["execution_by_flow"]["buy"]["execution_pct"] is None

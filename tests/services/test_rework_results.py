import datetime

from app.models import Item, Unit, Specification, PlanningRun, PlannedRework
from app.services.planning_service import get_run_rework, get_run_rework_grouped, get_run_summary


def _mk_run(db, snapshot=None) -> PlanningRun:
    run = PlanningRun(
        status="SUCCESS",
        started_by="test",
        horizon_days=10,
        pinned=False,
        config_version_id=None,
        config_snapshot=snapshot or {},
        warnings=[],
        kpi={},
        started_at=datetime.datetime.utcnow(),
        finished_at=datetime.datetime.utcnow(),
    )
    db.add(run)
    db.flush()
    return run


def test_get_run_rework_returns_rows_with_spec_and_shortage_fields(db_session):
    db = db_session

    unit = Unit(unit_ref1c="u-rw-res", unit_name="шт", short_name="шт", precision=0)
    item = Item(
        item_code="RW-RES-1",
        item_name="Rework Result 1",
        item_article="RW-RES-1",
        replenishment_method="Переработка",
        unit="u-rw-res",
        stock_qty=0,
        status="active",
    )
    spec = Specification(spec_code="SPEC-RW-1", spec_name="Spec RW 1", spec_ref1c="spec-rw-1")
    db.add_all([unit, item, spec])
    db.flush()

    run = _mk_run(db)
    db.add(
        PlannedRework(
            run_id=run.run_id,
            item_id=item.item_id,
            spec_id=spec.spec_id,
            requested_qty=7.0,
            planned_qty=5.0,
            qty=5.0,
            need_date=datetime.date(2025, 1, 10),
            order_date=datetime.date(2025, 1, 8),
            lead_time_days=2,
            priority_index=None,
            bucket_date=datetime.date(2025, 1, 10),
            component_limit=5.0,
            component_blocked=False,
            component_partial=True,
            shortage={"planned_qty": 5.0, "component_limit": 5.0},
        )
    )
    db.commit()

    result = get_run_rework(db=db, run_id=run.run_id)

    assert result["total"] == 1
    row = result["rows"][0]
    assert row["item_name"] == "Rework Result 1"
    assert row["spec_code"] == "SPEC-RW-1"
    assert row["qty"] == 5.0
    assert row["requested_qty"] == 7.0
    assert row["planned_qty"] == 5.0
    assert row["component_partial"] is True
    assert row["shortage"]["component_limit"] == 5.0


def test_get_run_rework_grouped_returns_fallback_group_without_item_category_link(db_session):
    db = db_session

    unit = Unit(unit_ref1c="u-rw-group", unit_name="шт", short_name="шт", precision=0)
    item1 = Item(
        item_code="RW-G-1",
        item_name="Rework Group 1",
        item_article="RW-G-1",
        replenishment_method="Переработка",
        unit="u-rw-group",
        stock_qty=0,
        status="active",
    )
    item2 = Item(
        item_code="RW-G-2",
        item_name="Rework Group 2",
        item_article="RW-G-2",
        replenishment_method="Переработка",
        unit="u-rw-group",
        stock_qty=0,
        status="active",
    )
    db.add_all([unit, item1, item2])
    db.flush()

    run = _mk_run(db)
    db.add_all(
        [
            PlannedRework(
                run_id=run.run_id,
                item_id=item1.item_id,
                spec_id=None,
                requested_qty=4.0,
                planned_qty=4.0,
                qty=4.0,
                need_date=datetime.date(2025, 1, 10),
                order_date=datetime.date(2025, 1, 9),
                lead_time_days=1,
                priority_index=None,
                bucket_date=datetime.date(2025, 1, 10),
                component_limit=4.0,
                component_blocked=False,
                component_partial=False,
                shortage={"planned_qty": 4.0},
            ),
            PlannedRework(
                run_id=run.run_id,
                item_id=item2.item_id,
                spec_id=None,
                requested_qty=6.0,
                planned_qty=0.0,
                qty=0.0,
                need_date=datetime.date(2025, 1, 11),
                order_date=datetime.date(2025, 1, 10),
                lead_time_days=1,
                priority_index=None,
                bucket_date=datetime.date(2025, 1, 11),
                component_limit=0.0,
                component_blocked=True,
                component_partial=False,
                shortage={"planned_qty": 0.0},
            ),
        ]
    )
    db.commit()

    result = get_run_rework_grouped(db=db, run_id=run.run_id)

    assert result["total_groups"] == 1
    assert result["total_orders"] == 2
    group = result["groups"][0]
    assert group["group_id"] is None
    assert group["group_name"] == "Без товарной группы"
    assert group["sum_qty"] == 4.0
    assert group["sum_requested_qty"] == 10.0
    assert group["sum_planned_qty"] == 4.0
    assert group["blocked_orders"] == 1
    assert group["partial_orders"] == 0


def test_get_run_summary_exposes_rework_count(db_session):
    db = db_session

    unit = Unit(unit_ref1c="u-rw-summary", unit_name="шт", short_name="шт", precision=0)
    item = Item(
        item_code="RW-SUM-1",
        item_name="Rework Summary 1",
        item_article="RW-SUM-1",
        replenishment_method="Переработка",
        unit="u-rw-summary",
        stock_qty=0,
        status="active",
    )
    db.add_all([unit, item])
    db.flush()

    run = _mk_run(db)
    db.add(
        PlannedRework(
            run_id=run.run_id,
            item_id=item.item_id,
            spec_id=None,
            requested_qty=3.0,
            planned_qty=3.0,
            qty=3.0,
            need_date=datetime.date(2025, 1, 10),
            order_date=datetime.date(2025, 1, 9),
            lead_time_days=1,
            priority_index=None,
            bucket_date=datetime.date(2025, 1, 10),
            component_limit=3.0,
            component_blocked=False,
            component_partial=False,
            shortage={"planned_qty": 3.0},
        )
    )
    db.commit()

    summary = get_run_summary(db=db, run_id=run.run_id)
    assert summary["counts"]["rework_requests"] == 1

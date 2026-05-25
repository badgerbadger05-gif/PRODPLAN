import datetime
from types import SimpleNamespace
from typing import List, Dict, Any

import pytest

from backend.app.services.planning_service import (
    get_run_production,
    get_run_purchases,
    get_run_summary,
)


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def outerjoin(self, *args, **kwargs):
        return self

    def add_columns(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows

    def count(self):
        return len(self._rows)

    def offset(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self


class FakeSession:
    def __init__(self, joins: Dict[Any, List[Any]]):
        self.joins = joins

    def query(self, *entities):
        key = tuple(entities)
        rows = self.joins.get(key, [])
        return FakeQuery(rows)


def _po(**kwargs):
    defaults = dict(
        order_id=1,
        item_id=100,
        qty=10,
        start_date=datetime.datetime(2025, 1, 2),
        finish_date=None,
        need_date=datetime.date(2025, 1, 10),
        route_ref="r",
        priority_index=1,
        bucket_type="daily",
        bucket_date=datetime.date(2025, 1, 2),
        demand_ref=None,
        demand_date=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _pos(**kwargs):
    defaults = dict(
        run_id=1,
        order_id=1,
        stage_id=1,
        area_id=2,
        bucket_type="daily",
        bucket_date=datetime.date(2025, 1, 2),
        hours=1,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_get_run_production_handles_missing_start_date():
    po_main = _po(order_id=10, item_id=2326, start_date=None, finish_date=datetime.date(2025, 1, 5))
    item_row = (po_main, "Item", "ART", "guid", "шт", "ШТ", "PCE")

    def fake_query_planned_order(*args):
        if args and getattr(args[0], "__name__", "") == "PlannedOrder":
            return FakeQuery([item_row])
        return FakeQuery([])

    fake_session = FakeSession({})
    fake_session.query = fake_query_planned_order

    result = get_run_production(fake_session, run_id=1)
    assert result["rows"], "Должна вернуться хотя бы одна строка"
    assert result["rows"][0]["start_date"] == "2025-01-05"
    assert result["rows"][0]["norm_hours_total"] >= 0


def test_get_run_purchases_handles_missing_columns():
    purchase_row = (
        1,                           # purchase_id
        200,                         # item_id
        5,                           # qty
        datetime.date(2025, 1, 10),  # need_date
        datetime.date(2025, 1, 2),   # order_date
        7,                           # lead_time_days
        0.5,                         # priority_index
        datetime.date(2025, 1, 2),   # bucket_date
        "SUP",                       # supplier_ref1c
        5,                           # requested_qty
        None,                        # item_name
        None,                        # item_article
        "guid",                      # unit (ref)
        None,                        # short_name
        None,                        # unit_name
        None,                        # unit_code
    )

    def fake_query_planned_purchase(*args):
        return FakeQuery([purchase_row])

    fake_session = FakeSession({})
    fake_session.query = fake_query_planned_purchase

    result = get_run_purchases(fake_session, run_id=1)
    assert result["rows"], "Должна вернуться хотя бы одна запись закупки"
    row = result["rows"][0]
    assert row["qty"] == 5
    assert row["item_id"] == 200
    assert row["unit"] == "guid"


def test_get_run_summary_keeps_production_warning_contract(db_session):
    from backend.app.models import PlanningRun

    db = db_session
    run = PlanningRun(
        status="SUCCESS",
        started_by="test",
        horizon_days=10,
        pinned=False,
        config_version_id=None,
        config_snapshot={},
        warnings=[
            {"code": "PRODUCTION_KIND_NOT_FOUND", "item_id": 10},
            {"code": "NO_AREA_FOR_PRODUCTION_KIND", "item_id": 10},
            {"code": "COMPONENT_SHORTAGE_BLOCKED", "item_id": 11},
            {"code": "COMPONENT_SHORTAGE_PARTIAL", "item_id": 12},
        ],
        kpi={},
        started_at=datetime.datetime.utcnow(),
        finished_at=datetime.datetime.utcnow(),
    )
    db.add(run)
    db.commit()

    result = get_run_summary(db=db, run_id=run.run_id)

    assert result["counts"]["production_orders"] == 0
    assert result["counts"]["purchase_requests"] == 0
    assert result["counts"]["rework_requests"] == 0
    assert result["componentShortages"]["blocked"] == 1
    assert result["componentShortages"]["partial"] == 1
    assert result["kindIssues"]["total"] == 2
    assert result["kindIssues"]["byCode"]["NO_PRODUCTION_KIND"] == 1
    assert result["kindIssues"]["byCode"]["NO_AREA_FOR_PRODUCTION_KIND"] == 1
    codes = [warning["code"] for warning in result["warnings"]]
    assert "NO_PRODUCTION_KIND" in codes
    assert "NO_AREA_FOR_PRODUCTION_KIND" in codes
    assert "COMPONENT_SHORTAGE_BLOCKED" in codes
    assert "COMPONENT_SHORTAGE_PARTIAL" in codes

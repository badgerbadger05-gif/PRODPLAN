from __future__ import annotations
from types import SimpleNamespace
from datetime import date, timedelta
import os
import sys
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.app.services.capacity_scheduler import CapacityScheduler


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, resources):
        self._resources = resources

    def query(self, *entities):
        try:
            first = entities[0]
            name = getattr(first, "__name__", str(first))
        except Exception:
            name = ""
        if "ProductionResource" in name:
            return FakeQuery(self._resources)
        else:
            return FakeQuery([])


def _resource(resource_id=7, daily_work_hours=8.0, capacity=1.0):
    return SimpleNamespace(resource_id=resource_id, daily_work_hours=daily_work_hours, capacity=capacity)


def test_limit_qty_by_capacity_ignores_stage_area_without_kind_mapping():
    db = FakeSession([_resource(7, 8.0, 1.0)])
    cfg = {"capacity": {"use_resource_calendars": False}}
    sched = CapacityScheduler(db, cfg)

    d0 = date.today()
    need = d0 + timedelta(days=3)

    requested_qty = 100.0
    stage_hours = {1: requested_qty * 2.0}  # NPU = 2.0
    stage_areas = {1: 7}

    limited_qty, limited_stage_hours, warnings = sched.limit_qty_by_capacity(
        item_id=123, requested_qty=requested_qty, need_date=need, stage_hours=stage_hours, stage_areas_by_stage=stage_areas
    )

    assert limited_qty == pytest.approx(0.0, rel=1e-6)
    assert limited_stage_hours[1] == pytest.approx(0.0, rel=1e-6)
    assert warnings[0]["code"] == "CAPACITY_LIMITED"


def test_schedule_backward_ignores_stage_area_without_kind_mapping():
    db = FakeSession([_resource(7, 8.0, 1.0)])
    cfg = {"capacity": {"use_resource_calendars": False}}
    sched = CapacityScheduler(db, cfg)

    d0 = date.today()
    need = d0 + timedelta(days=1)

    stages_with_hours = {1: 12.0}
    stage_areas = {1: 7}

    result, warnings = sched.schedule_backward(
        item_id=123, qty=10.0, need_date=need, stages_with_hours=stages_with_hours, stage_areas_by_stage=stage_areas
    )
    load = sched.get_aggregated_load()
    assert load == {}
    assert warnings[0]["code"] == "CAPACITY_UNSCHEDULED"

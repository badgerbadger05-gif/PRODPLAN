from __future__ import annotations

from types import SimpleNamespace
from datetime import date, timedelta
import os
import sys
import pytest

# Ensure project root on sys.path for "backend" import
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.app.services.capacity_scheduler import CapacityScheduler  # noqa: E402


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    """
    Minimal fake session that returns provided ProductionResource rows
    when queried. Other queries return empty.
    """
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


def _resource(resource_id=1, daily_work_hours=8.0, capacity=1.0, buffer_days=0):
    return SimpleNamespace(
        resource_id=resource_id,
        resource_name=f"RES-{resource_id}",
        daily_work_hours=daily_work_hours,
        capacity=capacity,
        buffer_days=buffer_days,
    )


def test_limit_qty_by_capacity_simple_window():
    """
    Window [d0..d0+3] = 4 workdays, resource daily capacity = 8h => free_hours = 32h.
    With norm_hours_per_unit=2.0, max_qty = 16.
    """
    db = FakeSession([_resource(1, 8.0, 1.0)])
    cfg = {"capacity": {"use_resource_calendars": False}, "planning_horizon_days": 30}
    sched = CapacityScheduler(db, cfg)

    d0 = date.today()
    need = d0 + timedelta(days=3)

    requested_qty = 100.0
    norm_hours_per_unit = 2.0
    stage_hours = {1: requested_qty * norm_hours_per_unit}
    stage_areas = {1: 1}

    limited_qty, limited_stage_hours, warnings = sched.limit_qty_by_capacity(
        item_id=123,
        requested_qty=requested_qty,
        need_date=need,
        stage_hours=stage_hours,
        stage_areas_by_stage=stage_areas,
    )

    free_hours = 4 * 8.0
    assert limited_qty == pytest.approx(free_hours / norm_hours_per_unit, rel=1e-9)
    # scaled stage hours should equal free_hours
    assert limited_stage_hours[1] == pytest.approx(free_hours, rel=1e-9)
    assert all(isinstance(w, dict) for w in (warnings or []))


def test_limit_qty_by_capacity_with_existing_usage():
    """
    Existing usage on d0+2:5h and d0+3:4h.
    Free = (8 + 8 + (8-5) + (8-4)) = 23h. With norm=2 => max qty = 11.5
    """
    db = FakeSession([_resource(1, 8.0, 1.0)])
    cfg = {"capacity": {"use_resource_calendars": False}, "planning_horizon_days": 30}
    sched = CapacityScheduler(db, cfg)

    d0 = date.today()
    need = d0 + timedelta(days=3)

    # Pre-used hours on d0+2 and d0+3
    wed = d0 + timedelta(days=2)
    thu = d0 + timedelta(days=3)
    sched._capacity_usage_daily[(1, wed)] = 5.0  # type: ignore[attr-defined]
    sched._capacity_usage_daily[(1, thu)] = 4.0  # type: ignore[attr-defined]

    requested_qty = 100.0
    norm_hours_per_unit = 2.0
    stage_hours = {1: requested_qty * norm_hours_per_unit}
    stage_areas = {1: 1}

    limited_qty, limited_stage_hours, _ = sched.limit_qty_by_capacity(
        item_id=123,
        requested_qty=requested_qty,
        need_date=need,
        stage_hours=stage_hours,
        stage_areas_by_stage=stage_areas,
    )

    free_hours = 8.0 + 8.0 + (8.0 - 5.0) + (8.0 - 4.0)
    assert limited_qty == pytest.approx(free_hours / norm_hours_per_unit, rel=1e-9)
    assert limited_stage_hours[1] == pytest.approx(free_hours, rel=1e-9)


def test_schedule_backward_allocates_greedily():
    """
    total_hours=20h, window [d0..d0+3], daily free=8h.
    Greedy backward from need date:
      need: 8h, need-1: 8h, need-2: 4h, residual 0
    """
    db = FakeSession([_resource(1, 8.0, 1.0)])
    cfg = {"capacity": {"use_resource_calendars": False}, "planning_horizon_days": 30}
    sched = CapacityScheduler(db, cfg)

    d0 = date.today()
    need = d0 + timedelta(days=3)

    stages_with_hours = {1: 20.0}
    stage_areas = {1: 1}

    result, warnings = sched.schedule_backward(
        item_id=123,
        qty=10.0,
        need_date=need,
        stages_with_hours=stages_with_hours,
        stage_areas_by_stage=stage_areas,
    )

    # Validate aggregated load equals 20h and split across 3 days (8,8,4)
    load = sched.get_aggregated_load()
    total_planned = sum(v["planned"] for v in load.values())
    assert total_planned == pytest.approx(20.0, rel=1e-9)

    # Expect three buckets with hours 8, 8, 4
    planned_by_day = {}
    for (area_id, bucket_date), info in load.items():
        if area_id == 1 and d0 <= bucket_date <= need:
            planned_by_day[bucket_date] = info["planned"]

    assert len(planned_by_day) == 3
    assert sorted(planned_by_day.values(), reverse=True) == [8.0, 8.0, 4.0]
    assert not any(w.get("code") == "CAPACITY_UNSCHEDULED" for w in (warnings or []))
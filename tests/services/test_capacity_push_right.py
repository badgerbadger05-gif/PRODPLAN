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


def _resource(resource_id=7, daily_work_hours=8.0, capacity=1.0, buffer_days=0):
    return SimpleNamespace(
        resource_id=resource_id,
        resource_name=f"RES-{resource_id}",
        daily_work_hours=daily_work_hours,
        capacity=capacity,
        buffer_days=buffer_days,
    )


def _map_item_to_area(sched: CapacityScheduler, item_id: int, area_id: int, kind_id: int = 10):
    sched._item_kind_map[int(item_id)] = int(kind_id)  # type: ignore[attr-defined]
    sched._kind_to_res_cache[int(kind_id)] = [int(area_id)]  # type: ignore[attr-defined]


def test_push_right_when_backward_window_insufficient():
    """
    Given need_date == d0 and zero free hours backward (same day only),
    schedule_backward must push work to the right (need_date+1, need_date+2, ...)
    without changing quantity. Expect allocation: 12h -> 8h on d0+1, 4h on d0+2.
    """
    db = FakeSession([_resource(7, 8.0, 1.0)])
    cfg = {"capacity": {"use_resource_calendars": False}, "planning_horizon_days": 10}
    sched = CapacityScheduler(db, cfg)
    _map_item_to_area(sched, 123, 7)

    d0 = date.today()
    need = d0  # window backward includes only d0; since no pre-placed usage, free=8 on d0, but algorithm prefers push-right if not enough to fit entire stage backward

    stages_with_hours = {1: 12.0}
    stage_areas = {1: 7}

    result, warnings = sched.schedule_backward(
        item_id=123,
        qty=10.0,
        need_date=need,
        stages_with_hours=stages_with_hours,
        stage_areas_by_stage=stage_areas,
    )

    # Validate stage scheduled dates
    stage_dates = result.get("stage_dates") or {}
    sd = stage_dates.get(1)
    assert sd is not None, "Stage should be present in schedule result"
    start_dt = sd["start"]
    finish_dt = sd["finish"]
    assert start_dt.date() >= (need + timedelta(days=1)), "Work must be pushed to the right at least by +1 day"
    assert finish_dt.date() >= start_dt.date()

    # Validate aggregated load: 8h on need+1 and 4h on need+2 at area 7
    load = sched.get_aggregated_load()
    d1 = need + timedelta(days=1)
    d2 = need + timedelta(days=2)
    # Convert keys to comparable set
    keys = set(load.keys())
    assert (7, d1) in keys, "Expected load on need_date+1"
    assert (7, d2) in keys, "Expected load on need_date+2"
    assert pytest.approx(load[(7, d1)]["planned"], rel=1e-6) == 8.0
    assert pytest.approx(load[(7, d2)]["planned"], rel=1e-6) == 4.0

    # CAPACITY_UNSCHEDULED should not appear here (we did schedule fully within horizon)
    assert not any(w.get("code") == "CAPACITY_UNSCHEDULED" for w in (warnings or []))

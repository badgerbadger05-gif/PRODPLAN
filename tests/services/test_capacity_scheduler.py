from __future__ import annotations

from types import SimpleNamespace
from datetime import date, timedelta
import os
import sys

# Ensure project root on sys.path for "backend" import
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.app.services.capacity_scheduler import CapacityScheduler  # noqa: E402


def _monday(year=2025, month=1, day=6) -> date:
    # 2025-01-06 is a Monday
    return date(year, month, day)


def _tuesday(ref: date) -> date:
    return ref + timedelta(days=1)


def _wednesday(ref: date) -> date:
    return ref + timedelta(days=2)


def _thursday(ref: date) -> date:
    return ref + timedelta(days=3)


def test_limit_qty_by_capacity_simple_window():
    """
    Window [Mon..Thu] = 4 workdays, resource daily capacity = 8h => free_hours = 32h.
    With norm_hours_per_unit=2.0, max_qty = 16.
    """
    # Fake resource: daily_work_hours=8, capacity=1.0
    res = SimpleNamespace(resource_id=1, daily_work_hours=8.0, capacity=1.0)
    res_by_id = {1: res}
    # Production kind mapping: pk=10 served by resource 1
    production_kinds_by_resource = {1: {10}}
    sched = CapacityScheduler(res_by_id=res_by_id, production_kinds_by_resource=production_kinds_by_resource, use_calendar_5_2=True)

    # Window Mon..Thu (inclusive)
    d0 = _monday()
    need = _thursday(d0)

    # No prior usage
    capacity_usage_daily = {}

    limited_qty, free_hours, workdays = sched.limit_qty_by_capacity(
        qty=100.0,
        norm_hours_per_unit=2.0,
        production_kind_id=10,
        d0=d0,
        need_date=need,
        capacity_usage_daily=capacity_usage_daily,
    )
    assert workdays == 4
    assert free_hours == 4 * 8.0
    assert limited_qty == (free_hours / 2.0)  # 16.0


def test_limit_qty_by_capacity_with_existing_usage():
    """
    If some hours already used in the window, free capacity shrinks accordingly.
    Wed used 5h, Thu used 4h => free Wed=3, Thu=4 -> total free = Mon 8 + Tue 8 + Wed 3 + Thu 4 = 23h
    With norm=2 => max qty = 11.5 -> 11.5
    """
    res = SimpleNamespace(resource_id=1, daily_work_hours=8.0, capacity=1.0)
    res_by_id = {1: res}
    production_kinds_by_resource = {1: {10}}
    sched = CapacityScheduler(res_by_id=res_by_id, production_kinds_by_resource=production_kinds_by_resource, use_calendar_5_2=True)

    d0 = _monday()
    need = _thursday(d0)

    # Pre-used hours on Wed and Thu
    wed = _wednesday(d0)
    thu = _thursday(d0)
    capacity_usage_daily = {
        (1, wed): 5.0,
        (1, thu): 4.0,
    }

    limited_qty, free_hours, workdays = sched.limit_qty_by_capacity(
        qty=100.0,
        norm_hours_per_unit=2.0,
        production_kind_id=10,
        d0=d0,
        need_date=need,
        capacity_usage_daily=capacity_usage_daily,
    )
    # Mon 8 + Tue 8 + Wed (8-5)=3 + Thu (8-4)=4 => total 23h
    assert workdays == 4
    assert free_hours == 23.0
    assert limited_qty == 23.0 / 2.0


def test_schedule_backward_allocates_greedily():
    """
    total_hours=20h, window Mon..Thu, daily free=8h.
    Greedy backward from Thu:
      Thu: 8, Wed: 8, Tue: 4, residual 0
    """
    res = SimpleNamespace(resource_id=1, daily_work_hours=8.0, capacity=1.0)
    res_by_id = {1: res}
    production_kinds_by_resource = {1: {10}}
    sched = CapacityScheduler(res_by_id=res_by_id, production_kinds_by_resource=production_kinds_by_resource, use_calendar_5_2=True)

    d0 = _monday()
    need = _thursday(d0)

    capacity_usage_daily = {}

    slices, residual = sched.schedule_backward(
        total_hours=20.0,
        production_kind_id=10,
        d0=d0,
        need_date=need,
        capacity_usage_daily=capacity_usage_daily,
    )

    # Validate slices content
    total_alloc = sum(h for (_, _, h) in slices)
    assert abs(total_alloc - 20.0) < 1e-9
    # Expect three days used: Thu 8, Wed 8, Tue 4 (order of slices is from need_date backward)
    assert len(slices) == 3
    # Check last slice is on Tuesday with 4h
    last_area, last_day, last_hours = slices[-1]
    assert last_day == _tuesday(d0)
    assert abs(last_hours - 4.0) < 1e-9
    assert residual == 0.0
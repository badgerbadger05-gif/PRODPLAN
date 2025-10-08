from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional, Set, Tuple, Any


class CapacityScheduler:
    """
    Capacity-aware helper:
    - limit_qty_by_capacity: caps qty by free capacity in window [d0..need_date] across candidate resources
    - schedule_backward: allocates norm-hours by days and areas without exceeding free capacity

    Assumptions:
    - 5/2 calendar (Mon..Fri workdays)
    - available hours per resource-day = daily_work_hours * capacity
    - capacity_usage_daily is a shared accumulator: Dict[(area_id, date), float]
    """

    def __init__(
        self,
        res_by_id: Dict[int, Any],
        production_kinds_by_resource: Dict[int, Set[int]],
        use_calendar_5_2: bool = True,
    ) -> None:
        self.res_by_id = res_by_id or {}
        self.production_kinds_by_resource = production_kinds_by_resource or {}
        self.use_calendar_5_2 = bool(use_calendar_5_2)

    # -------- Public API --------

    def limit_qty_by_capacity(
        self,
        qty: float,
        norm_hours_per_unit: float,
        production_kind_id: Optional[int],
        d0: date,
        need_date: date,
        capacity_usage_daily: Dict[Tuple[int, date], float],
    ) -> Tuple[float, float, int]:
        """
        Returns (limited_qty, available_hours_window, workdays_in_window).
        limited_qty = min(qty, available_hours_window / norm_hours_per_unit) when pk defined and norm>0
        """
        try:
            q = float(qty or 0.0)
        except Exception:
            q = 0.0
        nh = float(norm_hours_per_unit or 0.0)
        if q <= 0.0 or nh <= 0.0 or production_kind_id is None:
            return float(q), 0.0, 0

        # window free hours
        free_hours, workdays = self._sum_free_hours_window(
            production_kind_id=production_kind_id,
            d0=d0,
            need_date=need_date,
            capacity_usage_daily=capacity_usage_daily,
        )
        if free_hours <= 0.0:
            return 0.0, 0.0, workdays

        max_qty = float(free_hours) / float(nh) if nh > 0 else 0.0
        if max_qty < 0.0:
            max_qty = 0.0
        limited = min(float(q), float(max_qty))
        return float(limited), float(free_hours), int(workdays)

    def schedule_backward(
        self,
        total_hours: float,
        production_kind_id: Optional[int],
        d0: date,
        need_date: date,
        capacity_usage_daily: Dict[Tuple[int, date], float],
    ) -> Tuple[List[Tuple[int, date, float]], float]:
        """
        Greedy backward scheduling by date, picking area with maximum free capacity for this day.
        Returns (slices, residual_hours_not_scheduled).
        Each slice = (area_id, day, hours).
        """
        remaining = float(total_hours or 0.0)
        if remaining <= 1e-9 or production_kind_id is None:
            return [], float(remaining if remaining > 0 else 0.0)

        slices: List[Tuple[int, date, float]] = []
        cur = need_date
        while remaining > 1e-6 and cur >= d0:
            if not self._is_workday(cur):
                cur = cur - timedelta(days=1)
                continue

            sel_area_id, free = self._pick_area_for_day(production_kind_id, cur, capacity_usage_daily)
            if sel_area_id is None or free <= 1e-9:
                cur = cur - timedelta(days=1)
                continue

            place = min(remaining, free)
            if place <= 0.0:
                cur = cur - timedelta(days=1)
                continue

            slices.append((int(sel_area_id), cur, float(place)))
            capacity_usage_daily[(int(sel_area_id), cur)] = float(capacity_usage_daily.get((int(sel_area_id), cur), 0.0)) + float(place)
            remaining -= float(place)

            if remaining > 1e-6:
                cur = cur - timedelta(days=1)

        return slices, float(remaining if remaining > 0 else 0.0)

    # -------- Internals --------

    def _is_workday(self, d: date) -> bool:
        if not self.use_calendar_5_2:
            return True
        return d.weekday() <= 4  # Mon..Fri

    def _day_available_hours(self, area_id: int, d: date) -> float:
        if not self._is_workday(d):
            return 0.0
        res = self.res_by_id.get(int(area_id))
        if res is None:
            return 0.0
        try:
            daily_hours = float(getattr(res, "daily_work_hours", 8.0) or 8.0)
        except Exception:
            daily_hours = 8.0
        try:
            power_coeff = float(getattr(res, "capacity", 1.0) or 1.0)
        except Exception:
            power_coeff = 1.0
        return float(daily_hours) * float(power_coeff)

    def _candidate_areas(self, production_kind_id: int) -> List[int]:
        return [rid for rid, pkset in self.production_kinds_by_resource.items() if production_kind_id in pkset]

    def _pick_area_for_day(
        self,
        production_kind_id: int,
        d: date,
        capacity_usage_daily: Dict[Tuple[int, date], float],
    ) -> Tuple[Optional[int], float]:
        candidates = self._candidate_areas(int(production_kind_id))
        if not candidates:
            return None, 0.0

        best_rid: Optional[int] = None
        best_free: float = -1.0
        for rid in candidates:
            avail = self._day_available_hours(int(rid), d)
            used = float(capacity_usage_daily.get((int(rid), d), 0.0))
            free = max(0.0, float(avail) - float(used))
            if free > best_free:
                best_free = float(free)
                best_rid = int(rid)
        if best_rid is None:
            return None, 0.0
        return int(best_rid), float(best_free)

    def _sum_free_hours_window(
        self,
        production_kind_id: int,
        d0: date,
        need_date: date,
        capacity_usage_daily: Dict[Tuple[int, date], float],
    ) -> Tuple[float, int]:
        total_free = 0.0
        workdays = 0
        cur = d0
        while cur <= need_date:
            if self._is_workday(cur):
                workdays += 1
                # Sum free across all candidate areas
                day_free = 0.0
                for rid in self._candidate_areas(int(production_kind_id)):
                    avail = self._day_available_hours(int(rid), cur)
                    used = float(capacity_usage_daily.get((int(rid), cur), 0.0))
                    day_free += max(0.0, float(avail) - float(used))
                total_free += float(day_free)
            cur = cur + timedelta(days=1)
        return float(total_free), int(workdays)
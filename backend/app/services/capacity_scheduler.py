from __future__ import annotations

from datetime import date, timedelta, datetime
from typing import Dict, List, Optional, Set, Tuple, Any, DefaultDict
from collections import defaultdict

from sqlalchemy.orm import Session

from ..models import ProductionResource, ResourceProductionKind, PlannedOrderStage, DefaultSpecification, Specification


class CapacityScheduler:
    """
    Stateless capacity-aware helper.
    - limit_qty_by_capacity: caps qty by free capacity in a window.
    - schedule_backward: allocates norm-hours by days and areas.

    Assumes it is instantiated once per planning run and holds state for that run.
    """

    def __init__(self, db: Session, config: Dict[str, Any]):
        self._db = db
        self._config = config
        self._use_calendars = config.get("capacity", {}).get("use_resource_calendars", True)
        self._d0 = date.today()
        try:
            self._horizon_days = int(config.get("planning_horizon_days", 90))
        except Exception:
            self._horizon_days = 90
        self._dmax = self._d0 + timedelta(days=max(1, self._horizon_days) - 1)

        # Internal state for the run
        self._capacity_usage_daily: DefaultDict[Tuple[int, date], float] = defaultdict(float)
        
        # Caches
        self._res_cache: Dict[int, ProductionResource] = {r.resource_id: r for r in db.query(ProductionResource).all()}
        
        all_rpk = db.query(ResourceProductionKind).all()
        self._kind_to_res_cache: DefaultDict[int, List[int]] = defaultdict(list)
        for rpk in all_rpk:
            self._kind_to_res_cache[rpk.production_kind_id].append(rpk.resource_id)
        
        # Map item_id -> production_kind_id via DefaultSpecification -> Specification
        try:
            self._item_kind_map: Dict[int, int] = {}
            rows = (
                db.query(DefaultSpecification.item_id, Specification.production_kind_id)
                .join(Specification, DefaultSpecification.spec_id == Specification.spec_id)
                .all()
            )
            for iid, pkid in rows:
                if pkid is not None:
                    self._item_kind_map[int(iid)] = int(pkid)
        except Exception:
            self._item_kind_map = {}

    def _is_workday(self, d: date) -> bool:
        if not self._use_calendars:
            return True
        return d.weekday() < 5  # Mon-Fri

    def _get_day_available_hours(self, area_id: int, d: date) -> float:
        if not self._is_workday(d):
            return 0.0
        
        res = self._res_cache.get(area_id)
        if not res:
            return 0.0
        
        return float(res.daily_work_hours or 8.0) * float(res.capacity or 1.0)

    def _get_candidate_areas(self, production_kind_id: int) -> List[int]:
        return self._kind_to_res_cache.get(production_kind_id, [])

    def _get_production_kind_for_item(self, item_id: int) -> Optional[int]:
        try:
            return self._item_kind_map.get(int(item_id))
        except Exception:
            return None

    def limit_qty_by_capacity(
        self,
        item_id: int,
        requested_qty: float,
        need_date: date,
        stage_hours: Dict[int, float],
        stage_areas_by_stage: Optional[Dict[int, Optional[int]]] = None,
    ) -> Tuple[float, Dict[int, float], List[Dict[str, Any]]]:
        from logging import getLogger
        logger = getLogger("prodplan.capacity")

        # Нормализуем типы к float, так как в БД значения приходят как Decimal
        requested_qty_f = float(requested_qty or 0.0)
        stage_hours_f: Dict[int, float] = {int(k): float(v or 0.0) for k, v in (stage_hours or {}).items()}

        logger.debug(
            "Capacity limit request",
            extra={
                "item_id": item_id,
                "requested_qty": requested_qty_f,
                "need_date": need_date.isoformat() if need_date else None,
                "stage_hours": stage_hours_f,
            },
        )
        warnings: List[Dict[str, Any]] = []
        if not stage_hours_f or requested_qty_f <= 1e-9:
            logger.debug(
                "Capacity limit skipped due to missing stage hours",
                extra={
                    "item_id": item_id,
                    "requested_qty": requested_qty_f,
                    "need_date": need_date.isoformat() if need_date else None,
                    "stage_hours": stage_hours_f,
                },
            )
            return requested_qty_f, stage_hours_f, warnings

        # Calculate total norm-hours per unit for all stages
        total_norm_hours_per_unit = (
            sum(stage_hours_f.values()) / requested_qty_f if requested_qty_f > 0 else 0.0
        )
        if total_norm_hours_per_unit <= 1e-9:
            return requested_qty_f, stage_hours_f, warnings

        # Sum free hours across all candidate resources for the entire window
        total_free_hours = 0.0

        # This is a simplification. A real implementation should consider stage dependencies.
        # Для лимитирования используем объединение кандидатов по виду; если их нет — fallback на area_id этапов
        all_candidate_areas: Set[int] = set()
        kind_id = self._get_production_kind_for_item(item_id)
        if kind_id is not None:
            for area_id in self._get_candidate_areas(kind_id):
                try:
                    all_candidate_areas.add(int(area_id))
                except Exception:
                    continue

        # Fallback: если по виду нет кандидатов, используем участки из самих этапов (если есть)
        if not all_candidate_areas and stage_areas_by_stage:
            try:
                for area in stage_areas_by_stage.values():
                    if area is not None:
                        all_candidate_areas.add(int(area))
            except Exception:
                # игнорируем невалидные значения
                pass

        current_date = self._d0
        while current_date <= need_date:
            if self._is_workday(current_date):
                for area_id in all_candidate_areas:
                    available = float(self._get_day_available_hours(int(area_id), current_date))
                    used = float(self._capacity_usage_daily.get((int(area_id), current_date), 0.0))
                    total_free_hours += max(0.0, available - used)
            current_date += timedelta(days=1)

        max_possible_qty = (
            total_free_hours / total_norm_hours_per_unit if total_norm_hours_per_unit > 0 else 0.0
        )

        limited_qty_f = min(requested_qty_f, max_possible_qty)

        if limited_qty_f < requested_qty_f:
            warnings.append(
                {
                    "code": "CAPACITY_LIMITED",
                    "item_id": item_id,
                    "requested_qty": requested_qty_f,
                    "limited_qty": limited_qty_f,
                }
            )

        # Scale stage hours based on the new limited quantity
        scaling_factor = limited_qty_f / requested_qty_f if requested_qty_f > 0 else 0.0
        limited_stages_hours_f: Dict[int, float] = {
            int(stage_id): float(hours) * scaling_factor for stage_id, hours in stage_hours_f.items()
        }

        return limited_qty_f, limited_stages_hours_f, warnings

    def schedule_backward(
        self,
        item_id: int,
        qty: float,
        need_date: date,
        stages_with_hours: Dict[int, float],
        stage_areas_by_stage: Optional[Dict[int, Optional[int]]] = None,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Capacity-aware scheduling with push-right fallback:
        1) Try to place each stage entirely by backward allocation up to d0.
        2) If not enough capacity, place the whole stage to the right starting from need_date+1 up to dmax.
        3) Do NOT change qty; only dates and internal capacity usage are affected.
        4) If cannot fully place by dmax, emit CAPACITY_UNSCHEDULED and keep allocated part.
        """
        warnings: List[Dict[str, Any]] = []
        stage_dates: Dict[int, Dict[str, datetime]] = {}

        effective_need_date = max(need_date, self._d0)
        current_need_dt = datetime.combine(effective_need_date, datetime.min.time())

        # Normalize inputs
        try:
            stages_with_hours_f: Dict[int, float] = {
                int(sid): float(h or 0.0) for sid, h in (stages_with_hours or {}).items()
            }
        except Exception:
            stages_with_hours_f = {}

        stage_areas_norm: Dict[int, Optional[int]] = {}
        if stage_areas_by_stage:
            try:
                for sid, area in stage_areas_by_stage.items():
                    stage_areas_norm[int(sid)] = (int(area) if area is not None else None)
            except Exception:
                stage_areas_norm = dict(stage_areas_by_stage)

        kind_id = self._get_production_kind_for_item(item_id)

        def candidate_areas_for_stage(stage_id: int) -> List[int]:
            cands: List[int] = []
            if kind_id is not None:
                cands = list(self._get_candidate_areas(kind_id)) or []
            if (not cands) and (stage_id in stage_areas_norm) and (stage_areas_norm[stage_id] is not None):
                cands = [int(stage_areas_norm[stage_id])]
            return cands

        def best_free_for_day(day: date, area_ids: List[int]) -> Tuple[int, float]:
            best_area, max_free = -1, 0.0
            for aid in area_ids:
                aid_i = int(aid)
                available = float(self._get_day_available_hours(aid_i, day))
                used = float(self._capacity_usage_daily.get((aid_i, day), 0.0))
                free = max(0.0, available - used)
                if free > max_free:
                    max_free = free
                    best_area = aid_i
            return best_area, max_free

        def sum_free_backward(area_ids: List[int], finish_day: date) -> float:
            total = 0.0
            d = finish_day
            while d >= self._d0:
                if self._is_workday(d):
                    _, free = best_free_for_day(d, area_ids)
                    total += free
                d -= timedelta(days=1)
            return total

        def sum_free_forward(area_ids: List[int], start_day: date) -> float:
            total = 0.0
            d = start_day
            while d <= self._dmax:
                if self._is_workday(d):
                    _, free = best_free_for_day(d, area_ids)
                    total += free
                d += timedelta(days=1)
            return total

        def allocate_backward(hours: float, area_ids: List[int], finish_dt: datetime) -> Tuple[float, Optional[datetime], Optional[datetime]]:
            remaining = float(hours)
            last_finish_dt = finish_dt
            d = finish_dt.date()
            start_dt: Optional[datetime] = None
            while remaining > 1e-9 and d >= self._d0:
                if not self._is_workday(d):
                    d -= timedelta(days=1)
                    continue
                best_area, free = best_free_for_day(d, area_ids)
                if best_area != -1 and free > 1e-9:
                    place = float(min(remaining, free))
                    self._capacity_usage_daily[(best_area, d)] += place
                    remaining -= place
                    start_dt = datetime.combine(d, datetime.min.time())
                d -= timedelta(days=1)
            return remaining, start_dt, last_finish_dt

        def allocate_forward(hours: float, area_ids: List[int], start_dt: datetime) -> Tuple[float, Optional[datetime], Optional[datetime]]:
            remaining = float(hours)
            d = max((start_dt + timedelta(days=1)).date(), self._d0)
            first_start_dt: Optional[datetime] = None
            last_finish_dt: Optional[datetime] = None
            while remaining > 1e-9 and d <= self._dmax:
                if not self._is_workday(d):
                    d += timedelta(days=1)
                    continue
                best_area, free = best_free_for_day(d, area_ids)
                if best_area != -1 and free > 1e-9:
                    place = float(min(remaining, free))
                    self._capacity_usage_daily[(best_area, d)] += place
                    remaining -= place
                    if first_start_dt is None:
                        first_start_dt = datetime.combine(d, datetime.min.time())
                    last_finish_dt = datetime.combine(d, datetime.min.time())
                d += timedelta(days=1)
            return remaining, first_start_dt, last_finish_dt

        # Process stages in reverse (downstream first), keeping precedence via current_need_dt
        for stage_id, total_hours in sorted(stages_with_hours_f.items(), key=lambda x: x[0], reverse=True):
            if total_hours <= 1e-9:
                continue

            cands = candidate_areas_for_stage(int(stage_id))
            if not cands:
                # Nothing we can schedule without area; record unscheduled
                stage_dates[int(stage_id)] = {"start": current_need_dt, "finish": current_need_dt}
                warnings.append(
                    {
                        "code": "CAPACITY_UNSCHEDULED",
                        "item_id": int(item_id),
                        "stage_id": int(stage_id),
                        "required_hours": float(total_hours),
                        "available_hours": 0.0,
                        "need_date": current_need_dt.date().isoformat(),
                    }
                )
                continue

            # Decide strategy: backward if it can fully fit, else push-right (forward only)
            backward_free = sum_free_backward(cands, current_need_dt.date())
            if backward_free + 1e-9 >= float(total_hours):
                # place backward fully
                remaining, start_dt, finish_dt = allocate_backward(float(total_hours), cands, current_need_dt)
                # remaining should be ~0
                stage_dates[int(stage_id)] = {
                    "start": start_dt if start_dt is not None else current_need_dt,
                    "finish": finish_dt,
                }
                # Next stage must end before this stage starts
                current_need_dt = stage_dates[int(stage_id)]["start"]
            else:
                # push-right: allocate entirely to the right (do not split across before/after)
                remaining, start_dt, finish_dt = allocate_forward(float(total_hours), cands, current_need_dt)
                scheduled = float(total_hours) - float(remaining)
                if scheduled <= 1e-9:
                    # Couldn't schedule anything to the right
                    stage_dates[int(stage_id)] = {"start": current_need_dt, "finish": current_need_dt}
                    warnings.append(
                        {
                            "code": "CAPACITY_UNSCHEDULED",
                            "item_id": int(item_id),
                            "stage_id": int(stage_id),
                            "required_hours": float(total_hours),
                            "available_hours": 0.0,
                            "need_date": current_need_dt.date().isoformat(),
                        }
                    )
                else:
                    stage_dates[int(stage_id)] = {
                        "start": start_dt if start_dt is not None else current_need_dt,
                        "finish": finish_dt if finish_dt is not None else start_dt,
                    }
                    if remaining > 1e-9:
                        warnings.append(
                            {
                                "code": "CAPACITY_UNSCHEDULED",
                                "item_id": int(item_id),
                                "stage_id": int(stage_id),
                                "required_hours": float(total_hours),
                                "available_hours": float(scheduled),
                                "need_date": current_need_dt.date().isoformat(),
                            }
                        )
                # Precedence: earlier stages must finish before this starts
                current_need_dt = stage_dates[int(stage_id)]["start"]

        order_start_date = min((d["start"] for d in stage_dates.values()), default=current_need_dt)
        order_finish_date = max((d["finish"] for d in stage_dates.values()), default=current_need_dt)

        return {
            "order_start_date": order_start_date,
            "order_finish_date": order_finish_date,
            "stage_dates": stage_dates,
        }, warnings

    def get_aggregated_load(self) -> Dict[Tuple[int, date], Dict[str, float]]:
        """
        Aggregates the capacity usage into a structured format for DB insertion.
        All buckets are daily; bucket_type is no longer stored in DB.
        """
        aggregated_load = {}
        for (area_id, bucket_date), planned_hours in self._capacity_usage_daily.items():
            available_hours = self._get_day_available_hours(area_id, bucket_date)
            key = (area_id, bucket_date)
            aggregated_load[key] = {
                "planned": planned_hours,
                "available": available_hours,
            }
        return aggregated_load

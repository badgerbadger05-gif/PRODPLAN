from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session
from sqlalchemy import func

from ..models import PlannedOrder, PlannedPurchase, Item


class PriorityManager:
    """
    Управление приоритетами для заказов/закупок.
    - Для производственных заказов учитывает критичность (истощение запасов к дате потребности),
      важность и длительность цикла (через нормо-часы/lead time).
    - Для закупок приоритет строится на основе дней до потребности и длительности поставки.

    Конфигурация:
      snapshot['prioritization'] = {
        weight_criticality: float (0.4),
        weight_importance: float (0.3),
        weight_cycle_time: float (0.3),
        default_importance: float (1.0)
      }
    """

    def __init__(self, snapshot: Dict[str, Any]) -> None:
        prio_cfg = (snapshot.get("prioritization") or {}) if isinstance(snapshot, dict) else {}
        self.w_crit: float = float(prio_cfg.get("weight_criticality", 0.4) or 0.4)
        self.w_imp: float = float(prio_cfg.get("weight_importance", 0.3) or 0.3)
        self.w_cycle: float = float(prio_cfg.get("weight_cycle_time", 0.3) or 0.3)
        self.default_importance: float = float(prio_cfg.get("default_importance", 1) or 1.0)

    def compute_order_priorities(
        self,
        db: Session,
        created_orders: List[PlannedOrder],
        item_norm_cache: Dict[int, float],
        net_daily: Dict[str, Dict[str, float]],
        net_weekly: Dict[str, Dict[str, float]],
        items: List[Item],
    ) -> Dict[int, float]:
        """
        Рассчитывает priority_index для заказов на производство и возвращает словарь: order_id -> priority.
        Основано на логике из run_planning_run (предварительная сортировка заказов).
        """
        # Загружаем запасы и WIP
        stock_by_item: Dict[int, float] = {int(x.item_id): float(x.stock_qty or 0.0) for x in (items or [])}
        wip_by_item: Dict[int, float] = {}
        try:
            from ..models import ProductionProduct
            wip_rows = db.query(ProductionProduct.item_id, func.sum(ProductionProduct.quantity)).group_by(ProductionProduct.item_id).all()
            for iid, qty in wip_rows:
                try:
                    wip_by_item[int(iid)] = float(qty or 0.0)
                except Exception:
                    continue
        except Exception:
            wip_by_item = {}

        # Нормирование по макс. нормо-часам
        max_norm = max([float(v or 0.0) for v in item_norm_cache.values()] or [0.0])
        d0: date = date.today()

        order_priorities: Dict[int, float] = {}
        for o in created_orders:
            try:
                need_dt: date = o.need_date
                days_to_need = max(1, (need_dt - d0).days)
                iid = int(o.item_id)

                # Критичность: срок истощения текущего объёма (stock+WIP) относительно средней суточной потребности
                current_stock = float(stock_by_item.get(iid, 0.0) or 0.0) + float(wip_by_item.get(iid, 0.0) or 0.0)
                total_demand = 0.0
                demand_days_count = 0
                for qty_val in (net_daily.get(str(iid), {}) or {}).values():
                    total_demand += float(qty_val or 0.0)
                    demand_days_count += 1
                for qty_val in (net_weekly.get(str(iid), {}) or {}).values():
                    total_demand += float(qty_val or 0.0)
                    demand_days_count += 1

                avg_daily_consumption = total_demand / max(1, demand_days_count) if demand_days_count > 0 else 0.0
                time_to_deplete = current_stock / avg_daily_consumption if avg_daily_consumption > 0.0 else float("inf")

                if time_to_deplete > 0 and time_to_deplete != float("inf"):
                    criticality_coeff = float(days_to_need) / time_to_deplete
                    if criticality_coeff < 1.0:
                        criticality = 10.0
                    elif criticality_coeff < 1.2:
                        criticality = 5.0
                    elif criticality_coeff < 1.5:
                        criticality = 2.0
                    else:
                        criticality = 1.0
                else:
                    criticality = 0.5

                # Длительность цикла нормируем по макс. норме
                nh = float(item_norm_cache.get(iid, 0.0) or 0.0)
                cycle_norm = (nh / max_norm) if max_norm > 1e-12 else 0.0

                prio = self.w_crit * criticality + self.w_imp * self.default_importance + self.w_cycle * cycle_norm
                order_priorities[int(o.order_id)] = float(prio)
            except Exception:
                # Fail safe: если что-то пошло не так, присвоим минимальный приоритет
                order_priorities[int(o.order_id)] = 0.0

        return order_priorities

    def assign_purchase_priorities(self, created_purchases: List[PlannedPurchase]) -> None:
        """
        Устанавливает priority_index для заявок на закупку по упрощённой формуле
        (чем меньше дней до потребности, тем выше критичность; плюс нормированная длительность поставки).
        """
        if not created_purchases:
            return

        max_lt = max([int(p.lead_time_days or 0) for p in created_purchases] or [1])
        d0: date = date.today()

        for p in created_purchases:
            try:
                need_dt: date = p.need_date
                days_to_need = max(1, (need_dt - d0).days)
                criticality = 2.0 / float(days_to_need)
                cycle_norm = float(int(p.lead_time_days or 0)) / float(max_lt) if max_lt > 0 else 0.0
                prio = self.w_crit * criticality + self.w_imp * self.default_importance + self.w_cycle * cycle_norm
                p.priority_index = float(prio)
            except Exception:
                p.priority_index = 0.0
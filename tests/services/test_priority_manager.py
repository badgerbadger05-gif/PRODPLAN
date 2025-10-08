from __future__ import annotations

from types import SimpleNamespace
from datetime import date, timedelta
import os
import sys

# Ensure project root on sys.path for "backend" import
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.app.services.priority_manager import PriorityManager  # noqa: E402


def _fake_db():
    # db is only used for optional WIP query inside try/except; returning an object without query will cause
    # an exception and fallback to empty WIP, which is acceptable for these tests.
    return None


def test_priority_ordering_by_criticality_and_cycle_time():
    """
    Проверяем относительный порядок приоритетов:
    - order_A: высокий критический индекс (criticality=10) и большой norm_hours -> высокий приоритет
    - order_B: минимальная критичность (criticality=0.5) и маленький norm_hours -> низкий приоритет
    """
    snapshot = {
        "prioritization": {
            "weight_criticality": 0.4,
            "weight_importance": 0.3,
            "weight_cycle_time": 0.3,
            "default_importance": 1.0,
        }
    }
    pm = PriorityManager(snapshot)

    today = date.today()
    order_A = SimpleNamespace(order_id=1, item_id=101, need_date=today + timedelta(days=2))
    order_B = SimpleNamespace(order_id=2, item_id=102, need_date=today + timedelta(days=10))
    created_orders = [order_A, order_B]

    # net buckets: средний расход по 101 достаточно высокий, а stock большой => time_to_deplete большой => coeff маленький => criticality=10
    net_daily = {
        str(101): {today.isoformat(): 10.0, (today + timedelta(days=1)).isoformat(): 10.0},  # avg ~10
        str(102): {today.isoformat(): 0.0},  # практически нет спроса
    }
    net_weekly = {}

    # Нормо-часы: A больше B
    item_norm_cache = {101: 100.0, 102: 1.0}

    # Запасы для критичности:
    # 101: большой stock => time_to_deplete = (stock+WIP)/avg ≫ days_to_need -> criticality=10
    # 102: нулевой stock и нет спроса -> ветка "else" => criticality=0.5
    items = [
        SimpleNamespace(item_id=101, stock_qty=1000.0),
        SimpleNamespace(item_id=102, stock_qty=0.0),
    ]

    prios = pm.compute_order_priorities(
        db=_fake_db(),
        created_orders=created_orders,
        item_norm_cache=item_norm_cache,
        net_daily=net_daily,
        net_weekly=net_weekly,
        items=items,
    )

    assert prios[1] > prios[2], "Order with higher criticality and cycle time must get higher priority"
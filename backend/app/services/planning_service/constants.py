from __future__ import annotations

import re
import logging
from typing import Any, Dict

from ..supplier_order_status import (
    STATE_TO_PHASE,
    NETTING_PHASES,
)

logger = logging.getLogger("prodplan.planning")


_REF1C_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


# Default planning config fallback (aligned with Alembic seed)
DEFAULT_PLANNING_CONFIG: Dict[str, Any] = {
    "planning_horizon_days": 90,
    "mps_daily_horizon_days": 90,
    "weekly": {"enabled": True, "anchor_day": "Monday", "need_date_day": "Friday"},
    "procurement": {
        "default_lead_time_days": 30,
        "lead_time_min_policy": "max(default_lead_time_days, lead_time_from_item)",
        "lot_sizing": {"moq_source": "item_card_or_1", "multiple": 1, "rounding": "ceil"},
        "order_date_rounding_policy": "previous_workday",
    },
    "production": {"lot_sizing": {"min_batch": 1, "multiple": 1, "rounding": "ceil"}},
    "safety_stock_percent": 1,
    "capacity": {"use_resource_calendars": True, "consider_power_coefficients": True},
    "prioritization": {
        "weight_criticality": 0.4,
        "weight_importance": 0.3,
        "weight_cycle_time": 0.3,
        "default_importance": 1,
    },
    "toggles": {"include_wip": False, "enable_weekly_route_detail": False},
}

# Pagination constants
SERVER_MAX_LIMIT = 1000
DEFAULT_PAGE_LIMIT = 50

# 1C state key for completed production orders.
DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"

# Состояния заказа поставщику, НЕ учитываемые как ожидаемое поступление в MRP.
# Производная от канонической карты фаз (см. supplier_order_status): всё, что не
# относится к фазам «в пути» / «на складе». Сохранена для обратной совместимости
# импортов в production_control_material_availability / period_plan_service.
SUPPLIER_ORDER_EXCLUDED_STATE_NAMES = {
    name for name, phase in STATE_TO_PHASE.items() if phase not in NETTING_PHASES
}

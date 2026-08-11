from __future__ import annotations

from datetime import date, datetime
import math
from typing import Any, Optional


# Единственный источник ключа состояния «Завершен» заказа на производство в 1С.
# Значение сравнивается в нижнем регистре (см. `norm_guid`). Канон запрещает
# держать вторую копию одной величины: любой потребитель импортирует её отсюда,
# а не объявляет свой локальный литерал. Модуль намеренно не импортирует ничего
# из `app`, поэтому его можно тянуть из любого слоя, включая `item_ledger`.
DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"


def norm_guid(val: Any) -> str:
    s = str(val or "").strip().lower()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    if s.startswith("guid'") and s.endswith("'"):
        s = s[len("guid'") : -1].strip()
    return s


def looks_like_guid(val: Any) -> bool:
    s = norm_guid(val)
    if len(s) != 36:
        return False
    parts = s.split("-")
    return [len(part) for part in parts] == [8, 4, 4, 4, 12] and all(
        all(ch in "0123456789abcdef" for ch in part) for part in parts
    )


def to_float(val: Any) -> float:
    try:
        return float(val or 0.0)
    except Exception:
        return 0.0


def to_float_strict(
    value: Any,
    *,
    field: str | None = None,
) -> float:
    try:
        if value is None or isinstance(value, bool):
            raise ValueError("numeric field is malformed")
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("numeric field is malformed")
        numeric = float(value)
    except (TypeError, ValueError):
        suffix = f" {field}" if field else ""
        raise ValueError(f"numeric field{suffix} is malformed") from None
    if not math.isfinite(numeric):
        suffix = f" {field}" if field else ""
        raise ValueError(f"numeric field{suffix} is non-finite")
    return numeric


def date_to_iso(val: Any) -> Optional[str]:
    if not val:
        return None
    if hasattr(val, "date") and not isinstance(val, date):
        val = val.date()
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val).split("T")[0].split(" ")[0]


def parse_date(val: Any) -> Optional[date]:
    if not val:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    try:
        return date.fromisoformat(str(val)[:10])
    except Exception:
        return None


def line_number(product: Any) -> int:
    try:
        return int(product.line_number or product.product_id or 0)
    except Exception:
        return 0

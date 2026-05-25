from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional


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

"""Date coercion helpers shared across services and routers."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional


def to_date(value: Any) -> Optional[date]:
    """Coerce a value to :class:`date`, returning ``None`` on failure/empty.

    Accepts ``None``, :class:`datetime`, :class:`date`, or an ISO-ish string
    (only the first 10 chars are parsed). Consolidated from the copy in
    ``purchase_control_journal``.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_date(value: Any) -> Optional[date]:
    """Parse a value to :class:`date`, returning ``None`` on failure/empty.

    Like :func:`to_date` but also treats an empty string as ``None`` and
    swallows any parse error. Consolidated from the equivalent copies in
    ``paint_weld_chain`` and ``purchase_control_journal``.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None

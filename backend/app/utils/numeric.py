"""Numeric coercion helpers shared across services and routers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def to_float(val: Any, default: float = 0.0) -> float:
    """Coerce an arbitrary value to ``float``.

    ``None`` and any unparsable value fall back to ``default``. Numeric strings
    (including locale/decimal edge cases) are parsed via :class:`Decimal` for
    robustness. Consolidated from ~9 identical/equivalent local copies.
    """
    try:
        if val is None:
            return float(default)
        if isinstance(val, (int, float)):
            return float(val)
        return float(Decimal(str(val)))
    except (InvalidOperation, ValueError, TypeError):
        return float(default)

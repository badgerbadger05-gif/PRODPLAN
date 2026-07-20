"""GUID normalization helper shared across sync/export services."""

from __future__ import annotations

from typing import Any


def norm_guid(val: Any) -> str:
    """Нормализация GUID для сравнения (lowercase, без фигурных скобок, кавычек и обёрток).

    Strips ``{...}`` braces, a ``guid'...'`` wrapper, and surrounding single
    quotes. Consolidated from the identical copies in ``production_order_sync``
    and ``supplier_order_sync``.
    """
    s = str(val or "").strip().lower()
    if not s:
        return ""
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    if s.startswith("guid'") and s.endswith("'"):
        s = s[len("guid'") : -1].strip()
    if s.startswith("'") and s.endswith("'"):
        s = s[1:-1].strip()
    return s

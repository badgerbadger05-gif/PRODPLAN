from __future__ import annotations

from typing import Optional


REPLENISHMENT_FLOW_PRODUCTION = "production"
REPLENISHMENT_FLOW_PURCHASE = "purchase"
REPLENISHMENT_FLOW_REWORK = "rework"
REPLENISHMENT_FLOW_UNAVAILABLE = "unavailable"

PURCHASE_MARKERS = ("покуп", "закуп", "purchase", "buy")
REWORK_MARKERS = ("переработ", "rework")


def normalize_replenishment_method(method_raw: Optional[str]) -> str:
    """Return normalized replenishment method string for flow classification."""
    return str(method_raw or "").strip().lower()


def classify_replenishment_flow(method_raw: Optional[str]) -> str:
    """
    Classify replenishment method into an internal planning flow.

    Current behavior:
    - empty/unknown values => unavailable
    - purchase markers => purchase
    - rework markers => known, but executor-less, rework obligation
    - explicit production markers => production
    - everything else => unavailable
    """
    method = normalize_replenishment_method(method_raw)
    if not method:
        return REPLENISHMENT_FLOW_UNAVAILABLE
    if any(marker in method for marker in PURCHASE_MARKERS):
        return REPLENISHMENT_FLOW_PURCHASE
    if any(marker in method for marker in REWORK_MARKERS):
        return REPLENISHMENT_FLOW_REWORK
    if "производ" in method or "make" in method:
        return REPLENISHMENT_FLOW_PRODUCTION
    return REPLENISHMENT_FLOW_UNAVAILABLE


def is_purchase_replenishment(method_raw: Optional[str]) -> bool:
    return classify_replenishment_flow(method_raw) == REPLENISHMENT_FLOW_PURCHASE

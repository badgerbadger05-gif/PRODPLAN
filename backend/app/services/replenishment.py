from __future__ import annotations

from typing import Optional


REPLENISHMENT_FLOW_PRODUCTION = "production"
REPLENISHMENT_FLOW_PURCHASE = "purchase"
REPLENISHMENT_FLOW_REWORK = "rework"
REPLENISHMENT_FLOW_UNAVAILABLE = "unavailable"

# Closed vocabulary.  Substring matching is unsafe here: for example the 1C
# value "Не производится" contains "производ" and used to create a phantom
# make route in MRP/readiness.
PURCHASE_METHODS = frozenset(
    {"покупка", "закупка", "покупное изделие", "purchase", "buy"}
)
PRODUCTION_METHODS = frozenset({"производство", "make"})
REWORK_METHODS = frozenset({"переработка", "rework"})
NON_STOCK_ITEM_TYPES = frozenset({"услуга", "работа", "операция"})


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
    if method in PURCHASE_METHODS:
        return REPLENISHMENT_FLOW_PURCHASE
    if method in REWORK_METHODS:
        return REPLENISHMENT_FLOW_REWORK
    if method in PRODUCTION_METHODS:
        return REPLENISHMENT_FLOW_PRODUCTION
    return REPLENISHMENT_FLOW_UNAVAILABLE


def is_purchase_replenishment(method_raw: Optional[str]) -> bool:
    return classify_replenishment_flow(method_raw) == REPLENISHMENT_FLOW_PURCHASE


def is_non_stock_item_type(item_type_raw: Optional[str]) -> bool:
    """Return whether the exact 1C item type is intentionally non-stock."""

    return str(item_type_raw or "").strip().casefold() in NON_STOCK_ITEM_TYPES

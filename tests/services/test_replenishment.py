from __future__ import annotations

from app.services.replenishment import (
    REPLENISHMENT_FLOW_PRODUCTION,
    REPLENISHMENT_FLOW_PURCHASE,
    REPLENISHMENT_FLOW_REWORK,
    REPLENISHMENT_FLOW_UNAVAILABLE,
    classify_replenishment_flow,
    is_non_stock_item_type,
)


def test_classify_replenishment_flow_detects_purchase_synonyms():
    assert classify_replenishment_flow("Закупка") == REPLENISHMENT_FLOW_PURCHASE
    assert classify_replenishment_flow("покупное изделие") == REPLENISHMENT_FLOW_PURCHASE
    assert classify_replenishment_flow("Purchase") == REPLENISHMENT_FLOW_PURCHASE
    assert classify_replenishment_flow("buy") == REPLENISHMENT_FLOW_PURCHASE


def test_classify_replenishment_flow_defaults_unknown_values_to_unavailable():
    assert classify_replenishment_flow(None) == REPLENISHMENT_FLOW_UNAVAILABLE
    assert classify_replenishment_flow("") == REPLENISHMENT_FLOW_UNAVAILABLE
    assert classify_replenishment_flow("Производство") == REPLENISHMENT_FLOW_PRODUCTION
    assert classify_replenishment_flow("Переработка") == REPLENISHMENT_FLOW_REWORK
    assert classify_replenishment_flow("Не производится") == REPLENISHMENT_FLOW_UNAVAILABLE
    assert classify_replenishment_flow("unknown-method") == REPLENISHMENT_FLOW_UNAVAILABLE


def test_non_stock_item_type_uses_exact_1c_values():
    assert is_non_stock_item_type("Услуга") is True
    assert is_non_stock_item_type("Операция") is True
    assert is_non_stock_item_type("Услуга производственная") is False

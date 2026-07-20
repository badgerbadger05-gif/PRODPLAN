"""Тесты чистого ядра агрегации закупного спроса барабана.

Frappe не требуется — `aggregate_purchase_demand` работает на инъекциях.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.dbr.core.purchase_demand import aggregate_purchase_demand


def _kit(mapping):
    """Фабрика `lines_of_sku` из словаря {sku: [(item, qty_per, is_purchase)]}."""

    def lines_of_sku(sku):
        return mapping.get(sku, [])

    return lines_of_sku


def test_empty_input():
    assert aggregate_purchase_demand([], _kit({})) == {}


def test_single_slot_single_purchase_line():
    kit = _kit({"SKU": [("BOLT", 4.0, True)]})
    result = aggregate_purchase_demand([("SKU", 3, date(2026, 8, 1))], kit)
    assert result == {"BOLT": {"qty": 12.0, "earliest_need_date": date(2026, 8, 1)}}


def test_multiple_slots_same_sku_sum():
    kit = _kit({"SKU": [("BOLT", 2.0, True)]})
    result = aggregate_purchase_demand(
        [
            ("SKU", 5, date(2026, 8, 10)),
            ("SKU", 3, date(2026, 8, 1)),  # раньше → должна стать earliest
        ],
        kit,
    )
    assert result["BOLT"]["qty"] == pytest.approx(16.0)  # (5+3)*2
    assert result["BOLT"]["earliest_need_date"] == date(2026, 8, 1)


def test_shared_part_across_two_skus_min_date():
    kit = _kit(
        {
            "A": [("BOLT", 1.0, True)],
            "B": [("BOLT", 3.0, True)],
        }
    )
    result = aggregate_purchase_demand(
        [
            ("A", 10, date(2026, 9, 5)),
            ("B", 2, date(2026, 7, 20)),
        ],
        kit,
    )
    assert result["BOLT"]["qty"] == pytest.approx(16.0)  # 10*1 + 2*3
    assert result["BOLT"]["earliest_need_date"] == date(2026, 7, 20)


def test_non_purchase_lines_dropped():
    kit = _kit(
        {
            "SKU": [
                ("SUBASSY", 1.0, False),  # производимый узел — отсеиваем
                ("BOLT", 4.0, True),
                ("PLATE", 2.0, False),  # тоже не закупной
            ]
        }
    )
    result = aggregate_purchase_demand([("SKU", 5, date(2026, 8, 1))], kit)
    assert set(result) == {"BOLT"}
    assert result["BOLT"]["qty"] == pytest.approx(20.0)


def test_qty_per_unit_multiplication():
    kit = _kit({"SKU": [("WASHER", 2.5, True)]})
    result = aggregate_purchase_demand([("SKU", 4, date(2026, 8, 1))], kit)
    assert result["WASHER"]["qty"] == pytest.approx(10.0)


def test_zero_and_negative_slot_qty_skipped():
    kit = _kit({"SKU": [("BOLT", 1.0, True)]})
    result = aggregate_purchase_demand(
        [("SKU", 0, date(2026, 8, 1)), ("SKU", -5, date(2026, 8, 1))], kit
    )
    assert result == {}


def test_none_need_date_ignored_in_min():
    kit = _kit({"SKU": [("BOLT", 1.0, True)]})
    result = aggregate_purchase_demand(
        [
            ("SKU", 1, None),
            ("SKU", 1, date(2026, 8, 1)),
        ],
        kit,
    )
    assert result["BOLT"]["qty"] == pytest.approx(2.0)
    assert result["BOLT"]["earliest_need_date"] == date(2026, 8, 1)


def test_all_need_dates_none():
    kit = _kit({"SKU": [("BOLT", 1.0, True)]})
    result = aggregate_purchase_demand([("SKU", 2, None)], kit)
    assert result["BOLT"]["earliest_need_date"] is None


def test_negative_qty_per_unit_raises():
    kit = _kit({"SKU": [("BOLT", -1.0, True)]})
    with pytest.raises(ValueError):
        aggregate_purchase_demand([("SKU", 1, date(2026, 8, 1))], kit)


def test_kit_memoized_per_sku():
    calls = {"n": 0}

    def lines_of_sku(sku):
        calls["n"] += 1
        return [("BOLT", 1.0, True)]

    aggregate_purchase_demand(
        [("SKU", 1, date(2026, 8, 1)), ("SKU", 2, date(2026, 8, 2))],
        lines_of_sku,
    )
    assert calls["n"] == 1  # кит одного SKU развёрнут один раз

"""Тесты чистого ядра плановых темпов — pytest без Frappe.

Покрывают daily_rates_from_plan: обычный случай (план / рабочие дни),
ноль рабочих дней (пусто, без деления на ноль), нулевой qty (не в
результате), отрицательный qty (ValueError), пустой вход, детерминизм.

Запуск: python -m pytest prodflow/services/feeder/test_daily_rates.py -q
"""

from __future__ import annotations

import pytest

from app.services.dbr.core.feeder.adu import daily_rates_from_plan


class TestDailyRatesFromPlan:
	def test_ordinary_case(self):
		# 742 шт за 37 рабочих дней ≈ 20.05 шт/день (реальный спрос барабана).
		rates = daily_rates_from_plan({"A": 742}, workdays=37)
		assert rates["A"] == pytest.approx(742 / 37)

	def test_multiple_skus(self):
		rates = daily_rates_from_plan({"A": 100, "B": 50}, workdays=10)
		assert rates == {"A": 10.0, "B": 5.0}

	def test_zero_workdays_returns_empty(self):
		# Горизонт без рабочих дней — делить не на что, не роняем деление на ноль.
		assert daily_rates_from_plan({"A": 100}, workdays=0) == {}

	def test_negative_workdays_returns_empty(self):
		assert daily_rates_from_plan({"A": 100}, workdays=-5) == {}

	def test_zero_qty_sku_excluded(self):
		# SKU с нулевым планом буфер не нужен — в результат не попадает.
		rates = daily_rates_from_plan({"A": 0, "B": 20}, workdays=10)
		assert rates == {"B": 2.0}

	def test_none_qty_sku_excluded(self):
		rates = daily_rates_from_plan({"A": None, "B": 20}, workdays=10)
		assert rates == {"B": 2.0}

	def test_negative_qty_raises(self):
		with pytest.raises(ValueError):
			daily_rates_from_plan({"A": -1}, workdays=10)

	def test_empty_input(self):
		assert daily_rates_from_plan({}, workdays=10) == {}

	def test_deterministic(self):
		qty = {"C": 30, "A": 10, "B": 20}
		first = daily_rates_from_plan(qty, workdays=10)
		second = daily_rates_from_plan(qty, workdays=10)
		assert first == second
		# Ключи в отсортированном порядке — детерминированный обход.
		assert list(first.keys()) == ["A", "B", "C"]

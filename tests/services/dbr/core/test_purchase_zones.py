"""Тесты чистого ядра закупного буфера — pytest без Frappe.

Покрывают green_zone (выбор кванта min_order_qty vs ADU×batch_days) и
compute_purchase_zones (RT из lead_time, зелёная = мин. партия, риск на
красную, нулевые зоны при неизвестном сроке). Стиль — drum/test_rollforward.py.

Запуск: python -m pytest prodflow/services/feeder/test_purchase_zones.py -q
"""

from __future__ import annotations

import pytest

from app.services.dbr.core.feeder.zones import Zones, compute_purchase_zones, green_zone, has_shelf


class TestGreenZone:
	def test_min_order_qty_wins_when_set(self):
		# Мин. партия заказа поставщика задана — берём её, а не ADU×batch_days.
		assert green_zone(adu=10, batch_days=5, min_order_qty=200) == 200

	def test_falls_back_to_adu_times_batch(self):
		# min_order не задан — как у производимой: max(optimal, ADU×batch_days).
		assert green_zone(adu=10, batch_days=5, min_order_qty=0) == 50
		assert green_zone(adu=10, batch_days=5) == 50

	def test_optimal_batch_used_in_fallback(self):
		assert green_zone(adu=10, batch_days=2, optimal_batch=100) == 100  # 100 > 20

	def test_zero_min_order_is_ignored(self):
		# 0 — «не задано», не обнуляет квант.
		assert green_zone(adu=4, batch_days=10, min_order_qty=0) == 40

	def test_rejects_negative(self):
		with pytest.raises(ValueError):
			green_zone(adu=-1, batch_days=5)
		with pytest.raises(ValueError):
			green_zone(adu=10, batch_days=5, min_order_qty=-5)


class TestComputePurchaseZones:
	def test_lead_time_drives_yellow_and_red(self):
		# ADU 10, срок поставки 20 дн, k_var 0.5, риска нет.
		z = compute_purchase_zones(adu=10, lead_time_days=20, batch_days=5, k_var=0.5)
		assert z.yellow == 200  # ADU × lead_time
		assert z.red == pytest.approx(100.0)  # ADU × lead_time × k_var
		assert z.green == 50  # ADU × batch_days (нет min_order)
		assert z.target == pytest.approx(350.0)

	def test_min_order_sets_green(self):
		z = compute_purchase_zones(adu=10, lead_time_days=20, batch_days=5, min_order_qty=300)
		assert z.green == 300
		assert z.yellow == 200  # цикловая не зависит от партии

	def test_supply_risk_scales_only_red(self):
		base = compute_purchase_zones(adu=10, lead_time_days=20, batch_days=5, k_var=0.5)
		risky = compute_purchase_zones(
			adu=10, lead_time_days=20, batch_days=5, k_var=0.5, supply_risk_pct=30
		)
		assert risky.red == pytest.approx(base.red * 1.30)
		assert risky.yellow == base.yellow  # жёлтая не трогается
		assert risky.green == base.green  # зелёная не трогается

	def test_zero_lead_time_is_all_zero(self):
		# Срок поставки неизвестен (0) → зоны нулевые, не падаем.
		z = compute_purchase_zones(adu=10, lead_time_days=0, batch_days=5, min_order_qty=300)
		assert z == Zones(red=0.0, yellow=0.0, green=0.0)
		assert z.target == 0.0

	def test_zero_lead_time_means_no_shelf(self):
		# Режим позиции: без срока полки нет (ADU×RT = 0 < порога).
		assert not has_shelf(adu=10, rt_days=0, threshold_qty=5)

	def test_positive_lead_time_has_shelf(self):
		assert has_shelf(adu=10, rt_days=20, threshold_qty=5)

	def test_zero_adu_zones_are_zero(self):
		z = compute_purchase_zones(adu=0, lead_time_days=20, batch_days=5, min_order_qty=0)
		assert z.yellow == 0 and z.red == 0 and z.green == 0

	def test_rejects_negative_params(self):
		with pytest.raises(ValueError):
			compute_purchase_zones(adu=-1, lead_time_days=20, batch_days=5)
		with pytest.raises(ValueError):
			compute_purchase_zones(adu=10, lead_time_days=-1, batch_days=5)
		with pytest.raises(ValueError):
			compute_purchase_zones(adu=10, lead_time_days=20, batch_days=5, supply_risk_pct=-5)

	def test_deterministic(self):
		args = dict(adu=7, lead_time_days=14, batch_days=3, min_order_qty=50, k_var=0.25, supply_risk_pct=10)
		assert compute_purchase_zones(**args) == compute_purchase_zones(**args)

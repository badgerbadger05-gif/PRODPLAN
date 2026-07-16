"""Зоны буфера позиции супермаркета — чистое ядро питателя №2.

Методика: питатель-2-мехцех-буферы-и-верёвка.md §2–§4, решения владельца
04.07 (техдизайн Фазы 2 §12): RT-классы 7/15/21, порог полки 5 шт.

Чистый Python без Frappe (паттерн ядра барабана): импортируется
DocType-обвязкой и pytest-гарнитурой.


Портировано из prodflow prodflow/services/feeder/zones.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

import math
from typing import NamedTuple

GREEN = "Green"
YELLOW = "Yellow"
RED = "Red"


class Zones(NamedTuple):
	red: float
	yellow: float
	green: float

	@property
	def target(self) -> float:
		return self.red + self.yellow + self.green


def compute_zones(
	adu: float,
	rt_days: float,
	batch_days: float,
	optimal_batch: float = 0.0,
	k_var: float = 0.5,
	k_crit: bool = False,
	supply_risk_pct: float = 0.0,
) -> Zones:
	"""Трёхзонная модель (методика §3).

	Жёлтая (цикловая)   = ADU × RT — спрос за время пополнения.
	Зелёная (партия)    = max(optimal_batch, ADU × batch_days) — частота запусков.
	Красная (страховая) = ADU × RT × k_var, +25% при k_crit (единственный станок),
	                       × (1 + supply_risk_pct/100) — страховка риска поставки
	                       по категории (группа номенклатуры, Этап B). 0% (дефолт)
	                       — красная зона без изменений.
	"""
	if adu < 0 or rt_days < 0 or batch_days < 0:
		raise ValueError(f"Отрицательные параметры зон: adu={adu}, rt={rt_days}, batch_days={batch_days}")
	if supply_risk_pct < 0:
		raise ValueError(f"Отрицательная страховка риска поставки: {supply_risk_pct}%")
	yellow = adu * rt_days
	green = max(float(optimal_batch or 0.0), adu * batch_days)
	red = adu * rt_days * k_var * (1.25 if k_crit else 1.0) * (1.0 + supply_risk_pct / 100.0)
	return Zones(red=red, yellow=yellow, green=green)


def green_zone(
	adu: float,
	batch_days: float,
	min_order_qty: float = 0.0,
	optimal_batch: float = 0.0,
) -> float:
	"""Зелёная зона (партия пополнения) — выбор кванта.

	Закупная позиция: если у номенклатуры задан min_order_qty (мин. партия
	заказа поставщика, > 0) — берём его; иначе, как у производимой, —
	max(optimal_batch, ADU × batch_days). Вынесено отдельной чистой функцией
	ради pytest и общего использования производимым/закупным контуром.
	"""
	if adu < 0 or batch_days < 0:
		raise ValueError(f"Отрицательные параметры зелёной зоны: adu={adu}, batch_days={batch_days}")
	if min_order_qty < 0:
		raise ValueError(f"Отрицательная мин. партия заказа: {min_order_qty}")
	if min_order_qty and min_order_qty > 0:
		return float(min_order_qty)
	return max(float(optimal_batch or 0.0), adu * batch_days)


def compute_purchase_zones(
	adu: float,
	lead_time_days: float,
	batch_days: float,
	min_order_qty: float = 0.0,
	k_var: float = 0.5,
	supply_risk_pct: float = 0.0,
) -> Zones:
	"""Зоны закупной позиции-буфера (склад поступления материалов/комплектующих).

	Отличия от производимой (compute_zones):
	  RT      = lead_time_days — срок поставки из 1С (СрокПополнения), а не класс
	            маршрута мехцеха.
	  Зелёная = min_order_qty, если задан; иначе ADU × batch_days (green_zone).
	  Жёлтая  = ADU × RT.
	  Красная = ADU × RT × k_var × (1 + supply_risk_pct/100) — страховка риска
	            поставки по категории (группа номенклатуры).

	lead_time_days ≤ 0 → срок поставки неизвестен: зоны нулевые, позиция
	работает без полки (закупка запускается под слот графика), НЕ падаем.
	"""
	if adu < 0 or batch_days < 0 or lead_time_days < 0:
		raise ValueError(
			f"Отрицательные параметры зон закупного: adu={adu}, lead_time={lead_time_days}, batch_days={batch_days}"
		)
	if supply_risk_pct < 0:
		raise ValueError(f"Отрицательная страховка риска поставки: {supply_risk_pct}%")
	rt = float(lead_time_days or 0.0)
	if rt <= 0:
		# Срок неизвестен — цикловая/страховая/партия по времени не считаются:
		# позиция без полки, зоны нулевые (решение — не падать на пустом сроке).
		return Zones(red=0.0, yellow=0.0, green=0.0)
	green = green_zone(adu, batch_days, min_order_qty)
	yellow = adu * rt
	red = adu * rt * k_var * (1.0 + supply_risk_pct / 100.0)
	return Zones(red=red, yellow=yellow, green=green)


def has_shelf(adu: float, rt_days: float, threshold_qty: float) -> bool:
	"""Критерий «полка / без полки»: за время пополнения потребляется
	не меньше threshold_qty штук (решение 04.07: старт 5)."""
	return adu * rt_days >= threshold_qty


def nfp_zone(nfp: float, zones: Zones) -> str:
	"""Зона позиции потока (методика §4): К/Ж — сигнал пополнения, З — не запускать."""
	if nfp <= zones.red:
		return RED
	if nfp <= zones.red + zones.yellow:
		return YELLOW
	return GREEN


def penetration(nfp: float, zones: Zones) -> float:
	"""1 − NFP/Target: 0 — полный буфер, 1 — пусто, >1 — минус."""
	if zones.target <= 0:
		return 0.0
	return 1.0 - nfp / zones.target


def replenishment_qty(nfp: float, zones: Zones, multiple: float = 0.0) -> float:
	"""Рекомендованный заказ: Target − NFP, вверх до кратности.

	0 — если позиция в зелёной зоне (запуск = перепроизводство, §4).
	Квант Q отдельно не добивается: в жёлтой/красной зоне Target − NFP
	≥ зелёной зоны = max(Q, ADU × batch_days) по построению модели.
	"""
	if nfp_zone(nfp, zones) == GREEN:
		return 0.0
	qty = zones.target - nfp
	if multiple and multiple > 0:
		qty = math.ceil(qty / multiple) * multiple
	return qty


def round_up(qty: float, multiple: float = 1.0) -> float:
	"""Round a positive requirement up to the configured production quantum."""
	if qty <= 0:
		return 0.0
	quantum = float(multiple or 1.0)
	if quantum <= 0:
		quantum = 1.0
	return math.ceil(qty / quantum) * quantum

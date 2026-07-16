"""ADU позиций супермаркета от выровненного микса — чистое ядро.

ADU = Σ по SKU (qty_per_unit × дневной темп SKU), где обрезанная спека
SKU (kit-строки с границей супермаркета) и темпы — вход. Инвариант
методики §2: темпы берутся от квартального плана/барабана, НЕ от
фактического потребления сборкой.

Чистый Python без Frappe: Frappe-адаптер (обход живых BOM через
drum/kit.py и темпы из активного графика) — в position_service.


Портировано из prodflow prodflow/services/feeder/adu.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NamedTuple


class KitLine(NamedTuple):
	"""Строка обрезанной спеки SKU: деталь, кол-во на единицу, склад-полка."""

	item: str
	qty_per_unit: float
	warehouse: str


class AduRow(NamedTuple):
	adu: float
	commonality: int  # в скольких SKU микса участвует деталь


def build_adu(
	kits: Mapping[str, Sequence[KitLine]],
	daily_rate: Mapping[str, float],
) -> dict[tuple[str, str], AduRow]:
	"""ADU по позициям (item × warehouse) от обрезанных спек и темпов SKU.

	SKU с нулевым темпом (нет в миксе) вклад не даёт, но commonality
	не портит — считаем общность только по SKU с положительным темпом.
	Детерминированный результат: суммирование в отсортированном порядке.
	"""
	acc: dict[tuple[str, str], float] = {}
	users: dict[tuple[str, str], set[str]] = {}
	for sku in sorted(kits):
		rate = float(daily_rate.get(sku, 0.0) or 0.0)
		if rate < 0:
			raise ValueError(f"Отрицательный темп SKU {sku}: {rate}")
		if not rate:
			continue
		for line in kits[sku]:
			if line.qty_per_unit < 0:
				raise ValueError(f"Отрицательное количество в ките {sku}/{line.item}")
			key = (line.item, line.warehouse)
			acc[key] = acc.get(key, 0.0) + line.qty_per_unit * rate
			users.setdefault(key, set()).add(sku)
	return {
		key: AduRow(adu=qty, commonality=len(users[key]))
		for key, qty in acc.items()
		if qty > 0
	}


def daily_rates_from_plan(
	qty_by_sku: Mapping[str, float], workdays: int
) -> dict[str, float]:
	"""Суточные темпы SKU {item: шт/день} от ПЛАНА выпуска, а не от такта.

	База темпа = плановое количество SKU за горизонт / число рабочих дней
	горизонта. Это реальный суточный СПРОС барабана, под который держится
	буфер супермаркета.

	Почему план, а не такт: такт участка — это ПРОПУСКНАЯ СПОСОБНОСТЬ
	(сколько участок способен выпустить в день), она в разы выше реального
	спроса. Если кормить ADU тактом, зоны буферов раздуваются во столько же
	раз, и почти всё встаёт «в красном» на пустом месте. Буфер держится под
	спрос (сколько реально едет по графику), поэтому источник темпа — план
	барабана: Σ планового qty SKU за горизонт делим на рабочие дни горизонта.

	Контракт:
	- ``workdays <= 0`` → ``{}`` (горизонт без рабочих дней — делить не на что,
	  не роняем деление на ноль).
	- Отрицательное плановое qty → ``ValueError`` (данные битые).
	- SKU с нулевым/пустым qty в результат не попадают (буфер не нужен).
	- Детерминизм: обход в отсортированном порядке SKU.
	"""
	if workdays <= 0:
		return {}
	rates: dict[str, float] = {}
	for sku in sorted(qty_by_sku):
		qty = float(qty_by_sku.get(sku) or 0.0)
		if qty < 0:
			raise ValueError(f"Отрицательное плановое количество SKU {sku}: {qty}")
		if not qty:
			continue
		rates[sku] = qty / workdays
	return rates

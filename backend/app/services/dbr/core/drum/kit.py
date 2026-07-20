"""Обрезанная спека слота барабана — кит (техдизайн Фазы 1 §4).

Чистый Python без Frappe: обход BOM и классификация компонентов
инжектируются колбэками, Frappe-адаптер — в gate_service.

`build_kit` обходит активный BOM SKU вниз до первой границы, дальше
не разворачивает:
- заготовка-буфер Склада №2 (последняя операция BOM сдаётся на №2) →
  комплект Склада №2 (W2) — точка развязки DDMRP, приоритетнее №3/№4;
- позиция супермаркета №3 (окрашенная) → комплект Склада №3 (W3);
- позиция супермаркета №4 / узловая сборка / закупная → Склад №4 (W4);
- крепёж (группа метизов ⚙) → исключается из кита (free-issue,
  регламент ТМЦ) (FASTENER);
- деталь «без полки» → в кит с пометкой «под график» (UNDER_SCHEDULE);
- нескладируемая группировка (фантомный узел) → разворачивается
  дальше (RECURSE).

Кит кэшируется на (sku, bom_version) на стороне адаптера — пересчёт
при смене активного BOM.


Портировано из prodflow prodflow/services/drum/kit.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

# Решения классификатора
W2 = "w2"  # граница: заготовка-буфер Склада №2 (точка развязки мехцеха)
W3 = "w3"  # граница: комплект Склада №3 (окрашенная позиция)
W4 = "w4"  # граница: комплект Склада №4 (узловая сборка / закупная)
FASTENER = "fastener"  # крепёж: вне кита (free-issue)
UNDER_SCHEDULE = "under_schedule"  # деталь без полки: в кит с пометкой «под график»
RECURSE = "recurse"  # нескладируемая группировка: разворачиваем дальше


class KitLine(NamedTuple):
	item: str
	qty_per_unit: float
	source_warehouse: str
	under_schedule: bool


# classify(item_code) → (решение, склад-источник | None)
Classifier = Callable[[str], tuple[str, str | None]]
# get_components(item_code) → [(component_code, qty_per_unit)] активного BOM
ComponentProvider = Callable[[str], list[tuple[str, float]]]


def build_kit(sku: str, get_components: ComponentProvider, classify: Classifier) -> list[KitLine]:
	"""Кит на единицу SKU: [(item, qty_per_unit, source_warehouse, under_schedule)]."""
	acc: dict[tuple[str, str, bool], float] = {}
	_walk(sku, 1.0, get_components, classify, acc, stack=(sku,))
	return [
		KitLine(item, qty, warehouse, under_schedule)
		for (item, warehouse, under_schedule), qty in sorted(acc.items())
	]


def _walk(
	item: str,
	multiplier: float,
	get_components: ComponentProvider,
	classify: Classifier,
	acc: dict[tuple[str, str, bool], float],
	stack: tuple[str, ...],
) -> None:
	for component, qty_per_unit in get_components(item):
		if component in stack:
			raise ValueError(f"Цикл в BOM: {' → '.join((*stack, component))}")
		decision, warehouse = classify(component)
		qty = qty_per_unit * multiplier
		if decision == FASTENER:
			continue
		if decision == RECURSE:
			_walk(component, qty, get_components, classify, acc, (*stack, component))
			continue
		if decision in (W2, W3, W4, UNDER_SCHEDULE):
			if not warehouse:
				raise ValueError(f"Классификатор не вернул склад для «{component}» ({decision})")
			key = (component, warehouse, decision == UNDER_SCHEDULE)
			acc[key] = acc.get(key, 0.0) + qty
			continue
		raise ValueError(f"Неизвестное решение классификатора для «{component}»: {decision}")

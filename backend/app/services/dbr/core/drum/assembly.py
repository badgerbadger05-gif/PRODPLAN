"""Такт сборки от участка и его мощности — чистое ядро (решение владельца 08.07.2026).

Отказ от тактов семейств. Такт изделия задаётся не справочником семейств, а
парой «участок + норма на единицу мощности»:

	такт(SKU, шт/день) = норма_на_единицу_мощности × production_capacity участка

К15 = 3, К8 = 4 на единицу мощности. Мощность 2 → 6 и 8 шт/день; мощность 3 →
9 и 12. Добавили сборщика — поменяли мощность участка, весь барабан
пересчитался; назначения и нормы трогать не нужно.

Почему это убирает костыль: `leveling.level()` принимает
`{группа: (участок, такт)}` и мешает группы по дням, каждую со своим тактом.
Раньше группой было СЕМЕЙСТВО, а SKU внутри него делили общий такт — чтобы К8
и К15 ехали с разной скоростью, приходилось выделять виртуальные единицы
`family␟takt` (`program_input.build_takt_units`). Если группа = САМ SKU, ядро
выравнивания получает индивидуальные такты по построению и не меняется ни
строкой, а `build_takt_units` становится не нужен.

Ограничение модели: одна штука занимает `1/такт` дневной нагрузки при лимите
1.0 в день, поэтому такт обязан быть ≥ 1 — изделие медленнее одной штуки в
день в эту модель не укладывается (см. `validate_assignments`).

Чистый Python без Frappe: `python -m pytest prodflow/services/drum`.


Портировано из prodflow prodflow/services/drum/assembly.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NamedTuple

# Минимальный такт: ниже единицы штука не помещается в дневной лимит нагрузки.
MIN_TAKT = 1.0


class Assignment(NamedTuple):
	"""Назначение изделия на сборочный участок с нормой на единицу мощности."""

	workstation: str
	item: str
	qty_per_capacity: float


def effective_takt(qty_per_capacity: float, capacity: float | int | None) -> float:
	"""Такт изделия, шт/день: норма × мощность участка.

	Пустая или нулевая мощность трактуется как 1 (участок без явно заданной
	численности работает «в одно лицо»), иначе назначение молча обнулялось бы.
	"""
	units = float(capacity or 0) or 1.0
	return float(qty_per_capacity) * units


def build_sku_takts(
	assignments: Sequence[Assignment],
	capacities: Mapping[str, float | int | None],
) -> dict[str, tuple[str, float]]:
	"""Назначения → вход для leveling.level(): {SKU: (участок, такт)}.

	Группа выравнивания = сам SKU, поэтому у каждого изделия свой такт.
	Дубликаты изделия сюда попадать не должны (это ловит валидация); при
	повторе побеждает последнее назначение — детерминированно и без падения,
	чтобы пересборка графика не рушилась на кривых данных.
	"""
	takts: dict[str, tuple[str, float]] = {}
	for row in assignments:
		takts[row.item] = (
			row.workstation,
			effective_takt(row.qty_per_capacity, capacities.get(row.workstation)),
		)
	return takts


def validate_assignments(
	assignments: Sequence[Assignment],
	capacities: Mapping[str, float | int | None],
) -> list[str]:
	"""Ошибки настройки сборки, по одной строке на проблему (пустой список — всё чисто).

	Проверяем ровно то, что делает график невыравниваемым:
	- норма ≤ 0 — изделие не собирается вовсе;
	- одно изделие назначено на несколько участков — неоднозначно, где его планировать;
	- такт < 1 — штука не помещается в дневной лимит нагрузки (см. модуль-docstring).
	"""
	errors: list[str] = []

	seen: dict[str, list[str]] = {}
	for row in assignments:
		seen.setdefault(row.item, []).append(row.workstation)

	for item in sorted(seen):
		stations = seen[item]
		if len(stations) > 1:
			errors.append(
				f"Изделие «{item}» назначено на несколько участков: {', '.join(sorted(set(stations)))}. "
				"Изделие собирается ровно на одном участке."
			)

	for row in sorted(assignments):
		if row.qty_per_capacity <= 0:
			errors.append(
				f"Изделие «{row.item}» на участке «{row.workstation}»: норма на единицу мощности "
				f"должна быть больше нуля, задано {row.qty_per_capacity:g}."
			)
			continue
		takt = effective_takt(row.qty_per_capacity, capacities.get(row.workstation))
		if takt < MIN_TAKT:
			errors.append(
				f"Изделие «{row.item}» на участке «{row.workstation}»: такт {takt:g} шт/день меньше 1. "
				"В нагрузочной модели одна штука занимает 1/такт дня при дневном лимите 1.0, "
				"поэтому изделие медленнее штуки в день запланировать нельзя — "
				"увеличьте норму или мощность участка."
			)

	return errors

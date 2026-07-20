"""Выравнивание квартального плана сборки — ядро барабана, нагрузочная модель.

Решение заказчика 03.07: такты семейств одного участка ИНДИВИДУАЛЬНЫ
(пример: на одном участке толкач собирают 10/день, буксировщик К8 —
12/день, лыжный модуль — 5/день). Упрощение «такт общий на участок»
снято — ядро работает в нагрузочной модели (rate-based mixed-model):

- мощность участка в день = 1.0 нагрузки; одна штука семейства f
  потребляет 1/takt_f нагрузки; месячная ёмкость участка = число
  рабочих дней месяца (в единицах нагрузки);
- превышение: Σ qty_f/takt_f > раб.дни → спрос режется до ёмкости
  пропорционально нагрузке семейств (целые штуки, каждое scheduled_f
  ≤ спроса), избыток переносится вправо (следующий месяц, за концом
  горизонта — carried_over), разница фиксируется в gaps
  ПО-СЕМЕЙСТВЕННО: «в день D на участке W не влезает N шт семейства F»;
- дневная раскладка: жадный выбор по наибольшему дефициту нагрузки от
  пропорциональной квоты; лимит каждого дня жёсткий: нагрузка ≤ 1.0.
  Целочисленный объём, который физически не упаковывается по дням, не
  прячется в перегруз последнего дня, а переносится вправо и попадает в gap;
- микс SKU внутри семейства: по наибольшему дефициту от месячных
  долей — варианты чередуются, а не идут блоками.

Такт — float ≥ 1 (дробные такты валидны: floor в нагрузочной модели
не используется, мощность не теряется).

Чистый Python без Frappe-зависимостей: этот же модуль импортируют
DocType-обвязка и бэктест-гарнитура (решение 02.07).

Вход:
- volumes: [(family, sku, qty, month)] — квартальный план по месяцам;
- workdays: {month: [date, ...]} — рабочие дни из ERPNext Holiday List
  (календарь — вход, не константа);
- families: {family: (workstation, takt_per_day)}.

Гарантии (свойства для тестов): сумма слотов + carried_over = сумме
входа; нагрузка любого дня ≤ 1.0, кроме, возможно, последнего рабочего
дня месяца (целочисленный перелив); детерминированность.


Портировано из prodflow prodflow/services/drum/leveling.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import NamedTuple

_EPS = 1e-9


class Slot(NamedTuple):
	date: date
	workstation: str
	family: str
	sku: str
	qty: int


class CapacityGap(NamedTuple):
	date: date
	workstation: str
	family: str
	required_qty: int
	takt_qty: float  # такт семейства, шт/день
	gap_qty: int


@dataclass(frozen=True)
class LevelingResult:
	slots: list[Slot]
	gaps: list[CapacityGap]
	# (family, sku) → количество, не поместившееся до конца горизонта
	carried_over: dict[tuple[str, str], int]


def level(
	volumes: Iterable[tuple[str, str, float, str]],
	workdays: Mapping[str, Sequence[date]],
	families: Mapping[str, tuple[str, float]],
) -> LevelingResult:
	months = sorted(workdays)
	days_by_month = {m: sorted(set(workdays[m])) for m in months}
	_validate_families(families)

	# month → family → sku → qty
	demand: dict[str, dict[str, dict[str, int]]] = {m: {} for m in months}
	for family, sku, qty, month in volumes:
		if family not in families:
			raise ValueError(f"Семейство «{family}» отсутствует в конфигурации families")
		if month not in days_by_month:
			raise ValueError(f"Месяц «{month}» плана отсутствует в календаре workdays")
		q = _int_qty(qty, f"{family}/{sku}/{month}")
		if not q:
			continue
		sku_map = demand[month].setdefault(family, {})
		sku_map[sku] = sku_map.get(sku, 0) + q

	slots: list[Slot] = []
	gaps: list[CapacityGap] = []
	carry: dict[str, dict[str, int]] = {}  # family → sku → qty, переносимое вправо

	for month in months:
		days = days_by_month[month]
		month_demand = _merge_carry(demand[month], carry)
		carry = {}
		if not month_demand:
			continue
		if not days:
			carry = month_demand  # месяц без рабочих дней — весь объём уезжает вправо
			continue

		for ws in sorted({families[f][0] for f in month_demand}):
			ws_families = {f: sum(s.values()) for f, s in month_demand.items() if families[f][0] == ws}
			takts = {f: families[f][1] for f in ws_families}
			capacity_load = float(len(days))
			total_load = sum(q / takts[f] for f, q in ws_families.items())

			scheduled = dict(ws_families)
			if total_load > capacity_load + _EPS:
				scheduled = _fit_to_load(capacity_load, ws_families, takts)

			family_days, unpacked = _interleave_by_load(len(days), scheduled, takts)
			for f, qty in unpacked.items():
				scheduled[f] -= qty

			# Both aggregate overload and integer bin-packing overflow become an
			# explicit cut/carry. No day is allowed to exceed one load unit.
			cuts: dict[str, int] = {}
			for f in ws_families:
				cut = ws_families[f] - scheduled[f]
				if not cut:
					continue
				cuts[f] = cut
				kept = _largest_remainder(scheduled[f], month_demand[f])
				for sku, q in month_demand[f].items():
					rest = q - kept[sku]
					if rest:
						fam_carry = carry.setdefault(f, {})
						fam_carry[sku] = fam_carry.get(sku, 0) + rest
				month_demand[f] = {sku: q for sku, q in kept.items() if q}

			for f in sorted(f for f, q in scheduled.items() if q):
				sku_days = _interleave([row.get(f, 0) for row in family_days], month_demand[f])
				for i, day in enumerate(days):
					for sku in sorted(sku_days[i]):
						if sku_days[i][sku]:
							slots.append(Slot(day, ws, f, sku, sku_days[i][sku]))

			for f in sorted(cuts):
				gap_days = _quota_series(cuts[f], len(days))
				for i, day in enumerate(days):
					if gap_days[i]:
						fam_day_qty = family_days[i].get(f, 0)
						gaps.append(
							CapacityGap(day, ws, f, fam_day_qty + gap_days[i], takts[f], gap_days[i])
						)

	carried_over = {
		(f, sku): q for f in sorted(carry) for sku, q in sorted(carry[f].items()) if q
	}
	slots.sort()
	gaps.sort()
	return LevelingResult(slots=slots, gaps=gaps, carried_over=carried_over)


def _validate_families(families: Mapping[str, tuple[str, float]]) -> None:
	"""Такт каждого семейства — float ≥ 1; такты одного участка могут различаться."""
	for family in sorted(families):
		takt = families[family][1]
		if takt < 1:
			raise ValueError(f"Такт семейства «{family}» должен быть ≥ 1, получено {takt}")


def _int_qty(qty: float, context: str) -> int:
	if qty < 0:
		raise ValueError(f"Отрицательное количество в плане: {context} = {qty}")
	if float(qty) != int(qty):
		raise ValueError(f"Дробное количество в плане не поддерживается: {context} = {qty}")
	return int(qty)


def _merge_carry(
	month_demand: dict[str, dict[str, int]], carry: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
	merged = {f: dict(skus) for f, skus in month_demand.items()}
	for f, skus in carry.items():
		target = merged.setdefault(f, {})
		for sku, q in skus.items():
			target[sku] = target.get(sku, 0) + q
	return merged


def _quota_series(total: int, n_days: int) -> list[int]:
	"""Равномерная целочисленная раскладка total по n_days: сумма точная, разброс ≤ 1."""
	return [(total * (i + 1)) // n_days - (total * i) // n_days for i in range(n_days)]


def _largest_remainder(total: int, weights: Mapping[str, int]) -> dict[str, int]:
	"""Пропорциональная целочисленная раскладка total по весам (метод наибольших остатков).

	total ≤ sum(weights); результат по каждому ключу не превышает его вес.
	"""
	keys = sorted(weights)
	weight_sum = sum(weights.values())
	ideal = {k: total * weights[k] / weight_sum for k in keys}
	result = {k: math.floor(ideal[k]) for k in keys}
	remainder = total - sum(result.values())
	for k in sorted(keys, key=lambda k: (result[k] - ideal[k], k))[:remainder]:
		result[k] += 1
	return result


def _fit_to_load(
	capacity_load: float, demand: Mapping[str, int], takts: Mapping[str, float]
) -> dict[str, int]:
	"""Обрезка спроса до месячной ёмкости участка в единицах нагрузки.

	Пропорционально нагрузке семейств (аналог метода наибольших
	остатков, но веса — в нагрузке): старт с floor идеальной доли
	каждого семейства, затем добор по одной штуке семейства с
	наибольшим дефицитом нагрузки, пока штука влезает в остаток
	ёмкости. Результат целочисленный, scheduled_f ≤ demand_f,
	Σ scheduled_f/takt_f ≤ capacity_load.
	"""
	keys = sorted(demand)
	total_load = sum(demand[f] / takts[f] for f in keys)
	ideal = {f: demand[f] * capacity_load / total_load for f in keys}
	result = {f: min(demand[f], math.floor(ideal[f])) for f in keys}
	used = sum(result[f] / takts[f] for f in keys)
	while True:
		candidates = [
			f for f in keys if result[f] < demand[f] and used + 1.0 / takts[f] <= capacity_load + _EPS
		]
		if not candidates:
			break
		key = max(candidates, key=lambda f: ((ideal[f] - result[f]) / takts[f], f))
		result[key] += 1
		used += 1.0 / takts[key]
	return result


def _interleave_by_load(
	n_days: int, shares: Mapping[str, int], takts: Mapping[str, float]
) -> tuple[list[dict[str, int]], dict[str, int]]:
	"""Раскладка месячных объёмов семейств по дням с лимитом нагрузки 1.0.

	Largest-load-first units are placed into the currently least-loaded day
	that can accept them. This keeps the month balanced while respecting the
	hard daily capacity. Returns the physically unpackable remainder.
	"""
	keys = sorted(f for f, q in shares.items() if q)
	rows = [dict.fromkeys(keys, 0) for _ in range(n_days)]
	loads = [0.0] * n_days
	remaining: dict[str, int] = {}
	units = [f for f in sorted(keys, key=lambda f: (-1.0 / takts[f], f)) for _ in range(shares[f])]
	for family in units:
		unit_load = 1.0 / takts[family]
		candidates = [i for i in range(n_days) if loads[i] + unit_load <= 1.0 + _EPS]
		if not candidates:
			remaining[family] = remaining.get(family, 0) + 1
			continue
		day_index = min(candidates, key=lambda i: (loads[i], rows[i][family], i))
		rows[day_index][family] += 1
		loads[day_index] += unit_load
	rows = [{f: q for f, q in row.items() if q} for row in rows]
	return rows, remaining


def _interleave(day_totals: Sequence[int], shares: Mapping[str, int]) -> list[dict[str, int]]:
	"""Распределить месячные объёмы shares по дням с чередованием ключей.

	Жадный выбор по наибольшему дефициту от пропорциональной квоты
	(целочисленная арифметика): суммы по ключам сходятся точно, ни один
	ключ не получает больше своей месячной доли, варианты чередуются.
	Используется для микса SKU внутри семейства (такт у SKU семейства
	общий, лимит нагрузки уже обеспечен раскладкой семейств по дням).
	"""
	keys = sorted(k for k, q in shares.items() if q)
	month_total = sum(shares[k] for k in keys)
	assigned = dict.fromkeys(keys, 0)
	rows: list[dict[str, int]] = []
	cum = 0
	for day_qty in day_totals:
		cum += day_qty
		row = dict.fromkeys(keys, 0)
		for _ in range(day_qty):
			key = max(keys, key=lambda k: (shares[k] * cum - assigned[k] * month_total, k))
			assigned[key] += 1
			row[key] += 1
		rows.append({k: q for k, q in row.items() if q})
	return rows

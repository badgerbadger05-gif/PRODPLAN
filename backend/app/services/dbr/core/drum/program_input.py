"""Чистые преобразования входа программы для барабана (без Frappe).

Вынесено отдельно, чтобы агрегацию строк программы в volumes для
leveling.level() и выбор месяцев периода можно было покрыть pytest'ом
без импорта frappe (schedule_service тянет frappe на уровне модуля).


Портировано из prodflow prodflow/services/drum/program_input.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import NamedTuple


class ProgramRow(NamedTuple):
	"""Минимум строки программы, нужный выравниванию.

	`family` — группа выравнивания. С 08.07.2026 это САМ SKU: такт задаётся
	парой «участок + норма на единицу мощности» (`drum.assembly`), поэтому
	отдельная группировка по семейству больше не нужна.
	"""

	item_code: str
	qty: float
	program_date: date
	family: str


def month_key(day: date) -> str:
	"""Ключ месяца YYYY-MM для группировки объёмов (совпадает с leveling)."""
	return f"{day.year:04d}-{day.month:02d}"


def build_volumes(rows: Iterable[ProgramRow]) -> list[tuple[str, str, float, str]]:
	"""Строки программы → volumes для leveling.level().

	Возвращает агрегированный по (family, sku, month) список
	[(family, sku, qty, month)]. Детерминированный порядок:
	сортировка по (month, family, sku).
	"""
	acc: dict[tuple[str, str, str], float] = {}
	for row in rows:
		key = (row.family, row.item_code, month_key(row.program_date))
		acc[key] = acc.get(key, 0.0) + row.qty
	return [
		(family, sku, qty, month)
		for (family, sku, month), qty in sorted(acc.items(), key=lambda e: (e[0][2], e[0][0], e[0][1]))
	]


def frozen_window(workdays: Iterable[date], today: date, frozen_days: int) -> set[date]:
	"""Замороженная зона: ближайшие frozen_days рабочих дней от today (включительно).

	Прошедшие дни в зону не входят — невыполненный слот из прошлого можно
	переносить вперёд. Пустой набор при frozen_days <= 0.
	"""
	if frozen_days <= 0:
		return set()
	return set(sorted(d for d in workdays if d >= today)[:frozen_days])


def months_in_period(period_from: date, period_to: date) -> list[str]:
	"""Список ключей YYYY-MM всех месяцев периода [from; to] включительно."""
	if period_from > period_to:
		raise ValueError("period_from позже period_to")
	months: list[str] = []
	year, month = period_from.year, period_from.month
	while (year, month) <= (period_to.year, period_to.month):
		months.append(f"{year:04d}-{month:02d}")
		if month == 12:
			year, month = year + 1, 1
		else:
			month += 1
	return months


def workdays_by_month(months: Sequence[str], workday_dates: Iterable[date]) -> dict[str, list[date]]:
	"""Разложить плоский список рабочих дней по месяцам YYYY-MM.

	Каждый месяц периода присутствует в результате (возможно с пустым
	списком — месяц без рабочих дней), чтобы leveling видел весь горизонт.
	"""
	result: dict[str, list[date]] = {m: [] for m in months}
	for day in workday_dates:
		key = month_key(day)
		if key in result:
			result[key].append(day)
	for key in result:
		result[key].sort()
	return result

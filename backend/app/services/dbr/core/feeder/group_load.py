"""Гистограмма загрузки групп оборудования мехцеха — чистое ядро питателя №2.

Техдизайн Фазы 2 §4.3: недельные вёдра нормо-часов очереди по группам
оборудования (заготовка/гибка/токарка/фрезеровка/сварка/окраска) против
фонда групп. Классификатор групп — порт classify_pay_category отчёта
	по именам Workstation ERPNext, только
6 групп мехцеха: сборочные категории исключаются (вне мехцеха).

	Чистый Python без Frappe: сигналы приходят готовыми dict. Frappe-обвязка — load_service.py.


Портировано из prodflow prodflow/services/feeder/group_load.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

# --- Классификатор групп мехцеха ------------------------------------------

GROUP_ORDER: tuple[str, ...] = (
	"blanking",
	"sheet_bending",
	"turning",
	"milling",
	"weld",
	"paint",
)

GROUP_LABELS: dict[str, str] = {
	"blanking": "Заготовка",
	"sheet_bending": "Гибка листовая",
	"turning": "Токарка",
	"milling": "Фрезеровка",
	"weld": "Сварка",
	"paint": "Окраска",
	"other": "Прочее",
}


def _norm_text(value: Any) -> str:
	return str(value or "").strip().casefold().replace("ё", "е")


def classify_group(workstation: str, kind_name: str = "") -> str | None:
	"""Группа оборудования мехцеха по названию участка (порядок проверок — из
	classify_pay_category отчёта нормативов).

	Возвращает ключ одной из 6 групп мехцеха; None — сборочная категория
	(вне мехцеха, исключается из загрузки); "other" — не распознано.
	"""
	text = f"{_norm_text(workstation)} {_norm_text(kind_name)}"

	if "свар" in text or "сборка/сварка" in text:
		return "weld"
	if "порош" in text or "покрас" in text or "окрас" in text:
		return "paint"
	if "фрезер" in text:
		return "milling"
	if "токар" in text:
		return "turning"
	if "гибка лист" in text or "листового металла" in text or "гибочный (лист" in text:
		return "sheet_bending"
	if any(
		token in text
		for token in ("заготов", "резк", "лазер", "сверл", "штампов", "зенков", "гибка трубы", "гибка прутка")
	):
		return "blanking"
	# Сборочные категории — вне мехцеха, из загрузки групп исключаются (None).
	if any(
		token in text
		for token in (
			"модул",
			"навесных узлов",
			"переднего узла",
			"двигател",
			"коробок",
			"пластик",
			"балансир",
			"катков",
			"валов",
		)
	):
		return None
	if "сбор" in text or "метизы" in text or "комплект" in text or "наклей" in text:
		return None
	return "other"


# --- Недельные вёдра загрузки ---------------------------------------------


def _as_date(value: Any) -> date | None:
	"""Привести date / datetime / ISO-строку к date; иначе None."""
	if value is None:
		return None
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	try:
		return date.fromisoformat(str(value)[:10])
	except ValueError:
		return None


def _monday(d: date) -> date:
	return d - timedelta(days=d.weekday())


def week_index(target_date: Any, today: Any) -> int:
	"""Индекс недели цели относительно текущей (недели с понедельника).

	(monday(target) − monday(today)).days // 7; прошлое (и нечитаемые даты)
	сворачиваются в 0 — «запускать сейчас».
	"""
	t = _as_date(target_date)
	n = _as_date(today)
	if t is None or n is None:
		return 0
	idx = (_monday(t) - _monday(n)).days // 7
	return idx if idx > 0 else 0


def launch_week_for_signal(signal: dict, today: Any) -> int:
	"""Неделя запуска сигнала (§4.3): от какого понедельника считать его нормо-часы.

	- статус In Work → 0 (уже в работе, грузит текущую неделю);
	- «Под график» → need_date − RT дней → week_index;
	- «Пополнение»: красная зона → 0; иначе, если ADU > 0, запуск через
	  max(0, NFP/ADU − RT) дней от сегодня → week_index; ADU ≤ 0 → 0.
	"""
	if signal.get("status") == "In Work":
		return 0

	rt_days = float(signal.get("rt_days") or 0.0)

	if signal.get("signal_type") == "Под график":
		need_date = _as_date(signal.get("need_date"))
		if need_date is None:
			return 0
		launch = need_date - timedelta(days=int(round(rt_days)))
		return week_index(launch, today)

	# Пополнение.
	if signal.get("zone") == "Red":
		return 0
	adu = float(signal.get("adu") or 0.0)
	if adu <= 0:
		return 0
	n = _as_date(today)
	if n is None:
		return 0
	nfp = float(signal.get("nfp") or 0.0)
	days = max(0.0, nfp / adu - rt_days)
	launch = n + timedelta(days=int(days))
	return week_index(launch, today)


def build_group_load(signals: list[dict], week_count: int, today: Any) -> dict:
	"""Свернуть сигналы очереди в загрузку групп по неделям (§4.3).

	Каждый сигнал несёт item, qty, status, signal_type, zone, nfp, adu,
	rt_days, need_date и ops — список операций
	[{"group", "minutes_per_unit", "source"}] (операции сборочных групп
	уже отфильтрованы адаптером: group is None не приходит). Часы сигнала
	по группе = qty × Σ minutes_per_unit(группы) / 60. Неделя запуска ≥
	week_count уходит в ведро "later".

	Возврат: {"groups": {group: {"cells": [часы по неделям], "later_hours",
	"total_hours"}}, "signals_without_norms": int, "other_hours": float}.
	Чистая, детерминированная функция (pytest без Frappe).
	"""
	groups: dict[str, dict] = {}
	signals_without_norms = 0
	other_hours = 0.0

	for signal in signals:
		real_ops = [op for op in (signal.get("ops") or []) if op.get("group")]
		if not real_ops:
			# Ни одной операции с нормативом (нет BOM/операций) — деталь мимо загрузки.
			signals_without_norms += 1
			continue

		qty = float(signal.get("qty") or 0.0)
		week = launch_week_for_signal(signal, today)

		minutes_by_group: dict[str, float] = defaultdict(float)
		for op in real_ops:
			minutes_by_group[op["group"]] += float(op.get("minutes_per_unit") or 0.0)

		for group, minutes in minutes_by_group.items():
			hours = qty * minutes / 60.0
			bucket = groups.get(group)
			if bucket is None:
				bucket = {"cells": [0.0] * week_count, "later_hours": 0.0, "total_hours": 0.0}
				groups[group] = bucket
			if week >= week_count:
				bucket["later_hours"] += hours
			else:
				bucket["cells"][week] += hours
			bucket["total_hours"] += hours
			if group == "other":
				other_hours += hours

	return {
		"groups": groups,
		"signals_without_norms": signals_without_norms,
		"other_hours": round(other_hours, 6),
	}

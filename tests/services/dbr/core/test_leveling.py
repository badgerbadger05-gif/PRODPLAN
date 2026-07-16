"""Тесты ядра выравнивания — нагрузочная модель (чистый pytest, без Frappe).

Решение заказчика 03.07: такты семейств одного участка индивидуальны
(толкач 10/день, К8 12/день, ЛМ 5/день). Свойства модели:
- одна штука семейства f стоит 1/takt_f нагрузки, день участка = 1.0;
- сумма слотов + carried_over = сумме входа;
- нагрузка каждого дня ≤ 1.0;
- детерминированность; чередование SKU.

Запуск: `python -m pytest prodflow/services/drum/test_leveling.py`
из корня приложения prodflow.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.dbr.core.drum.leveling import CapacityGap, Slot, level


def _weekdays(year: int, month: int, holidays: set[date] | None = None) -> list[date]:
	"""Рабочие дни месяца: Пн–Пт минус праздники (в проде — Holiday List)."""
	holidays = holidays or set()
	day = date(year, month, 1)
	out = []
	while day.month == month:
		if day.weekday() < 5 and day not in holidays:
			out.append(day)
		day += timedelta(days=1)
	return out


FAMILIES = {"K15": ("Участок 1", 5.0)}


def _total_by_sku(slots: list[Slot]) -> dict[tuple[str, str], int]:
	out: dict[tuple[str, str], int] = {}
	for s in slots:
		out[(s.family, s.sku)] = out.get((s.family, s.sku), 0) + s.qty
	return out


def _day_loads(slots: list[Slot], families: dict) -> dict[tuple[date, str], float]:
	"""Нагрузка (date, workstation): Σ qty/takt по слотам дня."""
	out: dict[tuple[date, str], float] = {}
	for s in slots:
		takt = families[s.family][1]
		key = (s.date, s.workstation)
		out[key] = out.get(key, 0.0) + s.qty / takt
	return out


def _assert_daily_load_ok(result, families: dict, workdays: dict) -> None:
	"""Нагрузка каждого дня ≤ 1.0 без исключений."""
	for (day, ws), load in _day_loads(result.slots, families).items():
		assert load <= 1.0 + 1e-9, f"{day} {ws}: нагрузка {load}"


class TestLevelingBasics:
	def test_volume_fits_capacity(self):
		days = _weekdays(2026, 7)
		workdays = {"2026-07": days}
		result = level(
			volumes=[("K15", "K15-Dinking", 40, "2026-07"), ("K15", "K15-Lifan", 6, "2026-07")],
			workdays=workdays,
			families=FAMILIES,
		)
		assert not result.gaps
		assert not result.carried_over
		assert _total_by_sku(result.slots) == {("K15", "K15-Dinking"): 40, ("K15", "K15-Lifan"): 6}
		assert set(s.date for s in result.slots) <= set(days)
		_assert_daily_load_ok(result, FAMILIES, workdays)

	def test_daily_output_is_level(self):
		"""Разброс дневных сумм не больше 1 шт (одно семейство, спрос влезает)."""
		days = _weekdays(2026, 7)
		result = level(
			volumes=[("K15", "K15-Dinking", 47, "2026-07")],
			workdays={"2026-07": days},
			families=FAMILIES,
		)
		per_day = [sum(s.qty for s in result.slots if s.date == d) for d in days]
		assert max(per_day) - min(per_day) <= 1

	def test_sku_mix_interleaves(self):
		"""Варианты чередуются, а не идут блоками: при равных долях каждый день содержит оба SKU."""
		days = _weekdays(2026, 7)[:5]
		result = level(
			volumes=[("K15", "A", 5, "2026-07"), ("K15", "B", 5, "2026-07")],
			workdays={"2026-07": days},
			families={"K15": ("Участок 1", 2.0)},
		)
		for d in days:
			day_skus = {s.sku: s.qty for s in result.slots if s.date == d}
			assert day_skus == {"A": 1, "B": 1}

	def test_holidays_excluded(self):
		holidays = {date(2026, 7, 6), date(2026, 7, 7)}
		days = _weekdays(2026, 7, holidays)
		result = level(
			volumes=[("K15", "K15-Dinking", 30, "2026-07")],
			workdays={"2026-07": days},
			families=FAMILIES,
		)
		slot_dates = {s.date for s in result.slots}
		assert not slot_dates & holidays
		assert slot_dates <= set(days)


class TestMixedTakts:
	"""Семейства одного участка с разными тактами (решение 03.07)."""

	def test_different_takts_fit(self):
		"""Толкач 10/день + ЛМ 5/день, по 20 шт на 10 раб. дней: 2+4 = 6 дней нагрузки — влезает."""
		days = _weekdays(2026, 7)[:10]
		workdays = {"2026-07": days}
		families = {"Толкач": ("Участок 1", 10.0), "ЛМ": ("Участок 1", 5.0)}
		result = level(
			volumes=[("Толкач", "T-1", 20, "2026-07"), ("ЛМ", "LM-100", 20, "2026-07")],
			workdays=workdays,
			families=families,
		)
		assert not result.gaps
		assert not result.carried_over
		assert _total_by_sku(result.slots) == {("Толкач", "T-1"): 20, ("ЛМ", "LM-100"): 20}
		# запас ёмкости большой — лимит держится во все дни, включая последний
		for load in _day_loads(result.slots, families).values():
			assert load <= 1.0 + 1e-9

	def test_three_takts_on_one_workstation(self):
		"""Пример заказчика: толкач 10, К8 12, ЛМ 5 на одном участке — валидно."""
		days = _weekdays(2026, 7)[:10]
		workdays = {"2026-07": days}
		families = {
			"Толкач": ("Участок 1", 10.0),
			"К8": ("Участок 1", 12.0),
			"ЛМ": ("Участок 1", 5.0),
		}
		result = level(
			volumes=[
				("Толкач", "T-1", 20, "2026-07"),
				("К8", "K8-1", 24, "2026-07"),
				("ЛМ", "LM-100", 15, "2026-07"),
			],
			workdays=workdays,
			families=families,
		)
		assert not result.gaps
		assert not result.carried_over
		total_out = sum(s.qty for s in result.slots)
		assert total_out == 20 + 24 + 15
		_assert_daily_load_ok(result, families, workdays)

	def test_different_takts_overflow_cut_proportional_to_load(self):
		"""Толкач 60 + ЛМ 40 на 10 дней: нагрузка 6+8 = 14 > 10 — режется пропорционально нагрузке."""
		days = _weekdays(2026, 7)[:10]
		workdays = {"2026-07": days}
		families = {"Толкач": ("Участок 1", 10.0), "ЛМ": ("Участок 1", 5.0)}
		result = level(
			volumes=[("Толкач", "T-1", 60, "2026-07"), ("ЛМ", "LM-100", 40, "2026-07")],
			workdays=workdays,
			families=families,
		)
		totals = _total_by_sku(result.slots)
		# идеал в нагрузке: по 10/14 от спроса → Толкач 42.86, ЛМ 28.57;
		# целочисленный добор по дефициту нагрузки → 42 и 29 (нагрузка ровно 10.0)
		assert totals[("Толкач", "T-1")] == 42
		assert totals[("ЛМ", "LM-100")] == 29
		assert result.carried_over == {("ЛМ", "LM-100"): 11, ("Толкач", "T-1"): 18}
		# ничего не теряется
		assert sum(totals.values()) + sum(result.carried_over.values()) == 100
		# разрывы по-семейственные и сходятся с обрезкой
		gap_by_family: dict[str, int] = {}
		for gap in result.gaps:
			assert gap.workstation == "Участок 1"
			gap_by_family[gap.family] = gap_by_family.get(gap.family, 0) + gap.gap_qty
		assert gap_by_family == {"Толкач": 18, "ЛМ": 11}
		_assert_daily_load_ok(result, families, workdays)

	def test_fractional_takt_supported(self):
		"""Дробный такт валиден, но физический день не принимает шестую целую штуку."""
		days = _weekdays(2026, 7)[:5]
		workdays = {"2026-07": days}
		families = {"K15": ("Участок 1", 5.5)}
		result = level(
			volumes=[("K15", "K15-Dinking", 27, "2026-07")],
			workdays=workdays,
			families=families,
		)
		assert sum(s.qty for s in result.slots) == 25
		assert result.carried_over == {("K15", "K15-Dinking"): 2}
		assert sum(g.gap_qty for g in result.gaps) == 2
		_assert_daily_load_ok(result, families, workdays)


class TestCapacityOverflow:
	def test_excess_carries_to_next_month(self):
		"""Превышение ёмкости: режется, избыток уезжает в следующий месяц, gap фиксируется."""
		july = _weekdays(2026, 7)[:4]  # ёмкость = 4 дня × такт 5 = 20 шт
		august = _weekdays(2026, 8)
		result = level(
			volumes=[("K15", "K15-Dinking", 26, "2026-07")],
			workdays={"2026-07": july, "2026-08": august},
			families=FAMILIES,
		)
		july_qty = sum(s.qty for s in result.slots if s.date.month == 7)
		august_qty = sum(s.qty for s in result.slots if s.date.month == 8)
		assert july_qty == 20
		assert august_qty == 6
		assert sum(g.gap_qty for g in result.gaps) == 6
		assert all(g.date.month == 7 for g in result.gaps)
		assert not result.carried_over

	def test_no_day_exceeds_load(self):
		july = _weekdays(2026, 7)[:4]
		august = _weekdays(2026, 8)
		workdays = {"2026-07": july, "2026-08": august}
		result = level(
			volumes=[("K15", "K15-Dinking", 26, "2026-07"), ("K15", "K15-Lifan", 90, "2026-08")],
			workdays=workdays,
			families=FAMILIES,
		)
		_assert_daily_load_ok(result, FAMILIES, workdays)

	def test_overflow_beyond_horizon_reported(self):
		"""Количество не теряется никогда: остаток за горизонтом — в carried_over."""
		july = _weekdays(2026, 7)[:2]  # ёмкость = 2 дня × такт 5 = 10 шт
		result = level(
			volumes=[("K15", "K15-Dinking", 25, "2026-07")],
			workdays={"2026-07": july},
			families=FAMILIES,
		)
		scheduled = sum(s.qty for s in result.slots)
		assert scheduled == 10
		assert result.carried_over == {("K15", "K15-Dinking"): 15}
		assert scheduled + sum(result.carried_over.values()) == 25

	def test_two_families_share_workstation_proportionally(self):
		"""Ёмкость участка делится пропорционально нагрузке семейств (равные такты — как объёмам)."""
		days = _weekdays(2026, 7)[:5]  # ёмкость в нагрузке = 5 дней; 5 × 6 = 30 шт при такте 6
		workdays = {"2026-07": days}
		families = {"EKR": ("Участок модулей", 6.0), "LM": ("Участок модулей", 6.0)}
		result = level(
			volumes=[("EKR", "EKR-500", 40, "2026-07"), ("LM", "LM-100", 20, "2026-07")],
			workdays=workdays,
			families=families,
		)
		totals = _total_by_sku(result.slots)
		# 30 мощности на 60 спроса → доли 2:1
		assert totals[("EKR", "EKR-500")] == 20
		assert totals[("LM", "LM-100")] == 10
		assert result.carried_over == {("EKR", "EKR-500"): 20, ("LM", "LM-100"): 10}
		_assert_daily_load_ok(result, families, workdays)

	def test_gap_rows_are_per_family(self):
		"""Разрыв читается менеджментом: «в день D на участке W не влезает N шт семейства F»."""
		july = _weekdays(2026, 7)[:4]
		result = level(
			volumes=[("K15", "K15-Dinking", 26, "2026-07")],
			workdays={"2026-07": july},
			families=FAMILIES,
		)
		assert result.gaps
		for gap in result.gaps:
			assert isinstance(gap, CapacityGap)
			assert gap.workstation == "Участок 1"
			assert gap.family == "K15"
			assert gap.takt_qty == 5.0  # такт семейства
			assert gap.gap_qty > 0
			assert gap.required_qty - gap.gap_qty <= 5


class TestInvariants:
	def test_totals_always_converge(self):
		"""Сумма слотов + carried_over = сумме входа (ничего не теряется и не дублируется)."""
		volumes = [
			("K15", "K15-Dinking", 153, "2026-07"),
			("K15", "K15-Lifan", 1, "2026-07"),
			("K15", "K15-Dinking", 90, "2026-08"),
			("EKR", "EKR-500", 77, "2026-07"),
			("EKR", "EKR-700", 33, "2026-08"),
			("LM", "LM-100", 25, "2026-09"),
		]
		families = {
			"K15": ("Участок 1", 5.0),
			"EKR": ("Участок 2", 6.0),
			"LM": ("Участок 2", 4.5),  # разные такты одного участка валидны
		}
		workdays = {f"2026-0{m}": _weekdays(2026, m) for m in (7, 8, 9)}
		result = level(volumes, workdays, families)
		total_in = sum(v[2] for v in volumes)
		total_out = sum(s.qty for s in result.slots) + sum(result.carried_over.values())
		assert total_in == total_out
		_assert_daily_load_ok(result, families, workdays)

	def test_deterministic(self):
		volumes = [
			("K15", "K15-Dinking", 47, "2026-07"),
			("K15", "K15-Lifan", 13, "2026-07"),
			("EKR", "EKR-500", 88, "2026-07"),
		]
		families = {"K15": ("Участок 1", 5.0), "EKR": ("Участок 2", 6.0)}
		workdays = {"2026-07": _weekdays(2026, 7)}
		first = level(list(volumes), workdays, families)
		second = level(list(reversed(volumes)), workdays, families)
		assert first == second


class TestValidation:
	def test_unknown_family_raises(self):
		with pytest.raises(ValueError, match="Семейство"):
			level([("X", "X-1", 5, "2026-07")], {"2026-07": _weekdays(2026, 7)}, FAMILIES)

	def test_month_without_calendar_raises(self):
		with pytest.raises(ValueError, match="календаре"):
			level([("K15", "K15-Dinking", 5, "2026-08")], {"2026-07": _weekdays(2026, 7)}, FAMILIES)

	def test_fractional_qty_raises(self):
		with pytest.raises(ValueError, match="Дробное"):
			level([("K15", "K15-Dinking", 2.5, "2026-07")], {"2026-07": _weekdays(2026, 7)}, FAMILIES)

	def test_takt_below_one_raises(self):
		families = {"K15": ("Участок 1", 0.5)}
		with pytest.raises(ValueError, match="≥ 1"):
			level([("K15", "K15-Dinking", 5, "2026-07")], {"2026-07": _weekdays(2026, 7)}, families)

	def test_month_without_workdays_carries_forward(self):
		"""Месяц без рабочих дней: объём не теряется, уезжает в следующий месяц."""
		result = level(
			volumes=[("K15", "K15-Dinking", 8, "2026-07")],
			workdays={"2026-07": [], "2026-08": _weekdays(2026, 8)},
			families=FAMILIES,
		)
		assert sum(s.qty for s in result.slots) == 8
		assert all(s.date.month == 8 for s in result.slots)

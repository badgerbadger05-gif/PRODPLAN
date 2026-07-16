"""Тесты переноса невыполненных плиток вправо (чистое ядро, без Frappe)."""

from __future__ import annotations

from datetime import date

from app.services.dbr.core.drum.rollforward import (
	OverdueSlot,
	is_closed,
	plan_rollforward,
	remaining_qty,
	slot_load,
)

WS = "Сборка"
TAKTS = {"K15": (WS, 3.0), "K8": (WS, 4.0)}
DAYS = [date(2026, 7, d) for d in (8, 9, 10)]


class TestClosure:
	def test_closed_when_produced_reaches_qty(self):
		assert is_closed(5, 5) is True
		assert is_closed(5, 6) is True

	def test_open_while_underproduced(self):
		assert is_closed(5, 4) is False
		assert is_closed(5, 0) is False

	def test_remaining_never_negative(self):
		assert remaining_qty(5, 2) == 3
		assert remaining_qty(5, 7) == 0


class TestSlotLoad:
	def test_load_is_qty_over_takt(self):
		assert slot_load(3, 3.0) == 1.0
		assert slot_load(2, 4.0) == 0.5

	def test_unknown_takt_is_zero_load(self):
		"""Изделие не назначено на участок — нагрузку не считаем, плитку не теряем."""
		assert slot_load(5, 0) == 0.0


class TestPlanRollforward:
	def test_moves_to_first_free_day(self):
		overdue = [OverdueSlot("s1", date(2026, 7, 1), WS, "K15", 3)]
		moves = plan_rollforward(overdue, DAYS, TAKTS, {})
		assert moves == [("s1", DAYS[0], False)]

	def test_respects_existing_load(self):
		"""День уже занят на 100% будущими плитками — едем на следующий."""
		load = {(WS, DAYS[0]): 1.0}
		overdue = [OverdueSlot("s1", date(2026, 7, 1), WS, "K15", 3)]
		moves = plan_rollforward(overdue, DAYS, TAKTS, load)
		assert moves[0].to_date == DAYS[1]

	def test_cascades_when_days_fill_up(self):
		"""Три полных плитки — три дня подряд, каскадом."""
		overdue = [
			OverdueSlot("a", date(2026, 7, 1), WS, "K15", 3),
			OverdueSlot("b", date(2026, 7, 2), WS, "K15", 3),
			OverdueSlot("c", date(2026, 7, 3), WS, "K15", 3),
		]
		moves = plan_rollforward(overdue, DAYS, TAKTS, {})
		assert [m.to_date for m in moves] == DAYS
		assert not any(m.overloaded for m in moves)

	def test_fifo_by_planned_date(self):
		"""Разбор по исходной плановой дате: июньская плитка едет раньше июльской."""
		overdue = [
			OverdueSlot("july", date(2026, 7, 3), WS, "K15", 3),
			OverdueSlot("june", date(2026, 6, 1), WS, "K15", 3),
		]
		moves = plan_rollforward(overdue, DAYS, TAKTS, {})
		by_name = {m.name: m.to_date for m in moves}
		assert by_name["june"] == DAYS[0]
		assert by_name["july"] == DAYS[1]

	def test_partial_loads_share_a_day(self):
		"""K15 (1 шт = 1/3 дня) и K8 (2 шт = 1/2 дня) помещаются вместе."""
		overdue = [
			OverdueSlot("a", date(2026, 7, 1), WS, "K15", 1),
			OverdueSlot("b", date(2026, 7, 1), WS, "K8", 2),
		]
		moves = plan_rollforward(overdue, DAYS, TAKTS, {})
		assert {m.to_date for m in moves} == {DAYS[0]}

	def test_oversized_slot_is_placed_and_flagged(self):
		"""Плитка тяжелее дня не дробится: ставим и помечаем перегруз."""
		overdue = [OverdueSlot("big", date(2026, 7, 1), WS, "K15", 9)]
		moves = plan_rollforward(overdue, DAYS, TAKTS, {})
		assert moves == [("big", DAYS[0], True)]

	def test_no_free_day_in_horizon_is_flagged_not_dropped(self):
		load = {(WS, d): 1.0 for d in DAYS}
		overdue = [OverdueSlot("s1", date(2026, 7, 1), WS, "K15", 3)]
		moves = plan_rollforward(overdue, DAYS, TAKTS, load)
		assert len(moves) == 1
		assert moves[0].overloaded is True

	def test_overload_goes_to_least_loaded_day(self):
		"""Перегруз не сваливаем на сегодня — ищем наименее загруженный день."""
		load = {(WS, DAYS[0]): 1.0, (WS, DAYS[1]): 0.9, (WS, DAYS[2]): 1.0}
		overdue = [OverdueSlot("s1", date(2026, 7, 1), WS, "K15", 3)]
		moves = plan_rollforward(overdue, DAYS, TAKTS, load)
		assert moves == [("s1", DAYS[1], True)]

	def test_overload_spreads_across_days(self):
		"""Несколько неразмещаемых плиток расходятся по дням, а не в одну кучу."""
		load = {(WS, d): 1.0 for d in DAYS}
		overdue = [
			OverdueSlot("a", date(2026, 7, 1), WS, "K15", 3),
			OverdueSlot("b", date(2026, 7, 2), WS, "K15", 3),
			OverdueSlot("c", date(2026, 7, 3), WS, "K15", 3),
		]
		moves = plan_rollforward(overdue, DAYS, TAKTS, load)
		assert all(m.overloaded for m in moves)
		assert sorted(m.to_date for m in moves) == DAYS

	def test_unknown_item_takt_still_moves(self):
		"""Изделие без назначения: нагрузка 0, но плитка не теряется."""
		overdue = [OverdueSlot("s1", date(2026, 7, 1), WS, "НЕИЗВЕСТНО", 5)]
		moves = plan_rollforward(overdue, DAYS, TAKTS, {})
		assert moves == [("s1", DAYS[0], False)]

	def test_other_workstation_load_does_not_block(self):
		load = {("Другой участок", DAYS[0]): 1.0}
		overdue = [OverdueSlot("s1", date(2026, 7, 1), WS, "K15", 3)]
		moves = plan_rollforward(overdue, DAYS, TAKTS, load)
		assert moves[0].to_date == DAYS[0]

	def test_input_load_is_not_mutated(self):
		load = {(WS, DAYS[0]): 0.0}
		plan_rollforward([OverdueSlot("s1", date(2026, 7, 1), WS, "K15", 3)], DAYS, TAKTS, load)
		assert load == {(WS, DAYS[0]): 0.0}

	def test_empty_horizon_moves_nothing(self):
		overdue = [OverdueSlot("s1", date(2026, 7, 1), WS, "K15", 3)]
		assert plan_rollforward(overdue, [], TAKTS, {}) == []

	def test_deterministic(self):
		overdue = [
			OverdueSlot("b", date(2026, 7, 1), WS, "K15", 1),
			OverdueSlot("a", date(2026, 7, 1), WS, "K15", 1),
		]
		first = plan_rollforward(overdue, DAYS, TAKTS, {})
		second = plan_rollforward(list(reversed(overdue)), DAYS, TAKTS, {})
		assert first == second

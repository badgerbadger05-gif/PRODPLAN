"""Тесты чистого ядра продления барабана (без Frappe).

Проверяем два чистых решения extend_schedule: дедуп по программе (ключ
идемпотентности) и расширение горизонта графика. Гоняется тем же
`python -m pytest prodflow/services/drum`, что и остальное ядро.
"""

from __future__ import annotations

from datetime import date

from app.services.dbr.core.drum.extend import extend_period_to, is_program_covered


class TestProgramCovered:
	def test_covered_when_program_present(self):
		"""Программа уже среди источников плиток — продлевать повторно нельзя."""
		assert is_program_covered({"PP-2026-00843", "PP-2026-00844"}, "PP-2026-00844") is True

	def test_not_covered_for_new_program(self):
		assert is_program_covered({"PP-2026-00843"}, "PP-2026-00844") is False

	def test_empty_sources_is_never_covered(self):
		"""Первый месяц графика источников-плиток ещё не имеет (или все None)."""
		assert is_program_covered(set(), "PP-2026-00844") is False

	def test_accepts_any_iterable(self):
		"""Вход — любой итерабельный набор source_program плиток, не только set."""
		sources = ["PP-2026-00843", "PP-2026-00843", "PP-2026-00844"]  # с дублями
		assert is_program_covered(sources, "PP-2026-00844") is True
		assert is_program_covered(sources, "PP-2026-00845") is False


class TestExtendPeriodTo:
	def test_grows_to_new_program_end(self):
		"""Июль + август: горизонт растёт до конца августа."""
		assert extend_period_to(date(2026, 7, 31), date(2026, 8, 31)) == date(2026, 8, 31)

	def test_never_shrinks(self):
		"""Программа внутри уже покрытого периода не сдвигает конец назад."""
		assert extend_period_to(date(2026, 8, 31), date(2026, 8, 15)) == date(2026, 8, 31)

	def test_equal_dates_stable(self):
		assert extend_period_to(date(2026, 8, 31), date(2026, 8, 31)) == date(2026, 8, 31)

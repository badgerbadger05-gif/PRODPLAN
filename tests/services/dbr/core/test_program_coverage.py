"""Тесты чистой логики покрытия ячеек программы (без Frappe)."""

from __future__ import annotations

from datetime import date

from app.services.dbr.core.program_coverage import (
    assign_bucket,
    classify_cell,
    coverage_percent,
)


class TestClassifyCell:
    def test_no_plan_is_none(self):
        assert classify_cell(0, 0, 0) == "none"
        assert classify_cell(0, 5, 5) == "none"

    def test_produced_wins_over_covered(self):
        # выпущено ≥ плана — самый сильный статус
        assert classify_cell(10, 10, 10) == "produced"
        assert classify_cell(10, 10, 11) == "produced"

    def test_covered_when_released_reaches_plan(self):
        assert classify_cell(10, 10, 0) == "covered"
        assert classify_cell(10, 12, 3) == "covered"

    def test_partial_when_some_released(self):
        assert classify_cell(10, 4, 0) == "partial"

    def test_open_when_nothing_released(self):
        assert classify_cell(10, 0, 0) == "open"

    def test_eps_tolerance(self):
        assert classify_cell(10, 9.9999999, 0) == "covered"
        assert classify_cell(10, 0, 9.9999999) == "produced"


class TestAssignBucket:
    STARTS = [date(2026, 7, 1), date(2026, 7, 8), date(2026, 7, 15)]
    END = date(2026, 7, 21)

    def test_first_bucket(self):
        assert assign_bucket(date(2026, 7, 1), self.STARTS, self.END) == 0
        assert assign_bucket(date(2026, 7, 7), self.STARTS, self.END) == 0

    def test_middle_bucket(self):
        assert assign_bucket(date(2026, 7, 8), self.STARTS, self.END) == 1
        assert assign_bucket(date(2026, 7, 14), self.STARTS, self.END) == 1

    def test_last_bucket_up_to_period_end(self):
        assert assign_bucket(date(2026, 7, 15), self.STARTS, self.END) == 2
        assert assign_bucket(date(2026, 7, 21), self.STARTS, self.END) == 2

    def test_before_first_start_is_out(self):
        assert assign_bucket(date(2026, 6, 30), self.STARTS, self.END) is None

    def test_after_period_end_is_out(self):
        assert assign_bucket(date(2026, 7, 22), self.STARTS, self.END) is None

    def test_no_period_end_extends_last_bucket(self):
        assert assign_bucket(date(2027, 1, 1), self.STARTS, None) == 2

    def test_empty_starts_is_none(self):
        assert assign_bucket(date(2026, 7, 1), [], self.END) is None

    def test_none_date_is_none(self):
        assert assign_bucket(None, self.STARTS, self.END) is None


class TestCoveragePercent:
    def test_zero_plan_is_zero(self):
        assert coverage_percent(0, 0) == 0

    def test_half(self):
        assert coverage_percent(10, 5) == 50

    def test_rounds(self):
        assert coverage_percent(3, 1) == 33

    def test_overrelease_exceeds_hundred(self):
        assert coverage_percent(10, 15) == 150

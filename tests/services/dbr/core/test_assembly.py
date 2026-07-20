"""Тесты такта от участка и мощности (чистое ядро, без Frappe)."""

from __future__ import annotations

from app.services.dbr.core.drum.assembly import (
	Assignment,
	build_sku_takts,
	effective_takt,
	validate_assignments,
)


class TestEffectiveTakt:
	def test_owner_example_k15_k8(self):
		"""Пример владельца: К15=3, К8=4 на единицу мощности."""
		assert effective_takt(3, 2) == 6.0
		assert effective_takt(4, 2) == 8.0
		assert effective_takt(3, 3) == 9.0
		assert effective_takt(4, 3) == 12.0

	def test_missing_capacity_means_one(self):
		"""Мощность не задана — участок работает «в одно лицо», а не в ноль."""
		assert effective_takt(5, None) == 5.0
		assert effective_takt(5, 0) == 5.0

	def test_fractional_rate(self):
		assert effective_takt(2.5, 2) == 5.0


class TestBuildSkuTakts:
	def test_group_is_sku_with_own_takt(self):
		"""Группа выравнивания = сам SKU, у каждого свой такт."""
		out = build_sku_takts(
			[Assignment("Сборка МБ", "K15", 3), Assignment("Сборка МБ", "K8", 4)],
			{"Сборка МБ": 2},
		)
		assert out == {"K15": ("Сборка МБ", 6.0), "K8": ("Сборка МБ", 8.0)}

	def test_different_workstations_have_own_capacity(self):
		out = build_sku_takts(
			[Assignment("Сборка МБ", "K15", 3), Assignment("Сборка СН", "IH", 1)],
			{"Сборка МБ": 2, "Сборка СН": 5},
		)
		assert out["K15"] == ("Сборка МБ", 6.0)
		assert out["IH"] == ("Сборка СН", 5.0)

	def test_empty(self):
		assert build_sku_takts([], {}) == {}

	def test_duplicate_item_does_not_crash(self):
		"""Кривые данные ловит валидация; пересборка графика на них не падает."""
		out = build_sku_takts(
			[Assignment("A", "K15", 3), Assignment("B", "K15", 4)], {"A": 1, "B": 1}
		)
		assert out["K15"] == ("B", 4.0)


class TestValidateAssignments:
	def test_clean_config_has_no_errors(self):
		assert validate_assignments([Assignment("Сборка", "K15", 3)], {"Сборка": 2}) == []

	def test_item_on_two_workstations(self):
		errors = validate_assignments(
			[Assignment("A", "K15", 3), Assignment("B", "K15", 3)], {"A": 1, "B": 1}
		)
		assert len(errors) == 1
		assert "несколько участков" in errors[0]

	def test_non_positive_rate(self):
		errors = validate_assignments([Assignment("Сборка", "K15", 0)], {"Сборка": 2})
		assert any("больше нуля" in e for e in errors)

	def test_takt_below_one_is_rejected(self):
		"""Штука медленнее суток не влезает в дневной лимит нагрузки."""
		errors = validate_assignments([Assignment("Сборка", "Снегоход", 0.5)], {"Сборка": 1})
		assert any("меньше 1" in e for e in errors)

	def test_capacity_can_lift_takt_above_one(self):
		"""Та же норма при мощности 2 уже допустима: 0.5 × 2 = 1."""
		assert validate_assignments([Assignment("Сборка", "Снегоход", 0.5)], {"Сборка": 2}) == []

	def test_zero_rate_reported_once_without_takt_error(self):
		"""Нулевая норма — одна ошибка, а не две (такт не считаем)."""
		errors = validate_assignments([Assignment("Сборка", "X", 0)], {"Сборка": 1})
		assert len(errors) == 1

	def test_errors_are_deterministic(self):
		a = validate_assignments(
			[Assignment("W", "B", 0), Assignment("W", "A", 0)], {"W": 1}
		)
		b = validate_assignments(
			[Assignment("W", "A", 0), Assignment("W", "B", 0)], {"W": 1}
		)
		assert a == b

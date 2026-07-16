"""Тесты обрезанной спеки (кита) — чистый pytest, без Frappe."""

from __future__ import annotations

import pytest

from app.services.dbr.core.drum.kit import (
	FASTENER,
	RECURSE,
	UNDER_SCHEDULE,
	W2,
	W3,
	W4,
	KitLine,
	build_kit,
)

W2_NAME = "Склад №2"
W3_NAME = "Склад №3"
W4_NAME = "Склад №4"

# BOM снегохода: узловая сборка, окрашенная деталь, закупная, крепёж,
# фантомная группировка, разворачивающаяся до окрашенной детали и метиза.
BOMS = {
	"SNEG-100": [("MOD-LYZH", 1), ("RAMA-OKR", 1), ("PODSHIPNIK", 4), ("BOLT-M8", 12), ("KIT-PHANTOM", 1)],
	"KIT-PHANTOM": [("KAPOT-OKR", 1), ("GAYKA-M6", 6)],
	"KAPOT-OKR": [("KAPOT-RAW", 1)],  # ниже границы — не должен разворачиваться
}

CLASSES = {
	"MOD-LYZH": (W4, W4_NAME),
	"RAMA-OKR": (W3, W3_NAME),
	"PODSHIPNIK": (W4, W4_NAME),
	"BOLT-M8": (FASTENER, None),
	"GAYKA-M6": (FASTENER, None),
	"KIT-PHANTOM": (RECURSE, None),
	"KAPOT-OKR": (W3, W3_NAME),
	"REDKAYA": (UNDER_SCHEDULE, W4_NAME),
}


def _components(item: str) -> list[tuple[str, float]]:
	return [(c, float(q)) for c, q in BOMS.get(item, [])]


def _classify(item: str) -> tuple[str, str | None]:
	return CLASSES[item]


class TestBuildKit:
	def test_cuts_bom_at_first_boundary(self):
		kit = build_kit("SNEG-100", _components, _classify)
		items = {line.item for line in kit}
		assert "KAPOT-OKR" in items  # фантом развёрнут до границы
		assert "KAPOT-RAW" not in items  # ниже границы не разворачиваем
		assert "KIT-PHANTOM" not in items  # сама группировка в кит не входит

	def test_fasteners_excluded(self):
		kit = build_kit("SNEG-100", _components, _classify)
		items = {line.item for line in kit}
		assert not items & {"BOLT-M8", "GAYKA-M6"}

	def test_warehouses_assigned_by_boundary(self):
		kit = {line.item: line for line in build_kit("SNEG-100", _components, _classify)}
		assert kit["RAMA-OKR"].source_warehouse == W3_NAME
		assert kit["KAPOT-OKR"].source_warehouse == W3_NAME
		assert kit["MOD-LYZH"].source_warehouse == W4_NAME
		assert kit["PODSHIPNIK"].source_warehouse == W4_NAME
		assert kit["PODSHIPNIK"].qty_per_unit == 4

	def test_under_schedule_flagged(self):
		boms = {"SKU": [("REDKAYA", 2)]}
		kit = build_kit("SKU", lambda i: [(c, float(q)) for c, q in boms.get(i, [])], _classify)
		assert kit == [KitLine("REDKAYA", 2.0, W4_NAME, True)]

	def test_phantom_multiplier_propagates(self):
		boms = {"SKU": [("PH", 3)], "PH": [("KAPOT-OKR", 2)]}
		classes = {"PH": (RECURSE, None), "KAPOT-OKR": (W3, W3_NAME)}
		kit = build_kit(
			"SKU",
			lambda i: [(c, float(q)) for c, q in boms.get(i, [])],
			lambda i: classes[i],
		)
		assert kit == [KitLine("KAPOT-OKR", 6.0, W3_NAME, False)]

	def test_duplicate_lines_aggregate(self):
		boms = {"SKU": [("PH1", 1), ("RAMA-OKR", 1)], "PH1": [("RAMA-OKR", 2)]}
		classes = {"PH1": (RECURSE, None), "RAMA-OKR": (W3, W3_NAME)}
		kit = build_kit(
			"SKU",
			lambda i: [(c, float(q)) for c, q in boms.get(i, [])],
			lambda i: classes[i],
		)
		assert kit == [KitLine("RAMA-OKR", 3.0, W3_NAME, False)]

	def test_bom_cycle_raises(self):
		boms = {"SKU": [("A", 1)], "A": [("SKU", 1)]}
		classes = {"A": (RECURSE, None), "SKU": (RECURSE, None)}
		with pytest.raises(ValueError, match="Цикл"):
			build_kit(
				"SKU",
				lambda i: [(c, float(q)) for c, q in boms.get(i, [])],
				lambda i: classes[i],
			)

	def test_boundary_without_warehouse_raises(self):
		boms = {"SKU": [("A", 1)]}
		with pytest.raises(ValueError, match="не вернул склад"):
			build_kit(
				"SKU",
				lambda i: [(c, float(q)) for c, q in boms.get(i, [])],
				lambda i: (W3, None),
			)


class TestBlankBufferBoundaryW2:
	"""Заготовка-буфер №2 — граница развязки: кит останавливается на ней
	(как W3/W4), ниже BOM не разворачивается, склад-источник = №2."""

	def test_w2_stops_the_walk(self):
		# ZAG-W2 сдаётся на №2 и имеет свой BOM (сырьё), но за границу не идём.
		boms = {"SKU": [("ZAG-W2", 2)], "ZAG-W2": [("PRUT-RAW", 3)]}
		classes = {"ZAG-W2": (W2, W2_NAME), "PRUT-RAW": (W4, W4_NAME)}
		kit = build_kit(
			"SKU",
			lambda i: [(c, float(q)) for c, q in boms.get(i, [])],
			lambda i: classes[i],
		)
		# Заготовка в ките со складом №2; её сырьё PRUT-RAW НЕ развёрнуто.
		assert kit == [KitLine("ZAG-W2", 2.0, W2_NAME, False)]

	def test_w2_boundary_without_warehouse_raises(self):
		boms = {"SKU": [("ZAG-W2", 1)]}
		with pytest.raises(ValueError, match="не вернул склад"):
			build_kit(
				"SKU",
				lambda i: [(c, float(q)) for c, q in boms.get(i, [])],
				lambda i: (W2, None),
			)

	def test_w2_aggregates_and_multiplier_propagates(self):
		# Заготовка приходит и напрямую, и через фантом — суммируется, кратность копится.
		boms = {"SKU": [("ZAG-W2", 1), ("PH", 2)], "PH": [("ZAG-W2", 3)]}
		classes = {"ZAG-W2": (W2, W2_NAME), "PH": (RECURSE, None)}
		kit = build_kit(
			"SKU",
			lambda i: [(c, float(q)) for c, q in boms.get(i, [])],
			lambda i: classes[i],
		)
		assert kit == [KitLine("ZAG-W2", 7.0, W2_NAME, False)]  # 1 + 2×3

"""Тесты многоуровневого разворота спроса (чистое ядро, без Frappe)."""

from __future__ import annotations

from app.services.dbr.core.feeder.demand_explosion import (
	BOUNDARY,
	RECURSE,
	SKIP,
	Node,
	explode_kit,
)


def _components(tree):
	"""tree: {item: [(child, qty), ...]} → components_of."""
	return lambda item: tree.get(item, [])


def _classify(nodes):
	"""nodes: {item: Node} → classify (незнакомая деталь = терминал-лист W4)."""
	return lambda item: nodes.get(item, Node(BOUNDARY, "№4", explode_through=False))


# Топология: изделие → узловая сборка №4 → заготовка №2 → пруток (закуп).
TREE = {
	"ИЗДЕЛИЕ": [("СБОРКА4", 1.0)],
	"СБОРКА4": [("ЗАГОТОВКА2", 2.0)],
	"ЗАГОТОВКА2": [("ПРУТОК", 3.0)],
}
NODES = {
	"СБОРКА4": Node(BOUNDARY, "№4", explode_through=True),   # производится, идём сквозь
	"ЗАГОТОВКА2": Node(BOUNDARY, "№2", explode_through=True),  # заготовка-буфер, сквозь
	"ПРУТОК": Node(BOUNDARY, "№4", explode_through=False),     # закуп: терминал
}


def _by_item(lines):
	out = {}
	for l in lines:
		out.setdefault((l.item, l.warehouse), 0.0)
		out[(l.item, l.warehouse)] += l.qty_per_unit
	return out


class TestMultilevel:
	def test_reaches_nested_blank(self):
		lines = explode_kit("ИЗДЕЛИЕ", _components(TREE), _classify(NODES))
		agg = _by_item(lines)
		# СБОРКА4 (1), ЗАГОТОВКА2 (1×2=2), ПРУТОК (2×3=6)
		assert agg[("СБОРКА4", "№4")] == 1.0
		assert agg[("ЗАГОТОВКА2", "№2")] == 2.0
		assert agg[("ПРУТОК", "№4")] == 6.0

	def test_terminal_not_exploded(self):
		"""За закупным прутком дальше не идём даже если у него есть BOM."""
		tree = dict(TREE, ПРУТОК=[("НЕВИДИМ", 5.0)])
		lines = explode_kit("ИЗДЕЛИЕ", _components(tree), _classify(NODES))
		assert all(l.item != "НЕВИДИМ" for l in lines)


class TestSummationAndQty:
	def test_same_blank_two_parents_sums(self):
		tree = {
			"ИЗДЕЛИЕ": [("СБОРКА4", 1.0), ("СБОРКА4B", 1.0)],
			"СБОРКА4": [("ЗАГОТОВКА2", 2.0)],
			"СБОРКА4B": [("ЗАГОТОВКА2", 3.0)],
			"ЗАГОТОВКА2": [],
		}
		nodes = {
			"СБОРКА4": Node(BOUNDARY, "№4", explode_through=True),
			"СБОРКА4B": Node(BOUNDARY, "№4", explode_through=True),
			"ЗАГОТОВКА2": Node(BOUNDARY, "№2", explode_through=True),
		}
		agg = _by_item(explode_kit("ИЗДЕЛИЕ", _components(tree), _classify(nodes)))
		assert agg[("ЗАГОТОВКА2", "№2")] == 5.0  # 2 + 3

	def test_qty_multiplies_along_path(self):
		tree = {"ИЗДЕЛИЕ": [("СБОРКА4", 4.0)], "СБОРКА4": [("ЗАГОТОВКА2", 5.0)], "ЗАГОТОВКА2": []}
		nodes = {
			"СБОРКА4": Node(BOUNDARY, "№4", explode_through=True),
			"ЗАГОТОВКА2": Node(BOUNDARY, "№2", explode_through=True),
		}
		agg = _by_item(explode_kit("ИЗДЕЛИЕ", _components(tree), _classify(nodes)))
		assert agg[("ЗАГОТОВКА2", "№2")] == 20.0


class TestKinds:
	def test_phantom_transparent_no_line(self):
		tree = {"ИЗДЕЛИЕ": [("ФАНТОМ", 2.0)], "ФАНТОМ": [("ЗАГОТОВКА2", 3.0)], "ЗАГОТОВКА2": []}
		nodes = {
			"ФАНТОМ": Node(RECURSE),
			"ЗАГОТОВКА2": Node(BOUNDARY, "№2", explode_through=False),
		}
		lines = explode_kit("ИЗДЕЛИЕ", _components(tree), _classify(nodes))
		agg = _by_item(lines)
		assert ("ФАНТОМ", None) not in agg
		assert all(l.item != "ФАНТОМ" for l in lines)
		assert agg[("ЗАГОТОВКА2", "№2")] == 6.0  # 2×3, спрос пронесён сквозь фантом

	def test_fastener_skipped(self):
		tree = {"ИЗДЕЛИЕ": [("МЕТИЗ", 10.0), ("ЗАГОТОВКА2", 1.0)], "ЗАГОТОВКА2": []}
		nodes = {"МЕТИЗ": Node(SKIP), "ЗАГОТОВКА2": Node(BOUNDARY, "№2")}
		lines = explode_kit("ИЗДЕЛИЕ", _components(tree), _classify(nodes))
		assert all(l.item != "МЕТИЗ" for l in lines)


class TestGuards:
	def test_cycle_broken(self):
		tree = {"A": [("B", 1.0)], "B": [("A", 1.0)]}
		nodes = {
			"A": Node(BOUNDARY, "№2", explode_through=True),
			"B": Node(BOUNDARY, "№2", explode_through=True),
		}
		# Не должно зациклиться.
		lines = explode_kit("A", _components(tree), _classify(nodes))
		assert any(l.item == "B" for l in lines)

	def test_negative_qty_raises(self):
		tree = {"ИЗДЕЛИЕ": [("ЗАГОТОВКА2", -1.0)]}
		try:
			explode_kit("ИЗДЕЛИЕ", _components(tree), _classify({"ЗАГОТОВКА2": Node(BOUNDARY, "№2")}))
			assert False, "ожидалась ошибка на отрицательном qty"
		except ValueError:
			pass

	def test_empty(self):
		assert explode_kit("X", _components({"X": []}), _classify({})) == []

	def test_shared_memo_same_result(self):
		memo: dict = {}
		a = explode_kit("ИЗДЕЛИЕ", _components(TREE), _classify(NODES), memo=memo)
		b = explode_kit("ИЗДЕЛИЕ", _components(TREE), _classify(NODES), memo=memo)
		assert _by_item(a) == _by_item(b)

"""Тесты чистого ядра питателя №2 (зоны, ADU) — pytest без Frappe.

Запуск: `python -m pytest prodflow/services/feeder` из корня приложения.
Параметры примеров — из методики §7 (втулка CP-000077-RT, ступица
VP-000358-ShP) с поправкой RT-классов решения 04.07 (7/15/21).

Портировано из prodflow prodflow/services/feeder/test_feeder_core.py, коммит b1ebde2. Оставлены только классы чистого ядра; классы, зависящие
от frappe-адаптеров (erpnext_stock, nfp_service, release, material_service,
signals), исключены — их тесты вернутся вместе с адаптерами.
"""

from __future__ import annotations

import pytest

from datetime import date, datetime

from app.services.dbr.core.feeder.adu import AduRow, KitLine, build_adu
from app.services.dbr.core.feeder.group_load import (
	GROUP_LABELS,
	GROUP_ORDER,
	build_group_load,
	classify_group,
	launch_week_for_signal,
	week_index,
)
from app.services.dbr.core.feeder.plan_import import PlanLine, parse_plan_matrix, period_totals
from app.services.dbr.core.feeder.signal_identity import build_dedup_key
from app.services.dbr.core.feeder.zones import (
	GREEN,
	RED,
	YELLOW,
	Zones,
	compute_zones,
	has_shelf,
	nfp_zone,
	penetration,
	replenishment_qty,
)


class TestSignalIdentity:
	def test_same_business_signal_has_stable_key(self):
		first = build_dedup_key(
			"Под график", drum_slot="SLOT-1", item="PART-1", warehouse="WH-2"
		)
		second = build_dedup_key(
			"Под график", drum_slot="SLOT-1", item="PART-1", warehouse="WH-2"
		)
		assert first == second
		assert first.startswith("S:")

	def test_distinct_slots_do_not_collide(self):
		first = build_dedup_key(
			"Под график", drum_slot="SLOT-1", item="PART-1", warehouse="WH-2"
		)
		second = build_dedup_key(
			"Под график", drum_slot="SLOT-2", item="PART-1", warehouse="WH-2"
		)
		assert first != second


class TestZones:
	def test_vtolka_example_from_methodology(self):
		# Втулка рычага: ADU 90, токарка RT 7, EPEI 10, партия 250, общая деталь.
		z = compute_zones(adu=90, rt_days=7, batch_days=10, optimal_batch=250, k_var=0.25)
		assert z.yellow == 630
		assert z.green == 900  # ADU × batch_days > optimal_batch
		assert z.red == pytest.approx(157.5)
		assert z.target == pytest.approx(1687.5)

	def test_optimal_batch_wins_when_larger(self):
		z = compute_zones(adu=22, rt_days=21, batch_days=2.5, optimal_batch=100, k_var=0.25)
		assert z.green == 100
		assert z.yellow == 462

	def test_k_crit_adds_quarter_to_red(self):
		base = compute_zones(adu=10, rt_days=7, batch_days=5, k_var=0.5)
		crit = compute_zones(adu=10, rt_days=7, batch_days=5, k_var=0.5, k_crit=True)
		assert crit.red == pytest.approx(base.red * 1.25)
		assert crit.yellow == base.yellow and crit.green == base.green

	def test_monotonic_in_adu(self):
		small = compute_zones(adu=5, rt_days=7, batch_days=5)
		big = compute_zones(adu=10, rt_days=7, batch_days=5)
		assert big.target > small.target

	def test_rejects_negative(self):
		with pytest.raises(ValueError):
			compute_zones(adu=-1, rt_days=7, batch_days=5)


class TestSupplyRiskRedZone:
	"""Страховка «+N% по категориям» (Этап B): множитель ТОЛЬКО красной зоны."""

	def test_zero_pct_is_unchanged(self):
		# Дефолт 0% — красная зона как раньше (обратная совместимость).
		base = compute_zones(adu=10, rt_days=7, batch_days=5, k_var=0.5)
		same = compute_zones(adu=10, rt_days=7, batch_days=5, k_var=0.5, supply_risk_pct=0.0)
		assert same == base

	def test_pct_scales_only_red(self):
		base = compute_zones(adu=10, rt_days=7, batch_days=5, k_var=0.5)
		risky = compute_zones(adu=10, rt_days=7, batch_days=5, k_var=0.5, supply_risk_pct=30)
		assert risky.red == pytest.approx(base.red * 1.30)
		assert risky.yellow == base.yellow  # жёлтая не трогается
		assert risky.green == base.green  # зелёная не трогается
		assert risky.target == pytest.approx(base.red * 1.30 + base.yellow + base.green)

	def test_stacks_with_k_crit(self):
		# Единственный станок (+25%) и риск категории (+10%) перемножаются.
		z = compute_zones(adu=10, rt_days=7, batch_days=5, k_var=0.5, k_crit=True, supply_risk_pct=10)
		assert z.red == pytest.approx(10 * 7 * 0.5 * 1.25 * 1.10)

	def test_rejects_negative_pct(self):
		with pytest.raises(ValueError):
			compute_zones(adu=10, rt_days=7, batch_days=5, supply_risk_pct=-5)


class TestShelf:
	def test_threshold_five(self):
		# Решение 04.07: порог 5 шт за время пополнения.
		assert has_shelf(adu=90, rt_days=7, threshold_qty=5)
		assert has_shelf(adu=1, rt_days=7, threshold_qty=5)  # 7 ≥ 5
		assert not has_shelf(adu=0.5, rt_days=7, threshold_qty=5)  # 3.5 < 5
		assert not has_shelf(adu=0.05, rt_days=21, threshold_qty=5)  # редкая деталь


class TestNfp:
	Z = Zones(red=160, yellow=630, green=900)

	def test_zone_boundaries(self):
		assert nfp_zone(100, self.Z) == RED
		assert nfp_zone(160, self.Z) == RED  # на границе — красная
		assert nfp_zone(500, self.Z) == YELLOW
		assert nfp_zone(790, self.Z) == YELLOW
		assert nfp_zone(791, self.Z) == GREEN

	def test_penetration_scale(self):
		assert penetration(self.Z.target, self.Z) == pytest.approx(0.0)
		assert penetration(0, self.Z) == pytest.approx(1.0)
		assert penetration(-100, self.Z) > 1.0  # минус — глубже пустого

	def test_replenishment_green_is_zero(self):
		assert replenishment_qty(1000, self.Z) == 0.0

	def test_replenishment_rounds_to_multiple(self):
		# NFP 500 (жёлтая): Target − NFP = 1190 → вверх до кратности 250.
		assert replenishment_qty(500, self.Z, multiple=250) == 1250
		assert replenishment_qty(500, self.Z) == 1190

	def test_replenishment_at_least_green_zone(self):
		# Свойство модели: заказ в жёлтой/красной зоне не меньше зелёной
		# зоны (= max(Q, ADU × batch_days)) — квант обеспечен по построению.
		for nfp in (0, 160, 500, 790):
			assert replenishment_qty(nfp, self.Z) >= self.Z.green


class TestAdu:
	def test_sums_across_skus_and_counts_commonality(self):
		kits = {
			"K8": [KitLine("VTULKA", 2, "W4"), KitLine("STUPICA", 1, "W3")],
			"K15": [KitLine("VTULKA", 2, "W4")],
			"RARE": [KitLine("VTULKA", 4, "W4")],  # темп 0 — вклада нет
		}
		rates = {"K8": 4.0, "K15": 3.0, "RARE": 0.0}
		adu = build_adu(kits, rates)
		assert adu[("VTULKA", "W4")] == AduRow(adu=14.0, commonality=2)
		assert adu[("STUPICA", "W3")] == AduRow(adu=4.0, commonality=1)

	def test_same_item_different_warehouse_is_two_positions(self):
		kits = {"A": [KitLine("X", 1, "W3"), KitLine("X", 1, "W4")]}
		adu = build_adu(kits, {"A": 5})
		assert adu[("X", "W3")].adu == 5 and adu[("X", "W4")].adu == 5

	def test_rejects_negative_rate(self):
		with pytest.raises(ValueError):
			build_adu({"A": [KitLine("X", 1, "W4")]}, {"A": -1})

	def test_deterministic(self):
		kits = {"B": [KitLine("X", 1, "W4")], "A": [KitLine("X", 2, "W4")]}
		assert build_adu(kits, {"A": 1, "B": 2}) == build_adu(dict(reversed(list(kits.items()))), {"B": 2, "A": 1})


class TestPlanImport:
	"""Матрица в формате реального плана: месяц-итог + недельные колонки,
	групповые строки без артикула, #REF!, дубликаты артикула."""

	MATRIX = [
		[None, None, None, None],
		[None, "Номенклатура", "Артикул для пр-ва", datetime(2025, 1, 1), 1, 2, datetime(2025, 2, 1), 1, 2, "ИТОГО в ед"],
		[None, "Снегоходы", None, 80, 20, 60, 51, 20, 31, 131],  # группа — без артикула, мимо
		[None, "IH LS 28", "Г0007842", 68, 20, 48, 40, 20, 20, 108],
		[None, "K15 Dinkin", "Г0003202", 20, None, 20, "#REF!", None, None, 20],
		[None, "K15 Dinkin (дубль)", "Г0003202", 5, 5, None, 10, 10, None, 15],
		[None, "EKR22 Long", None, 1, 1, None, 1, 1, None, 2],  # нет артикула — мимо
		[None, "Сидение", "Г0006579", "1 290", None, None, 0, None, None, "1 290"],
	]

	def test_parses_months_skips_groups_and_weeks(self):
		lines = parse_plan_matrix(self.MATRIX, year=2026)
		by_article = {l.article: l for l in lines}
		assert set(by_article) == {"Г0007842", "Г0003202", "Г0006579"}
		assert by_article["Г0007842"].qty_by_month == {"2026-01": 68.0, "2026-02": 40.0}
		assert by_article["Г0007842"].name == "IH LS 28"

	def test_ref_errors_zero_and_duplicates_sum(self):
		lines = parse_plan_matrix(self.MATRIX, year=2026)
		k15 = {l.article: l for l in lines}["Г0003202"]
		# 20 + 5 за январь; #REF! → 0 + 10 за февраль.
		assert k15.qty_by_month == {"2026-01": 25.0, "2026-02": 10.0}

	def test_thousands_separator(self):
		lines = parse_plan_matrix(self.MATRIX, year=2026)
		assert {l.article: l for l in lines}["Г0006579"].qty_by_month["2026-01"] == 1290.0

	def test_string_month_headers(self):
		matrix = [
			["Номенклатура", "Артикул", "янв.-26", "1", "февр.-26"],
			["A", "Г01", 10, 5, 20],
		]
		lines = parse_plan_matrix(matrix, year=2026)
		assert lines[0].qty_by_month == {"2026-01": 10.0, "2026-02": 20.0}

	def test_period_totals(self):
		lines = [PlanLine("Г01", "A", {"2026-07": 10, "2026-08": 5, "2026-09": 0}),
			PlanLine("Г02", "B", {"2026-07": 0, "2026-08": 0})]
		assert period_totals(lines, ["2026-07", "2026-08"]) == {"Г01": 15}

	def test_missing_header_raises(self):
		with pytest.raises(ValueError):
			parse_plan_matrix([["x", "y"], [1, 2]], year=2026)


class TestAvailability:
	"""Обеспеченность очереди: netting одного пула сверху вниз (дизайн §3)."""

	def _need(self, item, need, kind="buy", level=""):
		from app.services.dbr.core.feeder.availability import KitLineNeed

		return KitLineNeed(item=item, need=need, kind=kind, level=level)

	def test_full_kit_is_ready(self):
		from app.services.dbr.core.feeder.availability import evaluate_queue

		kits = [[self._need("A", 10), self._need("B", 5)]]
		res = evaluate_queue(kits, {"A": 100, "B": 50})
		assert res[0].status == "Готов" and res[0].cls == "ok"
		assert res[0].can_launch
		assert res[0].covered == 2 and res[0].total == 2

	def test_deficit_when_nothing_on_hand(self):
		from app.services.dbr.core.feeder.availability import evaluate_queue

		res = evaluate_queue([[self._need("A", 10)]], {})
		assert res[0].status == "Дефицит" and res[0].cls == "no"
		assert not res[0].can_launch
		assert res[0].lines[0].cls == "no"

	def test_partial_when_some_covered(self):
		from app.services.dbr.core.feeder.availability import evaluate_queue

		# A хватает (10≤100), C нет (нет остатка) → Частично 1/2.
		kits = [[self._need("A", 10), self._need("C", 4)]]
		res = evaluate_queue(kits, {"A": 100})
		assert res[0].status == "Частично" and res[0].cls == "part"
		assert res[0].covered == 1 and res[0].total == 2

	def test_partial_line_when_stock_below_need(self):
		from app.services.dbr.core.feeder.availability import evaluate_queue

		# Остаток 6 < потребности 10, но 6 есть на предприятии → строка part.
		res = evaluate_queue([[self._need("A", 10)]], {"A": 6})
		assert res[0].lines[0].cls == "part"
		assert res[0].status == "Дефицит"  # ни одна строка не покрыта целиком

	def test_cumulative_netting_top_down(self):
		from app.services.dbr.core.feeder.availability import evaluate_queue

		# 100 прутка на пул; верхний берёт 80, среднему хватает 20, нижнему — 0.
		kits = [
			[self._need("PRUT", 80)],
			[self._need("PRUT", 20)],
			[self._need("PRUT", 30)],
		]
		res = evaluate_queue(kits, {"PRUT": 100})
		assert res[0].cls == "ok"
		assert res[1].cls == "ok"
		# Нижнему не досталось, хотя на предприятии прутка было достаточно (100≥30)
		# → «Расписан выше» (q), а не «нет» (no).
		assert res[2].status == "Расписан выше" and res[2].cls == "q"
		assert res[2].lines[0].cls == "q"

	def test_reserved_above_vs_genuinely_absent(self):
		from app.services.dbr.core.feeder.availability import evaluate_queue

		# X расписан выше (q); Y на предприятии нет вовсе (no) — разные оси.
		kits = [
			[self._need("X", 100)],
			[self._need("X", 50), self._need("Y", 10)],
		]
		res = evaluate_queue(kits, {"X": 100})
		second = res[1]
		by_item = {ln.item: ln for ln in second.lines}
		assert by_item["X"].cls == "q"   # весь X забрал верхний
		assert by_item["Y"].cls == "no"  # Y нет в принципе
		# Есть и q, и no → не «Расписан выше», а «Дефицит».
		assert second.status == "Дефицит" and second.cls == "no"

	def test_competition_within_single_kit(self):
		from app.services.dbr.core.feeder.availability import evaluate_queue

		# Две строки одного кита на общий код: 30 остатка, первая берёт всё.
		kits = [[self._need("SHARED", 30, level="строка 1"), self._need("SHARED", 10, level="строка 2")]]
		res = evaluate_queue(kits, {"SHARED": 30})
		assert res[0].lines[0].cls == "ok"
		assert res[0].lines[1].cls == "q"  # предприятие имело 30 ≥ 10, но занято строкой выше

	def test_empty_kit_is_ready(self):
		from app.services.dbr.core.feeder.availability import evaluate_queue

		# Нет строк кита (нет спеки сырья) — гейт пропускает, как release.launch_signal.
		res = evaluate_queue([[]], {})
		assert res[0].status == "Готов" and res[0].can_launch
		assert res[0].total == 0

	def test_float_tail_not_a_shortage(self):
		from app.services.dbr.core.feeder.availability import evaluate_queue

		# 0.1 × 3 = 0.30000000000000004; остаток 0.3 → покрыто в пределах EPS.
		res = evaluate_queue([[self._need("A", 0.1 * 3)]], {"A": 0.3})
		assert res[0].cls == "ok"

	def test_reserved_all_q_is_raspisan_vyshe(self):
		from app.services.dbr.core.feeder.availability import evaluate_queue

		# Обе строки нижнего сигнала были на предприятии, но обе забраны выше.
		kits = [
			[self._need("A", 100), self._need("B", 100)],
			[self._need("A", 10), self._need("B", 10)],
		]
		res = evaluate_queue(kits, {"A": 100, "B": 100})
		assert res[1].status == "Расписан выше" and res[1].cls == "q"


class TestRoots:
	"""Корневые изделия детали — обратный обход BOM (roots.py)."""

	def _provider(self, graph):
		"""graph: компонент → родители (кто его содержит)."""
		return lambda item: graph.get(item, ())

	def test_item_without_parents_is_its_own_root(self):
		from app.services.dbr.core.feeder.roots import resolve_roots

		assert resolve_roots("Снегоход", self._provider({})) == ("Снегоход",)

	def test_linear_chain_to_root(self):
		from app.services.dbr.core.feeder.roots import resolve_roots

		# труба → дышло → рама → Снегоход
		graph = {"труба": ["дышло"], "дышло": ["рама"], "рама": ["Снегоход"]}
		assert resolve_roots("труба", self._provider(graph)) == ("Снегоход",)

	def test_common_part_has_several_roots(self):
		"""Ходовая деталь входит в несколько изделий — это и есть общность."""
		from app.services.dbr.core.feeder.roots import resolve_roots

		graph = {"втулка": ["рама", "навес"], "рама": ["Снегоход"], "навес": ["Мотобуксировщик"]}
		assert resolve_roots("втулка", self._provider(graph)) == ("Мотобуксировщик", "Снегоход")

	def test_diamond_collapses_to_single_root(self):
		from app.services.dbr.core.feeder.roots import resolve_roots

		graph = {"болт": ["узелA", "узелB"], "узелA": ["Изделие"], "узелB": ["Изделие"]}
		assert resolve_roots("болт", self._provider(graph)) == ("Изделие",)

	def test_cycle_does_not_hang(self):
		from app.services.dbr.core.feeder.roots import resolve_roots

		graph = {"A": ["B"], "B": ["A"]}
		assert resolve_roots("A", self._provider(graph)) == ()

	def test_cycle_with_escape_still_finds_root(self):
		from app.services.dbr.core.feeder.roots import resolve_roots

		graph = {"A": ["B"], "B": ["A", "Изделие"]}
		assert resolve_roots("A", self._provider(graph)) == ("Изделие",)

	def test_depth_cap_truncates(self):
		from app.services.dbr.core.feeder.roots import resolve_roots

		graph = {f"n{i}": [f"n{i + 1}"] for i in range(10)}
		assert resolve_roots("n0", self._provider(graph), max_depth=3) == ()

	def test_truncated_result_is_not_memoized(self):
		"""Обрыв зависит от пути обхода — кэшировать его нельзя."""
		from app.services.dbr.core.feeder.roots import resolve_roots

		graph = {"A": ["B"], "B": ["A", "Изделие"]}
		memo: dict = {}
		resolve_roots("A", self._provider(graph), memo=memo)
		# «A» обошлось через обрыв цикла — в кэш не попало; «Изделие» — корень.
		assert "A" not in memo
		assert memo.get("Изделие") == ("Изделие",)

	def test_memo_is_reused_across_calls(self):
		from app.services.dbr.core.feeder.roots import resolve_roots

		graph = {"x": ["узел"], "y": ["узел"], "узел": ["Изделие"]}
		memo: dict = {}
		assert resolve_roots("x", self._provider(graph), memo=memo) == ("Изделие",)
		assert memo["узел"] == ("Изделие",)
		assert resolve_roots("y", self._provider(graph), memo=memo) == ("Изделие",)


class TestPlannedRoots:
	"""Корни, ограниченные планируемыми изделиями (roots.resolve_planned_roots)."""

	def _provider(self, graph):
		return lambda item: graph.get(item, ())

	def test_returns_only_planned_ancestor(self):
		from app.services.dbr.core.feeder.roots import resolve_planned_roots

		# труба входит в Снегоход (в плане) и Мотобуксировщик (не в плане).
		graph = {"труба": ["рама", "навес"], "рама": ["Снегоход"], "навес": ["Мотобуксировщик"]}
		roots = resolve_planned_roots("труба", self._provider(graph), {"Снегоход"})
		assert roots == ("Снегоход",)

	def test_empty_when_no_ancestor_planned(self):
		from app.services.dbr.core.feeder.roots import resolve_planned_roots

		graph = {"труба": ["рама"], "рама": ["Снегоход"]}
		assert resolve_planned_roots("труба", self._provider(graph), {"Мотобуксировщик"}) == ()

	def test_intermediate_planned_subassembly_counted(self):
		"""Подузел в плане берётся как корень даже при планируемом изделии выше."""
		from app.services.dbr.core.feeder.roots import resolve_planned_roots

		# болт → рама(в плане) → Снегоход(в плане): оба — валидные корни детали.
		graph = {"болт": ["рама"], "рама": ["Снегоход"]}
		roots = resolve_planned_roots("болт", self._provider(graph), {"рама", "Снегоход"})
		assert roots == ("Снегоход", "рама")

	def test_self_is_never_a_root(self):
		from app.services.dbr.core.feeder.roots import resolve_planned_roots

		# Деталь сама стоит в плане, но себе корнем не является.
		graph = {"рама": ["Снегоход"]}
		assert resolve_planned_roots("рама", self._provider(graph), {"рама", "Снегоход"}) == ("Снегоход",)

	def test_cycle_does_not_hang(self):
		from app.services.dbr.core.feeder.roots import resolve_planned_roots

		graph = {"A": ["B"], "B": ["A", "Изделие"]}
		assert resolve_planned_roots("A", self._provider(graph), {"Изделие"}) == ("Изделие",)

	def test_diamond_collapses(self):
		from app.services.dbr.core.feeder.roots import resolve_planned_roots

		graph = {"болт": ["узелA", "узелB"], "узелA": ["Изделие"], "узелB": ["Изделие"]}
		assert resolve_planned_roots("болт", self._provider(graph), {"Изделие"}) == ("Изделие",)


class TestChainRules:
	"""Правила пробития цепочки — дочерние сигналы (chain.py, дизайн §4)."""

	def _line(self, cls, kind="make", buffered=False, need=10.0, have=0.0, item="A"):
		from app.services.dbr.core.feeder.availability import LineStatus

		return LineStatus(item, need, have, have, kind, "", cls, buffered)

	def test_covered_line_needs_nothing(self):
		from app.services.dbr.core.feeder.chain import NONE, line_action

		assert line_action(self._line("ok")) == NONE

	def test_reserved_above_spawns_nothing(self):
		"""cls=q: материал на предприятии есть, забран выше — заготовку не льём."""
		from app.services.dbr.core.feeder.chain import NONE, line_action

		assert line_action(self._line("q")) == NONE

	def test_shelf_links_not_duplicates(self):
		"""Полка в дефиците → ссылка на её сигнал пополнения, не дубль (§4 п.5)."""
		from app.services.dbr.core.feeder.chain import LINK, line_action

		assert line_action(self._line("no", buffered=True)) == LINK
		assert line_action(self._line("part", buffered=True)) == LINK

	def test_covered_shelf_still_none(self):
		"""Покрытая строка важнее признака полки: действий нет."""
		from app.services.dbr.core.feeder.chain import NONE, line_action

		assert line_action(self._line("ok", buffered=True)) == NONE

	def test_made_without_shelf_spawns_child(self):
		from app.services.dbr.core.feeder.chain import SPAWN, line_action

		assert line_action(self._line("no", kind="make")) == SPAWN
		assert line_action(self._line("part", kind="make")) == SPAWN

	def test_purchased_goes_to_procurement(self):
		from app.services.dbr.core.feeder.chain import PURCHASE, line_action

		assert line_action(self._line("no", kind="buy")) == PURCHASE

	def test_chain_qty_rounds_up_to_multiple(self):
		from app.services.dbr.core.feeder.chain import chain_qty

		assert chain_qty(10, 3) == 7  # кратность 1
		assert chain_qty(10, 3, multiple=5) == 10  # 7 → вверх до 10
		assert chain_qty(10, 10) == 0.0
		assert chain_qty(10, 12) == 0.0  # остатка больше потребности

	def test_chain_qty_tolerates_float_tail(self):
		from app.services.dbr.core.feeder.chain import chain_qty

		# Дробные нормы дают 3.0000000001 — это 3, а не 4.
		assert chain_qty(3.0000000001, 0.0) == 3.0

	def test_chain_qty_bad_multiple_falls_back_to_one(self):
		from app.services.dbr.core.feeder.chain import chain_qty

		assert chain_qty(7, 0, multiple=0) == 7
		assert chain_qty(7, 0, multiple=None) == 7

	def test_plan_children_aggregates_same_item(self):
		"""Две строки одного компонента → одна потребность; кратность к сумме."""
		from app.services.dbr.core.feeder.chain import plan_children

		lines = [
			self._line("no", need=5.0, have=0.0, item="A"),
			self._line("part", need=3.0, have=1.0, item="A"),
		]
		demands = plan_children(lines, multiple_by_item={"A": 5.0})
		assert len(demands) == 1
		assert demands[0].item == "A"
		assert demands[0].shortfall == 7.0  # 5 + 2
		assert demands[0].qty == 10.0  # 7 вверх до кратности 5

	def test_plan_children_skips_non_spawn_lines(self):
		from app.services.dbr.core.feeder.chain import plan_children

		lines = [
			self._line("ok", item="A"),
			self._line("q", item="B"),
			self._line("no", kind="buy", item="C"),
			self._line("no", buffered=True, item="D"),
			self._line("no", kind="make", item="E"),
		]
		assert [d.item for d in plan_children(lines)] == ["E"]

	def test_plan_children_is_deterministic(self):
		from app.services.dbr.core.feeder.chain import plan_children

		lines = [self._line("no", item=c) for c in ("C", "A", "B")]
		assert [d.item for d in plan_children(lines)] == ["A", "B", "C"]

	def test_inherited_priority(self):
		from app.services.dbr.core.feeder.chain import inherited_priority

		assert inherited_priority(0.87654321) == 0.8765
		assert inherited_priority(None) == 0.0


class TestClassifyGroup:
	"""Классификатор групп мехцеха (§4.3): 6 групп, сборка → None, прочее → other."""

	def test_recognizes_six_mechshop_groups(self):
		assert classify_group("Сварочный участок") == "weld"
		assert classify_group("Окрасочный порошковый участок") == "paint"
		assert classify_group("Фрезерный ЧПУ") == "milling"
		assert classify_group("Токарный участок") == "turning"
		assert classify_group("Гибка листового металла") == "sheet_bending"
		assert classify_group("Заготовительный участок (резка/сверловка)") == "blanking"

	def test_assembly_categories_are_excluded(self):
		# Сборочные категории вне мехцеха — исключаются из загрузки (None).
		assert classify_group("Сборка снегоходов") is None
		assert classify_group("Участок навесных узлов") is None
		assert classify_group("Участок катков и балансиров") is None

	def test_unrecognized_is_other(self):
		assert classify_group("Гальванический участок") == "other"
		assert classify_group("") == "other"
		# Порядок проверок: токарка ловится раньше «валов» (сборка).
		assert classify_group("Токарная обработка валов") == "turning"

	def test_labels_cover_order_and_other(self):
		for key in GROUP_ORDER:
			assert key in GROUP_LABELS
		assert GROUP_LABELS["other"] == "Прочее"


class TestWeekIndex:
	"""Недельные вёдра (§4.3): недели с понедельника, прошлое → 0."""

	MONDAY = date(2026, 7, 6)  # текущий понедельник

	def test_same_week_is_zero(self):
		assert week_index(date(2026, 7, 9), self.MONDAY) == 0  # четверг той же недели
		assert week_index(self.MONDAY, self.MONDAY) == 0

	def test_next_weeks(self):
		assert week_index(date(2026, 7, 13), self.MONDAY) == 1  # следующий понедельник
		assert week_index(date(2026, 7, 19), self.MONDAY) == 1  # воскресенье той недели
		assert week_index(date(2026, 7, 20), self.MONDAY) == 2

	def test_past_folds_to_zero(self):
		assert week_index(date(2026, 6, 30), self.MONDAY) == 0
		# Середина недели как «сегодня» — граница всё равно по понедельникам.
		assert week_index(date(2026, 7, 13), date(2026, 7, 8)) == 1

	def test_iso_string_and_bad_date(self):
		assert week_index("2026-07-20", self.MONDAY) == 2
		assert week_index(None, self.MONDAY) == 0


class TestLaunchWeek:
	"""Неделя запуска сигнала (§4.3): Red/In Work → 0, жёлтая с запасом, под график."""

	MONDAY = date(2026, 7, 6)

	def test_red_replenishment_is_zero(self):
		sig = {"status": "Open", "signal_type": "Пополнение", "zone": "Red", "nfp": 0, "adu": 5, "rt_days": 0}
		assert launch_week_for_signal(sig, self.MONDAY) == 0

	def test_in_work_always_zero(self):
		sig = {"status": "In Work", "signal_type": "Под график", "need_date": date(2026, 8, 30), "rt_days": 7}
		assert launch_week_for_signal(sig, self.MONDAY) == 0

	def test_yellow_with_slack_lands_on_right_week(self):
		# NFP/ADU − RT = 100/10 − 0 = 10 дней → 16.07 (неделя 1).
		sig = {"status": "Open", "signal_type": "Пополнение", "zone": "Yellow", "nfp": 100, "adu": 10, "rt_days": 0}
		assert launch_week_for_signal(sig, self.MONDAY) == 1

	def test_replenishment_zero_adu_is_zero(self):
		sig = {"status": "Open", "signal_type": "Пополнение", "zone": "Yellow", "nfp": 100, "adu": 0, "rt_days": 0}
		assert launch_week_for_signal(sig, self.MONDAY) == 0

	def test_under_schedule_need_minus_rt(self):
		# need_date 27.07 − RT 7 = 20.07 (понедельник) → неделя 2.
		sig = {"status": "Open", "signal_type": "Под график", "need_date": date(2026, 7, 27), "rt_days": 7}
		assert launch_week_for_signal(sig, self.MONDAY) == 2


class TestBuildGroupLoad:
	"""Агрегация загрузки групп (§4.3): суммы, later, детерминированность, дедуп норм."""

	MONDAY = date(2026, 7, 6)

	def test_sums_and_week_placement(self):
		signals = [
			{
				"qty": 10, "status": "Open", "signal_type": "Пополнение",
				"zone": "Red", "nfp": 0, "adu": 5, "rt_days": 0,
				# 10 × (6+6) / 60 = 2 ч, красная → неделя 0.
				"ops": [
					{"group": "turning", "minutes_per_unit": 6.0, "source": "calculated"},
					{"group": "turning", "minutes_per_unit": 6.0, "source": "calculated"},
				],
			},
			{
				"qty": 10, "status": "Open", "signal_type": "Пополнение",
				"zone": "Yellow", "nfp": 100, "adu": 10, "rt_days": 0,
				# 10 × 30 / 60 = 5 ч, запуск через 10 дней → неделя 1.
				"ops": [{"group": "weld", "minutes_per_unit": 30.0, "source": "bom"}],
			},
		]
		result = build_group_load(signals, week_count=4, today=self.MONDAY)
		turning = result["groups"]["turning"]
		assert turning["cells"][0] == pytest.approx(2.0)
		assert turning["total_hours"] == pytest.approx(2.0)
		weld = result["groups"]["weld"]
		assert weld["cells"][1] == pytest.approx(5.0)
		assert sum(weld["cells"]) + weld["later_hours"] == pytest.approx(weld["total_hours"])
		assert result["signals_without_norms"] == 0

	def test_beyond_horizon_goes_to_later(self):
		signals = [
			{
				"qty": 10, "status": "Open", "signal_type": "Пополнение",
				"zone": "Yellow", "nfp": 1000, "adu": 10, "rt_days": 0,
				# 100 дней → далеко за 4 недели → later.
				"ops": [{"group": "milling", "minutes_per_unit": 60.0, "source": "bom"}],
			}
		]
		result = build_group_load(signals, week_count=4, today=self.MONDAY)
		milling = result["groups"]["milling"]
		assert milling["cells"] == [0.0, 0.0, 0.0, 0.0]
		assert milling["later_hours"] == pytest.approx(10.0)

	def test_signals_without_norms_and_other_hours(self):
		signals = [
			{"qty": 5, "status": "Open", "signal_type": "Пополнение", "zone": "Red", "nfp": 0, "adu": 1, "rt_days": 0, "ops": []},
			{
				"qty": 10, "status": "In Work", "signal_type": "Пополнение",
				"zone": "Yellow", "nfp": 50, "adu": 5, "rt_days": 0,
				"ops": [{"group": "other", "minutes_per_unit": 12.0, "source": "bom"}],  # In Work → неделя 0
			},
		]
		result = build_group_load(signals, week_count=4, today=self.MONDAY)
		assert result["signals_without_norms"] == 1
		assert result["other_hours"] == pytest.approx(2.0)
		assert result["groups"]["other"]["cells"][0] == pytest.approx(2.0)

	def test_deterministic(self):
		signals = [
			{
				"qty": 7, "status": "Open", "signal_type": "Пополнение",
				"zone": "Yellow", "nfp": 60, "adu": 6, "rt_days": 0,
				"ops": [{"group": "blanking", "minutes_per_unit": 9.0, "source": "calculated"}],
			}
		]
		assert build_group_load(signals, 4, self.MONDAY) == build_group_load(signals, 4, self.MONDAY)

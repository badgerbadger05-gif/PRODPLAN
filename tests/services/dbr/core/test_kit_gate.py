"""Тесты гейта комплектности — чистый pytest, без Frappe."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.dbr.core.drum.kit import KitLine
from app.services.dbr.core.drum.kit_gate import GREEN, RED, YELLOW, GateSlot, evaluate

W3 = "Склад №3"
W4 = "Склад №4"

KITS = {
	"SNEG-100": [
		KitLine("RAMA-OKR", 1.0, W3, False),
		KitLine("PODSHIPNIK", 4.0, W4, False),
	],
}

D1, D2, D3 = date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)


class TestGateV1:
	def test_green_when_stock_covers(self):
		verdicts = evaluate(
			slots=[GateSlot(D1, "SNEG-100", 2)],
			kits=KITS,
			stock={("RAMA-OKR", W3): 2, ("PODSHIPNIK", W4): 8},
		)
		assert verdicts[0].status == GREEN
		assert not verdicts[0].shortages

	def test_red_with_shortage_list(self):
		verdicts = evaluate(
			slots=[GateSlot(D1, "SNEG-100", 2)],
			kits=KITS,
			stock={("RAMA-OKR", W3): 2, ("PODSHIPNIK", W4): 5},
		)
		assert verdicts[0].status == RED
		(shortage,) = verdicts[0].shortages
		assert shortage.item == "PODSHIPNIK"
		assert shortage.required == 8
		assert shortage.available == 5
		assert shortage.warehouse == W4

	def test_earlier_slots_reserve_cumulatively(self):
		"""Остатка хватает на первый слот, второй краснеет из-за резерва первого."""
		verdicts = evaluate(
			slots=[GateSlot(D1, "SNEG-100", 2), GateSlot(D2, "SNEG-100", 2)],
			kits=KITS,
			stock={("RAMA-OKR", W3): 3, ("PODSHIPNIK", W4): 100},
		)
		assert verdicts[0].status == GREEN
		assert verdicts[1].status == RED
		(shortage,) = verdicts[1].shortages
		assert shortage.item == "RAMA-OKR"
		assert shortage.available == 1

	def test_red_slot_still_reserves_for_later_slots(self):
		"""Красный слот резервирует свою потребность: его добирает kit-форс, а не следующий слот."""
		verdicts = evaluate(
			slots=[GateSlot(D1, "SNEG-100", 3), GateSlot(D2, "SNEG-100", 1)],
			kits=KITS,
			stock={("RAMA-OKR", W3): 4, ("PODSHIPNIK", W4): 8},
		)
		# слот 1: подшипников надо 12, есть 8 → RED; остаток обнулён резервом
		assert verdicts[0].status == RED
		assert verdicts[1].status == RED
		(shortage,) = verdicts[1].shortages
		assert shortage.item == "PODSHIPNIK"
		assert shortage.available == 0

	def test_unsorted_slots_raise(self):
		with pytest.raises(ValueError, match="отсортированы"):
			evaluate(
				slots=[GateSlot(D2, "SNEG-100", 1), GateSlot(D1, "SNEG-100", 1)],
				kits=KITS,
				stock={},
			)

	def test_missing_kit_raises(self):
		with pytest.raises(ValueError, match="Нет кита"):
			evaluate(slots=[GateSlot(D1, "X", 1)], kits={}, stock={})


class TestGateV2Inbound:
	def test_yellow_when_inbound_covers(self):
		"""Не покрыт остатком, но покрывается открытым пополнением с датой ≤ даты слота."""
		verdicts = evaluate(
			slots=[GateSlot(D2, "SNEG-100", 2)],
			kits=KITS,
			stock={("RAMA-OKR", W3): 0, ("PODSHIPNIK", W4): 8},
			inbound=[("RAMA-OKR", W3, D1, 5)],
		)
		assert verdicts[0].status == YELLOW
		assert not verdicts[0].shortages

	def test_inbound_after_slot_date_does_not_count(self):
		verdicts = evaluate(
			slots=[GateSlot(D1, "SNEG-100", 2)],
			kits=KITS,
			stock={("RAMA-OKR", W3): 0, ("PODSHIPNIK", W4): 8},
			inbound=[("RAMA-OKR", W3, D3, 5)],
		)
		assert verdicts[0].status == RED
		(shortage,) = verdicts[0].shortages
		assert shortage.item == "RAMA-OKR"

	def test_inbound_reserved_by_earlier_slot(self):
		verdicts = evaluate(
			slots=[GateSlot(D2, "SNEG-100", 2), GateSlot(D3, "SNEG-100", 2)],
			kits=KITS,
			stock={("RAMA-OKR", W3): 0, ("PODSHIPNIK", W4): 100},
			inbound=[("RAMA-OKR", W3, D1, 3)],
		)
		assert verdicts[0].status == YELLOW
		assert verdicts[1].status == RED
		(shortage,) = verdicts[1].shortages
		assert shortage.available == 1

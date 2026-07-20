"""Гейт комплектности слотов барабана (техдизайн Фазы 1 §5).

Чистый Python без Frappe. Для слотов в порядке дат:

    доступно(item, склад) = фактический остаток
                            − резерв более ранних слотов (кумулятивно)
                            + приходы открытого пополнения с датой ≤ даты слота (v2)

- все позиции кита покрыты остатком → GREEN;
- не покрыты остатком, но покрываются с учётом открытого пополнения → YELLOW;
- нет → RED + shortage-list (item, требуется, доступно, склад).

v1 честно работает только по фактическим Bin (inbound пустой) — даёт
GREEN/RED и shortage-list. v2 добавит прогноз пополнения питателей.


Портировано из prodflow prodflow/services/drum/kit_gate.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import NamedTuple

from .kit import KitLine

GREEN = "Green"
YELLOW = "Yellow"
RED = "Red"


class Shortage(NamedTuple):
	item: str
	required: float
	available: float
	warehouse: str


class GateSlot(NamedTuple):
	"""Минимум, который гейту нужно знать о слоте."""

	date: date
	sku: str
	qty: int


@dataclass(frozen=True)
class SlotVerdict:
	status: str  # GREEN / YELLOW / RED
	shortages: list[Shortage]  # пусто для GREEN


def evaluate(
	slots: Sequence[GateSlot],
	kits: Mapping[str, Sequence[KitLine]],
	stock: Mapping[tuple[str, str], float],
	inbound: Iterable[tuple[str, str, date, float]] = (),
) -> list[SlotVerdict]:
	"""Вердикты в порядке слотов; слоты обрабатываются кумулятивно по датам.

	- slots — уже отфильтрованные по горизонту ⚙ и отсортированные по дате;
	- kits — кит на единицу SKU (build_kit);
	- stock — фактический остаток: (item, warehouse) → qty;
	- inbound — открытые пополнения: (item, warehouse, плановая дата, qty).
	"""
	if any(slots[i].date > slots[i + 1].date for i in range(len(slots) - 1)):
		raise ValueError("Слоты должны быть отсортированы по дате")

	remaining = dict(stock)
	# приходы, ещё не введённые в пул: сортировка по дате
	pending = sorted(inbound, key=lambda e: (e[2], e[0], e[1]))
	inbound_pool: dict[tuple[str, str], float] = {}
	next_event = 0

	verdicts: list[SlotVerdict] = []
	for slot in slots:
		while next_event < len(pending) and pending[next_event][2] <= slot.date:
			item, warehouse, _day, qty = pending[next_event]
			key = (item, warehouse)
			inbound_pool[key] = inbound_pool.get(key, 0.0) + qty
			next_event += 1

		kit = kits.get(slot.sku)
		if kit is None:
			raise ValueError(f"Нет кита для SKU «{slot.sku}»")

		shortages: list[Shortage] = []
		needs_inbound = False
		for line in kit:
			key = (line.item, line.source_warehouse)
			need = line.qty_per_unit * slot.qty
			on_hand = remaining.get(key, 0.0)
			with_inbound = on_hand + inbound_pool.get(key, 0.0)
			if on_hand + 1e-9 < need:
				if with_inbound + 1e-9 >= need:
					needs_inbound = True
				else:
					shortages.append(Shortage(line.item, need, with_inbound, line.source_warehouse))

		# резерв кумулятивно: потребность слота списывается из пулов,
		# даже если слот красный — его нехватку добирает kit-форс питателя
		for line in kit:
			key = (line.item, line.source_warehouse)
			need = line.qty_per_unit * slot.qty
			from_stock = min(remaining.get(key, 0.0), need)
			if from_stock:
				remaining[key] = remaining[key] - from_stock
			rest = need - from_stock
			if rest and key in inbound_pool:
				from_inbound = min(inbound_pool[key], rest)
				inbound_pool[key] = inbound_pool[key] - from_inbound

		if shortages:
			status = RED
		elif needs_inbound:
			status = YELLOW
		else:
			status = GREEN
		verdicts.append(SlotVerdict(status=status, shortages=shortages))

	return verdicts

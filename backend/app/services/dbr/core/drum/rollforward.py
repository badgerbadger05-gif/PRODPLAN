"""Перенос невыполненных плиток барабана вправо — чистое ядро.

Требование владельца 08.07.2026: «План не выполнен, барабан уехал влево.
Потребность исчезла? А детали куда? Плитка может исчезнуть только закрытым
выпуском либо удалением с правами администратора».

Сейчас плитка «гаснет» в момент создания Work Order (`release_status`
Pending → Released), а потребители (очередь мехцеха, гейт) отбрасывают всё,
что раньше сегодня. Значит невыполненный план молча испаряется вместе с
потребностью в деталях.

Модель:
- `planned_date` слота НЕИЗМЕННА (куда план поставил изначально) — по ней
  видно «в августе висит плитка из июня»;
- `date` подвижна: незакрытая плитка с датой в прошлом едет вправо;
- закрывает плитку только выпуск: `produced_qty ≥ qty` (`Work Order.produced_qty`
  двигает ядро ERPNext на проводке «Изготовление»);
- **релизнутая** плитка (WO уже создан) НЕ двигается: за неё отвечает заказ,
  у него свои даты. Она остаётся на месте просроченной, пока выпуск её не
  закроет. Двигаются только плитки плана, до которых ещё не дошли руки.

Нагрузка: одна штука занимает `1/такт` дня, лимит дня — 1.0 (та же модель,
что в `leveling`). Плитка едет на ПЕРВЫЙ рабочий день, где остаток ещё
влезает; порядок разбора — по исходной плановой дате (FIFO), чтобы старое не
пропускало вперёд новое.

Плитка, чей остаток не влезает ни в один день горизонта (в т.ч. когда её
собственная нагрузка > 1.0), не теряется и не дробится («одна плитка — одна
единица учёта»): она ставится на НАИМЕНЕЕ загруженный день горизонта и
помечается перегрузом. Сваливать такие плитки на первый день нельзя — план
и так отстал, а сегодняшний день превратился бы в свалку.

Чистый Python без Frappe: `python -m pytest prodflow/services/drum`.


Портировано из prodflow prodflow/services/drum/rollforward.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import NamedTuple

_EPS = 1e-9
DAY_LOAD_LIMIT = 1.0


class OverdueSlot(NamedTuple):
	"""Просроченная незакрытая плитка, подлежащая переносу."""

	name: str            # имя строки слота (идентичность плитки сохраняется)
	planned_date: date   # исходная плановая дата — не меняется
	workstation: str
	item: str
	remaining_qty: int   # qty − produced_qty, > 0


class SlotMove(NamedTuple):
	name: str
	to_date: date
	overloaded: bool  # не влезла в лимит нагрузки, поставлена принудительно


def slot_load(remaining_qty: int, takt: float) -> float:
	"""Нагрузка плитки в долях дня. Такт ≤ 0 — нагрузка неизвестна, считаем нулевой."""
	if takt <= 0:
		return 0.0
	return float(remaining_qty) / float(takt)


def plan_rollforward(
	overdue: Sequence[OverdueSlot],
	workdays: Sequence[date],
	takts: Mapping[str, tuple[str, float]],
	load_by_day: Mapping[tuple[str, date], float],
) -> list[SlotMove]:
	"""Куда переехать просроченным плиткам.

	`workdays` — рабочие дни горизонта начиная с сегодняшнего, по возрастанию.
	`takts` — {item: (участок, такт)} из «Настроек сборки».
	`load_by_day` — текущая нагрузка {(участок, день): доля} от плиток, которые
	никуда не едут (будущие и релизнутые). МУТИРУЕТСЯ копией: вход не портим.

	Возвращает перемещения в порядке разбора. Плитки без рабочих дней в
	горизонте не двигаются (пустой результат).
	"""
	if not workdays:
		return []

	load: dict[tuple[str, date], float] = dict(load_by_day)
	moves: list[SlotMove] = []

	# FIFO по исходной плановой дате: старое не должно пропускать вперёд новое.
	for slot in sorted(overdue, key=lambda s: (s.planned_date, s.item, s.name)):
		takt = takts.get(slot.item, ("", 0.0))[1]
		need = slot_load(slot.remaining_qty, takt)

		target: date | None = None
		for day in workdays:
			if load.get((slot.workstation, day), 0.0) + need <= DAY_LOAD_LIMIT + _EPS:
				target = day
				break

		if target is None:
			# Не влезает никуда (день переполнен или собственная нагрузка > 1.0):
			# кладём на наименее загруженный день (при равенстве — ранний) и
			# честно помечаем перегруз. Иначе всё свалилось бы на сегодня.
			target = min(workdays, key=lambda d: (load.get((slot.workstation, d), 0.0), d))
			moves.append(SlotMove(slot.name, target, overloaded=True))
		else:
			moves.append(SlotMove(slot.name, target, overloaded=False))

		load[(slot.workstation, target)] = load.get((slot.workstation, target), 0.0) + need

	return moves


def is_closed(qty: int, produced_qty: float) -> bool:
	"""Плитка закрыта выпуском: произведено не меньше запланированного."""
	return float(produced_qty) + _EPS >= float(qty)


def remaining_qty(qty: int, produced_qty: float) -> int:
	"""Остаток к выпуску по плитке, не меньше нуля (перевыпуск не уводит в минус)."""
	rest = int(qty) - int(float(produced_qty))
	return max(rest, 0)

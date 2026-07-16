"""Пробитие цепочки мехцеха — правила дочерних сигналов (Фаза 3.2, чистое ядро).

Дизайн: `prodflow/дизайн-обеспеченность-и-цепочка-мехцеха.md` §4.

Когда full kit сигнала (ядро `availability`) упирается в непокрытую строку,
решение зависит от того, ЧТО это за граница:

- производимая деталь **без полки** → порождаем дочерний сигнал «Цепочка»
  (SPAWN): pegging на родителя, приоритет наследуется — цепочку тянет тот же
  буфер;
- **позиция супермаркета** (полка) → дочерний сигнал НЕ порождаем (LINK):
  у полки уже есть собственный сигнал пополнения, дубль дал бы двойной счёт
  спроса (§4 п.5). Родитель просто показывает «ждёт полку Y»;
- **закупной** материал → в контур закупок (PURCHASE), в очередь мехцеха
  ничего не добавляем;
- строка класса `q` («расписан выше») → НИЧЕГО (NONE): материал на
  предприятии есть, это конкуренция за очередь, а не дефицит цепочки.
  Порождать под неё заготовку — значит производить лишнее.
- покрытая строка `ok` → NONE.

Автозапуска нет: система порождает и приоритизирует дочерний сигнал, запускает
его человек (принцип владельца, техдизайн Фазы 2 §12 п.7).

Чистый Python без Frappe — `python -m pytest prodflow/services/feeder`.


Портировано из prodflow prodflow/services/feeder/chain.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from math import ceil
from typing import NamedTuple

from .availability import EPS, MAKE

# Тип сигнала (значение поля signal_type в DocType).
CHAIN_TYPE = "Цепочка"

# Предел глубины пробития: защита от патологических/циклических деревьев BOM.
# Реальная цепочка мехцеха — заготовка → гибка → сварка → окраска (≤4).
MAX_CHAIN_DEPTH = 5

# Решения по строке кита.
SPAWN = "spawn"        # породить дочерний сигнал «Цепочка» на компонент
LINK = "link"          # ждать чужой сигнал (полка) — спрос уже учтён
PURCHASE = "purchase"  # дефицит закупного — вход контура закупок
NONE = "none"          # действий не требуется


class ChainDemand(NamedTuple):
	"""Потребность в дочернем сигнале на один компонент одного родителя."""

	item: str
	qty: float        # к запуску: дефицит, округлённый вверх до кратности
	shortfall: float  # исходный дефицит (need − доступное этому сигналу)


def line_action(line) -> str:
	"""Что делать с оценённой строкой кита (`availability.LineStatus`).

	Порядок проверок важен: `ok`/`q` отсекаются ДО признака полки и вида
	границы — по покрытой строке и по «расписанной выше» действий нет.
	"""
	if line.cls == "ok":
		return NONE
	if line.cls == "q":
		return NONE
	if line.buffered:
		return LINK
	if line.kind == MAKE:
		return SPAWN
	return PURCHASE


def chain_qty(need: float, have: float, multiple: float = 1.0) -> float:
	"""Количество дочернего запуска: дефицит вверх до кратности.

	`have` — остаток, доступный этому сигналу после netting (LineStatus.have);
	дефицит = need − have, но не меньше нуля. Кратность ≤ 0 трактуем как 1.
	Допуск EPS гасит float-хвосты дробных норм: 3.0000000001 → 3, не 4.
	"""
	shortfall = float(need) - max(float(have), 0.0)
	if shortfall <= EPS:
		return 0.0
	if multiple is None or multiple <= 0:
		multiple = 1.0
	units = ceil(shortfall / multiple - EPS)
	return round(units * multiple, 4)


def plan_children(lines, multiple_by_item=None) -> list[ChainDemand]:
	"""Дочерние потребности одного родителя по строкам его кита.

	Строки одного и того же компонента (кит может дать их несколько — разные
	склады-источники / пометки) агрегируются в ОДНУ потребность: дефициты
	складываются, кратность применяется один раз к сумме. Порядок результата
	детерминирован (по коду компонента).
	"""
	multiple_by_item = multiple_by_item or {}
	shortfall_by_item: dict[str, float] = {}
	for line in lines:
		if line_action(line) != SPAWN:
			continue
		short = float(line.need) - max(float(line.have), 0.0)
		if short <= EPS:
			continue
		shortfall_by_item[line.item] = shortfall_by_item.get(line.item, 0.0) + short

	demands: list[ChainDemand] = []
	for item in sorted(shortfall_by_item):
		shortfall = shortfall_by_item[item]
		qty = chain_qty(shortfall, 0.0, multiple_by_item.get(item, 1.0))
		if qty > 0:
			demands.append(ChainDemand(item=item, qty=qty, shortfall=round(shortfall, 4)))
	return demands


def inherited_priority(parent_priority: float) -> float:
	"""Приоритет дочернего сигнала = приоритет родителя (§4 п.1).

	Единый язык очереди: цепочку тянет тот же буфер, что и родителя, поэтому
	ребёнок встаёт рядом с ним, а не в хвост. Отдельная функция — точка
	расширения (например, лёгкий бонус за длину цепочки).
	"""
	return round(float(parent_priority or 0.0), 4)

"""Обеспеченность очереди мехцеха материалом (Фаза 3.1, чистое ядро).

Дизайн: `prodflow/дизайн-обеспеченность-и-цепочка-мехцеха.md` §3
(готовность комплекта в очереди) и §8 (две оси цвета: капсула = риск
буфера, чип «Комплект» = материальная готовность).

Идея: у каждого Open-сигнала есть КИТ — набор строк-границ до первой
развязки (позиция супермаркета / закупной / производимая деталь без
полки), полученный `kit.build_kit` на стороне Frappe-адаптера. Здесь —
чистая арифметика ОДНОГО пула остатка:

- `evaluate_queue` раздаёт доступный остаток сигналам СВЕРХУ ВНИЗ по
  порядку очереди (кумулятивный netting, §3 п.2): сигнал ниже видит
  остаток за вычетом «расписанного» на верхние. Без захвата — просто
  честный учёт одного пула, чтобы два сигнала не «видели» один пруток
  (двойной счёт покрытия, §1 п.3);
- `evaluate_kit` оценивает один кит против пула и возвращает статус
  строки и сигнала.

Класс строки (для цвета точки в карточке):
  ok   — покрыто;
  part — часть предприятия есть, но на всю потребность не хватает;
  q    — на предприятии хватало, но остаток расписан на сигналы выше/
         соседние строки (конкуренция) — «Расписан выше»/«занято»;
  no   — на предприятии нет (или почти нет).

Статус сигнала: Готов / Частично / Дефицит / Расписан выше.

Пул item-ориентирован: доступность считается по предприятию (сумма по
selected-складам минус игнорируемые — фильтр в адаптере ERPNext Bin),
без привязки к складу-источнику. Точная пер-складская привязка — после
cutover на Bin (как и в release.py). Крепёж (free-issue) в кит не
попадает (исключается ещё в build_kit), поэтому здесь его нет.

Чистый Python без Frappe — тестируется локально
(`python -m pytest prodflow/services/feeder`).


Портировано из prodflow prodflow/services/feeder/availability.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

# Допуск на равенство потребности/остатка: дробные нормы × qty дают
# float-хвосты, не считаем их дефицитом (паттерн release.full_kit_shortage).
EPS = 1e-6

# Вид строки кита (граница развязки) — определяется адаптером по спеке 1С.
BUFFER = "buffer"  # позиция супермаркета (полка №3/№4) — развязка
BUY = "buy"        # закупной материал/комплектующая — нет полки
MAKE = "make"      # производимая деталь без полки → в 3.2 породит дочерний сигнал


class KitLineNeed(NamedTuple):
	"""Строка кита сигнала: потребность на границе развязки.

	need — уже помноженная на количество сигнала потребность (шт/м/кг);
	kind — вид границы (BUFFER/BUY/MAKE) для иконки и агрегата дефицитов;
	level — человекочитаемая пометка уровня («полка», «закупной»,
	«без полки → цепочка») для карточки.
	"""

	item: str
	need: float
	kind: str
	level: str = ""
	# Компонент — активная позиция супермаркета (полка-развязка). У полки
	# свой сигнал пополнения, поэтому дочерний сигнал цепочки на неё НЕ
	# порождается (§4 п.5, двойной счёт спроса) — правила в chain.py.
	buffered: bool = False


class LineStatus(NamedTuple):
	"""Оценённая строка кита против пула."""

	item: str
	need: float
	have: float   # остаток, доступный ЭТОМУ сигналу на момент проверки
	gross: float  # валовый остаток предприятия (для «расписан выше»)
	kind: str
	level: str
	cls: str      # ok | part | q | no
	buffered: bool = False


class SignalKit(NamedTuple):
	"""Итог обеспеченности одного сигнала."""

	status: str            # Готов | Частично | Дефицит | Расписан выше
	cls: str               # ok | part | no | q — класс чипа «Комплект»
	covered: int           # строк кита покрыто
	total: int             # строк кита всего
	lines: list[LineStatus]

	@property
	def can_launch(self) -> bool:
		"""Полный комплект — «Запустить» активна (§8 п.6)."""
		return self.cls == "ok"


def _line_cls(need: float, take: float, gross: float) -> str:
	"""Класс строки по потребности, взятому из пула и валовому остатку."""
	short = need - take
	if short <= EPS:
		return "ok"
	# Предприятие имело достаточно, но остаток уже расписан выше/рядом.
	if gross + EPS >= need:
		return "q"
	# Часть остатка предприятия есть, но на всю потребность не хватает.
	if take > EPS:
		return "part"
	return "no"


def _signal_status(lines: list[LineStatus], covered: int) -> tuple[str, str]:
	"""Статус и класс чипа сигнала из строк кита."""
	total = len(lines)
	if total == 0 or covered == total:
		return "Готов", "ok"
	shortages = [ln for ln in lines if ln.cls != "ok"]
	# Всё, чего не хватает, физически на предприятии есть — забрали выше.
	if shortages and all(ln.cls == "q" for ln in shortages):
		return "Расписан выше", "q"
	if covered > 0:
		return "Частично", "part"
	return "Дефицит", "no"


def evaluate_kit(
	lines: list[KitLineNeed],
	gross: Mapping[str, float],
	remaining: dict[str, float],
) -> SignalKit:
	"""Оценить кит одного сигнала против пула; МУТИРУЕТ `remaining`.

	`gross` — валовый остаток предприятия по коду (immutable); `remaining`
	— общий изменяемый пул кумулятивного netting (лениво инициализируется
	из gross при первом обращении к коду). Внутри одного кита строки тоже
	конкурируют за общий пул (две строки на один код видят убывающий
	остаток).
	"""
	result_lines: list[LineStatus] = []
	covered = 0
	for ln in lines:
		avail = remaining.get(ln.item)
		if avail is None:
			avail = float(gross.get(ln.item, 0.0) or 0.0)
			remaining[ln.item] = avail
		take = min(float(ln.need), max(avail, 0.0))
		remaining[ln.item] = avail - take
		g = float(gross.get(ln.item, 0.0) or 0.0)
		cls = _line_cls(float(ln.need), take, g)
		if cls == "ok":
			covered += 1
		result_lines.append(
			LineStatus(ln.item, float(ln.need), avail, g, ln.kind, ln.level, cls, ln.buffered)
		)
	status, cls = _signal_status(result_lines, covered)
	return SignalKit(status, cls, covered, len(result_lines), result_lines)


def evaluate_queue(
	kits: list[list[KitLineNeed]],
	gross: Mapping[str, float],
) -> list[SignalKit]:
	"""Кумулятивный netting по очереди: киты в ПОРЯДКЕ очереди (§3 п.2).

	`kits` — список китов сигналов, УЖЕ отсортированных ключом очереди
	(kit-форс → приоритет → RT). Общий пул один на всю очередь; сигнал
	ниже видит остаток за вычетом расписанного на верхние. Возвращает
	статусы в том же порядке.
	"""
	remaining: dict[str, float] = {}
	return [evaluate_kit(lines, gross, remaining) for lines in kits]

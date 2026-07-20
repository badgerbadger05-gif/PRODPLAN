"""Корневые изделия детали — обратный обход дерева BOM (чистое ядро).

Запрос владельца (08.07): в очереди мехцеха нужно видеть, ДЛЯ КАКОГО изделия
требуется деталь, и уметь отбирать очередь по изделию.

Корень — позиция, которую никто не содержит в своей спецификации (верх дерева:
снегоход, мотобуксировщик, модуль). Идём от детали ВВЕРХ по связям
«компонент → содержащие его изделия» до позиций без родителей. Работает и для
дочерних заготовок цепочки (труба → дышло → рама → изделие), поэтому
специальной логики для сигналов «Цепочка» не нужно.

У ходовой детали корней несколько (это и есть общность, `commonality`) —
возвращаем все, отсортированными и без повторов.

Защита: цикл в спеке и предельная глубина обрываются молча (деталь просто не
даёт корней через эту ветку), результат обрыва НЕ кэшируется — он зависит от
пути обхода, а не только от узла.

Чистый Python без Frappe: карту «компонент → родители» строит адаптер
(material_service). Тесты: `python -m pytest prodflow/services/feeder`.


Портировано из prodflow prodflow/services/feeder/roots.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

# Реальные деревья мехцеха мельче 10 уровней; запас на патологии.
MAX_ROOT_DEPTH = 20

# parents_of(item) → коды позиций, чьи спецификации содержат item
ParentProvider = Callable[[str], Iterable[str]]


def _walk(
	item: str,
	parents_of: ParentProvider,
	memo: dict[str, tuple[str, ...]],
	stack: frozenset[str],
	max_depth: int,
) -> tuple[tuple[str, ...], bool]:
	"""Корни узла и признак обрыва (цикл/предел глубины) в его поддереве."""
	cached = memo.get(item)
	if cached is not None:
		return cached, False
	if item in stack or len(stack) >= max_depth:
		return (), True

	parents = tuple(sorted(set(parents_of(item) or ())))
	if not parents:
		memo[item] = (item,)  # никто не содержит — это вершина дерева
		return (item,), False

	acc: set[str] = set()
	truncated = False
	for parent in parents:
		result, cut = _walk(parent, parents_of, memo, stack | {item}, max_depth)
		acc.update(result)
		truncated = truncated or cut

	roots = tuple(sorted(acc))
	# Обрыв делает результат зависимым от пути — такой ответ не кэшируем.
	if not truncated:
		memo[item] = roots
	return roots, truncated


def resolve_roots(
	item: str,
	parents_of: ParentProvider,
	memo: dict[str, tuple[str, ...]] | None = None,
	max_depth: int = MAX_ROOT_DEPTH,
) -> tuple[str, ...]:
	"""Корневые изделия детали (отсортированы, без повторов).

	Позиция без родителей — сама себе корень (сигнал на готовое изделие).
	`memo` можно переиспользовать между вызовами: обход очереди мемоизируется.
	"""
	roots, _ = _walk(item, parents_of, memo if memo is not None else {}, frozenset(), max_depth)
	return roots


def _walk_planned(
	item: str,
	parents_of: ParentProvider,
	planned: frozenset[str],
	memo: dict[str, frozenset[str]],
	stack: frozenset[str],
	max_depth: int,
) -> tuple[frozenset[str], bool]:
	"""Планируемые предки узла (∈ planned) и признак обрыва в его поддереве."""
	cached = memo.get(item)
	if cached is not None:
		return cached, False
	if item in stack or len(stack) >= max_depth:
		return frozenset(), True

	acc: set[str] = set()
	truncated = False
	for parent in sorted(set(parents_of(item) or ())):
		if parent in planned:
			acc.add(parent)  # предок сам в плане — берём его как корень
		# Идём выше и над планируемым предком: одна деталь может входить и в
		# подузел, и в изделие верхнего уровня, если оба стоят в графике.
		result, cut = _walk_planned(parent, parents_of, planned, memo, stack | {item}, max_depth)
		acc.update(result)
		truncated = truncated or cut

	roots = frozenset(acc)
	if not truncated:
		memo[item] = roots
	return roots, truncated


def resolve_planned_roots(
	item: str,
	parents_of: ParentProvider,
	planned: Iterable[str],
	memo: dict[str, frozenset[str]] | None = None,
	max_depth: int = MAX_ROOT_DEPTH,
) -> tuple[str, ...]:
	"""Корни детали, ограниченные множеством планируемых изделий `planned`.

	В отличие от resolve_roots (все вершины дерева BOM), возвращает только тех
	предков детали, что реально стоят в планах, питающих очередь (SKU активного
	графика сборки). Так из отбора уходит BOM-шум — верхнеуровневые позиции без
	спроса (инструмент, снятые изделия). Сама деталь в корни не попадает.
	"""
	planned_set = planned if isinstance(planned, frozenset) else frozenset(planned)
	roots, _ = _walk_planned(
		item, parents_of, planned_set, memo if memo is not None else {}, frozenset(), max_depth
	)
	return tuple(sorted(roots))

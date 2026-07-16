"""Многоуровневый разворот спроса мехцеха — чистое ядро (без Frappe).

Кит барабана (`kit.build_kit`) для ПРОВЕРКИ НАЛИЧИЯ идёт до первой границы
развязки и там встаёт. Для САЙЗИНГА БУФЕРОВ (ADU) нужен другой обход: на
границе-буфере надо записать спрос И ПРОДОЛЖИТЬ разворот сквозь неё —
пополнение буфера потребляет его компоненты, поэтому спрос доходит до нижних
буферов (заготовок Склада №2), вложенных внутри узловых сборок Склада №4.

`explode_kit` возвращает те же `adu.KitLine(item, qty_per_unit, warehouse)`,
что и `build_kit`, но по ВСЕМ уровням развязки, с накопленным (перемноженным
по пути) qty. `adu.build_adu` суммирует их по (item, warehouse) без изменений,
так что существующие позиции №3/№4 не меняются, а вложенные №2 добавляются.

Классификация детали ИНЪЕКТИРУЕТСЯ (для pytest без Frappe) — `classify(item)`
возвращает `Node(kind, warehouse, explode_through)`:
  - `kind == RECURSE`  — фантом/нескладируемый узел: строки НЕ даём, идём
    прозрачно вниз (спрос проносится сквозь фантом к его компонентам);
  - `kind == SKIP`     — крепёж (free-issue): исключаем из кита целиком;
  - `kind == BOUNDARY` — точка развязки (буфер/закупка/под-график): даём
    строку (item, qty, warehouse). Если `explode_through` (деталь производится
    и имеет компоненты) — ПРОДОЛЖАЕМ разворот сквозь неё; иначе терминал
    (закупное/лист — глубже не идём).

Производительность: субкиты мемоизируются по детали (`memo`), поэтому общий
подузел разворачивается один раз и переиспользуется — обход O(уникальных
деталей), а не O(путей). Циклы BOM разрываются по стеку пути; `max_depth` —
страховочный предел глубины.


Портировано из prodflow prodflow/services/feeder/demand_explosion.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import NamedTuple

from app.services.dbr.core.feeder.adu import KitLine

# Виды классификации детали при развороте.
RECURSE = "recurse"
SKIP = "skip"
BOUNDARY = "boundary"

MAX_DEPTH = 20


class Node(NamedTuple):
    """Классификация детали для разворота спроса."""

    kind: str
    warehouse: str | None = None
    explode_through: bool = False


def explode_kit(
    sku: str,
    components_of: Callable[[str], Iterable[tuple[str, float]]],
    classify: Callable[[str], Node],
    max_depth: int = MAX_DEPTH,
    memo: dict[str, list[KitLine]] | None = None,
) -> list[KitLine]:
    """Многоуровневый кит SKU: строки развязки по ВСЕМ уровням с накопленным qty.

    `components_of(item)` → последовательность (компонент, кол-во на единицу
    родителя). `classify(item)` → `Node`. `memo` можно передать общий на весь
    прогон (несколько SKU) — субкиты переиспользуются между изделиями.

    Возвращает список `KitLine`; одна деталь может встретиться несколькими
    строками (разные родители/пути) — `build_adu` их суммирует по (item, wh).
    """
    if memo is None:
        memo = {}
    return _subkit(sku, components_of, classify, max_depth, memo, frozenset((sku,)))


def _subkit(
    item: str,
    components_of: Callable[[str], Iterable[tuple[str, float]]],
    classify: Callable[[str], Node],
    max_depth: int,
    memo: dict[str, list[KitLine]],
    stack: frozenset[str],
) -> list[KitLine]:
    """Строки развязки поддерева `item` на ЕДИНИЦУ `item` (без строки самого item)."""
    cached = memo.get(item)
    if cached is not None:
        return cached
    if len(stack) > max_depth:
        return []

    lines: list[KitLine] = []
    for child, qty in components_of(item):
        qty = float(qty)
        if qty < 0:
            raise ValueError(f"Отрицательное количество {item}/{child}: {qty}")
        node = classify(child)

        if node.kind == SKIP:
            continue

        if node.kind == RECURSE:
            # Фантом: строки нет, спрос проносим прозрачно к его компонентам.
            if child in stack:
                continue  # цикл BOM — разрываем
            for sub in _subkit(child, components_of, classify, max_depth, memo, stack | {child}):
                lines.append(KitLine(sub.item, sub.qty_per_unit * qty, sub.warehouse))
            continue

        # BOUNDARY — точка развязки: строка на эту деталь.
        if not node.warehouse:
            raise ValueError(f"Классификатор не вернул склад для границы «{child}»")
        lines.append(KitLine(child, qty, node.warehouse))

        # Производимый буфер с компонентами — продолжаем сквозь него.
        if node.explode_through and child not in stack:
            for sub in _subkit(child, components_of, classify, max_depth, memo, stack | {child}):
                lines.append(KitLine(sub.item, sub.qty_per_unit * qty, sub.warehouse))

    memo[item] = lines
    return lines

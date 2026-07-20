"""Агрегация закупного спроса от плиток барабана — чистое ядро (без Frappe).

Второй питатель контура закупок: «спрос барабана → Material Request ядра».
В отличие от программного питателя (`purchase_plan.explode_program_demand`,
разворот BOM производственной программы), здесь источником спроса служат
плитки активного графика барабана, а разворот кита каждого SKU выполняется
многоуровневым `demand_explosion.explode_kit` (сквозь буферизованные узлы).

Этот модуль — чистый редьюсер: он НЕ ходит в БД. Frappe-адаптер
(`purchase_plan.explode_drum_demand`) снимает плитки, строит строки кита и
классификацию снабжения, а затем зовёт `aggregate_purchase_demand`.

Контракт результата совпадает с `explode_program_demand`:
`{item: {"qty": float, "earliest_need_date": datetime.date | None}}`,
поэтому обёртка подставляется в `load_demand` как drop-in.


Портировано из prodflow prodflow/services/purchase_demand.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date


def aggregate_purchase_demand(
    slot_demand: Iterable[tuple[str, float, date | None]],
    lines_of_sku: Callable[[str], Iterable[tuple[str, float, bool]]],
) -> dict[str, dict]:
    """Суммарный закупной спрос по плиткам барабана.

    Аргументы:
      - `slot_demand`: последовательность `(sku, qty, need_date)` — плитки
        барабана (SKU готового изделия, кол-во, плановая дата сборки).
      - `lines_of_sku(sku)`: строки развёрнутого кита SKU —
        `[(item, qty_per_unit, is_purchase)]`, где `qty_per_unit` — кол-во
        детали на единицу SKU (накопленное по всем уровням развязки), а
        `is_purchase` помечает закупные строки.

    Возвращает `{item: {"qty": Σ(slot_qty × qty_per_unit),
    "earliest_need_date": min(need_date)}}` ТОЛЬКО по закупным строкам
    (`is_purchase=True`). Суммирование идёт по всем плиткам и всем путям
    кита; `earliest_need_date` — минимальная дата среди плиток, где деталь
    встретилась (даты сравниваются как `datetime.date`; `None` игнорируются).

    Кит каждого уникального SKU запрашивается через `lines_of_sku` один раз
    (мемоизация) — при нескольких плитках одного SKU разворот не повторяется.
    Результат детерминирован при детерминированных входах.
    """
    kit_memo: dict[str, list[tuple[str, float, bool]]] = {}
    aggregate: dict[str, dict] = {}

    for sku, qty, need_date in slot_demand:
        slot_qty = float(qty)
        if slot_qty <= 0:
            continue

        lines = kit_memo.get(sku)
        if lines is None:
            lines = list(lines_of_sku(sku))
            kit_memo[sku] = lines

        for item, qty_per_unit, is_purchase in lines:
            if not is_purchase:
                continue
            per = float(qty_per_unit)
            if per < 0:
                raise ValueError(f"Отрицательное кол-во в ките {sku}/{item}: {per}")
            add = slot_qty * per
            if add <= 0:
                continue
            entry = aggregate.get(item)
            if entry is None:
                aggregate[item] = {"qty": add, "earliest_need_date": need_date}
            else:
                entry["qty"] += add
                cur = entry["earliest_need_date"]
                if need_date is not None and (cur is None or need_date < cur):
                    entry["earliest_need_date"] = need_date

    return aggregate

"""Чистое ядро расчёта NFP позиции супермаркета (без frappe).

    NFP = Остаток + ОткрытоеПополнение − КвалифСпрос

Природа позиции (производимая vs закупная) определяет ИСТОЧНИКИ остатка и
открытого пополнения, но не саму арифметику. Здесь — арифметика и выбор
источника по флагу is_purchased; выборки Frappe (снапшот, Bin, WO) остаются
в nfp_service.

Почему у закупных источники другие (значения подставляет nfp_service):
- Остаток закупного размазан: буфер номинально числится на складе
  поступления (№1 — под переработку, №4 — комплектующие), но собственный
  запас лежит по легаси-складам (Сборка, Разработка), в WIP участков и в
  точке выдачи №2 рядом с участками. Считать остаток по ОДНОМУ складу полки
  значит выдумывать дефицит (метиз на №2 → «нет на №4» → повторный заказ).
  Берём остаток по предприятию (сумма минус игнорируемые склады).
- Открытое пополнение закупного ведёт САМО ЯДРО ERPNext: открытые Purchase
  Order (Bin.ordered_qty) и Material Request (Bin.indented_qty), а не
  Work Order (тот бывает только у производимых).

Всё покрыто pytest (test_nfp_core.py) — frappe в окружении теста не нужен.


Портировано из prodflow prodflow/services/feeder/nfp_core.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections.abc import Iterable


def compute_nfp(stock: float, supply: float, demand: float) -> float:
    """NFP = Остаток + ОткрытоеПополнение − КвалифСпрос."""
    return float(stock) + float(supply) - float(demand)


def is_purchased(supply_type: str) -> bool:
    """Закупная позиция = всё, что не производится своими руками.

    Классификатор снабжения (gate_service._supply_type, повторён в
    nfp_service._supply_type) возвращает Manufacture / Subcontract /
    Purchase / Unknown. Производимая = Manufacture: открытый Work Order
    пополнения существует только у неё, а остаток передела ложится на склад
    полки. Всё остальное (Purchase / Subcontract / Unknown) — закупное:
    остаток по предприятию, пополнение из Bin ядра. Для не-Manufacture
    WO-пополнение всё равно 0, поэтому выбор безопасен даже для Unknown.
    """
    return supply_type != "Manufacture"


def select_stock(purchased: bool, shelf_stock: float, enterprise_stock: float) -> float:
    """Источник остатка по природе позиции.

    Закупная — остаток по предприятию (размазан по складам/WIP/точке выдачи);
    производимая — остаток конкретного склада полки (собственный передел
    ложится именно туда)."""
    return float(enterprise_stock) if purchased else float(shelf_stock)


def select_supply(purchased: bool, wo_supply: float, bin_supply: float) -> float:
    """Источник открытого пополнения по природе позиции.

    Закупная — открытые PO+MR ядра (Bin.ordered_qty+indented_qty);
    производимая — открытые Work Order пополнения."""
    return float(bin_supply) if purchased else float(wo_supply)


def bin_open_supply(rows: Iterable[dict]) -> float:
    """Открытое пополнение закупной позиции по строкам Bin ядра.

    Σ (ordered_qty + indented_qty): открытые Purchase Order (ordered_qty) и
    открытые Material Request (indented_qty), которые ведёт само ядро ERPNext
    (списываются, когда заказ поступил/закрыт). Оба поля неотрицательны по
    построению ядра; clamp'им на всякий случай, чтобы отрицательная аномалия
    не занижала пополнение соседей. Вход уже отфильтрован по item и
    не-игнорируемым складам (см. nfp_service._bin_open_supply_for)."""
    total = 0.0
    for row in rows:
        ordered = float(row.get("ordered_qty") or 0.0)
        indented = float(row.get("indented_qty") or 0.0)
        total += max(ordered, 0.0) + max(indented, 0.0)
    return total

"""Чистая логика покрытия ячеек программы выпуска плитками барабана (без Frappe).

Покрытие «программа → барабан»: сколько из планового количества ячейки
(изделие × бакет периода) уже релизнуто в производство и сколько выпущено.
Классификация статуса ячейки и раскладка плиток по бакетам вынесены сюда,
чтобы покрыть pytest'ом без импорта frappe (production_program тянет frappe
на уровне модуля).


Портировано из prodflow prodflow/services/program_coverage.py, коммит b1ebde2. Чистое ядро (без frappe) — логика не изменялась, поправлены только импорты.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

EPS = 1e-6


def classify_cell(plan_qty: float, released_qty: float, produced_qty: float) -> str:
    """Статус ячейки покрытия по плану / релизу / выпуску.

    - нет плана (plan_qty ≤ EPS) → "none";
    - выпущено ≥ плана → "produced";
    - релизнуто ≥ плана → "covered";
    - что-то релизнуто → "partial";
    - иначе → "open".
    """
    if plan_qty <= EPS:
        return "none"
    if produced_qty + EPS >= plan_qty:
        return "produced"
    if released_qty + EPS >= plan_qty:
        return "covered"
    if released_qty > EPS:
        return "partial"
    return "open"


def assign_bucket(
    planned_date: date | None,
    bucket_starts: Sequence[date],
    period_end: date | None = None,
) -> int | None:
    """Индекс бакета для плановой даты плитки.

    `bucket_starts` — возрастающий список дат-границ бакетов (from_date ячеек
    программы). Бакет i покрывает [starts[i], starts[i+1]); последний —
    [starts[-1], period_end]. Дата раньше первой границы или (если задан
    `period_end`) позже периода — вне сетки, возвращается None. Покрытие меряем
    против плана, поэтому на вход подаётся именно плановая дата плитки, а не
    сдвинутая переносом.
    """
    if not bucket_starts or planned_date is None:
        return None
    if planned_date < bucket_starts[0]:
        return None
    if period_end is not None and planned_date > period_end:
        return None
    idx = 0
    for i, start in enumerate(bucket_starts):
        if start <= planned_date:
            idx = i
        else:
            break
    return idx


def coverage_percent(qty: float, released_qty: float) -> int:
    """Процент покрытия релизом, целое (0 при нулевом плане).

    Формула контракта: round(100 * released / qty). Перевыпуск/переоткрытие
    может дать > 100 — намеренно не капим, чтобы фронт видел факт переброса.
    """
    if qty <= EPS:
        return 0
    return round(100.0 * released_qty / qty)

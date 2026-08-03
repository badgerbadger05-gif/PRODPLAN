"""Факт выпуска на строку заказа, считанный назад из принятого Item Ledger.

CANON: «Факт выпуска — считанный назад результат проведения `СборкаЗапасов`
в принятом Item Ledger». Этот модуль — единственный вычислитель выпуска по
строке производственного заказа (`ProductionProduct`). Он ничего не пишет:
`ProductionProduct.produced_qty` / `remaining_qty` остаются кэшем этого чтения,
и обновлять их вправе только один писатель
(`production_order_sync.sync_production_facts`).

Второго канала факта нет: документы 1С здесь не читаются. В расчёт входят
только видимые в границе импорта поколения положительные `assembly_in`
StockLedgerEntry (`physical_visibility.visible_sle_query`).

Идентичность факта до заказа/строки — тот же материал, что и у
`historical_replay_persistence._identity_for_sle`, но с точностью до строки:

1. **Точная связь до строки.** `ProductionManufacture.exported_ref1c` и
   терминальный `SyncLink(source_doctype='manufacture')` (`success`/`posted`)
   называют локальную команду «Произвести», а та несёт `product_id`.
   Принимается только при совпадении номенклатуры факта и строки.
2. **Связь до заказа + FIFO по строкам.** `StockRecorderPull.order_ref`
   (шапка `СборкаЗапасов.ЗаказНаПроизводство_Key`), прямое совпадение
   `ProductionOrder.order_ref1c` с recorder'ом и `SyncLink` перемещений
   материалов дают заказ. Внутри заказа факт раскладывается по строкам этой
   же номенклатуры oldest-first (по `line_number`), с предпочтением строк с
   совпадающей характеристикой.

Неоднозначность (несколько заказов) и полное отсутствие связи сохраняются в
метриках и НЕ приписываются произвольной строке.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app import models

from .physical_visibility import PhysicalVisibilityError, visible_sle_query
from .recorder_identity import build_recorder_identity_index


ASSEMBLY_MOVEMENT_KIND = "assembly_in"
ZERO = Decimal("0")


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def _norm(value: Any) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class ProductionFactProjection:
    """Детерминированный выпуск по строкам заказов одного поколения."""

    ledger_generation_id: int
    cutoff: datetime | None
    physical_import_batch_id: int
    produced_by_product: dict[int, Decimal]
    facts: int
    fact_qty: Decimal
    matched_facts: int
    matched_qty: Decimal
    exact_link_facts: int
    order_scope_facts: int
    ambiguous_facts: int
    unmatched_facts: int
    surplus_qty: Decimal


def _visible_assembly_facts(
    db: Session,
    *,
    physical_import_batch_id: int,
    cutoff: datetime | None,
) -> list[models.StockLedgerEntry]:
    return (
        visible_sle_query(
            db,
            physical_import_batch_id=int(physical_import_batch_id),
            cutoff=cutoff,
        )
        .filter(
            models.StockLedgerEntry.movement_kind == ASSEMBLY_MOVEMENT_KIND,
            models.StockLedgerEntry.qty > 0,
        )
        .order_by(
            models.StockLedgerEntry.posting_at.asc(),
            models.StockLedgerEntry.id.asc(),
        )
        .all()
    )


def _line_sort_key(product: models.ProductionProduct) -> tuple[int, int, int]:
    line_number = product.line_number
    return (
        1 if line_number is None else 0,
        int(line_number or 0),
        int(product.product_id),
    )


def _order_lines(
    db: Session,
    cache: dict[tuple[int, int], list[models.ProductionProduct]],
    *,
    order_id: int,
    item_id: int,
) -> list[models.ProductionProduct]:
    key = (int(order_id), int(item_id))
    if key not in cache:
        cache[key] = sorted(
            db.query(models.ProductionProduct)
            .filter(
                models.ProductionProduct.order_id == int(order_id),
                models.ProductionProduct.item_id == int(item_id),
            )
            .all(),
            key=_line_sort_key,
        )
    return cache[key]


def _preferred_lines(
    lines: list[models.ProductionProduct],
    characteristic_ref: str,
) -> list[models.ProductionProduct]:
    if not characteristic_ref:
        return lines
    exact = [
        row for row in lines
        if _norm(row.characteristic_ref1c) == characteristic_ref
    ]
    return exact or lines


def _assign_fifo(
    candidates: Iterable[models.ProductionProduct],
    qty: Decimal,
    produced_by_product: dict[int, Decimal],
) -> Decimal:
    """Разложить факт по строкам oldest-first; вернуть неразмещённый излишек.

    Излишек не теряется и не приписывается строке сверх её обязательства:
    вызывающий сохраняет его отдельным ``surplus_qty``.
    """
    ordered = list(candidates)
    remaining = qty
    for product in ordered:
        if remaining <= ZERO:
            break
        assigned = produced_by_product.get(int(product.product_id), ZERO)
        capacity = _dec(product.quantity) - assigned
        if capacity <= ZERO:
            continue
        take = capacity if capacity < remaining else remaining
        produced_by_product[int(product.product_id)] = assigned + take
        remaining -= take
    return max(remaining, ZERO)


def derive_production_output(
    db: Session,
    *,
    ledger_generation_id: int,
    cutoff: datetime | None = None,
) -> ProductionFactProjection:
    """Свести видимый выпуск одного поколения к строкам производственных заказов.

    Чтение без записи. `cutoff` по умолчанию берётся у поколения — один cutoff
    на все связанные проекции (`planning-truth-contract` §Инварианты 4).
    """
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise PhysicalVisibilityError(
            f"Ledger generation {ledger_generation_id} does not exist"
        )
    if generation.physical_import_batch_id is None:
        raise PhysicalVisibilityError(
            f"Ledger generation {generation.id} has no physical import batch"
        )
    effective_cutoff = cutoff if cutoff is not None else generation.cutoff

    rows = _visible_assembly_facts(
        db,
        physical_import_batch_id=int(generation.physical_import_batch_id),
        cutoff=effective_cutoff,
    )
    index = build_recorder_identity_index(db, [_norm(row.recorder_ref) for row in rows])
    line_cache: dict[tuple[int, int], list[models.ProductionProduct]] = {}

    produced_by_product: dict[int, Decimal] = {}
    facts = 0
    fact_qty = ZERO
    matched_facts = 0
    matched_qty = ZERO
    exact_link_facts = 0
    order_scope_facts = 0
    ambiguous_facts = 0
    unmatched_facts = 0
    surplus_qty = ZERO

    for row in rows:
        qty = _dec(row.qty)
        if qty <= ZERO:
            continue
        facts += 1
        fact_qty += qty
        recorder_ref = _norm(row.recorder_ref)
        if not recorder_ref:
            unmatched_facts += 1
            continue

        exact_lines = [
            product
            for product in (
                db.get(models.ProductionProduct, product_id)
                for product_id in sorted(index.exact_product_ids.get(recorder_ref, ()))
            )
            if product is not None and int(product.item_id) == int(row.item_id)
        ]
        if len(exact_lines) == 1:
            surplus_qty += _assign_fifo(exact_lines, qty, produced_by_product)
            exact_link_facts += 1
            matched_facts += 1
            matched_qty += qty
            continue

        order_ids = index.order_ids.get(recorder_ref, set())
        if len(order_ids) > 1:
            ambiguous_facts += 1
            continue
        if not order_ids:
            unmatched_facts += 1
            continue
        candidates = _preferred_lines(
            _order_lines(
                db,
                line_cache,
                order_id=next(iter(order_ids)),
                item_id=int(row.item_id),
            ),
            _norm(row.characteristic_ref),
        )
        if not candidates:
            unmatched_facts += 1
            continue
        surplus_qty += _assign_fifo(candidates, qty, produced_by_product)
        order_scope_facts += 1
        matched_facts += 1
        matched_qty += qty

    return ProductionFactProjection(
        ledger_generation_id=int(generation.id),
        cutoff=effective_cutoff,
        physical_import_batch_id=int(generation.physical_import_batch_id),
        produced_by_product=produced_by_product,
        facts=facts,
        fact_qty=fact_qty,
        matched_facts=matched_facts,
        matched_qty=matched_qty,
        exact_link_facts=exact_link_facts,
        order_scope_facts=order_scope_facts,
        ambiguous_facts=ambiguous_facts,
        unmatched_facts=unmatched_facts,
        surplus_qty=surplus_qty,
    )

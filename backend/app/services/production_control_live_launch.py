"""Исполнительный факт запуска, появившийся после cutoff принятого поколения.

Канон разделяет неподвижное и подвижное: строки плана, развёртка BOM, полный
резерв и исходная потребность пополнения заморожены, а «состояние
исполнительных заказов» продолжает меняться. Заказ, открытый после cutoff
принятого поколения, физически не может оказаться в его неизменяемом снимке —
но документ уже создан в 1С и лежит в исполнительных таблицах. Пока не примут
следующее поколение (такт около часа), журнал показывал бы «Не создан» по
заказу, который оператор держит в руках, а маршрутный лист по нему не
печатался бы вовсе.

Здесь накладывается ТОЛЬКО исполнительная часть строки: идентификация заказа и
его состояние. Ни одна плановая величина не пересчитывается и снимок не
переписывается — замороженная строка сохраняет все опубликованные вместе с ней
числа, включая потребность, покрытие и обеспеченность. Это наложение факта на
план, а не второй движок расчёта.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, MutableMapping, Sequence

from sqlalchemy.orm import Session, joinedload

from app import models
from app.services.production_output_truth import accepted_product_output


def _iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


def _datetime_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def launched_after_cutoff(
    db: Session,
    *,
    cutoff: datetime | None,
    requirement_ids: Sequence[int],
) -> dict[int, tuple[models.ProductionProduct, models.ProductionOrder]]:
    """Живые исполнительные строки, созданные после cutoff, по MRP-потребности.

    Ключ — `source_mrp_requirement_id`: именно он связывает предложение журнала
    с созданным по нему заказом. Удалённые заказы игнорируются: снятая пометка
    удаления в 1С не должна воскрешать строку.
    """
    if cutoff is None:
        return {}
    ids = sorted({int(value) for value in requirement_ids if value is not None})
    if not ids:
        return {}
    rows = (
        db.query(models.ProductionProduct, models.ProductionOrder)
        .join(
            models.ProductionOrder,
            models.ProductionOrder.order_id == models.ProductionProduct.order_id,
        )
        .filter(
            models.ProductionProduct.source_mrp_requirement_id.in_(ids),
            models.ProductionOrder.deletion_mark.is_(False),
            models.ProductionOrder.created_at > cutoff,
        )
        .order_by(models.ProductionProduct.product_id.asc())
        .all()
    )
    resolved: dict[int, tuple[models.ProductionProduct, models.ProductionOrder]] = {}
    for product, order in rows:
        key = int(product.source_mrp_requirement_id)
        # Одну потребность могли запускать частями. Берём самый ранний заказ,
        # чтобы строка журнала не «прыгала» между документами при обновлении.
        resolved.setdefault(key, (product, order))
    return resolved


def overlay_launch_facts(
    db: Session,
    rows: Sequence[MutableMapping[str, Any]],
    *,
    cutoff: datetime | None,
) -> None:
    """Дописать в строки-предложения факт уже созданного заказа. Мутирует rows.

    Трогаются только исполнительные поля. Плановые величины строки (потребность,
    покрытие, обеспеченность, даты плана) остаются ровно теми, что были
    опубликованы в снимке.
    """
    pending: dict[int, MutableMapping[str, Any]] = {}
    for row in rows:
        if row.get("product_id") is not None:
            continue
        requirement_id = row.get("source_mrp_requirement_id")
        if requirement_id is None:
            continue
        pending.setdefault(int(requirement_id), row)
    if not pending:
        return

    launched = launched_after_cutoff(
        db, cutoff=cutoff, requirement_ids=tuple(pending)
    )
    for requirement_id, (product, order) in launched.items():
        row = pending.get(requirement_id)
        if row is None:
            continue
        order_ref1c = str(order.order_ref1c or "").strip()
        row["product_id"] = int(product.product_id)
        row["order_id"] = int(order.order_id)
        row["order_number"] = str(order.order_number or row.get("order_number") or "")
        row["order_prodplan_number"] = str(
            order.order_number or row.get("order_prodplan_number") or ""
        )
        row["order_date"] = _iso(order.order_date)
        row["order_ref1c"] = order_ref1c or None
        row["line_number"] = (
            int(product.line_number) if product.line_number is not None else None
        )
        row["materialized_order_qty"] = _to_float(product.quantity)
        row["opened_at"] = _iso(order.created_at)
        row["status"] = "created"
        # Повторный запуск той же потребности запрещён: заказ уже есть.
        # Закрытие в 1С доступно ровно на тех же условиях, что и у строк,
        # попавших в снимок штатно.
        row["available_actions"] = ["close_1c"] if order_ref1c else []
        row["selection_disabled_reason"] = None
        row["comment"] = (
            "Заказ создан после cutoff принятого поколения; "
            "плановые величины строки обновятся в следующем поколении"
        )


def overlay_execution_state(
    db: Session,
    rows: Sequence[MutableMapping[str, Any]],
) -> None:
    """Overlay mutable workshop facts on immutable journal rows.

    Printing and material-transfer workflow happen after a Ledger snapshot is
    accepted.  They are execution facts, not planning math, so the journal must
    show them immediately while leaving every frozen quantity untouched.
    """
    by_product_id = {
        int(row["product_id"]): row
        for row in rows
        if row.get("product_id") is not None
    }
    if not by_product_id:
        return

    states = (
        db.query(models.ProductionOrderLineState)
        .filter(
            models.ProductionOrderLineState.product_id.in_(
                sorted(by_product_id)
            )
        )
        .all()
    )
    for state in states:
        row = by_product_id.get(int(state.product_id))
        if row is None:
            continue
        line_status = str(state.status or "shortage")
        row["status"] = (
            "created" if line_status in {"shortage", "partial"}
            else "ready" if line_status == "assembled"
            else line_status
        )
        row["issue_status"] = str(state.issue_status or "not_requested")
        row["route_sheet_printed_at"] = _datetime_iso(
            state.route_sheet_printed_at
        )

    # Количество исполнительного документа — тоже исполнительный факт. Оператор
    # меняет его у локального заказа до выгрузки, и до следующего поколения
    # снимок нёс бы прежнее число вместе с пересчитанной по новому количеству
    # комплектацией. Плановые величины строки (потребность, покрытие, остаток
    # пополнения) при этом остаются снимочными.
    products = (
        db.query(models.ProductionProduct)
        .options(joinedload(models.ProductionProduct.order))
        .filter(models.ProductionProduct.product_id.in_(sorted(by_product_id)))
        .all()
    )
    for product in products:
        row = by_product_id.get(int(product.product_id))
        if row is None:
            continue
        output = accepted_product_output(product)
        # Строка-предложение, на которую наложили факт запуска, количество
        # документа не перенимает: её «количество» — это остаток к запуску по
        # расчёту, и смешивать два разных числа в одном поле нельзя.
        if row.get("work_item_id") is None:
            row["quantity"] = float(output.planned_qty)
            row["produced_qty"] = float(output.produced_qty)
            row["remaining_qty"] = float(output.remaining_qty)
        order = getattr(product, "order", None)
        locked = (
            bool(order is not None and order.order_ref1c)
            or float(output.produced_qty) > 1e-9
            or str(row.get("issue_status") or "") in {"exported", "posted"}
        )
        if locked and "edit_quantity" in (row.get("available_actions") or []):
            row["available_actions"] = [
                action
                for action in row["available_actions"]
                if action != "edit_quantity"
            ]


def route_sheets_after_cutoff(
    db: Session,
    product_ids: Sequence[int],
    *,
    cutoff: datetime | None,
    ledger_generation_id: int,
) -> dict[int, dict[str, Any]]:
    """Маршрутные листы для изделий, созданных после cutoff.

    Снимок таких изделий не содержит и содержать не может, поэтому payload
    собирается тем же каноническим сборщиком, что и при построении снимка —
    второго формата маршрутного листа не появляется.
    """
    if cutoff is None:
        return {}
    ids = sorted({int(value) for value in product_ids if value is not None})
    if not ids:
        return {}
    live_ids = [
        int(product_id)
        for (product_id,) in db.query(models.ProductionProduct.product_id)
        .join(
            models.ProductionOrder,
            models.ProductionOrder.order_id == models.ProductionProduct.order_id,
        )
        .filter(
            models.ProductionProduct.product_id.in_(ids),
            models.ProductionOrder.deletion_mark.is_(False),
            models.ProductionOrder.created_at > cutoff,
        )
        .all()
    ]
    if not live_ids:
        return {}
    from app.services.production_control_printing import (
        build_route_sheet_snapshot_payloads,
    )

    payloads = build_route_sheet_snapshot_payloads(
        db,
        product_ids=live_ids,
        ledger_generation_id=int(ledger_generation_id),
    )
    return {
        int(product_id): payload
        for product_id, payload in payloads.items()
        if isinstance(payload, Mapping)
    }

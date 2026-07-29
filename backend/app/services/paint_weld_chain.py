"""Цепочка открытия «окраска → сварка» из штатного журнала (этап 2).

См. .docs/paint_weld_chain_logic.md.

Логика открытия окрасочного заказа с автоматической цепочкой на сварку:

- Ledger-гард текущего принятого `MrpRequirement`/`ReservationEntry` даёт
  вердикт по сварной детали:
  * ``stock_covers`` / ``order_open`` / ``no_pair`` — сварочный заказ НЕ создаётся,
    окрасочный заказ создаётся/выгружается штатно;
  * ``need_weld`` — создаётся ПАРА заказов: сначала окрасочный (штатный путь
    журнала: production_orders/production_products + экспорт через
    ``one_c_production_order_export``), затем сварочный НА ОСНОВАНИИ окрасочного.

- сварочный заказ:
  * qty ограничено незакрытым `make`-обязательством Ledger за вычетом уже
    материализованных из него сварочных строк;
  * финиш сварки = старт окраски, старт сварки = финиш − buffer_days сварочного
    участка;
  * связь «на основании»: сварочный 1С-документ выгружается со штатными полями
    основания (``ЗаказНаПроизводствоОснование_Key`` + ``ДокументОснование``/
    ``_Type`` через ``basis_order_refs`` экспортёра). Локальная связь
    ``paint_weld_chain_links`` остаётся источником истины на стороне PRODPLAN
    (якорь идемпотентности), комментарий дублирует основание для людей.

- ``dry_run=True`` — полный предпросмотр (оба payload'а + вердикт гарда) без
  записи; ``dry_run=False`` — реальное создание в правильном порядке
  (окраска → сварка), идемпотентно при повторе (якорь — ``painted_order_id`` в
  ``paint_weld_chain_links`` и sync_link экспортёра).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..models import (
    Item,
    MrpRequirement,
    PaintWeldChainLink,
    PaintWeldPair,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionManufacture,
    ProductionProduct,
    ProductionResource,
    ReservationEntry,
)
from .mrp_mutation_guard import (
    MrpMutationLineageError,
    require_materialized_orders,
)
from .one_c_document_numbers import production_order_number
from .one_c_production_order_export import export_production_orders_to_1c
from .workshop_resolution import default_spec_ids_for_items, resolve_workshop_for_spec

# Fallback сварочного участка, если у сварной спеки не проставлен вид
# производства (resource_id=2 — сварочный участок, справочник участков).
WELD_RESOURCE_ID_FALLBACK = 2


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _parse_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _default_spec_id(db: Session, item_id: int) -> Optional[int]:
    return default_spec_ids_for_items(db, [int(item_id)]).get(int(item_id))


def _weld_buffer_days(db: Session, welded_item_id: int) -> int:
    """buffer_days сварочного участка (по спеке сварной → вид производства → участок)."""
    spec_id = _default_spec_id(db, welded_item_id)
    resource_id = resolve_workshop_for_spec(db, spec_id) or WELD_RESOURCE_ID_FALLBACK
    resource = (
        db.query(ProductionResource)
        .filter(ProductionResource.resource_id == int(resource_id))
        .first()
    )
    return int(resource.buffer_days or 0) if resource else 0


@dataclass(frozen=True)
class _PaintedContext:
    product: ProductionProduct
    order: ProductionOrder
    run: PlanningRun
    generation_id: int
    item_id: int
    qty: float
    start: Optional[date]
    finish: Optional[date]


@dataclass(frozen=True)
class _WeldObligation:
    requirement: MrpRequirement
    reservation: ReservationEntry
    available_qty: float
    allocated_qty: float
    open_orders: tuple[dict[str, Any], ...]


def _resolve_painted_context(
    db: Session,
    *,
    painted_product_id: Optional[int],
    painted_item_id: Optional[int],
    qty: Optional[float],
    planned_start: Any,
    planned_finish: Any,
) -> _PaintedContext:
    start = _parse_date(planned_start)
    finish = _parse_date(planned_finish)

    if painted_product_id is None:
        if painted_item_id is not None:
            raise ValueError(
                "painted_item_id+qty is an unpublished demand; publish it through "
                "the Ledger obligation refresh before opening the chain"
            )
        raise ValueError("painted_product_id is required")
    product = db.get(ProductionProduct, int(painted_product_id))
    if product is None:
        raise ValueError(f"painted_product_id={painted_product_id}: строка заказа не найдена")
    order = db.get(ProductionOrder, int(product.order_id))
    if order is None:
        raise ValueError(f"painted_product_id={painted_product_id}: заказ не найден")
    try:
        generation_id = require_materialized_orders(
            db, [order], consumer="paint_weld_chain.open"
        )
    except MrpMutationLineageError as exc:
        raise ValueError(str(exc)) from exc
    run = db.get(PlanningRun, int(order.source_run_id))
    if run is None:  # guarded above; keeps the context type total.
        raise ValueError("accepted planning run is unavailable")
    obligation_qty = _to_float(product.quantity)
    if obligation_qty <= 0:
        raise ValueError("painted obligation quantity must be positive")
    if qty is not None and abs(_to_float(qty) - obligation_qty) > 1e-9:
        raise ValueError(
            "qty must match the published painted obligation quantity"
        )
    state = (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == int(product.product_id))
        .one_or_none()
    )
    return _PaintedContext(
        product=product,
        order=order,
        run=run,
        generation_id=int(generation_id),
        item_id=int(product.item_id),
        qty=obligation_qty,
        start=start or (state.planned_start_date if state else None),
        finish=finish or (state.planned_finish_date if state else None),
    )


def _resolve_weld_obligation(
    db: Session,
    *,
    ctx: _PaintedContext,
    welded_item_id: int,
) -> _WeldObligation:
    requirement = (
        db.query(MrpRequirement)
        .filter(
            MrpRequirement.run_id == int(ctx.run.run_id),
            MrpRequirement.item_id == int(welded_item_id),
            MrpRequirement.freeze_version == int(ctx.run.active_freeze_version),
        )
        .with_for_update()
        .one_or_none()
    )
    if requirement is None:
        raise ValueError(
            "published welded obligation is unavailable for this paint order"
        )
    reservation = (
        db.query(ReservationEntry)
        .filter(
            ReservationEntry.ledger_generation_id == int(ctx.generation_id),
            ReservationEntry.requirement_id == int(requirement.id),
            ReservationEntry.run_id == int(ctx.run.run_id),
            ReservationEntry.freeze_version == int(ctx.run.active_freeze_version),
            ReservationEntry.realization_mode == "make",
        )
        .one_or_none()
    )
    if reservation is None:
        raise ValueError(
            "published welded Ledger reservation is unavailable"
        )
    rows = (
        db.query(ProductionProduct, ProductionOrder)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .filter(
            ProductionProduct.source_mrp_requirement_id == int(requirement.id),
            ProductionProduct.ledger_generation_id == int(ctx.generation_id),
            ProductionOrder.source_run_id == int(ctx.run.run_id),
            ProductionOrder.deletion_mark.is_(False),
        )
        .with_for_update()
        .all()
    )
    # ReservationEntry is the only numeric execution source.  Distribute its
    # realized fold deterministically over linked order rows solely for
    # drill-down; no execution-allocation read model participates in the guard.
    realized_left = _to_float(reservation.realized_qty)
    open_by_product: dict[int, float] = {}
    for product, _order in sorted(rows, key=lambda pair: int(pair[0].product_id)):
        quantity = _to_float(product.quantity)
        realized = min(max(realized_left, 0.0), max(quantity, 0.0))
        realized_left -= realized
        open_by_product[int(product.product_id)] = max(quantity - realized, 0.0)
    allocated = sum(open_by_product.values())
    raw_outstanding = max(
        _to_float(reservation.reserved_qty) - _to_float(reservation.realized_qty),
        0.0,
    )
    available = max(raw_outstanding - allocated, 0.0)
    open_orders = tuple(
        {
            "number": str(order.order_number or ""),
            "qty": open_by_product[int(product.product_id)],
            "product_id": int(product.product_id),
        }
        for product, order in rows
    )
    return _WeldObligation(
        requirement=requirement,
        reservation=reservation,
        available_qty=available,
        allocated_qty=allocated,
        open_orders=open_orders,
    )


def _ensure_paint_order(
    db: Session, ctx: _PaintedContext
) -> Tuple[ProductionOrder, bool]:
    """The painted side must already be a current accepted materialization."""
    del db
    return ctx.order, True


def _weld_dates(
    db: Session, welded_item_id: int, paint_start: Optional[date]
) -> Tuple[Optional[date], Optional[date]]:
    """Финиш сварки = старт окраски; старт сварки = финиш − buffer_days участка."""
    if paint_start is None:
        return None, None
    buffer_days = _weld_buffer_days(db, welded_item_id)
    weld_finish = paint_start
    weld_start = paint_start - timedelta(days=int(buffer_days))
    return weld_start, weld_finish


def _ensure_weld_order(
    db: Session,
    *,
    ctx: _PaintedContext,
    obligation: _WeldObligation,
    pair: PaintWeldPair,
    painted_order: ProductionOrder,
    welded_item_id: int,
    weld_qty: float,
    weld_start: Optional[date],
    weld_finish: Optional[date],
    basis_comment: str,
) -> Tuple[ProductionOrder, bool]:
    """Вернуть (сварочный заказ, reused?). Идемпотентность — по
    painted_order_id в paint_weld_chain_links."""
    link = (
        db.query(PaintWeldChainLink)
        .filter(PaintWeldChainLink.painted_order_id == int(painted_order.order_id))
        .first()
    )
    if link is not None:
        order = (
            db.query(ProductionOrder)
            .filter(ProductionOrder.order_id == int(link.welded_order_id))
            .first()
        )
        if order is not None:
            product = (
                db.query(ProductionProduct)
                .filter(ProductionProduct.order_id == int(order.order_id))
                .one_or_none()
            )
            if (
                product is None
                or int(order.source_run_id or -1) != int(ctx.run.run_id)
                or int(product.ledger_generation_id or -1) != int(ctx.generation_id)
                or int(product.source_mrp_requirement_id or -1)
                != int(obligation.requirement.id)
            ):
                raise ValueError("existing paint/weld chain has stale Ledger lineage")
            return order, True

    if weld_qty <= 1e-9 or weld_qty > obligation.available_qty + 1e-9:
        raise ValueError("weld quantity exceeds the outstanding Ledger obligation")
    # Exporting the painted parent may commit its own 1C sync state.  Re-lock
    # and re-read the shared requirement immediately before allocating the
    # welded slice so another painted order cannot spend the same quantity in
    # that gap.
    current = _resolve_weld_obligation(
        db, ctx=ctx, welded_item_id=int(welded_item_id)
    )
    if (
        int(current.requirement.id) != int(obligation.requirement.id)
        or weld_qty > current.available_qty + 1e-9
    ):
        raise ValueError("weld quantity exceeds the outstanding Ledger obligation")
    obligation = current
    order = ProductionOrder(
        order_number=f"PWC-W-{int(painted_order.order_id)}",
        order_date=painted_order.order_date,
        order_ref1c=None,
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=int(ctx.run.run_id),
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=int(order.order_id),
        item_id=int(welded_item_id),
        line_number=1,
        quantity=weld_qty,
        produced_qty=0,
        remaining_qty=weld_qty,
        spec_id=_default_spec_id(db, welded_item_id),
        source_mrp_requirement_id=int(obligation.requirement.id),
        source_mrp_allocation_key=(
            f"paint_weld:{int(painted_order.order_id)}:"
            f"requirement:{int(obligation.requirement.id)}"
        ),
        ledger_generation_id=int(ctx.generation_id),
    )
    db.add(product)
    db.flush()
    db.add(
        ProductionOrderLineState(
            product_id=int(product.product_id),
            status="shortage",
            issue_status="not_requested",
            planned_start_date=weld_start,
            planned_finish_date=weld_finish,
            comment=basis_comment,
        )
    )
    db.add(
        PaintWeldChainLink(
            painted_order_id=int(painted_order.order_id),
            welded_order_id=int(order.order_id),
            pair_id=int(pair.id),
        )
    )
    db.flush()
    return order, False


def _basis_comment(paint_order: ProductionOrder) -> str:
    ref = str(paint_order.order_ref1c or "").strip()
    number = production_order_number(paint_order)
    if ref:
        return f"основание: окрасочный заказ {number} (1С ref {ref})"
    return f"основание: окрасочный заказ {number}"


def _order_payload(export_summary: Dict[str, Any], order_id: int) -> Optional[Dict[str, Any]]:
    for row in export_summary.get("payloads", []) or []:
        if int(row.get("order_id", -1)) == int(order_id):
            return row.get("payload")
    return None


def _order_export_entry(
    export_summary: Dict[str, Any], order_id: int
) -> Optional[Dict[str, Any]]:
    for row in export_summary.get("entries", []) or []:
        try:
            row_order_id = int(row.get("order_id", -1))
        except (TypeError, ValueError):
            continue
        if row_order_id == int(order_id):
            return row
    return None


def _parent_export_failure(
    export_summary: Dict[str, Any], *, order_id: int
) -> Optional[str]:
    """Причина, по которой окрасочный заказ нельзя считать выгруженным в 1С.

    Контракт .docs/one_c_export_from_prodplan.md: «Если основание неизвестно или
    родительский документ ещё не создан в 1С, дочерний документ выгружать
    нельзя». Значит результат экспорта родителя обязан анализироваться, а не
    приниматься на веру по общему ``status`` сводки: батч-статус ``ok`` ничего
    не говорит о конкретной строке, а строка без ``target_ref_key`` — это
    ненайденное основание. Возвращает None, когда основание подтверждено.
    """
    entry = _order_export_entry(export_summary, int(order_id))
    if entry is None:
        skipped = [
            str(row.get("reason") or row)
            for row in (export_summary.get("skipped_rows") or [])
            if str(row.get("order_id", "")) == str(order_id)
        ]
        return "; ".join(skipped) or "1С не вернула результат по окрасочному заказу"
    status = str(entry.get("status") or "")
    if status not in ("created", "existing"):
        return str(
            entry.get("error")
            or entry.get("reason")
            or f"статус выгрузки заказа — '{status or 'unknown'}'"
        )
    if not str(entry.get("target_ref_key") or "").strip():
        return "1С не вернула Ref_Key окрасочного заказа"
    return None


def open_paint_chain(
    db: Session,
    *,
    painted_product_id: Optional[int] = None,
    painted_item_id: Optional[int] = None,
    qty: Optional[float] = None,
    planned_start: Any = None,
    planned_finish: Any = None,
    dry_run: bool = True,
    initiated_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Открыть окрасочный заказ + (при need_weld) сварочный на основании.

    Возвращает вердикт гарда, сводку по обоим заказам и — в dry_run — payload'ы,
    которые ушли бы в 1С.
    """
    ctx = _resolve_painted_context(
        db,
        painted_product_id=painted_product_id,
        painted_item_id=painted_item_id,
        qty=qty,
        planned_start=planned_start,
        planned_finish=planned_finish,
    )
    pair = (
        db.query(PaintWeldPair)
        .filter(
            PaintWeldPair.painted_item_id == int(ctx.item_id),
            PaintWeldPair.is_active.is_(True),
        )
        .one_or_none()
    )
    obligation: Optional[_WeldObligation] = None
    if pair is None:
        verdict = "no_pair"
        guard = {
            "painted_item_id": int(ctx.item_id),
            "ledger_generation_id": int(ctx.generation_id),
            "welded_item": None,
            "outstanding_qty": 0.0,
            "allocated_qty": 0.0,
            "open_orders": [],
            "verdict": verdict,
        }
    else:
        obligation = _resolve_weld_obligation(
            db, ctx=ctx, welded_item_id=int(pair.welded_item_id)
        )
        if obligation.available_qty > 1e-9:
            verdict = "need_weld"
        elif obligation.allocated_qty > 1e-9:
            verdict = "order_open"
        else:
            verdict = "stock_covers"
        welded_item = db.get(Item, int(pair.welded_item_id))
        guard = {
            "painted_item_id": int(ctx.item_id),
            "ledger_generation_id": int(ctx.generation_id),
            "requirement_id": int(obligation.requirement.id),
            "reservation_id": int(obligation.reservation.id),
            "welded_item": {
                "item_id": int(pair.welded_item_id),
                "item_code": str(welded_item.item_code or "") if welded_item else "",
                "item_name": str(welded_item.item_name or "") if welded_item else "",
            },
            "outstanding_qty": round(obligation.available_qty, 3),
            "allocated_qty": round(obligation.allocated_qty, 3),
            "open_orders": list(obligation.open_orders),
            "verdict": verdict,
        }

    result: Dict[str, Any] = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "verdict": verdict,
        "guard": guard,
        "weld_needed": False,
        "initiated_by": initiated_by,
        "painted": None,
        "welded": None,
    }

    try:
        # ----- окрасочный заказ (штатно, всегда) -----
        paint_order, paint_reused = _ensure_paint_order(db, ctx)

        # Идемпотентность: если цепочка уже открывалась для этого окрасочного
        # заказа, гард увидит собственный открытый сварочный заказ (вердикт
        # order_open). Наличие связи форсирует путь переиспользования, чтобы
        # повтор не создавал дубль и корректно показывал существующую пару.
        existing_link = (
            db.query(PaintWeldChainLink)
            .filter(PaintWeldChainLink.painted_order_id == int(paint_order.order_id))
            .first()
        )
        weld_needed = verdict == "need_weld" or existing_link is not None
        result["weld_needed"] = weld_needed

        welded_item_id = int(pair.welded_item_id) if pair is not None else None
        weld_qty = 0.0
        weld_start: Optional[date] = None
        weld_finish: Optional[date] = None
        if weld_needed and welded_item_id is not None:
            if existing_link is not None:
                existing_product = (
                    db.query(ProductionProduct)
                    .filter(
                        ProductionProduct.order_id
                        == int(existing_link.welded_order_id)
                    )
                    .one_or_none()
                )
                if existing_product is None:
                    raise ValueError("existing paint/weld chain has no welded product")
                weld_qty = _to_float(existing_product.quantity)
            else:
                if obligation is None:
                    raise ValueError("published welded obligation is unavailable")
                weld_qty = min(ctx.qty, obligation.available_qty)
            weld_start, weld_finish = _weld_dates(db, welded_item_id, ctx.start)

        if dry_run:
            paint_export = export_production_orders_to_1c(
                db, [int(paint_order.order_id)], dry_run=True
            )
            result["painted"] = {
                "order_id": int(paint_order.order_id),
                "order_number": str(paint_order.order_number or ""),
                "item_id": ctx.item_id,
                "qty": round(ctx.qty, 3),
                "planned_start_date": ctx.start.isoformat() if ctx.start else None,
                "planned_finish_date": ctx.finish.isoformat() if ctx.finish else None,
                "reused": paint_reused,
                "payload": _order_payload(paint_export, int(paint_order.order_id)),
            }
            if weld_needed and welded_item_id is not None:
                pair = _active_pair(db, ctx.item_id)
                weld_order, weld_reused = _ensure_weld_order(
                    db,
                    ctx=ctx,
                    obligation=obligation,
                    pair=pair,
                    painted_order=paint_order,
                    welded_item_id=welded_item_id,
                    weld_qty=weld_qty,
                    weld_start=weld_start,
                    weld_finish=weld_finish,
                    basis_comment=_basis_comment(paint_order),
                )
                weld_export = export_production_orders_to_1c(
                    db,
                    [int(weld_order.order_id)],
                    dry_run=True,
                    comment_suffixes={int(weld_order.order_id): _basis_comment(paint_order)},
                    # В предпросмотре ref окрасочного есть только если он уже
                    # выгружен в 1С; при реальном открытии поле появится всегда.
                    basis_order_refs={
                        int(weld_order.order_id): str(paint_order.order_ref1c or "")
                    },
                )
                result["welded"] = {
                    "order_id": int(weld_order.order_id),
                    "order_number": str(weld_order.order_number or ""),
                    "item_id": welded_item_id,
                    "qty": weld_qty,
                    "planned_start_date": weld_start.isoformat() if weld_start else None,
                    "planned_finish_date": weld_finish.isoformat() if weld_finish else None,
                    "basis": _basis_comment(paint_order),
                    "reused": weld_reused,
                    "payload": _order_payload(weld_export, int(weld_order.order_id)),
                }
            # Предпросмотр ничего не пишет.
            db.rollback()
            return result

        # ----- реальная запись: окраска, затем сварка -----
        paint_export = export_production_orders_to_1c(
            db,
            [int(paint_order.order_id)],
            dry_run=False,
        )
        db.flush()
        db.refresh(paint_order)
        # Основание дочернего документа обязано быть подтверждённым, а не
        # предполагаемым: без этой проверки сварочный заказ уходил в 1С без
        # ЗаказНаПроизводствоОснование_Key, как только окрасочный не создался.
        paint_failure = _parent_export_failure(
            paint_export, order_id=int(paint_order.order_id)
        )
        paint_ref = str(paint_order.order_ref1c or "").strip()
        if paint_failure is None and not paint_ref:
            paint_failure = "order_ref1c окрасочного заказа остался пустым"
        if paint_failure is not None:
            raise ValueError(
                f"Окрасочный заказ {production_order_number(paint_order)} не выгружен "
                f"в 1С: {paint_failure}. Сварочный заказ не выгружается без "
                "подтверждённого основания."
            )
        result["painted"] = {
            "order_id": int(paint_order.order_id),
            "order_number": str(paint_order.order_number or ""),
            "order_ref1c": str(paint_order.order_ref1c or "") or None,
            "item_id": ctx.item_id,
            "qty": round(ctx.qty, 3),
            "planned_start_date": ctx.start.isoformat() if ctx.start else None,
            "planned_finish_date": ctx.finish.isoformat() if ctx.finish else None,
            "reused": paint_reused,
            "export": paint_export,
        }

        if weld_needed and welded_item_id is not None:
            pair = _active_pair(db, ctx.item_id)
            basis = _basis_comment(paint_order)
            weld_order, weld_reused = _ensure_weld_order(
                db,
                ctx=ctx,
                obligation=obligation,
                pair=pair,
                painted_order=paint_order,
                welded_item_id=welded_item_id,
                weld_qty=weld_qty,
                weld_start=weld_start,
                weld_finish=weld_finish,
                basis_comment=basis,
            )
            weld_export = export_production_orders_to_1c(
                db,
                [int(weld_order.order_id)],
                dry_run=False,
                comment_suffixes={int(weld_order.order_id): basis},
                basis_order_refs={int(weld_order.order_id): paint_ref},
            )
            db.refresh(weld_order)
            weld_failure = _parent_export_failure(
                weld_export, order_id=int(weld_order.order_id)
            )
            if weld_failure is None and not str(weld_order.order_ref1c or "").strip():
                weld_failure = "order_ref1c сварочного заказа остался пустым"
            result["welded"] = {
                "order_id": int(weld_order.order_id),
                "order_number": str(weld_order.order_number or ""),
                "order_ref1c": str(weld_order.order_ref1c or "") or None,
                "item_id": welded_item_id,
                "qty": weld_qty,
                "planned_start_date": weld_start.isoformat() if weld_start else None,
                "planned_finish_date": weld_finish.isoformat() if weld_finish else None,
                "basis": basis,
                "reused": weld_reused,
                "export": weld_export,
                "error": weld_failure,
            }
            if weld_failure is not None:
                # Окрасочный заказ уже живёт в 1С (экспортёр коммитит построчно),
                # поэтому откатывать нечего — но и «ok» это не есть.
                result["status"] = "partial_error"
                result["error"] = (
                    f"Окрасочный заказ выгружен, сварочный "
                    f"{production_order_number(weld_order)} — нет: {weld_failure}. "
                    "Повторите открытие цепочки."
                )

        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _active_pair(db: Session, painted_item_id: int) -> PaintWeldPair:
    pair = (
        db.query(PaintWeldPair)
        .filter(PaintWeldPair.painted_item_id == int(painted_item_id))
        .filter(PaintWeldPair.is_active.is_(True))
        .first()
    )
    if pair is None:
        raise ValueError(f"painted_item_id={painted_item_id}: активная пара не найдена")
    return pair


# ---------------------------------------------------------------------------
# Этап 4: одновременное закрытие обоих заказов цепочки из одного окна журнала.
# Выпуски обеих строк → СборкаЗапасов обоих заказов → ОДИН комбинированный
# СдельныйНаряд (см. one_c_piecework_export.export_chain_piecework_to_1c),
# который закрывает оба заказа.
# ---------------------------------------------------------------------------


def _chain_link_for_product(
    db: Session, product_id: int
) -> Tuple[PaintWeldChainLink, ProductionProduct, ProductionProduct]:
    """Найти цепочку по строке журнала (любая сторона) и оба продукта."""
    product = (
        db.query(ProductionProduct)
        .filter(ProductionProduct.product_id == int(product_id))
        .one_or_none()
    )
    if product is None:
        raise ValueError(f"product_id={product_id}: строка заказа не найдена")
    link = (
        db.query(PaintWeldChainLink)
        .filter(
            (PaintWeldChainLink.painted_order_id == int(product.order_id))
            | (PaintWeldChainLink.welded_order_id == int(product.order_id))
        )
        .first()
    )
    if link is None:
        raise ValueError(
            f"product_id={product_id}: цепочка окраска↔сварка для этой строки не найдена"
        )

    def _first_product(order_id: int) -> ProductionProduct:
        row = (
            db.query(ProductionProduct)
            .filter(ProductionProduct.order_id == int(order_id))
            .order_by(ProductionProduct.line_number.asc(), ProductionProduct.product_id.asc())
            .first()
        )
        if row is None:
            raise ValueError(f"order_id={order_id}: в заказе цепочки нет строк")
        return row

    return (
        link,
        _first_product(int(link.painted_order_id)),
        _first_product(int(link.welded_order_id)),
    )


def _latest_manufacture(db: Session, product_id: int):
    from ..models import ProductionManufacture

    return (
        db.query(ProductionManufacture)
        .filter(ProductionManufacture.product_id == int(product_id))
        .filter(ProductionManufacture.status != "cancelled")
        .order_by(ProductionManufacture.manufacture_id.desc())
        .first()
    )


def close_paint_chain(
    db: Session,
    *,
    product_id: int,
    weld_qty: Optional[float] = None,
    paint_qty: Optional[float] = None,
    executor: Optional[str] = None,
    weld_operation_executors: Optional[Any] = None,
    paint_operation_executors: Optional[Any] = None,
    comment: Optional[str] = None,
    dry_run: bool = True,
    initiated_by: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Закрыть цепочку «окраска↔сварка» одним действием из окна журнала.

    Порядок (dry_run=False): выпуск сварочной строки → выпуск окрасочной →
    экспорт обеих СборкаЗапасов → один комбинированный СдельныйНаряд
    (основание — окрасочная СборкаЗапасов), закрывающий оба заказа.

    Количества по умолчанию — remaining_qty строк; если сторона уже
    произведена полностью, переиспользуется её последний выпуск. Требования
    штатного produce_line (проведённые перемещения материалов) сохраняются.

    dry_run=True — предпросмотр: что будет произведено и, если оба выпуска уже
    существуют, payload комбинированного сдельного.

    Закрытие возобновляемо (см. produce_line после доработки «докат цепочки»).
    Обе СборкиЗапасов уходят одним вызовом экспортёра, но их результаты
    разбираются построчно: проведённая в 1С сборка НИКОГДА не откатывается
    из-за того, что вторая не прошла. При частичном успехе ответ несёт
    ``status='partial'`` / ``chain_state='partially_posted'`` /
    ``resume_required=True``, а повторный вызов докатывает недостающую сборку
    и комбинированный наряд, не создавая дублей: успешная сторона узнаётся по
    ``exported_ref1c`` (produce_line возвращает её как ``resumed``), а
    неудачная — локальный выпуск без 1С-документа — откатывается, чтобы гард
    «всё уже скомандовано» не заблокировал докат.
    """
    from .one_c_manufacture_export import export_manufactures_to_1c
    from .one_c_piecework_export import export_chain_piecework_to_1c
    from .production_control_production_flow import produce_line, rollback_local_manufacture

    link, paint_product, weld_product = _chain_link_for_product(db, product_id)

    def _plan_side(
        product: ProductionProduct, qty: Optional[float]
    ) -> Dict[str, Any]:
        remaining = _to_float(product.remaining_qty)
        planned = _to_float(qty) if qty is not None else remaining
        existing = _latest_manufacture(db, int(product.product_id))
        if planned <= 0 and existing is None:
            raise ValueError(
                f"product_id={product.product_id}: нечего закрывать — "
                "ничего не произведено и количество к выпуску 0"
            )
        if planned < 0:
            raise ValueError(
                f"product_id={product.product_id}: количество к выпуску должно быть неотрицательным"
            )
        return {
            "product_id": int(product.product_id),
            "order_id": int(product.order_id),
            "remaining_qty": remaining,
            "qty_to_produce": planned if remaining > 0 and planned > 0 else 0.0,
            "existing_manufacture_id": int(existing.manufacture_id) if existing else None,
            "manufacture_ref1c": (
                str(existing.exported_ref1c or "").strip() or None if existing else None
            ),
        }

    weld_plan = _plan_side(weld_product, weld_qty)
    paint_plan = _plan_side(paint_product, paint_qty)

    result: Dict[str, Any] = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "initiated_by": initiated_by,
        "chain_link_id": int(link.id),
        "weld": weld_plan,
        "paint": paint_plan,
    }

    if dry_run:
        posted = [
            side
            for side, plan in (("weld", weld_plan), ("paint", paint_plan))
            if plan["manufacture_ref1c"]
        ]
        result["posted_sides"] = posted
        result["pending_sides"] = [
            side for side in ("weld", "paint") if side not in posted
        ]
        result["chain_state"] = (
            "manufactures_posted"
            if len(posted) == 2
            else ("partially_posted" if posted else "not_started")
        )
        result["resume_required"] = len(posted) == 1
        if weld_plan["existing_manufacture_id"] and paint_plan["existing_manufacture_id"]:
            result["piecework_preview"] = export_chain_piecework_to_1c(
                db,
                weld_manufacture_id=weld_plan["existing_manufacture_id"],
                paint_manufacture_id=paint_plan["existing_manufacture_id"],
                dry_run=True,
            )
        else:
            result["piecework_preview"] = None
        return result

    created_manufacture_ids: List[int] = []

    def _ensure_manufacture(plan: Dict[str, Any], operation_executors: Any) -> int:
        if plan["qty_to_produce"] > 0:
            produced = produce_line(
                db,
                plan["product_id"],
                qty=plan["qty_to_produce"],
                executor=executor,
                operation_executors=operation_executors,
                comment=comment,
            )
            plan["produce"] = produced
            manufacture_id = int(produced["manufacture_id"])
            # Возобновлённый выпуск уже владеет документом 1С — он не «создан
            # этим вызовом» и не подлежит локальному откату.
            if not bool(produced.get("resumed")):
                created_manufacture_ids.append(manufacture_id)
            return manufacture_id
        plan["produce"] = None
        if plan["existing_manufacture_id"] is None:
            raise ValueError(
                f"product_id={plan['product_id']}: не найден выпуск для повторного закрытия"
            )
        return int(plan["existing_manufacture_id"])

    # Сварка первой: её выпуск — вход окраски.
    weld_manufacture_id = _ensure_manufacture(weld_plan, weld_operation_executors)
    paint_manufacture_id = _ensure_manufacture(paint_plan, paint_operation_executors)
    weld_plan["manufacture_id"] = weld_manufacture_id
    paint_plan["manufacture_id"] = paint_manufacture_id

    manufactures_export = export_manufactures_to_1c(
        db,
        [weld_manufacture_id, paint_manufacture_id],
        dry_run=False,
    )
    result["manufactures_export"] = manufactures_export
    export_entries = {
        int(entry.get("manufacture_id")): entry
        for entry in (manufactures_export.get("entries") or [])
        if entry.get("manufacture_id") is not None
    }
    posted_sides: List[str] = []
    pending_sides: List[str] = []
    failures: List[str] = []
    for side, plan, manufacture_id in (
        ("weld", weld_plan, weld_manufacture_id),
        ("paint", paint_plan, paint_manufacture_id),
    ):
        entry = export_entries.get(int(manufacture_id), {})
        persisted = db.get(ProductionManufacture, int(manufacture_id))
        exported_ref = str(
            entry.get("target_ref_key")
            or (persisted.exported_ref1c if persisted is not None else "")
            or ""
        ).strip()
        # Документ, созданный но не проведённый, экспортёр помечает status=error
        # и всё равно штампует exported_ref1c — такой стороне нельзя выдавать
        # «проведено».
        if exported_ref and str(entry.get("status") or "") != "error":
            plan["manufacture_ref1c"] = exported_ref
            posted_sides.append(side)
            continue
        reason = str(
            entry.get("error")
            or entry.get("reason")
            or f"manufacture_id={manufacture_id}: 1С не создала и не провела СборкаЗапасов"
        )
        plan["manufacture_ref1c"] = exported_ref or None
        plan["error"] = reason
        pending_sides.append(side)
        failures.append(f"{side}: {reason}")
        # Локальный выпуск без 1С-документа откатывается, иначе гард
        # «по этой строке уже создана команда на весь объём» заблокирует докат.
        if manufacture_id in created_manufacture_ids and not exported_ref:
            rollback_local_manufacture(db, manufacture_id)

    result["posted_sides"] = posted_sides
    result["pending_sides"] = pending_sides

    if pending_sides and not posted_sides:
        # Ни одна СборкаЗапасов не попала в 1С — докатывать нечего.
        raise ValueError("; ".join(failures) or "Ошибка экспорта СборкаЗапасов цепочки")
    if pending_sides:
        db.commit()
        result["status"] = "partial"
        result["chain_state"] = "partially_posted"
        result["resume_required"] = True
        result["error"] = "; ".join(failures)
        result["message"] = (
            "Цепочка частично проведена: СборкаЗапасов стороны "
            f"{', '.join(posted_sides)} в 1С, сторона "
            f"{', '.join(pending_sides)} — нет, комбинированный СдельныйНаряд не "
            "создавался. Требуется докат: повторите закрытие цепочки, проведённая "
            "сборка переиспользуется без дубля."
        )
        return result

    piecework_export = export_chain_piecework_to_1c(
        db,
        weld_manufacture_id=weld_manufacture_id,
        paint_manufacture_id=paint_manufacture_id,
        dry_run=False,
    )
    result["piecework_export"] = piecework_export
    if piecework_export.get("status") not in ("ok", "existing"):
        # Обе сборки живут в 1С — откатывать их нельзя, наряд докатывается
        # повторным закрытием (sync_link 'piecework' держит идемпотентность).
        db.commit()
        result["status"] = "partial"
        result["chain_state"] = "manufactures_posted_piecework_pending"
        result["resume_required"] = True
        result["error"] = str(
            piecework_export.get("error")
            or "1С не создала и не провела комбинированный СдельныйНаряд"
        )
        result["message"] = (
            "Обе СборкиЗапасов проведены в 1С, комбинированный СдельныйНаряд — нет. "
            "Требуется докат: повторите закрытие цепочки."
        )
        return result
    result["chain_state"] = "closed"
    result["resume_required"] = False
    result["ledger_readback"] = "queued"
    db.commit()
    return result

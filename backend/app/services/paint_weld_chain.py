"""Цепочка открытия «окраска → сварка» из штатного журнала (этап 2).

См. .docs/paint_weld_chain_logic.md.

Логика открытия окрасочного заказа с автоматической цепочкой на сварку:

- гард (`paint_weld_pairs.guard_paint_order`) даёт вердикт по сварной детали:
  * ``stock_covers`` / ``order_open`` / ``no_pair`` — сварочный заказ НЕ создаётся,
    окрасочный заказ создаётся/выгружается штатно;
  * ``need_weld`` — создаётся ПАРА заказов: сначала окрасочный (штатный путь
    журнала: production_orders/production_products + экспорт через
    ``one_c_production_order_export``), затем сварочный НА ОСНОВАНИИ окрасочного.

- сварочный заказ:
  * qty = qty окраски за вычетом эффективного остатка сварной (если частично
    покрыт);
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

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from ..models import (
    Item,
    PaintWeldChainLink,
    PaintWeldPair,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
)
from .one_c_document_numbers import production_order_number
from .one_c_production_order_export import export_production_orders_to_1c
from .paint_weld_pairs import guard_paint_order
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


class _PaintedContext:
    __slots__ = ("item_id", "qty", "start", "finish", "order_id")

    def __init__(
        self,
        *,
        item_id: int,
        qty: float,
        start: Optional[date],
        finish: Optional[date],
        order_id: Optional[int],
    ) -> None:
        self.item_id = item_id
        self.qty = qty
        self.start = start
        self.finish = finish
        self.order_id = order_id


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

    if painted_product_id is not None:
        product = (
            db.query(ProductionProduct)
            .filter(ProductionProduct.product_id == int(painted_product_id))
            .first()
        )
        if product is None:
            raise ValueError(f"painted_product_id={painted_product_id}: строка заказа не найдена")
        state = (
            db.query(ProductionOrderLineState)
            .filter(ProductionOrderLineState.product_id == int(product.product_id))
            .first()
        )
        resolved_qty = _to_float(qty) if qty else (_to_float(product.quantity) or _to_float(product.remaining_qty))
        return _PaintedContext(
            item_id=int(product.item_id),
            qty=resolved_qty,
            start=start or (state.planned_start_date if state else None),
            finish=finish or (state.planned_finish_date if state else None),
            order_id=int(product.order_id),
        )

    if painted_item_id is None:
        raise ValueError("нужен painted_product_id или painted_item_id")
    resolved_qty = _to_float(qty)
    if resolved_qty <= 0:
        raise ValueError("qty должен быть положительным")
    return _PaintedContext(
        item_id=int(painted_item_id),
        qty=resolved_qty,
        start=start,
        finish=finish,
        order_id=None,
    )


def _new_local_order(
    db: Session,
    *,
    order_number: str,
    item_id: int,
    qty: float,
    spec_id: Optional[int],
    start: Optional[date],
    finish: Optional[date],
) -> ProductionOrder:
    """Создать локальный заказ штатным для журнала способом (order/product/state).

    Только flush — commit/rollback контролирует вызывающий (dry_run).
    """
    order = ProductionOrder(
        order_number=order_number,
        order_date=datetime.now(timezone.utc),
        order_ref1c=None,
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=None,
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=int(order.order_id),
        item_id=int(item_id),
        line_number=1,
        quantity=qty,
        produced_qty=0,
        remaining_qty=qty,
        spec_id=spec_id,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductionOrderLineState(
            product_id=int(product.product_id),
            status="shortage",
            issue_status="not_requested",
            planned_start_date=start,
            planned_finish_date=finish,
        )
    )
    db.flush()
    return order


def _sync_local_order(
    db: Session,
    order: ProductionOrder,
    *,
    qty: float,
    start: Optional[date],
    finish: Optional[date],
) -> None:
    """Обновить qty/даты повторно используемого локального заказа (идемпотентность)."""
    product = (
        db.query(ProductionProduct)
        .filter(ProductionProduct.order_id == int(order.order_id))
        .order_by(ProductionProduct.line_number.asc())
        .first()
    )
    if product is None:
        return
    produced = _to_float(product.produced_qty)
    product.quantity = max(qty, produced)
    product.remaining_qty = max(qty - produced, 0.0)
    state = (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == int(product.product_id))
        .first()
    )
    if state is not None:
        if start is not None:
            state.planned_start_date = start
        if finish is not None:
            state.planned_finish_date = finish
    db.flush()


def _ensure_paint_order(
    db: Session, ctx: _PaintedContext
) -> Tuple[ProductionOrder, bool]:
    """Вернуть (окрасочный заказ, reused?). painted_product_id → существующий;
    ad-hoc (по item) → дедуп по номеру PWC-P-{item}, иначе создать."""
    if ctx.order_id is not None:
        order = (
            db.query(ProductionOrder)
            .filter(ProductionOrder.order_id == int(ctx.order_id))
            .first()
        )
        if order is None:
            raise ValueError(f"order_id={ctx.order_id}: заказ окраски не найден")
        return order, True

    order_number = f"PWC-P-{int(ctx.item_id)}"
    existing = (
        db.query(ProductionOrder)
        .filter(
            ProductionOrder.order_number == order_number,
            ProductionOrder.source == "mrp",
            ProductionOrder.deletion_mark.is_(False),
        )
        .order_by(ProductionOrder.order_id.desc())
        .first()
    )
    if existing is not None:
        _sync_local_order(db, existing, qty=ctx.qty, start=ctx.start, finish=ctx.finish)
        ctx.order_id = int(existing.order_id)
        return existing, True

    order = _new_local_order(
        db,
        order_number=order_number,
        item_id=ctx.item_id,
        qty=ctx.qty,
        spec_id=_default_spec_id(db, ctx.item_id),
        start=ctx.start,
        finish=ctx.finish,
    )
    ctx.order_id = int(order.order_id)
    return order, False


def _weld_qty(paint_qty: float, welded_stock_qty: float) -> float:
    """qty сварки = qty окраски за вычетом эффективного остатка сварной."""
    remaining = _to_float(paint_qty) - max(_to_float(welded_stock_qty), 0.0)
    return round(max(remaining, 0.0), 3)


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
            _sync_local_order(db, order, qty=weld_qty, start=weld_start, finish=weld_finish)
            return order, True

    order = _new_local_order(
        db,
        order_number=f"PWC-W-{int(painted_order.order_id)}",
        item_id=int(welded_item_id),
        qty=weld_qty,
        spec_id=_default_spec_id(db, welded_item_id),
        start=weld_start,
        finish=weld_finish,
    )
    # Локальная связь (источник истины) + локальный комментарий строки.
    db.add(
        PaintWeldChainLink(
            painted_order_id=int(painted_order.order_id),
            welded_order_id=int(order.order_id),
            pair_id=int(pair.id),
        )
    )
    state = (
        db.query(ProductionOrderLineState)
        .join(ProductionProduct, ProductionProduct.product_id == ProductionOrderLineState.product_id)
        .filter(ProductionProduct.order_id == int(order.order_id))
        .first()
    )
    if state is not None:
        state.comment = basis_comment
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


def open_paint_chain(
    db: Session,
    *,
    painted_product_id: Optional[int] = None,
    painted_item_id: Optional[int] = None,
    qty: Optional[float] = None,
    planned_start: Any = None,
    planned_finish: Any = None,
    dry_run: bool = True,
    allow_production: bool = False,
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
    guard = guard_paint_order(db, ctx.item_id, ctx.qty)
    verdict = str(guard["verdict"])

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

        welded_item_id = (
            int(guard["welded_item"]["item_id"]) if guard.get("welded_item") else None
        )
        weld_qty = 0.0
        weld_start: Optional[date] = None
        weld_finish: Optional[date] = None
        if weld_needed and welded_item_id is not None:
            weld_qty = _weld_qty(ctx.qty, guard.get("stock_qty", 0.0))
            weld_start, weld_finish = _weld_dates(db, welded_item_id, ctx.start)

        if dry_run:
            paint_export = export_production_orders_to_1c(
                db, [int(paint_order.order_id)], dry_run=True, allow_production=allow_production
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
                    allow_production=allow_production,
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
            allow_production=allow_production,
        )
        db.flush()
        db.refresh(paint_order)
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
                allow_production=allow_production,
                comment_suffixes={int(weld_order.order_id): basis},
                basis_order_refs={
                    int(weld_order.order_id): str(paint_order.order_ref1c or "")
                },
            )
            db.refresh(weld_order)
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
            }

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

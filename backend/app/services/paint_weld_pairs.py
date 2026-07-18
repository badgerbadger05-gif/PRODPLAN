"""Справочник пар «окрашенная ↔ сварная» (окраска ↔ сварка), этап 1.

Семейство «… после покраски»: в 1С окрашенная деталь = двухуровневый BOM, где
default-спека окрашенной содержит ровно один компонент типа «Сборка» — сварную
(неокрашенную) деталь. Пары строятся автоматически из спек (source='auto'),
ручные правки допустимы (source='manual').

См. .docs/paint_weld_chain_logic.md — утверждённое ТЗ. Этап 1 без записи в 1С:
только справочник, серость журнала/MRP, гард открытия и фильтр reconcile.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    DefaultSpecification,
    Item,
    PaintWeldPair,
    ProductionOrder,
    ProductionProduct,
    SpecComponent,
)
from .replenishment import REPLENISHMENT_FLOW_PRODUCTION, classify_replenishment_flow

# Признаки семейства по данным разведки (2026-07-18):
#   - окрашенная деталь: имя содержит «после покраски» (46 позиций);
#   - сварная (неокрашенная) деталь: имя содержит «после сварки» либо
#     «без покраски» (219 сварных в Производстве).
PAINTED_MARKER = "после покраски"
WELDED_MARKERS = ("после сварки", "без покраски")
ORPHAN_MARKER = "после сварки"

ASSEMBLY_COMPONENT_TYPE = "Сборка"


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _name_has(name: Optional[str], markers: Iterable[str]) -> bool:
    lowered = str(name or "").lower()
    return any(marker in lowered for marker in markers)


def _default_spec_id(db: Session, item_id: int) -> Optional[int]:
    row = (
        db.query(DefaultSpecification.spec_id)
        .filter(DefaultSpecification.item_id == int(item_id))
        .order_by(DefaultSpecification.id.asc())
        .first()
    )
    return int(row[0]) if row else None


def _detect_auto_pairs(db: Session) -> Dict[int, int]:
    """
    Вернуть {painted_item_id: welded_item_id} для семейства «после покраски».

    Пара образуется, когда у окрашенной детали есть default-спека, в которой
    ровно один компонент типа «Сборка», и имя этого компонента содержит
    «после сварки»/«без покраски».
    """
    painted_items = (
        db.query(Item.item_id)
        .filter(func.lower(Item.item_name).like(f"%{PAINTED_MARKER}%"))
        .all()
    )
    result: Dict[int, int] = {}
    for (painted_id,) in painted_items:
        painted_id = int(painted_id)
        spec_id = _default_spec_id(db, painted_id)
        if spec_id is None:
            continue
        assembly_rows = (
            db.query(SpecComponent.item_id, Item.item_name)
            .join(Item, Item.item_id == SpecComponent.item_id)
            .filter(SpecComponent.spec_id == spec_id)
            .filter(SpecComponent.component_type == ASSEMBLY_COMPONENT_TYPE)
            .all()
        )
        # Ровно один компонент-«Сборка».
        if len(assembly_rows) != 1:
            continue
        welded_id, welded_name = assembly_rows[0]
        if not _name_has(welded_name, WELDED_MARKERS):
            continue
        result[painted_id] = int(welded_id)
    return result


def _orphan_welded(db: Session, paired_welded: Set[int]) -> Dict[str, Any]:
    """
    Сварные «после сварки» (Производство) БЕЗ окрашенного родителя (не входят
    активной парой). Для таких серость снимать нельзя — иначе их не заказать.
    """
    rows = (
        db.query(Item.item_id, Item.item_code, Item.item_name, Item.replenishment_method)
        .filter(func.lower(Item.item_name).like(f"%{ORPHAN_MARKER}%"))
        .all()
    )
    orphans: List[Dict[str, Any]] = []
    for item_id, item_code, item_name, method in rows:
        if classify_replenishment_flow(method) != REPLENISHMENT_FLOW_PRODUCTION:
            continue
        if int(item_id) in paired_welded:
            continue
        orphans.append(
            {
                "item_id": int(item_id),
                "item_code": str(item_code or ""),
                "item_name": str(item_name or ""),
            }
        )
    orphans.sort(key=lambda row: row["item_id"])
    return {"count": len(orphans), "examples": orphans[:20]}


def rebuild_auto_pairs(db: Session) -> Dict[str, Any]:
    """
    Пересобрать auto-пары из спек: upsert обнаруженных, деактивация исчезнувших.
    Ручные (source='manual') пары не трогаются. Возвращает сводку + сироты.
    """
    detected = _detect_auto_pairs(db)

    existing = {
        int(pair.painted_item_id): pair
        for pair in db.query(PaintWeldPair).filter(PaintWeldPair.source == "auto").all()
    }
    # painted_item_id, закреплённые за ручной парой, auto не переопределяет.
    manual_painted = {
        int(pid)
        for (pid,) in db.query(PaintWeldPair.painted_item_id)
        .filter(PaintWeldPair.source == "manual")
        .all()
    }

    created = 0
    updated = 0
    reactivated = 0
    deactivated = 0

    for painted_id, welded_id in detected.items():
        if painted_id in manual_painted:
            continue
        pair = existing.get(painted_id)
        if pair is None:
            db.add(
                PaintWeldPair(
                    painted_item_id=painted_id,
                    welded_item_id=welded_id,
                    source="auto",
                    is_active=True,
                )
            )
            created += 1
            continue
        was_inactive = not bool(pair.is_active)
        welded_changed = int(pair.welded_item_id) != welded_id
        if welded_changed:
            pair.welded_item_id = welded_id
        if was_inactive:
            pair.is_active = True
            reactivated += 1
        elif welded_changed:
            updated += 1

    # Деактивация auto-пар, которых больше нет в спеках.
    for painted_id, pair in existing.items():
        if painted_id in detected:
            continue
        if bool(pair.is_active):
            pair.is_active = False
            deactivated += 1

    db.commit()

    active_pairs = (
        db.query(PaintWeldPair).filter(PaintWeldPair.is_active.is_(True)).all()
    )
    paired_welded = {int(p.welded_item_id) for p in active_pairs}
    orphans = _orphan_welded(db, paired_welded)

    return {
        "status": "ok",
        "detected": len(detected),
        "created": created,
        "updated": updated,
        "reactivated": reactivated,
        "deactivated": deactivated,
        "active_pairs": len(active_pairs),
        "orphans": orphans,
    }


def list_orphans(db: Session) -> Dict[str, Any]:
    """
    Сироты: сварные «после сварки» (Производство) без активной пары (нет
    окрашенного родителя). Для UI/замера — счёт и примеры.
    """
    paired_welded = {
        int(r[0])
        for r in db.query(PaintWeldPair.welded_item_id)
        .filter(PaintWeldPair.is_active.is_(True))
        .distinct()
        .all()
    }
    return _orphan_welded(db, paired_welded)


def list_pairs(db: Session, *, active_only: bool = True) -> List[Dict[str, Any]]:
    """Список пар с именами позиций (для UI справочника)."""
    query = db.query(PaintWeldPair)
    if active_only:
        query = query.filter(PaintWeldPair.is_active.is_(True))
    pairs = query.order_by(PaintWeldPair.painted_item_id.asc()).all()

    item_ids: Set[int] = set()
    for pair in pairs:
        item_ids.add(int(pair.painted_item_id))
        item_ids.add(int(pair.welded_item_id))
    names: Dict[int, Dict[str, str]] = {}
    if item_ids:
        for iid, code, name in (
            db.query(Item.item_id, Item.item_code, Item.item_name)
            .filter(Item.item_id.in_(item_ids))
            .all()
        ):
            names[int(iid)] = {"item_code": str(code or ""), "item_name": str(name or "")}

    result: List[Dict[str, Any]] = []
    for pair in pairs:
        painted = names.get(int(pair.painted_item_id), {})
        welded = names.get(int(pair.welded_item_id), {})
        result.append(
            {
                "id": int(pair.id),
                "painted_item_id": int(pair.painted_item_id),
                "painted_item_code": painted.get("item_code", ""),
                "painted_item_name": painted.get("item_name", ""),
                "welded_item_id": int(pair.welded_item_id),
                "welded_item_code": welded.get("item_code", ""),
                "welded_item_name": welded.get("item_name", ""),
                "source": str(pair.source),
                "is_active": bool(pair.is_active),
            }
        )
    return result


def is_welded_blocked(db: Session, item_ids: Iterable[int]) -> Set[int]:
    """
    Вернуть подмножество item_ids, которые являются сварной деталью активной
    пары (серые/недоступные к самостоятельному заказу).
    """
    ids = [int(x) for x in item_ids if x is not None]
    if not ids:
        return set()
    rows = (
        db.query(PaintWeldPair.welded_item_id)
        .filter(PaintWeldPair.is_active.is_(True))
        .filter(PaintWeldPair.welded_item_id.in_(ids))
        .distinct()
        .all()
    )
    return {int(r[0]) for r in rows}


def upsert_manual_pair(
    db: Session, *, painted_item_id: int, welded_item_id: int
) -> Dict[str, Any]:
    """Создать/обновить ручную пару (перекрывает auto для той же окрашенной)."""
    painted_item_id = int(painted_item_id)
    welded_item_id = int(welded_item_id)
    if not db.query(Item.item_id).filter(Item.item_id == painted_item_id).first():
        raise ValueError(f"painted_item_id={painted_item_id}: номенклатура не найдена")
    if not db.query(Item.item_id).filter(Item.item_id == welded_item_id).first():
        raise ValueError(f"welded_item_id={welded_item_id}: номенклатура не найдена")

    pair = (
        db.query(PaintWeldPair)
        .filter(PaintWeldPair.painted_item_id == painted_item_id)
        .first()
    )
    if pair is None:
        pair = PaintWeldPair(
            painted_item_id=painted_item_id,
            welded_item_id=welded_item_id,
            source="manual",
            is_active=True,
        )
        db.add(pair)
    else:
        pair.welded_item_id = welded_item_id
        pair.source = "manual"
        pair.is_active = True
    db.commit()
    db.refresh(pair)
    return {
        "id": int(pair.id),
        "painted_item_id": int(pair.painted_item_id),
        "welded_item_id": int(pair.welded_item_id),
        "source": str(pair.source),
        "is_active": bool(pair.is_active),
    }


def deactivate_pair(db: Session, pair_id: int) -> Dict[str, Any]:
    """Деактивировать (исключить) пару по id."""
    pair = db.query(PaintWeldPair).filter(PaintWeldPair.id == int(pair_id)).first()
    if pair is None:
        raise ValueError(f"pair_id={pair_id}: пара не найдена")
    pair.is_active = False
    db.commit()
    return {"id": int(pair.id), "is_active": False}


def _effective_welded_stock(db: Session, welded_item_id: int) -> float:
    """Эффективный остаток сварной: выбранные − игнорируемые склады − резервы."""
    from .production_control_material_availability import _stock_by_item
    from .production_control_reservations import open_reservations_by_item

    stock = _to_float(_stock_by_item(db, [welded_item_id]).get(welded_item_id, 0.0))
    reserved = _to_float(open_reservations_by_item(db, [welded_item_id]).get(welded_item_id, 0.0))
    return max(stock - reserved, 0.0)


def _open_weld_orders(db: Session, welded_item_id: int) -> List[Dict[str, Any]]:
    """Открытые сварочные заказы: строки production_products с remaining_qty>0."""
    rows = (
        db.query(
            ProductionOrder.order_number,
            ProductionProduct.remaining_qty,
        )
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .filter(ProductionProduct.item_id == int(welded_item_id))
        .filter(ProductionOrder.deletion_mark.is_(False))
        .filter(func.coalesce(ProductionProduct.remaining_qty, 0.0) > 0)
        .all()
    )
    orders: List[Dict[str, Any]] = []
    for number, remaining in rows:
        orders.append(
            {"number": str(number or ""), "remaining": _to_float(remaining)}
        )
    return orders


def guard_paint_order(
    db: Session, painted_item_id: int, qty: float
) -> Dict[str, Any]:
    """
    Гард открытия окрасочного заказа. Проверяет эффективный остаток сварной
    детали и открытые сварочные заказы.

    verdict:
      - 'stock_covers' — сварная есть на остатке (заказ на сварку не нужен);
      - 'order_open'   — сварка уже заказана (есть открытый сварочный заказ);
      - 'need_weld'    — нужно открывать сварочный заказ;
      - 'no_pair'      — у окрашенной нет активной пары (нет сварного звена).
    """
    painted_item_id = int(painted_item_id)
    qty = _to_float(qty)
    pair = (
        db.query(PaintWeldPair)
        .filter(PaintWeldPair.painted_item_id == painted_item_id)
        .filter(PaintWeldPair.is_active.is_(True))
        .first()
    )
    if pair is None:
        return {
            "painted_item_id": painted_item_id,
            "welded_item": None,
            "stock_qty": 0.0,
            "open_orders": [],
            "verdict": "no_pair",
        }

    welded_item_id = int(pair.welded_item_id)
    welded = db.query(Item).filter(Item.item_id == welded_item_id).first()
    stock_qty = _effective_welded_stock(db, welded_item_id)
    open_orders = _open_weld_orders(db, welded_item_id)

    if qty > 0 and stock_qty + 1e-9 >= qty:
        verdict = "stock_covers"
    elif open_orders:
        verdict = "order_open"
    else:
        verdict = "need_weld"

    return {
        "painted_item_id": painted_item_id,
        "welded_item": {
            "item_id": welded_item_id,
            "item_code": str(welded.item_code) if welded else "",
            "item_name": str(welded.item_name) if welded else "",
        },
        "stock_qty": round(stock_qty, 3),
        "open_orders": open_orders,
        "verdict": verdict,
    }

"""Справочник пар «окрашенная ↔ предшественник» (окраска ↔ сварка/др.), этап 1.

Окрашенная деталь определяется ПО ВИДУ ПРОИЗВОДСТВА: у её default-спеки
production_kind — красящий (имя содержит «покрас»/«окрас»/«маляр»). В такой
спеке ровно один компонент типа «Сборка» — это деталь-предшественник (после
любой обработки: сварка, гибка, токарка, сборка). Дополнительные НЕ-«Сборка»
компоненты (расходники: резинки, трафареты) пару не ломают. Имя предшественника
НЕ фильтруется.

Пары строятся автоматически (source='auto'), ручные правки допустимы
(source='manual'). В welded-блокировку (серость) попадают только предшественники
с методом снабжения «Производство».

См. .docs/paint_weld_chain_logic.md — утверждённое ТЗ. Этап 1 без записи в 1С:
только справочник, серость журнала/MRP, гард открытия и фильтр reconcile.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    DefaultSpecification,
    Item,
    PaintWeldPair,
    ProductionKind,
    ProductionOrder,
    ProductionProduct,
    SpecComponent,
    Specification,
)
from .replenishment import REPLENISHMENT_FLOW_PRODUCTION, classify_replenishment_flow

# Красящий вид производства: имя production_kind содержит любой из маркеров.
# «окрас» — подстрока «покраска», поэтому ловит и «Узел (покраска)».
PAINT_KIND_MARKERS = ("покрас", "окрас", "маляр")

ASSEMBLY_COMPONENT_TYPE = "Сборка"

# Причины, по которым красящаяся позиция не даёт пары (для отчёта rebuild).
UNPAIRED_NO_ASSEMBLY = "no_assembly_component"
UNPAIRED_MULTIPLE_ASSEMBLY = "multiple_assembly_components"


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _name_has(name: Optional[str], markers: Iterable[str]) -> bool:
    lowered = str(name or "").lower()
    return any(marker in lowered for marker in markers)


def _paint_kind_ids(db: Session) -> Set[int]:
    """id красящих видов производства (имя содержит покрас/окрас/маляр)."""
    return {
        int(kind_id)
        for kind_id, name in db.query(ProductionKind.id, ProductionKind.name).all()
        if _name_has(name, PAINT_KIND_MARKERS)
    }


def _painted_specs(db: Session) -> Dict[int, int]:
    """
    {painted_item_id: spec_id} — позиции, чья DEFAULT-спека (минимальный
    default_specifications.id для позиции) имеет красящий production_kind.
    """
    paint_kind_ids = _paint_kind_ids(db)
    if not paint_kind_ids:
        return {}
    rows = (
        db.query(
            DefaultSpecification.item_id,
            DefaultSpecification.spec_id,
            Specification.production_kind_id,
        )
        .join(Specification, Specification.spec_id == DefaultSpecification.spec_id)
        .order_by(DefaultSpecification.item_id.asc(), DefaultSpecification.id.asc())
        .all()
    )
    result: Dict[int, int] = {}
    seen: Set[int] = set()
    for item_id, spec_id, kind_id in rows:
        iid = int(item_id)
        if iid in seen:
            continue  # только default-спека (первая по id)
        seen.add(iid)
        if kind_id is not None and int(kind_id) in paint_kind_ids:
            result[iid] = int(spec_id)
    return result


def _detect_auto_pairs(db: Session) -> Tuple[Dict[int, int], Dict[int, str]]:
    """
    Разобрать красящиеся позиции на пары и «сирот» (unpaired).

    Возвращает (pairs, unpaired):
      - pairs: {painted_item_id: predecessor_item_id} — спека с ровно одним
        компонентом-«Сборка» (предшественник после любой обработки);
      - unpaired: {painted_item_id: reason} — спека с 0 или >1 «Сборка».
    """
    painted_specs = _painted_specs(db)
    pairs: Dict[int, int] = {}
    unpaired: Dict[int, str] = {}
    if not painted_specs:
        return pairs, unpaired

    spec_ids = set(painted_specs.values())
    assembly_by_spec: Dict[int, List[int]] = defaultdict(list)
    for spec_id, comp_item_id in (
        db.query(SpecComponent.spec_id, SpecComponent.item_id)
        .filter(SpecComponent.spec_id.in_(spec_ids))
        .filter(SpecComponent.component_type == ASSEMBLY_COMPONENT_TYPE)
        .all()
    ):
        assembly_by_spec[int(spec_id)].append(int(comp_item_id))

    for painted_id, spec_id in painted_specs.items():
        assemblies = assembly_by_spec.get(spec_id, [])
        if len(assemblies) == 1:
            pairs[painted_id] = assemblies[0]
        elif len(assemblies) == 0:
            unpaired[painted_id] = UNPAIRED_NO_ASSEMBLY
        else:
            unpaired[painted_id] = UNPAIRED_MULTIPLE_ASSEMBLY
    return pairs, unpaired


def _unpaired_report(db: Session, unpaired: Dict[int, str]) -> Dict[str, Any]:
    """
    Сироты: красящиеся по виду производства позиции БЕЗ пары (0 или несколько
    компонентов-«Сборка»). Их серость снимать нельзя. Позиции, закрытые ручной
    активной парой, из отчёта исключаются.
    """
    if not unpaired:
        return {"count": 0, "by_reason": {}, "examples": []}

    manually_paired = {
        int(pid)
        for (pid,) in db.query(PaintWeldPair.painted_item_id)
        .filter(PaintWeldPair.is_active.is_(True))
        .filter(PaintWeldPair.painted_item_id.in_(list(unpaired.keys())))
        .distinct()
        .all()
    }
    remaining = {pid: reason for pid, reason in unpaired.items() if pid not in manually_paired}

    names: Dict[int, Tuple[str, str]] = {}
    if remaining:
        for iid, code, name in (
            db.query(Item.item_id, Item.item_code, Item.item_name)
            .filter(Item.item_id.in_(list(remaining.keys())))
            .all()
        ):
            names[int(iid)] = (str(code or ""), str(name or ""))

    by_reason: Dict[str, int] = defaultdict(int)
    examples: List[Dict[str, Any]] = []
    for pid in sorted(remaining):
        reason = remaining[pid]
        by_reason[reason] += 1
        code, name = names.get(pid, ("", ""))
        examples.append(
            {
                "item_id": pid,
                "item_code": code,
                "item_name": name,
                "reason": reason,
            }
        )
    return {
        "count": len(remaining),
        "by_reason": dict(by_reason),
        "examples": examples[:20],
    }


def rebuild_auto_pairs(db: Session) -> Dict[str, Any]:
    """
    Пересобрать auto-пары из спек: upsert обнаруженных, деактивация исчезнувших.
    Ручные (source='manual') пары не трогаются. Возвращает сводку + сироты
    (красящиеся позиции без пары, с разбивкой по причинам).
    """
    detected, unpaired = _detect_auto_pairs(db)

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

    active_pairs = db.query(PaintWeldPair).filter(PaintWeldPair.is_active.is_(True)).all()
    orphans = _unpaired_report(db, unpaired)

    return {
        "status": "ok",
        "detected": len(detected),
        "created": created,
        "updated": updated,
        "reactivated": reactivated,
        "deactivated": deactivated,
        "active_pairs": len(active_pairs),
        "unpaired": orphans,
        "orphans": orphans,
    }


def list_orphans(db: Session) -> Dict[str, Any]:
    """
    Сироты: красящиеся по виду производства позиции БЕЗ пары (спека с 0 или
    несколькими компонентами-«Сборка»). Для UI/замера — счёт, разбивка по
    причинам и примеры.
    """
    _detected, unpaired = _detect_auto_pairs(db)
    return _unpaired_report(db, unpaired)


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
    Вернуть подмножество item_ids, которые являются предшественником активной
    пары И снабжаются «Производством» (серые/недоступные к самостоятельному
    заказу). Предшественники-закупки/переработки НЕ блокируются — их надо
    заказывать своим потоком.
    """
    ids = [int(x) for x in item_ids if x is not None]
    if not ids:
        return set()
    rows = (
        db.query(PaintWeldPair.welded_item_id, Item.replenishment_method)
        .join(Item, Item.item_id == PaintWeldPair.welded_item_id)
        .filter(PaintWeldPair.is_active.is_(True))
        .filter(PaintWeldPair.welded_item_id.in_(ids))
        .distinct()
        .all()
    )
    return {
        int(welded_id)
        for welded_id, method in rows
        if classify_replenishment_flow(method) == REPLENISHMENT_FLOW_PRODUCTION
    }


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

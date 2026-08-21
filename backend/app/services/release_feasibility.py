"""Проверка выпуска: что мешает выпустить изделие в заданном количестве.

Разворачивает BOM изделия по основным спецификациям (тот же источник состава,
что и MRP: `default_specifications` → `spec_components`), гасит потребность
свободным остатком складов и возвращает позиции, которые мешают выпуску:

* ``make``     — самого узла на складе нет, но всех его компонентов хватает
                 (жёлтый: узел надо просто изготовить);
* ``shortage`` — материал/покупное, которое нечем закрыть (красный: жёсткая нехватка);
* ``blocked``  — узел изготовить нельзя, потому что чего-то не хватает внутри
                 (красный: узел заблокирован дефицитом ниже по дереву).

Расчёт чисто аналитический: ничего не пишет в БД и не учитывает открытые заказы,
резервы и поставки в пути — только текущий свободный остаток. Это ответ на вопрос
«что мешает прямо сейчас», а не MRP-прогон.

Особенности разворота:

* корень не гасится собственным остатком — задание на выпуск это обязательство,
  а не потребность, которую можно закрыть складом;
* остаток гасится глобально по позиции: один и тот же компонент под разными
  узлами конкурирует за один и тот же складской остаток. Позиции обрабатываются
  в порядке low-level code, поэтому к моменту гашения потребность позиции собрана
  со всех веток;
* повторяющиеся строки состава (одна номенклатура несколько раз в одной
  спецификации) складываются в одну — считаем количество, а не структуру строк;
* циклы в составе обрезаются и попадают в ``summary.warnings``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..models import (
    DefaultSpecification,
    IgnoredWarehouse,
    Item,
    SpecComponent,
    StockBin,
    StockWarehouse,
    Unit,
)
from .mrp_stock_helpers import effective_stock_by_item_all
from .planning_truth import require_accepted

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 20
MAX_TREE_NODES = 20000
EPS = 1e-9

STATUS_OK = "ok"
STATUS_MAKE = "make"
STATUS_SHORTAGE = "shortage"
STATUS_BLOCKED = "blocked"

_STATUS_RANK = {
    STATUS_SHORTAGE: 0,
    STATUS_BLOCKED: 1,
    STATUS_MAKE: 2,
    STATUS_OK: 3,
}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _round_qty(value: float, places: int = 3) -> float:
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# Загрузка справочников
# --------------------------------------------------------------------------

def _units_map(db: Session) -> Dict[str, str]:
    """GUID единицы измерения → человекочитаемое обозначение."""
    mapping: Dict[str, str] = {}
    try:
        for unit in db.query(Unit).all():
            guid = str(unit.unit_ref1c or "").strip()
            if not guid:
                continue
            label = str(unit.short_name or unit.unit_name or unit.iso_code or unit.unit_code or "").strip()
            if label:
                mapping[guid.lower()] = label
    except Exception:
        return {}
    return mapping


def _unit_label(units: Dict[str, str], raw: Optional[str]) -> str:
    key = str(raw or "").strip().strip("{}").strip()
    if not key:
        return ""
    if key.lower().startswith("guid'") and key.endswith("'"):
        key = key[5:-1].strip()
    return units.get(key.lower(), "")


def _components_by_item(db: Session) -> Dict[int, Dict[int, float]]:
    """
    ``{item_id: {component_item_id: qty_per_parent}}`` по основным спецификациям.

    Грузится одним махом (как в `period_plan_service._explode_bom_net_first`),
    чтобы разворот дерева не превращался в N+1 запросов.
    """
    default_spec_map: Dict[int, int] = {}
    for row in db.query(DefaultSpecification.item_id, DefaultSpecification.spec_id).all():
        item_id, spec_id = row
        if item_id is None or spec_id is None:
            continue
        default_spec_map[int(item_id)] = int(spec_id)

    if not default_spec_map:
        return {}

    comps_by_spec: Dict[int, Dict[int, float]] = {}
    spec_ids = set(default_spec_map.values())
    rows = (
        db.query(SpecComponent.spec_id, SpecComponent.item_id, SpecComponent.quantity)
        .filter(SpecComponent.spec_id.in_(spec_ids))
        .all()
    )
    for spec_id, comp_item_id, quantity in rows:
        if spec_id is None or comp_item_id is None:
            continue
        qty_per = _to_float(quantity)
        if qty_per <= 0:
            continue
        bucket = comps_by_spec.setdefault(int(spec_id), {})
        bucket[int(comp_item_id)] = bucket.get(int(comp_item_id), 0.0) + qty_per

    result: Dict[int, Dict[int, float]] = {}
    for item_id, spec_id in default_spec_map.items():
        comps = comps_by_spec.get(int(spec_id))
        if comps:
            result[int(item_id)] = comps
    return result


def _item_meta(db: Session, item_ids: Sequence[int], units: Dict[str, str]) -> Dict[int, Dict[str, Any]]:
    ids = [int(x) for x in item_ids]
    if not ids:
        return {}
    meta: Dict[int, Dict[str, Any]] = {}
    for item in db.query(Item).filter(Item.item_id.in_(ids)).all():
        meta[int(item.item_id)] = {
            "item_id": int(item.item_id),
            "item_code": str(item.item_code or ""),
            "item_article": str(item.item_article or ""),
            "item_name": str(item.item_name or ""),
            "unit": _unit_label(units, item.unit),
            "replenishment_method": str(item.replenishment_method or ""),
        }
    return meta


def _warehouse_breakdown(db: Session, item_ids: Sequence[int]) -> Dict[int, List[Dict[str, Any]]]:
    """
    ``{item_id: [{warehouse_name, qty, counted}]}`` — где физически лежит остаток.

    ``counted=False`` у складов, которые исключены настройками (не выбран или
    в списке игнорируемых): их остаток виден пользователю, но в расчёт не идёт.
    """
    ids = [int(x) for x in item_ids]
    if not ids:
        return {}

    ignored_refs = {
        str(r[0]) for r in db.query(IgnoredWarehouse.warehouse_ref1c).all() if r and r[0]
    }
    warehouse_rows = db.query(
        StockWarehouse.warehouse_ref1c,
        StockWarehouse.warehouse_name,
        StockWarehouse.is_selected,
    ).all()
    names: Dict[str, str] = {}
    selected_refs: Set[str] = set()
    for ref, name, is_selected in warehouse_rows:
        if not ref:
            continue
        names[str(ref)] = str(name or "")
        if bool(is_selected):
            selected_refs.add(str(ref))
    has_warehouse_settings = bool(warehouse_rows)

    # Склад берётся из принятого поколения Item Ledger — того же источника, что
    # и суммарный остаток проверки (CANON: «Остаток принятого поколения»).
    # Legacy-таблица остатков как запасной источник запрещена: разошедшиеся
    # цифры в шапке и в разбивке по складам объяснить будет нечем.
    truth = require_accepted(db)
    result: Dict[int, List[Dict[str, Any]]] = {}
    rows = (
        db.query(
            StockBin.item_id,
            StockBin.warehouse_ref1c,
            func.sum(StockBin.on_hand),
        )
        .filter(
            StockBin.ledger_generation_id == int(truth.generation_id),
            StockBin.item_id.in_(ids),
        )
        .group_by(StockBin.item_id, StockBin.warehouse_ref1c)
        .all()
    )
    for item_id, ref, qty_sum in rows:
        qty_value = _to_float(qty_sum)
        if abs(qty_value) <= EPS:
            continue
        ref_str = str(ref or "")
        counted = ref_str not in ignored_refs and (not has_warehouse_settings or ref_str in selected_refs)
        result.setdefault(int(item_id), []).append(
            {
                "warehouse_name": names.get(ref_str) or ref_str,
                "qty": _round_qty(qty_value),
                "counted": bool(counted),
            }
        )
    for bucket in result.values():
        bucket.sort(key=lambda row: (not row["counted"], -_to_float(row["qty"])))
    return result


# --------------------------------------------------------------------------
# Разворот состава
# --------------------------------------------------------------------------

def _collect_subgraph(
    root_item_id: int,
    components: Dict[int, Dict[int, float]],
    max_depth: int,
) -> Tuple[Dict[int, Dict[int, float]], Set[int], List[str], bool]:
    """
    Обходит состав от корня и возвращает подграф, достижимый из него.

    Возвращает ``(edges, reachable, cycles, depth_truncated)``. Рёбра, замыкающие
    цикл, не записываются — ветка обрезается, а сам цикл попадает в ``cycles``.
    """
    edges: Dict[int, Dict[int, float]] = {}
    reachable: Set[int] = {int(root_item_id)}
    cycles: List[str] = []
    depth_truncated = False
    visited_at_depth: Dict[int, int] = {}

    def walk(item_id: int, depth: int, path: Set[int]) -> None:
        nonlocal depth_truncated
        if depth >= max_depth:
            if components.get(item_id):
                depth_truncated = True
            return
        # Один и тот же узел под разными родителями раскрываем один раз: состав
        # у него тот же самый, а глубже он даёт те же рёбра.
        seen_depth = visited_at_depth.get(item_id)
        if seen_depth is not None and seen_depth <= depth:
            return
        visited_at_depth[item_id] = depth

        comps = components.get(item_id)
        if not comps:
            return
        bucket = edges.setdefault(item_id, {})
        for comp_id, qty_per in comps.items():
            if comp_id in path:
                marker = f"{item_id}->{comp_id}"
                if marker not in cycles:
                    cycles.append(marker)
                continue
            bucket[comp_id] = qty_per
            reachable.add(comp_id)
            walk(comp_id, depth + 1, path | {comp_id})

    walk(int(root_item_id), 0, {int(root_item_id)})
    return edges, reachable, cycles, depth_truncated


def _low_level_codes(
    root_item_id: int,
    reachable: Set[int],
    edges: Dict[int, Dict[int, float]],
) -> Dict[int, int]:
    """
    Low-level code каждой позиции — максимальная глубина её вхождения.

    Позиции обрабатываются в этом порядке, чтобы потребность позиции была
    собрана со всех веток до того, как её начнут гасить остатком.
    """
    indegree: Dict[int, int] = {int(item_id): 0 for item_id in reachable}
    for parent, comps in edges.items():
        for comp_id in comps:
            indegree[comp_id] = indegree.get(comp_id, 0) + 1

    llc: Dict[int, int] = {int(item_id): 0 for item_id in reachable}
    queue: List[int] = sorted(item_id for item_id, degree in indegree.items() if degree == 0)
    order: List[int] = []
    while queue:
        current = queue.pop(0)
        order.append(current)
        for comp_id, _ in sorted(edges.get(current, {}).items()):
            if llc.get(comp_id, 0) < llc.get(current, 0) + 1:
                llc[comp_id] = llc[current] + 1
            indegree[comp_id] -= 1
            if indegree[comp_id] == 0:
                queue.append(comp_id)

    # Страховка: если из-за неучтённого цикла что-то осталось, задвигаем в конец.
    if len(order) < len(reachable):
        leftover = max(llc.values() or [0]) + 1
        for item_id in reachable:
            if item_id not in order:
                llc[item_id] = leftover
    llc[int(root_item_id)] = 0
    return llc


def _explode(
    root_item_id: int,
    root_qty: float,
    order: List[int],
    edges: Dict[int, Dict[int, float]],
    stock: Dict[int, float],
) -> Dict[int, Dict[str, float]]:
    """
    Гасит потребность остатками и возвращает ``{item_id: {gross, stock, allocated, net}}``.

    Корень не гасится собственным остатком: выпуск заданного количества — это
    задание, а не потребность, которую можно закрыть складом.
    """
    gross: Dict[int, float] = {int(root_item_id): float(root_qty)}
    result: Dict[int, Dict[str, float]] = {}
    for item_id in order:
        demand = gross.get(item_id, 0.0)
        on_hand = _to_float(stock.get(item_id, 0.0))
        if item_id == root_item_id:
            allocated = 0.0
        else:
            allocated = min(demand, on_hand) if on_hand > 0 else 0.0
        net = demand - allocated
        if net < EPS:
            net = 0.0
        result[item_id] = {
            "gross": demand,
            "stock": on_hand,
            "allocated": allocated,
            "net": net,
        }
        if net <= EPS:
            continue
        for comp_id, qty_per in edges.get(item_id, {}).items():
            gross[comp_id] = gross.get(comp_id, 0.0) + net * qty_per
    return result


def _classify(
    root_item_id: int,
    order: List[int],
    edges: Dict[int, Dict[int, float]],
    exploded: Dict[int, Dict[str, float]],
) -> Dict[int, str]:
    """
    Красит позиции: ok / make (жёлтый) / shortage (красный) / blocked (красный).

    Идём в обратном порядке low-level code, чтобы дефицит компонента успел
    подняться до всех узлов, которые из-за него нельзя изготовить.
    """
    status: Dict[int, str] = {}
    for item_id in reversed(order):
        values = exploded.get(item_id) or {}
        net = _to_float(values.get("net"))
        comps = edges.get(item_id) or {}
        if net <= EPS and item_id != root_item_id:
            status[item_id] = STATUS_OK
            continue
        if not comps:
            # Лист без состава — закрыть можно только закупкой/поставкой.
            status[item_id] = STATUS_SHORTAGE
            continue
        blocked = any(
            status.get(comp_id) in (STATUS_SHORTAGE, STATUS_BLOCKED)
            for comp_id in comps
        )
        status[item_id] = STATUS_BLOCKED if blocked else STATUS_MAKE
    return status


def _classify_structural(
    order: List[int],
    edges: Dict[int, Dict[int, float]],
    stock: Dict[int, float],
) -> Dict[int, str]:
    """Цвет позиции по её собственному состоянию, без оглядки на ветку.

    Ветка, чей родитель закрыт складом, не участвует в текущем выпуске, и её
    потребность обнуляется. Красить по ней нельзя: позиция, которой нет вовсе и
    делать которую не из чего, показывалась зелёным «хватает» — оператор видел
    полный состав, в котором ничего не мешает, хотя мешало.

    Здесь вопрос другой и не зависит от количества: чем эту позицию вообще
    закрыть. Есть остаток — зелёная; нет, но все компоненты доступны — жёлтая
    (надо изготовить); нет и делать не из чего — красная.
    """
    status: Dict[int, str] = {}
    for item_id in reversed(order):
        comps = edges.get(item_id) or {}
        if _to_float(stock.get(item_id, 0.0)) > EPS:
            status[item_id] = STATUS_OK
            continue
        if not comps:
            status[item_id] = STATUS_SHORTAGE
            continue
        blocked = any(
            status.get(comp_id) in (STATUS_SHORTAGE, STATUS_BLOCKED)
            for comp_id in comps
        )
        status[item_id] = STATUS_BLOCKED if blocked else STATUS_MAKE
    return status


def _has_hard_shortage(
    root_item_id: int,
    order: List[int],
    edges: Dict[int, Dict[int, float]],
    exploded: Dict[int, Dict[str, float]],
) -> bool:
    for item_id in order:
        if item_id == root_item_id:
            continue
        values = exploded.get(item_id) or {}
        if _to_float(values.get("net")) <= EPS:
            continue
        if not edges.get(item_id):
            return True
    return False


def _max_producible_qty(
    root_item_id: int,
    requested_qty: float,
    order: List[int],
    edges: Dict[int, Dict[int, float]],
    stock: Dict[int, float],
) -> float:
    """
    Максимальное количество корня, которое сейчас нечем заблокировать.

    Потребность монотонна по количеству корня, поэтому ищем границу делением
    отрезка: разворот здесь чисто арифметический (все справочники уже в памяти).
    """
    def feasible(qty: float) -> bool:
        exploded = _explode(root_item_id, qty, order, edges, stock)
        return not _has_hard_shortage(root_item_id, order, edges, exploded)

    if feasible(requested_qty):
        return requested_qty
    low, high = 0.0, float(requested_qty)
    if not feasible(min(1e-6, high)):
        return 0.0
    for _ in range(40):
        mid = (low + high) / 2.0
        if feasible(mid):
            low = mid
        else:
            high = mid
    return low


# --------------------------------------------------------------------------
# Публичный API
# --------------------------------------------------------------------------

def find_items(db: Session, term: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Поиск изделия по артикулу, коду, названию или GUID (для строки ввода)."""
    text = str(term or "").strip()
    units = _units_map(db)
    query = db.query(Item)
    if text:
        like = f"%{text}%"
        query = query.filter(
            or_(
                Item.item_article.ilike(like),
                Item.item_code.ilike(like),
                Item.item_name.ilike(like),
                Item.item_ref1c.ilike(like),
            )
        )
    items = (
        query.order_by(Item.item_article.asc(), Item.item_code.asc())
        .limit(int(limit))
        .all()
    )
    if not items:
        return []

    spec_item_ids = {
        int(row[0])
        for row in db.query(DefaultSpecification.item_id)
        .filter(DefaultSpecification.item_id.in_([int(i.item_id) for i in items]))
        .all()
        if row and row[0] is not None
    }
    stock = effective_stock_by_item_all(db)
    rows: List[Dict[str, Any]] = []
    for item in items:
        item_id = int(item.item_id)
        rows.append(
            {
                "item_id": item_id,
                "item_code": str(item.item_code or ""),
                "item_article": str(item.item_article or ""),
                "item_name": str(item.item_name or ""),
                "unit": _unit_label(units, item.unit),
                "stock_on_hand": _round_qty(stock.get(item_id, 0.0)),
                "has_spec": item_id in spec_item_ids,
            }
        )
    return rows


def resolve_item(
    db: Session,
    *,
    item_id: Optional[int] = None,
    article: Optional[str] = None,
) -> Tuple[Optional[Item], List[Item]]:
    """
    Ищет изделие по item_id или артикулу.

    Возвращает ``(item, candidates)``: если по артикулу нашлось несколько
    позиций, ``item`` пустой, а вызывающий показывает список кандидатов.
    """
    if item_id is not None:
        item = db.query(Item).filter(Item.item_id == int(item_id)).first()
        return (item, [item] if item else [])

    text = str(article or "").strip()
    if not text:
        return (None, [])

    exact = (
        db.query(Item)
        .filter(func.lower(Item.item_article) == text.lower())
        .order_by(Item.item_id.asc())
        .all()
    )
    if not exact:
        exact = (
            db.query(Item)
            .filter(func.lower(Item.item_code) == text.lower())
            .order_by(Item.item_id.asc())
            .all()
        )
    if len(exact) == 1:
        return (exact[0], exact)
    if exact:
        return (None, exact)

    like = f"%{text}%"
    partial = (
        db.query(Item)
        .filter(or_(Item.item_article.ilike(like), Item.item_code.ilike(like), Item.item_name.ilike(like)))
        .order_by(Item.item_article.asc(), Item.item_code.asc())
        .limit(50)
        .all()
    )
    if len(partial) == 1:
        return (partial[0], partial)
    return (None, partial)


def analyze_release(
    db: Session,
    root_item: Item,
    qty: float,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    include_tree: bool = False,
) -> Dict[str, Any]:
    """
    Считает, что мешает выпустить ``root_item`` в количестве ``qty``.

    ``include_tree=False`` (по умолчанию) возвращает только проблемные позиции —
    полный BOM собирается лишь по явному запросу, потому что на реальных изделиях
    он тяжёлый.
    """
    root_item_id = int(root_item.item_id)
    root_qty = max(0.0, _to_float(qty))
    depth_limit = max(1, int(max_depth or DEFAULT_MAX_DEPTH))

    units = _units_map(db)
    components = _components_by_item(db)
    edges, reachable, cycles, depth_truncated = _collect_subgraph(root_item_id, components, depth_limit)
    llc = _low_level_codes(root_item_id, reachable, edges)
    order = sorted(reachable, key=lambda iid: (llc.get(iid, 0), iid))

    stock_all = effective_stock_by_item_all(db)
    stock = {int(iid): _to_float(stock_all.get(int(iid), 0.0)) for iid in reachable}

    exploded = _explode(root_item_id, root_qty, order, edges, stock)
    status_map = _classify(root_item_id, order, edges, exploded)
    meta = _item_meta(db, list(reachable), units)

    warnings: List[str] = []
    if cycles:
        warnings.append("CYCLE_DETECTED")
    if depth_truncated:
        warnings.append("DEPTH_LIMIT_REACHED")

    root_meta = meta.get(root_item_id) or {
        "item_id": root_item_id,
        "item_code": str(root_item.item_code or ""),
        "item_article": str(root_item.item_article or ""),
        "item_name": str(root_item.item_name or ""),
        "unit": _unit_label(units, root_item.unit),
        "replenishment_method": str(root_item.replenishment_method or ""),
    }
    root_has_spec = bool(edges.get(root_item_id))
    if not root_has_spec:
        warnings.append("ROOT_NO_SPEC")

    # Родители по каждой позиции — «где применяется» без отдельного запроса.
    parents_by_item: Dict[int, List[int]] = {}
    for parent, comps in edges.items():
        for comp_id in comps:
            parents_by_item.setdefault(comp_id, []).append(parent)

    blocking: List[Dict[str, Any]] = []
    for item_id in order:
        if item_id == root_item_id:
            continue
        values = exploded.get(item_id) or {}
        net = _to_float(values.get("net"))
        if net <= EPS:
            continue
        status = status_map.get(item_id, STATUS_OK)
        info = meta.get(item_id, {})
        blocking.append(
            {
                "item_id": item_id,
                "item_code": info.get("item_code", ""),
                "item_article": info.get("item_article", ""),
                "item_name": info.get("item_name", ""),
                "unit": info.get("unit", ""),
                "replenishment_method": info.get("replenishment_method", ""),
                "level": int(llc.get(item_id, 0)),
                "kind": "node" if edges.get(item_id) else "material",
                "status": status,
                "is_blocking": status in (STATUS_SHORTAGE, STATUS_BLOCKED),
                "required_qty": _round_qty(values.get("gross", 0.0)),
                "stock_on_hand": _round_qty(values.get("stock", 0.0)),
                "allocated_qty": _round_qty(values.get("allocated", 0.0)),
                "shortage_qty": _round_qty(net),
                "used_in": [
                    {
                        "item_id": parent_id,
                        "item_article": (meta.get(parent_id) or {}).get("item_article", ""),
                        "item_name": (meta.get(parent_id) or {}).get("item_name", ""),
                    }
                    for parent_id in sorted(parents_by_item.get(item_id, []))[:8]
                ],
                "warehouses": [],
            }
        )

    breakdown = _warehouse_breakdown(db, [row["item_id"] for row in blocking])
    for row in blocking:
        row["warehouses"] = breakdown.get(int(row["item_id"]), [])

    blocking.sort(
        key=lambda row: (
            _STATUS_RANK.get(row["status"], 9),
            int(row["level"]),
            str(row["item_article"] or ""),
            str(row["item_name"] or ""),
        )
    )

    shortage_rows = [row for row in blocking if row["status"] == STATUS_SHORTAGE]
    blocked_rows = [row for row in blocking if row["status"] == STATUS_BLOCKED]
    make_rows = [row for row in blocking if row["status"] == STATUS_MAKE]

    if shortage_rows:
        overall = STATUS_BLOCKED
    elif make_rows:
        overall = STATUS_MAKE
    else:
        overall = STATUS_OK

    producible = _max_producible_qty(root_item_id, root_qty, order, edges, stock) if root_qty > 0 else 0.0

    payload: Dict[str, Any] = {
        "root": {
            **root_meta,
            "requested_qty": _round_qty(root_qty),
            "stock_on_hand": _round_qty(stock.get(root_item_id, 0.0)),
            "has_spec": root_has_spec,
        },
        "summary": {
            "status": overall,
            "shortage_count": len(shortage_rows),
            "blocked_count": len(blocked_rows),
            "make_count": len(make_rows),
            "items_checked": len(reachable),
            "max_level": max(llc.values()) if llc else 0,
            "producible_qty": _round_qty(producible),
            "fully_producible": producible >= root_qty - 1e-6,
            "warnings": warnings,
            "cycles": cycles[:20],
            "max_depth": depth_limit,
        },
        "blocking": blocking,
        "tree": None,
        "tree_truncated": False,
    }

    if include_tree:
        tree, truncated = _build_tree(
            root_item_id=root_item_id,
            root_qty=root_qty,
            edges=edges,
            exploded=exploded,
            status_map=status_map,
            structural_status=_classify_structural(order, edges, stock),
            meta=meta,
            llc=llc,
            depth_limit=depth_limit,
        )
        payload["tree"] = tree
        payload["tree_truncated"] = truncated

    return payload


def _build_tree(
    *,
    root_item_id: int,
    root_qty: float,
    edges: Dict[int, Dict[int, float]],
    exploded: Dict[int, Dict[str, float]],
    status_map: Dict[int, str],
    structural_status: Dict[int, str],
    meta: Dict[int, Dict[str, Any]],
    llc: Dict[int, int],
    depth_limit: int,
) -> Tuple[Dict[str, Any], bool]:
    """
    Полный BOM с количествами по ветке.

    По ветке спускается ровно та часть потребности, которую не закрыл склад:
    доля ``net/gross`` позиции одинакова для всех её вхождений, поэтому сумма
    веток сходится с глобальным расчётом.
    """
    budget = {"nodes": 0}
    truncated = {"value": False}

    def branch_net(item_id: int, branch_required: float) -> float:
        values = exploded.get(item_id) or {}
        gross = _to_float(values.get("gross"))
        net = _to_float(values.get("net"))
        if gross <= EPS:
            return 0.0
        return branch_required * (net / gross)

    def node(item_id: int, qty_per_parent: Optional[float], branch_required: float, depth: int, path: Set[int]) -> Dict[str, Any]:
        budget["nodes"] += 1
        values = exploded.get(item_id) or {}
        info = meta.get(item_id, {})
        own_net = branch_net(item_id, branch_required)
        comps = edges.get(item_id) or {}
        branch_stock = _to_float(values.get("stock", 0.0))
        # Нужна ветка — красим по расчёту выпуска; не нужна — по самой позиции.
        row_status = (
            status_map.get(item_id, STATUS_OK)
            if branch_required > EPS
            else structural_status.get(item_id, STATUS_OK)
        )
        # Остатка не хватает на потребность ветки: строка может быть жёлтой
        # («собрать можно»), но цифру остатка надо показать красной.
        stock_short = (
            branch_stock + EPS < branch_required
            if branch_required > EPS
            else branch_stock <= EPS
        )
        payload: Dict[str, Any] = {
            "key": f"{item_id}:{depth}:{budget['nodes']}",
            "item_id": item_id,
            "item_code": info.get("item_code", ""),
            "item_article": info.get("item_article", ""),
            "item_name": info.get("item_name", ""),
            "unit": info.get("unit", ""),
            "replenishment_method": info.get("replenishment_method", ""),
            "level": depth,
            "kind": "node" if comps else "material",
            "status": row_status,
            "stock_short": bool(stock_short),
            "qty_per_parent": None if qty_per_parent is None else _round_qty(qty_per_parent),
            "branch_required_qty": _round_qty(branch_required),
            "branch_shortage_qty": _round_qty(own_net),
            "required_qty": _round_qty(values.get("gross", 0.0)),
            "stock_on_hand": _round_qty(values.get("stock", 0.0)),
            "shortage_qty": _round_qty(values.get("net", 0.0)),
            "has_children": bool(comps),
            "children": [],
        }
        if not comps or depth >= depth_limit or item_id in path:
            if comps and (depth >= depth_limit or item_id in path):
                truncated["value"] = True
            return payload
        for comp_id, per in sorted(
            comps.items(),
            key=lambda kv: (
                str((meta.get(kv[0]) or {}).get("item_article", "")),
                str((meta.get(kv[0]) or {}).get("item_name", "")),
            ),
        ):
            if budget["nodes"] >= MAX_TREE_NODES:
                truncated["value"] = True
                break
            payload["children"].append(
                node(comp_id, per, own_net * per, depth + 1, path | {item_id})
            )
        return payload

    root = node(root_item_id, None, root_qty, 0, set())
    return root, truncated["value"]

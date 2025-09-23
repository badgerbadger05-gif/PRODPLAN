from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
import logging
logger = logging.getLogger("specification")
from decimal import Decimal, InvalidOperation

from ..database import get_db
from ..models import (
    Item,
    DefaultSpecification,
    SpecComponent,
    SpecOperation,
    Operation,
    ProductionStage,
    Specification,
    Unit,
)

router = APIRouter(prefix="/v1/specification", tags=["specification"])


# ------- helpers

def _to_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return float(default)
        if isinstance(val, (int, float)):
            return float(val)
        return float(Decimal(str(val)))
    except (InvalidOperation, ValueError, TypeError):
        return float(default)


def _round_qty(val: float, places: int = 3) -> float:
    try:
        q = Decimal(str(val)).quantize(Decimal("1." + "0" * places))
        return float(q)
    except Exception:
        return round(float(val or 0.0), places)


def _round_time(val: float, places: int = 2) -> float:
    try:
        q = Decimal(str(val)).quantize(Decimal("1." + "0" * places))
        return float(q)
    except Exception:
        return round(float(val or 0.0), places)


def _get_item_by_code_or_id(db: Session, item_code: Optional[str], item_id: Optional[int], item_ref1c: Optional[str] = None) -> Optional[Item]:
    """
    Универсальный поиск изделия:
      1) по GUID (item_ref1c, Ref_Key из 1С) — приоритетно, если передан
      2) по item_id
      3) по item_code
    """
    logger.info(f"[spec.tree] _get_item_by_code_or_id item_code={item_code} item_id={item_id} item_ref1c={item_ref1c}")
    # 1) Поиск по GUID из 1С
    if item_ref1c:
        ref = str(item_ref1c).strip()
        if ref:
            it = db.query(Item).filter(Item.item_ref1c == ref).first()
            logger.info(f"[spec.tree] _get_item_by_code_or_id by ref1c -> {it.item_id if it else None}")
            if it:
                return it
    # 2) Поиск по локальному ID
    if item_id is not None:
        it = db.query(Item).filter(Item.item_id == int(item_id)).first()
        logger.info(f"[spec.tree] _get_item_by_code_or_id by id -> {it.item_id if it else None}")
        if it:
            return it
    # 3) Поиск по локальному коду
    if item_code:
        it = db.query(Item).filter(Item.item_code == str(item_code).strip()).first()
        logger.info(f"[spec.tree] _get_item_by_code_or_id by code -> {it.item_id if it else None}")
        if it:
            return it
    logger.info("[spec.tree] _get_item_by_code_or_id no match")
    return None


def _get_default_spec_id(db: Session, item_id: int) -> Optional[int]:
    rec = (
        db.query(DefaultSpecification)
        .filter(DefaultSpecification.item_id == int(item_id))
        .first()
    )
    return int(rec.spec_id) if rec and rec.spec_id is not None else None


def _resolve_spec_id_for_item(db: Session, item: Item) -> Optional[int]:
    """
    Fallback-логика разрешения spec_id для изделия:
    1) default_specifications (основной путь)
    2) подстановка по совпадению spec_code==item_code или spec_name==item_name
    """
    logger.info(f"[spec.tree] resolve spec for item_id={item.item_id} code={item.item_code} name={item.item_name}")
    sid = _get_default_spec_id(db, int(item.item_id))
    if sid:
        logger.info(f"[spec.tree] found default_spec_id={sid}")
        return sid
    # Fallback: пытаемся подобрать спецификацию по коду/наименованию
    spec = (
        db.query(Specification)
        .filter(or_(Specification.spec_code == item.item_code, Specification.spec_name == item.item_name))
        .order_by(Specification.spec_id.desc())
        .first()
    )
    if spec and spec.spec_id is not None:
        logger.info(f"[spec.tree] fallback matched spec_id={spec.spec_id} by code/name")
        return int(spec.spec_id)
    logger.warning(f"[spec.tree] spec not found for item_id={item.item_id}")
    return None


def _resolve_spec_id_for_item_id(db: Session, item_id: int) -> Optional[int]:
    logger.info(f"[spec.tree] _resolve_spec_id_for_item_id item_id={item_id}")
    item = db.query(Item).filter(Item.item_id == int(item_id)).first()
    if not item:
        logger.warning(f"[spec.tree] item not found item_id={item_id}")
        return None
    return _resolve_spec_id_for_item(db, item)


def _has_children(db: Session, for_item_id: int) -> bool:
    spec_id = _resolve_spec_id_for_item_id(db, for_item_id)
    if not spec_id:
        logger.info(f"[spec.tree] _has_children no spec for item_id={for_item_id}")
        return False
    comps = db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).limit(1).count()
    ops = db.query(SpecOperation).filter(SpecOperation.spec_id == spec_id).limit(1).count()
    logger.info(f"[spec.tree] _has_children item_id={for_item_id} spec_id={spec_id} comps>0={comps>0} ops>0={ops>0}")
    if comps > 0:
        return True
    if ops > 0:
        return True
    return False


def _parse_node_id(node_id: str) -> Tuple[str, Dict[str, Any]]:
    """
    Supported formats:
      - item:{item_id}:{tree_qty}
      - op:{spec_operation_id}:{parent_item_id}:{parent_tree_qty}
    """
    if not node_id or not isinstance(node_id, str):
        return ("", {})
    parts = node_id.split(":")
    if len(parts) >= 1:
        kind = parts[0]
        if kind == "item" and len(parts) >= 3:
            try:
                return ("item", {
                    "item_id": int(parts[1]),
                    "tree_qty": _to_float(parts[2], 1.0),
                })
            except Exception:
                return ("", {})
        if kind == "op" and len(parts) >= 4:
            try:
                return ("op", {
                    "spec_operation_id": int(parts[1]),
                    "parent_item_id": int(parts[2]),
                    "parent_tree_qty": _to_float(parts[3], 1.0),
                })
            except Exception:
                return ("", {})
    return ("", {})


def _build_units_map(db: Session) -> Dict[str, str]:
    """
    Строит словарь соответствий GUID единицы измерения (unit_ref1c) → человекочитаемое обозначение.
    Приоритет полей для ярлыка: short_name → iso_code → unit_code → unit_name.
    """
    mapping: Dict[str, str] = {}
    try:
        rows = db.query(Unit).all()
        for u in rows:
            guid = str(u.unit_ref1c or "").strip()
            if not guid:
                continue
            # Предпочитаем максимально человекочитаемое обозначение:
            # short_name → unit_name → iso_code → unit_code
            label = (u.short_name or u.unit_name or u.iso_code or u.unit_code or "").strip()
            if label:
                mapping[guid] = label
    except Exception:
        mapping = {}
    return mapping


def _unit_label(units_map: Dict[str, str], unit_guid: Optional[str]) -> Optional[str]:
    """
    Возвращает человекочитаемое обозначение ЕИ по GUID. Если нет в словаре — None (GUID в UI не показываем).

    Нормализуем формат GUID'...' / {....} / верхний/нижний регистр:
      - убираем префикс guid' и одинарные кавычки
      - убираем фигурные скобки
      - пробуем исходное значение, lower(), нормализованное (lower без 'guid' и кавычек), а также upper-варианты
    """
    if not unit_guid:
        return None

    raw = str(unit_guid).strip()
    if not raw:
        return None

    # Исходные варианты
    candidates: list[str] = [raw]

    # Нижний регистр
    low = raw.lower()
    if low not in candidates:
        candidates.append(low)

    # Снятие обёрток GUID-формата
    def cleanup(s: str) -> str:
        t = s.strip().strip("{}").strip()
        # убрать префикс guid'...'
        if t.lower().startswith("guid'") and t.endswith("'"):
            t = t[5:-1].strip()
        # повторная зачистка скобок на всякий случай
        t = t.strip().strip("{}").strip()
        return t

    cl_raw = cleanup(raw)
    if cl_raw and cl_raw not in candidates:
        candidates.append(cl_raw)
    cl_low = cleanup(low)
    if cl_low and cl_low not in candidates:
        candidates.append(cl_low)
    # Верхний регистр для некоторых БД/источников
    up = raw.upper()
    if up not in candidates:
        candidates.append(up)
    cl_up = cleanup(up)
    if cl_up and cl_up not in candidates:
        candidates.append(cl_up)

    # Пытаемся найти метку по любому из вариантов ключа
    for key in candidates:
        if key in units_map:
            return units_map[key]

    return None
def _make_item_node(
    *,
    item: Item,
    parent_id: Optional[str],
    qty_per_parent: Optional[float],
    tree_qty: float,
    stage: Optional[ProductionStage],
    unit: Optional[str],
    has_children: bool,
    warnings: List[str],
) -> Dict[str, Any]:
    return {
        "id": f"item:{int(item.item_id)}:{_round_qty(tree_qty, 6)}",
        "parentId": parent_id,
        "type": "item",
        "name": str(item.item_name or ""),
        "article": str(item.item_article or "") if item.item_article else None,
        "stage": ({
            "id": str(stage.stage_id),
            "name": str(stage.stage_name or "")
        } if stage and stage.stage_id is not None else None),
        "operation": None,
        "qtyPerParent": _round_qty(qty_per_parent, 3) if qty_per_parent is not None else None,
        "unit": str(unit) if unit else None,
        "replenishmentMethod": str(item.replenishment_method or "").strip() if getattr(item, "replenishment_method", None) else None,
        "timeNormNh": None,
        "computed": {
            "treeQty": _round_qty(tree_qty, 3),
            "treeTimeNh": None
        },
        "hasChildren": bool(has_children),
        "warnings": warnings,
        "item": {
            "id": int(item.item_id),
            "code": str(item.item_code or ""),
        }
    }


def _make_operation_node(
    *,
    spec_operation_id: int,
    op: Operation,
    parent_id: Optional[str],
    parent_item: Item,
    parent_tree_qty: float,
    stage: Optional[ProductionStage],
    warnings: List[str],
) -> Dict[str, Any]:
    time_norm = _to_float(op.time_norm, 0.0)
    tree_time = time_norm * _to_float(parent_tree_qty, 1.0)
    return {
        "id": f"op:{int(spec_operation_id)}:{int(parent_item.item_id)}:{_round_qty(parent_tree_qty, 6)}",
        "parentId": parent_id,
        "type": "operation",
        "name": None,
        "article": None,
        "stage": ({
            "id": str(stage.stage_id),
            "name": str(stage.stage_name or "")
        } if stage and stage.stage_id is not None else None),
        "operation": {
            "id": str(op.operation_id) if op.operation_id is not None else None,
            "name": str(op.operation_name or None) if hasattr(op, "operation_name") else None
        },
        "qtyPerParent": None,
        "unit": None,
        "timeNormNh": _round_time(time_norm, 3),
        "computed": {
            "treeQty": None,
            "treeTimeNh": _round_time(tree_time, 3)
        },
        "hasChildren": False,
        "warnings": warnings,
        "item": {
            "id": int(parent_item.item_id),
            "code": str(parent_item.item_code or ""),
        }
    }


def _children_for_item(db: Session, item_id: int, parent_tree_qty: float, parent_node_id: str, units_map: Dict[str, str]) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    spec_id = _resolve_spec_id_for_item_id(db, item_id)
    logger.info(f"[spec.tree] _children_for_item item_id={item_id} parent_tree_qty={parent_tree_qty} parent_node_id={parent_node_id} -> spec_id={spec_id}")
    if not spec_id:
        return nodes

    # Components (child items)
    comps = (
        db.query(SpecComponent, Item, ProductionStage)
        .join(Item, SpecComponent.item_id == Item.item_id)
        .outerjoin(ProductionStage, SpecComponent.stage_id == ProductionStage.stage_id)
        .filter(SpecComponent.spec_id == spec_id)
        .all()
    )
    logger.info(f"[spec.tree] components count={len(comps)} for spec_id={spec_id}")

    for comp, child_item, stg in comps:
        qty_per_parent = _to_float(comp.quantity, 0.0)
        child_tree_qty = _to_float(parent_tree_qty, 1.0) * qty_per_parent
        warn: List[str] = []
        if comp.stage_id is None:
            warn.append("NO_STAGE")
        child_has_children = _has_children(db, int(child_item.item_id))
        nodes.append(
            _make_item_node(
                item=child_item,
                parent_id=parent_node_id,
                qty_per_parent=qty_per_parent,
                tree_qty=child_tree_qty,
                stage=stg,
                unit=_unit_label(units_map, child_item.unit),
                has_children=child_has_children,
                warnings=warn,
            )
        )

    # Operations under this item (operations of this item's spec)
    spec_ops = (
        db.query(SpecOperation, Operation, ProductionStage)
        .join(Operation, SpecOperation.operation_id == Operation.operation_id)
        .outerjoin(ProductionStage, SpecOperation.stage_id == ProductionStage.stage_id)
        .filter(SpecOperation.spec_id == spec_id)
        .all()
    )
    logger.info(f"[spec.tree] operations count={len(spec_ops)} for spec_id={spec_id}")

    for spec_op, op, stg in spec_ops:
        warn: List[str] = []
        time_norm = _to_float(spec_op.time_norm if getattr(spec_op, "time_norm", None) is not None else op.time_norm, 0.0)
        if stg is None or spec_op.stage_id is None:
            warn.append("NO_STAGE")
        if time_norm <= 0:
            warn.append("NO_TIME_NORM")

        nodes.append(
            _make_operation_node(
                spec_operation_id=int(spec_op.spec_operation_id),
                op=op,
                parent_id=parent_node_id,
                parent_item=db.query(Item).filter(Item.item_id == item_id).first(),
                parent_tree_qty=parent_tree_qty,
                stage=stg,
                warnings=warn,
            )
        )

    logger.info(f"[spec.tree] children total={len(nodes)} for item_id={item_id}")
    return nodes


# ------- endpoint

@router.get("/tree")
def get_specification_tree(
    item_code: Optional[str] = Query(None, description="Код изделия (альтернатива item_id/item_ref1c)"),
    item_id: Optional[int] = Query(None, description="ID изделия (альтернатива item_code/item_ref1c)"),
    item_ref1c: Optional[str] = Query(None, description="GUID изделия (Ref_Key из 1С, альтернатива item_code/item_id)"),
    root_qty: float = Query(1.0, description="Количество корневого изделия для расчёта"),
    parent_id: Optional[str] = Query(None, description="Идентификатор узла (для ленивой подгрузки детей)"),
    depth: int = Query(0, ge=0, le=2, description="Глубина разворота (0 - только корень, 1 - корень + дети)"),
    debug: bool = Query(False, description="Возвращать диагностическую информацию в meta.debug"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Возвращает узлы спецификации (дерево) для QTable в режиме tree.

    Режимы:
      - Корень (без parent_id): возвращает 1 узел типа 'item' по item_code|item_id.
      - Дети узла (с parent_id): возвращает список дочерних 'item' и 'operation'.

    Идентификаторы узлов:
      - item:{item_id}:{tree_qty}
      - op:{spec_operation_id}:{parent_item_id}:{parent_tree_qty}
    """
    try:
        logger.info(f"[spec.tree] request parent_id={parent_id} item_code={item_code} item_id={item_id} root_qty={root_qty} depth={depth}")
        units_map = _build_units_map(db)
        if parent_id:
            # Lazy-load children for given node
            kind, data = _parse_node_id(parent_id)
            logger.info(f"[spec.tree] parent mode kind={kind} parsed={data}")
            if kind != "item":
                return {"nodes": [], "meta": {"parentId": parent_id, "mode": "children"}}

            p_item_id = int(data.get("item_id"))
            p_tree_qty = _to_float(data.get("tree_qty"), 1.0)

            # Build children nodes
            nodes = _children_for_item(db, p_item_id, p_tree_qty, parent_id, units_map)
            logger.info(f"[spec.tree] children returned count={len(nodes)} for parent_id={parent_id}")
            return {
                "nodes": nodes,
                "meta": {
                    "parentId": parent_id,
                    "mode": "children",
                }
            }

        # Root node case
        if item_code is None and item_id is None and (item_ref1c is None or str(item_ref1c).strip() == ""):
            logger.error("[spec.tree] missing all of item_code, item_id, item_ref1c")
            raise HTTPException(status_code=400, detail="Either item_code, item_id or item_ref1c is required")

        item = _get_item_by_code_or_id(db, item_code=item_code, item_id=item_id, item_ref1c=item_ref1c)
        if not item:
            logger.error(f"[spec.tree] item not found item_code={item_code} item_id={item_id}")
            raise HTTPException(status_code=404, detail="Item not found")

        r_qty = _to_float(root_qty, 1.0)
        node_warnings: List[str] = []
        # root node doesn't have a stage; children will.
        node = _make_item_node(
            item=item,
            parent_id=None,
            qty_per_parent=None,
            tree_qty=r_qty,
            stage=None,
            unit=_unit_label(units_map, item.unit),
            has_children=_has_children(db, int(item.item_id)),
            warnings=node_warnings,
        )

        # Optional pre-expand first level (depth >= 1)
        if int(depth or 0) >= 1:
            try:
                logger.info(f"[spec.tree] pre-expand depth={depth} for item_id={item.item_id}")
                children_nodes = _children_for_item(db, int(item.item_id), r_qty, parent_node_id=str(node["id"]), units_map=units_map)
                if isinstance(children_nodes, list):
                    # QTable tree ожидает поле children у строки
                    node["children"] = children_nodes
                logger.info(f"[spec.tree] pre-expand children count={len(node.get('children', []))}")
            except Exception as ex:
                logger.exception(f"[spec.tree] pre-expand error: {ex}")
                # не валим ответ, просто без детей
                node["children"] = []

        meta: Dict[str, Any] = {
            "rootId": node["id"],
            "requested": {
                "item_code": item_code,
                "item_id": int(item.item_id),
                "root_qty": _round_qty(r_qty, 3),
                "depth": int(depth or 0),
            }
        }

        if debug:
            try:
                root_spec_id = _resolve_spec_id_for_item_id(db, int(item.item_id))
                comps_cnt = 0
                ops_cnt = 0
                if root_spec_id:
                    comps_cnt = db.query(SpecComponent).filter(SpecComponent.spec_id == root_spec_id).count()
                    ops_cnt = db.query(SpecOperation).filter(SpecOperation.spec_id == root_spec_id).count()
                meta["debug"] = {
                    "root_item_id": int(item.item_id),
                    "root_item_code": item.item_code,
                    "resolved_spec_id": root_spec_id,
                    "components_count": comps_cnt,
                    "operations_count": ops_cnt,
                    "preexpanded_children": len(node.get("children", []) or []),
                    "hasChildren_flag": node.get("hasChildren"),
                }
            except Exception as ex:
                meta["debug"] = {"error": f"root debug failed: {ex}"}

        resp = {
            "nodes": [node],
            "meta": meta
        }
        logger.info(f"[spec.tree] root response children={len(node.get('children', []))} hasChildren={node.get('hasChildren')}")
        return resp

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[spec.tree] error: {e}")
        raise HTTPException(status_code=500, detail=f"Specification tree error: {e}")
# --- DEBUG endpoint: quick diagnostics without reading server logs
@router.get("/debug")
def get_specification_debug(
    item_code: Optional[str] = Query(None, description="Код изделия (альтернатива item_id/item_ref1c)"),
    item_id: Optional[int] = Query(None, description="ID изделия (альтернатива item_code/item_ref1c)"),
    item_ref1c: Optional[str] = Query(None, description="GUID изделия (Ref_Key, альтернатива item_code/item_id)"),
    root_qty: float = Query(1.0, description="Количество корневого изделия для расчёта"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Диагностика разрешения спецификации и построения первого уровня детей.
    Удобно открыть в браузере:
      http://localhost:8000/api/v1/specification/debug?item_code=КОД&root_qty=1

    Возвращает:
      {
        "item": { id, code, name },
        "default_spec_id": int|null,
        "resolved_spec_id": int|null,
        "used_fallback": bool,
        "components_count": int,
        "operations_count": int,
        "children_count": int,
        "children_sample": [ { id, type, name, operationName, stageName } ... up to 10 ]
      }
    """
    try:
        logger.info(f"[spec.debug] request item_code={item_code} item_id={item_id} root_qty={root_qty}")

        if item_code is None and item_id is None and (item_ref1c is None or str(item_ref1c).strip() == ""):
            raise HTTPException(status_code=400, detail="Either item_code, item_id or item_ref1c is required")

        item = _get_item_by_code_or_id(db, item_code=item_code, item_id=item_id, item_ref1c=item_ref1c)
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")

        # default and fallback spec resolution
        default_spec_id = _get_default_spec_id(db, int(item.item_id))
        resolved_spec_id = _resolve_spec_id_for_item(db, item)
        used_fallback = (resolved_spec_id is not None and resolved_spec_id != default_spec_id)

        comps_cnt = 0
        ops_cnt = 0
        if resolved_spec_id:
            comps_cnt = db.query(SpecComponent).filter(SpecComponent.spec_id == resolved_spec_id).count()
            ops_cnt = db.query(SpecOperation).filter(SpecOperation.spec_id == resolved_spec_id).count()

        # try build first level children (like tree does)
        units_map = _build_units_map(db)
        parent_node_id = f"item:{int(item.item_id)}:{_round_qty(_to_float(root_qty,1.0), 6)}"
        children = _children_for_item(db, int(item.item_id), _to_float(root_qty, 1.0), parent_node_id, units_map)

        sample: List[Dict[str, Any]] = []
        for n in (children or [])[:10]:
            # Попытаемся получить raw GUID ЕИ по item_id узла (для узлов type=item)
            unit_guid = None
            unit_label = None
            try:
                if n.get("type") == "item":
                    child_item = (n.get("item") or {})
                    cid = child_item.get("id")
                    if cid is not None:
                        ch = db.query(Item).filter(Item.item_id == int(cid)).first()
                        if ch and ch.unit:
                            unit_guid = str(ch.unit).strip()
                            unit_label = _unit_label(units_map, unit_guid)
            except Exception:
                unit_guid = None
                unit_label = None

            sample.append({
                "id": n.get("id"),
                "type": n.get("type"),
                "name": n.get("name"),
                "operationName": (n.get("operation") or {}).get("name") if isinstance(n.get("operation"), dict) else None,
                "stageName": (n.get("stage") or {}).get("name") if isinstance(n.get("stage"), dict) else None,
                "unitGuid": unit_guid if n.get("type") == "item" else None,
                "unitLabel": unit_label if n.get("type") == "item" else None,
            })

        # Данные по корневому изделию с raw GUID и резолвом
        root_unit_guid = (item.unit or "").strip() if item.unit else None
        root_unit_label = _unit_label(units_map, root_unit_guid) if root_unit_guid else None

        result = {
            "item": {
                "id": int(item.item_id),
                "code": item.item_code,
                "name": item.item_name,
                "unit_guid": root_unit_guid,
                "unit_label": root_unit_label,
            },
            "default_spec_id": default_spec_id,
            "resolved_spec_id": resolved_spec_id,
            "used_fallback": bool(used_fallback),
            "components_count": int(comps_cnt),
            "operations_count": int(ops_cnt),
            "children_count": len(children or []),
            "children_sample": sample
        }
        logger.info(f"[spec.debug] item_id={item.item_id} default_spec={default_spec_id} resolved_spec={resolved_spec_id} comps={comps_cnt} ops={ops_cnt} children={len(children or [])}")
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[spec.debug] error: {e}")
        raise HTTPException(status_code=500, detail=f"Specification debug error: {e}")
@router.get("/units-debug")
def get_units_debug(
    item_code: Optional[str] = Query(None, description="Код изделия для проверки его ЕИ"),
    item_id: Optional[int] = Query(None, description="ID изделия для проверки его ЕИ"),
    item_ref1c: Optional[str] = Query(None, description="GUID изделия (Ref_Key) для проверки его ЕИ"),
    unit_guid: Optional[str] = Query(None, description="GUID ЕИ (Ref_Key) для прямой проверки"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Диагностика: что выгружено в таблицу units и как резолвится единица измерения у изделия/в узле спецификации.

    Возвращает:
      {
        "units_total": int,
        "units_sample": [ { unit_ref1c, unit_code, unit_name, short_name, iso_code, base_unit_ref1c, ratio, precision } ... up to 10 ],
        "check": {
          "unit_guid": "...",
          "unit_row": {...} | null
        },
        "item": {
          "id": int,
          "code": str,
          "name": str,
          "unit_guid": str|null,
          "mapped_label": str|null,
          "unit_row": {...} | null
        } | null
      }
    """
    result: Dict[str, Any] = {}

    # 1) Общая информация по справочнику ЕИ
    try:
        total = db.query(Unit).count()
    except Exception as e:
        total = -1
        logger.exception(f"[units.debug] failed to count units: {e}")

    sample_rows: List[Dict[str, Any]] = []
    try:
        rows = db.query(Unit).limit(10).all()
        for u in rows:
            sample_rows.append({
                "unit_ref1c": (u.unit_ref1c or "").strip(),
                "unit_code": (u.unit_code or "").strip() if u.unit_code else None,
                "unit_name": (u.unit_name or "").strip() if u.unit_name else None,
                "short_name": (u.short_name or "").strip() if u.short_name else None,
                "iso_code": (u.iso_code or "").strip() if u.iso_code else None,
                "base_unit_ref1c": (u.base_unit_ref1c or "").strip() if u.base_unit_ref1c else None,
                "ratio": float(u.ratio or 1.0),
                "precision": int(u.precision) if u.precision is not None else None,
            })
    except Exception as e:
        logger.exception(f"[units.debug] failed to fetch sample units: {e}")

    result["units_total"] = total
    result["units_sample"] = sample_rows

    # Словарь GUID -> метка для резолва
    units_map = _build_units_map(db)

    # 2) Прямая проверка GUID (если передан)
    check: Dict[str, Any] = {"unit_guid": unit_guid, "unit_row": None}
    try:
        if unit_guid:
            g = str(unit_guid).strip()
            row = db.query(Unit).filter(Unit.unit_ref1c == g).first()
            if row:
                check["unit_row"] = {
                    "unit_ref1c": (row.unit_ref1c or "").strip(),
                    "unit_code": (row.unit_code or "").strip() if row.unit_code else None,
                    "unit_name": (row.unit_name or "").strip() if row.unit_name else None,
                    "short_name": (row.short_name or "").strip() if row.short_name else None,
                    "iso_code": (row.iso_code or "").strip() if row.iso_code else None,
                    "base_unit_ref1c": (row.base_unit_ref1c or "").strip() if row.base_unit_ref1c else None,
                    "ratio": float(row.ratio or 1.0),
                    "precision": int(row.precision) if row.precision is not None else None,
                    "mapped_label": _unit_label(units_map, g),
                }
    except Exception as e:
        logger.exception(f"[units.debug] check unit_guid failed: {e}")
    result["check"] = check

    # 3) Проверка по изделию (если переданы item_code|item_id)
    item_info: Optional[Dict[str, Any]] = None
    try:
        item: Optional[Item] = _get_item_by_code_or_id(db, item_code=item_code, item_id=item_id, item_ref1c=item_ref1c)
        if item:
            item_unit_guid = (item.unit or "").strip() if item.unit else None
            item_label = _unit_label(units_map, item_unit_guid) if item_unit_guid else None
            unit_row = None
            if item_unit_guid:
                try:
                    u = db.query(Unit).filter(Unit.unit_ref1c == item_unit_guid).first()
                    if u:
                        unit_row = {
                            "unit_ref1c": (u.unit_ref1c or "").strip(),
                            "unit_code": (u.unit_code or "").strip() if u.unit_code else None,
                            "unit_name": (u.unit_name or "").strip() if u.unit_name else None,
                            "short_name": (u.short_name or "").strip() if u.short_name else None,
                            "iso_code": (u.iso_code or "").strip() if u.iso_code else None,
                            "base_unit_ref1c": (u.base_unit_ref1c or "").strip() if u.base_unit_ref1c else None,
                            "ratio": float(u.ratio or 1.0),
                            "precision": int(u.precision) if u.precision is not None else None,
                            "mapped_label": item_label,
                        }
                except Exception:
                    unit_row = None

            item_info = {
                "id": int(item.item_id),
                "code": item.item_code,
                "name": item.item_name,
                "unit_guid": item_unit_guid,
                "mapped_label": item_label,
                "unit_row": unit_row,
            }
    except Exception as e:
        logger.exception(f"[units.debug] item check failed: {e}")

    result["item"] = item_info
    return result
# ------- full tree (recursive) -------

def _build_full_tree(
    db: Session,
    root_item: Item,
    root_qty: float,
    units_map: Dict[str, str],
    max_depth: int = 15,
) -> Dict[str, Any]:
    """
    Строит полное дерево спецификации (BOM) с рекурсивной разверткой всех уровней.
    - Узлы формируются в том же формате, что и /v1/specification/tree
    - Операции включаются на каждом уровне
    - Защита от циклов: помечаем узел предупреждением CYCLE_DETECTED и не уходим глубже по этой ветке
    - Ограничение глубины max_depth (по умолчанию 15)
    """
    # Корневой узел
    node = _make_item_node(
        item=root_item,
        parent_id=None,
        qty_per_parent=None,
        tree_qty=_to_float(root_qty, 1.0),
        stage=None,
        unit=_unit_label(units_map, root_item.unit),
        has_children=_has_children(db, int(root_item.item_id)),
        warnings=[],
    )

    try:
        def _recurse(curr_node: Dict[str, Any], curr_item_id: int, curr_tree_qty: float, depth: int, path: set[int]) -> None:
            if depth >= int(max_depth or 0):
                return

            # Дети текущего узла: номенклатуры и операции
            children = _children_for_item(
                db=db,
                item_id=curr_item_id,
                parent_tree_qty=curr_tree_qty,
                parent_node_id=str(curr_node["id"]),
                units_map=units_map,
            )
            curr_node["children"] = children

            # Рекурсия только для узлов type=item
            for ch in children:
                if ch.get("type") == "item":
                    ch_item = (ch.get("item") or {})
                    cid = ch_item.get("id")
                    if cid is None:
                        continue
                    cid = int(cid)

                    # Защита от циклов
                    if cid in path:
                        w = list(ch.get("warnings") or [])
                        if "CYCLE_DETECTED" not in w:
                            w.append("CYCLE_DETECTED")
                        ch["warnings"] = w
                        # цикл обнаружен — детей для этого узла не строим
                        continue

                    # Рекурсивный заход
                    ch_tree_qty = _to_float(((ch.get("computed") or {}).get("treeQty")), 0.0)
                    _recurse(
                        curr_node=ch,
                        curr_item_id=cid,
                        curr_tree_qty=ch_tree_qty,
                        depth=depth + 1,
                        path=path | {cid},
                    )

        _recurse(
            curr_node=node,
            curr_item_id=int(root_item.item_id),
            curr_tree_qty=_to_float(root_qty, 1.0),
            depth=0,
            path={int(root_item.item_id)},
        )
    except Exception as ex:
        logger.exception(f"[spec.full] recursion error: {ex}")

    return node


@router.get("/full")
def get_specification_full(
    item_code: Optional[str] = Query(None, description="Код изделия (альтернатива item_id/item_ref1c)"),
    item_id: Optional[int] = Query(None, description="ID изделия (альтернатива item_code/item_ref1c)"),
    item_ref1c: Optional[str] = Query(None, description="GUID изделия (Ref_Key, альтернатива item_code/item_id)"),
    root_qty: float = Query(1.0, description="Количество корневого изделия для расчёта"),
    max_depth: int = Query(15, ge=1, le=50, description="Максимальная глубина разворота дерева"),
    debug: bool = Query(False, description="Возвращать диагностическую информацию в meta.debug"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Полное дерево спецификации (с операциями) одним запросом.
    Формат узлов полностью совместим с /v1/specification/tree.

    Пример:
      GET /api/v1/specification/full?item_code=XXX&amp;root_qty=1&amp;max_depth=15
    """
    try:
        logger.info(f"[spec.full] request item_code={item_code} item_id={item_id} root_qty={root_qty} max_depth={max_depth}")

        if item_code is None and item_id is None and (item_ref1c is None or str(item_ref1c).strip() == ""):
            raise HTTPException(status_code=400, detail="Either item_code, item_id or item_ref1c is required")

        item = _get_item_by_code_or_id(db, item_code=item_code, item_id=item_id, item_ref1c=item_ref1c)
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")

        units_map = _build_units_map(db)
        root_node = _build_full_tree(
            db=db,
            root_item=item,
            root_qty=_to_float(root_qty, 1.0),
            units_map=units_map,
            max_depth=int(max_depth or 15),
        )

        meta: Dict[str, Any] = {
            "rootId": root_node.get("id"),
            "requested": {
                "item_code": item_code,
                "item_id": int(item.item_id),
                "root_qty": _round_qty(_to_float(root_qty, 1.0), 3),
                "max_depth": int(max_depth or 15),
            }
        }

        if debug:
            try:
                root_spec_id = _resolve_spec_id_for_item_id(db, int(item.item_id))
                comps_cnt = 0
                ops_cnt = 0
                if root_spec_id:
                    comps_cnt = db.query(SpecComponent).filter(SpecComponent.spec_id == root_spec_id).count()
                    ops_cnt = db.query(SpecOperation).filter(SpecOperation.spec_id == root_spec_id).count()
                meta["debug"] = {
                    "resolved_spec_id": root_spec_id,
                    "components_count": comps_cnt,
                    "operations_count": ops_cnt,
                }
            except Exception as ex:
                meta["debug"] = {"error": f"debug failed: {ex}"}

        logger.info(f"[spec.full] built tree root_id={meta['rootId']}")
        return {"nodes": [root_node], "meta": meta}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[spec.full] error: {e}")
        raise HTTPException(status_code=500, detail=f"Specification full error: {e}")
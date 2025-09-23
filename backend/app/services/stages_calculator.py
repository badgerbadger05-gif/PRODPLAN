from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from ..models import (
    Item,
    RootProduct,
    DefaultSpecification,
    SpecComponent,
    ProductionStage,
)


@dataclass
class StageComponent:
    item_id: int
    item_code: str
    item_name: str
    qty_per_unit: float
    stock_qty: float
    replenishment_method: Optional[str]
    min_batch: Optional[float] = None
    max_batch: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StageProductBlock:
    root_item_id: int
    root_item_code: str
    root_item_name: str
    components: List[Dict[str, Any]]

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StageResult:
    stage_id: int
    stage_name: str
    products: List[Dict[str, Any]]

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _read_last_stock_sync_at() -> Optional[str]:
    """
    Возвращает ISO-дату/время последней синхронизации остатков из файла config/last_sync_time.json,
    если он существует (совместимость с PRODPLANOLD). Иначе None.
    """
    p = Path("config") / "last_sync_time.json"
    if not p.exists():
        return None
    try:
        import json
        data = json.loads(p.read_text("utf-8") or "{}")
        val = str(data.get("last_sync") or "").strip()
        return val or None
    except Exception:
        return None


def _is_production_method(s: Optional[str]) -> bool:
    """
    Фильтр: в этапы попадают только детали с методом пополнения 'Производство'
    (сравнение регистронезависимое, пробелы обрезаются).
    """
    if not s:
        return False
    return str(s).strip().lower() in {"производство", "production"}


def calculate_stages(db: Session) -> Dict[str, Any]:
    """
    Рассчитывает этапы производства по всем изделиям из строк плана выпуска (root_products):

    Алгоритм:
    1) Для каждого изделия из root_products определяем спецификацию по умолчанию (DefaultSpecification).
    2) Рекурсивно разворачиваем спецификации:
       - Для каждого компонента учитываем количество = произведение количеств по цепочке (множитель родителя).
       - Этап появления компонента берём из поля stage_id строки компонента (SpecComponent.stage_id).
       - Включаем в результат только компоненты, у которых replenishment_method == 'Производство'.
    3) Возвращаем структуру: этап -> список изделий -> список компонентов с количеством на 1 изделие.
       Добавляем колонки: "минимальная партия запуска" и "максимальная партия запуска" (нет данных — None),
       а также текущий остаток (items.stock_qty) и метку времени остатков (asOf).

    Возврат:
    {
      "asOf": "2025-09-19T09:06:38.432061" | null,
      "stages": [
        {
          "stage_id": 1,
          "stage_name": "Сборка",
          "products": [
            {
              "root_item_id": 123,
              "root_item_code": "PRD-001",
              "root_item_name": "Изделие 1",
              "components": [
                {
                  "item_id": 456,
                  "item_code": "CMP-001",
                  "item_name": "Деталь 1",
                  "qty_per_unit": 4.0,
                  "stock_qty": 12.0,
                  "replenishment_method": "Производство",
                  "min_batch": null,
                  "max_batch": null
                }
              ]
            }
          ]
        }
      ]
    }
    """
    # Кэш справочников из БД
    # Стадии
    stages = db.query(ProductionStage).all()
    stage_by_id: Dict[int, ProductionStage] = {int(s.stage_id): s for s in stages if s.stage_id is not None}

    # Изделия (item_id -> Item)
    items = db.query(Item).all()
    item_by_id: Dict[int, Item] = {int(x.item_id): x for x in items}

    # Спецификации по умолчанию (item_id -> spec_id), предпочитаем запись без характеристики, иначе первую попавшуюся
    default_specs = db.query(DefaultSpecification).all()
    default_spec_map: Dict[int, int] = {}
    for rec in default_specs:
        iid = int(rec.item_id)
        sid = int(rec.spec_id)
        # Приоритет: если ещё нет записи для item_id, ставим; характеристика не учитывается пока
        if iid not in default_spec_map:
            default_spec_map[iid] = sid

    # Компоненты спецификаций: spec_id -> [SpecComponent]
    # Загружаем лениво и кешируем
    components_cache: Dict[int, List[SpecComponent]] = {}

    def get_components_for_spec(spec_id: int) -> List[SpecComponent]:
        if spec_id in components_cache:
            return components_cache[spec_id]
        comps = db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()
        components_cache[spec_id] = comps
        return comps

    # Корневые изделия плана (строки плана выпуска)
    root_rows = (
        db.query(RootProduct)
        .all()
    )
    root_item_ids: List[int] = [int(r.item_id) for r in root_rows if r.item_id is not None and int(r.item_id) in item_by_id]

    results_per_stage: Dict[int, List[StageProductBlock]] = {}

    def expand(item_id: int, multiplier: float, accum: Dict[Tuple[int, int], float], path: Set[int], depth: int = 0) -> None:
        """
        Рекурсивная развёртка спецификации по умолчанию по item_id.
        accum аккумулирует количество по ключу (stage_id, component_item_id).
        """
        if depth > 50:
            # защита от чрезмерной глубины
            return
        if item_id in path:
            # защита от циклов
            return

        spec_id = default_spec_map.get(item_id)
        if not spec_id:
            return

        new_path = set(path)
        new_path.add(item_id)

        comps = get_components_for_spec(spec_id)
        for comp in comps:
            try:
                child_item_id = int(comp.item_id)
            except Exception:
                continue

            # Количество компонента = множитель родителя * количество из строки состава
            try:
                comp_qty = float(comp.quantity or 0.0)
            except Exception:
                comp_qty = 0.0
            total_qty = multiplier * comp_qty
            if total_qty <= 0:
                # нечего учитывать и разворачивать
                continue

            # Этап появления
            stage_id = int(comp.stage_id) if comp.stage_id is not None else None

            # Отбор только по методу пополнения "Производство"
            child_item = item_by_id.get(child_item_id)
            repl = (child_item.replenishment_method if child_item else None)
            is_prod = _is_production_method(repl)

            if stage_id is not None and is_prod:
                key = (stage_id, child_item_id)
                accum[key] = (accum.get(key, 0.0) + total_qty)

            # Рекурсивно разворачиваем дальше независимо от того, попал ли элемент в текущий этап
            # (т.к. у дочернего могут быть свои этапы появления).
            expand(child_item_id, total_qty, accum, new_path, depth + 1)

    # Строим результат для каждого корневого изделия и группируем по этапам
    for rid in root_item_ids:
        root_item = item_by_id.get(rid)
        if not root_item:
            continue

        # Если нет спецификации по умолчанию — пропускаем изделие
        if rid not in default_spec_map:
            continue

        stage_qty_map: Dict[Tuple[int, int], float] = {}
        expand(rid, 1.0, stage_qty_map, set(), depth=0)

        # Подготовим блоки StageProductBlock по этапам
        # Группируем по stage_id
        per_stage_components: Dict[int, List[StageComponent]] = {}
        for (stage_id, comp_item_id), qty in stage_qty_map.items():
            comp_item = item_by_id.get(comp_item_id)
            if not comp_item:
                continue

            sc = StageComponent(
                item_id=comp_item_id,
                item_code=str(comp_item.item_code or ""),
                item_name=str(comp_item.item_name or ""),
                qty_per_unit=float(qty),
                stock_qty=float(comp_item.stock_qty or 0.0),
                replenishment_method=(comp_item.replenishment_method or None),
                min_batch=None,  # нет явного источника данных
                max_batch=None,  # нет явного источника данных
            )
            per_stage_components.setdefault(stage_id, []).append(sc)

        # Добавим в общую структуру по этапам
        for stg_id, comps in per_stage_components.items():
            # Сортируем компоненты для стабильности (по коду, затем по названию)
            comps_sorted = sorted(comps, key=lambda x: (x.item_code or "", x.item_name or ""))

            block = StageProductBlock(
                root_item_id=rid,
                root_item_code=str(root_item.item_code or ""),
                root_item_name=str(root_item.item_name or ""),
                components=[c.as_dict() for c in comps_sorted],
            )

            if stg_id not in results_per_stage:
                results_per_stage[stg_id] = []
            results_per_stage[stg_id].append(block)

    # Сборка итоговой структуры
    stages_out: List[Dict[str, Any]] = []
    # Отобразим только те этапы, по которым есть данные
    for stg_id, product_blocks in results_per_stage.items():
        s = stage_by_id.get(stg_id)
        stg_name = str(s.stage_name) if s and s.stage_name else f"Этап {stg_id}"
        # Стабилизируем порядок изделий внутри этапа
        products_sorted = sorted(
            product_blocks,
            key=lambda b: (b.root_item_code or "", b.root_item_name or ""),
        )
        stage_res = StageResult(
            stage_id=stg_id,
            stage_name=stg_name,
            products=[p.as_dict() for p in products_sorted],
        )
        stages_out.append(stage_res.as_dict())

    # Стабилизируем порядок этапов по stage_order (если есть) или по имени/ID
    def _stage_sort_key(sr: Dict[str, Any]) -> Tuple[int, str]:
        stg_id = int(sr.get("stage_id") or 0)
        if stg_id in stage_by_id and getattr(stage_by_id[stg_id], "stage_order", None) is not None:
            return (int(stage_by_id[stg_id].stage_order or 0), str(sr.get("stage_name") or ""))
        return (999999, str(sr.get("stage_name") or ""))

    stages_out_sorted = sorted(stages_out, key=_stage_sort_key)

    return {
        "asOf": _read_last_stock_sync_at(),
        "stages": stages_out_sorted,
    }
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
    ProductionResource,
    ResourceStage,
    SpecOperation,
)


@dataclass
class DistributedComponent:
    item_id: int
    item_article: Optional[str]
    item_code: str
    item_name: str
    qty_per_unit: float
    stock_qty: float
    replenishment_method: Optional[str]
    norm_hours: float = 0.0
    stage_id: Optional[int] = None
    stage_name: Optional[str] = None
    min_batch: Optional[float] = None
    max_batch: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProductDistributionBlock:
    root_item_id: int
    root_item_code: str
    root_item_name: str
    components: List[Dict[str, Any]]

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ResourceDistributionResult:
    resource_id: int
    resource_name: str
    products: List[Dict[str, Any]]
    norm_hours: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _read_last_stock_sync_at() -> Optional[str]:
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
    if not s:
        return False
    return str(s).strip().lower() in {"производство", "production"}


def calculate_resource_distribution(db: Session) -> Dict[str, Any]:
    # Кэши справочников
    items = db.query(Item).all()
    item_by_id: Dict[int, Item] = {int(x.item_id): x for x in items}

    default_specs = db.query(DefaultSpecification).all()
    default_spec_map: Dict[int, int] = {}
    for rec in default_specs:
        iid = int(rec.item_id)
        sid = int(rec.spec_id)
        if iid not in default_spec_map:
            default_spec_map[iid] = sid

    components_cache: Dict[int, List[SpecComponent]] = {}
    operations_cache: Dict[int, List[SpecOperation]] = {}

    def get_components_for_spec(spec_id: int) -> List[SpecComponent]:
        if spec_id in components_cache:
            return components_cache[spec_id]
        comps = db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()
        components_cache[spec_id] = comps
        return comps

    def get_operations_for_spec(spec_id: int) -> List[SpecOperation]:
        if spec_id in operations_cache:
            return operations_cache[spec_id]
        ops = db.query(SpecOperation).filter(SpecOperation.spec_id == spec_id).all()
        operations_cache[spec_id] = ops
        return ops

    # Производственные участки и их этапы
    resources = db.query(ProductionResource).all()
    resource_stages = db.query(ResourceStage).all()
    
    stages_by_resource: Dict[int, Set[int]] = {}
    for rs in resource_stages:
        stages_by_resource.setdefault(rs.resource_id, set()).add(rs.stage_id)

    all_stages = db.query(ProductionStage).all()
    stage_name_map: Dict[int, str] = {s.stage_id: s.stage_name for s in all_stages}

    # Корневые изделия из плана
    root_rows = db.query(RootProduct).all()
    root_item_ids: List[int] = [int(r.item_id) for r in root_rows if r.item_id is not None and int(r.item_id) in item_by_id]

    # Результат, сгруппированный по участкам
    results_per_resource: Dict[int, List[ProductDistributionBlock]] = {}

    def expand(item_id: int, multiplier: float, accum: Dict[Tuple[int, int], Dict[str, float]], path: Set[int], depth: int = 0) -> None:
        if depth > 50 or item_id in path:
            return

        spec_id = default_spec_map.get(item_id)
        if not spec_id:
            return

        new_path = set(path)
        new_path.add(item_id)

        # Суммируем нормо-часы операций для текущей спецификации
        spec_ops = get_operations_for_spec(spec_id)
        total_norm_hours = sum(float(op.time_norm or 0.0) for op in spec_ops)

        comps = get_components_for_spec(spec_id)
        for comp in comps:
            # Определяем дочерний элемент и количество
            try:
                child_item_id = int(comp.item_id)
                comp_qty = float(comp.quantity or 0.0)
                total_qty = multiplier * comp_qty
            except (ValueError, TypeError):
                continue

            if total_qty <= 0:
                continue

            # Фильтр по методу пополнения — оставляем в распределении только производимые позиции
            child_item = item_by_id.get(child_item_id)
            is_prod = bool(child_item) and _is_production_method(child_item.replenishment_method if child_item else None)

            # 1) Базово используем этап текущей строки состава
            stage_id: Optional[int] = int(comp.stage_id) if getattr(comp, "stage_id", None) is not None else None

            # Если этап строки сборочный (имя этапа содержит 'сбор' или 'заверш')
            # или тип компонента = 'Сборка', то игнорируем такой stage_id и берём этап из дочерней спецификации
            if stage_id is not None:
                try:
                    st_name = (stage_name_map.get(stage_id) or "").strip().lower()
                except Exception:
                    st_name = ""
                comp_type = (getattr(comp, "component_type", None) or "").strip().lower()
                is_assembly_like = ("сбор" in st_name) or ("заверш" in st_name) or (comp_type == "сборка")
                if is_assembly_like:
                    stage_id = None

            # 2) Если этап не задан в текущем компоненте (или сборочный) — fallback:
            #    берем первый непустой stage_id из детей дочерней спецификации
            if stage_id is None:
                child_spec_id = default_spec_map.get(child_item_id)
                if child_spec_id:
                    grand_children = get_components_for_spec(child_spec_id)
                    for gc in grand_children:
                        if gc.stage_id is not None:
                            try:
                                stage_id = int(gc.stage_id)
                                break
                            except (ValueError, TypeError):
                                continue

            # В аккумулятор попадают только производимые компоненты с распознанным этапом
            if is_prod and stage_id is not None:
                key = (stage_id, child_item_id)
                if key not in accum:
                    accum[key] = {"qty": 0.0, "norm_hours": 0.0}
                accum[key]["qty"] += total_qty
                accum[key]["norm_hours"] += total_norm_hours * multiplier

            # Рекурсивная развёртка продолжается всегда, чтобы найти этапы глубже по дереву
            expand(child_item_id, total_qty, accum, new_path, depth + 1)

    # Расчет для каждого корневого изделия
    for rid in root_item_ids:
        root_item = item_by_id.get(rid)
        if not root_item or rid not in default_spec_map:
            continue

        # stage_id, comp_item_id -> {qty, norm_hours}
        component_data_map: Dict[Tuple[int, int], Dict[str, float]] = {}
        expand(rid, 1.0, component_data_map, set())
        
        # Распределяем компоненты по участкам
        for (stage_id, comp_item_id), data in component_data_map.items():
            comp_item = item_by_id.get(comp_item_id)
            if not comp_item:
                continue

            # --- Финальная логика выбора лучшего участка ---
            candidate_res_ids = [res_id for res_id, st_ids in stages_by_resource.items() if stage_id in st_ids]

            best_resource_id = None
            if len(candidate_res_ids) == 1:
                best_resource_id = candidate_res_ids[0]
            elif len(candidate_res_ids) > 1:
                stage_name = (stage_name_map.get(stage_id) or "").lower().strip()
                
                # Ищем "идеального" кандидата
                perfect_matches = [
                    res_id for res_id in candidate_res_ids
                    if stage_name in (next((r.resource_name for r in resources if r.resource_id == res_id), "") or "").lower()
                ]
                
                if perfect_matches:
                    # Если есть идеальные совпадения, берем первое из них (отсортировав для стабильности)
                    best_resource_id = sorted(perfect_matches)[0]
                else:
                    # Если идеальных нет, берем просто первого кандидата (отсортировав для стабильности)
                    best_resource_id = sorted(candidate_res_ids)[0]
            
            if best_resource_id is not None:
                # Создаем компонент для добавления
                dc = DistributedComponent(
                    item_id=comp_item_id,
                    item_article=str(comp_item.item_article or ""),
                    item_code=str(comp_item.item_code or ""),
                    item_name=str(comp_item.item_name or ""),
                    qty_per_unit=float(data.get("qty", 0.0)),
                    stock_qty=float(comp_item.stock_qty or 0.0),
                    replenishment_method=(comp_item.replenishment_method or None),
                    norm_hours=float(data.get("norm_hours", 0.0)),
                    stage_id=stage_id,
                    stage_name=stage_name_map.get(stage_id)
                )

                # Находим или создаем блок продукта для этого участка
                if best_resource_id not in results_per_resource:
                    results_per_resource[best_resource_id] = []
                
                product_block = next((p for p in results_per_resource[best_resource_id] if p.root_item_id == rid), None)
                
                if not product_block:
                    product_block = ProductDistributionBlock(
                        root_item_id=rid,
                        root_item_code=str(root_item.item_code or ""),
                        root_item_name=str(root_item.item_name or ""),
                        components=[],
                    )
                    results_per_resource[best_resource_id].append(product_block)
                
                # Добавляем компонент в блок продукта, избегая дубликатов
                if not any(c['item_id'] == dc.item_id for c in product_block.components):
                        product_block.components.append(dc.as_dict())


    # Финальная сборка
    output_data: List[Dict[str, Any]] = []
    for res_id, prod_blocks in results_per_resource.items():
        resource = next((r for r in resources if r.resource_id == res_id), None)
        if not resource:
            continue
        
        # Сортировка для стабильности
        for block in prod_blocks:
            block.components = sorted(block.components, key=lambda c: (c.get('item_code', ''), c.get('item_name', '')))
        
        prod_blocks_sorted = sorted(prod_blocks, key=lambda b: (b.root_item_code or "", b.root_item_name or ""))

        # Рассчитаем суммарные нормо-часы для ресурса
        total_resource_norm_hours = sum(
            sum(c.get("norm_hours", 0.0) for c in p.components)
            for p in prod_blocks
        )

        res_result = ResourceDistributionResult(
            resource_id=res_id,
            resource_name=resource.resource_name,
            norm_hours=total_resource_norm_hours,
            products=[p.as_dict() for p in prod_blocks_sorted],
        )
        output_data.append(res_result.as_dict())

    # Сортировка участков
    output_data_sorted = sorted(output_data, key=lambda r: r.get('resource_name', ''))

    return {
        "asOf": _read_last_stock_sync_at(),
        "resources": output_data_sorted,
    }

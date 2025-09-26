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
    norm_hours_total: float = 0.0
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
    root_item_ids: List[int] = [
        int(r.item_id) for r in root_rows if r.item_id is not None and int(r.item_id) in item_by_id
    ]

    # Результат, сгруппированный по участкам
    results_per_resource: Dict[int, List[ProductDistributionBlock]] = {}
    # Список неоднозначных узлов
    ambiguous_entries: List[Dict[str, Any]] = []

    # Новая логика развёртки:
    # - На каждом уровне рассматриваем РОДИТЕЛЯ P (item_id), у которого есть дети-компоненты в его спецификации.
    # - Этап родителя = единый stage_id всех ЕГО детей (если он один); иначе — ambiguous (ошибка данных).
    # - Норматив родителя = сумма time_norm всех операций его спецификации; умножаем на количество вхождений (occurrences).
    # - Листья (без детей) сами в распределение не попадают.
    def expand(
        parent_item_id: int,
        occurrences: float,
        parent_entries: List[Dict[str, Any]],
        ambiguous: List[Dict[str, Any]],
        path: Set[int],
        depth: int = 0,
    ) -> None:
        if depth > 200:
            return
        if parent_item_id in path:
            # защита от цикла
            return

        spec_id = default_spec_map.get(parent_item_id)
        if not spec_id:
            return

        new_path = set(path)
        new_path.add(parent_item_id)

        comps = get_components_for_spec(spec_id)
        if not comps:
            # Лист — не распределяем, но его stage_id мог определять этап для его родителя (уже учтено на уровне выше)
            return

        # Собираем stage_id из детей РОВНО этого уровня
        child_stage_ids: Set[int] = set()
        for comp in comps:
            if comp.stage_id is not None:
                try:
                    child_stage_ids.add(int(comp.stage_id))
                except (ValueError, TypeError):
                    continue

        # Определяем этап родителя
        parent_stage_id: Optional[int] = None
        if len(child_stage_ids) == 1:
            parent_stage_id = next(iter(child_stage_ids))
        elif len(child_stage_ids) == 0:
            # Ошибка данных: у всех детей отсутствует этап
            ambiguous.append(
                {
                    "item_id": parent_item_id,
                    "item_code": str(item_by_id.get(parent_item_id).item_code) if item_by_id.get(parent_item_id) else "",
                    "item_name": str(item_by_id.get(parent_item_id).item_name) if item_by_id.get(parent_item_id) else "",
                    "spec_id": spec_id,
                    "reason": "NO_CHILD_STAGE",
                    "child_stage_ids": [],
                }
            )
        else:
            # Ошибка данных: у детей разные этапы
            ambiguous.append(
                {
                    "item_id": parent_item_id,
                    "item_code": str(item_by_id.get(parent_item_id).item_code) if item_by_id.get(parent_item_id) else "",
                    "item_name": str(item_by_id.get(parent_item_id).item_name) if item_by_id.get(parent_item_id) else "",
                    "spec_id": spec_id,
                    "reason": "MIXED_CHILD_STAGES",
                    "child_stage_ids": sorted(child_stage_ids),
                }
            )

        # Если этап родителя определён, фиксируем запись распределения для РОДИТЕЛЯ
        if parent_stage_id is not None:
            # Норматив родителей: сумма time_norm всех операций ЭТОЙ спецификации (stage операций игнорируем)
            spec_ops = get_operations_for_spec(spec_id)
            parent_norm_hours_single = sum(float(op.time_norm or 0.0) for op in spec_ops)
            total_parent_norm = parent_norm_hours_single
            parent_entries.append(
                {
                    "stage_id": parent_stage_id,
                    "item_id": parent_item_id,
                    "occurrences": float(occurrences or 0.0),
                    "norm_hours": total_parent_norm,
                }
            )

        # Независимо от статуса этапа родителя спускаемся ниже — на каждом уровне родителя считаем отдельно
        for comp in comps:
            try:
                child_item_id = int(comp.item_id)
                comp_qty = float(comp.quantity or 0.0)
                child_occurrences = float(occurrences or 0.0) * comp_qty
            except (ValueError, TypeError):
                continue

            if child_occurrences <= 0:
                continue

            expand(child_item_id, child_occurrences, parent_entries, ambiguous, new_path, depth + 1)

    # Расчёт для каждого корневого изделия
    for rid in root_item_ids:
        root_item = item_by_id.get(rid)
        if not root_item or rid not in default_spec_map:
            continue

        # Список распределённых РОДИТЕЛЕЙ для текущего корня
        parent_entries: List[Dict[str, Any]] = []
        expand(rid, 1.0, parent_entries, ambiguous_entries, set())

        # Распределяем РОДИТЕЛЕЙ по участкам на основе их этапа
        for entry in parent_entries:
            stage_id = int(entry["stage_id"])
            parent_item_id = int(entry["item_id"])
            norm_hours_val = float(entry.get("norm_hours", 0.0))
            occurrences_val = float(entry.get("occurrences", 0.0))

            parent_item = item_by_id.get(parent_item_id)
            if not parent_item:
                continue

            # Подбор кандидатов-участков по этапу
            candidate_res_ids = [res_id for res_id, st_ids in stages_by_resource.items() if stage_id in st_ids]

            best_resource_id = None
            if len(candidate_res_ids) == 1:
                best_resource_id = candidate_res_ids[0]
            elif len(candidate_res_ids) > 1:
                stage_name = (stage_name_map.get(stage_id) or "").lower().strip()

                # Ищем "идеального" кандидата по включению имени этапа в имя участка
                perfect_matches = [
                    res_id
                    for res_id in candidate_res_ids
                    if stage_name in (next((r.resource_name for r in resources if r.resource_id == res_id), "") or "").lower()
                ]

                if perfect_matches:
                    best_resource_id = sorted(perfect_matches)[0]
                else:
                    best_resource_id = sorted(candidate_res_ids)[0]

            if best_resource_id is not None:
                # Создаём компонент-строку для РОДИТЕЛЯ
                dc = DistributedComponent(
                    item_id=parent_item_id,
                    item_article=str(parent_item.item_article or ""),
                    item_code=str(parent_item.item_code or ""),
                    item_name=str(parent_item.item_name or ""),
                    qty_per_unit=occurrences_val,  # кратность вхождений родителя в изделии
                    stock_qty=float(parent_item.stock_qty or 0.0),
                    replenishment_method=(parent_item.replenishment_method or None),
                    norm_hours=norm_hours_val,
                    norm_hours_total=norm_hours_val * occurrences_val,
                    stage_id=stage_id,
                    stage_name=stage_name_map.get(stage_id),
                )

                # Находим или создаём блок продукта для этого участка
                if best_resource_id not in results_per_resource:
                    results_per_resource[best_resource_id] = []

                product_block = next(
                    (p for p in results_per_resource[best_resource_id] if p.root_item_id == rid), None
                )
                if not product_block:
                    product_block = ProductDistributionBlock(
                        root_item_id=rid,
                        root_item_code=str(root_item.item_code or ""),
                        root_item_name=str(root_item.item_name or ""),
                        components=[],
                    )
                    results_per_resource[best_resource_id].append(product_block)

                # ВАЖНО: не удаляем дубликаты по item_id — каждое вхождение учитывается отдельно
                product_block.components.append(dc.as_dict())

    # Финальная сборка
    output_data: List[Dict[str, Any]] = []
    for res_id, prod_blocks in results_per_resource.items():
        resource = next((r for r in resources if r.resource_id == res_id), None)
        if not resource:
            continue

        # Сортировка для стабильности
        for block in prod_blocks:
            block.components = sorted(block.components, key=lambda c: (c.get("item_code", ""), c.get("item_name", "")))

        prod_blocks_sorted = sorted(prod_blocks, key=lambda b: (b.root_item_code or "", b.root_item_name or ""))

        # Суммарные нормо-часы для ресурса
        total_resource_norm_hours = sum(
            sum(
                float(
                    c.get(
                        "norm_hours_total",
                        float(c.get("norm_hours", 0.0)) * float(c.get("qty_per_unit", 1.0))
                    )
                )
                for c in p.components
            )
            for p in prod_blocks_sorted
        )

        res_result = ResourceDistributionResult(
            resource_id=res_id,
            resource_name=resource.resource_name,
            norm_hours=total_resource_norm_hours,
            products=[p.as_dict() for p in prod_blocks_sorted],
        )
        output_data.append(res_result.as_dict())

    # Сортировка участков
    output_data_sorted = sorted(output_data, key=lambda r: r.get("resource_name", ""))

    # Возвращаем также список неоднозначных узлов (опционально для UI)
    return {
        "asOf": _read_last_stock_sync_at(),
        "resources": output_data_sorted,
        "ambiguous": ambiguous_entries,
    }

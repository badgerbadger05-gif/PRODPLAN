"""Ремонтный модуль спецификаций — операция A (правка состава).

Три безопасных действия над составом спецификации, БЕЗ «сырого» удаления:
- restage_component  — сменить этап списания компонента в той же спеке;
- move_component     — перенести компонент между спеками (уровнями) одной транзакцией
                       (delete в A + add в B = «переиспользовать, не удалить»);
- add_component      — добавить компонент с where-used pre-check (если деталь уже есть
                       на другом уровне — предупреждаем, возможно нужен move).

Инвариант безопасности: операция не должна «полностью удалить» деталь из состава
(глобальное присутствие в spec_components не падает до нуля).

ВАЖНО про 1С: фактическая запись здесь НЕ выполняется. Каждый результат несёт
`pending_1c` — список затронутых спек. Реальная выгрузка табличной части `Состав`
в 1С — отдельный supervised-шаг (read-modify-write против unf_demo), потому что
PRODPLAN не хранит `СпособПополнения`/`СкладПоУмолчанию` нетронутых строк и не может
собрать полный массив `Состав` только из локального состояния.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import (
    DefaultSpecification,
    Item,
    ProductionKind,
    ProductionStage,
    SpecComponent,
    Specification,
)
from .specification_sync import _norm_component_spec_ref


class SpecRepairError(ValueError):
    """Невозможно безопасно выполнить правку состава."""


def specs_containing_item(db: Session, item_id: int) -> List[int]:
    """spec_id всех спецификаций, где деталь стоит компонентом (прямое вхождение)."""
    rows = (
        db.query(SpecComponent.spec_id)
        .filter(SpecComponent.item_id == int(item_id))
        .distinct()
        .all()
    )
    return sorted({int(r[0]) for r in rows})


def _global_presence(db: Session, item_id: int) -> int:
    return db.query(SpecComponent).filter(SpecComponent.item_id == int(item_id)).count()


def _neighbor_stage_id(db: Session, spec_id: int, exclude_component_id: Optional[int] = None) -> Optional[int]:
    """Этап «как у соседей» — самый частый stage_id среди строк целевой спеки.

    Этап в 1С обязателен, но инертен (этапное производство не используется), поэтому
    при move/add подставляем его от соседних строк, а не заставляем технолога думать.
    """
    q = (
        db.query(SpecComponent.stage_id)
        .filter(SpecComponent.spec_id == int(spec_id), SpecComponent.stage_id.isnot(None))
    )
    if exclude_component_id is not None:
        q = q.filter(SpecComponent.component_id != int(exclude_component_id))
    stages = [int(r[0]) for r in q.all() if r[0] is not None]
    if not stages:
        return None
    return Counter(stages).most_common(1)[0][0]


def _get_component(db: Session, component_id: int) -> SpecComponent:
    comp = db.query(SpecComponent).filter_by(component_id=int(component_id)).first()
    if not comp:
        raise SpecRepairError(f"Строка состава не найдена: component_id={component_id}")
    return comp


def _require_spec(db: Session, spec_id: int) -> Specification:
    spec = db.query(Specification).filter_by(spec_id=int(spec_id)).first()
    if not spec:
        raise SpecRepairError(f"Спецификация не найдена: spec_id={spec_id}")
    return spec


def _pending_1c(spec_ids: List[int]) -> Dict[str, Any]:
    return {
        "specs": sorted({int(s) for s in spec_ids}),
        "note": (
            "Запись в 1С не выполнена. Реальная выгрузка состава — supervised "
            "read-modify-write против unf_demo (PATCH полного массива Состав с "
            "сохранением СпособПополнения/СкладПоУмолчанию нетронутых строк)."
        ),
    }


def _finish(db: Session, dry_run: bool) -> None:
    if dry_run:
        db.rollback()
    else:
        db.commit()


def restage_component(db: Session, *, component_id: int, new_stage_id: Optional[int], dry_run: bool = True) -> Dict[str, Any]:
    """Сменить этап списания компонента в той же спецификации."""
    comp = _get_component(db, component_id)
    if new_stage_id is not None and not db.query(ProductionStage).filter_by(stage_id=int(new_stage_id)).first():
        raise SpecRepairError(f"Этап не найден: stage_id={new_stage_id}")

    old_stage_id = comp.stage_id
    comp.stage_id = new_stage_id
    db.flush()

    result = {
        "action": "restage",
        "ok": True,
        "component_id": int(component_id),
        "spec_id": int(comp.spec_id),
        "old_stage_id": (int(old_stage_id) if old_stage_id is not None else None),
        "new_stage_id": (int(new_stage_id) if new_stage_id is not None else None),
        "warnings": [],
        "pending_1c": _pending_1c([comp.spec_id]),
        "dry_run": bool(dry_run),
    }
    _finish(db, dry_run)
    return result


def move_component(
    db: Session,
    *,
    component_id: int,
    target_spec_id: int,
    new_stage_id: Optional[int] = None,
    force: bool = False,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Перенести компонент из текущей спеки в целевую (другой уровень сборки).

    Делается одной транзакцией: добавляем строку в целевую спеку, удаляем исходную.
    Деталь не теряется из изделия. Этап в целевой спеке — явный или «как у соседей».
    """
    comp = _get_component(db, component_id)
    source_spec_id = int(comp.spec_id)
    item_id = int(comp.item_id)

    if int(target_spec_id) == source_spec_id:
        raise SpecRepairError("Целевая спецификация совпадает с исходной")
    _require_spec(db, target_spec_id)

    stage_id = new_stage_id if new_stage_id is not None else _neighbor_stage_id(db, int(target_spec_id))

    specs_before = specs_containing_item(db, item_id)

    new_row = SpecComponent(
        spec_id=int(target_spec_id),
        item_id=item_id,
        quantity=comp.quantity,
        stage_id=stage_id,
        component_type=comp.component_type,
        component_spec_ref1c=comp.component_spec_ref1c,
    )
    db.add(new_row)
    db.delete(comp)
    db.flush()

    presence_after = _global_presence(db, item_id)
    if presence_after < 1 and not force:
        db.rollback()
        raise SpecRepairError("Перемещение оставит деталь вне всех спецификаций (используйте force для подтверждения)")

    specs_after = specs_containing_item(db, item_id)

    result = {
        "action": "move",
        "ok": True,
        "component_id": int(component_id),
        "item_id": item_id,
        "from_spec_id": source_spec_id,
        "to_spec_id": int(target_spec_id),
        "stage_id": (int(stage_id) if stage_id is not None else None),
        "safety": {
            "global_presence_after": int(presence_after),
            "specs_before": specs_before,
            "specs_after": specs_after,
        },
        "warnings": [],
        "pending_1c": _pending_1c([source_spec_id, int(target_spec_id)]),
        "dry_run": bool(dry_run),
    }
    _finish(db, dry_run)
    return result


def add_component(
    db: Session,
    *,
    spec_id: int,
    item_id: int,
    quantity: Any,
    component_type: str = "Сборка",
    stage_id: Optional[int] = None,
    component_spec_ref1c: Optional[str] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Добавить компонент в спеку. where-used pre-check: если деталь уже есть на
    другом уровне — предупреждаем (возможно, нужен move, а не дубль)."""
    _require_spec(db, spec_id)
    if not db.query(Item).filter_by(item_id=int(item_id)).first():
        raise SpecRepairError(f"Номенклатура не найдена: item_id={item_id}")
    if quantity is None or float(quantity) <= 0:
        raise SpecRepairError("Количество должно быть > 0")

    if stage_id is None:
        stage_id = _neighbor_stage_id(db, int(spec_id))

    warnings: List[Dict[str, Any]] = []
    used_elsewhere = [sid for sid in specs_containing_item(db, item_id) if sid != int(spec_id)]
    if used_elsewhere:
        warnings.append({
            "code": "ALREADY_USED_ELSEWHERE",
            "specs": used_elsewhere,
            "hint": "Деталь уже есть в других спеках — проверьте, не нужен ли перенос (move) вместо добавления.",
        })

    row = SpecComponent(
        spec_id=int(spec_id),
        item_id=int(item_id),
        quantity=quantity,
        stage_id=stage_id,
        component_type=component_type,
        component_spec_ref1c=_norm_component_spec_ref(component_spec_ref1c),
    )
    db.add(row)
    db.flush()

    result = {
        "action": "add",
        "ok": True,
        "spec_id": int(spec_id),
        "item_id": int(item_id),
        "stage_id": (int(stage_id) if stage_id is not None else None),
        "component_type": component_type,
        "warnings": warnings,
        "pending_1c": _pending_1c([spec_id]),
        "dry_run": bool(dry_run),
    }
    _finish(db, dry_run)
    return result


def _resolve_default_spec_id(db: Session, item_id: int) -> Optional[int]:
    rec = (
        db.query(DefaultSpecification)
        .filter(DefaultSpecification.item_id == int(item_id))
        .order_by(DefaultSpecification.updated_at.desc(), DefaultSpecification.id.desc())
        .first()
    )
    return int(rec.spec_id) if rec and rec.spec_id is not None else None


def _kind_payload(kind: Optional[ProductionKind]) -> Optional[Dict[str, Any]]:
    if not kind:
        return None
    return {"id": int(kind.id), "ref_1c": kind.ref_1c, "name": kind.name}


def preview_kind_change(db: Session, *, item_id: int, new_production_kind_id: int) -> Dict[str, Any]:
    """Операция Б (read-only превью). Чек-лист родителей, которых затронет смена вида
    производства детали.

    Смена вида в 1С требует НОВОЙ спеки (новый GUID присваивает 1С), поэтому сам apply —
    supervised-шаг. Здесь только показываем: какие родительские строки состава явно
    закрепили текущую спеку детали (Сборка/Узел) и потому потребуют каскадного
    переключения на новую спеку. Строки с пустой закреплённой спекой пойдут по новой
    основной сами и в каскад не входят.
    """
    item = db.query(Item).filter_by(item_id=int(item_id)).first()
    if not item:
        raise SpecRepairError(f"Номенклатура не найдена: item_id={item_id}")

    new_kind = db.query(ProductionKind).filter_by(id=int(new_production_kind_id)).first()
    if not new_kind:
        raise SpecRepairError(f"Вид производства не найден: id={new_production_kind_id}")

    spec_id = _resolve_default_spec_id(db, int(item_id))
    if not spec_id:
        raise SpecRepairError("У детали нет основной спецификации (default_specifications)")
    spec = db.query(Specification).filter_by(spec_id=int(spec_id)).first()
    current_kind = (
        db.query(ProductionKind).filter_by(id=int(spec.production_kind_id)).first()
        if spec and spec.production_kind_id
        else None
    )

    old_ref = spec.spec_ref1c if spec else None
    affected: List[Dict[str, Any]] = []
    if old_ref:
        rows = (
            db.query(SpecComponent)
            .filter(
                SpecComponent.item_id == int(item_id),
                SpecComponent.component_spec_ref1c == old_ref,
            )
            .all()
        )
        for r in rows:
            parent = db.query(Specification).filter_by(spec_id=int(r.spec_id)).first()
            affected.append({
                "parent_spec_id": int(r.spec_id),
                "parent_spec_code": (parent.spec_code if parent else None),
                "component_id": int(r.component_id),
                "component_type": r.component_type,
            })

    return {
        "action": "kind_change_preview",
        "item_id": int(item_id),
        "current_spec_id": int(spec_id),
        "current_spec_ref1c": old_ref,
        "current_kind": _kind_payload(current_kind),
        "new_kind": _kind_payload(new_kind),
        "cascade": {
            "affected_parent_rows": len(affected),
            "parents": affected,
            "note": (
                "Превью только. Apply (POST новой спеки + переключение основной в "
                "СпецификацииПоУмолчанию + каскадный PATCH родителей) — supervised "
                "шаг против unf_demo: новой спеке нужен GUID из 1С."
            ),
        },
    }


# --- Мост к 1С: резолв локальной правки в ключи 1С для spec_writeback_1c ---
# Возвращают «план» в терминах GUID 1С; сам read-modify-write выполняет supervised
# раннер через SpecWriteback. Здесь нет ни одного обращения к 1С.

def _require_spec_ref(spec: Specification) -> str:
    ref = (spec.spec_ref1c or "").strip()
    if not ref:
        raise SpecRepairError(f"У спецификации spec_id={spec.spec_id} нет spec_ref1c — нечего писать в 1С")
    return ref


def _stage_ref(db: Session, stage_id: Optional[int]) -> Optional[str]:
    if stage_id is None:
        return None
    st = db.query(ProductionStage).filter_by(stage_id=int(stage_id)).first()
    if not st:
        raise SpecRepairError(f"Этап не найден: stage_id={stage_id}")
    return (st.stage_ref1c or "").strip() or None


def build_restage_plan(db: Session, *, component_id: int, new_stage_id: Optional[int]) -> Dict[str, Any]:
    """Ключи 1С для restage: какую строку какой спеки и на какой этап перевести."""
    comp = _get_component(db, component_id)
    spec = _require_spec(db, comp.spec_id)
    item = db.query(Item).filter_by(item_id=int(comp.item_id)).first()
    return {
        "op": "restage",
        "spec_ref": _require_spec_ref(spec),
        "nomenclature_key": (item.item_ref1c if item else None),
        "child_spec_key": comp.component_spec_ref1c,
        "new_stage_key": _stage_ref(db, new_stage_id),
    }


def build_move_plan(db: Session, *, component_id: int, target_spec_id: int, new_stage_id: Optional[int] = None) -> Dict[str, Any]:
    """Ключи 1С для move: вынуть строку из source-спеки и добавить в target-спеку."""
    comp = _get_component(db, component_id)
    if int(target_spec_id) == int(comp.spec_id):
        raise SpecRepairError("Целевая спецификация совпадает с исходной")
    source_spec = _require_spec(db, comp.spec_id)
    target_spec = _require_spec(db, target_spec_id)
    item = db.query(Item).filter_by(item_id=int(comp.item_id)).first()
    return {
        "op": "move",
        "source_spec_ref": _require_spec_ref(source_spec),
        "target_spec_ref": _require_spec_ref(target_spec),
        "nomenclature_key": (item.item_ref1c if item else None),
        "child_spec_key": comp.component_spec_ref1c,
        "new_stage_key": _stage_ref(db, new_stage_id),
    }

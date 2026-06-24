"""Запись исправлений состава спецификаций обратно в 1С (read-modify-write).

ПРИНЦИП БЕЗОПАСНОСТИ: табличную часть `Состав` в 1С OData нельзя патчить построчно —
PATCH заменяет весь массив. PRODPLAN не хранит все поля строки (СпособПополнения,
СкладПоУмолчанию, ЕдиницаИзмерения), поэтому собрать массив из локального состояния
нельзя — он затёр бы эти поля. Решение: читаем текущий `Состав` из 1С, меняем ТОЛЬКО
целевые строки/поля, патчим обратно. Все нетронутые строки сохраняются как есть.

Чистые helper-функции (без I/O) держат ошибкоопасную логику мутации и легко тестируются.
Оркестрация (`SpecWriteback`) по умолчанию dry_run=True: ничего не пишет, возвращает
предпросмотр payload. Первый реальный write — только против unf_demo под присмотром.
"""
from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional, Tuple

from .specification_sync import _norm_component_spec_ref

SOSTAV = "Состав"
SPEC_ENTITY = "Catalog_Спецификации"


def _norm_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _row_matches(row: Dict[str, Any], nomenclature_key: str, child_spec_key: Optional[str]) -> bool:
    same_item = _norm_key(row.get("Номенклатура_Key")) == _norm_key(nomenclature_key)
    same_spec = _norm_component_spec_ref(row.get("Спецификация_Key")) == _norm_component_spec_ref(child_spec_key)
    return same_item and same_spec


def renumber(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Проставляет LineNumber последовательно (1С нумерует строки табличной части)."""
    out = []
    for idx, row in enumerate(rows, start=1):
        r = copy.deepcopy(row)
        r["LineNumber"] = str(idx)
        out.append(r)
    return out


def set_stage_on_rows(
    rows: List[Dict[str, Any]],
    *,
    nomenclature_key: str,
    child_spec_key: Optional[str],
    new_stage_key: Optional[str],
) -> Tuple[List[Dict[str, Any]], int]:
    """restage: на совпавших строках меняет только Этап_Key, остальные поля не трогает."""
    changed = 0
    out: List[Dict[str, Any]] = []
    for row in rows:
        r = copy.deepcopy(row)
        if _row_matches(r, nomenclature_key, child_spec_key):
            r["Этап_Key"] = new_stage_key or ""
            changed += 1
        out.append(r)
    return out, changed


def pop_row(
    rows: List[Dict[str, Any]],
    *,
    nomenclature_key: str,
    child_spec_key: Optional[str],
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """move (источник): вынимает первую совпавшую строку целиком (со всеми её полями)."""
    out: List[Dict[str, Any]] = []
    popped: Optional[Dict[str, Any]] = None
    for row in rows:
        if popped is None and _row_matches(row, nomenclature_key, child_spec_key):
            popped = copy.deepcopy(row)
            continue
        out.append(copy.deepcopy(row))
    return out, popped


def append_row(rows: List[Dict[str, Any]], row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """move (приёмник) / add: добавляет строку (полный 1С-словарь) в конец состава."""
    out = [copy.deepcopy(r) for r in rows]
    out.append(copy.deepcopy(row))
    return out


def repoint_child_spec(
    rows: List[Dict[str, Any]],
    *,
    nomenclature_key: str,
    old_spec_key: str,
    new_spec_key: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """Каскад смены вида: на строках детали с закреплённой старой спекой меняет
    только Спецификация_Key на новую. Остальные поля сохраняет."""
    changed = 0
    out: List[Dict[str, Any]] = []
    for row in rows:
        r = copy.deepcopy(row)
        if _row_matches(r, nomenclature_key, old_spec_key):
            r["Спецификация_Key"] = new_spec_key
            changed += 1
        out.append(r)
    return out, changed


class SpecWriteback:
    """Тонкая оркестрация read-modify-write над одной/несколькими спеками.

    client — объект с интерфейсом OData1CClient: get_all(entity, filter_query=, select_fields=)
    и patch(endpoint, payload). По умолчанию dry_run=True (ничего не пишет).
    """

    def __init__(self, client: Any, *, dry_run: bool = True):
        self.client = client
        self.dry_run = bool(dry_run)

    def read_sostav(self, spec_ref: str) -> List[Dict[str, Any]]:
        records = self.client.get_all(
            SPEC_ENTITY,
            filter_query=f"Ref_Key eq guid'{spec_ref}'",
            select_fields=["Ref_Key", SOSTAV],
        )
        if not records:
            raise ValueError(f"Спецификация не найдена в 1С: {spec_ref}")
        return list(records[0].get(SOSTAV) or [])

    def patch_sostav(self, spec_ref: str, new_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        payload = {SOSTAV: renumber(new_rows)}
        if self.dry_run:
            return {"dry_run": True, "spec_ref": spec_ref, "would_patch": payload}
        endpoint = f"{SPEC_ENTITY}(guid'{spec_ref}')"
        resp = self.client.patch(endpoint, payload)
        return {"dry_run": False, "spec_ref": spec_ref, "response": resp}

    def apply_to_sostav(self, spec_ref: str, mutate: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]) -> Dict[str, Any]:
        rows = self.read_sostav(spec_ref)
        new_rows = mutate(rows)
        return self.patch_sostav(spec_ref, new_rows)

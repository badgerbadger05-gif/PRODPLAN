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
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Tuple

from .specification_sync import _norm_component_spec_ref

SOSTAV = "Состав"
SPEC_ENTITY = "Catalog_Спецификации"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"


class SpecWritebackError(RuntimeError):
    """Не удалось записать исправление состава в 1С."""


def _guard(op: str, fn: "Callable[[], Any]") -> Any:
    """Выполняет I/O-операцию записи в 1С, превращая любую ошибку (сеть/HTTP/данные)
    в SpecWritebackError — чтобы вызывающий слой отдал 502, а не 500."""
    try:
        return fn()
    except SpecWritebackError:
        raise
    except Exception as exc:  # noqa: BLE001 — намеренно широко: I/O 1С ненадёжно
        raise SpecWritebackError(f"{op}: ошибка записи в 1С: {exc}") from exc


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


def _dominant_stage(rows: List[Dict[str, Any]]) -> Optional[str]:
    stages = [r.get("Этап_Key") for r in rows if r.get("Этап_Key")]
    return Counter(stages).most_common(1)[0][0] if stages else None


def build_new_sostav_row(
    template: Optional[Dict[str, Any]],
    *,
    nomenclature_key: str,
    unit_key: Optional[str],
    quantity: Any,
    stage_key: Optional[str],
    component_type: str,
    child_spec_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Полный 1С-словарь новой строки состава. Структурные поля берём у строки-шаблона
    того же состава (если есть), затирая идентифицирующие. Пустой Ref_Key — 1С присвоит свой."""
    row = copy.deepcopy(template) if template else {}
    row = {k: v for k, v in row.items() if "@" not in k}
    row.update({
        "Ref_Key": "",
        "Номенклатура_Key": nomenclature_key,
        "Количество": quantity,
        "КоличествоПродукции": 1,
        "Этап_Key": stage_key or "",
        "ТипСтрокиСостава": component_type,
        "Спецификация_Key": child_spec_key or ZERO_GUID,
        "Характеристика_Key": ZERO_GUID,
        "Описание": "",
    })
    if unit_key:
        row["ЕдиницаИзмерения"] = unit_key
    return row


def writeback_restage(
    client: Any,
    *,
    spec_ref: str,
    nomenclature_key: str,
    child_spec_key: Optional[str],
    new_stage_key: Optional[str],
    dry_run: bool = True,
) -> Dict[str, Any]:
    """restage в 1С: на совпавшей строке состава меняем только Этап_Key."""
    def _run():
        sb = SpecWriteback(client, dry_run=dry_run)
        rows = sb.read_sostav(spec_ref)
        new_rows, changed = set_stage_on_rows(
            rows, nomenclature_key=nomenclature_key, child_spec_key=child_spec_key, new_stage_key=new_stage_key
        )
        if changed != 1:
            raise SpecWritebackError(
                f"restage: в 1С ожидалась ровно 1 совпавшая строка, найдено {changed} "
                f"(spec={spec_ref}, ном={nomenclature_key})"
            )
        res = sb.patch_sostav(spec_ref, new_rows)
        return {"op": "restage", "changed": changed, "rows": len(new_rows), "patch": res}

    return _guard("restage", _run)


def writeback_move(
    client: Any,
    *,
    source_spec_ref: str,
    target_spec_ref: str,
    nomenclature_key: str,
    child_spec_key: Optional[str],
    new_stage_key: Optional[str] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """move в 1С: APPEND в target первым, потом POP из source (деталь не теряется)."""
    if _norm_key(source_spec_ref) == _norm_key(target_spec_ref):
        raise SpecWritebackError("move: исходная и целевая спека совпадают")

    def _run():
        sb = SpecWriteback(client, dry_run=dry_run)
        src = sb.read_sostav(source_spec_ref)
        dst = sb.read_sostav(target_spec_ref)
        remaining, popped = pop_row(src, nomenclature_key=nomenclature_key, child_spec_key=child_spec_key)
        if popped is None:
            raise SpecWritebackError(
                f"move: строка не найдена в исходной спеке 1С (spec={source_spec_ref}, ном={nomenclature_key})"
            )
        moved = copy.deepcopy(popped)
        moved["Этап_Key"] = new_stage_key or _dominant_stage(dst) or moved.get("Этап_Key") or ""
        new_dst = append_row(dst, moved)
        # порядок критичен: сначала добавить в target, затем убрать из source
        res_target = sb.patch_sostav(target_spec_ref, new_dst)
        res_source = sb.patch_sostav(source_spec_ref, remaining)
        return {
            "op": "move",
            "stage_key": moved["Этап_Key"],
            "target_rows": len(new_dst),
            "source_rows": len(remaining),
            "patch_target": res_target,
            "patch_source": res_source,
        }

    return _guard("move", _run)


def writeback_add(
    client: Any,
    *,
    spec_ref: str,
    nomenclature_key: str,
    unit_key: Optional[str],
    quantity: Any,
    stage_key: Optional[str],
    component_type: str,
    child_spec_key: Optional[str] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """add в 1С: append новой строки (полный словарь из шаблона состава)."""
    def _run():
        sb = SpecWriteback(client, dry_run=dry_run)
        rows = sb.read_sostav(spec_ref)
        template = rows[0] if rows else None
        new_row = build_new_sostav_row(
            template,
            nomenclature_key=nomenclature_key,
            unit_key=unit_key,
            quantity=quantity,
            stage_key=stage_key,
            component_type=component_type,
            child_spec_key=child_spec_key,
        )
        new_rows = append_row(rows, new_row)
        res = sb.patch_sostav(spec_ref, new_rows)
        return {"op": "add", "rows": len(new_rows), "patch": res}

    return _guard("add", _run)


def build_client_from_config() -> Any:
    """OData1CClient из сохранённого конфига (config/odata_config.json)."""
    from .odata_config import load_odata_config, resolve_config_secrets
    from .odata_client import OData1CClient

    cfg = resolve_config_secrets(load_odata_config())
    base_url = (cfg.get("base_url") or "").strip()
    if not base_url:
        raise SpecWritebackError("OData не настроен: пустой base_url в config/odata_config.json")
    return OData1CClient(
        base_url=base_url,
        username=cfg.get("username") or None,
        password=cfg.get("password") or None,
        token=cfg.get("token") or None,
    )

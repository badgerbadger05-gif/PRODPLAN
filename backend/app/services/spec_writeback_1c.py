"""Запись исправлений состава спецификаций обратно в 1С (read-modify-write).

ПРИНЦИП БЕЗОПАСНОСТИ: табличную часть `Состав` в 1С OData нельзя патчить построчно —
PATCH заменяет весь массив. PRODPLAN не хранит все поля строки (СпособПополнения,
СкладПоУмолчанию, ЕдиницаИзмерения), поэтому собрать массив из локального состояния
нельзя — он затёр бы эти поля. Решение: читаем текущий `Состав` из 1С, меняем ТОЛЬКО
целевые строки/поля, патчим обратно. Все нетронутые строки сохраняются как есть.

Чистые helper-функции (без I/O) держат ошибкоопасную логику мутации и легко тестируются.
Оркестрация (`SpecWriteback`) по умолчанию dry_run=True: ничего не пишет, возвращает
предпросмотр payload. Guarded-пути после полного fresh-read и полного read-back
владеют изменением заголовка, атомарным batch состава, созданием спецификации и
записью основной спецификации по полному ключу номенклатура + характеристика.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .specification_sync import _norm_component_spec_ref

SOSTAV = "Состав"
SPEC_ENTITY = "Catalog_Спецификации"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
PRODUCTION_KIND_FIELD = "ВидПроизводства_Key"
OPERATIONS_FIELD = "Операции"
DEFAULT_SPEC_ENTITY = "InformationRegister_СпецификацииПоУмолчанию"
MATERIAL_ROW_TYPE = "Материал"
ASSEMBLY_ROW_TYPE = "Сборка"

# Category migration may promote a material row or attach/replace the pin on an
# existing assembly row.  Both result in an explicitly pinned assembly.  Adding
# any other transition requires a separate 1C transport proof.
ALLOWED_COMPONENT_TYPE_TRANSITIONS = frozenset(
    {
        (MATERIAL_ROW_TYPE, ASSEMBLY_ROW_TYPE),
        (ASSEMBLY_ROW_TYPE, ASSEMBLY_ROW_TYPE),
    }
)

# Fields returned by 1C but forbidden in a create payload.  Ref_Key is replaced
# with a caller-preallocated UUID, which makes a retry observable/idempotent.
SPEC_CREATE_READONLY_FIELDS = frozenset(
    {"Code", "DataVersion", "Predefined", "PredefinedDataName"}
)


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


def _require_guid(value: Any, *, field: str, allow_zero: bool = True) -> str:
    raw = str(value or "").strip()
    try:
        normalized = str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError) as exc:
        raise SpecWritebackError(f"{field}: некорректный GUID {value!r}") from exc
    if not allow_zero and normalized == ZERO_GUID:
        raise SpecWritebackError(f"{field}: нулевой GUID недопустим")
    return normalized


def specification_before_hash(record: Mapping[str, Any]) -> str:
    """Stable SHA-256 of the complete 1C before-image, including DataVersion."""
    encoded = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _line_number_as_int(value: Any, *, field: str) -> int:
    raw = str(value if value is not None else "").strip()
    if not raw or not raw.isdecimal():
        raise SpecWritebackError(f"{field}: некорректный LineNumber {value!r}")
    return int(raw)


def _validate_unique_line_numbers(rows: Sequence[Mapping[str, Any]], *, field: str) -> None:
    numbers = [
        _line_number_as_int(row.get("LineNumber"), field=f"{field}[{index}]")
        for index, row in enumerate(rows)
    ]
    if len(set(numbers)) != len(numbers):
        raise SpecWritebackError(f"{field}: дублирующийся LineNumber")


def _normalize_tabular_array_order(value: Any, *, field: str) -> Any:
    """Sort 1C tabular arrays by numeric LineNumber for comparison only.

    No field or row is removed. Arrays without LineNumber retain their order.
    """
    if isinstance(value, Mapping):
        return {
            key: _normalize_tabular_array_order(child, field=f"{field}.{key}")
            for key, child in value.items()
        }
    if isinstance(value, list):
        normalized = [
            _normalize_tabular_array_order(child, field=f"{field}[{index}]")
            for index, child in enumerate(value)
        ]
        if normalized and all(
            isinstance(row, Mapping) and "LineNumber" in row for row in normalized
        ):
            _validate_unique_line_numbers(normalized, field=field)
            return sorted(
                normalized,
                key=lambda row: _line_number_as_int(
                    row.get("LineNumber"), field=f"{field}.LineNumber"
                ),
            )
        return normalized
    return copy.deepcopy(value)


def _without_data_version(record: Mapping[str, Any]) -> Dict[str, Any]:
    comparable = copy.deepcopy(dict(record))
    comparable.pop("DataVersion", None)
    return _normalize_tabular_array_order(comparable, field="specification")


def _validate_active_specification(record: Mapping[str, Any], *, op: str, spec_ref: str) -> None:
    if _norm_key(record.get("Ref_Key")) != _norm_key(spec_ref):
        raise SpecWritebackError(
            f"{op}: drift Ref_Key: ожидалось {spec_ref!r}, получено {record.get('Ref_Key')!r}"
        )
    for field in ("DeletionMark", "Недействителен", "ЭтоШаблон"):
        if bool(record.get(field)):
            raise SpecWritebackError(
                f"{op}: спецификация недопустима для записи: {field}=true (spec={spec_ref})"
            )


@dataclass(frozen=True)
class MaterialAssemblyRowChange:
    """Exact identity and before/after state of one composition row.

    Sanctioned transitions are ``Материал -> Сборка`` and the pin-only
    ``Сборка -> Сборка``.  The row identity includes ``LineNumber``,
    nomenclature and characteristic, so non-zero characteristics cannot be
    accidentally collapsed into the zero one.
    """

    line_number: str
    nomenclature_key: str
    characteristic_key: str
    old_row_type: str
    old_specification_key: str
    new_specification_key: str


def _coerce_material_assembly_change(
    value: MaterialAssemblyRowChange | Mapping[str, Any],
    *,
    index: int,
) -> MaterialAssemblyRowChange:
    if isinstance(value, MaterialAssemblyRowChange):
        change = value
    elif isinstance(value, Mapping):
        required = {
            "line_number",
            "nomenclature_key",
            "characteristic_key",
            "old_row_type",
            "old_specification_key",
            "new_specification_key",
        }
        missing = sorted(required.difference(value))
        if missing:
            raise SpecWritebackError(
                f"composition_batch: change[{index}] не содержит: {', '.join(missing)}"
            )
        change = MaterialAssemblyRowChange(
            line_number=str(value["line_number"]),
            nomenclature_key=str(value["nomenclature_key"]),
            characteristic_key=str(value["characteristic_key"]),
            old_row_type=str(value["old_row_type"]),
            old_specification_key=str(value["old_specification_key"] or ZERO_GUID),
            new_specification_key=str(value["new_specification_key"]),
        )
    else:
        raise SpecWritebackError(
            f"composition_batch: change[{index}] должен быть объектом строки"
        )

    line_number = str(change.line_number or "").strip()
    if not line_number:
        raise SpecWritebackError(f"composition_batch: change[{index}] пустой LineNumber")
    item_ref = _require_guid(
        change.nomenclature_key,
        field=f"composition_batch.change[{index}].nomenclature_key",
        allow_zero=False,
    )
    characteristic_ref = _require_guid(
        change.characteristic_key,
        field=f"composition_batch.change[{index}].characteristic_key",
    )
    old_spec_ref = _require_guid(
        change.old_specification_key or ZERO_GUID,
        field=f"composition_batch.change[{index}].old_specification_key",
    )
    target_ref = _require_guid(
        change.new_specification_key,
        field=f"composition_batch.change[{index}].new_specification_key",
        allow_zero=False,
    )
    transition = (str(change.old_row_type or "").strip(), ASSEMBLY_ROW_TYPE)
    if transition not in ALLOWED_COMPONENT_TYPE_TRANSITIONS:
        raise SpecWritebackError(
            f"composition_batch: недопустимый переход типа строки {transition!r}; "
            f"разрешены {MATERIAL_ROW_TYPE!r} -> {ASSEMBLY_ROW_TYPE!r} и "
            f"{ASSEMBLY_ROW_TYPE!r} -> {ASSEMBLY_ROW_TYPE!r}"
        )
    return MaterialAssemblyRowChange(
        line_number=line_number,
        nomenclature_key=item_ref,
        characteristic_key=characteristic_ref,
        old_row_type=str(change.old_row_type or "").strip(),
        old_specification_key=old_spec_ref,
        new_specification_key=target_ref,
    )


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


def set_quantity_on_rows(
    rows: List[Dict[str, Any]],
    *,
    nomenclature_key: str,
    child_spec_key: Optional[str],
    quantity: Any,
) -> Tuple[List[Dict[str, Any]], int]:
    """set_quantity: на совпавших строках меняет только Количество, остальное не трогает."""
    changed = 0
    out: List[Dict[str, Any]] = []
    for row in rows:
        r = copy.deepcopy(row)
        if _row_matches(r, nomenclature_key, child_spec_key):
            r["Количество"] = quantity
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

    def read_specification(self, spec_ref: str) -> Dict[str, Any]:
        """Read the complete current 1C object used by guarded header writes."""
        records = self.client.get_all(
            SPEC_ENTITY,
            filter_query=f"Ref_Key eq guid'{spec_ref}'",
            select_fields=None,
        )
        if len(records) != 1:
            raise SpecWritebackError(
                f"Ожидалась ровно 1 спецификация в 1С: {spec_ref}; найдено {len(records)}"
            )
        return copy.deepcopy(records[0])

    def patch_sostav(self, spec_ref: str, new_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        payload = {SOSTAV: renumber(new_rows)}
        if self.dry_run:
            return {"dry_run": True, "spec_ref": spec_ref, "would_patch": payload}
        endpoint = f"{SPEC_ENTITY}(guid'{spec_ref}')"
        resp = self.client.patch(endpoint, payload)
        return {"dry_run": False, "spec_ref": spec_ref, "response": resp}

    def patch_production_kind(self, spec_ref: str, target_kind_ref: str) -> Dict[str, Any]:
        """PATCH exactly the sanctioned specification header field."""
        payload = {PRODUCTION_KIND_FIELD: target_kind_ref}
        if self.dry_run:
            return {"dry_run": True, "spec_ref": spec_ref, "would_patch": payload}
        endpoint = f"{SPEC_ENTITY}(guid'{spec_ref}')"
        response = self.client.patch(endpoint, payload)
        return {
            "dry_run": False,
            "spec_ref": spec_ref,
            "endpoint": endpoint,
            "payload": payload,
            "response": response,
        }

    def apply_to_sostav(self, spec_ref: str, mutate: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]) -> Dict[str, Any]:
        rows = self.read_sostav(spec_ref)
        new_rows = mutate(rows)
        return self.patch_sostav(spec_ref, new_rows)


def _validate_kind_change_source(
    record: Dict[str, Any],
    *,
    spec_ref: str,
    expected_owner_ref: str,
    expected_code: str,
    old_kind_ref: str,
    target_kind_ref: str,
    expected_operation_ref: str,
    expected_data_version: Optional[str],
) -> str:
    """Validate the fresh full object and return ``source`` or ``already_applied``."""
    checks = (
        ("Ref_Key", spec_ref),
        ("Owner_Key", expected_owner_ref),
        ("Code", expected_code),
    )
    for field, expected in checks:
        if _norm_key(record.get(field)) != _norm_key(expected):
            raise SpecWritebackError(
                f"kind_change: drift {field}: ожидалось {expected!r}, "
                f"получено {record.get(field)!r} (spec={spec_ref})"
            )

    for field in ("DeletionMark", "Недействителен", "ЭтоШаблон"):
        if bool(record.get(field)):
            raise SpecWritebackError(
                f"kind_change: спецификация недопустима для записи: {field}=true (spec={spec_ref})"
            )

    operations = record.get(OPERATIONS_FIELD)
    if not isinstance(operations, list) or len(operations) != 1:
        count = len(operations) if isinstance(operations, list) else "not_a_list"
        raise SpecWritebackError(
            f"kind_change: ожидалась ровно 1 операция, получено {count} (spec={spec_ref})"
        )
    actual_operation_ref = operations[0].get("Операция_Key")
    if _norm_key(actual_operation_ref) != _norm_key(expected_operation_ref):
        raise SpecWritebackError(
            f"kind_change: drift Операция_Key: ожидалось {expected_operation_ref!r}, "
            f"получено {actual_operation_ref!r} (spec={spec_ref})"
        )

    actual_kind = _norm_key(record.get(PRODUCTION_KIND_FIELD))
    if actual_kind == _norm_key(target_kind_ref):
        return "already_applied"
    if actual_kind != _norm_key(old_kind_ref):
        raise SpecWritebackError(
            f"kind_change: drift {PRODUCTION_KIND_FIELD}: ожидался исходный "
            f"{old_kind_ref!r} или целевой {target_kind_ref!r}, "
            f"получено {record.get(PRODUCTION_KIND_FIELD)!r} (spec={spec_ref})"
        )
    if expected_data_version is not None and str(record.get("DataVersion") or "") != str(expected_data_version):
        raise SpecWritebackError(
            f"kind_change: drift DataVersion: ожидалось {expected_data_version!r}, "
            f"получено {record.get('DataVersion')!r} (spec={spec_ref})"
        )
    return "source"


def _without_kind_write_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    """Comparable full object excluding the intended field and 1C revision marker."""
    comparable = copy.deepcopy(record)
    comparable.pop(PRODUCTION_KIND_FIELD, None)
    comparable.pop("DataVersion", None)
    return comparable


def writeback_change_production_kind(
    client: Any,
    *,
    spec_ref: str,
    expected_owner_ref: str,
    expected_code: str,
    old_kind_ref: str,
    target_kind_ref: str,
    expected_operation_ref: str,
    expected_data_version: Optional[str] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Guarded, resumable header-only production-kind change.

    The full 1C object is read immediately before the write.  Only
    ``ВидПроизводства_Key`` is PATCHed.  A real write is accepted only when a second
    full read shows the target kind and every other field is byte-for-byte
    equivalent apart from 1C's ``DataVersion`` marker.
    """
    def _run() -> Dict[str, Any]:
        required = {
            "spec_ref": spec_ref,
            "expected_owner_ref": expected_owner_ref,
            "expected_code": expected_code,
            "old_kind_ref": old_kind_ref,
            "target_kind_ref": target_kind_ref,
            "expected_operation_ref": expected_operation_ref,
        }
        empty = [name for name, value in required.items() if not str(value or "").strip()]
        if empty:
            raise SpecWritebackError(
                f"kind_change: пустые обязательные поля: {', '.join(empty)}"
            )
        if _norm_key(old_kind_ref) == _norm_key(target_kind_ref):
            raise SpecWritebackError("kind_change: исходный и целевой виды производства совпадают")

        sb = SpecWriteback(client, dry_run=dry_run)
        before = sb.read_specification(spec_ref)
        state = _validate_kind_change_source(
            before,
            spec_ref=spec_ref,
            expected_owner_ref=expected_owner_ref,
            expected_code=expected_code,
            old_kind_ref=old_kind_ref,
            target_kind_ref=target_kind_ref,
            expected_operation_ref=expected_operation_ref,
            expected_data_version=expected_data_version,
        )
        common = {
            "op": "change_production_kind",
            "spec_ref": str(before.get("Ref_Key") or spec_ref),
            "spec_code": str(before.get("Code") or ""),
            "owner_ref": str(before.get("Owner_Key") or ""),
            "old_kind_ref": old_kind_ref,
            "target_kind_ref": target_kind_ref,
            "operation_ref": expected_operation_ref,
            "before_data_version": before.get("DataVersion"),
        }
        if state == "already_applied":
            return {**common, "status": "already_applied", "dry_run": bool(dry_run)}

        payload = {PRODUCTION_KIND_FIELD: target_kind_ref}
        if dry_run:
            return {
                **common,
                "status": "dry_run",
                "dry_run": True,
                "would_patch": payload,
            }

        patch_result = sb.patch_production_kind(spec_ref, target_kind_ref)
        after = sb.read_specification(spec_ref)
        if _norm_key(after.get(PRODUCTION_KIND_FIELD)) != _norm_key(target_kind_ref):
            raise SpecWritebackError(
                f"kind_change: read-back не подтвердил {PRODUCTION_KIND_FIELD}={target_kind_ref!r} "
                f"(spec={spec_ref})"
            )
        if _without_kind_write_fields(after) != _without_kind_write_fields(before):
            raise SpecWritebackError(
                f"kind_change: read-back обнаружил изменение полей кроме "
                f"{PRODUCTION_KIND_FIELD} и DataVersion (spec={spec_ref})"
            )
        return {
            **common,
            "status": "updated",
            "dry_run": False,
            "after_data_version": after.get("DataVersion"),
            "patch": patch_result,
        }

    return _guard("kind_change", _run)


def writeback_promote_material_rows(
    client: Any,
    *,
    parent_spec_ref: str,
    row_changes: Sequence[MaterialAssemblyRowChange | Mapping[str, Any]],
    expected_before: Optional[Mapping[str, Any]] = None,
    expected_before_hash: Optional[str] = None,
    expected_data_version: Optional[str] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Atomically make exact parent rows explicitly pinned assemblies.

    One call owns one parent specification and emits at most one PATCH containing
    the complete fresh ``Состав`` array.  Unknown row fields and every existing
    ``LineNumber`` are copied byte-for-byte.  Each target specification is read
    once and must be active and owned by the row nomenclature.  Direct cycles
    (the parent itself or another specification of the parent's owner) are
    rejected.

    A real source-state write requires at least one complete before-image proof:
    the full object, its SHA-256, or its DataVersion.  Rows already in the exact
    target state are resumable and do not require the now-stale source proof.
    """

    def _run_promote_material_rows() -> Dict[str, Any]:
        normalized_parent_ref = _require_guid(
            parent_spec_ref, field="composition_batch.parent_spec_ref", allow_zero=False
        )
        if not row_changes:
            raise SpecWritebackError("composition_batch: пустой список row_changes")
        changes = [
            _coerce_material_assembly_change(value, index=index)
            for index, value in enumerate(row_changes)
        ]
        identities = [
            (
                change.line_number,
                _norm_key(change.nomenclature_key),
                _norm_key(change.characteristic_key),
            )
            for change in changes
        ]
        if len(set(identities)) != len(identities):
            raise SpecWritebackError("composition_batch: повторная identity целевой строки")

        sb = SpecWriteback(client, dry_run=dry_run)
        before = sb.read_specification(normalized_parent_ref)
        _validate_active_specification(
            before, op="composition_batch", spec_ref=normalized_parent_ref
        )
        parent_owner_ref = _require_guid(
            before.get("Owner_Key"),
            field="composition_batch.parent.Owner_Key",
            allow_zero=False,
        )
        rows = before.get(SOSTAV)
        if not isinstance(rows, list):
            raise SpecWritebackError("composition_batch: fresh Состав не является массивом")
        if not all(isinstance(row, Mapping) for row in rows):
            raise SpecWritebackError("composition_batch: fresh Состав содержит не-объект")
        _validate_unique_line_numbers(rows, field="composition_batch.Состав")

        new_rows = copy.deepcopy(rows)
        source_indexes: List[int] = []
        row_states: List[str] = []
        for change in changes:
            matches = [
                index
                for index, row in enumerate(rows)
                if str(row.get("LineNumber") or "").strip() == change.line_number
                and _norm_key(row.get("Номенклатура_Key"))
                == _norm_key(change.nomenclature_key)
                and _norm_key(row.get("Характеристика_Key"))
                == _norm_key(change.characteristic_key)
            ]
            if len(matches) != 1:
                raise SpecWritebackError(
                    "composition_batch: ожидалась ровно одна строка по "
                    f"LineNumber/item/characteristic, найдено {len(matches)} "
                    f"(line={change.line_number}, item={change.nomenclature_key}, "
                    f"characteristic={change.characteristic_key})"
                )
            row_index = matches[0]
            row = rows[row_index]
            actual_type = str(row.get("ТипСтрокиСостава") or "").strip()
            actual_pin = _require_guid(
                row.get("Спецификация_Key") or ZERO_GUID,
                field=f"composition_batch.Состав[{change.line_number}].Спецификация_Key",
            )
            if (
                actual_type == ASSEMBLY_ROW_TYPE
                and _norm_key(actual_pin) == _norm_key(change.new_specification_key)
            ):
                row_states.append("already_applied")
                continue
            if (
                actual_type != change.old_row_type
                or _norm_key(actual_pin) != _norm_key(change.old_specification_key)
            ):
                raise SpecWritebackError(
                    "composition_batch: drift строки "
                    f"{change.line_number}: ожидались type={change.old_row_type!r}, "
                    f"pin={change.old_specification_key!r} либо уже применённые "
                    f"type={ASSEMBLY_ROW_TYPE!r}, pin={change.new_specification_key!r}; "
                    f"получено type={actual_type!r}, pin={actual_pin!r}"
                )
            row_states.append("source")
            source_indexes.append(row_index)
            new_rows[row_index]["ТипСтрокиСостава"] = ASSEMBLY_ROW_TYPE
            new_rows[row_index]["Спецификация_Key"] = change.new_specification_key

        # Validate each distinct child exactly once, even for already-applied rows.
        child_records: Dict[str, Dict[str, Any]] = {}
        for change in changes:
            target_norm = _norm_key(change.new_specification_key)
            child = child_records.get(target_norm)
            if child is None:
                if target_norm == _norm_key(normalized_parent_ref):
                    raise SpecWritebackError(
                        "composition_batch: циклическая ссылка на родительскую спецификацию"
                    )
                child = sb.read_specification(change.new_specification_key)
                _validate_active_specification(
                    child,
                    op="composition_batch.target",
                    spec_ref=change.new_specification_key,
                )
                child_records[target_norm] = child
            child_owner = _require_guid(
                child.get("Owner_Key"),
                field=f"composition_batch.target[{change.new_specification_key}].Owner_Key",
                allow_zero=False,
            )
            if _norm_key(child_owner) != _norm_key(change.nomenclature_key):
                raise SpecWritebackError(
                    "composition_batch: target specification owner mismatch: "
                    f"target={change.new_specification_key}, owner={child_owner}, "
                    f"row_item={change.nomenclature_key}"
                )
            child_characteristic = _require_guid(
                child.get("ХарактеристикаПродукции_Key") or ZERO_GUID,
                field=(
                    "composition_batch.target"
                    f"[{change.new_specification_key}].ХарактеристикаПродукции_Key"
                ),
            )
            if _norm_key(child_characteristic) != _norm_key(change.characteristic_key):
                raise SpecWritebackError(
                    "composition_batch: target specification characteristic mismatch: "
                    f"target={change.new_specification_key}, "
                    f"target_characteristic={child_characteristic}, "
                    f"row_characteristic={change.characteristic_key}"
                )
            if _norm_key(child_owner) == _norm_key(parent_owner_ref):
                raise SpecWritebackError(
                    "composition_batch: запрещён прямой цикл через спецификацию "
                    f"того же owner={parent_owner_ref}"
                )

        common = {
            "op": "promote_material_rows",
            "parent_spec_ref": normalized_parent_ref,
            "before_data_version": before.get("DataVersion"),
            "row_count": len(changes),
            "source_count": row_states.count("source"),
            "already_applied_count": row_states.count("already_applied"),
            "validated_target_count": len(child_records),
        }
        if not source_indexes:
            return {**common, "status": "already_applied", "dry_run": bool(dry_run)}

        if expected_before is None and not expected_before_hash and expected_data_version is None:
            raise SpecWritebackError(
                "composition_batch: требуется expected_before, expected_before_hash "
                "или expected_data_version"
            )
        if expected_before is not None and dict(expected_before) != before:
            raise SpecWritebackError("composition_batch: fresh object отличается от expected_before")
        if expected_before_hash:
            actual_hash = specification_before_hash(before)
            if actual_hash.lower() != str(expected_before_hash).strip().lower():
                raise SpecWritebackError(
                    "composition_batch: fresh object hash отличается от expected_before_hash"
                )
        if expected_data_version is not None and str(before.get("DataVersion") or "") != str(
            expected_data_version
        ):
            raise SpecWritebackError(
                "composition_batch: drift DataVersion: "
                f"ожидалось {expected_data_version!r}, получено {before.get('DataVersion')!r}"
            )

        payload = {SOSTAV: new_rows}
        if dry_run:
            return {
                **common,
                "status": "dry_run",
                "dry_run": True,
                "would_patch": payload,
            }

        endpoint = f"{SPEC_ENTITY}(guid'{normalized_parent_ref}')"
        response = client.patch(endpoint, payload)
        after = sb.read_specification(normalized_parent_ref)
        expected_after = copy.deepcopy(before)
        expected_after[SOSTAV] = new_rows
        if _without_data_version(after) != _without_data_version(expected_after):
            raise SpecWritebackError(
                "composition_batch: read-back изменил объект вне намеренных "
                "ТипСтрокиСостава/Спецификация_Key либо не подтвердил payload"
            )
        return {
            **common,
            "status": "updated",
            "dry_run": False,
            "after_data_version": after.get("DataVersion"),
            "endpoint": endpoint,
            "payload": payload,
            "response": response,
        }

    return _guard("composition_batch", _run_promote_material_rows)


def _strip_spec_create_transport_fields(template: Mapping[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key, value in template.items():
        key_text = str(key)
        if (
            key_text in SPEC_CREATE_READONLY_FIELDS
            or key_text == "odata.metadata"
            or "@" in key_text
        ):
            continue
        payload[key_text] = copy.deepcopy(value)
    return payload


def build_specification_create_payload(
    template: Mapping[str, Any],
    *,
    new_spec_ref: str,
    new_owner_ref: str,
    material_ref: str,
    material_characteristic_ref: str,
    material_quantity: Any,
    operation_ref: str,
    operation_time: Any,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a lossless, transport-safe one-material/one-operation clone payload.

    The caller supplies the complete freshly read template and explicitly
    approves the new owner, material, quantity, operation and time.  The helper
    does not infer business values.  A preallocated UUID is assigned to the
    header and every tabular row; server-owned identity/version fields are
    removed before transport.
    """
    spec_ref = _require_guid(new_spec_ref, field="spec_create.new_spec_ref", allow_zero=False)
    owner_ref = _require_guid(new_owner_ref, field="spec_create.new_owner_ref", allow_zero=False)
    material_key = _require_guid(material_ref, field="spec_create.material_ref", allow_zero=False)
    characteristic_key = _require_guid(
        material_characteristic_ref,
        field="spec_create.material_characteristic_ref",
    )
    operation_key = _require_guid(
        operation_ref, field="spec_create.operation_ref", allow_zero=False
    )
    if _norm_key(owner_ref) == _norm_key(material_key):
        raise SpecWritebackError("spec_create: запрещён прямой цикл owner -> material")
    try:
        material_quantity_float = float(material_quantity)
        operation_time_float = float(operation_time)
        if (
            not math.isfinite(material_quantity_float)
            or not math.isfinite(operation_time_float)
            or material_quantity_float <= 0
            or operation_time_float <= 0
        ):
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise SpecWritebackError(
            "spec_create: material_quantity и operation_time должны быть положительными"
        ) from exc

    payload = _strip_spec_create_transport_fields(template)
    for field in ("DeletionMark", "Недействителен", "ЭтоШаблон"):
        if bool(payload.get(field)):
            raise SpecWritebackError(f"spec_create: template {field}=true")
    composition = payload.get(SOSTAV)
    operations = payload.get(OPERATIONS_FIELD)
    if not isinstance(composition, list) or len(composition) != 1:
        raise SpecWritebackError("spec_create: template должен содержать ровно 1 строку Состав")
    if not isinstance(operations, list) or len(operations) != 1:
        raise SpecWritebackError("spec_create: template должен содержать ровно 1 строку Операции")
    if str(composition[0].get("ТипСтрокиСостава") or "").strip() != MATERIAL_ROW_TYPE:
        raise SpecWritebackError(
            f"spec_create: разрешён только template row type {MATERIAL_ROW_TYPE!r}"
        )
    production_kind_ref = _require_guid(
        payload.get(PRODUCTION_KIND_FIELD),
        field=f"spec_create.template.{PRODUCTION_KIND_FIELD}",
        allow_zero=False,
    )

    payload["Ref_Key"] = spec_ref
    payload["Owner_Key"] = owner_ref
    payload[PRODUCTION_KIND_FIELD] = production_kind_ref
    if description is not None:
        payload["Description"] = str(description)
    material_row = payload[SOSTAV][0]
    material_row.update(
        {
            "Ref_Key": spec_ref,
            "ТипСтрокиСостава": MATERIAL_ROW_TYPE,
            "Номенклатура_Key": material_key,
            "Характеристика_Key": characteristic_key,
            "Спецификация_Key": ZERO_GUID,
            "Количество": material_quantity,
        }
    )
    operation_row = payload[OPERATIONS_FIELD][0]
    operation_row.update(
        {
            "Ref_Key": spec_ref,
            "Операция_Key": operation_key,
            "НормаВремени": operation_time,
        }
    )
    for value in payload.values():
        if isinstance(value, list):
            for row in value:
                if isinstance(row, dict):
                    row["Ref_Key"] = spec_ref
    return payload


def _spec_create_business_projection(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Comparable business data; tabular Ref_Key is transport identity only."""
    projected = _strip_spec_create_transport_fields(record)
    for value in projected.values():
        if isinstance(value, list):
            for row in value:
                if isinstance(row, dict):
                    row.pop("Ref_Key", None)
                    for key in list(row):
                        if "@" in str(key):
                            row.pop(key, None)
    return projected


def _verify_created_specification(
    actual: Mapping[str, Any],
    *,
    expected_payload: Mapping[str, Any],
) -> None:
    expected = _spec_create_business_projection(expected_payload)
    actual_projection = _spec_create_business_projection(actual)
    missing_or_changed = {
        field: {"expected": expected_value, "actual": actual_projection.get(field)}
        for field, expected_value in expected.items()
        if actual_projection.get(field) != expected_value
    }
    if missing_or_changed:
        raise SpecWritebackError(
            "spec_create: read-back не подтвердил все business fields: "
            + ", ".join(sorted(missing_or_changed))
        )


def writeback_create_specification(
    client: Any,
    *,
    template: Mapping[str, Any],
    new_spec_ref: str,
    new_owner_ref: str,
    material_ref: str,
    material_characteristic_ref: str,
    material_quantity: Any,
    operation_ref: str,
    operation_time: Any,
    description: Optional[str] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Create a specification via canonical ``client.post`` with UUID idempotence."""

    def _run_create_specification() -> Dict[str, Any]:
        payload = build_specification_create_payload(
            template,
            new_spec_ref=new_spec_ref,
            new_owner_ref=new_owner_ref,
            material_ref=material_ref,
            material_characteristic_ref=material_characteristic_ref,
            material_quantity=material_quantity,
            operation_ref=operation_ref,
            operation_time=operation_time,
            description=description,
        )
        spec_ref = payload["Ref_Key"]
        sb = SpecWriteback(client, dry_run=dry_run)
        existing = client.get_all(
            SPEC_ENTITY,
            filter_query=f"Ref_Key eq guid'{spec_ref}'",
            select_fields=None,
        )
        if len(existing) > 1:
            raise SpecWritebackError(
                f"spec_create: preallocated Ref_Key {spec_ref} вернул {len(existing)} объектов"
            )
        if existing:
            _verify_created_specification(existing[0], expected_payload=payload)
            return {
                "op": "create_specification",
                "status": "already_applied",
                "dry_run": bool(dry_run),
                "spec_ref": spec_ref,
            }
        if dry_run:
            return {
                "op": "create_specification",
                "status": "dry_run",
                "dry_run": True,
                "spec_ref": spec_ref,
                "would_post": payload,
            }
        response = client.post(SPEC_ENTITY, payload)
        after = sb.read_specification(spec_ref)
        _verify_created_specification(after, expected_payload=payload)
        return {
            "op": "create_specification",
            "status": "created",
            "dry_run": False,
            "spec_ref": spec_ref,
            "payload": payload,
            "response": response,
            "after_data_version": after.get("DataVersion"),
        }

    return _guard("spec_create", _run_create_specification)


def _read_default_specification_record(
    client: Any,
    *,
    nomenclature_key: str,
    characteristic_key: str,
) -> Optional[Dict[str, Any]]:
    # Demo 1C rejects an AND comparison on Характеристика_Key.  Query the exact
    # owner, then enforce the full composite key locally.
    records = client.get_all(
        DEFAULT_SPEC_ENTITY,
        filter_query=f"Номенклатура_Key eq guid'{nomenclature_key}'",
        select_fields=None,
        order_by=None,
    )
    matches = [
        copy.deepcopy(record)
        for record in records
        if _norm_key(record.get("Номенклатура_Key")) == _norm_key(nomenclature_key)
        and _norm_key(record.get("Характеристика_Key")) == _norm_key(characteristic_key)
    ]
    if len(matches) > 1:
        raise SpecWritebackError(
            "default_spec: неоднозначная запись по composite key "
            f"({nomenclature_key}, {characteristic_key})"
        )
    return matches[0] if matches else None


def writeback_default_specification(
    client: Any,
    *,
    nomenclature_key: str,
    characteristic_key: str,
    target_spec_ref: str,
    expected_old_spec_ref: Optional[str] = None,
    expect_absent: bool = False,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Guarded default-register upsert for its full composite business key."""

    def _run_default_specification() -> Dict[str, Any]:
        item_ref = _require_guid(
            nomenclature_key, field="default_spec.nomenclature_key", allow_zero=False
        )
        characteristic_ref = _require_guid(
            characteristic_key, field="default_spec.characteristic_key"
        )
        target_ref = _require_guid(
            target_spec_ref, field="default_spec.target_spec_ref", allow_zero=False
        )
        if expect_absent == (expected_old_spec_ref is not None):
            raise SpecWritebackError(
                "default_spec: укажите ровно одно из expected_old_spec_ref или expect_absent=true"
            )
        expected_old = None
        if expected_old_spec_ref is not None:
            expected_old = _require_guid(
                expected_old_spec_ref,
                field="default_spec.expected_old_spec_ref",
                allow_zero=False,
            )

        sb = SpecWriteback(client, dry_run=dry_run)
        target = sb.read_specification(target_ref)
        _validate_active_specification(target, op="default_spec.target", spec_ref=target_ref)
        if _norm_key(target.get("Owner_Key")) != _norm_key(item_ref):
            raise SpecWritebackError(
                "default_spec: target specification owner mismatch: "
                f"target_owner={target.get('Owner_Key')!r}, item={item_ref!r}"
            )
        target_characteristic = _require_guid(
            target.get("ХарактеристикаПродукции_Key") or ZERO_GUID,
            field="default_spec.target.ХарактеристикаПродукции_Key",
        )
        if _norm_key(target_characteristic) != _norm_key(characteristic_ref):
            raise SpecWritebackError(
                "default_spec: target specification characteristic mismatch: "
                f"target_characteristic={target_characteristic!r}, "
                f"composite_characteristic={characteristic_ref!r}"
            )

        before = _read_default_specification_record(
            client,
            nomenclature_key=item_ref,
            characteristic_key=characteristic_ref,
        )
        if before is not None and _norm_key(before.get("Спецификация_Key")) == _norm_key(target_ref):
            return {
                "op": "write_default_specification",
                "status": "already_applied",
                "dry_run": bool(dry_run),
                "nomenclature_key": item_ref,
                "characteristic_key": characteristic_ref,
                "target_spec_ref": target_ref,
            }
        if expect_absent and before is not None:
            raise SpecWritebackError("default_spec: ожидалось отсутствие записи, но она существует")
        if not expect_absent:
            if before is None:
                raise SpecWritebackError("default_spec: ожидаемая старая запись отсутствует")
            if _norm_key(before.get("Спецификация_Key")) != _norm_key(expected_old):
                raise SpecWritebackError(
                    "default_spec: drift Спецификация_Key: "
                    f"ожидалось {expected_old!r}, получено {before.get('Спецификация_Key')!r}"
                )

        full_payload = {
            "Номенклатура_Key": item_ref,
            "Характеристика_Key": characteristic_ref,
            "Спецификация_Key": target_ref,
        }
        if expect_absent:
            endpoint = DEFAULT_SPEC_ENTITY
            write_payload = full_payload
            method = "post"
        else:
            endpoint = (
                f"{DEFAULT_SPEC_ENTITY}(Номенклатура_Key=guid'{item_ref}',"
                f"Характеристика_Key=guid'{characteristic_ref}')"
            )
            write_payload = {"Спецификация_Key": target_ref}
            method = "patch"
        if dry_run:
            return {
                "op": "write_default_specification",
                "status": "dry_run",
                "dry_run": True,
                "method": method,
                "endpoint": endpoint,
                "would_write": write_payload,
            }

        if method == "post":
            response = client.post(endpoint, write_payload)
        else:
            response = client.patch(endpoint, write_payload)
        after = _read_default_specification_record(
            client,
            nomenclature_key=item_ref,
            characteristic_key=characteristic_ref,
        )
        if after is None:
            raise SpecWritebackError("default_spec: read-back не нашёл composite key")
        actual_business = {
            "Номенклатура_Key": after.get("Номенклатура_Key"),
            "Характеристика_Key": after.get("Характеристика_Key"),
            "Спецификация_Key": after.get("Спецификация_Key"),
        }
        if {
            key: _norm_key(value) for key, value in actual_business.items()
        } != {key: _norm_key(value) for key, value in full_payload.items()}:
            raise SpecWritebackError("default_spec: read-back не подтвердил composite record")
        if before is not None:
            expected_after = copy.deepcopy(before)
            expected_after["Спецификация_Key"] = target_ref
            comparable_after = copy.deepcopy(after)
            for key in list(comparable_after):
                if "@" in str(key) or key == "odata.metadata":
                    comparable_after.pop(key, None)
            comparable_expected = copy.deepcopy(expected_after)
            for key in list(comparable_expected):
                if "@" in str(key) or key == "odata.metadata":
                    comparable_expected.pop(key, None)
            if comparable_after != comparable_expected:
                raise SpecWritebackError(
                    "default_spec: read-back изменил поля кроме Спецификация_Key"
                )
        return {
            "op": "write_default_specification",
            "status": "updated" if before is not None else "created",
            "dry_run": False,
            "method": method,
            "endpoint": endpoint,
            "payload": write_payload,
            "response": response,
        }

    return _guard("default_spec", _run_default_specification)


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


def writeback_set_quantity(
    client: Any,
    *,
    spec_ref: str,
    nomenclature_key: str,
    child_spec_key: Optional[str],
    quantity: Any,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """set_quantity в 1С: на совпавшей строке состава меняем только Количество."""
    def _run():
        sb = SpecWriteback(client, dry_run=dry_run)
        rows = sb.read_sostav(spec_ref)
        new_rows, changed = set_quantity_on_rows(
            rows, nomenclature_key=nomenclature_key, child_spec_key=child_spec_key, quantity=quantity
        )
        if changed != 1:
            raise SpecWritebackError(
                f"set_quantity: в 1С ожидалась ровно 1 совпавшая строка, найдено {changed} "
                f"(spec={spec_ref}, ном={nomenclature_key})"
            )
        res = sb.patch_sostav(spec_ref, new_rows)
        return {"op": "set_quantity", "changed": changed, "rows": len(new_rows), "patch": res}

    return _guard("set_quantity", _run)


def writeback_remove(
    client: Any,
    *,
    spec_ref: str,
    nomenclature_key: str,
    child_spec_key: Optional[str],
    dry_run: bool = True,
) -> Dict[str, Any]:
    """remove в 1С: POP совпавшей строки из состава и PATCH оставшихся."""
    def _run():
        sb = SpecWriteback(client, dry_run=dry_run)
        rows = sb.read_sostav(spec_ref)
        remaining, popped = pop_row(rows, nomenclature_key=nomenclature_key, child_spec_key=child_spec_key)
        if popped is None:
            raise SpecWritebackError(
                f"remove: строка не найдена в спеке 1С (spec={spec_ref}, ном={nomenclature_key})"
            )
        res = sb.patch_sostav(spec_ref, remaining)
        return {"op": "remove", "removed": 1, "rows": len(remaining), "patch": res}

    return _guard("remove", _run)


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

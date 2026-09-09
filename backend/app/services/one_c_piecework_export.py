"""Export ProductionManufacture records to 1C as Document_СдельныйНаряд.

Pattern: mirrors one_c_manufacture_export.py.
Documentation: .docs/piecework_order_odata.md.

Safety per the doc:
1. Default dry_run=True (full preview, nothing is written).
2. Create as not posted, then close and conduct through standard 1C Post operation.
3. Idempotency via sync_link (source_doctype='piecework').

Basis rule (from piecework_order_odata.md):
  Document_СдельныйНаряд.ДокументОснование = manufacture.exported_ref1c
  Document_СдельныйНаряд.ДокументОснование_Type = StandardODATA.Document_СборкаЗапасов

The manufacture must already be exported to 1C (exported_ref1c set) before a
piecework order can reference it as its basis.

Норма времени and расценка are taken from the product specification operations.
operation_ref is still accepted as a manual single-operation override.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field, replace
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from ..models import (
    ProductionManufacture,
    ProductionManufactureOperation,
    ProductionProduct,
    Employee,
    Operation,
    ProductionStage,
    Specification,
    SpecOperation,
    SyncLink,
)
from .workshop_resolution import (
    resolve_workshop_for_product,
    warehouse_binding_for_workshop,
)
from .one_c_export_common import (
    DEFAULT_ORGANIZATION_REF1C,
    DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C,
    EMPTY_REF1C,
    add_unit_payload as _add_unit_payload,
    clean_ref1c as _clean_ref1c,
    config_ref1c as _config_ref1c,
    create_odata_client as _create_odata_client,
    current_1c_datetime as _current_1c_datetime,
    find_sync_link as _find_sync_link,
    post_document_operational as _post_document_operational,
    post_export_entries as _post_export_entries,
    upsert_sync_link as _upsert_sync_link,
)
from .odata_config import load_odata_config as _load_odata_config
from .odata_client import OData1CClient
from .one_c_document_numbers import piecework_number
from .one_c_manufacture_export import (
    export_manufactures_to_1c,
)
from .bom_specification_resolver import BomSpecificationResolver


PIECEWORK_ENTITY = "Document_СдельныйНаряд"
BASIS_TYPE = "StandardODATA.Document_СборкаЗапасов"
ORDER_TYPE = "StandardODATA.Document_ЗаказНаПроизводство"
PIECEWORK_PRICE_REGISTER = "InformationRegister_ЦеныНоменклатуры"
DEFAULT_ACCOUNTING_PRICE_TYPE_REF1C = "81c4a02c-991b-11eb-e39a-fa163e61326a"


@dataclass
class PieceworkOperationLine:
    operation_ref1c: str
    time_norm: float = 0.0
    price: float = 0.0
    stage_ref1c: Optional[str] = None
    spec_operation_id: Optional[int] = None
    operation_id: Optional[int] = None
    employee_ref1c: Optional[str] = None
    employee_type: str = "employee"


@dataclass
class PieceworkExportEntry:
    manufacture_id: int
    product_id: int
    order_id: int
    order_ref1c: Optional[str]
    basis_ref1c: Optional[str]
    item_ref1c: str
    item_name: str
    unit_ref1c: Optional[str]
    qty: float
    number: str
    operation_ref1c: Optional[str] = None
    time_norm: float = 0.0
    price: float = 0.0
    spec_ref1c: Optional[str] = None
    stage_ref1c: Optional[str] = None
    operation_lines: List[PieceworkOperationLine] = field(default_factory=list)
    structural_unit_ref1c: Optional[str] = None
    employee_ref1c: Optional[str] = None
    employee_type: str = "employee"
    characteristic_ref1c: Optional[str] = None
    document_datetime: Optional[str] = None
    target_ref_key: Optional[str] = None
    unpost_before_patch: bool = False
    status: str = "planned"
    error: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class PieceworkOperationDefaults:
    operation_ref1c: Optional[str] = None
    time_norm: float = 0.0
    price: float = 0.0
    spec_ref1c: Optional[str] = None
    stage_ref1c: Optional[str] = None
    operation_lines: Tuple[PieceworkOperationLine, ...] = ()
    structural_unit_ref1c: Optional[str] = None


def _piecework_price_type_ref(config: Dict[str, Any]) -> str:
    return (
        _config_ref1c(config, "piecework_price_type_ref1c")
        or _config_ref1c(config, "default_piecework_price_type_ref1c")
        or _config_ref1c(config, "default_accounting_price_type_ref1c")
        or DEFAULT_ACCOUNTING_PRICE_TYPE_REF1C
    )


def _datetime_literal_1c(value: Optional[str]) -> str:
    text = str(value or "").strip()
    if not text:
        return _current_1c_datetime()
    if text.endswith("Z"):
        text = text[:-1]
    if "+" in text:
        text = text.split("+", 1)[0]
    elif len(text) > 10 and "-" in text[10:]:
        text = text.rsplit("-", 1)[0]
    return text


def _extract_price_register_value(response: Dict[str, Any]) -> Optional[float]:
    rows = response.get("value")
    if isinstance(rows, list):
        candidates = rows
    else:
        candidates = [response]
    for row in candidates:
        if not isinstance(row, dict):
            continue
        if row.get("Актуальность") is False:
            continue
        try:
            price = float(row.get("Цена") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


def _lookup_piecework_operation_price(
    client: OData1CClient,
    *,
    operation_ref: str,
    price_type_ref: str,
    at_datetime: str,
) -> Optional[float]:
    operation_ref = _clean_ref1c(operation_ref)
    price_type_ref = _clean_ref1c(price_type_ref)
    if not operation_ref or not price_type_ref:
        return None
    response = client._make_request(
        f"{PIECEWORK_PRICE_REGISTER}/SliceLast(Period=datetime'{_datetime_literal_1c(at_datetime)}')",
        params={
            "$select": "Period,ВидЦен_Key,Номенклатура_Key,Цена,Актуальность",
            "$filter": (
                f"Номенклатура_Key eq guid'{operation_ref}' "
                f"and ВидЦен_Key eq guid'{price_type_ref}'"
            ),
            "$top": "1",
            "$format": "json",
        },
        timeout=60,
        retries=1,
    )
    return _extract_price_register_value(response)


def _enrich_payload_prices_from_1c(
    client: OData1CClient,
    entry: PieceworkExportEntry,
    payload: Dict[str, Any],
    *,
    price_type_ref: str,
) -> Dict[str, Any]:
    lookups: List[Dict[str, Any]] = []
    cache: Dict[str, Optional[float]] = {}
    at_datetime = str(payload.get("Date") or entry.document_datetime or _current_1c_datetime())
    for row in payload.get("Операции") or []:
        operation_ref = _clean_ref1c(row.get("Операция_Key"))
        if not operation_ref:
            continue
        price: Optional[float]
        error: Optional[str] = None
        if operation_ref in cache:
            price = cache[operation_ref]
        else:
            try:
                price = _lookup_piecework_operation_price(
                    client,
                    operation_ref=operation_ref,
                    price_type_ref=price_type_ref,
                    at_datetime=at_datetime,
                )
            except Exception as exc:
                price = None
                error = str(exc)
            cache[operation_ref] = price
        if price and price > 0:
            row["Расценка"] = price
            row["Стоимость"] = float(row.get("КоличествоФакт") or 0) * price
            source = PIECEWORK_PRICE_REGISTER
        else:
            try:
                existing_price = float(row.get("Расценка") or 0)
            except (TypeError, ValueError):
                existing_price = 0.0
            source = "payload" if existing_price > 0 else "missing"
        lookups.append({
            "operation_ref1c": operation_ref,
            "price_type_ref1c": price_type_ref,
            "price": price,
            "source": source,
            **({"error": error} if error else {}),
        })
    payload["ВидЦен_Key"] = price_type_ref
    return {
        "manufacture_id": entry.manufacture_id,
        "number": entry.number,
        "lookups": lookups,
    }


def _existing_link(db: Session, manufacture_id: int) -> Optional[SyncLink]:
    return _find_sync_link(
        db,
        SyncLink,
        source_doctype="piecework",
        source_id=int(manufacture_id),
        target_entity=PIECEWORK_ENTITY,
    )


def _piecework_spec_id(db: Session, product: Optional[ProductionProduct]) -> Optional[int]:
    if not product:
        return None
    if product.spec_id:
        return int(product.spec_id)
    item_id = getattr(product, "item_id", None)
    if not item_id:
        return None
    return BomSpecificationResolver(db).default_spec_id(int(item_id))


def _piecework_operation_defaults(
    db: Session,
    product: Optional[ProductionProduct],
) -> PieceworkOperationDefaults:
    spec_id = _piecework_spec_id(db, product)
    if not spec_id:
        return PieceworkOperationDefaults()

    spec = db.query(Specification).filter(Specification.spec_id == int(spec_id)).one_or_none()
    spec_ref = _clean_ref1c(getattr(spec, "spec_ref1c", None)) or None
    spec_operations = (
        db.query(SpecOperation, Operation)
        .join(Operation, Operation.operation_id == SpecOperation.operation_id)
        .filter(SpecOperation.spec_id == int(spec_id))
        .filter(Operation.operation_ref1c.isnot(None))
        .order_by(SpecOperation.spec_operation_id.asc())
        .all()
    )
    if not spec_operations:
        return PieceworkOperationDefaults(spec_ref1c=spec_ref)

    # Structural unit of the piecework order = the line's resolved workshop
    # (production kind / manual assignment), not the stage chain. The document
    # has always carried a single unit (the first operation's stage used to
    # pick it), so this loses no granularity and keeps the piecework order
    # consistent with the journal and the transfers.
    structural_unit_ref = None
    workshop_id = resolve_workshop_for_product(db, product, spec_id=spec_id) if product else None
    binding = warehouse_binding_for_workshop(db, workshop_id)
    if binding:
        structural_unit_ref = (
            _clean_ref1c(binding.production_warehouse_ref1c)
            or _clean_ref1c(binding.warehouse_ref1c)
            or None
        )

    stage_ids = {
        int(so.stage_id)
        for so, _op in spec_operations
        if getattr(so, "stage_id", None)
    }
    stages_by_id = {}
    if stage_ids:
        stages_by_id = {
            int(stage.stage_id): _clean_ref1c(getattr(stage, "stage_ref1c", None)) or None
            for stage in db.query(ProductionStage)
            .filter(ProductionStage.stage_id.in_(stage_ids))
            .all()
        }

    operation_lines: List[PieceworkOperationLine] = []
    for so, op in spec_operations:
        op_ref = _clean_ref1c(op.operation_ref1c)
        if not op_ref:
            continue
        stage_ref = stages_by_id.get(int(so.stage_id)) if so.stage_id else None
        operation_lines.append(
            PieceworkOperationLine(
                operation_ref1c=op_ref,
                time_norm=float(so.time_norm if so.time_norm is not None else op.time_norm or 0),
                price=float(op.operation_price or 0),
                stage_ref1c=stage_ref,
                spec_operation_id=int(so.spec_operation_id),
                operation_id=int(op.operation_id),
            )
        )

    if not operation_lines:
        return PieceworkOperationDefaults(
            spec_ref1c=spec_ref,
            structural_unit_ref1c=structural_unit_ref,
        )

    first = operation_lines[0]
    return PieceworkOperationDefaults(
        operation_ref1c=first.operation_ref1c,
        time_norm=first.time_norm,
        price=first.price,
        spec_ref1c=spec_ref,
        stage_ref1c=first.stage_ref1c,
        operation_lines=tuple(operation_lines),
        structural_unit_ref1c=structural_unit_ref,
    )


def _collect_export_entries(
    db: Session,
    manufacture_ids: List[int],
) -> Tuple[List[PieceworkExportEntry], List[Dict[str, Any]]]:
    entries: List[PieceworkExportEntry] = []
    skipped: List[Dict[str, Any]] = []

    ids = [int(x) for x in manufacture_ids if x is not None]
    if not ids:
        return entries, skipped

    rows = (
        db.query(ProductionManufacture)
        .options(
            joinedload(ProductionManufacture.product).joinedload(ProductionProduct.item),
            joinedload(ProductionManufacture.order),
        )
        .filter(ProductionManufacture.manufacture_id.in_(ids))
        .all()
    )
    found_ids = {int(m.manufacture_id) for m in rows}
    for missing in [x for x in ids if x not in found_ids]:
        skipped.append({"manufacture_id": missing, "reason": "ProductionManufacture не найден"})

    for m in rows:
        if str(m.status or "").lower() == "cancelled":
            skipped.append({"manufacture_id": int(m.manufacture_id), "reason": "status='cancelled'"})
            continue

        basis_ref = _clean_ref1c(m.exported_ref1c)
        if not basis_ref:
            skipped.append({
                "manufacture_id": int(m.manufacture_id),
                "reason": "exported_ref1c пустой — сначала выгрузите manufacture в 1С (Document_СборкаЗапасов)",
            })
            continue

        item = m.product.item if m.product else None
        item_ref = _clean_ref1c(item.item_ref1c) if item else ""
        if not item_ref:
            skipped.append({
                "manufacture_id": int(m.manufacture_id),
                "reason": "item_ref1c пустой, нельзя сопоставить с номенклатурой 1С",
            })
            continue

        operation_defaults = _piecework_operation_defaults(db, m.product)

        employee_ref = None
        employee_type = "employee"
        if m.executor:
            employee = (
                db.query(Employee)
                .filter(Employee.employee_name == str(m.executor))
                .filter(Employee.deletion_mark.is_(False))
                .one_or_none()
            )
            if employee:
                employee_ref = _clean_ref1c(employee.employee_ref1c) or None
                employee_type = str(getattr(employee, "employee_type", None) or "employee")
        operation_lines = list(operation_defaults.operation_lines)
        operation_employee_rows = (
            db.query(ProductionManufactureOperation)
            .filter(ProductionManufactureOperation.manufacture_id == int(m.manufacture_id))
            .order_by(ProductionManufactureOperation.line_number.asc(), ProductionManufactureOperation.id.asc())
            .all()
        )
        employees_by_spec_operation_id = {
            int(row.spec_operation_id): row
            for row in operation_employee_rows
            if row.spec_operation_id is not None
        }
        employees_by_operation_id = {
            int(row.operation_id): row
            for row in operation_employee_rows
            if row.operation_id is not None
        }
        if operation_employee_rows:
            enriched_lines: List[PieceworkOperationLine] = []
            for line in operation_lines:
                employee_row = None
                spec_operation_id = getattr(line, "spec_operation_id", None)
                operation_id = getattr(line, "operation_id", None)
                if spec_operation_id is not None:
                    employee_row = employees_by_spec_operation_id.get(int(spec_operation_id))
                if employee_row is None and operation_id is not None:
                    employee_row = employees_by_operation_id.get(int(operation_id))
                enriched_lines.append(PieceworkOperationLine(
                    operation_ref1c=line.operation_ref1c,
                    time_norm=line.time_norm,
                    price=line.price,
                    stage_ref1c=line.stage_ref1c,
                    spec_operation_id=getattr(line, "spec_operation_id", None),
                    operation_id=getattr(line, "operation_id", None),
                    employee_ref1c=_clean_ref1c(getattr(employee_row, "employee_ref1c", None)) if employee_row else None,
                    employee_type=str(getattr(employee_row, "employee_type", None) or "employee") if employee_row else "employee",
                ))
            operation_lines = enriched_lines
            # Исполнитель наряда НЕ обнуляется: оператор мог назначить его не на
            # каждую операцию, и незакрытые строки должны взять его как запасной.
            # Обнуление здесь оставляло строку регистра «Сдельные наряды» пустой,
            # и 1С отказывалась проводить уже созданный документ.
        elif employee_ref and operation_lines:
            operation_lines = [
                PieceworkOperationLine(
                    operation_ref1c=line.operation_ref1c,
                    time_norm=line.time_norm,
                    price=line.price,
                    stage_ref1c=line.stage_ref1c,
                    spec_operation_id=getattr(line, "spec_operation_id", None),
                    operation_id=getattr(line, "operation_id", None),
                    employee_ref1c=employee_ref,
                    employee_type=employee_type,
                )
                for line in operation_lines
            ]
            employee_ref = None

        entries.append(PieceworkExportEntry(
            manufacture_id=int(m.manufacture_id),
            product_id=int(m.product_id),
            order_id=int(m.order_id),
            order_ref1c=_clean_ref1c(m.order.order_ref1c) if m.order else None,
            basis_ref1c=basis_ref,
            item_ref1c=item_ref,
            item_name=str(item.item_name or "") if item else "",
            unit_ref1c=_clean_ref1c(item.unit) if item else None,
            qty=float(m.qty or 0),
            characteristic_ref1c=m.product.characteristic_ref1c,
            operation_ref1c=operation_defaults.operation_ref1c,
            time_norm=operation_defaults.time_norm,
            price=operation_defaults.price,
            spec_ref1c=operation_defaults.spec_ref1c,
            stage_ref1c=operation_defaults.stage_ref1c,
            operation_lines=operation_lines,
            structural_unit_ref1c=operation_defaults.structural_unit_ref1c,
            employee_ref1c=employee_ref,
            employee_type=employee_type,
            number=piecework_number(db, m),
        ))

    return entries, skipped


def _build_header_payload(
    entry: PieceworkExportEntry,
    *,
    operation_ref: str,
    time_norm: float = 0.0,
    price: float = 0.0,
    organization_ref: Optional[str] = None,
    structural_unit_ref: Optional[str] = None,
    business_operation_ref: Optional[str] = None,
) -> Dict[str, Any]:
    when = entry.document_datetime or _current_1c_datetime()
    entry.document_datetime = when
    operation_ref = _clean_ref1c(operation_ref)
    if operation_ref:
        operation_lines = [
            PieceworkOperationLine(
                operation_ref1c=operation_ref,
                time_norm=float(time_norm or entry.time_norm or 0.0),
                price=float(price or entry.price or 0.0),
                stage_ref1c=entry.stage_ref1c,
                employee_ref1c=entry.employee_ref1c,
                employee_type=entry.employee_type,
            )
        ]
    else:
        operation_lines = list(entry.operation_lines)
        if not operation_lines and entry.operation_ref1c:
            operation_lines = [
                PieceworkOperationLine(
                    operation_ref1c=entry.operation_ref1c,
                    time_norm=float(entry.time_norm or 0.0),
                    price=float(entry.price or 0.0),
                    stage_ref1c=entry.stage_ref1c,
                )
            ]
    if not operation_lines:
        raise ValueError(
            f"manufacture_id={entry.manufacture_id}: не найдена операция спецификации для сдельного наряда"
        )
    structural_unit_ref = structural_unit_ref or entry.structural_unit_ref1c
    base_link_key = int(entry.manufacture_id) % 2_000_000_000

    comment = (
        f"PRODPLAN source=piecework/{entry.manufacture_id}; "
        f"order_id={entry.order_id}; product_id={entry.product_id}; "
        f"number={entry.number}"
    )

    header_executor_type = (
        "StandardODATA.Catalog_Бригады"
        if entry.employee_type == "brigade"
        else "StandardODATA.Catalog_Сотрудники"
    )
    operation_rows: List[Dict[str, Any]] = []
    for idx, line in enumerate(operation_lines, start=1):
        row_operation_ref = _clean_ref1c(line.operation_ref1c)
        if not row_operation_ref:
            continue
        row_time_norm = float(line.time_norm or 0.0)
        row_price = float(line.price or 0.0)
        operation_row: Dict[str, Any] = {
            "LineNumber": idx,
            "Период": when,
            "Номенклатура_Key": entry.item_ref1c,
            "Операция_Key": row_operation_ref,
            "КоличествоПлан": float(entry.qty),
            "КоличествоФакт": float(entry.qty),
            "НормаВремени": row_time_norm,
            "Нормочасы": float(entry.qty) * row_time_norm,
            "КлючСвязи": base_link_key + idx - 1,
        }
        if entry.characteristic_ref1c:
            operation_row["Характеристика_Key"] = entry.characteristic_ref1c
        if row_price > 0:
            operation_row["Расценка"] = row_price
            operation_row["Стоимость"] = float(entry.qty) * row_price
        if entry.order_ref1c:
            operation_row["ЗаказНаПроизводство_Key"] = entry.order_ref1c
        if structural_unit_ref:
            operation_row["СтруктурнаяЕдиница_Key"] = structural_unit_ref
        if entry.spec_ref1c:
            operation_row["Спецификация_Key"] = entry.spec_ref1c
        if line.stage_ref1c:
            operation_row["Этап_Key"] = line.stage_ref1c
        if structural_unit_ref:
            operation_row["ПодразделениеЗавершающегоЭтапа_Key"] = structural_unit_ref
        _add_unit_payload(operation_row, entry.unit_ref1c)
        row_executor_ref = _clean_ref1c(line.employee_ref1c)
        if row_executor_ref:
            operation_row["Исполнитель"] = row_executor_ref
            operation_row["Исполнитель_Type"] = (
                "StandardODATA.Catalog_Бригады"
                if line.employee_type == "brigade"
                else "StandardODATA.Catalog_Сотрудники"
            )
        operation_rows.append(operation_row)

    if not operation_rows:
        raise ValueError(
            f"manufacture_id={entry.manufacture_id}: не найдена операция спецификации для сдельного наряда"
        )
    has_row_executor = any(_clean_ref1c(line.employee_ref1c) for line in operation_lines)
    fallback_executor = _clean_ref1c(entry.employee_ref1c)
    if has_row_executor and fallback_executor:
        for row in operation_rows:
            if not _clean_ref1c(row.get("Исполнитель")):
                row["Исполнитель"] = fallback_executor
                row["Исполнитель_Type"] = header_executor_type

    # 1С не проводит наряд, у которого хоть в одной строке регистра «Сдельные
    # наряды» пустой исполнитель, и отвечает на Post пятисоткой. Раньше документ
    # к этому моменту был уже создан: в 1С оставался непроведённый сирота, а
    # оператор видел сырой HTTP 500 с закодированным URL. Проверяем до записи.
    if has_row_executor:
        missing_rows = [
            int(row.get("LineNumber") or 0)
            for row in operation_rows
            if not _clean_ref1c(row.get("Исполнитель"))
        ]
        if missing_rows:
            raise ValueError(
                f"manufacture_id={entry.manufacture_id}: не указан исполнитель по "
                f"операциям {', '.join(str(number) for number in missing_rows)}; "
                "1С не проведёт сдельный наряд с пустой строкой исполнителя"
            )
    elif not fallback_executor:
        raise ValueError(
            f"manufacture_id={entry.manufacture_id}: не указан исполнитель; "
            "1С не проведёт сдельный наряд с пустым исполнителем"
        )

    payload: Dict[str, Any] = {
        "Number": entry.number,
        "Date": when,
        "Posted": False,
        "Закрыт": True,
        "ДатаЗакрытия": when,
        "Комментарий": comment,
        "Операции": operation_rows,
    }
    if entry.order_ref1c:
        payload["ЗаказНаПроизводство_Key"] = entry.order_ref1c
    if entry.basis_ref1c:
        payload["ДокументОснование"] = entry.basis_ref1c
        payload["ДокументОснование_Type"] = BASIS_TYPE
    if organization_ref:
        payload["Организация_Key"] = organization_ref
    if structural_unit_ref:
        payload["СтруктурнаяЕдиница_Key"] = structural_unit_ref
    if business_operation_ref:
        payload["ХозяйственнаяОперация_Key"] = business_operation_ref
    if entry.employee_ref1c and not has_row_executor:
        payload["Исполнитель"] = entry.employee_ref1c
        payload["Исполнитель_Type"] = header_executor_type
        payload["ПоложениеИсполнителя"] = "ВШапке"
        if entry.employee_type != "brigade":
            payload["СоставБригады"] = [
                {
                    "LineNumber": 1,
                    "Сотрудник_Key": entry.employee_ref1c,
                    "КТУ": 1,
                    "КлючСвязи": base_link_key,
                    **({"СтруктурнаяЕдиница_Key": structural_unit_ref} if structural_unit_ref else {}),
                }
            ]
    elif has_row_executor:
        payload["Исполнитель"] = EMPTY_REF1C
        payload["Исполнитель_Type"] = "StandardODATA.Catalog_Сотрудники"
        payload["ПоложениеИсполнителя"] = "ВТабличнойЧасти"

    return payload


def _add_brigade_composition_to_payload(
    client: OData1CClient,
    entry: PieceworkExportEntry,
    payload: Dict[str, Any],
) -> None:
    if entry.employee_type != "brigade":
        return
    brigade_ref = _clean_ref1c(entry.employee_ref1c)
    if not brigade_ref:
        return
    link_key = int(entry.manufacture_id) % 2_000_000_000
    structural_unit_ref = (
        _clean_ref1c(payload.get("СтруктурнаяЕдиница_Key"))
        or _clean_ref1c(entry.structural_unit_ref1c)
        or None
    )
    try:
        doc = client._make_request(
            f"Catalog_Бригады(guid'{brigade_ref}')",
            params={"$format": "json"},
            timeout=60,
            retries=1,
        )
    except Exception:
        return
    rows = []
    for row in doc.get("Состав") or []:
        employee_ref = _clean_ref1c(row.get("Сотрудник_Key"))
        if not employee_ref:
            continue
        rows.append(
            {
                "LineNumber": len(rows) + 1,
                "Сотрудник_Key": employee_ref,
                "КТУ": float(row.get("КТУ") or 1),
                "КлючСвязи": link_key,
                **({"СтруктурнаяЕдиница_Key": structural_unit_ref} if structural_unit_ref else {}),
            }
        )
    if rows:
        payload["СоставБригады"] = rows


def _upsert_link(
    db: Session,
    *,
    entry: PieceworkExportEntry,
    payload_hash: str,
    target_ref_key: Optional[str],
    status: str,
    last_error: Optional[str],
) -> None:
    _upsert_sync_link(
        db,
        SyncLink,
        source_doctype="piecework",
        source_id=int(entry.manufacture_id),
        target_entity=PIECEWORK_ENTITY,
        target_number=entry.number,
        payload_hash=payload_hash,
        target_ref_key=target_ref_key,
        status=status,
        last_error=last_error,
    )


def _record_manufacture_export_error(db: Session, manufacture_id: int, error: str) -> None:
    """
    Surface a piecework failure on the выпуск row.

    Without it a posted Document_СборкаЗапасов whose Document_СдельныйНаряд
    never made it into 1C looks perfectly healthy in the journal.
    """
    m_row = (
        db.query(ProductionManufacture)
        .filter(ProductionManufacture.manufacture_id == int(manufacture_id))
        .one_or_none()
    )
    if m_row is None:
        return
    m_row.export_error = f"СдельныйНаряд: {error}"


def _chain_export_parent_manufactures(
    db: Session,
    manufacture_ids: List[int],
    *,
    dry_run: bool,
) -> Optional[Dict[str, Any]]:
    """
    Per .docs/one_c_export_from_prodplan.md: a Document_СдельныйНаряд MUST be
    created on the basis of a Document_СборкаЗапасов. So before exporting any
    piecework order, ensure its parent ProductionManufacture is in 1C —
    auto-export the missing ones first. That export itself chains through
    the production order if needed.
    """
    parent_ids_rows = (
        db.query(ProductionManufacture.manufacture_id)
        .filter(ProductionManufacture.manufacture_id.in_(list(manufacture_ids)))
        .filter(
            (ProductionManufacture.exported_ref1c.is_(None))
            | (ProductionManufacture.exported_ref1c == "")
        )
        .all()
    )
    parent_ids = [int(r[0]) for r in parent_ids_rows]
    if not parent_ids:
        return None
    return export_manufactures_to_1c(
        db,
        parent_ids,
        dry_run=dry_run,
    )


def export_piecework_to_1c(
    db: Session,
    manufacture_ids: List[int],
    *,
    operation_ref: Optional[str] = None,
    time_norm: float = 0.0,
    price: float = 0.0,
    organization_ref: Optional[str] = None,
    structural_unit_ref: Optional[str] = None,
    business_operation_ref: Optional[str] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Export labor for production; reconcile actual 1C operations before writing."""
    parent = _chain_export_parent_manufactures(db, list(manufacture_ids), dry_run=dry_run)
    entries, skipped = _collect_export_entries(db, list(manufacture_ids))
    result = {"status": "ok", "dry_run": dry_run, "entity": PIECEWORK_ENTITY,
              "manufactures_requested": len(manufacture_ids), "manufactures_eligible": len(entries),
              "manufactures_already_linked": 0, "manufactures_created": 0, "manufactures_error": 0,
              "entries": [], "payloads": [], "piecework_price_lookup": [], "skipped_rows": skipped, "parent_manufactures_export": parent}
    for entry in entries:
        previous_link = _existing_link(db, entry.manufacture_id)
        if dry_run and previous_link and previous_link.status == "success" and previous_link.target_ref_key:
            result["manufactures_eligible"] -= 1
            result["manufactures_already_linked"] += 1
            entry.status = "existing"
            result["entries"].append(asdict(entry))
            continue
        if operation_ref:
            entry.operation_lines = [PieceworkOperationLine(operation_ref1c=operation_ref,
                time_norm=time_norm or entry.time_norm, price=price or entry.price,
                employee_ref1c=entry.employee_ref1c, employee_type=entry.employee_type)]
        try:
            if dry_run:
                config = _load_odata_config()
                payload = _build_header_payload(entry, operation_ref=operation_ref, time_norm=time_norm, price=price,
                    organization_ref=organization_ref or _config_ref1c(config, "default_organization_ref1c", DEFAULT_ORGANIZATION_REF1C),
                    structural_unit_ref=structural_unit_ref or entry.structural_unit_ref1c or _config_ref1c(
                        config, "default_production_structural_unit_ref1c", DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C),
                    business_operation_ref=business_operation_ref)
                result["payloads"].append({"manufacture_id": entry.manufacture_id, "payload": payload})
            else:
                one = _export_checked_piecework(db, [entry], dry_run=False, organization_ref=organization_ref, structural_unit_ref=structural_unit_ref, business_operation_ref=business_operation_ref)
                result["piecework_price_lookup"].extend(one.get("piecework_price_lookup", []))
                result["manufactures_created"] += one["created"]
                result["manufactures_error"] += one["errored"]
                result["manufactures_already_linked"] += int(one["status"] == "existing")
        except Exception as exc:
            entry.status, entry.error = "error", str(exc)
            result["manufactures_error"] += 1
            if not dry_run:
                _record_manufacture_export_error(db, entry.manufacture_id, str(exc))
                _upsert_link(db, entry=entry, payload_hash="", target_ref_key=previous_link.target_ref_key if previous_link else None,
                             status="error", last_error=str(exc))
                db.commit()
        result["entries"].append(asdict(entry))
    if result["manufactures_error"]:
        result["status"] = "partial_error"
    return result


# ---------------------------------------------------------------------------
# Комбинированный сдельный цепочки «окраска↔сварка» (этап 4).
# См. .docs/paint_weld_chain_logic.md п.6: бумага одна, операции сварки и
# окраски в одном документе, у каждой строки свой ЗаказНаПроизводство_Key и
# участок, основание — окрасочная СборкаЗапасов. Состояние самих заказов
# экспорт не пишет: заказ закрывает оператор в 1С.
# ---------------------------------------------------------------------------


def _merge_chain_payloads(
    *, weld_payload: Dict[str, Any], paint_payload: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Слить два штатных payload'а СдельныйНаряд в один комбинированный.

    Шапка (Number, Date, основание-СборкаЗапасов, ЗаказНаПроизводство_Key,
    организация) — от окрасочного. Операции — сварочный блок, затем окрасочный;
    каждая строка сохраняет свои заказ/участок/номенклатуру/этап. Исполнители:
    если после слияния есть построчные — документ переводится в
    «ВТабличнойЧасти», исполнитель из шапки каждого блока опускается в его
    строки.
    """

    def _rows_of(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows = [dict(row) for row in payload.get("Операции") or []]
        header_executor = _clean_ref1c(payload.get("Исполнитель"))
        if payload.get("ПоложениеИсполнителя") == "ВШапке" and header_executor:
            for row in rows:
                row.setdefault("Исполнитель", header_executor)
                row.setdefault(
                    "Исполнитель_Type",
                    payload.get("Исполнитель_Type") or "StandardODATA.Catalog_Сотрудники",
                )
        return rows

    combined = {
        key: value
        for key, value in paint_payload.items()
        if key not in ("Операции", "СоставБригады")
    }
    rows = _rows_of(weld_payload) + _rows_of(paint_payload)
    for idx, row in enumerate(rows, start=1):
        row["LineNumber"] = idx
    combined["Операции"] = rows

    has_row_executor = any(_clean_ref1c(row.get("Исполнитель")) for row in rows)
    if has_row_executor:
        combined["Исполнитель"] = EMPTY_REF1C
        combined["Исполнитель_Type"] = "StandardODATA.Catalog_Сотрудники"
        combined["ПоложениеИсполнителя"] = "ВТабличнойЧасти"
    else:
        brigade_rows: List[Dict[str, Any]] = []
        seen_employees: set = set()
        for row in (weld_payload.get("СоставБригады") or []) + (
            paint_payload.get("СоставБригады") or []
        ):
            employee_key = _clean_ref1c(row.get("Сотрудник_Key"))
            if not employee_key or employee_key in seen_employees:
                continue
            seen_employees.add(employee_key)
            brigade_rows.append({**dict(row), "LineNumber": len(brigade_rows) + 1})
        if brigade_rows:
            combined["СоставБригады"] = brigade_rows

    weld_comment = str(weld_payload.get("Комментарий") or "")
    paint_comment = str(paint_payload.get("Комментарий") or "")
    combined["Комментарий"] = (
        f"{paint_comment}; {weld_comment}; комбинированный сдельный цепочки окраска↔сварка"
    )
    return combined


def export_chain_piecework_to_1c(
    db: Session,
    *,
    weld_manufacture_id: int,
    paint_manufacture_id: int,
    organization_ref: Optional[str] = None,
    business_operation_ref: Optional[str] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Complete only unpaid operations; separate weld labor is retained in 1C."""
    parent = _chain_export_parent_manufactures(db, [weld_manufacture_id, paint_manufacture_id], dry_run=dry_run)
    entries, skipped = _collect_export_entries(db, [weld_manufacture_id, paint_manufacture_id])
    by_id = {entry.manufacture_id: entry for entry in entries}
    if any(mid not in by_id for mid in (weld_manufacture_id, paint_manufacture_id)):
        return {"status": "error", "error": "не собраны данные по обоим выпускам цепочки", "skipped_rows": skipped}
    ordered = [by_id[weld_manufacture_id], by_id[paint_manufacture_id]]
    if dry_run:
        config = _load_odata_config()
        payloads = [_build_header_payload(entry, operation_ref="",
            organization_ref=organization_ref or _config_ref1c(config, "default_organization_ref1c", DEFAULT_ORGANIZATION_REF1C),
            structural_unit_ref=entry.structural_unit_ref1c or _config_ref1c(
                config, "default_production_structural_unit_ref1c", DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C),
            business_operation_ref=business_operation_ref) for entry in ordered]
        payload = _merge_chain_payloads(weld_payload=payloads[0], paint_payload=payloads[1])
        return {"status": "ok", "combined": True, "dry_run": True, "entries": [asdict(e) for e in ordered],
                "payloads": [{"payload": payload}], "parent_manufactures_export": parent}
    try:
        result = _export_checked_piecework(db, ordered, dry_run=False, combined=True)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    result["parent_manufactures_export"] = parent
    return result


# Labor coverage is a document-write guard, never a source of production facts.
def _labor_key(row):
    def key(name):
        value = _clean_ref1c(row.get(name)).lower()
        return "" if value == EMPTY_REF1C else value
    return tuple(key(name) for name in (
        "ЗаказНаПроизводство_Key", "Номенклатура_Key", "Характеристика_Key", "Операция_Key", "Этап_Key",
    ))


def _read_piecework_rows(client, order_refs, *, retry_ref=None):
    """Read every matching line, including weld rows in a painted-header document."""
    import math
    rows, docs = [], {}
    for order_ref in sorted(set(order_refs)):
        for page in range(100):
            response = client._make_request(PIECEWORK_ENTITY + "_Операции", params={
                "$filter": f"ЗаказНаПроизводство_Key eq guid'{order_ref}'",
                "$orderby": "Ref_Key,LineNumber", "$top": 250, "$skip": page * 250,
            })
            if not isinstance(response, dict) or not isinstance(response.get("value"), list):
                raise ValueError("1С не вернула полный список операций сдельных нарядов")
            batch = response["value"]
            for row in batch:
                if _clean_ref1c(row.get("ЗаказНаПроизводство_Key")).lower() != order_ref.lower():
                    raise ValueError("1С вернула операции другого заказа")
                ref = _clean_ref1c(row.get("Ref_Key"))
                if not ref:
                    raise ValueError("У операции сдельного наряда нет ссылки на документ")
                if ref not in docs:
                    docs[ref] = client._make_request(f"{PIECEWORK_ENTITY}(guid'{ref}')")
                doc = docs[ref]
                if not isinstance(doc, dict) or "Posted" not in doc or "DeletionMark" not in doc:
                    raise ValueError("1С не подтвердила состояние сдельного наряда")
                if doc["DeletionMark"] is True:
                    continue
                if "КоличествоФакт" not in row:
                    raise ValueError("1С не вернула количество операции")
                qty = float(row.get("КоличествоФакт") or 0)
                if not math.isfinite(qty) or qty < 0:
                    raise ValueError("Некорректное количество операции в сдельном наряде 1С")
                if doc["Posted"] is not True:
                    if ref == retry_ref:
                        continue
                    row = {**row, "_unposted": True}
                rows.append({**row, "_qty": qty, "_ref": ref})
            if len(batch) < 250:
                break
        else:
            raise ValueError("Список сдельных нарядов усечён; создание заблокировано")
    return rows


def _missing_piecework_payload(entry, payload, existing_rows, target_qty):
    import math
    missing, references = [], set()
    keys = [_labor_key(row) for row in payload["Операции"]]
    if len(keys) != len(set(keys)):
        raise ValueError("Операция повторяется в спецификации без отличающегося этапа; требуется уточнение")
    for row in payload["Операции"]:
        key = _labor_key(row)
        covered = 0.0
        for previous in existing_rows:
            previous_key = _labor_key(previous)
            # A manual row without a stage can match only one requested stage.
            matches = previous_key == key
            if not previous_key[-1] and previous_key[:-1] == key[:-1]:
                if sum(candidate[:-1] == key[:-1] for candidate in keys) > 1:
                    raise ValueError("В наряде 1С не указан этап повторяющейся операции")
                matches = True
            if not matches:
                continue
            if previous.get("_unposted"):
                raise ValueError(f"Есть непроведённый сдельный наряд {previous['_ref']}; проведите или отмените его в 1С")
            covered += previous["_qty"]
            references.add(previous["_ref"])
        qty = min(float(row["КоличествоФакт"]), max(float(target_qty) - covered, 0.0))
        if not math.isfinite(qty):
            raise ValueError("Некорректное количество сдельного наряда")
        if qty <= 1e-6:
            continue
        missing.append({**row, "КоличествоПлан": qty, "КоличествоФакт": qty,
                        "Нормочасы": qty * float(row.get("НормаВремени") or 0),
                        "Стоимость": qty * float(row.get("Расценка") or 0)})
    for index, row in enumerate(missing, 1):
        row["LineNumber"] = index
    return {**payload, "Операции": missing}, sorted(references)


from contextlib import contextmanager


@contextmanager
def _piecework_write_lock(db, order_ids):
    """Session locks survive exporter commits and serialize both UI commands."""
    from sqlalchemy import text
    if db.get_bind().dialect.name != "postgresql":
        yield
        return
    with db.get_bind().connect() as connection:
        locked = []
        try:
            for order_id in sorted(set(order_ids)):
                if not connection.execute(text("SELECT pg_try_advisory_lock(87123, :id)"), {"id": int(order_id)}).scalar():
                    raise ValueError("По этому заказу уже оформляется сдельный наряд. Повторите после завершения операции.")
                locked.append(order_id)
            yield
        finally:
            for order_id in reversed(locked):
                connection.execute(text("SELECT pg_advisory_unlock(87123, :id)"), {"id": int(order_id)})


def _export_checked_piecework(db, entries, *, dry_run, combined=False, standalone=False, organization_ref=None, structural_unit_ref=None, business_operation_ref=None):
    """One writer for standalone labor, single manufacture and combined chain."""
    from sqlalchemy import func
    config = _load_odata_config()
    client = _create_odata_client(config, OData1CClient)
    source_doctype = "standalone_piecework" if standalone else "piecework"
    with _piecework_write_lock(db, [entry.order_id for entry in entries]):
        payloads = []
        existing_refs = {}
        target = entries[-1]
        link = _find_sync_link(db, SyncLink, source_doctype=source_doctype,
                              source_id=target.manufacture_id, target_entity=PIECEWORK_ENTITY)
        retry_ref = _clean_ref1c(link.target_ref_key) if link else ""
        # Recover POST-after-timeout by stable identity, including unposted headers.
        marker = f"PRODPLAN source={source_doctype}/{target.manufacture_id};"
        found = client._make_request(PIECEWORK_ENTITY, params={
            "$filter": f"substringof('{marker}', Комментарий)", "$top": 2,
        })
        if not isinstance(found, dict) or not isinstance(found.get("value"), list):
            raise ValueError("1С не подтвердила поиск ранее созданного сдельного наряда")
        candidates = [doc for doc in found["value"] if doc.get("DeletionMark") is not True]
        if len(candidates) > 1:
            raise ValueError("В 1С несколько нарядов одной команды; требуется проверка")
        if candidates:
            retry_ref = _clean_ref1c(candidates[0].get("Ref_Key"))
        live = _read_piecework_rows(client, [e.order_ref1c for e in entries], retry_ref=retry_ref)
        for entry in entries:
            payload = _build_header_payload(entry, operation_ref="", business_operation_ref=business_operation_ref, organization_ref=organization_ref or _config_ref1c(
                config, "default_organization_ref1c", DEFAULT_ORGANIZATION_REF1C),
                structural_unit_ref=structural_unit_ref or entry.structural_unit_ref1c or _config_ref1c(
                    config, "default_production_structural_unit_ref1c", DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C))
            target_qty = entry.qty if standalone else float(db.query(func.sum(ProductionManufacture.qty)).filter(
                ProductionManufacture.product_id == entry.product_id,
                ProductionManufacture.manufacture_id <= entry.manufacture_id,
                ProductionManufacture.status != "cancelled").scalar() or 0)
            payload, refs = _missing_piecework_payload(entry, payload, live, target_qty)
            existing_refs[entry.manufacture_id] = refs
            payloads.append(payload)
        payload = _merge_chain_payloads(weld_payload=payloads[0], paint_payload=payloads[1]) if combined else payloads[0]
        payload["Комментарий"] = marker + (f" order_id={target.order_id}; product_id={target.product_id}" if standalone else " " + str(payload.get("Комментарий") or ""))
        summary = {"status": "ok", "dry_run": dry_run, "entries": [asdict(e) for e in entries],
                   "payloads": [payload] if payload["Операции"] else [], "created": 0, "errored": 0,
                   "manufactures_created": 0, "manufactures_error": 0, "covered_refs": existing_refs}
        if dry_run:
            return summary

        def save_links(*, entry, payload_hash, target_ref_key, status, last_error):
            for index, current in enumerate(entries):
                ref = target_ref_key if payloads[index]["Операции"] else next(iter(existing_refs[current.manufacture_id]), None)
                _upsert_sync_link(db, SyncLink, source_doctype=source_doctype,
                    source_id=current.manufacture_id, target_entity=PIECEWORK_ENTITY,
                    target_number=target.number, payload_hash=payload_hash,
                    target_ref_key=ref, status=status, last_error=last_error)

        if not payload["Операции"]:
            save_links(entry=target, payload_hash="live-coverage", target_ref_key=retry_ref,
                       status="success", last_error=None)
            db.commit()
            for entry in entries:
                entry.status = "existing"
                entry.target_ref_key = next(iter(existing_refs[entry.manufacture_id]), None)
            summary.update(status="existing", target_ref_key=entries[-1].target_ref_key,
                           entries=[asdict(e) for e in entries], message="Операции уже оформлены в 1С; новый наряд не создан")
            return summary
        if retry_ref:
            doc = client._make_request(f"{PIECEWORK_ENTITY}(guid'{retry_ref}')")
            if doc.get("Posted") is True and not doc.get("DeletionMark"):
                raise ValueError("Ранее созданный наряд проведён, но не покрывает выбранные операции; требуется проверка")
            target.target_ref_key = retry_ref
            target.unpost_before_patch = False
        summary["piecework_price_lookup"] = [_enrich_payload_prices_from_1c(client, target, payload, price_type_ref=_piecework_price_type_ref(config))]
        _add_brigade_composition_to_payload(client, target, payload)

        def posted(entry, ref):
            _post_document_operational(client, entity=PIECEWORK_ENTITY, ref_key=ref, unpost_first=False)
            client.patch(f"{PIECEWORK_ENTITY}(guid'{ref}')", {
                "Date": entry.document_datetime, "Закрыт": True, "ДатаЗакрытия": entry.document_datetime})
            doc = client._make_request(f"{PIECEWORK_ENTITY}(guid'{ref}')")
            if doc.get("Posted") is not True or doc.get("DeletionMark") is not False:
                raise ValueError("1С не подтвердила проведение сдельного наряда")
            for wanted in payload["Операции"]:
                actual = sum(float(row.get("КоличествоФакт") or 0) for row in doc.get("Операции", []) if _labor_key(row) == _labor_key(wanted))
                if abs(actual - float(wanted["КоличествоФакт"])) > 1e-6:
                    raise ValueError("1С не подтвердила количество операции в сдельном наряде")

        def failed(entry, error):
            if not standalone:
                for current in entries:
                    _record_manufacture_export_error(db, current.manufacture_id, error)

        created, errors = _post_export_entries(db, entries=[(target, {"payload": payload})], client=client,
            target_entity=PIECEWORK_ENTITY, missing_ref_error="1С не вернула ссылку на наряд",
            upsert_link=save_links, on_success=posted, on_error=failed)
        for index, entry in enumerate(entries):
            entry.target_ref_key = target.target_ref_key if payloads[index]["Операции"] else next(iter(existing_refs[entry.manufacture_id]), None)
        summary.update(status="ok" if not errors else "partial_error", created=created, errored=errors,
            manufactures_created=created, manufactures_error=errors, target_ref_key=target.target_ref_key,
            entries=[asdict(e) for e in entries], message="Сдельный наряд оформлен; уже оплаченные операции исключены")
        return summary


def standalone_piecework_product(db, product_id):
    """The separate button targets welding when either linked side is selected."""
    from ..models import PaintWeldChainLink
    product = db.get(ProductionProduct, int(product_id))
    if product is None:
        raise ValueError("Строка заказа не найдена")
    link = db.query(PaintWeldChainLink).filter(
        (PaintWeldChainLink.painted_order_id == product.order_id)
        | (PaintWeldChainLink.welded_order_id == product.order_id)).one_or_none()
    if link is not None:
        from .paint_weld_chain import _chain_link_for_product
        _, _, product = _chain_link_for_product(db, product_id)
    return product


def create_standalone_piecework(db, product_id, *, qty, operation_executors, request_key):
    import math
    from ..models import ProductionPieceworkCommand
    from .planning_truth import require_accepted_truth
    require_accepted_truth(db, "standalone_piecework_command")
    product = standalone_piecework_product(db, product_id)
    if not product.order.order_ref1c or product.order.deletion_mark:
        raise ValueError("Для сдельного нужен существующий заказ в 1С")
    if not math.isfinite(qty) or qty <= 0:
        raise ValueError("Количество должно быть положительным")
    if not operation_executors:
        raise ValueError("Выберите хотя бы одну операцию и исполнителя")
    command = db.query(ProductionPieceworkCommand).filter_by(request_key=request_key).one_or_none()
    if command is None:
        command = ProductionPieceworkCommand(product_id=product.product_id, request_key=request_key,
                    target_qty=qty, operation_executors=operation_executors)
        db.add(command)
        db.flush()
    elif command.product_id != product.product_id or float(command.target_qty) != qty or command.operation_executors != operation_executors:
        raise ValueError("Параметры повторной команды изменились. Откройте новый диалог.")
    defaults = _piecework_operation_defaults(db, product)
    by_id = {line.spec_operation_id: line for line in defaults.operation_lines}
    selected, seen = [], set()
    for row in command.operation_executors:
        spec_id = row.get("spec_operation_id")
        if spec_id not in by_id or spec_id in seen:
            raise ValueError("Выбрана чужая или повторяющаяся операция спецификации")
        seen.add(spec_id)
        employee = db.query(Employee).filter_by(employee_ref1c=row.get("employee_ref1c"), deletion_mark=False).one_or_none()
        if employee is None:
            raise ValueError("Исполнитель операции не найден")
        selected.append(replace(by_id[spec_id], employee_ref1c=employee.employee_ref1c,
                                employee_type=employee.employee_type or "employee"))
    from .one_c_document_numbers import standalone_piecework_number
    entry = PieceworkExportEntry(manufacture_id=command.id, product_id=product.product_id,
        order_id=product.order_id, order_ref1c=product.order.order_ref1c, basis_ref1c=None,
        item_ref1c=product.item.item_ref1c, item_name=product.item.item_name,
        unit_ref1c=product.item.unit, qty=float(command.target_qty), number=standalone_piecework_number(command.id),
        spec_ref1c=defaults.spec_ref1c, characteristic_ref1c=product.characteristic_ref1c,
        structural_unit_ref1c=defaults.structural_unit_ref1c, operation_lines=selected)
    db.commit()
    result = _export_checked_piecework(db, [entry], dry_run=False, standalone=True)
    if result.get("errored"):
        raise ValueError(entry.error or "Не удалось оформить сдельный наряд; повторите ту же команду")
    return {**result, "product_id": product.product_id, "command_id": command.id,
            "message": result["message"] + ". Производство и закрытие заказа не выполнялись."}

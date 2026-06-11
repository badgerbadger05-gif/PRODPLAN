"""Export ProductionManufacture records to 1C as Document_СдельныйНаряд.

Pattern: mirrors one_c_manufacture_export.py.
Documentation: .docs/piecework_order_odata.md.

Safety per the doc:
1. Default dry_run=True.
2. Refuse non-demo base_url unless allow_production=True.
3. Create as not posted, then close and conduct through standard 1C Post operation.
4. Idempotency via sync_link (source_doctype='piecework').

Basis rule (from piecework_order_odata.md):
  Document_СдельныйНаряд.ДокументОснование = manufacture.exported_ref1c
  Document_СдельныйНаряд.ДокументОснование_Type = StandardODATA.Document_СборкаЗапасов

The manufacture must already be exported to 1C (exported_ref1c set) before a
piecework order can reference it as its basis.

Норма времени and расценка are taken from the product specification operations.
operation_ref is still accepted as a manual single-operation override.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from ..models import (
    DefaultSpecification,
    ProductionManufacture,
    ProductionManufactureOperation,
    ProductionOrder,
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
from .one_c_manufacture_export import export_manufactures_to_1c


PIECEWORK_ENTITY = "Document_СдельныйНаряд"
PRODUCTION_ORDER_ENTITY = "Document_ЗаказНаПроизводство"
BASIS_TYPE = "StandardODATA.Document_СборкаЗапасов"
ORDER_TYPE = "StandardODATA.Document_ЗаказНаПроизводство"
DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"


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
    document_datetime: Optional[str] = None
    target_ref_key: Optional[str] = None
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


def _short_piecework_number(manufacture_id: int) -> str:
    return f"PW{int(manufacture_id) % 1_000_000_000:09d}"


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
    row = (
        db.query(DefaultSpecification.spec_id)
        .filter(DefaultSpecification.item_id == int(item_id))
        .order_by(DefaultSpecification.id.asc())
        .first()
    )
    return int(row.spec_id) if row else None


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
            employee_ref = None
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


def _chain_export_parent_manufactures(
    db: Session,
    manufacture_ids: List[int],
    *,
    dry_run: bool,
    allow_production: bool,
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
        allow_production=allow_production,
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
    allow_production: bool = False,
) -> Dict[str, Any]:
    """
    Export selected ProductionManufactures to 1C as Document_СдельныйНаряд
    The document is closed with the same Date/ДатаЗакрытия value and then
    conducted through 1C OData Post. Idempotent via sync_link
    (source_doctype='piecework').

    Enforces the full chain: parent ProductionManufacture is auto-exported as
    Document_СборкаЗапасов first (which itself ensures Document_ЗаказНаПроизводство
    is in 1C), so the piecework order can carry a valid ДокументОснование.
    """
    parent_export = _chain_export_parent_manufactures(
        db, list(manufacture_ids), dry_run=dry_run, allow_production=allow_production
    )
    entries, skipped = _collect_export_entries(db, list(manufacture_ids))

    eligible: List[PieceworkExportEntry] = []
    already_linked: List[PieceworkExportEntry] = []
    for entry in entries:
        link = _existing_link(db, entry.manufacture_id)
        if link and link.status == "success" and (link.target_ref_key or ""):
            entry.status = "existing"
            entry.target_ref_key = str(link.target_ref_key)
            entry.reason = "уже выгружен в 1С (sync_link)"
            already_linked.append(entry)
            continue
        if link and _clean_ref1c(link.target_ref_key):
            entry.target_ref_key = _clean_ref1c(link.target_ref_key)
            entry.reason = "повторная отправка: 1С-документ уже был создан, обновляем реквизиты и проводим"
        eligible.append(entry)

    summary: Dict[str, Any] = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "entity": PIECEWORK_ENTITY,
        "manufactures_requested": len(manufacture_ids),
        "manufactures_eligible": len(eligible),
        "manufactures_already_linked": len(already_linked),
        "manufactures_created": 0,
        "manufactures_error": 0,
        "skipped_rows": skipped,
        "entries": [],
        "parent_manufactures_export": parent_export,
    }

    config = _load_odata_config()
    organization_ref = organization_ref or _config_ref1c(
        config, "default_organization_ref1c", DEFAULT_ORGANIZATION_REF1C
    )
    structural_unit_ref = structural_unit_ref or _config_ref1c(
        config, "default_production_structural_unit_ref1c", DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C
    )

    payloads: List[Dict[str, Any]] = []
    for entry in eligible:
        payload = _build_header_payload(
            entry,
            operation_ref=operation_ref,
            time_norm=time_norm,
            price=price,
            organization_ref=organization_ref,
            structural_unit_ref=structural_unit_ref,
            business_operation_ref=business_operation_ref,
        )
        payloads.append({"manufacture_id": entry.manufacture_id, "number": entry.number, "payload": payload})

    if dry_run:
        summary["entries"] = [asdict(e) for e in entries]
        summary["payloads"] = payloads
        return summary

    client = _create_odata_client(
        config,
        OData1CClient,
        allow_production=allow_production,
        require_demo_base=True,
    )
    for entry, payload_envelope in zip(eligible, payloads):
        _add_brigade_composition_to_payload(client, entry, payload_envelope["payload"])

    def _mark_success(entry: PieceworkExportEntry, ref_key: str) -> None:
        _post_document_operational(
            client,
            entity=PIECEWORK_ENTITY,
            ref_key=ref_key,
            unpost_first=False,
        )
        # 1C can move Date to the posting moment. Keep the business timestamp
        # identical to creation time and mark the order closed explicitly.
        patch = getattr(client, "patch", None)
        if patch is not None and entry.document_datetime:
            patch(
                f"{PIECEWORK_ENTITY}(guid'{ref_key}')",
                {
                    "Date": entry.document_datetime,
                    "Закрыт": True,
                    "ДатаЗакрытия": entry.document_datetime,
                },
            )
        order_ref = _clean_ref1c(entry.order_ref1c)
        if order_ref:
            if patch is None:
                raise RuntimeError("OData client cannot patch production order completion state")
            patch(
                f"{PRODUCTION_ORDER_ENTITY}(guid'{order_ref}')",
                {"СостояниеЗаказа_Key": DONE_STATE_KEY},
            )
            order = (
                db.query(ProductionOrder)
                .filter(ProductionOrder.order_id == int(entry.order_id))
                .one_or_none()
            )
            if order is not None:
                order.order_state_key = DONE_STATE_KEY
                order.order_state_name = "Завершен"

    created, errored = _post_export_entries(
        db,
        entries=zip(eligible, payloads),
        client=client,
        target_entity=PIECEWORK_ENTITY,
        missing_ref_error=f"1C did not return Ref_Key for new {PIECEWORK_ENTITY}",
        upsert_link=lambda **kwargs: _upsert_link(db, **kwargs),
        on_success=_mark_success,
        on_error=lambda entry, error: None,
        log_error=lambda entry: (
            f"[1C piecework export] manufacture_id={entry.manufacture_id} failed: {entry.error}"
        ),
    )

    summary["manufactures_created"] = created
    summary["manufactures_error"] = errored
    summary["entries"] = [asdict(e) for e in entries]
    summary["status"] = "ok" if errored == 0 else "partial_error"
    return summary

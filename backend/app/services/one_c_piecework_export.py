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

Норма времени and расценка default to 0 — they can be filled by the 1C admin
from the routing sheet. operation_ref is required and supplied by the caller
at the batch level (one operation per export run).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from ..models import (
    ProductionManufacture,
    ProductionOrder,
    ProductionProduct,
    Employee,
    Operation,
    ProductionStage,
    ResourceStage,
    Specification,
    SpecOperation,
    SyncLink,
    WorkshopWarehouseBinding,
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
    spec_ref1c: Optional[str] = None
    stage_ref1c: Optional[str] = None
    structural_unit_ref1c: Optional[str] = None
    employee_ref1c: Optional[str] = None
    document_datetime: Optional[str] = None
    target_ref_key: Optional[str] = None
    status: str = "planned"
    error: Optional[str] = None
    reason: Optional[str] = None


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

        operation_ref = None
        time_norm = 0.0
        stage_ref = None
        structural_unit_ref = None
        spec_ref = None
        if m.product and m.product.spec_id:
            spec = db.query(Specification).filter(Specification.spec_id == int(m.product.spec_id)).one_or_none()
            spec_ref = _clean_ref1c(getattr(spec, "spec_ref1c", None)) or None
            spec_operation = (
                db.query(SpecOperation, Operation)
                .join(Operation, Operation.operation_id == SpecOperation.operation_id)
                .filter(SpecOperation.spec_id == int(m.product.spec_id))
                .filter(Operation.operation_ref1c.isnot(None))
                .order_by(SpecOperation.spec_operation_id.asc())
                .first()
            )
            if spec_operation:
                so, op = spec_operation
                operation_ref = _clean_ref1c(op.operation_ref1c) or None
                time_norm = float(so.time_norm or 0)
                if so.stage_id:
                    stage = db.query(ProductionStage).filter(ProductionStage.stage_id == int(so.stage_id)).one_or_none()
                    stage_ref = _clean_ref1c(getattr(stage, "stage_ref1c", None)) or None
                    resource_stage = (
                        db.query(ResourceStage)
                        .filter(ResourceStage.stage_id == int(so.stage_id))
                        .order_by(ResourceStage.id.asc())
                        .first()
                    )
                    if resource_stage:
                        binding = (
                            db.query(WorkshopWarehouseBinding)
                            .filter(WorkshopWarehouseBinding.workshop_id == int(resource_stage.resource_id))
                            .one_or_none()
                        )
                        if binding:
                            structural_unit_ref = (
                                _clean_ref1c(binding.production_warehouse_ref1c)
                                or _clean_ref1c(binding.warehouse_ref1c)
                                or None
                            )

        employee_ref = None
        if m.executor:
            employee = (
                db.query(Employee)
                .filter(Employee.employee_name == str(m.executor))
                .filter(Employee.deletion_mark.is_(False))
                .one_or_none()
            )
            if employee:
                employee_ref = _clean_ref1c(employee.employee_ref1c) or None

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
            operation_ref1c=operation_ref,
            time_norm=time_norm,
            spec_ref1c=spec_ref or None,
            stage_ref1c=stage_ref,
            structural_unit_ref1c=structural_unit_ref,
            employee_ref1c=employee_ref,
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
    operation_ref = _clean_ref1c(operation_ref) or entry.operation_ref1c
    if not operation_ref:
        raise ValueError(
            f"manufacture_id={entry.manufacture_id}: не найдена операция спецификации для сдельного наряда"
        )
    time_norm = float(time_norm or entry.time_norm or 0.0)
    structural_unit_ref = structural_unit_ref or entry.structural_unit_ref1c
    link_key = int(entry.manufacture_id) % 2_000_000_000
    hours = entry.qty * time_norm
    cost = entry.qty * price

    comment = (
        f"PRODPLAN source=piecework/{entry.manufacture_id}; "
        f"order_id={entry.order_id}; product_id={entry.product_id}; "
        f"number={entry.number}"
    )

    operation_row: Dict[str, Any] = {
        "LineNumber": 1,
        "Период": when,
        "Номенклатура_Key": entry.item_ref1c,
        "Операция_Key": operation_ref,
        "КоличествоПлан": float(entry.qty),
        "КоличествоФакт": float(entry.qty),
        "НормаВремени": float(time_norm),
        "Расценка": float(price),
        "Нормочасы": float(hours),
        "Стоимость": float(cost),
        "КлючСвязи": link_key,
    }
    if entry.order_ref1c:
        operation_row["ЗаказНаПроизводство_Key"] = entry.order_ref1c
    if structural_unit_ref:
        operation_row["СтруктурнаяЕдиница_Key"] = structural_unit_ref
    if entry.spec_ref1c:
        operation_row["Спецификация_Key"] = entry.spec_ref1c
    if entry.stage_ref1c:
        operation_row["Этап_Key"] = entry.stage_ref1c
    if structural_unit_ref:
        operation_row["ПодразделениеЗавершающегоЭтапа_Key"] = structural_unit_ref
    _add_unit_payload(operation_row, entry.unit_ref1c)
    if entry.employee_ref1c:
        operation_row["Исполнитель"] = entry.employee_ref1c
        operation_row["Исполнитель_Type"] = "StandardODATA.Catalog_Сотрудники"

    payload: Dict[str, Any] = {
        "Number": entry.number,
        "Date": when,
        "Posted": False,
        "Закрыт": True,
        "ДатаЗакрытия": when,
        "Комментарий": comment,
        "Операции": [operation_row],
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
    if entry.employee_ref1c:
        payload["Исполнитель"] = entry.employee_ref1c
        payload["Исполнитель_Type"] = "StandardODATA.Catalog_Сотрудники"
        payload["СоставБригады"] = [
            {
                "LineNumber": 1,
                "Сотрудник_Key": entry.employee_ref1c,
                "КТУ": 1,
                "КлючСвязи": link_key,
                **({"СтруктурнаяЕдиница_Key": structural_unit_ref} if structural_unit_ref else {}),
            }
        ]

    return payload


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

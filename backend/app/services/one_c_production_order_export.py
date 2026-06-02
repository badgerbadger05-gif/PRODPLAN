"""Export internal MRP-source ProductionOrders to 1C as Document_ЗаказНаПроизводство.

Pattern: mirrors backend/app/services/one_c_purchase_order_export.py.
Documentation: .docs/one_c_export_from_prodplan.md.

Safety rules from the doc are enforced on top of the call site:
1. Default `dry_run=True`; explicit dry_run=False is required to write.
2. Refuse to write if the configured base_url doesn't look like a demo DB
   (substring 'unf_demo'), unless `allow_production=True` is also set.
3. Always send `Posted=false`, then immediately conduct the created order
   through the standard 1C `Post?PostingModeOperational=true` command.
4. Idempotency: skip orders that already have a successful sync_link OR a
   non-empty `production_orders.order_ref1c` (it gets stamped from the
   1C response on first successful export).

Only MRP-source production_orders (source='mrp') are eligible. 1C-synced
orders (source='1c') already exist in 1C — we wouldn't re-export them.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from ..models import (
    DefaultSpecification,
    Item,
    Operation,
    ProductionMaterialIssue,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ResourceStage,
    SpecComponent,
    Specification,
    SpecOperation,
    SyncLink,
    Unit,
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
    fmt_1c_datetime as _fmt_1c_datetime,
    post_document_operational as _post_document_operational,
    post_export_entries as _post_export_entries,
    upsert_sync_link as _upsert_sync_link,
)
from .one_c_document_numbers import production_order_number
from .odata_config import load_odata_config as _load_odata_config
from .odata_client import OData1CClient


PRODUCTION_ORDER_ENTITY = "Document_ЗаказНаПроизводство"
PRODUCTION_ORDER_PRODUCTS_ENTITY = "Document_ЗаказНаПроизводство_Продукция"
EMPTY_REF1C = "00000000-0000-0000-0000-000000000000"


@dataclass
class ProductionOrderExportLine:
    line_number: int
    item_id: int
    item_ref1c: str
    item_name: str
    item_article: str
    unit_ref1c: Optional[str]
    qty: float
    characteristic_ref1c: Optional[str] = None
    spec_ref1c: Optional[str] = None
    structural_unit_ref1c: Optional[str] = None


@dataclass
class ProductionOrderExportMaterial:
    line_number: int
    component_item_id: int
    item_ref1c: str
    unit_ref1c: Optional[str]
    qty: float
    spec_ref1c: Optional[str] = None
    reserve_structural_unit_ref1c: Optional[str] = None


@dataclass
class ProductionOrderExportOperation:
    line_number: int
    operation_ref1c: str
    unit_ref1c: Optional[str]
    qty: float
    time_norm: float
    norm_hours: float
    structural_unit_ref1c: Optional[str] = None
    product_link_key: Optional[int] = None


@dataclass
class ProductionOrderExportEntry:
    order_id: int
    number: str
    source_planned_order_id: Optional[int] = None
    source_run_id: Optional[int] = None
    lines: List[ProductionOrderExportLine] = field(default_factory=list)
    materials: List[ProductionOrderExportMaterial] = field(default_factory=list)
    operations: List[ProductionOrderExportOperation] = field(default_factory=list)
    reserve_structural_unit_ref1c: Optional[str] = None
    product_structural_unit_ref1c: Optional[str] = None
    planned_start_date: Optional[Any] = None
    planned_finish_date: Optional[Any] = None
    target_ref_key: Optional[str] = None
    status: str = "planned"  # planned | created | existing | error | skipped
    error: Optional[str] = None
    reason: Optional[str] = None  # human-readable explanation for skipped/error


@dataclass
class ProductionOrderExportDefaults:
    organization_ref1c: str = ""
    structural_unit_ref1c: str = ""
    product_structural_unit_ref1c: str = ""
    operation_structural_unit_ref1c: str = ""


def _export_defaults(config: Dict[str, Any]) -> ProductionOrderExportDefaults:
    structural_unit = _config_ref1c(
        config,
        "default_production_structural_unit_ref1c",
        DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C,
    )
    return ProductionOrderExportDefaults(
        organization_ref1c=_config_ref1c(
            config,
            "default_organization_ref1c",
            DEFAULT_ORGANIZATION_REF1C,
        ),
        structural_unit_ref1c=structural_unit,
        product_structural_unit_ref1c=_config_ref1c(
            config,
            "default_product_structural_unit_ref1c",
            structural_unit,
        ),
        operation_structural_unit_ref1c=_config_ref1c(
            config,
            "default_operation_structural_unit_ref1c",
            structural_unit,
        ),
    )


def _short_order_number(order_id: int, run_id: Optional[int]) -> str:
    """
    Short, recognizable, unique-per-MRP-order number that fits 1C's Number
    column (per plan: 1C truncates long strings).
    Format: PP{run_id:04d}{order_id:05d}. Total length 11 chars, well under
    1C's typical Number limit. Collisions impossible while order_id is < 10^5
    within a single run_id.
    """
    run_part = (int(run_id) if run_id is not None else 0) % 10000
    return f"PP{run_part:04d}{int(order_id) % 100000:05d}"


def _default_spec_id(db: Session, product: ProductionProduct) -> Optional[int]:
    if product.spec_id:
        return int(product.spec_id)
    row = (
        db.query(DefaultSpecification)
        .filter(DefaultSpecification.item_id == int(product.item_id))
        .order_by(DefaultSpecification.id.asc())
        .first()
    )
    return int(row.spec_id) if row else None


def _workshop_id_for_product(db: Session, product: ProductionProduct, spec_id: Optional[int]) -> Optional[int]:
    state = (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == int(product.product_id))
        .first()
    )
    if state and state.workshop_id:
        return int(state.workshop_id)

    if not spec_id:
        return None
    stage_hours = (
        db.query(SpecOperation.stage_id)
        .filter(SpecOperation.spec_id == int(spec_id), SpecOperation.stage_id.isnot(None))
        .group_by(SpecOperation.stage_id)
        .order_by(func.sum(SpecOperation.time_norm).desc())
        .first()
    )
    stage_id = int(stage_hours[0]) if stage_hours else None
    if stage_id is None:
        comp_stage = (
            db.query(SpecComponent.stage_id)
            .filter(SpecComponent.spec_id == int(spec_id), SpecComponent.stage_id.isnot(None))
            .first()
        )
        stage_id = int(comp_stage[0]) if comp_stage else None
    if stage_id is None:
        return None
    resource = (
        db.query(ResourceStage.resource_id)
        .filter(ResourceStage.stage_id == int(stage_id))
        .order_by(ResourceStage.id.asc())
        .first()
    )
    return int(resource[0]) if resource else None


def _workshop_warehouse_refs(db: Session, workshop_id: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
    if not workshop_id:
        return None, None
    binding = (
        db.query(WorkshopWarehouseBinding)
        .filter(WorkshopWarehouseBinding.workshop_id == int(workshop_id))
        .first()
    )
    if not binding:
        return None, None
    workshop_ref = _clean_ref1c(binding.warehouse_ref1c) or None
    production_ref = _clean_ref1c(binding.production_warehouse_ref1c) or None
    return workshop_ref, production_ref


def _active_issue_for_product(db: Session, product_id: int) -> Optional[ProductionMaterialIssue]:
    return (
        db.query(ProductionMaterialIssue)
        .filter(
            ProductionMaterialIssue.product_id == int(product_id),
            ProductionMaterialIssue.direction == "issue",
        )
        .order_by(ProductionMaterialIssue.issue_id.desc())
        .first()
    )


def _materials_for_spec(
    db: Session,
    *,
    spec_id: Optional[int],
    order_qty: float,
    reserve_structural_unit_ref1c: Optional[str],
) -> List[ProductionOrderExportMaterial]:
    if not spec_id or order_qty <= 0:
        return []
    spec = db.query(Specification).filter(Specification.spec_id == int(spec_id)).first()
    spec_ref = _clean_ref1c(spec.spec_ref1c) if spec else None
    rows = (
        db.query(SpecComponent, Item)
        .join(Item, Item.item_id == SpecComponent.item_id)
        .filter(SpecComponent.spec_id == int(spec_id))
        .order_by(Item.item_name.asc(), SpecComponent.component_id.asc())
        .all()
    )
    result: List[ProductionOrderExportMaterial] = []
    for idx, (comp, item) in enumerate(rows, start=1):
        item_ref = _clean_ref1c(item.item_ref1c)
        qty = float(comp.quantity or 0.0) * float(order_qty)
        if not item_ref or qty <= 0:
            continue
        result.append(
            ProductionOrderExportMaterial(
                line_number=idx,
                component_item_id=int(item.item_id),
                item_ref1c=item_ref,
                unit_ref1c=_clean_ref1c(item.unit) or None,
                qty=qty,
                spec_ref1c=spec_ref or None,
                reserve_structural_unit_ref1c=reserve_structural_unit_ref1c,
            )
        )
    return result


def _operations_for_spec(
    db: Session,
    *,
    spec_id: Optional[int],
    order_qty: float,
    product_unit_ref1c: Optional[str],
    structural_unit_ref1c: Optional[str],
    product_link_key: int,
) -> List[ProductionOrderExportOperation]:
    if not spec_id or order_qty <= 0:
        return []
    rows = (
        db.query(SpecOperation, Operation)
        .join(Operation, Operation.operation_id == SpecOperation.operation_id)
        .filter(SpecOperation.spec_id == int(spec_id))
        .order_by(SpecOperation.spec_operation_id.asc())
        .all()
    )
    result: List[ProductionOrderExportOperation] = []
    for idx, (spec_op, op) in enumerate(rows, start=1):
        op_ref = _clean_ref1c(op.operation_ref1c)
        if not op_ref:
            continue
        time_norm = float(spec_op.time_norm if spec_op.time_norm is not None else op.time_norm or 0.0)
        result.append(
            ProductionOrderExportOperation(
                line_number=idx,
                operation_ref1c=op_ref,
                unit_ref1c=product_unit_ref1c,
                qty=float(order_qty),
                time_norm=time_norm,
                norm_hours=float(order_qty) * time_norm,
                structural_unit_ref1c=structural_unit_ref1c,
                product_link_key=int(product_link_key),
            )
        )
    return result


def _existing_link(db: Session, order_id: int) -> Optional[SyncLink]:
    return _find_sync_link(
        db,
        SyncLink,
        source_doctype="production_order",
        source_id=int(order_id),
        target_entity=PRODUCTION_ORDER_ENTITY,
    )


def _combine_planned_date_with_time(value: Optional[Any], time_source: str) -> Optional[str]:
    if value is None:
        return None
    try:
        source_dt = datetime.fromisoformat(str(time_source))
    except Exception:
        source_dt = datetime.now().replace(microsecond=0)
    if isinstance(value, datetime):
        planned_date = value.date()
    elif isinstance(value, date):
        planned_date = value
    else:
        try:
            planned_date = date.fromisoformat(str(value)[:10])
        except Exception:
            return None
    return datetime.combine(planned_date, source_dt.time()).replace(microsecond=0).isoformat()


def _current_moscow_datetime() -> str:
    try:
        return datetime.now(ZoneInfo("Europe/Moscow")).replace(microsecond=0).isoformat()
    except Exception:
        return datetime.now().replace(microsecond=0).isoformat()


def _collect_export_entries(
    db: Session, order_ids: List[int]
) -> Tuple[List[ProductionOrderExportEntry], List[Dict[str, Any]]]:
    """
    Load production_orders + their single ProductionProduct line + Item lookup.
    Returns (entries, skipped) where skipped contains diagnostic dicts for
    orders that can't be exported (wrong source, missing item ref, etc).
    """
    entries: List[ProductionOrderExportEntry] = []
    skipped: List[Dict[str, Any]] = []

    ids = [int(x) for x in order_ids if x is not None]
    if not ids:
        return entries, skipped

    rows = (
        db.query(ProductionOrder)
        .options(joinedload(ProductionOrder.products).joinedload(ProductionProduct.item))
        .filter(ProductionOrder.order_id.in_(ids))
        .all()
    )
    found_ids = {int(o.order_id) for o in rows}
    for missing_id in [x for x in ids if x not in found_ids]:
        skipped.append({"order_id": missing_id, "reason": "ProductionOrder не найден"})

    for order in rows:
        if str(order.source or "1c").lower() != "mrp":
            skipped.append(
                {
                    "order_id": int(order.order_id),
                    "reason": f"source='{order.source}', экспортируем только MRP-source",
                }
            )
            continue
        if bool(order.deletion_mark):
            skipped.append({"order_id": int(order.order_id), "reason": "deletion_mark=true"})
            continue

        lines: List[ProductionOrderExportLine] = []
        materials: List[ProductionOrderExportMaterial] = []
        operations: List[ProductionOrderExportOperation] = []
        reserve_ref: Optional[str] = None
        product_structural_unit_ref: Optional[str] = None
        start_dates: List[Any] = []
        finish_dates: List[Any] = []
        for product in order.products or []:
            item = product.item
            ref1c = _clean_ref1c(item.item_ref1c) if item else ""
            if not ref1c:
                skipped.append(
                    {
                        "order_id": int(order.order_id),
                        "reason": f"item_id={product.item_id}: пустой item_ref1c, "
                        "нельзя сопоставить с номенклатурой 1С",
                    }
                )
                lines = []
                break
            spec_id = _default_spec_id(db, product)
            workshop_id = _workshop_id_for_product(db, product, spec_id)
            issue = _active_issue_for_product(db, int(product.product_id))
            workshop_warehouse_ref, production_warehouse_ref = _workshop_warehouse_refs(db, workshop_id)
            material_destination_ref = (
                _clean_ref1c(issue.warehouse_ref1c) if issue else None
            ) or workshop_warehouse_ref
            product_destination_ref = production_warehouse_ref or material_destination_ref
            product_reserve_ref = material_destination_ref
            if product_reserve_ref and not reserve_ref:
                reserve_ref = product_reserve_ref
            if product_destination_ref and not product_structural_unit_ref:
                product_structural_unit_ref = product_destination_ref
            state = (
                db.query(ProductionOrderLineState)
                .filter(ProductionOrderLineState.product_id == int(product.product_id))
                .first()
            )
            if state and state.planned_start_date:
                start_dates.append(state.planned_start_date)
            if state and state.planned_finish_date:
                finish_dates.append(state.planned_finish_date)
            product_unit_ref = _clean_ref1c(item.unit) or None
            product_qty = float(product.quantity or 0.0)
            product_link_key = int(product.line_number or len(lines) + 1)
            lines.append(
                ProductionOrderExportLine(
                    line_number=product_link_key,
                    item_id=int(product.item_id),
                    item_ref1c=ref1c,
                    item_name=str(item.item_name or ""),
                    item_article=str(item.item_article or ""),
                    unit_ref1c=product_unit_ref,
                    qty=product_qty,
                    characteristic_ref1c=_clean_ref1c(product.characteristic_ref1c) or None,
                    structural_unit_ref1c=product_destination_ref,
                )
            )
            materials.extend(
                _materials_for_spec(
                    db,
                    spec_id=spec_id,
                    order_qty=product_qty,
                    reserve_structural_unit_ref1c=product_reserve_ref,
                )
            )
            operations.extend(
                _operations_for_spec(
                    db,
                    spec_id=spec_id,
                    order_qty=product_qty,
                    product_unit_ref1c=product_unit_ref,
                    structural_unit_ref1c=product_destination_ref,
                    product_link_key=product_link_key,
                )
            )

        if not lines:
            continue

        entries.append(
            ProductionOrderExportEntry(
                order_id=int(order.order_id),
                number=production_order_number(order),
                source_planned_order_id=None,
                source_run_id=int(order.source_run_id) if order.source_run_id else None,
                lines=lines,
                materials=materials,
                operations=operations,
                reserve_structural_unit_ref1c=reserve_ref,
                product_structural_unit_ref1c=product_structural_unit_ref,
                planned_start_date=min(start_dates) if start_dates else None,
                planned_finish_date=max(finish_dates) if finish_dates else None,
            )
        )

    return entries, skipped


def _build_header_payload(
    entry: ProductionOrderExportEntry,
    defaults: Optional[ProductionOrderExportDefaults] = None,
) -> Dict[str, Any]:
    defaults = defaults or ProductionOrderExportDefaults()
    comment = (
        f"PRODPLAN source=production_order/{entry.order_id}; "
        f"run={entry.source_run_id or 0}; number={entry.number}"
    )
    products = []
    for ln in entry.lines:
        product_link_key = int(ln.line_number or len(products) + 1)
        row: Dict[str, Any] = {
            "LineNumber": product_link_key,
            "Номенклатура_Key": ln.item_ref1c,
            "Количество": float(ln.qty),
            "КлючСвязи": product_link_key,
            **(
                {"СтруктурнаяЕдиница_Key": ln.structural_unit_ref1c or defaults.product_structural_unit_ref1c}
                if (ln.structural_unit_ref1c or defaults.product_structural_unit_ref1c)
                else {}
            ),
            **(
                {"Характеристика_Key": ln.characteristic_ref1c}
                if ln.characteristic_ref1c
                else {}
            ),
        }
        _add_unit_payload(row, ln.unit_ref1c)
        products.append(row)
    stock_lines = []
    for idx, ln in enumerate(entry.materials, start=1):
        row: Dict[str, Any] = {
            "LineNumber": idx,
            "Номенклатура_Key": ln.item_ref1c,
            "Количество": float(ln.qty),
        }
        _add_unit_payload(row, ln.unit_ref1c)
        if ln.spec_ref1c:
            row["Спецификация_Key"] = ln.spec_ref1c
        if ln.reserve_structural_unit_ref1c:
            row["СтруктурнаяЕдиница_Key"] = ln.reserve_structural_unit_ref1c
        row["КлючСвязи"] = idx
        stock_lines.append(row)
    operation_lines = []
    for idx, op in enumerate(entry.operations, start=1):
        row: Dict[str, Any] = {
            "LineNumber": idx,
            "Операция_Key": op.operation_ref1c,
            "КоличествоПлан": float(op.qty),
            "НормаВремени": float(op.time_norm),
            "Нормочасы": float(op.norm_hours),
            "КлючСвязи": idx,
        }
        if op.product_link_key:
            row["КлючСвязиПродукция"] = int(op.product_link_key)
        _add_unit_payload(row, op.unit_ref1c)
        if op.structural_unit_ref1c:
            row["СтруктурнаяЕдиница_Key"] = op.structural_unit_ref1c
        elif defaults.operation_structural_unit_ref1c:
            row["СтруктурнаяЕдиница_Key"] = defaults.operation_structural_unit_ref1c
        operation_lines.append(row)
    document_dt = _current_1c_datetime()
    payload: Dict[str, Any] = {
        "Number": entry.number,
        "Date": document_dt,
        "Posted": False,
        "Комментарий": comment,
        "Продукция": products,
    }
    planned_time_source = _current_moscow_datetime()
    start_dt = _combine_planned_date_with_time(entry.planned_start_date, planned_time_source)
    finish_dt = _combine_planned_date_with_time(entry.planned_finish_date or entry.planned_start_date, planned_time_source)
    if start_dt:
        payload["Старт"] = start_dt
    if finish_dt:
        payload["Финиш"] = finish_dt
    if stock_lines:
        payload["Запасы"] = stock_lines
    if operation_lines:
        payload["Операции"] = operation_lines
        payload["ЗапланированыОперации"] = True
    if defaults.organization_ref1c:
        payload["Организация_Key"] = defaults.organization_ref1c
    if defaults.structural_unit_ref1c:
        payload["СтруктурнаяЕдиница_Key"] = defaults.structural_unit_ref1c
    product_structural_unit = entry.product_structural_unit_ref1c or defaults.product_structural_unit_ref1c
    if product_structural_unit:
        payload["СтруктурнаяЕдиницаПродукции_Key"] = product_structural_unit
    if defaults.operation_structural_unit_ref1c:
        payload["СтруктурнаяЕдиницаОпераций_Key"] = defaults.operation_structural_unit_ref1c
    payload["СтруктурнаяЕдиницаРезерв_Key"] = entry.reserve_structural_unit_ref1c or EMPTY_REF1C
    return payload


def _upsert_link(
    db: Session,
    *,
    entry: ProductionOrderExportEntry,
    payload_hash: str,
    target_ref_key: Optional[str],
    status: str,
    last_error: Optional[str],
) -> None:
    _upsert_sync_link(
        db,
        SyncLink,
        source_doctype="production_order",
        source_id=int(entry.order_id),
        target_entity=PRODUCTION_ORDER_ENTITY,
        target_number=entry.number,
        payload_hash=payload_hash,
        target_ref_key=target_ref_key,
        status=status,
        last_error=last_error,
    )


def export_production_orders_to_1c(
    db: Session,
    order_ids: List[int],
    *,
    dry_run: bool = True,
    allow_production: bool = False,
) -> Dict[str, Any]:
    """
    Export the given internal MRP production_orders to 1C as
    Document_ЗаказНаПроизводство with Posted=false, then operationally posts
    each created 1C document.

    Default is `dry_run=True` per plan safety rule. Caller must pass
    `dry_run=False` to actually write. A second guard refuses to write to a
    base_url that doesn't look like a demo DB unless `allow_production=True`
    is also passed.
    """
    entries, skipped = _collect_export_entries(db, list(order_ids))

    # Pre-flight: split entries into eligible / already-linked.
    eligible: List[ProductionOrderExportEntry] = []
    already_linked: List[ProductionOrderExportEntry] = []
    for entry in entries:
        link = _existing_link(db, entry.order_id)
        if link and link.status == "success" and (link.target_ref_key or ""):
            entry.status = "existing"
            entry.target_ref_key = str(link.target_ref_key)
            entry.reason = "уже выгружен в 1С (sync_link)"
            already_linked.append(entry)
            continue
        if link and _clean_ref1c(link.target_ref_key):
            entry.target_ref_key = _clean_ref1c(link.target_ref_key)
            entry.reason = "повторная отправка: 1С-документ уже был создан, обновляем реквизиты и проводим"
        # Also treat orders with order_ref1c already set as existing — defensive
        # for the case where sync_link wasn't populated by an older export.
        order_row = db.query(ProductionOrder).filter(ProductionOrder.order_id == entry.order_id).one()
        if _clean_ref1c(order_row.order_ref1c):
            entry.status = "existing"
            entry.target_ref_key = _clean_ref1c(order_row.order_ref1c)
            entry.reason = "production_orders.order_ref1c уже стоит"
            already_linked.append(entry)
            continue
        eligible.append(entry)

    summary: Dict[str, Any] = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "entity": PRODUCTION_ORDER_ENTITY,
        "orders_requested": len(order_ids),
        "orders_eligible": len(eligible),
        "orders_already_linked": len(already_linked),
        "orders_created": 0,
        "orders_error": 0,
        "skipped_rows": skipped,
        "entries": [],
    }

    config = _load_odata_config()
    defaults = _export_defaults(config)

    # Build payloads for the dry-run preview.
    payloads: List[Dict[str, Any]] = []
    for entry in eligible:
        payload = _build_header_payload(entry, defaults)
        payloads.append({"order_id": entry.order_id, "number": entry.number, "payload": payload})

    if dry_run:
        summary["entries"] = [asdict(e) for e in entries]
        summary["payloads"] = payloads
        return summary

    # ----- real write below -----
    client = _create_odata_client(
        config,
        OData1CClient,
        allow_production=allow_production,
        require_demo_base=True,
    )

    def _mark_success(entry: ProductionOrderExportEntry, ref_key: str) -> None:
        _post_document_operational(
            client,
            entity=PRODUCTION_ORDER_ENTITY,
            ref_key=ref_key,
            unpost_first=False,
        )
        # Stamp success on production_orders.order_ref1c so the journal stops
        # treating it as MRP-only.
        order_row = db.query(ProductionOrder).filter(ProductionOrder.order_id == entry.order_id).one()
        order_row.order_ref1c = ref_key

    created, errored = _post_export_entries(
        db,
        entries=zip(eligible, payloads),
        client=client,
        target_entity=PRODUCTION_ORDER_ENTITY,
        missing_ref_error=f"1C did not return Ref_Key for the new {PRODUCTION_ORDER_ENTITY}",
        upsert_link=lambda **kwargs: _upsert_link(db, **kwargs),
        on_success=_mark_success,
        log_error=lambda entry: f"[1C production export] order_id={entry.order_id} failed: {entry.error}",
    )

    summary["orders_created"] = created
    summary["orders_error"] = errored
    summary["entries"] = [asdict(e) for e in entries]
    summary["status"] = "ok" if errored == 0 else "partial_error"
    return summary

"""Export ProductionManufacture records to 1C as Document_СборкаЗапасов.

Pattern: mirrors one_c_production_order_export.py / one_c_stock_transfer_export.py.
Documentation: .docs/one_c_export_from_prodplan.md.

Safety per the doc:
1. Default dry_run=True.
2. Refuse non-demo base_url unless allow_production=True.
3. Posted=false on create, then conduct through standard 1C Post operation.
4. Idempotency via sync_link (source_doctype='manufacture').

A ProductionManufacture represents one "Произвести" event on a
production_products line. In 1C this maps to Document_СборкаЗапасов
("Сборка/выпуск") that links back to the parent Document_ЗаказНаПроизводство
via ЗаказНаПроизводство_Key and lists the finished product in the Продукция
table part.

Payload includes header + Продукция[] + Запасы[] so the 1C document is ready
for posting and does not rely on UI-side autofill.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from ..models import (
    Item,
    DefaultSpecification,
    ProductionManufacture,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionStage,
    ResourceStage,
    SpecComponent,
    SpecOperation,
    Specification,
    StockWarehouse,
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
from .one_c_document_numbers import manufacture_number
from .one_c_production_order_export import export_production_orders_to_1c


MANUFACTURE_ENTITY = "Document_СборкаЗапасов"
EMPTY_REF1C = "00000000-0000-0000-0000-000000000000"


@dataclass
class ManufactureExportEntry:
    manufacture_id: int
    product_id: int
    order_id: int
    order_ref1c: Optional[str]
    item_ref1c: str
    item_name: str
    item_article: str
    unit_ref1c: Optional[str]
    qty: float
    spec_id: Optional[int] = None
    spec_ref1c: Optional[str] = None
    material_structural_unit_ref1c: Optional[str] = None
    product_structural_unit_ref1c: Optional[str] = None
    stage_ref1c: Optional[str] = None
    completed_stage_refs: List[str] = field(default_factory=list)
    materials: List[Dict[str, Any]] = field(default_factory=list)
    executor: Optional[str] = None
    number: str = ""
    target_ref_key: Optional[str] = None
    unpost_before_patch: bool = False
    status: str = "planned"
    error: Optional[str] = None
    reason: Optional[str] = None


def _short_manufacture_number(manufacture_id: int) -> str:
    """Short, recognizable, unique number that fits 1C's Number column."""
    return f"PM{int(manufacture_id) % 1_000_000_000:09d}"


def _existing_link(db: Session, manufacture_id: int) -> Optional[SyncLink]:
    return _find_sync_link(
        db,
        SyncLink,
        source_doctype="manufacture",
        source_id=int(manufacture_id),
        target_entity=MANUFACTURE_ENTITY,
    )


def _main_stage_id_for_spec(db: Session, spec_id: Optional[int]) -> Optional[int]:
    if not spec_id:
        return None
    stage_hours = (
        db.query(SpecOperation.stage_id)
        .filter(SpecOperation.spec_id == int(spec_id), SpecOperation.stage_id.isnot(None))
        .order_by(SpecOperation.time_norm.desc(), SpecOperation.spec_operation_id.asc())
        .first()
    )
    if stage_hours:
        return int(stage_hours.stage_id)
    component_stage = (
        db.query(SpecComponent.stage_id)
        .filter(SpecComponent.spec_id == int(spec_id), SpecComponent.stage_id.isnot(None))
        .order_by(SpecComponent.component_id.asc())
        .first()
    )
    return int(component_stage.stage_id) if component_stage else None


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


def _binding_for_product(db: Session, product: ProductionProduct) -> Optional[WorkshopWarehouseBinding]:
    state_workshop_id = None
    state = getattr(product, "control_state", None)
    if state and state.workshop_id:
        state_workshop_id = int(state.workshop_id)

    workshop_id = state_workshop_id
    if workshop_id is None:
        stage_id = _main_stage_id_for_spec(db, _default_spec_id(db, product))
        if stage_id:
            resource_stage = (
                db.query(ResourceStage)
                .filter(ResourceStage.stage_id == int(stage_id))
                .order_by(ResourceStage.id.asc())
                .first()
            )
            if resource_stage:
                workshop_id = int(resource_stage.resource_id)

    if workshop_id is None:
        return None
    return (
        db.query(WorkshopWarehouseBinding)
        .filter(WorkshopWarehouseBinding.workshop_id == int(workshop_id))
        .one_or_none()
    )


def _component_rows(db: Session, product: ProductionProduct, qty: float, spec_id: Optional[int]) -> List[Dict[str, Any]]:
    if not spec_id:
        return []
    rows: List[Dict[str, Any]] = []
    spec = db.query(Specification).filter(Specification.spec_id == int(spec_id)).first()
    spec_ref = _clean_ref1c(getattr(spec, "spec_ref1c", None)) if spec else None
    components = (
        db.query(SpecComponent, Item)
        .join(Item, Item.item_id == SpecComponent.item_id)
        .filter(SpecComponent.spec_id == int(spec_id))
        .order_by(SpecComponent.component_id.asc())
        .all()
    )
    for idx, (component, item) in enumerate(components, start=1):
        item_ref = _clean_ref1c(item.item_ref1c)
        if not item_ref:
            continue
        row: Dict[str, Any] = {
            "LineNumber": idx,
            "Номенклатура_Key": item_ref,
            "Количество": float(component.quantity or 0) * float(qty or 0),
            "КлючСвязи": idx,
        }
        _add_unit_payload(row, item.unit)
        if spec_ref:
            row["Спецификация_Key"] = spec_ref
        rows.append(row)
    return rows


def _completion_stage_ref1c(db: Session) -> Optional[str]:
    row = (
        db.query(ProductionStage)
        .filter(
            or_(
                ProductionStage.stage_name.ilike("%заверш%"),
                ProductionStage.stage_name.like("%Заверш%"),
            )
        )
        .order_by(ProductionStage.stage_order.asc().nullslast(), ProductionStage.stage_id.asc())
        .first()
    )
    return _clean_ref1c(getattr(row, "stage_ref1c", None)) or None


def _completed_stage_refs_for_spec(db: Session, spec_id: Optional[int]) -> List[str]:
    refs: List[str] = []
    if spec_id:
        for (stage_ref,) in (
            db.query(ProductionStage.stage_ref1c)
            .join(SpecOperation, SpecOperation.stage_id == ProductionStage.stage_id)
            .filter(SpecOperation.spec_id == int(spec_id), ProductionStage.stage_ref1c.isnot(None))
            .order_by(SpecOperation.spec_operation_id.asc())
            .all()
        ):
            ref = _clean_ref1c(stage_ref)
            if ref and ref not in refs:
                refs.append(ref)
        for (stage_ref,) in (
            db.query(ProductionStage.stage_ref1c)
            .join(SpecComponent, SpecComponent.stage_id == ProductionStage.stage_id)
            .filter(SpecComponent.spec_id == int(spec_id), ProductionStage.stage_ref1c.isnot(None))
            .order_by(SpecComponent.component_id.asc())
            .all()
        ):
            ref = _clean_ref1c(stage_ref)
            if ref and ref not in refs:
                refs.append(ref)
    completion_ref = _completion_stage_ref1c(db)
    if completion_ref and completion_ref not in refs:
        refs.append(completion_ref)
    return refs


def _inherit_structural_units_from_parent_order(client: OData1CClient, entries: List[ManufactureExportEntry]) -> None:
    """
    Fill manufacture warehouses from the linked 1C production order when local
    PRODPLAN data cannot resolve a workshop. This happens for rows synced back
    from 1C without spec_id/workshop_id: the parent order still carries the
    authoritative reserve/product structural units.
    """
    need_refs = {
        str(entry.order_ref1c)
        for entry in entries
        if entry.order_ref1c
    }
    if not need_refs:
        return

    by_ref: Dict[str, Dict[str, Any]] = {}
    for ref in sorted(need_refs):
        try:
            by_ref[ref] = client._make_request(
                f"Document_ЗаказНаПроизводство(guid'{ref}')",
                params={
                    "$format": "json",
                    "$select": (
                        "Ref_Key,СтруктурнаяЕдиницаРезерв_Key,"
                        "СтруктурнаяЕдиницаПродукции_Key,Продукция,Запасы"
                    ),
                },
                timeout=60,
                retries=1,
            )
        except Exception:
            # Export can still proceed with the local/default fallback; the
            # actual post/patch will surface any critical 1C connectivity issue.
            continue

    for entry in entries:
        doc = by_ref.get(str(entry.order_ref1c or ""))
        if not doc:
            continue
        reserve_ref = _clean_ref1c(doc.get("СтруктурнаяЕдиницаРезерв_Key"))
        product_ref = _clean_ref1c(doc.get("СтруктурнаяЕдиницаПродукции_Key"))
        if not reserve_ref:
            for row in doc.get("Запасы") or []:
                reserve_ref = _clean_ref1c(row.get("СтруктурнаяЕдиница_Key"))
                if reserve_ref:
                    break
        if not product_ref:
            for row in doc.get("Продукция") or []:
                product_ref = _clean_ref1c(row.get("СтруктурнаяЕдиница_Key"))
                if product_ref:
                    break
        if reserve_ref and not entry.material_structural_unit_ref1c:
            entry.material_structural_unit_ref1c = reserve_ref
        if product_ref:
            entry.product_structural_unit_ref1c = product_ref


def _collect_export_entries(
    db: Session, manufacture_ids: List[int]
) -> Tuple[List[ManufactureExportEntry], List[Dict[str, Any]]]:
    entries: List[ManufactureExportEntry] = []
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
            skipped.append(
                {
                    "manufacture_id": int(m.manufacture_id),
                    "reason": "status='cancelled', экспорт не нужен",
                }
            )
            continue

        # Contract rule (.docs/one_c_export_from_prodplan.md): child documents
        # (here: Document_СборкаЗапасов) must carry ДокументОснование pointing
        # at Document_ЗаказНаПроизводство. Without a parent order_ref1c, the
        # сборка cannot be exported.
        order_ref = _clean_ref1c(m.order.order_ref1c) if m.order else None
        if not order_ref:
            skipped.append(
                {
                    "manufacture_id": int(m.manufacture_id),
                    "reason": (
                        "order_ref1c пуст — родительский ЗаказНаПроизводство "
                        "ещё не выгружен в 1С, основание не сформировать"
                    ),
                }
            )
            continue

        item = m.product.item if m.product else None
        item_ref = _clean_ref1c(item.item_ref1c) if item else ""
        if not item_ref:
            skipped.append(
                {
                    "manufacture_id": int(m.manufacture_id),
                    "reason": "item_ref1c пустой, нельзя сопоставить с номенклатурой 1С",
                }
            )
            continue

        resolved_spec_id = _default_spec_id(db, m.product) if m.product else None
        resolved_spec = (
            db.query(Specification).filter(Specification.spec_id == int(resolved_spec_id)).first()
            if resolved_spec_id
            else None
        )
        entries.append(
            ManufactureExportEntry(
                manufacture_id=int(m.manufacture_id),
                product_id=int(m.product_id),
                order_id=int(m.order_id),
                order_ref1c=order_ref,
                item_ref1c=item_ref,
                item_name=str(item.item_name or "") if item else "",
                item_article=str(item.item_article or "") if item else "",
                unit_ref1c=_clean_ref1c(item.unit) if item else None,
                qty=float(m.qty or 0),
                spec_id=resolved_spec_id,
                spec_ref1c=_clean_ref1c(getattr(resolved_spec, "spec_ref1c", None)) or None,
                executor=str(m.executor) if m.executor else None,
                number=manufacture_number(db, m),
                completed_stage_refs=_completed_stage_refs_for_spec(db, resolved_spec_id),
            )
        )
        entry = entries[-1]
        if m.product:
            binding = _binding_for_product(db, m.product)
            if binding:
                entry.material_structural_unit_ref1c = _clean_ref1c(binding.warehouse_ref1c) or None
                entry.product_structural_unit_ref1c = (
                    _clean_ref1c(binding.production_warehouse_ref1c)
                    or _clean_ref1c(binding.warehouse_ref1c)
                    or None
                )
            entry.materials = _component_rows(db, m.product, float(m.qty or 0), entry.spec_id)

    return entries, skipped


_BALANCE_FILTER_CHUNK = 15


def _live_unit_balances(
    client: OData1CClient,
    item_refs: List[str],
    unit_ref1c: str,
) -> Optional[Dict[str, float]]:
    """
    Live 1C stock of the given items on one structural unit, summed over
    cells/organizations: {item_ref1c: qty}. Items without a register row are
    simply absent (= 0 on the unit).

    Returns None when the balance cannot be read (client without get_all or a
    failed OData request) — the caller must fail open and let 1C validate at
    posting time, otherwise a connectivity hiccup would block all exports.
    """
    get_all = getattr(client, "get_all", None)
    if get_all is None:
        return None
    refs = [ref for ref in item_refs if ref]
    if not refs:
        return {}
    entity = (
        "AccumulationRegister_ЗапасыНаСкладах/Balance("
        f"Period=datetime'{_current_1c_datetime()}',"
        "Dimensions='Номенклатура,СтруктурнаяЕдиница')"
    )
    from .one_c_stock_transfer_export import _qty_from_balance_row

    result: Dict[str, float] = {}
    for start in range(0, len(refs), _BALANCE_FILTER_CHUNK):
        chunk = refs[start : start + _BALANCE_FILTER_CHUNK]
        item_filter = " or ".join(f"Номенклатура_Key eq guid'{ref}'" for ref in chunk)
        filter_query = f"СтруктурнаяЕдиница_Key eq guid'{unit_ref1c}' and ({item_filter})"
        try:
            rows = get_all(
                entity,
                filter_query=filter_query,
                top=200,
                max_records=500,
                max_pages=10,
                order_by=None,
            )
        except Exception:
            return None
        for row in rows or []:
            ref = _clean_ref1c(row.get("Номенклатура_Key"))
            if ref:
                result[ref] = result.get(ref, 0.0) + _qty_from_balance_row(row)
    return result


def _entry_material_requirements(
    entry: ManufactureExportEntry,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Dict[str, float]]:
    """(material unit ref, {item_ref1c: write-off qty}) for one manufacture."""
    cfg = config or {}
    default_structural_unit = _config_ref1c(
        cfg,
        "default_production_structural_unit_ref1c",
        DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C,
    )
    material_unit = _clean_ref1c(entry.material_structural_unit_ref1c or default_structural_unit)
    needed: Dict[str, float] = {}
    for row in entry.materials or []:
        ref = _clean_ref1c(row.get("Номенклатура_Key"))
        qty = float(row.get("Количество") or 0.0)
        if ref and qty > 1e-9:
            needed[ref] = needed.get(ref, 0.0) + qty
    return material_unit or None, needed


def _balance_shortfall_message(
    db: Session,
    material_unit: str,
    shortfalls: List[Tuple[str, float, float]],
) -> str:
    names = {
        _clean_ref1c(item_ref): str(name or article or code or "")
        for item_ref, name, article, code in (
            db.query(Item.item_ref1c, Item.item_name, Item.item_article, Item.item_code)
            .filter(Item.item_ref1c.in_([ref for ref, _, _ in shortfalls]))
            .all()
        )
    }
    warehouse_row = (
        db.query(StockWarehouse.warehouse_name)
        .filter(StockWarehouse.warehouse_ref1c == material_unit)
        .first()
    )
    unit_name = str(warehouse_row[0]) if warehouse_row and warehouse_row[0] else material_unit
    details = "; ".join(
        f"{names.get(ref) or ref}: нужно {qty:g}, в 1С {have:g}"
        for ref, qty, have in shortfalls[:10]
    )
    return (
        f"Недостаточно остатков в 1С на складе материалов «{unit_name}»: {details}. "
        "СборкаЗапасов не выгружена — переместите недостающее в 1С и повторите выпуск."
    )


def _apply_balance_guard(
    db: Session,
    client: OData1CClient,
    eligible: List[ManufactureExportEntry],
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[List[ManufactureExportEntry], int]:
    """
    PRODPLAN reservations and the 1C ledger are separate books: manual 1C
    documents, re-posted transfers or cell-tracked warehouses can leave the
    workshop unit short even when the local kit looks complete. Posting
    Document_СборкаЗапасов would then fail with an opaque 1C error after the
    document is already created. Refuse such exports up front with a
    human-readable message; returns (exportable_entries, blocked_count).

    The batch is checked against one shared live balance per material unit:
    every approved entry consumes its write-off from the remaining pool, so
    two manufactures that fit individually but not together do not slip
    through. Entries repairing an existing 1C document (target_ref_key set)
    are skipped — if that document was posted, its write-off has already left
    the register and the live balance would double-count it.
    """
    requirements: List[Tuple[ManufactureExportEntry, Optional[str], Dict[str, float]]] = []
    refs_by_unit: Dict[str, set] = {}
    for entry in eligible:
        if _clean_ref1c(entry.target_ref_key):
            requirements.append((entry, None, {}))
            continue
        unit, needed = _entry_material_requirements(entry, config)
        if unit and needed:
            refs_by_unit.setdefault(unit, set()).update(needed.keys())
        requirements.append((entry, unit, needed))

    balances_by_unit: Dict[str, Optional[Dict[str, float]]] = {
        unit: _live_unit_balances(client, sorted(refs), unit)
        for unit, refs in refs_by_unit.items()
    }

    remaining: Dict[Tuple[str, str], float] = {}
    exportable: List[ManufactureExportEntry] = []
    blocked = 0
    for entry, unit, needed in requirements:
        if not unit or not needed:
            exportable.append(entry)
            continue
        unit_balances = balances_by_unit.get(unit)
        if unit_balances is None:
            # Balance unreadable: fail open, 1C validates at posting time.
            exportable.append(entry)
            continue
        shortfalls: List[Tuple[str, float, float]] = []
        for ref, qty in needed.items():
            key = (unit, ref)
            if key not in remaining:
                remaining[key] = unit_balances.get(ref, 0.0)
            if remaining[key] + 1e-6 < qty:
                shortfalls.append((ref, qty, remaining[key]))
        if shortfalls:
            error = _balance_shortfall_message(db, unit, shortfalls)
            entry.status = "error"
            entry.error = error
            m_row = (
                db.query(ProductionManufacture)
                .filter(ProductionManufacture.manufacture_id == entry.manufacture_id)
                .one()
            )
            m_row.export_error = error
            blocked += 1
            continue
        for ref, qty in needed.items():
            remaining[(unit, ref)] -= qty
        exportable.append(entry)
    if blocked:
        db.commit()
    return exportable, blocked


def _build_header_payload(entry: ManufactureExportEntry, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = config or {}
    organization_ref = _config_ref1c(cfg, "default_organization_ref1c", DEFAULT_ORGANIZATION_REF1C)
    default_structural_unit = _config_ref1c(
        cfg,
        "default_production_structural_unit_ref1c",
        DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C,
    )
    product_structural_unit = entry.product_structural_unit_ref1c or _config_ref1c(
        cfg,
        "default_product_structural_unit_ref1c",
        default_structural_unit,
    )
    material_structural_unit = entry.material_structural_unit_ref1c or default_structural_unit
    comment = (
        f"PRODPLAN source=manufacture/{entry.manufacture_id}; "
        f"order_id={entry.order_id}; product_id={entry.product_id}; "
        f"number={entry.number}"
    )
    if entry.executor:
        comment += f"; executor={entry.executor}"
    product_row: Dict[str, Any] = {
        "LineNumber": 1,
        "Номенклатура_Key": entry.item_ref1c,
        "Количество": float(entry.qty),
        "КлючСвязи": 1,
    }
    _add_unit_payload(product_row, entry.unit_ref1c)
    if entry.spec_ref1c:
        product_row["Спецификация_Key"] = entry.spec_ref1c
    if product_structural_unit:
        product_row["СтруктурнаяЕдиница_Key"] = product_structural_unit
    if default_structural_unit:
        product_row["ПодразделениеЗавершающегоЭтапа_Key"] = default_structural_unit
    products = [product_row]
    material_rows: List[Dict[str, Any]] = []
    for idx, material in enumerate(entry.materials, start=1):
        row = dict(material)
        row["LineNumber"] = idx
        if material_structural_unit:
            row["СтруктурнаяЕдиница_Key"] = material_structural_unit
        material_rows.append(row)
    payload: Dict[str, Any] = {
        "Number": entry.number,
        "Date": _current_1c_datetime(),
        "Posted": False,
        "Комментарий": comment,
        "Продукция": products,
    }
    if material_rows:
        payload["Запасы"] = material_rows
    if entry.completed_stage_refs:
        payload["ВыполненныеЭтапы"] = [
            {
                "LineNumber": idx,
                "КлючСвязи": 1,
                "Этап_Key": stage_ref,
            }
            for idx, stage_ref in enumerate(entry.completed_stage_refs, start=1)
        ]
    if organization_ref:
        payload["Организация_Key"] = organization_ref
    if default_structural_unit:
        payload["СтруктурнаяЕдиница_Key"] = default_structural_unit
    if product_structural_unit:
        payload["СтруктурнаяЕдиницаПродукции_Key"] = product_structural_unit
    if material_structural_unit:
        payload["СтруктурнаяЕдиницаЗапасов_Key"] = material_structural_unit
    # 1C UNF links assembly to production order through this dedicated field.
    # The generic composite ДокументОснование on Document_СборкаЗапасов does
    # not accept Document_ЗаказНаПроизводство in the current OData metadata.
    assert entry.order_ref1c, "manufacture export requires order_ref1c basis"
    payload["ЗаказНаПроизводство_Key"] = entry.order_ref1c
    return payload


def _upsert_link(
    db: Session,
    *,
    entry: ManufactureExportEntry,
    payload_hash: str,
    target_ref_key: Optional[str],
    status: str,
    last_error: Optional[str],
) -> None:
    _upsert_sync_link(
        db,
        SyncLink,
        source_doctype="manufacture",
        source_id=int(entry.manufacture_id),
        target_entity=MANUFACTURE_ENTITY,
        target_number=entry.number,
        payload_hash=payload_hash,
        target_ref_key=target_ref_key,
        status=status,
        last_error=last_error,
    )


def _chain_export_parent_orders(
    db: Session,
    manufacture_ids: List[int],
    *,
    dry_run: bool,
    allow_production: bool,
) -> Optional[Dict[str, Any]]:
    """
    Per .docs/one_c_export_from_prodplan.md: a Document_СборкаЗапасов MUST be
    created in 1C on the basis of a Document_ЗаказНаПроизводство. So before
    exporting any manufacture, ensure its parent production_order is in 1C —
    auto-export the missing ones first.
    """
    parent_ids_rows = (
        db.query(ProductionOrder.order_id)
        .join(ProductionManufacture, ProductionManufacture.order_id == ProductionOrder.order_id)
        .filter(ProductionManufacture.manufacture_id.in_(list(manufacture_ids)))
        .filter(
            (ProductionOrder.order_ref1c.is_(None))
            | (ProductionOrder.order_ref1c == "")
            | (ProductionOrder.order_ref1c == EMPTY_REF1C)
        )
        .distinct()
        .all()
    )
    parent_ids = [int(r[0]) for r in parent_ids_rows]
    if not parent_ids:
        return None
    return export_production_orders_to_1c(
        db,
        parent_ids,
        dry_run=dry_run,
        allow_production=allow_production,
    )


def export_manufactures_to_1c(
    db: Session,
    manufacture_ids: List[int],
    *,
    dry_run: bool = True,
    allow_production: bool = False,
) -> Dict[str, Any]:
    """
    Export selected ProductionManufactures to 1C as Document_СборкаЗапасов
    with Posted=false. Idempotent via sync_link.

    Enforces the chain rule: any parent ProductionOrder that is not yet in 1C
    is exported first (so the manufacture can carry a valid ДокументОснование).
    """
    parent_export = _chain_export_parent_orders(
        db, list(manufacture_ids), dry_run=dry_run, allow_production=allow_production
    )
    entries, skipped = _collect_export_entries(db, list(manufacture_ids))

    eligible: List[ManufactureExportEntry] = []
    already_linked: List[ManufactureExportEntry] = []
    for entry in entries:
        link = _existing_link(db, entry.manufacture_id)
        if link and _clean_ref1c(link.target_ref_key):
            entry.target_ref_key = _clean_ref1c(link.target_ref_key)
            entry.unpost_before_patch = True
            entry.reason = "повторная отправка: 1С-документ уже был создан, обновляем реквизиты и проводим"
        m_row = (
            db.query(ProductionManufacture)
            .filter(ProductionManufacture.manufacture_id == entry.manufacture_id)
            .one()
        )
        manufacture_ref = _clean_ref1c(m_row.exported_ref1c)
        if manufacture_ref:
            entry.target_ref_key = entry.target_ref_key or manufacture_ref
            entry.unpost_before_patch = True
            entry.reason = "повторная отправка: 1С-документ уже был создан, обновляем реквизиты и проводим"
        eligible.append(entry)

    summary: Dict[str, Any] = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "entity": MANUFACTURE_ENTITY,
        "manufactures_requested": len(manufacture_ids),
        "manufactures_eligible": len(eligible),
        "manufactures_already_linked": len(already_linked),
        "manufactures_created": 0,
        "manufactures_error": 0,
        "skipped_rows": skipped,
        "entries": [],
        "parent_orders_export": parent_export,
    }

    config = _load_odata_config()
    if dry_run:
        payloads: List[Dict[str, Any]] = []
        for entry in eligible:
            payload = _build_header_payload(entry, config)
            payloads.append(
                {"manufacture_id": entry.manufacture_id, "number": entry.number, "payload": payload}
            )
        summary["entries"] = [asdict(e) for e in entries]
        summary["payloads"] = payloads
        return summary

    client = _create_odata_client(
        config,
        OData1CClient,
        allow_production=allow_production,
        require_demo_base=True,
    )
    _inherit_structural_units_from_parent_order(client, eligible)

    # Pre-flight: refuse exports whose component write-off cannot be covered
    # by the live 1C balance of the material unit. Catches PRODPLAN/1C ledger
    # divergence before a document is created instead of an opaque posting
    # failure after.
    eligible, blocked = _apply_balance_guard(db, client, eligible, config)

    payloads: List[Dict[str, Any]] = []
    for entry in eligible:
        payload = _build_header_payload(entry, config)
        payloads.append(
            {"manufacture_id": entry.manufacture_id, "number": entry.number, "payload": payload}
        )

    def _mark_success(entry: ManufactureExportEntry, ref_key: str) -> None:
        _post_document_operational(
            client,
            entity=MANUFACTURE_ENTITY,
            ref_key=ref_key,
            unpost_first=False,
        )
        m_row = (
            db.query(ProductionManufacture)
            .filter(ProductionManufacture.manufacture_id == entry.manufacture_id)
            .one()
        )
        m_row.status = "exported"
        m_row.exported_ref1c = ref_key
        m_row.exported_at = datetime.utcnow()
        m_row.export_error = None

    def _mark_error(entry: ManufactureExportEntry, error: str) -> None:
        m_row = (
            db.query(ProductionManufacture)
            .filter(ProductionManufacture.manufacture_id == entry.manufacture_id)
            .one()
        )
        created_ref = _clean_ref1c(getattr(entry, "target_ref_key", None))
        if created_ref:
            m_row.status = "error"
            m_row.exported_ref1c = created_ref
            m_row.exported_at = datetime.utcnow()
        m_row.export_error = error

    created, errored = _post_export_entries(
        db,
        entries=zip(eligible, payloads),
        client=client,
        target_entity=MANUFACTURE_ENTITY,
        missing_ref_error=f"1C did not return Ref_Key for new {MANUFACTURE_ENTITY}",
        upsert_link=lambda **kwargs: _upsert_link(db, **kwargs),
        on_success=_mark_success,
        on_error=_mark_error,
        log_error=lambda entry: (
            f"[1C manufacture export] manufacture_id={entry.manufacture_id} failed: {entry.error}"
        ),
    )

    summary["manufactures_created"] = created
    summary["manufactures_blocked"] = blocked
    summary["manufactures_error"] = errored + blocked
    summary["entries"] = [asdict(e) for e in entries]
    summary["status"] = "ok" if errored + blocked == 0 else "partial_error"
    return summary

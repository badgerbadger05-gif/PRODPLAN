from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..models import Item, PlannedPurchase, Unit
from .one_c_export_common import (
    clean_ref1c as _clean_ref1c,
    create_odata_client as _create_odata_client,
    fmt_1c_datetime as _fmt_1c_datetime,
)
from .odata_config import load_odata_config as _load_odata_config
from .odata_client import OData1CClient

PURCHASE_ORDER_ENTITY = "Document_ЗаказПоставщику"
EMPTY_REF1C = "00000000-0000-0000-0000-000000000000"
UNIT_TYPE_1C = "StandardODATA.Catalog_КлассификаторЕдиницИзмерения"


@dataclass
class PurchaseOrderExportLine:
    item_id: int
    item_ref1c: str
    item_name: str
    item_article: str
    unit_ref1c: str
    unit_name: str
    qty: float
    need_date: Optional[str]
    order_date: Optional[str]


@dataclass
class PurchaseOrderExportGroup:
    supplier_ref1c: str
    number: str
    lines: List[PurchaseOrderExportLine] = field(default_factory=list)
    target_ref_key: Optional[str] = None
    status: str = "planned"
    error: Optional[str] = None


def _short_order_number(run_id: int, index: int) -> str:
    return f"PP{int(run_id) % 100000:05d}{int(index) % 1000:03d}"


def _existing_order_by_number(client: OData1CClient, number: str) -> Optional[Dict[str, Any]]:
    rows = client.get_all(
        PURCHASE_ORDER_ENTITY,
        filter_query=f"Number eq '{number}'",
        select_fields=["Ref_Key", "Number", "Контрагент_Key", "Комментарий", "Запасы"],
        top=1,
        max_records=1,
        max_pages=1,
        order_by=None,
    )
    return rows[0] if rows else None


def _collect_purchase_groups(
    db: Session,
    run_id: int,
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    purchase_ids: Optional[List[int]] = None,
) -> Tuple[List[PurchaseOrderExportGroup], List[Dict[str, Any]]]:
    q = (
        db.query(
            PlannedPurchase.purchase_id,
            PlannedPurchase.item_id,
            PlannedPurchase.qty,
            PlannedPurchase.need_date,
            PlannedPurchase.order_date,
            PlannedPurchase.supplier_ref1c,
            Item.item_ref1c,
            Item.supplier_ref1c.label("item_supplier_ref1c"),
            Item.item_name,
            Item.item_article,
            Item.unit,
            Unit.short_name,
            Unit.unit_name,
            Unit.unit_code,
        )
        .join(Item, PlannedPurchase.item_id == Item.item_id)
        .outerjoin(Unit, Item.unit == Unit.unit_ref1c)
        .filter(PlannedPurchase.run_id == int(run_id))
    )
    if date_from:
        q = q.filter(PlannedPurchase.bucket_date >= date.fromisoformat(date_from))
    if date_to:
        q = q.filter(PlannedPurchase.bucket_date <= date.fromisoformat(date_to))
    selected_ids = sorted({int(pid) for pid in (purchase_ids or []) if int(pid) > 0})
    if selected_ids:
        q = q.filter(PlannedPurchase.purchase_id.in_(selected_ids))

    missing: List[Dict[str, Any]] = []
    grouped: Dict[str, Dict[Tuple[int, str, Optional[str]], PurchaseOrderExportLine]] = {}

    for row in q.all():
        supplier_ref = (row.supplier_ref1c or row.item_supplier_ref1c or "").strip()
        supplier_ref = _clean_ref1c(supplier_ref)
        item_ref = _clean_ref1c(row.item_ref1c)
        qty = float(row.qty or 0.0)
        if qty <= 0:
            continue
        if not supplier_ref or not item_ref:
            missing.append(
                {
                    "purchase_id": int(row.purchase_id),
                    "item_id": int(row.item_id),
                    "item_name": row.item_name,
                    "missing_supplier": not bool(supplier_ref),
                    "missing_item_ref1c": not bool(item_ref),
                }
            )
            continue

        unit_ref1c = _clean_ref1c(row.unit)
        unit_name = (row.short_name or row.unit_name or row.unit_code or row.unit or "").strip()
        need_iso = row.need_date.isoformat() if row.need_date else None
        order_iso = row.order_date.isoformat() if row.order_date else None
        key = (int(row.item_id), unit_ref1c or unit_name, need_iso)
        supplier_bucket = grouped.setdefault(supplier_ref, {})
        if key not in supplier_bucket:
            supplier_bucket[key] = PurchaseOrderExportLine(
                item_id=int(row.item_id),
                item_ref1c=item_ref,
                item_name=row.item_name or "",
                item_article=row.item_article or "",
                unit_ref1c=unit_ref1c,
                unit_name=unit_name,
                qty=0.0,
                need_date=need_iso,
                order_date=order_iso,
            )
        supplier_bucket[key].qty += qty

    groups: List[PurchaseOrderExportGroup] = []
    for idx, supplier_ref in enumerate(sorted(grouped.keys()), start=1):
        lines = sorted(
            grouped[supplier_ref].values(),
            key=lambda line: ((line.need_date or ""), line.item_name.lower(), line.item_article.lower()),
        )
        groups.append(
            PurchaseOrderExportGroup(
                supplier_ref1c=supplier_ref,
                number=_short_order_number(run_id, idx),
                lines=lines,
            )
        )
    return groups, missing


def _order_lines_payload(ref_key: str, group: PurchaseOrderExportGroup) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line_no, line in enumerate(group.lines, start=1):
        row = {
            "LineNumber": line_no,
            "Номенклатура_Key": line.item_ref1c,
            "Характеристика_Key": EMPTY_REF1C,
            "Количество": float(line.qty or 0.0),
            "ДатаПоступления": _fmt_1c_datetime(date.fromisoformat(line.need_date)) if line.need_date else None,
            "Содержание": line.item_name,
            "Цена": 0,
            "ПроцентСкидкиНаценки": 0,
            "СуммаСкидкиНаценки": 0,
            "Сумма": 0,
            "СтавкаНДС_Key": EMPTY_REF1C,
            "СуммаНДС": 0,
            "Всего": 0,
            "Спецификация_Key": EMPTY_REF1C,
            "ЗаказПокупателя_Key": EMPTY_REF1C,
            "СтруктурнаяЕдиницаРезерв_Key": EMPTY_REF1C,
            "НоменклатураПоставщика_Key": EMPTY_REF1C,
            "КлючСвязи": 0,
        }
        if line.unit_ref1c:
            row["ЕдиницаИзмерения"] = line.unit_ref1c
            row["ЕдиницаИзмерения_Type"] = UNIT_TYPE_1C
        elif line.unit_name:
            row["ЕдиницаИзмерения"] = line.unit_name
        if ref_key:
            row["Ref_Key"] = ref_key
        rows.append({k: v for k, v in row.items() if v is not None})
    return rows


def _doc_endpoint(ref_key: str) -> str:
    return f"{PURCHASE_ORDER_ENTITY}(guid'{ref_key}')"


def _has_stock_lines(doc: Dict[str, Any]) -> bool:
    lines = doc.get("Запасы")
    return isinstance(lines, list) and len(lines) > 0


def _is_prodplan_order_for_run(doc: Dict[str, Any], run_id: int) -> bool:
    comment = str(doc.get("Комментарий") or "")
    return f"PRODPLAN source=planned_purchase/run:{int(run_id)}" in comment


def _ensure_free_or_reusable_number(
    client: OData1CClient,
    group: PurchaseOrderExportGroup,
    run_id: int,
    start_index: int,
) -> Optional[Dict[str, Any]]:
    index = start_index
    while index < start_index + 1000:
        group.number = _short_order_number(run_id, index)
        existing = _existing_order_by_number(client, group.number)
        if not existing:
            return None
        existing_supplier = _clean_ref1c(existing.get("Контрагент_Key"))
        if (
            existing_supplier == group.supplier_ref1c
            and _is_prodplan_order_for_run(existing, run_id)
        ):
            return existing
        index += 1
    raise RuntimeError(f"Не удалось подобрать свободный номер заказа для поставщика {group.supplier_ref1c}")


def export_planned_purchases_to_1c(
    db: Session,
    run_id: int,
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    purchase_ids: Optional[List[int]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    groups, skipped_rows = _collect_purchase_groups(
        db,
        run_id,
        date_from=date_from,
        date_to=date_to,
        purchase_ids=purchase_ids,
    )
    if dry_run:
        return {
            "status": "ok",
            "dry_run": True,
            "orders_planned": len(groups),
            "orders_created": 0,
            "orders_existing": 0,
            "lines_total": sum(len(g.lines) for g in groups),
            "skipped_rows": skipped_rows,
            "orders": [asdict(g) for g in groups],
        }

    client = _create_odata_client(_load_odata_config(), OData1CClient)

    created = 0
    existing = 0
    for group_index, group in enumerate(groups, start=1):
        try:
            existing_doc = _ensure_free_or_reusable_number(client, group, run_id, group_index)
            if existing_doc:
                group.target_ref_key = str(existing_doc.get("Ref_Key") or "") or None
                if not _has_stock_lines(existing_doc):
                    if not group.target_ref_key:
                        raise RuntimeError(f"1C did not return Ref_Key for existing {group.number}")
                    client.patch(_doc_endpoint(group.target_ref_key), {"Запасы": _order_lines_payload(group.target_ref_key, group)})
                    group.status = "created"
                    created += 1
                else:
                    group.status = "existing"
                    existing += 1
                continue

            min_need = min((date.fromisoformat(line.need_date) for line in group.lines if line.need_date), default=None)
            header_payload = {
                "Number": group.number,
                "Date": _fmt_1c_datetime(date.today()),
                "Posted": False,
                "Контрагент_Key": group.supplier_ref1c,
                "ДатаПоступления": _fmt_1c_datetime(min_need),
                "Комментарий": f"PRODPLAN source=planned_purchase/run:{int(run_id)}; number={group.number}",
                "Запасы": [],
            }
            header_payload["Запасы"] = _order_lines_payload("", group)
            created_header = client.post(PURCHASE_ORDER_ENTITY, header_payload)
            ref_key = str(created_header.get("Ref_Key") or "").strip()
            if not ref_key:
                raise RuntimeError(f"1C did not return Ref_Key for {group.number}")
            group.target_ref_key = ref_key

            group.status = "created"
            created += 1
        except Exception as exc:
            group.status = "error"
            group.error = str(exc)
            try:
                print(f"[1C purchase export] {group.number} failed: {group.error}")
            except Exception:
                pass

    return {
        "status": "ok" if all(g.status != "error" for g in groups) else "partial_error",
        "dry_run": False,
        "orders_planned": len(groups),
        "orders_created": created,
        "orders_existing": existing,
        "lines_total": sum(len(g.lines) for g in groups),
        "skipped_rows": skipped_rows,
        "orders": [asdict(g) for g in groups],
    }

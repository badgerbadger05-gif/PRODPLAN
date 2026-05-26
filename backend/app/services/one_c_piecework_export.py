"""Export ProductionManufacture records to 1C as Document_СдельныйНаряд.

Pattern: mirrors one_c_manufacture_export.py.
Documentation: .docs/piecework_order_odata.md.

Safety per the doc:
1. Default dry_run=True.
2. Refuse non-demo base_url unless allow_production=True.
3. Posted=false. Posting stays on 1C admin side.
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
    SyncLink,
)
from .one_c_export_common import (
    clean_ref1c as _clean_ref1c,
    create_odata_client as _create_odata_client,
    fmt_1c_datetime as _fmt_1c_datetime,
    find_sync_link as _find_sync_link,
    post_export_entries as _post_export_entries,
    upsert_sync_link as _upsert_sync_link,
)
from .odata_config import load_odata_config as _load_odata_config
from .odata_client import OData1CClient
from .one_c_manufacture_export import export_manufactures_to_1c


PIECEWORK_ENTITY = "Document_СдельныйНаряд"
BASIS_TYPE = "StandardODATA.Document_СборкаЗапасов"
ORDER_TYPE = "StandardODATA.Document_ЗаказНаПроизводство"


@dataclass
class PieceworkExportEntry:
    manufacture_id: int
    product_id: int
    order_id: int
    order_ref1c: Optional[str]
    basis_ref1c: Optional[str]
    item_ref1c: str
    item_name: str
    qty: float
    number: str
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

        entries.append(PieceworkExportEntry(
            manufacture_id=int(m.manufacture_id),
            product_id=int(m.product_id),
            order_id=int(m.order_id),
            order_ref1c=_clean_ref1c(m.order.order_ref1c) if m.order else None,
            basis_ref1c=basis_ref,
            item_ref1c=item_ref,
            item_name=str(item.item_name or "") if item else "",
            qty=float(m.qty or 0),
            number=_short_piecework_number(int(m.manufacture_id)),
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
    when = _fmt_1c_datetime(date.today())
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

    payload: Dict[str, Any] = {
        "Number": entry.number,
        "Date": when,
        "Posted": False,
        "Закрыт": False,
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
    operation_ref: str,
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
    with Posted=false. Idempotent via sync_link (source_doctype='piecework').

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
        _load_odata_config(),
        OData1CClient,
        allow_production=allow_production,
        require_demo_base=True,
    )

    created, errored = _post_export_entries(
        db,
        entries=zip(eligible, payloads),
        client=client,
        target_entity=PIECEWORK_ENTITY,
        missing_ref_error=f"1C did not return Ref_Key for new {PIECEWORK_ENTITY}",
        upsert_link=lambda **kwargs: _upsert_link(db, **kwargs),
        on_success=lambda entry, ref_key: None,
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

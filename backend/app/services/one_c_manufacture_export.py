"""Export ProductionManufacture records to 1C as Document_СборкаЗапасов.

Pattern: mirrors one_c_production_order_export.py / one_c_stock_transfer_export.py.
Documentation: .docs/one_c_export_from_prodplan.md.

Safety per the doc:
1. Default dry_run=True.
2. Refuse non-demo base_url unless allow_production=True.
3. Posted=false. Posting stays on 1C admin side.
4. Idempotency via sync_link (source_doctype='manufacture').

A ProductionManufacture represents one "Произвести" event on a
production_products line. In 1C this maps to Document_СборкаЗапасов
("Сборка/выпуск") that links back to the parent Document_ЗаказНаПроизводство
via ЗаказНаПроизводство_Key and lists the finished product in the Продукция
table part.

Minimal payload: header + Продукция[]. Material consumption (Запасы table
part) is intentionally omitted in this first iteration — material movements
are handled separately via the transfer export (PR #8). The 1C admin can
augment the draft document if needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from ..models import (
    Item,
    ProductionManufacture,
    ProductionOrder,
    ProductionOrderLineState,
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
    qty: float
    executor: Optional[str] = None
    number: str = ""
    target_ref_key: Optional[str] = None
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

        entries.append(
            ManufactureExportEntry(
                manufacture_id=int(m.manufacture_id),
                product_id=int(m.product_id),
                order_id=int(m.order_id),
                order_ref1c=order_ref,
                item_ref1c=item_ref,
                item_name=str(item.item_name or "") if item else "",
                item_article=str(item.item_article or "") if item else "",
                qty=float(m.qty or 0),
                executor=str(m.executor) if m.executor else None,
                number=_short_manufacture_number(int(m.manufacture_id)),
            )
        )

    return entries, skipped


def _build_header_payload(entry: ManufactureExportEntry) -> Dict[str, Any]:
    comment = (
        f"PRODPLAN source=manufacture/{entry.manufacture_id}; "
        f"order_id={entry.order_id}; product_id={entry.product_id}; "
        f"number={entry.number}"
    )
    if entry.executor:
        comment += f"; executor={entry.executor}"
    products = [
        {
            "LineNumber": 1,
            "Номенклатура_Key": entry.item_ref1c,
            "Количество": float(entry.qty),
        }
    ]
    payload: Dict[str, Any] = {
        "Number": entry.number,
        "Date": _fmt_1c_datetime(date.today()),
        "Posted": False,
        "Комментарий": comment,
        "Продукция": products,
    }
    # Per contract: ДокументОснование is mandatory for child documents.
    # _collect_export_entries guarantees order_ref1c is set; this assertion
    # protects against accidental drift if the collector ever changes.
    assert entry.order_ref1c, "manufacture export requires order_ref1c basis"
    payload["ЗаказНаПроизводство_Key"] = entry.order_ref1c
    payload["ДокументОснование"] = entry.order_ref1c
    payload["ДокументОснование_Type"] = "StandardODATA.Document_ЗаказНаПроизводство"
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
        if link and link.status == "success" and (link.target_ref_key or ""):
            entry.status = "existing"
            entry.target_ref_key = str(link.target_ref_key)
            entry.reason = "уже выгружен в 1С (sync_link)"
            already_linked.append(entry)
            continue
        m_row = (
            db.query(ProductionManufacture)
            .filter(ProductionManufacture.manufacture_id == entry.manufacture_id)
            .one()
        )
        if _clean_ref1c(m_row.exported_ref1c):
            entry.status = "existing"
            entry.target_ref_key = _clean_ref1c(m_row.exported_ref1c)
            entry.reason = "exported_ref1c уже стоит"
            already_linked.append(entry)
            continue
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

    payloads: List[Dict[str, Any]] = []
    for entry in eligible:
        payload = _build_header_payload(entry)
        payloads.append(
            {"manufacture_id": entry.manufacture_id, "number": entry.number, "payload": payload}
        )

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

    def _mark_success(entry: ManufactureExportEntry, ref_key: str) -> None:
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
    summary["manufactures_error"] = errored
    summary["entries"] = [asdict(e) for e in entries]
    summary["status"] = "ok" if errored == 0 else "partial_error"
    return summary

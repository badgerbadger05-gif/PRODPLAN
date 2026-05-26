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
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from ..models import (
    Item,
    ProductionOrder,
    ProductionProduct,
    SyncLink,
    Unit,
)
from .one_c_export_common import (
    clean_ref1c as _clean_ref1c,
    create_odata_client as _create_odata_client,
    fmt_1c_datetime as _fmt_1c_datetime,
    find_sync_link as _find_sync_link,
    post_document_operational as _post_document_operational,
    post_export_entries as _post_export_entries,
    upsert_sync_link as _upsert_sync_link,
)
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
    qty: float
    characteristic_ref1c: Optional[str] = None
    spec_ref1c: Optional[str] = None


@dataclass
class ProductionOrderExportEntry:
    order_id: int
    number: str
    source_planned_order_id: Optional[int] = None
    source_run_id: Optional[int] = None
    lines: List[ProductionOrderExportLine] = field(default_factory=list)
    target_ref_key: Optional[str] = None
    status: str = "planned"  # planned | created | existing | error | skipped
    error: Optional[str] = None
    reason: Optional[str] = None  # human-readable explanation for skipped/error


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


def _existing_link(db: Session, order_id: int) -> Optional[SyncLink]:
    return _find_sync_link(
        db,
        SyncLink,
        source_doctype="production_order",
        source_id=int(order_id),
        target_entity=PRODUCTION_ORDER_ENTITY,
    )


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
            lines.append(
                ProductionOrderExportLine(
                    line_number=int(product.line_number or 1),
                    item_id=int(product.item_id),
                    item_ref1c=ref1c,
                    item_name=str(item.item_name or ""),
                    item_article=str(item.item_article or ""),
                    qty=float(product.quantity or 0.0),
                    characteristic_ref1c=_clean_ref1c(product.characteristic_ref1c) or None,
                )
            )

        if not lines:
            continue

        entries.append(
            ProductionOrderExportEntry(
                order_id=int(order.order_id),
                number=_short_order_number(int(order.order_id), order.source_run_id),
                source_planned_order_id=None,
                source_run_id=int(order.source_run_id) if order.source_run_id else None,
                lines=lines,
            )
        )

    return entries, skipped


def _build_header_payload(entry: ProductionOrderExportEntry) -> Dict[str, Any]:
    comment = (
        f"PRODPLAN source=production_order/{entry.order_id}; "
        f"run={entry.source_run_id or 0}; number={entry.number}"
    )
    products = [
        {
            "LineNumber": ln.line_number,
            "Номенклатура_Key": ln.item_ref1c,
            "Количество": float(ln.qty),
            **(
                {"Характеристика_Key": ln.characteristic_ref1c}
                if ln.characteristic_ref1c
                else {}
            ),
        }
        for ln in entry.lines
    ]
    return {
        "Number": entry.number,
        "Date": _fmt_1c_datetime(date.today()),
        "Posted": False,
        "Комментарий": comment,
        "Продукция": products,
    }


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

    # Build payloads for the dry-run preview.
    payloads: List[Dict[str, Any]] = []
    for entry in eligible:
        payload = _build_header_payload(entry)
        payloads.append({"order_id": entry.order_id, "number": entry.number, "payload": payload})

    if dry_run:
        summary["entries"] = [asdict(e) for e in entries]
        summary["payloads"] = payloads
        return summary

    # ----- real write below -----
    client = _create_odata_client(
        _load_odata_config(),
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

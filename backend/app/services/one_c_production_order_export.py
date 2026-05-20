"""Export internal MRP-source ProductionOrders to 1C as Document_ЗаказНаПроизводство.

Pattern: mirrors backend/app/services/one_c_purchase_order_export.py.
Documentation: .docs/one_c_export_from_prodplan.md.

Safety rules from the doc are enforced on top of the call site:
1. Default `dry_run=True`; explicit dry_run=False is required to write.
2. Refuse to write if the configured base_url doesn't look like a demo DB
   (substring 'unf_demo'), unless `allow_production=True` is also set.
3. Always send `Posted=false`. Posting is on the 1C admin side per plan.
4. Idempotency: skip orders that already have a successful sync_link OR a
   non-empty `production_orders.order_ref1c` (it gets stamped from the
   1C response on first successful export).

Only MRP-source production_orders (source='mrp') are eligible. 1C-synced
orders (source='1c') already exist in 1C — we wouldn't re-export them.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from ..models import (
    Item,
    ProductionOrder,
    ProductionProduct,
    SyncLink,
    Unit,
)
from .odata_client import OData1CClient


CONFIG_PATH = Path("config") / "odata_config.json"
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


def _load_odata_config() -> Dict[str, Any]:
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text("utf-8") or "{}")
    except Exception:
        pass
    return {}


def _fmt_1c_datetime(value: Optional[date]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    return datetime.combine(value, datetime.min.time()).isoformat()


def _clean_ref1c(value: Any) -> str:
    ref = str(value or "").strip()
    if not ref or ref == EMPTY_REF1C:
        return ""
    return ref


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


def _payload_hash(payload: Dict[str, Any]) -> str:
    try:
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        normalized = str(payload)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _existing_link(db: Session, order_id: int) -> Optional[SyncLink]:
    return (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "production_order",
            SyncLink.source_id == int(order_id),
            SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
        )
        .one_or_none()
    )


def _is_demo_base_url(base_url: str) -> bool:
    return "unf_demo" in (base_url or "").lower()


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
    """Atomic upsert on (source_system, source_doctype, source_id, target_entity)."""
    existing = _existing_link(db, entry.order_id)
    if existing is None:
        db.add(
            SyncLink(
                source_system="PRODPLAN",
                source_doctype="production_order",
                source_id=int(entry.order_id),
                target_system="1C",
                target_entity=PRODUCTION_ORDER_ENTITY,
                target_ref_key=target_ref_key,
                target_number=entry.number,
                payload_hash=payload_hash,
                status=status,
                last_error=last_error,
                last_synced_at=datetime.utcnow() if status == "success" else None,
            )
        )
        return
    existing.target_number = entry.number
    existing.payload_hash = payload_hash
    existing.status = status
    existing.last_error = last_error
    if target_ref_key:
        existing.target_ref_key = target_ref_key
    if status == "success":
        existing.last_synced_at = datetime.utcnow()


def export_production_orders_to_1c(
    db: Session,
    order_ids: List[int],
    *,
    dry_run: bool = True,
    allow_production: bool = False,
) -> Dict[str, Any]:
    """
    Export the given internal MRP production_orders to 1C as
    Document_ЗаказНаПроизводство with Posted=false.

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
    cfg = _load_odata_config()
    base_url = str(cfg.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("OData config is not set. Save 1C connection settings first.")
    if not _is_demo_base_url(base_url) and not allow_production:
        raise PermissionError(
            f"Refusing to write to non-demo base_url '{base_url}'. "
            "Pass allow_production=true to override (use with caution)."
        )

    client = OData1CClient(
        base_url=base_url,
        username=cfg.get("username") or None,
        password=cfg.get("password") or None,
        token=cfg.get("token") or None,
    )

    created = 0
    errored = 0
    for entry, payload_envelope in zip(eligible, payloads):
        payload = payload_envelope["payload"]
        try:
            phash = _payload_hash(payload)
            _upsert_link(
                db,
                entry=entry,
                payload_hash=phash,
                target_ref_key=None,
                status="planned",
                last_error=None,
            )
            db.flush()

            created_header = client.post(PRODUCTION_ORDER_ENTITY, payload)
            ref_key = _clean_ref1c(created_header.get("Ref_Key"))
            if not ref_key:
                raise RuntimeError("1C did not return Ref_Key for the new Document_ЗаказНаПроизводство")

            entry.target_ref_key = ref_key
            entry.status = "created"
            created += 1

            # Stamp success on sync_link AND on production_orders.order_ref1c
            # so the journal stops treating it as MRP-only.
            _upsert_link(
                db,
                entry=entry,
                payload_hash=phash,
                target_ref_key=ref_key,
                status="success",
                last_error=None,
            )
            order_row = (
                db.query(ProductionOrder).filter(ProductionOrder.order_id == entry.order_id).one()
            )
            order_row.order_ref1c = ref_key
        except Exception as exc:
            entry.status = "error"
            entry.error = str(exc)
            errored += 1
            try:
                _upsert_link(
                    db,
                    entry=entry,
                    payload_hash=_payload_hash(payload),
                    target_ref_key=None,
                    status="error",
                    last_error=str(exc),
                )
            except Exception:
                # Don't let bookkeeping failure mask the original error.
                pass
            try:
                print(f"[1C production export] order_id={entry.order_id} failed: {entry.error}")
            except Exception:
                pass

    db.commit()

    summary["orders_created"] = created
    summary["orders_error"] = errored
    summary["entries"] = [asdict(e) for e in entries]
    summary["status"] = "ok" if errored == 0 else "partial_error"
    return summary

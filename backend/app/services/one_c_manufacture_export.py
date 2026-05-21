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

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
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
from .odata_client import OData1CClient


CONFIG_PATH = Path("config") / "odata_config.json"
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


def _payload_hash(payload: Dict[str, Any]) -> str:
    try:
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        normalized = str(payload)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _short_manufacture_number(manufacture_id: int) -> str:
    """Short, recognizable, unique number that fits 1C's Number column."""
    return f"PM{int(manufacture_id) % 1_000_000_000:09d}"


def _existing_link(db: Session, manufacture_id: int) -> Optional[SyncLink]:
    return (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "manufacture",
            SyncLink.source_id == int(manufacture_id),
            SyncLink.target_entity == MANUFACTURE_ENTITY,
        )
        .one_or_none()
    )


def _is_demo_base_url(base_url: str) -> bool:
    return "unf_demo" in (base_url or "").lower()


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
                order_ref1c=_clean_ref1c(m.order.order_ref1c) if m.order else None,
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
    if entry.order_ref1c:
        # Link the manufacture back to the parent production order in 1C.
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
    existing = _existing_link(db, entry.manufacture_id)
    if existing is None:
        db.add(
            SyncLink(
                source_system="PRODPLAN",
                source_doctype="manufacture",
                source_id=int(entry.manufacture_id),
                target_system="1C",
                target_entity=MANUFACTURE_ENTITY,
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
    """
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

            created_header = client.post(MANUFACTURE_ENTITY, payload)
            ref_key = _clean_ref1c(created_header.get("Ref_Key"))
            if not ref_key:
                raise RuntimeError("1C did not return Ref_Key for new Document_СборкаЗапасов")

            entry.target_ref_key = ref_key
            entry.status = "created"
            created += 1

            _upsert_link(
                db,
                entry=entry,
                payload_hash=phash,
                target_ref_key=ref_key,
                status="success",
                last_error=None,
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
                m_row = (
                    db.query(ProductionManufacture)
                    .filter(ProductionManufacture.manufacture_id == entry.manufacture_id)
                    .one()
                )
                m_row.export_error = str(exc)
            except Exception:
                pass
            try:
                print(
                    f"[1C manufacture export] manufacture_id={entry.manufacture_id} failed: {entry.error}"
                )
            except Exception:
                pass

    db.commit()

    summary["manufactures_created"] = created
    summary["manufactures_error"] = errored
    summary["entries"] = [asdict(e) for e in entries]
    summary["status"] = "ok" if errored == 0 else "partial_error"
    return summary

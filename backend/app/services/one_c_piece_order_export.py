"""Export ProductionManufacture records to 1C as Document_СдельныйНаряд.

Pattern: mirrors one_c_production_order_export / one_c_stock_transfer_export
/ one_c_manufacture_export.

Plan rule: завершает write-сервис для четвёртого документа из матрицы
`.docs/one_c_export_from_prodplan.md`. Реквизиты EntitySet'а подтверждены
прогоном metadata + сэмпла на demo-копии 1С (см. .docs/piece_order_probe).

Safety per the doc:
1. Default dry_run=True.
2. Refuse non-demo base_url unless allow_production=True.
3. Posted=false + Закрыт=false; проведение/закрытие — на 1С админе.
4. Idempotency via sync_link (source_doctype='piece_order', source_id =
   manufacture_id).

One ProductionManufacture maps to one Document_СдельныйНаряд. The line
table part is built from SpecOperation rows of the parent product's spec:
each operation produces one Операции[] row with qty=manufacture.qty,
норма from SpecOperation.time_norm, нормочасы = qty * norm.

The executor / department / currency / business operation / rate fields
are intentionally left empty — they're catalog refs that PRODPLAN does
not currently map locally. The 1C admin fills them on the draft.
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
    Operation,
    ProductionManufacture,
    ProductionOrder,
    ProductionProduct,
    ProductionStage,
    SpecOperation,
    Specification,
    SyncLink,
)
from .odata_client import OData1CClient


CONFIG_PATH = Path("config") / "odata_config.json"
PIECE_ORDER_ENTITY = "Document_СдельныйНаряд"
EMPTY_REF1C = "00000000-0000-0000-0000-000000000000"


@dataclass
class PieceOrderOperationLine:
    line_number: int
    operation_ref1c: Optional[str]
    operation_name: str
    stage_ref1c: Optional[str]
    stage_name: str
    item_ref1c: str
    spec_ref1c: Optional[str]
    qty_plan: float
    qty_fact: float
    norma_time: float
    norm_hours: float


@dataclass
class PieceOrderExportEntry:
    manufacture_id: int
    product_id: int
    order_id: int
    order_ref1c: Optional[str]
    item_ref1c: str
    item_name: str
    qty: float
    executor: Optional[str]
    number: str = ""
    lines: List[PieceOrderOperationLine] = field(default_factory=list)
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


def _short_piece_order_number(manufacture_id: int) -> str:
    """Short, recognizable, unique. Fits 1C Number column (~11 chars)."""
    return f"PN{int(manufacture_id) % 1_000_000_000:09d}"


def _existing_link(db: Session, manufacture_id: int) -> Optional[SyncLink]:
    return (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "piece_order",
            SyncLink.source_id == int(manufacture_id),
            SyncLink.target_entity == PIECE_ORDER_ENTITY,
        )
        .one_or_none()
    )


def _is_demo_base_url(base_url: str) -> bool:
    return "unf_demo" in (base_url or "").lower()


def _build_operation_lines(
    db: Session,
    product: ProductionProduct,
    qty: float,
) -> List[PieceOrderOperationLine]:
    """
    One Операции[] row per SpecOperation of the parent product's spec.
    qty_plan = qty_fact = manufacture.qty. norm_hours = qty * SpecOp.time_norm.
    Lines without an Operation.operation_ref1c are still emitted (operation_ref1c
    becomes empty) so the admin can pick the operation in 1С on the draft.
    """
    spec_id = product.spec_id
    if spec_id is None:
        # Fallback: look up DefaultSpecification for the item.
        from ..models import DefaultSpecification

        default_row = (
            db.query(DefaultSpecification)
            .filter(DefaultSpecification.item_id == product.item_id)
            .first()
        )
        spec_id = int(default_row.spec_id) if default_row else None
    if spec_id is None:
        return []

    rows = (
        db.query(SpecOperation, Operation, ProductionStage)
        .outerjoin(Operation, Operation.operation_id == SpecOperation.operation_id)
        .outerjoin(ProductionStage, ProductionStage.stage_id == SpecOperation.stage_id)
        .filter(SpecOperation.spec_id == spec_id)
        .order_by(SpecOperation.spec_operation_id.asc())
        .all()
    )

    # Get spec_ref1c once.
    spec_ref = ""
    if spec_id:
        spec_obj = db.query(Specification).filter(Specification.spec_id == spec_id).first()
        spec_ref = _clean_ref1c(spec_obj.spec_ref1c) if spec_obj else ""

    item_ref = _clean_ref1c(product.item.item_ref1c) if product.item else ""

    lines: List[PieceOrderOperationLine] = []
    for idx, (spec_op, op, stage) in enumerate(rows, start=1):
        norm_time = float(
            (spec_op.time_norm if spec_op.time_norm is not None else None)
            or (op.time_norm if op and op.time_norm is not None else 0)
            or 0
        )
        lines.append(
            PieceOrderOperationLine(
                line_number=idx,
                operation_ref1c=_clean_ref1c(op.operation_ref1c) if op else None,
                operation_name=str(op.operation_name or "") if op else "",
                stage_ref1c=_clean_ref1c(stage.stage_ref1c) if stage else None,
                stage_name=str(stage.stage_name or "") if stage else "",
                item_ref1c=item_ref,
                spec_ref1c=spec_ref or None,
                qty_plan=float(qty),
                qty_fact=float(qty),
                norma_time=norm_time,
                norm_hours=float(qty) * norm_time,
            )
        )
    return lines


def _collect_export_entries(
    db: Session, manufacture_ids: List[int]
) -> Tuple[List[PieceOrderExportEntry], List[Dict[str, Any]]]:
    entries: List[PieceOrderExportEntry] = []
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
    found = {int(m.manufacture_id) for m in rows}
    for missing in [x for x in ids if x not in found]:
        skipped.append(
            {"manufacture_id": missing, "reason": "ProductionManufacture не найден"}
        )

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

        op_lines = _build_operation_lines(db, m.product, float(m.qty or 0))
        if not op_lines:
            skipped.append(
                {
                    "manufacture_id": int(m.manufacture_id),
                    "reason": "нет SpecOperation у спецификации продукта — не из чего строить наряд",
                }
            )
            continue

        entries.append(
            PieceOrderExportEntry(
                manufacture_id=int(m.manufacture_id),
                product_id=int(m.product_id),
                order_id=int(m.order_id),
                order_ref1c=_clean_ref1c(m.order.order_ref1c) if m.order else None,
                item_ref1c=item_ref,
                item_name=str(item.item_name or "") if item else "",
                qty=float(m.qty or 0),
                executor=str(m.executor) if m.executor else None,
                number=_short_piece_order_number(int(m.manufacture_id)),
                lines=op_lines,
            )
        )
    return entries, skipped


def _build_payload(entry: PieceOrderExportEntry) -> Dict[str, Any]:
    today_iso = _fmt_1c_datetime(date.today())
    comment_parts = [
        f"PRODPLAN source=piece_order/{entry.manufacture_id}",
        f"order_id={entry.order_id}",
        f"product_id={entry.product_id}",
        f"number={entry.number}",
    ]
    if entry.executor:
        comment_parts.append(f"executor={entry.executor}")
    comment = "; ".join(comment_parts)

    op_lines: List[Dict[str, Any]] = []
    for ln in entry.lines:
        op = {
            "LineNumber": ln.line_number,
            "Период": today_iso,
            "Номенклатура_Key": ln.item_ref1c,
            "КоличествоПлан": float(ln.qty_plan),
            "КоличествоФакт": float(ln.qty_fact),
            "НормаВремени": float(ln.norma_time),
            "Нормочасы": float(ln.norm_hours),
            # Расценка / Стоимость пока 0 — расценки исполнителей не
            # маппятся локально, 1С админ заполняет их на черновике.
            "Расценка": 0.0,
            "Стоимость": 0.0,
            "КлючСвязи": ln.line_number,
        }
        if ln.operation_ref1c:
            op["Операция_Key"] = ln.operation_ref1c
        if ln.stage_ref1c:
            op["Этап_Key"] = ln.stage_ref1c
        if ln.spec_ref1c:
            op["Спецификация_Key"] = ln.spec_ref1c
        if entry.order_ref1c:
            op["ЗаказНаПроизводство_Key"] = entry.order_ref1c
        op_lines.append(op)

    payload: Dict[str, Any] = {
        "Number": entry.number,
        "Date": today_iso,
        "Posted": False,
        "Закрыт": False,
        "Комментарий": comment,
        # "ВТабличнойЧасти" / "ВШапке" — выбираем "В шапке" для одного
        # исполнителя на весь наряд, как на скриншоте пользователя; при
        # отсутствии конкретного GUID-сотрудника поле Исполнитель пустое и
        # заполняется в 1С на черновике.
        "ПоложениеИсполнителя": "ВШапке",
        "ПоложениеЗаказаНаПроизводство": "ВТабличнойЧасти",
        "ПоложениеСтруктурнойЕдиницы": "ВТабличнойЧасти",
        "Операции": op_lines,
    }
    if entry.order_ref1c:
        payload["ЗаказНаПроизводство_Key"] = entry.order_ref1c
    return payload


def _upsert_link(
    db: Session,
    *,
    entry: PieceOrderExportEntry,
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
                source_doctype="piece_order",
                source_id=int(entry.manufacture_id),
                target_system="1C",
                target_entity=PIECE_ORDER_ENTITY,
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


def export_piece_orders_to_1c(
    db: Session,
    manufacture_ids: List[int],
    *,
    dry_run: bool = True,
    allow_production: bool = False,
) -> Dict[str, Any]:
    """Export selected manufactures as 1С Document_СдельныйНаряд (Posted=false)."""
    entries, skipped = _collect_export_entries(db, list(manufacture_ids))

    eligible: List[PieceOrderExportEntry] = []
    already: List[PieceOrderExportEntry] = []
    for entry in entries:
        link = _existing_link(db, entry.manufacture_id)
        if link and link.status == "success" and (link.target_ref_key or ""):
            entry.status = "existing"
            entry.target_ref_key = str(link.target_ref_key)
            entry.reason = "уже выгружен в 1С (sync_link)"
            already.append(entry)
            continue
        eligible.append(entry)

    summary: Dict[str, Any] = {
        "status": "ok",
        "dry_run": bool(dry_run),
        "entity": PIECE_ORDER_ENTITY,
        "manufactures_requested": len(manufacture_ids),
        "manufactures_eligible": len(eligible),
        "manufactures_already_linked": len(already),
        "manufactures_created": 0,
        "manufactures_error": 0,
        "skipped_rows": skipped,
        "entries": [],
    }

    payloads: List[Dict[str, Any]] = []
    for entry in eligible:
        payload = _build_payload(entry)
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
    for entry, envelope in zip(eligible, payloads):
        payload = envelope["payload"]
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

            created_header = client.post(PIECE_ORDER_ENTITY, payload)
            ref_key = _clean_ref1c(created_header.get("Ref_Key"))
            if not ref_key:
                raise RuntimeError("1C did not return Ref_Key for new Document_СдельныйНаряд")

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
                pass
            try:
                print(
                    f"[1C piece-order export] manufacture_id={entry.manufacture_id} failed: {entry.error}"
                )
            except Exception:
                pass

    db.commit()

    summary["manufactures_created"] = created
    summary["manufactures_error"] = errored
    summary["entries"] = [asdict(e) for e in entries]
    summary["status"] = "ok" if errored == 0 else "partial_error"
    return summary

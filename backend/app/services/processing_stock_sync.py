"""Read-only 1C sync for customer-owned stock currently held by processors."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from sqlalchemy import func

from ..models import Item, ProcessingContractorStock, ProcessingStockSyncState
from ..schemas import ODataSyncRequest
from .odata_client import OData1CClient


REGISTER_ENTITY = "AccumulationRegister_ЗапасыПереданные/Balance"
DIMENSIONS = "Номенклатура,Контрагент,Заказ,ТипПриемаПередачи"
SELECT_FIELDS = [
    "Номенклатура_Key",
    "Контрагент_Key",
    "Заказ",
    "Заказ_Type",
    "ТипПриемаПередачи",
    "КоличествоBalance",
]


def _now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None)


def _ref(value: Any) -> str:
    if isinstance(value, dict):
        return str(
            value.get("Ref_Key")
            or value.get("RefKey")
            or value.get("ref_key")
            or ""
        ).strip()
    return str(value or "").strip()


def _state(db: Session) -> ProcessingStockSyncState:
    row = db.get(ProcessingStockSyncState, 1)
    if row is None:
        row = ProcessingStockSyncState(id=1)
        db.add(row)
        db.flush()
    return row


def processing_stock_status(db: Session) -> dict[str, Any]:
    state = db.get(ProcessingStockSyncState, 1)
    stock_rows = int(db.query(ProcessingContractorStock).count() or 0)
    total_qty = sum(
        float(row[0] or 0)
        for row in db.query(ProcessingContractorStock.qty).all()
    )
    return {
        "status": state.status if state else "never",
        "last_attempt_at": state.last_attempt_at.isoformat() if state and state.last_attempt_at else None,
        "last_success_at": state.last_success_at.isoformat() if state and state.last_success_at else None,
        "rows_seen": int(state.rows_seen or 0) if state else 0,
        "rows_stored": stock_rows,
        "unmatched_items": int(state.unmatched_items or 0) if state else 0,
        "total_qty": total_qty,
        "last_error": state.last_error if state else None,
    }


def processing_stock_totals(
    db: Session,
    item_ids: set[int] | None = None,
) -> dict[int, float]:
    """Return exact at-contractor quantity per local item for DBR readers."""
    query = db.query(
        ProcessingContractorStock.item_id,
        func.sum(ProcessingContractorStock.qty),
    )
    if item_ids is not None:
        if not item_ids:
            return {}
        query = query.filter(ProcessingContractorStock.item_id.in_(item_ids))
    return {
        int(item_id): float(qty or 0)
        for item_id, qty in query.group_by(ProcessingContractorStock.item_id).all()
    }


def sync_processing_stock_from_odata(db: Session, req: ODataSyncRequest) -> dict[str, Any]:
    """Atomically replace the local snapshot after a successful Balance query.

    The OData call happens before any snapshot row is changed. Query/parse
    failures therefore leave the last known-good stock intact and only update
    the health record.
    """
    attempted_at = _now()
    client = OData1CClient(
        req.base_url,
        req.username,
        req.password,
        req.token,
    )
    period = attempted_at.strftime("%Y-%m-%dT%H:%M:%S")
    entity = (
        f"{REGISTER_ENTITY}(Period=datetime'{period}',"
        f"Dimensions='{DIMENSIONS}')"
    )

    try:
        raw_rows = client.get_all(
            entity_name=entity,
            filter_query=req.filter_query,
            select_fields=req.select_fields or SELECT_FIELDS,
            top=1000,
            max_records=None,
            max_pages=1000,
            order_by=None,
        )

        item_by_ref = {
            str(ref).strip(): int(item_id)
            for item_id, ref in db.query(Item.item_id, Item.item_ref1c).all()
            if str(ref or "").strip()
        }
        aggregated: dict[tuple[int, str, str, str, str], float] = {}
        unmatched = 0
        for raw in raw_rows:
            item_ref = _ref(
                raw.get("Номенклатура_Key")
                or raw.get("Номенклатура")
            )
            item_id = item_by_ref.get(item_ref)
            if item_id is None:
                unmatched += 1
                continue
            qty = float(raw.get("КоличествоBalance") or 0)
            if qty <= 0:
                continue
            key = (
                item_id,
                _ref(raw.get("Контрагент_Key") or raw.get("Контрагент")),
                _ref(raw.get("Заказ")),
                str(raw.get("Заказ_Type") or "").strip(),
                str(raw.get("ТипПриемаПередачи") or "").strip(),
            )
            aggregated[key] = aggregated.get(key, 0.0) + qty

        if req.dry_run:
            return {
                "dry_run": True,
                "rows_seen": len(raw_rows),
                "rows_stored": len(aggregated),
                "unmatched_items": unmatched,
                "total_qty": sum(aggregated.values()),
            }

        db.query(ProcessingContractorStock).delete(synchronize_session=False)
        for (item_id, contractor, order_ref, order_type, transfer_type), qty in aggregated.items():
            db.add(
                ProcessingContractorStock(
                    item_id=item_id,
                    contractor_ref1c=contractor,
                    order_ref1c=order_ref,
                    order_type=order_type,
                    transfer_type=transfer_type,
                    qty=qty,
                    synced_at=attempted_at,
                )
            )
        state = _state(db)
        state.status = "ok"
        state.last_attempt_at = attempted_at
        state.last_success_at = attempted_at
        state.rows_seen = len(raw_rows)
        state.rows_stored = len(aggregated)
        state.unmatched_items = unmatched
        state.last_error = None
        db.commit()
        return processing_stock_status(db)
    except Exception as exc:
        db.rollback()
        state = _state(db)
        state.status = "error"
        state.last_attempt_at = attempted_at
        state.last_error = str(exc)[:2000]
        db.commit()
        raise

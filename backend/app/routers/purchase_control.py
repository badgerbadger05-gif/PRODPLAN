from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.purchase_control_materialization import (
    PurchaseControlMaterializationError,
    PurchaseControlMaterializerNotConfigured,
    PurchaseControlSnapshotUnavailable,
    materialize_rows,
)
from ..services.purchase_control_journal import (
    get_order_card,
    get_selection_summary,
    list_filters,
    list_journal,
)
from ..services.purchase_control_snapshot import PurchaseJournalSnapshotUnavailable

router = APIRouter(prefix="/v1/purchase-control", tags=["purchase-control"])


class PurchaseControlMaterializeRequest(BaseModel):
    snapshot_id: int = Field(..., ge=1)
    row_keys: list[str] = Field(default_factory=list)
    dry_run: bool = True


class PurchaseControlSelectionSummaryRequest(BaseModel):
    snapshot_id: int = Field(..., ge=1)
    row_keys: list[str] = Field(..., min_length=1, max_length=500)
    horizon_period_to: Optional[date] = None


class PurchaseControlSelectionSummaryResponse(BaseModel):
    snapshot_id: int
    selected_rows: int
    priced_rows: int
    unpriced_rows: int
    known_amount: float
    total_amount: Optional[float] = None
    amount_status: Literal["complete", "partial", "unavailable"]


@router.get("/orders", response_model=dict)
def get_orders(
    order_id: Optional[int] = None,
    supplier_id: Optional[int] = None,
    state: Optional[str] = None,
    phase: Optional[str] = None,
    line_status: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    active_only: bool = True,
    include_to_order: bool = True,
    horizon_period_to: Optional[date] = Query(
        None,
        description=(
            "Горизонт формирования заказов: показывать 'to_order'-строки только "
            "по активным прогонам, чей план заканчивается не позже этой даты "
            "(ISO). None = весь горизонт (все активные прогоны)."
        ),
    ),
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """
    Журнал закупок: строки заказов поставщику (1С) + незаказанные MRP-потребности
    (`line_status = to_order`) последнего FIXED_SNAPSHOT-прогона.
    """
    try:
        from ..services.item_ledger.current_execution import (
            CurrentExecutionUnavailable,
            load_current_execution_rows,
            require_current_execution_scope,
        )
        current_manifest = require_current_execution_scope(
            db,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )
        rows = [dict(row.payload or {}) for row in load_current_execution_rows(
            db,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )]
        if horizon_period_to is not None:
            from ..services.purchase_control_journal import _reconcile_buy_row_for_horizon
            rows = [
                projected
                for row in rows
                for projected in [_reconcile_buy_row_for_horizon(row, horizon_period_to.isoformat())]
                if projected is not None
            ]
        if order_id is not None:
            rows = [row for row in rows if row.get("order_id") == int(order_id)]
        if supplier_id is not None:
            rows = [row for row in rows if row.get("supplier_id") == int(supplier_id)]
        if state:
            rows = [row for row in rows if str(row.get("order_state_name") or "") == str(state)]
        if phase:
            rows = [row for row in rows if str(row.get("supply_phase") or "") == str(phase)]
        if line_status:
            rows = [row for row in rows if str(row.get("line_status") or "") == str(line_status)]
        if not include_to_order:
            rows = [row for row in rows if row.get("line_status") != "to_order"]
        if active_only:
            rows = [row for row in rows if float(row.get("remaining_qty") or 0) > 0]
        if search:
            needle = str(search).casefold()
            rows = [row for row in rows if needle in " ".join(
                str(row.get(key) or "") for key in ("order_number", "item_name", "item_code", "item_article", "supplier_name")
            ).casefold()]
        if date_from:
            rows = [row for row in rows if row.get("delivery_date") is not None and str(row["delivery_date"]) >= str(date_from)]
        if date_to:
            rows = [row for row in rows if row.get("delivery_date") is not None and str(row["delivery_date"]) <= str(date_to)]
        sort_key = sort_by if sort_by in {"delivery_date", "order_date", "order_number", "item_code", "remaining_qty"} else "delivery_date"
        rows.sort(key=lambda row: (row.get(sort_key) is None, row.get(sort_key) if row.get(sort_key) is not None else "", row.get("row_key")))
        if str(sort_dir or "asc").casefold() == "desc":
            rows.reverse()
        effective_limit = max(1, min(int(limit or 100), 500))
        effective_offset = max(0, int(offset or 0))
        saved = dict(current_manifest.summary or {})
        saved_summary = saved.get("summary")
        if not isinstance(saved_summary, dict):
            raise CurrentExecutionUnavailable("purchase current summary is missing")
        return {
                "rows": rows[effective_offset:effective_offset + effective_limit],
                "total": len(rows),
                "limit": effective_limit,
                "offset": effective_offset,
                "run_id": saved.get("run_id"),
                "run_ids": list(saved.get("run_ids") or []),
                "truth_status": saved.get("truth_status"),
                "ledger_generation_id": current_manifest.source_generation_id,
                "summary": saved_summary,
                "meta": saved,
        }
    except PurchaseJournalSnapshotUnavailable as e:
        raise HTTPException(status_code=503, detail=e.as_dict())
    except CurrentExecutionUnavailable as e:
        raise HTTPException(status_code=503, detail={"code": "purchase_control_current_unavailable", "reason": str(e)})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders/{order_id}", response_model=dict)
def get_order(order_id: int, db: Session = Depends(get_db)):
    """Карточка заказа поставщику со всеми строками (для detail pane)."""
    try:
        from ..services.item_ledger.current_execution import (
            CurrentExecutionUnavailable,
            load_current_execution_rows,
            require_current_execution_scope,
        )
        manifest = require_current_execution_scope(
            db,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )
        saved = dict(manifest.summary or {})
        cards = saved.get("cards")
        if isinstance(cards, dict) and str(int(order_id)) in cards:
            return {**dict(cards[str(int(order_id))]), "meta": saved}
        rows = [dict(row.payload or {}) for row in load_current_execution_rows(
            db,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        ) if row.payload and row.payload.get("order_id") == int(order_id)]
        if not rows:
            raise ValueError(f"Supplier order {order_id} not found in current purchase journal")
        return {"order_id": int(order_id), "lines": rows, "meta": saved}
    except PurchaseJournalSnapshotUnavailable as e:
        raise HTTPException(status_code=503, detail=e.as_dict())
    except CurrentExecutionUnavailable as e:
        raise HTTPException(status_code=503, detail={"code": "purchase_control_current_unavailable", "reason": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/filters", response_model=dict)
def get_filters(db: Session = Depends(get_db)):
    """Справочники для фильтров журнала: поставщики и состояния заказов 1С."""
    try:
        from ..services.item_ledger.current_execution import (
            CurrentExecutionUnavailable,
            load_current_execution_rows,
            require_current_execution_scope,
        )
        require_current_execution_scope(
            db,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )
        manifest = require_current_execution_scope(
            db,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )
        rows = [dict(row.payload or {}) for row in load_current_execution_rows(
            db,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )]
        saved = dict(manifest.summary or {})
        card_rows = [
            line
            for card in (saved.get("cards") or {}).values()
            if isinstance(card, dict)
            for line in (card.get("lines") or [])
            if isinstance(line, dict)
        ]
        all_rows = [*rows, *card_rows]
        suppliers = sorted({
            (int(row["supplier_id"]), str(row.get("supplier_name") or ""))
            for row in all_rows if row.get("supplier_id") is not None
        }, key=lambda value: (value[1].casefold(), value[0]))
        states = sorted({
            str(row["order_state_name"])
            for row in all_rows if row.get("order_state_name")
        })
        return {
            "suppliers": [{"supplier_id": value, "supplier_name": name} for value, name in suppliers],
            "states": states,
        }
    except PurchaseJournalSnapshotUnavailable as e:
        raise HTTPException(status_code=503, detail=e.as_dict())
    except CurrentExecutionUnavailable as e:
        raise HTTPException(status_code=503, detail={"code": "purchase_control_current_unavailable", "reason": str(e)})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/selection-summary",
    response_model=PurchaseControlSelectionSummaryResponse,
)
def summarize_purchase_control_selection(
    payload: PurchaseControlSelectionSummaryRequest,
    db: Session = Depends(get_db),
):
    """Backend-owned totals for the selected immutable purchase rows."""
    try:
        return get_selection_summary(
            db,
            snapshot_id=payload.snapshot_id,
            row_keys=payload.row_keys,
            horizon_period_to=payload.horizon_period_to,
        )
    except PurchaseJournalSnapshotUnavailable as e:
        raise HTTPException(status_code=503, detail=e.as_dict())
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/materialize", response_model=dict)
def materialize_purchase_control_rows(
    payload: PurchaseControlMaterializeRequest,
    db: Session = Depends(get_db),
):
    """Materialize selected neutral MRP purchase rows from the accepted snapshot."""
    try:
        return materialize_rows(
            db,
            snapshot_id=payload.snapshot_id,
            row_keys=payload.row_keys,
            dry_run=payload.dry_run,
        )
    except PurchaseControlSnapshotUnavailable as e:
        raise HTTPException(status_code=503, detail=e.detail)
    except PurchaseJournalSnapshotUnavailable as e:
        raise HTTPException(status_code=503, detail=e.as_dict())
    except PurchaseControlMaterializerNotConfigured as e:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "purchase_control_materializer_not_configured",
                "consumer": "purchase_control",
                "reason": str(e),
            },
        )
    except PurchaseControlMaterializationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

from __future__ import annotations

from datetime import date
from typing import Optional

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
from ..services.purchase_control_journal import get_order_card, list_filters, list_journal
from ..services.purchase_control_snapshot import PurchaseJournalSnapshotUnavailable

router = APIRouter(prefix="/v1/purchase-control", tags=["purchase-control"])


class PurchaseControlMaterializeRequest(BaseModel):
    snapshot_id: int = Field(..., ge=1)
    row_keys: list[str] = Field(default_factory=list)
    dry_run: bool = True


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
        return list_journal(
            db,
            order_id=order_id,
            supplier_id=supplier_id,
            state=state,
            phase=phase,
            line_status=line_status,
            search=search,
            date_from=date_from,
            date_to=date_to,
            active_only=active_only,
            include_to_order=include_to_order,
            horizon_period_to=horizon_period_to,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    except PurchaseJournalSnapshotUnavailable as e:
        raise HTTPException(status_code=503, detail=e.as_dict())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders/{order_id}", response_model=dict)
def get_order(order_id: int, db: Session = Depends(get_db)):
    """Карточка заказа поставщику со всеми строками (для detail pane)."""
    try:
        return get_order_card(db, int(order_id))
    except PurchaseJournalSnapshotUnavailable as e:
        raise HTTPException(status_code=503, detail=e.as_dict())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/filters", response_model=dict)
def get_filters(db: Session = Depends(get_db)):
    """Справочники для фильтров журнала: поставщики и состояния заказов 1С."""
    try:
        return list_filters(db)
    except PurchaseJournalSnapshotUnavailable as e:
        raise HTTPException(status_code=503, detail=e.as_dict())
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

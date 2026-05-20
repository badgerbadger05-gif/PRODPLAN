from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import ODataSyncRequest
from ..services.production_control import (
    create_material_issues,
    create_orders_from_mrp,
    get_issue,
    list_journal,
    mark_route_sheets_printed,
    preview_materials,
    render_route_sheets_html,
    export_issue_to_1c,
    update_line_state,
)


router = APIRouter(prefix="/v1/production-control", tags=["production-control"])


class LineStatePayload(BaseModel):
    status: Optional[str] = None
    issue_status: Optional[str] = None
    workshop_id: Optional[int] = None
    planned_start_date: Optional[str] = None
    planned_finish_date: Optional[str] = None
    comment: Optional[str] = None


class MaterialIssueCreatePayload(BaseModel):
    product_ids: List[int]
    initiated_by: Optional[str] = None
    warehouse_ref1c: Optional[str] = None


class OrdersFromMrpPayload(BaseModel):
    planned_order_ids: List[int]
    initiated_by: Optional[str] = None


@router.get("/orders", response_model=dict)
def get_orders_journal(
    workshop_id: Optional[int] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    try:
        return list_journal(
            db,
            workshop_id=workshop_id,
            status=status,
            search=search,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/orders/{product_id}/state", response_model=dict)
def patch_order_line_state(product_id: int, payload: LineStatePayload, db: Session = Depends(get_db)):
    try:
        return update_line_state(db, int(product_id), payload.dict(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders/{product_id}/materials", response_model=dict)
def get_order_line_materials(product_id: int, db: Session = Depends(get_db)):
    try:
        return preview_materials(db, int(product_id))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/orders/from-mrp", response_model=dict)
def post_orders_from_mrp(payload: OrdersFromMrpPayload, db: Session = Depends(get_db)):
    """
    Materialize selected MRP planned_order rows as internal production orders
    (production_orders.source='mrp'). Idempotent: planned_orders that already
    back a production_products line are returned under `reused`.
    """
    if not payload.planned_order_ids:
        raise HTTPException(status_code=400, detail="Не выбраны строки MRP")
    try:
        return create_orders_from_mrp(
            db,
            [int(x) for x in payload.planned_order_ids],
            initiated_by=payload.initiated_by,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/material-issues", response_model=dict)
def post_material_issues(payload: MaterialIssueCreatePayload, db: Session = Depends(get_db)):
    if not payload.product_ids:
        raise HTTPException(status_code=400, detail="Не выбраны строки заказа")
    try:
        return create_material_issues(
            db,
            [int(x) for x in payload.product_ids],
            initiated_by=payload.initiated_by,
            warehouse_ref1c=payload.warehouse_ref1c,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/material-issues/{issue_id}", response_model=dict)
def get_material_issue(issue_id: int, db: Session = Depends(get_db)):
    try:
        return get_issue(db, int(issue_id))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/material-issues/{issue_id}/export-to-1c", response_model=dict)
def post_material_issue_to_1c(issue_id: int, payload: ODataSyncRequest, db: Session = Depends(get_db)):
    """
    Экспорт внутреннего документа выдачи в 1С.

    Для первой настройки используйте dry_run=true: endpoint вернет payload, который
    нужно сопоставить с фактическим документом/обработкой в вашей конфигурации 1С.
    """
    try:
        return export_issue_to_1c(db, int(issue_id), payload)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/route-sheets/print", response_class=HTMLResponse)
def print_route_sheets(
    product_ids: str = Query(..., description="Comma-separated production product ids"),
    mark_printed: bool = True,
    db: Session = Depends(get_db),
):
    try:
        ids = [int(x) for x in product_ids.split(",") if x.strip()]
        if not ids:
            raise ValueError("Не выбраны строки заказа")
        html = render_route_sheets_html(db, ids)
        if mark_printed:
            mark_route_sheets_printed(db, ids)
        return HTMLResponse(content=html)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

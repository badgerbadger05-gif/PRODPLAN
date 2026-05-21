from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import ODataSyncRequest
from ..services.one_c_posted_transfer_sync import sync_posted_transfers
from ..services.one_c_production_order_export import export_production_orders_to_1c
from ..services.one_c_stock_transfer_export import export_material_issues_to_1c
from ..services.production_control import (
    create_material_issues,
    create_orders_from_mrp,
    delete_ignored_warehouse,
    delete_workshop_binding,
    export_issue_to_1c,
    get_issue,
    list_journal,
    list_settings,
    mark_route_sheets_printed,
    preview_materials,
    render_route_sheets_html,
    update_line_state,
    upsert_ignored_warehouse,
    upsert_workshop_binding,
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


class WorkshopBindingPayload(BaseModel):
    warehouse_ref1c: str


class IgnoredWarehousePayload(BaseModel):
    warehouse_ref1c: str
    warehouse_name: Optional[str] = None
    reason: Optional[str] = None


class ExportProductionOrdersPayload(BaseModel):
    order_ids: List[int]
    dry_run: bool = True
    allow_production: bool = False


class ExportMaterialIssuesPayload(BaseModel):
    issue_ids: List[int]
    dry_run: bool = True
    allow_production: bool = False


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


@router.post("/orders/export-to-1c", response_model=dict)
def post_export_production_orders_to_1c(
    payload: ExportProductionOrdersPayload,
    db: Session = Depends(get_db),
):
    """
    Export selected internal MRP production_orders to 1C as
    Document_ЗаказНаПроизводство (Posted=false).

    Idempotent via sync_link: orders already linked are returned in the
    response under entries[].status='existing' and not re-sent.

    Safety:
    - Default `dry_run=true` returns the payload that would be sent without
      contacting 1C.
    - To actually write, pass `dry_run=false`. Refuses non-demo base_url
      unless `allow_production=true` is also set.
    """
    if not payload.order_ids:
        raise HTTPException(status_code=400, detail="Не выбраны заказы для экспорта")
    try:
        return export_production_orders_to_1c(
            db,
            [int(x) for x in payload.order_ids],
            dry_run=bool(payload.dry_run),
            allow_production=bool(payload.allow_production),
        )
    except PermissionError as e:
        # Demo-DB safety guard tripped.
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


@router.post("/material-issues/export-to-1c", response_model=dict)
def post_export_material_issues_to_1c(
    payload: ExportMaterialIssuesPayload,
    db: Session = Depends(get_db),
):
    """
    Bulk-экспорт выдач материалов в 1С как Document_ПеремещениеЗапасов
    (Posted=false). Идемпотентно через sync_link.

    - `dry_run=true` (default) — возвращает payload, не пишет в 1С.
    - `dry_run=false` — реально пишет; refuse при non-demo base_url без
      `allow_production=true`.
    """
    if not payload.issue_ids:
        raise HTTPException(status_code=400, detail="Не выбраны документы выдачи")
    try:
        return export_material_issues_to_1c(
            db,
            [int(x) for x in payload.issue_ids],
            dry_run=bool(payload.dry_run),
            allow_production=bool(payload.allow_production),
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/material-issues/{issue_id}/export-to-1c", response_model=dict, deprecated=True)
def post_material_issue_to_1c_legacy(
    issue_id: int,
    payload: ODataSyncRequest,
    db: Session = Depends(get_db),
):
    """
    Legacy: single-issue export. Kept for backwards-compatibility with
    existing clients. Prefer POST /material-issues/export-to-1c.
    """
    try:
        return export_issue_to_1c(db, int(issue_id), payload)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sync-posted-transfers", response_model=dict)
def post_sync_posted_transfers(dry_run: bool = False, db: Session = Depends(get_db)):
    """
    Pull Posted=true flag from 1C for previously-exported material-issue
    transfers and advance local state to 'assembled' per plan rule
    ("К перемещению" -> "Собран"). Read-only on the 1C side.

    `?dry_run=true` performs the same reads but skips local DB writes.
    """
    try:
        return sync_posted_transfers(db, dry_run=bool(dry_run))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


# ---------------------------------------------------------------------------
# Settings: workshop -> warehouse bindings + ignored warehouses
# ---------------------------------------------------------------------------


@router.get("/settings", response_model=dict)
def get_settings(db: Session = Depends(get_db)):
    """Return current workshop->warehouse bindings and ignored warehouses."""
    return list_settings(db)


@router.put("/settings/workshop-bindings/{workshop_id}", response_model=dict)
def put_workshop_binding(
    workshop_id: int,
    payload: WorkshopBindingPayload,
    db: Session = Depends(get_db),
):
    try:
        return upsert_workshop_binding(db, int(workshop_id), payload.warehouse_ref1c)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/settings/workshop-bindings/{workshop_id}", response_model=dict)
def delete_binding(workshop_id: int, db: Session = Depends(get_db)):
    try:
        return delete_workshop_binding(db, int(workshop_id))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/settings/ignored-warehouses", response_model=dict)
def post_ignored_warehouse(payload: IgnoredWarehousePayload, db: Session = Depends(get_db)):
    try:
        return upsert_ignored_warehouse(
            db,
            payload.warehouse_ref1c,
            warehouse_name=payload.warehouse_name,
            reason=payload.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/settings/ignored-warehouses/{warehouse_ref1c}", response_model=dict)
def delete_ignored(warehouse_ref1c: str, db: Session = Depends(get_db)):
    try:
        return delete_ignored_warehouse(db, str(warehouse_ref1c))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

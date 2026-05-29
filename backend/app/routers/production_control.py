from __future__ import annotations

from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Employee
from ..schemas import ODataSyncRequest
from ..services.one_c_manufacture_export import export_manufactures_to_1c
from ..services.one_c_piecework_export import export_piecework_to_1c
from ..services.one_c_posted_transfer_sync import sync_posted_transfers
from ..services.one_c_production_order_export import export_production_orders_to_1c
from ..services.one_c_stock_transfer_export import export_material_issues_to_1c
from ..services.production_control_material_issues import (
    assemble_material_issue,
    create_material_issues,
    export_issue_to_1c,
    get_issue,
    list_material_issues,
)
from ..services.production_control_journal import (
    create_orders_from_mrp,
    create_production_orders_from_mrp_requirements,
    list_journal,
    update_line_state,
    update_product_quantity,
)
from ..services.production_control_material_availability import preview_materials
from ..services.production_control_printing import mark_route_sheets_printed, render_route_sheets_html
from ..services.production_control_production_flow import (
    produce_line,
    return_leftover_components,
    rollback_local_manufacture,
)
from .production_control_settings import router as settings_router


router = APIRouter(prefix="/v1/production-control", tags=["production-control"])


@router.get("/employees", response_model=dict)
def list_employees(
    search: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    db: Session = Depends(get_db),
):
    query = db.query(Employee).filter(Employee.deletion_mark.is_(False))
    if search:
        pattern = f"%{search.strip()}%"
        query = query.filter(
            (Employee.employee_name.ilike(pattern))
            | (Employee.employee_code.ilike(pattern))
        )
    rows = (
        query.order_by(Employee.employee_name.asc(), Employee.employee_code.asc())
        .limit(int(limit))
        .all()
    )
    return {
        "rows": [
            {
                "employee_id": int(row.employee_id),
                "employee_ref1c": row.employee_ref1c,
                "employee_code": row.employee_code,
                "employee_name": row.employee_name,
            }
            for row in rows
        ],
        "total": len(rows),
    }


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
    source_warehouse_ref1c: Optional[str] = None


class OrdersFromMrpPayload(BaseModel):
    planned_order_ids: List[int]
    initiated_by: Optional[str] = None


class OrdersFromMrpRequirementsPayload(BaseModel):
    requirement_ids: List[int]
    initiated_by: Optional[str] = None


class UpdateQuantityPayload(BaseModel):
    quantity: float


class ExportProductionOrdersPayload(BaseModel):
    order_ids: List[int]
    dry_run: bool = True
    allow_production: bool = False


class ExportMaterialIssuesPayload(BaseModel):
    issue_ids: List[int]
    dry_run: bool = True
    allow_production: bool = False


class AssembleMaterialIssuePayload(BaseModel):
    allow_production: bool = False


class ProduceLinePayload(BaseModel):
    qty: float
    executor: Optional[str] = None
    comment: Optional[str] = None


class ExportManufacturesPayload(BaseModel):
    manufacture_ids: List[int]
    dry_run: bool = True
    allow_production: bool = False


class ExportPieceworkPayload(BaseModel):
    manufacture_ids: List[int]
    operation_ref: Optional[str] = None
    time_norm: float = 0.0
    price: float = 0.0
    organization_ref: Optional[str] = None
    structural_unit_ref: Optional[str] = None
    business_operation_ref: Optional[str] = None
    dry_run: bool = True
    allow_production: bool = False


@router.get("/orders", response_model=dict)
def get_orders_journal(
    workshop_id: Optional[int] = None,
    status: Optional[str] = None,
    coverage_status: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    try:
        return list_journal(
            db,
            workshop_id=workshop_id,
            status=status,
            coverage_status=coverage_status,
            search=search,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/orders/{product_id}/quantity", response_model=dict)
def patch_order_line_quantity(product_id: int, payload: UpdateQuantityPayload, db: Session = Depends(get_db)):
    """
    Adjust the planned quantity of a production line.
    Cannot be set below already-produced qty.
    Recalculates remaining_qty automatically.
    """
    try:
        return update_product_quantity(db, int(product_id), float(payload.quantity))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


@router.post("/orders/{product_id}/produce", response_model=dict)
def post_produce_line(
    product_id: int,
    payload: ProduceLinePayload,
    db: Session = Depends(get_db),
):
    """
    Record one production event on the line. Bumps produced_qty / decreases
    remaining_qty / promotes line status to produced_partial or produced.
    Creates a ProductionManufacture row. Local only вЂ” does NOT send to 1C;
    use POST /manufactures/export-to-1c for that.
    """
    try:
        return produce_line(
            db,
            int(product_id),
            qty=float(payload.qty),
            executor=payload.executor,
            comment=payload.comment,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/orders/{product_id}/return-leftovers", response_model=dict)
def post_return_leftovers(
    product_id: int,
    initiated_by: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Создать обратное перемещение лишних компонентов на исходные склады для
    частично произведённой строки. Локально только вЂ” в 1С документ
    отправится отдельно через /material-issues/export-to-1c.

    Возвращает либо status='ok' с return_issue_id и list of lines, либо
    status='skipped' с человекочитаемой причиной (produced_qty=0, нет
    выгруженных исходящих перемещений, или нет компонентов с остатком).
    """
    try:
        return return_leftover_components(db, int(product_id), initiated_by=initiated_by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/manufactures/export-to-1c", response_model=dict)
def post_export_manufactures_to_1c(
    payload: ExportManufacturesPayload,
    db: Session = Depends(get_db),
):
    """
    Bulk-экспорт выпусков (производств) в 1С как Document_СборкаЗапасов
    (Posted=false). РРґРµРјРїРѕС‚РµРЅС‚РЅРѕ через sync_link.
    """
    if not payload.manufacture_ids:
        raise HTTPException(status_code=400, detail="Не выбраны выпуски")
    try:
        return export_manufactures_to_1c(
            db,
            [int(x) for x in payload.manufacture_ids],
            dry_run=bool(payload.dry_run),
            allow_production=bool(payload.allow_production),
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/manufactures/{manufacture_id}/rollback-local", response_model=dict)
def post_rollback_local_manufacture(
    manufacture_id: int,
    db: Session = Depends(get_db),
):
    try:
        return rollback_local_manufacture(db, int(manufacture_id))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/manufactures/export-piecework-to-1c", response_model=dict)
def post_export_piecework_to_1c(
    payload: ExportPieceworkPayload,
    db: Session = Depends(get_db),
):
    """
    Bulk-экспорт выпусков в 1С как Document_СдельныйНаряд.
    Идемпотентно через sync_link (source_doctype='piecework').

    Требование: каждый manufacture должен быть уже выгружен как
    Document_СборкаЗапасов (поле exported_ref1c заполнено) — он используется
    как ДокументОснование сдельного наряда. Операция по умолчанию берется
    из спецификации выпуска; operation_ref нужен только для ручного override.
    """
    if not payload.manufacture_ids:
        raise HTTPException(status_code=400, detail="Не выбраны выпуски")
    try:
        return export_piecework_to_1c(
            db,
            [int(x) for x in payload.manufacture_ids],
            operation_ref=payload.operation_ref,
            time_norm=float(payload.time_norm),
            price=float(payload.price),
            organization_ref=payload.organization_ref,
            structural_unit_ref=payload.structural_unit_ref,
            business_operation_ref=payload.business_operation_ref,
            dry_run=bool(payload.dry_run),
            allow_production=bool(payload.allow_production),
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


@router.post("/orders/from-mrp-requirements", response_model=dict)
def post_orders_from_mrp_requirements(
    payload: OrdersFromMrpRequirementsPayload,
    db: Session = Depends(get_db),
):
    """
    Materialize selected MrpRequirement rows (production-flow items from a
    period-plan MRP snapshot) into internal production orders.

    Idempotent: requirements that already have a ProductionProduct linked via
    source_mrp_requirement_id are returned under `reused`.
    Purchase/rework requirements are returned under `skipped`.
    MrpRequirement.covered_qty / remaining_qty are updated.
    """
    if not payload.requirement_ids:
        raise HTTPException(status_code=400, detail="Не выбраны требования MRP")
    try:
        return create_production_orders_from_mrp_requirements(
            db,
            [int(x) for x in payload.requirement_ids],
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
            source_warehouse_ref1c=payload.source_warehouse_ref1c,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/material-issues", response_model=dict)
def get_material_issues_journal(
    status: Optional[str] = None,
    search: Optional[str] = None,
    source_warehouse_ref1c: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    try:
        return list_material_issues(
            db,
            status=status,
            search=search,
            source_warehouse_ref1c=source_warehouse_ref1c,
            limit=limit,
            offset=offset,
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
    (Posted=false). РРґРµРјРїРѕС‚РµРЅС‚РЅРѕ через sync_link.

    - `dry_run=true` (default) вЂ” возвращает payload, не пишет в 1С.
    - `dry_run=false` вЂ” реально пишет; refuse при non-demo base_url без
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


@router.post("/material-issues/{issue_id}/assembled", response_model=dict)
def post_material_issue_assembled(
    issue_id: int,
    payload: AssembleMaterialIssuePayload,
    db: Session = Depends(get_db),
):
    try:
        return assemble_material_issue(
            db,
            int(issue_id),
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


router.include_router(settings_router)


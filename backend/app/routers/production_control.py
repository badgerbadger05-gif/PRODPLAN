from __future__ import annotations

from typing import Annotated, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from .. import models
from ..services import planning_truth
from ..routers.truth_meta import TruthMeta, build_truth_meta
from ..database import get_db
from ..models import DefaultSpecification, Employee, Operation, ProductionProduct, ProductionStage, Specification, SpecOperation
from ..services.one_c_manufacture_export import export_manufactures_to_1c
from ..services.one_c_posted_transfer_sync import sync_posted_transfers
from ..services.one_c_piecework_export import export_piecework_to_1c
from ..services.one_c_production_order_export import export_production_orders_to_1c
from ..services.one_c_stock_transfer_export import export_material_issues_to_1c
from ..services.production_control_material_issues import (
    assemble_material_issue,
    create_material_issues,
    delete_local_material_issue,
    get_issue,
    list_material_issues,
)
from ..services.production_control_journal import (
    materialize_make_work_items,
    cancel_local_order,
    dedupe_mrp_production_orders,
    list_journal,
    update_line_state,
    update_product_quantity,
)
from ..services.production_control_material_availability import (
    get_materials_snapshot,
    preview_materials,
    refresh_materials_snapshot,
)
from ..services.production_control_printing import mark_route_sheets_printed, render_route_sheets_html
from ..services.production_control_production_flow import (
    produce_line,
    return_leftover_components,
    rollback_local_manufacture,
)
from .production_control_settings import router as settings_router


router = APIRouter(prefix="/v1/production-control", tags=["production-control"])


class AssemblyQueueRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    plan_line_id: int
    run_id: int
    item_id: int
    item_code: str
    item_name: str
    bucket_date: str
    period_from: str
    period_to: str
    planned_output_qty: float
    accepted_plan_output_qty: float
    assembly_remaining_qty: float
    priority_key: list[Union[str, int]]


class AssemblyQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[AssemblyQueueRow]
    total_rows: int
    total_queue_qty: float
    truth_meta: TruthMeta


class DrumSlotRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    plan_line_id: int
    item_id: int
    resource_id: int
    slot_date: str
    slot_qty: float
    slot_ordinal: int
    original_priority: list[Union[str, int]]


class DrumGapRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    plan_line_id: int
    item_id: int
    resource_id: int
    gap_date: str
    gap_qty: float
    original_priority: list[Union[str, int]]


class DrumScheduleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule_from: str
    schedule_to: str
    slots: list[DrumSlotRow]
    gaps: list[DrumGapRow]
    total_open_qty: float
    total_slot_qty: float
    total_gap_qty: float
    truth_meta: TruthMeta


class ShelfProjectionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: int
    item_id: int
    warehouse_ref1c: str
    protection_until: str
    target_qty: float
    shelf_physical_qty: float
    other_stock_qty: float
    projected_qty: float
    gap_qty: float
    transfer_qty: float
    unlaunched_mrp_qty: float
    pull_qty: float
    materialized_qty: float
    first_shortage_date: str | None
    latest_start_date: str | None


class ShelfProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[ShelfProjectionRow]
    total_rows: int
    truth_meta: TruthMeta


@router.get("/assembly-queue", response_model=AssemblyQueueResponse)
def get_assembly_queue(
    db: Session = Depends(get_db),
) -> AssemblyQueueResponse:
    """Read the immutable queue belonging to the exact accepted generation."""
    try:
        snapshot = planning_truth.get_latest_read_snapshot(
            db,
            consumer="assembly_queue",
            snapshot_key="current:v1",
            required_capabilities=(
                planning_truth.CAPABILITY_PHYSICAL_LEDGER,
                planning_truth.CAPABILITY_RESERVATION_REPLAY,
                planning_truth.CAPABILITY_PLANNING_SNAPSHOTS,
                planning_truth.CAPABILITY_ASSEMBLY_QUEUE,
            ),
        )
    except planning_truth.PlanningTruthUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.as_dict()) from exc
    if snapshot is None:
        readiness = planning_truth.get_truth_state(db)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "assembly_queue_unavailable",
                "reason": "assembly queue snapshot is missing for accepted generation",
                **readiness.as_dict(),
            },
        )
    payload = dict(snapshot.payload or {})
    response = {
        "rows": list(payload.get("rows") or []),
        "total_rows": int(payload.get("total_rows") or 0),
        "total_queue_qty": float(payload.get("total_queue_qty") or 0),
        "truth_meta": build_truth_meta(planning_truth.get_truth_state(db)),
    }
    return AssemblyQueueResponse.model_validate(response)


@router.get("/drum", response_model=DrumScheduleResponse)
def get_drum_schedule(db: Session = Depends(get_db)) -> DrumScheduleResponse:
    """Read the persisted drum of the exact accepted generation."""
    try:
        truth = planning_truth.require_accepted_truth(
            db,
            "drum_schedule",
            required_capabilities=(
                planning_truth.CAPABILITY_PHYSICAL_LEDGER,
                planning_truth.CAPABILITY_ASSEMBLY_QUEUE,
                planning_truth.CAPABILITY_DRUM_SCHEDULE,
            ),
        )
    except planning_truth.PlanningTruthUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.as_dict()) from exc
    schedule = (
        db.query(models.DrumSchedule)
        .filter(models.DrumSchedule.ledger_generation_id == truth.generation_id)
        .one_or_none()
    )
    if schedule is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "drum_schedule_unavailable",
                "reason": "drum schedule is missing for accepted generation",
                **truth.as_dict(),
            },
        )
    slots = (
        db.query(models.DrumSlot)
        .filter(models.DrumSlot.drum_schedule_id == schedule.id)
        .order_by(
            models.DrumSlot.slot_date,
            models.DrumSlot.resource_id,
            models.DrumSlot.slot_ordinal,
            models.DrumSlot.id,
        )
        .all()
    )
    gaps = (
        db.query(models.DrumCapacityGap)
        .filter(models.DrumCapacityGap.drum_schedule_id == schedule.id)
        .order_by(
            models.DrumCapacityGap.gap_date,
            models.DrumCapacityGap.resource_id,
            models.DrumCapacityGap.id,
        )
        .all()
    )
    return DrumScheduleResponse.model_validate(
        {
            "schedule_from": schedule.schedule_from.isoformat(),
            "schedule_to": schedule.schedule_to.isoformat(),
            "slots": [
                {
                    "plan_id": row.plan_id,
                    "plan_line_id": row.plan_line_id,
                    "item_id": row.item_id,
                    "resource_id": row.resource_id,
                    "slot_date": row.slot_date.isoformat(),
                    "slot_qty": float(row.slot_qty),
                    "slot_ordinal": row.slot_ordinal,
                    "original_priority": list(row.original_priority or []),
                }
                for row in slots
            ],
            "gaps": [
                {
                    "plan_id": row.plan_id,
                    "plan_line_id": row.plan_line_id,
                    "item_id": row.item_id,
                    "resource_id": row.resource_id,
                    "gap_date": row.gap_date.isoformat(),
                    "gap_qty": float(row.gap_qty),
                    "original_priority": list(row.original_priority or []),
                }
                for row in gaps
            ],
            "total_open_qty": float(schedule.total_open_qty),
            "total_slot_qty": float(schedule.total_slot_qty),
            "total_gap_qty": float(schedule.total_gap_qty),
            "truth_meta": build_truth_meta(truth),
        }
    )


@router.get("/shelves", response_model=ShelfProjectionResponse)
def get_shelf_projections(
    db: Session = Depends(get_db),
) -> ShelfProjectionResponse:
    """Read persisted shelf pull priorities of the accepted generation."""
    try:
        truth = planning_truth.require_accepted_truth(
            db,
            "shelf_projection",
            required_capabilities=(
                planning_truth.CAPABILITY_PHYSICAL_LEDGER,
                planning_truth.CAPABILITY_DRUM_SCHEDULE,
                planning_truth.CAPABILITY_SHELF_PROJECTION,
            ),
        )
    except planning_truth.PlanningTruthUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.as_dict()) from exc
    rows = (
        db.query(models.ShelfProjection)
        .filter(
            models.ShelfProjection.ledger_generation_id == truth.generation_id
        )
        .order_by(
            models.ShelfProjection.latest_start_date.asc().nullslast(),
            models.ShelfProjection.item_id,
            models.ShelfProjection.id,
        )
        .all()
    )
    payload = [
        {
            "policy_id": row.shelf_policy_id,
            "item_id": row.item_id,
            "warehouse_ref1c": row.warehouse_ref1c,
            "protection_until": row.protection_until.isoformat(),
            "target_qty": float(row.target_qty),
            "shelf_physical_qty": float(row.shelf_physical_qty),
            "other_stock_qty": float(row.other_stock_qty),
            "projected_qty": float(row.projected_qty),
            "gap_qty": float(row.gap_qty),
            "transfer_qty": float(row.transfer_qty),
            "unlaunched_mrp_qty": float(row.unlaunched_mrp_qty),
            "pull_qty": float(row.pull_qty),
            "materialized_qty": float(row.materialized_qty),
            "first_shortage_date": (
                row.first_shortage_date.isoformat()
                if row.first_shortage_date
                else None
            ),
            "latest_start_date": (
                row.latest_start_date.isoformat()
                if row.latest_start_date
                else None
            ),
        }
        for row in rows
    ]
    return ShelfProjectionResponse.model_validate(
        {
            "rows": payload,
            "total_rows": len(payload),
            "truth_meta": build_truth_meta(truth),
        }
    )


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
                "employee_type": getattr(row, "employee_type", "employee") or "employee",
                "employee_code": row.employee_code,
                "employee_name": row.employee_name,
            }
            for row in rows
        ],
        "total": len(rows),
    }


@router.get("/orders/{product_id}/operations", response_model=dict)
def get_order_line_operations(
    product_id: int,
    db: Session = Depends(get_db),
):
    product = (
        db.query(ProductionProduct)
        .filter(ProductionProduct.product_id == int(product_id))
        .one_or_none()
    )
    if product is None:
        raise HTTPException(status_code=404, detail="Строка заказа не найдена")
    spec_id = product.spec_id
    if not spec_id:
        default_spec = (
            db.query(DefaultSpecification.spec_id)
            .filter(DefaultSpecification.item_id == int(product.item_id))
            .order_by(DefaultSpecification.id.asc())
            .first()
        )
        spec_id = int(default_spec.spec_id) if default_spec else None
    if not spec_id:
        return {"rows": [], "total": 0}
    spec = db.query(Specification).filter(Specification.spec_id == int(spec_id)).one_or_none()
    rows = (
        db.query(SpecOperation, Operation, ProductionStage)
        .join(Operation, Operation.operation_id == SpecOperation.operation_id)
        .outerjoin(ProductionStage, ProductionStage.stage_id == SpecOperation.stage_id)
        .filter(SpecOperation.spec_id == int(spec_id))
        .filter(Operation.operation_ref1c.isnot(None))
        .order_by(SpecOperation.spec_operation_id.asc())
        .all()
    )
    result = [
        {
            "line_number": idx,
            "spec_id": int(spec_id),
            "spec_ref1c": spec.spec_ref1c if spec else None,
            "spec_operation_id": int(spec_op.spec_operation_id),
            "operation_id": int(operation.operation_id),
            "operation_ref1c": operation.operation_ref1c,
            "operation_name": operation.operation_name,
            "stage_id": int(stage.stage_id) if stage else None,
            "stage_name": stage.stage_name if stage else None,
            "time_norm": float(spec_op.time_norm if spec_op.time_norm is not None else operation.time_norm or 0),
        }
        for idx, (spec_op, operation, stage) in enumerate(rows, start=1)
    ]
    return {"rows": result, "total": len(result)}


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


class OrdersFromWorkItemsPayload(BaseModel):
    work_item_ids: List[int]
    initiated_by: Optional[str] = None


class DedupeMrpOrdersPayload(BaseModel):
    dry_run: bool = True


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


class ProduceLinePayload(BaseModel):
    qty: float
    executor: Optional[str] = None
    operation_executors: Optional[List[dict]] = None
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


class AssembleMaterialIssuePayload(BaseModel):
    allow_production: bool = False


class PrintRouteSheetsPayload(BaseModel):
    product_ids: List[int]
    mark_printed: bool = True
    auto_print: bool = True



class PaintWeldChainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    link_id: int
    counterpart_order_id: Optional[int] = None
    counterpart_product_id: Optional[int] = None


class ProductionOrderJournalRowResponse(BaseModel):
    """One real production line in the unified production-control journal."""

    model_config = ConfigDict(extra="forbid")

    product_id: int
    order_id: int
    order_number: str
    order_prodplan_number: Optional[str] = None
    order_date: Optional[str] = None
    order_source: str
    source: str
    order_ref1c: Optional[str] = None
    order_one_c_number: Optional[str] = None
    line_number: Optional[Union[int, str]] = None
    item_id: int
    item_code: str
    item_name: str
    item_article: str
    optimal_batch: Optional[float] = None
    unit: str
    quantity: float
    produced_qty: float
    remaining_qty: float
    status: str
    coverage_status: str
    coverage_label: str
    issue_status: str
    material_coverage_status: Optional[str] = None
    material_coverage_label: Optional[str] = None
    material_coverage_calculated_at: Optional[str] = None
    planned_start_date: Optional[str] = None
    planned_finish_date: Optional[str] = None
    forecast_date: Optional[str] = None
    forecast_shift_days: Optional[int] = None
    forecast_reason: Optional[str] = None
    opened_at: Optional[str] = None
    workshop_id: Optional[int] = None
    workshop_name: Optional[str] = None
    stage_id: Optional[int] = None
    stage_name: Optional[str] = None
    spec_id: Optional[int] = None
    issue_count: int
    route_sheet_printed_at: Optional[str] = None
    comment: str
    source_run_id: Optional[int] = None
    source_plan_id: Optional[int] = None
    source_plan_name: Optional[str] = None
    source_plan_period_from: Optional[str] = None
    source_plan_period_to: Optional[str] = None
    source_planned_order_id: Optional[int] = None
    source_mrp_requirement_id: Optional[int] = None
    source_mrp_allocation_key: Optional[str] = None
    mrp_req_net_qty: Optional[float] = None
    mrp_req_covered_qty: Optional[float] = None
    mrp_req_remaining_qty: Optional[float] = None
    paint_weld_chain: Optional[PaintWeldChainResponse] = None


class ProductionOrderJournalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: List[ProductionOrderJournalRowResponse]
    total: int
    limit: int
    offset: int
    latest_run_id: Optional[int] = None
    latest_source_plan_id: Optional[int] = None
    truth_meta: TruthMeta


@router.get("/orders", response_model=ProductionOrderJournalResponse)
def get_orders_journal(
    product_id: Optional[int] = None,
    order_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    workshop_id: Optional[int] = None,
    status: Optional[str] = None,
    coverage_status: Optional[str] = None,
    planning_contour: Optional[str] = Query(
        None,
        description="Контур планирования: mrp или 1c для источника заказа.",
    ),
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
        truth = planning_truth.require_accepted_truth(db, "production_control.orders")
        journal = list_journal(
            db,
            truth=truth,
            product_id=product_id,
            order_id=order_id,
            root_item_id=root_item_id,
            workshop_id=workshop_id,
            status=status,
            coverage_status=coverage_status,
            planning_contour=planning_contour,
            search=search,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            sort_dir=sort_dir,
            limit=limit,
            offset=offset,
        )
        journal["truth_meta"] = build_truth_meta(truth).model_dump()
        return journal
    except planning_truth.PlanningTruthUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.as_dict()) from exc
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


@router.delete("/orders/{product_id}", response_model=dict)
def delete_local_order(product_id: int, db: Session = Depends(get_db)):
    try:
        return cancel_local_order(db, int(product_id))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/orders/dedupe-mrp", response_model=dict)
def post_dedupe_mrp_orders(payload: DedupeMrpOrdersPayload, db: Session = Depends(get_db)):
    """
    Repair local MRP production-order overcoverage.

    Dry-run by default. The applied mode only touches local PRODPLAN MRP rows
    that are not linked to 1C: duplicates are cancelled, and a single oversized
    row is reduced to the latest MRP requirement.
    """
    try:
        return dedupe_mrp_production_orders(db, dry_run=bool(payload.dry_run))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/orders/{product_id}/materials", response_model=dict)
def get_order_line_materials(
    product_id: int,
    # Kept temporarily so old clients do not break. GET never honours this as
    # a mutation; explicit recalculation moved to POST below.
    refresh: bool = False,
    db: Session = Depends(get_db),
):
    try:
        return get_materials_snapshot(db, int(product_id))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/orders/{product_id}/materials/refresh", response_model=dict)
def post_order_line_materials_refresh(
    product_id: int,
    db: Session = Depends(get_db),
):
    try:
        return refresh_materials_snapshot(db, int(product_id))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


def _export_failure_detail(
    export: dict,
    entry: dict,
    default: str,
) -> str:
    """
    Human-readable reason a 1C export step failed.

    A row that never became an export entry (missing item_ref1c, missing parent
    order, cancelled …) leaves `entries` empty and carries its real diagnosis in
    `skipped_rows` — without this those reasons were lost behind the generic
    "1С не создала и не провела ..." message.
    """
    detail = str(entry.get("error") or entry.get("reason") or "").strip()
    if detail:
        return detail
    reasons = [
        str(row.get("reason") or "").strip()
        for row in (export.get("skipped_rows") or [])
        if str(row.get("reason") or "").strip()
    ]
    if reasons:
        return f"{default}: " + "; ".join(reasons)
    return default


@router.post("/orders/{product_id}/produce", response_model=dict)
def post_produce_line(
    product_id: int,
    payload: ProduceLinePayload,
    db: Session = Depends(get_db),
):
    """
    One operator action: create the durable command, export and post
    СборкаЗапасов, then export and post СдельныйНаряд. The assembly exporter
    enqueues the posted recorder for immediate Item Ledger read-back. None of
    these document writes is itself a production fact.
    """
    try:
        command = produce_line(
            db,
            int(product_id),
            qty=float(payload.qty),
            executor=payload.executor,
            operation_executors=payload.operation_executors,
            comment=payload.comment,
        )
        manufacture_id = int(command["manufacture_id"])
        resumed = bool(command.get("resumed"))
        manufacture_export = export_manufactures_to_1c(
            db,
            [manufacture_id],
            dry_run=False,
            allow_production=True,
        )
        manufacture_entry = (manufacture_export.get("entries") or [{}])[0]
        manufacture_ref = str(manufacture_entry.get("target_ref_key") or "")
        if (
            int(manufacture_export.get("manufactures_error") or 0) > 0
            or not manufacture_ref
        ):
            if not manufacture_ref and not resumed:
                rollback_local_manufacture(db, manufacture_id)
            raise ValueError(
                _export_failure_detail(
                    manufacture_export,
                    manufacture_entry,
                    "1С не создала и не провела СборкаЗапасов",
                )
            )
        piecework_export = export_piecework_to_1c(
            db,
            [manufacture_id],
            dry_run=False,
            allow_production=True,
        )
        piecework_entry = (piecework_export.get("entries") or [{}])[0]
        if (
            int(piecework_export.get("manufactures_error") or 0) > 0
            or not str(piecework_entry.get("target_ref_key") or "")
        ):
            raise ValueError(
                _export_failure_detail(
                    piecework_export,
                    piecework_entry,
                    "1С не создала и не провела СдельныйНаряд",
                )
            )
        return {
            **command,
            "manufacture_export": manufacture_export,
            "piecework_export": piecework_export,
            "ledger_readback": "queued",
        }
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
    Create inbound transfer for leftover components from partial production.
    Export remains in /material-issues/export-to-1c.
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
    Bulk-экспорт выпусков в 1С как Document_СборкаЗапасов.
    """
    if not payload.manufacture_ids:
        raise HTTPException(status_code=400, detail="Не выбраны выпуски")
    try:
        return export_manufactures_to_1c(
            db,
            [int(x) for x in payload.manufacture_ids],
            dry_run=bool(payload.dry_run),
            allow_production=bool(payload.allow_production) or not bool(payload.dry_run),
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
            allow_production=bool(payload.allow_production) or not bool(payload.dry_run),
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/orders/from-work-items", response_model=dict)
def post_orders_from_work_items(
    payload: OrdersFromWorkItemsPayload,
    db: Session = Depends(get_db),
):
    """
    Materialize selected current-generation make work items into orders.

    Frozen requirement, reservation and work-item quantities are not changed.
    """
    if not payload.work_item_ids:
        raise HTTPException(status_code=400, detail="Не выбраны рабочие строки")
    try:
        return materialize_make_work_items(
            db,
            [int(x) for x in payload.work_item_ids],
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
            allow_production=bool(payload.allow_production) or not bool(payload.dry_run),
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


@router.delete("/material-issues/{issue_id}", response_model=dict)
def delete_material_issue(issue_id: int, db: Session = Depends(get_db)):
    try:
        return delete_local_material_issue(db, int(issue_id))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
            allow_production=bool(payload.allow_production) or not bool(payload.dry_run),
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
            allow_production=True,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    auto_print: bool = False,
    db: Session = Depends(get_db),
):
    try:
        ids = [int(x) for x in product_ids.split(",") if x.strip()]
        if not ids:
            raise ValueError("Не выбраны строки заказа")
        html = render_route_sheets_html(db, ids, auto_print=auto_print)
        if mark_printed:
            mark_route_sheets_printed(db, ids)
        return HTMLResponse(content=html)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/route-sheets/print", response_class=HTMLResponse)
def post_print_route_sheets(
    payload: PrintRouteSheetsPayload,
    db: Session = Depends(get_db),
):
    try:
        ids = [int(x) for x in payload.product_ids if x is not None]
        if not ids:
            raise ValueError("Не выбраны строки заказа")
        html = render_route_sheets_html(db, ids, auto_print=bool(payload.auto_print))
        if payload.mark_printed:
            mark_route_sheets_printed(db, ids)
        return HTMLResponse(content=html)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


router.include_router(settings_router)

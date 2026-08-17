from __future__ import annotations

from typing import Annotated, List, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .. import models
from ..services import planning_truth
from ..routers.truth_meta import TruthMeta, build_truth_meta
from ..database import get_db
from ..models import Employee, Operation, ProductionProduct, ProductionStage, Specification, SpecOperation
from ..services.bom_specification_resolver import BomSpecificationResolver
from ..services.one_c_manufacture_export import export_manufactures_to_1c
from ..services.one_c_posted_transfer_sync import sync_posted_transfers
from ..services.one_c_piecework_export import export_piecework_to_1c
from ..services.one_c_production_order_export import (
    close_production_orders_to_1c,
    export_production_orders_to_1c,
)
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
    update_line_state,
)
from ..services.production_control_journal_snapshot import (
    RouteSheetSnapshotUnavailable,
    ProductionControlJournalSnapshotUnavailable,
    list_root_product_options,
    read_snapshot as read_production_control_journal_snapshot,
    read_route_sheet_snapshot_rows,
)
from ..services.production_control_material_availability import (
    MaterialCoverageSnapshotUnavailable,
    get_materials_snapshot,
    preview_make_work_item_materials,
)
from ..services.paint_weld_chain import open_paint_chains_for_products
from ..services.production_control_printing import (
    mark_route_sheets_printed_by_snapshot_members,
    render_route_sheets_from_snapshots,
)
from ..services.production_control_production_flow import (
    produce_line,
    return_leftover_components,
    rollback_local_manufacture,
)
from .production_control_settings import router as settings_router


router = APIRouter(prefix="/v1/production-control", tags=["production-control"])


def _route_sheet_snapshot_error(exc: RouteSheetSnapshotUnavailable) -> dict[str, object]:
    detail = exc.as_dict()
    detail.setdefault("code", "route_sheet_snapshot_unavailable")
    return detail


def _route_sheet_member_ids(payloads: List[dict]) -> List[int]:
    members: set[int] = set()
    for payload in payloads:
        sheet = payload.get("sheet") if isinstance(payload, dict) else None
        if not isinstance(sheet, dict):
            continue
        members.add(int(sheet["product_id"]))
        chain = sheet.get("chain")
        if isinstance(chain, dict) and chain.get("weld_product_id") is not None:
            members.add(int(chain["weld_product_id"]))
    return sorted(members)


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
    # Frozen ordering of the snapshot row: the queue is published with both, and
    # the endpoint never saw them only because the queue had been empty since
    # the live-plan scope was lost.
    sort_key: str
    eligible_from: Optional[str] = None


class AssemblyQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[AssemblyQueueRow]
    total_rows: int
    total_queue_qty: float
    limit: int
    offset: int
    truth_meta: TruthMeta


class ProductionMaterialsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ledger_generation_id: int
    truth_status: str
    cutoff: str
    product_id: Optional[int] = None
    work_item_id: Optional[int] = None
    order_number: str
    item_name: str
    item_article: str
    qty: float
    spec_id: int | None
    components: list[dict]
    coverage: str
    coverage_status: str
    coverage_label: str
    coverage_basis: Literal["direct_bom", "welded_bom"] = "direct_bom"
    coverage_basis_item_id: Optional[int] = None
    coverage_basis_item_name: Optional[str] = None
    coverage_basis_item_article: Optional[str] = None


class DrumSlotRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    plan_line_id: int
    item_id: int
    # Additive: the drum board used to render bare item ids. Nullable because a
    # slot is keyed by the persisted generation, not by the live item table.
    item_code: str | None = None
    item_name: str | None = None
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
    total_slots: int
    total_gaps: int
    limit: int
    offset: int
    truth_meta: TruthMeta


class ShelfProjectionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: int
    item_id: int
    item_code: str | None
    item_name: str | None
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
    demand_manifest: list[dict]


class ShelfProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[ShelfProjectionRow]
    total_rows: int
    limit: int
    offset: int
    truth_meta: TruthMeta


# Read pages default wide enough that today's whole generation still arrives in
# one call, but never unbounded: one accepted generation can hold tens of
# thousands of slots.
DBR_PAGE_DEFAULT = 1000
DBR_PAGE_MAX = 10000


def _items_by_id(db: Session, item_ids: set[int]) -> dict[int, models.Item]:
    """Label lookup for one already-paged read; never widens the page itself."""
    if not item_ids:
        return {}
    return {
        int(item.item_id): item
        for item in db.query(models.Item)
        .filter(models.Item.item_id.in_(sorted(item_ids)))
        .all()
    }


@router.get("/assembly-queue", response_model=AssemblyQueueResponse)
def get_assembly_queue(
    limit: Annotated[int, Query(ge=1, le=DBR_PAGE_MAX)] = DBR_PAGE_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
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
                # Readiness first: its own ``reason`` is None whenever the truth
                # pointer is healthy, so unpacking it last used to erase the only
                # sentence that explains what is actually missing.
                **readiness.as_dict(),
                "code": "assembly_queue_unavailable",
                "reason": "assembly queue snapshot is missing for accepted generation",
            },
        )
    payload = dict(snapshot.payload or {})
    all_rows = list(payload.get("rows") or [])
    # total_rows / total_queue_qty stay whole-queue totals: the page is a window
    # into the snapshot, not a different queue.
    response = {
        "rows": all_rows[offset : offset + limit],
        "total_rows": int(payload.get("total_rows") or 0),
        "total_queue_qty": float(payload.get("total_queue_qty") or 0),
        "limit": limit,
        "offset": offset,
        "truth_meta": build_truth_meta(planning_truth.get_truth_state(db)),
    }
    return AssemblyQueueResponse.model_validate(response)


@router.get("/drum", response_model=DrumScheduleResponse)
def get_drum_schedule(
    limit: Annotated[int, Query(ge=1, le=DBR_PAGE_MAX)] = DBR_PAGE_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
    db: Session = Depends(get_db),
) -> DrumScheduleResponse:
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
                # Same ordering rule as the assembly queue above: the explicit
                # reason must survive the readiness projection.
                **truth.as_dict(),
                "code": "drum_schedule_unavailable",
                "reason": "drum schedule is missing for accepted generation",
            },
        )
    slot_query = db.query(models.DrumSlot).filter(
        models.DrumSlot.drum_schedule_id == schedule.id
    )
    gap_query = db.query(models.DrumCapacityGap).filter(
        models.DrumCapacityGap.drum_schedule_id == schedule.id
    )
    total_slots = int(slot_query.count() or 0)
    total_gaps = int(gap_query.count() or 0)
    # limit/offset window both collections independently; the schedule totals
    # below always describe the whole persisted schedule.
    slots = (
        slot_query.order_by(
            models.DrumSlot.slot_date,
            models.DrumSlot.resource_id,
            models.DrumSlot.slot_ordinal,
            models.DrumSlot.id,
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    gaps = (
        gap_query.order_by(
            models.DrumCapacityGap.gap_date,
            models.DrumCapacityGap.resource_id,
            models.DrumCapacityGap.id,
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    # Item labels for this page only, in one query — the drum board needs a name
    # next to every slot, not the raw item id.
    slot_items = _items_by_id(db, {int(row.item_id) for row in slots})
    return DrumScheduleResponse.model_validate(
        {
            "schedule_from": schedule.schedule_from.isoformat(),
            "schedule_to": schedule.schedule_to.isoformat(),
            "slots": [
                {
                    "plan_id": row.plan_id,
                    "plan_line_id": row.plan_line_id,
                    "item_id": row.item_id,
                    "item_code": (
                        slot_items[int(row.item_id)].item_code
                        if int(row.item_id) in slot_items
                        else None
                    ),
                    "item_name": (
                        slot_items[int(row.item_id)].item_name
                        if int(row.item_id) in slot_items
                        else None
                    ),
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
            "total_slots": total_slots,
            "total_gaps": total_gaps,
            "limit": limit,
            "offset": offset,
            "truth_meta": build_truth_meta(truth),
        }
    )


@router.get("/shelves", response_model=ShelfProjectionResponse)
def get_shelf_projections(
    limit: Annotated[int, Query(ge=1, le=DBR_PAGE_MAX)] = DBR_PAGE_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
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
    query = db.query(models.ShelfProjection).filter(
        models.ShelfProjection.ledger_generation_id == truth.generation_id
    )
    total_rows = int(query.count() or 0)
    rows = (
        query.order_by(
            models.ShelfProjection.latest_start_date.asc().nullslast(),
            models.ShelfProjection.item_id,
            models.ShelfProjection.id,
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = _items_by_id(db, {int(row.item_id) for row in rows})
    payload = [
        {
            "policy_id": row.shelf_policy_id,
            "item_id": row.item_id,
            "item_code": (
                items[int(row.item_id)].item_code
                if int(row.item_id) in items
                else None
            ),
            "item_name": (
                items[int(row.item_id)].item_name
                if int(row.item_id) in items
                else None
            ),
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
            # Which drum slots this shelf line defends — the master needs it to
            # see what stops when the pull is late.
            "demand_manifest": list(row.demand_manifest or []),
        }
        for row in rows
    ]
    return ShelfProjectionResponse.model_validate(
        {
            "rows": payload,
            "total_rows": total_rows,
            "limit": limit,
            "offset": offset,
            "truth_meta": build_truth_meta(truth),
        }
    )


class ProductionEmployeeOptionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: int
    employee_ref1c: str
    employee_type: Literal["employee", "brigade"]
    employee_code: str | None = None
    employee_name: str


class ProductionEmployeeListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[ProductionEmployeeOptionResponse]
    total: int


@router.get("/employees", response_model=ProductionEmployeeListResponse)
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
                "employee_type": row.employee_type,
                "employee_code": row.employee_code,
                "employee_name": row.employee_name,
            }
            for row in rows
        ],
        "total": len(rows),
    }


class ProductionOperationOptionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: int
    spec_id: int
    spec_ref1c: str | None = None
    spec_operation_id: int
    operation_id: int
    operation_ref1c: str
    operation_name: str | None = None
    stage_id: int | None = None
    stage_name: str | None = None
    time_norm: float


class ProductionOperationsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[ProductionOperationOptionResponse]
    total: int


@router.get(
    "/orders/{product_id}/operations",
    response_model=ProductionOperationsResponse,
)
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
        spec_id = BomSpecificationResolver(db).default_spec_id(int(product.item_id))
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


class MakeWorkItemLaunchPayload(BaseModel):
    work_item_id: int
    launch_qty: float = Field(gt=0)
    expected_materialized_qty: float = Field(default=0, ge=0)


class OrdersFromWorkItemsPayload(BaseModel):
    work_item_ids: List[int] = Field(default_factory=list)
    work_items: List[MakeWorkItemLaunchPayload] = Field(default_factory=list)
    initiated_by: Optional[str] = None


class OpenPaintWeldChainsPayload(BaseModel):
    product_ids: List[int]
    initiated_by: Optional[str] = None


class ExportProductionOrdersPayload(BaseModel):
    order_ids: List[int]
    dry_run: bool = True
    # DEPRECATED: демо-гард записи удалён после go-live. Поле принимается и
    # игнорируется, чтобы существующие клиенты не получали 422.
    allow_production: bool = False


class ExportMaterialIssuesPayload(BaseModel):
    issue_ids: List[int]
    dry_run: bool = True
    # DEPRECATED, см. ExportProductionOrdersPayload: принимается, не влияет.
    allow_production: bool = False


class ProduceLinePayload(BaseModel):
    # The executable quantity is server-owned.  An explicit value remains
    # accepted for non-UI integrations but is bounded again by the service.
    qty: Optional[float] = None
    executor: Optional[str] = None
    operation_executors: Optional[List[dict]] = None
    comment: Optional[str] = None


class CloseProductionOrderPayload(BaseModel):
    dry_run: bool = True


class ExportManufacturesPayload(BaseModel):
    manufacture_ids: List[int]
    dry_run: bool = True
    # DEPRECATED, см. ExportProductionOrdersPayload: принимается, не влияет.
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
    # DEPRECATED, см. ExportProductionOrdersPayload: принимается, не влияет.
    allow_production: bool = False


class AssembleMaterialIssuePayload(BaseModel):
    # DEPRECATED, см. ExportProductionOrdersPayload: принимается, не влияет.
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
    counterpart_order_number: Optional[str] = None
    counterpart_order_prodplan_number: Optional[str] = None
    counterpart_item_name: Optional[str] = None
    counterpart_item_article: Optional[str] = None
    counterpart_item_code: Optional[str] = None
    counterpart_quantity: Optional[float] = None
    counterpart_remaining_qty: Optional[float] = None
    counterpart_unit: Optional[str] = None
    counterpart_workshop_name: Optional[str] = None


class PaintWeldPairResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair_id: int
    role: Literal["painted", "welded"]
    counterpart_item_id: int
    counterpart_item_code: str
    counterpart_item_name: str
    counterpart_item_article: str
    selection_disabled_reason: Optional[str] = None


class ProductionOrderJournalRowResponse(BaseModel):
    """One executor order or saved MRP proposal in the unified journal."""

    model_config = ConfigDict(extra="forbid")

    journal_row_key: Optional[str] = None
    work_item_id: Optional[int] = None
    product_id: Optional[int] = None
    order_id: Optional[int] = None
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
    forecast_status: Optional[Literal["early", "on_time", "delayed", "critical", "unavailable"]] = None
    opened_at: Optional[str] = None
    workshop_id: Optional[int] = None
    workshop_name: Optional[str] = None
    stage_id: Optional[int] = None
    stage_name: Optional[str] = None
    spec_id: Optional[int] = None
    spec_revision_hash: Optional[str] = None
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
    available_actions: list[str] = []
    selection_disabled_reason: Optional[str] = None
    # DBR shelf pull: what drives this launch, how much and onto which shelf.
    launch_source: str = "mrp_remaining"
    shelf_warehouse_ref1c: Optional[str] = None
    shelf_pull_qty: Optional[float] = None
    shelf_materialized_qty: Optional[float] = None
    shelf_latest_start_date: Optional[str] = None
    materialized_order_qty: Optional[float] = None
    launchable_qty: Optional[float] = None
    paint_weld_chain: Optional[PaintWeldChainResponse] = None
    paint_weld_pair: Optional[PaintWeldPairResponse] = None


class ProductionOrderJournalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: List[ProductionOrderJournalRowResponse]
    total: int
    limit: int
    offset: int
    latest_run_id: Optional[int] = None
    latest_source_plan_id: Optional[int] = None
    truth_meta: TruthMeta


class ProductionControlRootProductOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int
    item_name: str
    item_article: Optional[str] = None
    item_code: Optional[str] = None


class ProductionControlRootProductOptionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: List[ProductionControlRootProductOption]
    total: int


@router.get("/orders/root-products", response_model=ProductionControlRootProductOptionsResponse)
def list_root_products(
    db: Session = Depends(get_db),
):
    try:
        options = list_root_product_options(db)
        return {"rows": options, "total": len(options)}
    except ProductionControlJournalSnapshotUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.as_dict()) from exc
    except planning_truth.PlanningTruthUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.as_dict()) from exc
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


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
        journal = read_production_control_journal_snapshot(
            db,
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
    except ProductionControlJournalSnapshotUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.as_dict()) from exc
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


@router.delete("/orders/{product_id}", response_model=dict)
def delete_local_order(product_id: int, db: Session = Depends(get_db)):
    try:
        return cancel_local_order(db, int(product_id))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/orders/{product_id}/materials", response_model=ProductionMaterialsResponse)
def get_order_line_materials(
    product_id: int,
    db: Session = Depends(get_db),
):
    try:
        return get_materials_snapshot(db, int(product_id))
    except planning_truth.PlanningTruthUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.as_dict()) from exc
    except MaterialCoverageSnapshotUnavailable as e:
        raise HTTPException(status_code=503, detail=e.detail) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/work-items/{work_item_id}/materials", response_model=ProductionMaterialsResponse)
def get_work_item_materials(
    work_item_id: int,
    qty: Optional[float] = None,
    db: Session = Depends(get_db),
):
    """Preview BOM coverage for a saved MRP row without creating an order."""
    try:
        truth = planning_truth.require_accepted_truth(
            db, "production_control.work_item_materials"
        )
        work = db.get(models.ReplenishmentWorkItem, int(work_item_id))
        if work is None or int(work.ledger_generation_id) != int(truth.generation_id):
            raise HTTPException(status_code=404, detail="Актуальная расчётная строка не найдена")
        launch_qty = float(qty if qty is not None else work.replenishment_remaining_qty)
        if launch_qty <= 0 or launch_qty > float(work.replenishment_remaining_qty) + 1e-6:
            raise HTTPException(status_code=400, detail="Количество запуска вне доступного остатка")
        payload = preview_make_work_item_materials(
            db,
            work_item_id=int(work.id),
            item_id=int(work.item_id),
            quantity=launch_qty,
            spec_id=BomSpecificationResolver(db).default_spec_id(int(work.item_id)),
            ledger_generation_id=int(truth.generation_id),
            order_number=f"MRP-R-{int(work.requirement_id)}",
            run_id=int(work.run_id),
        )
        payload["truth_status"] = "accepted"
        payload["cutoff"] = truth.cutoff.isoformat()
        return payload
    except HTTPException:
        raise
    except planning_truth.PlanningTruthUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.as_dict()) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
            qty=payload.qty,
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


@router.post("/orders/{product_id}/close", response_model=dict)
def post_close_production_order(
    product_id: int,
    payload: CloseProductionOrderPayload,
    db: Session = Depends(get_db),
):
    try:
        product = (
            db.query(ProductionProduct)
            .filter(ProductionProduct.product_id == int(product_id))
            .first()
        )
        if product is None or product.order_id is None:
            raise ValueError("ProductionProduct для close не найден")
        return close_production_orders_to_1c(
            db,
            [int(product.order_id)],
            dry_run=bool(payload.dry_run),
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
        )
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
        )
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
    selected_ids = [int(x) for x in payload.work_item_ids]
    launch_requests = {}
    for row in payload.work_items:
        work_id = int(row.work_item_id)
        selected_ids.append(work_id)
        launch_requests[work_id] = {
            "launch_qty": float(row.launch_qty),
            "expected_materialized_qty": float(row.expected_materialized_qty),
        }
    selected_ids = list(dict.fromkeys(selected_ids))
    if not selected_ids:
        raise HTTPException(status_code=400, detail="Не выбраны рабочие строки")
    try:
        return materialize_make_work_items(
            db,
            selected_ids,
            initiated_by=payload.initiated_by,
            launch_requests=launch_requests or None,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/orders/open-paint-weld-chains", response_model=dict)
def post_open_paint_weld_chains(
    payload: OpenPaintWeldChainsPayload,
    db: Session = Depends(get_db),
):
    """Открыть сварочную сторону для выбранных окрасочных строк и вернуть
    полный набор product_id, который должен пройти выдачу материалов и печать.
    """
    if not payload.product_ids:
        raise HTTPException(status_code=400, detail="Не выбраны строки заказов")
    result = open_paint_chains_for_products(
        db,
        product_ids=payload.product_ids,
        initiated_by=payload.initiated_by,
    )
    if result.get("status") == "partial_error":
        errors = result.get("errors") or []
        detail = "; ".join(str(row.get("error") or "ошибка цепочки") for row in errors)
        raise HTTPException(status_code=400, detail=detail or "Не удалось открыть цепочку окраска-сварка")
    return result


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
    - To actually write, pass `dry_run=false`; запись идёт в базу 1С из
      настроек подключения.
    """
    if not payload.order_ids:
        raise HTTPException(status_code=400, detail="Не выбраны заказы для экспорта")
    try:
        return export_production_orders_to_1c(
            db,
            [int(x) for x in payload.order_ids],
            dry_run=bool(payload.dry_run),
        )
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
    (Posted=false). Идемпотентно через sync_link.

    - `dry_run=true` (default) вЂ” возвращает payload, не пишет в 1С.
    - `dry_run=false` вЂ” реально пишет в базу 1С из настроек подключения.
    """
    if not payload.issue_ids:
        raise HTTPException(status_code=400, detail="Не выбраны документы выдачи")
    try:
        return export_material_issues_to_1c(
            db,
            [int(x) for x in payload.issue_ids],
            dry_run=bool(payload.dry_run),
        )
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
        return assemble_material_issue(db, int(issue_id))
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
    mark_printed: bool = False,
    auto_print: bool = False,
    db: Session = Depends(get_db),
):
    try:
        ids = [int(x) for x in product_ids.split(",") if x.strip()]
        if not ids:
            raise ValueError("Не выбраны строки заказа")
        route_payloads = read_route_sheet_snapshot_rows(db, ids)
        html = render_route_sheets_from_snapshots(route_payloads, auto_print=auto_print)
        # Compatibility-only query parameter.  GET is strictly read-only even
        # when an old bookmark sends mark_printed=true; persistence belongs to
        # the explicit POST endpoint below.
        _ = mark_printed
        return HTMLResponse(content=html)
    except RouteSheetSnapshotUnavailable as exc:
        raise HTTPException(status_code=503, detail=_route_sheet_snapshot_error(exc)) from exc
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
        route_payloads = read_route_sheet_snapshot_rows(db, ids)
        html = render_route_sheets_from_snapshots(route_payloads, auto_print=bool(payload.auto_print))
        if payload.mark_printed:
            mark_route_sheets_printed_by_snapshot_members(db, _route_sheet_member_ids(route_payloads))
        return HTMLResponse(content=html)
    except RouteSheetSnapshotUnavailable as exc:
        raise HTTPException(status_code=503, detail=_route_sheet_snapshot_error(exc)) from exc
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


router.include_router(settings_router)

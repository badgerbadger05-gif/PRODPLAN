from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, List, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
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
    STATUS_FILTER_GROUPS,
    materialize_make_work_items,
    cancel_local_order,
    update_line_state,
    update_local_order_quantity,
)
from ..services.production_control_journal_snapshot import (
    CONSUMER as PRODUCTION_JOURNAL_CONSUMER,
    PROPOSAL_ROW_KIND as PRODUCTION_JOURNAL_PROPOSAL_ROW_KIND,
    SNAPSHOT_KEY as PRODUCTION_JOURNAL_SNAPSHOT_KEY,
    RouteSheetSnapshotUnavailable,
    ProductionControlJournalSnapshotUnavailable,
    list_root_product_options,
    read_snapshot as read_production_control_journal_snapshot,
    _public_journal_row,
    read_route_sheet_snapshot_rows,
)
from ..services.production_control_material_availability import (
    MaterialCoverageSnapshotUnavailable,
    get_materials_snapshot,
    preview_make_work_item_materials,
)
from ..services.production_control_live_launch import overlay_execution_state, overlay_launch_facts
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
from ..services.production_order_sync import (
    configured_production_order_sync_request,
    sync_production_orders_from_odata,
)
from ..services.item_ledger.drum_manual_move import move_drum_slot
from ..services.item_ledger.drum_saved_calendar import (
    DrumSavedCalendarError,
    saved_working_days,
)
from ..services.item_ledger.drum_schedule_persistence import (
    ALGORITHM_VERSION as DRUM_SCHEDULE_ALGORITHM_VERSION,
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


class ReadinessActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_kind: str
    item_id: int
    item_code: str
    item_article: str
    item_name: str
    qty: str
    available_date: str | None = None
    confidence: str
    source_key: str
    source_warehouse_ref1c: str
    source_warehouse_name: str = ""
    destination_warehouse_ref1c: str
    destination_warehouse_name: str = ""
    resource_id: int | None = None
    resource_name: str = ""
    path: list[int]


class ReadinessCoverageSourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage_kind: Literal[
        "point_of_use",
        "custody",
        "transit",
        "wip_order",
        "supplier_order",
        "other_stock",
    ]
    qty: str
    source_key: str
    warehouse_ref1c: str = ""
    warehouse_name: str = ""
    destination_warehouse_ref1c: str = ""
    destination_warehouse_name: str = ""
    available_date: str | None = None
    confidence: str = "physical"
    source_kind: str = ""
    source_ref: str = ""


class ReadinessBlockerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int | None = None
    item_code: str = ""
    item_article: str = ""
    item_name: str = ""
    required_qty: str | None = None
    available_qty: str | None = None
    shortage_qty: str | None = None
    reason: str = "SHORTAGE"
    destination_warehouse_ref1c: str = ""
    destination_warehouse_name: str = ""
    path: list[int] = Field(default_factory=list)
    point_of_use_qty: str = "0"
    custody_qty: str = "0"
    transit_qty: str = "0"
    wip_qty: str = "0"
    supplier_qty: str = "0"
    other_stock_qty: str = "0"
    coverage_sources: list[ReadinessCoverageSourceResponse] = Field(
        default_factory=list
    )


class ReadinessCurvePointResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    horizon: Literal["now", "transfer", "kitting", "committed", "launch"]
    cumulative_qty: str
    available_date: str | None = None
    actions: list[ReadinessActionResponse] = Field(default_factory=list)
    required_actions: list[ReadinessActionResponse] = Field(default_factory=list)
    blockers: list[ReadinessBlockerResponse] = Field(default_factory=list)


class AssemblyReadinessRowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queue_line_id: int
    plan_id: int
    plan_line_id: int
    run_id: int
    item_id: int
    item_code: str
    item_name: str
    resource_id: int | None
    status: Literal["ready", "recoverable", "partial", "blocked", "unavailable"]
    open_qty: float
    ready_qty: float
    transferable_qty: float
    kitting_qty: float
    committed_qty: float
    launchable_qty: float
    readiness_date: str | None = None
    readiness_curve: list[ReadinessCurvePointResponse]
    action_manifest: list[ReadinessActionResponse]
    unavailable_reasons: list[str]
    blocker_count: int
    blockers: list[ReadinessBlockerResponse]
    original_priority: list[Union[str, int]]


class AssemblyReadinessListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[AssemblyReadinessRowResponse]
    total: int
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

    slot_id: int
    queue_line_id: int
    run_id: int | None = None
    plan_id: int
    plan_line_id: int
    period_from: str | None = None
    period_to: str | None = None
    item_id: int
    # Additive: the drum board used to render bare item ids. Nullable because a
    # slot is keyed by the persisted generation, not by the live item table.
    item_code: str | None = None
    item_name: str | None = None
    resource_id: int
    slot_date: str
    auto_slot_date: str | None = None
    slot_qty: float
    planned_output_qty: float | None = None
    accepted_plan_output_qty: float | None = None
    assembly_remaining_qty: float | None = None
    slot_ordinal: int
    readiness_phase: Literal["now", "transfer", "kitting", "committed", "launch", "blocked", "unavailable"]
    readiness_date: str | None = None
    readiness_curve: list[ReadinessCurvePointResponse]
    action_manifest: list[ReadinessActionResponse]
    unavailable_reasons: list[str]
    blocking_manifest: list[ReadinessBlockerResponse]
    manual_override: bool = False
    manual_moved_at: str | None = None
    manual_moved_by: str | None = None
    original_priority: list[Union[str, int]]
    current_identity: str | None = None
    source_revision: str | None = None


class DrumSlotMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_date: str
    new_resource_id: int | None = None
    moved_by: str | None = None
    expected_source_revision: str | None = None
    current_identity: str | None = None


class DrumSlotMoveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    moved: bool
    slot_id: int
    from_date: str
    to_date: str
    resource_id: int
    manual_moved_at: str | None = None
    manual_moved_by: str | None = None


class DrumGapRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gap_id: int
    queue_line_id: int
    run_id: int | None = None
    plan_id: int
    plan_line_id: int
    period_from: str | None = None
    period_to: str | None = None
    item_id: int
    item_code: str | None = None
    item_name: str | None = None
    resource_id: int
    gap_date: str
    required_qty: float
    available_capacity: float
    gap_qty: float
    planned_output_qty: float | None = None
    accepted_plan_output_qty: float | None = None
    assembly_remaining_qty: float | None = None
    readiness_phase: Literal["now", "transfer", "kitting", "committed", "launch", "blocked", "unavailable", "mixed"]
    readiness_date: str | None = None
    readiness_curve: list[ReadinessCurvePointResponse] = Field(default_factory=list)
    action_manifest: list[ReadinessActionResponse] = Field(default_factory=list)
    unavailable_reasons: list[str] = Field(default_factory=list)
    blocking_manifest: list[ReadinessBlockerResponse] = Field(default_factory=list)
    original_priority: list[Union[str, int]]
    current_identity: str | None = None
    source_revision: str | None = None


class DrumResourceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: int
    resource_name: str


class DrumExcludedRow(BaseModel):
    """Saved open queue row that cannot enter the calendar without one takt."""

    model_config = ConfigDict(extra="forbid")

    queue_line_id: int
    plan_id: int
    plan_line_id: int
    run_id: int
    item_id: int
    period_from: str
    period_to: str
    item_code: str | None = None
    item_name: str | None = None
    planned_output_qty: float
    accepted_plan_output_qty: float
    assembly_remaining_qty: float
    reason: Literal["ASSEMBLY_RATE_MISSING"] = "ASSEMBLY_RATE_MISSING"
    readiness_status: Literal["ready", "recoverable", "partial", "blocked", "unavailable"]
    readiness_date: str | None = None
    readiness_curve: list[ReadinessCurvePointResponse] = Field(default_factory=list)
    action_manifest: list[ReadinessActionResponse] = Field(default_factory=list)
    unavailable_reasons: list[str] = Field(default_factory=list)
    blocking_manifest: list[ReadinessBlockerResponse] = Field(default_factory=list)
    original_priority: list[Union[str, int]] = Field(default_factory=list)


class DrumScheduleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule_from: str
    schedule_to: str
    days: list[str]
    resources: list[DrumResourceRow]
    slots: list[DrumSlotRow]
    gaps: list[DrumGapRow]
    excluded: list[DrumExcludedRow]
    total_open_qty: float
    total_slot_qty: float
    total_gap_qty: float
    total_slots: int
    total_gaps: int
    total_excluded: int
    total_excluded_open_qty: float
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
    """Read the compact current queue; legacy snapshots serve only old data."""
    from ..services.item_ledger.current_execution import (
        CurrentExecutionUnavailable,
        get_current_execution_scope,
        load_current_execution_rows,
        require_current_execution_scope,
    )
    try:
        current_scope = require_current_execution_scope(
            db, entity_kind="assembly_queue", scope_key="assembly:all-live-plans"
        )
    except CurrentExecutionUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "assembly_queue_unavailable", "reason": str(exc)}) from exc
    current_rows = load_current_execution_rows(db, entity_kind="assembly_queue")
    if current_scope is not None:
        if not bool(current_scope.result_ready):
            raise HTTPException(status_code=503, detail={"code": "assembly_queue_unavailable", "reason": "current queue result is not ready"})
        try:
            truth = planning_truth.require_accepted_truth(
                db,
                consumer="assembly_queue",
                required_capabilities=(
                    planning_truth.CAPABILITY_PHYSICAL_LEDGER,
                    planning_truth.CAPABILITY_RESERVATION_REPLAY,
                    planning_truth.CAPABILITY_ASSEMBLY_QUEUE,
                ),
            )
        except planning_truth.PlanningTruthUnavailable as exc:
            raise HTTPException(status_code=503, detail=jsonable_encoder(exc.as_dict())) from exc
        item_ids = {int(row.payload.get("item_id")) for row in current_rows if row.payload.get("item_id") is not None}
        items = _items_by_id(db, item_ids)
        ordered = sorted(
            current_rows,
            key=lambda row: (
                str(row.payload.get("period_from") or ""),
                str(row.payload.get("period_to") or ""),
                int(row.payload.get("plan_id") or 0),
                int(row.payload.get("plan_line_id") or 0),
                str(row.business_identity),
            ),
        )
        payload_rows = []
        for row in ordered:
            payload = dict(row.payload or {})
            item = items.get(int(payload.get("item_id")))
            payload_rows.append({
                "plan_id": int(payload["plan_id"]),
                "plan_line_id": int(payload["plan_line_id"]),
                "run_id": int(payload.get("run_id") or 0),
                "item_id": int(payload["item_id"]),
                "item_code": str(item.item_code or "") if item is not None else "",
                "item_name": str(item.item_name or "") if item is not None else "",
                "bucket_date": payload.get("bucket_date"),
                "period_from": str(payload["period_from"]),
                "period_to": str(payload["period_to"]),
                "planned_output_qty": float(payload["planned_output_qty"]),
                "accepted_plan_output_qty": float(payload["accepted_plan_output_qty"]),
                "assembly_remaining_qty": float(payload["assembly_remaining_qty"]),
                "eligible_from": payload.get("eligible_from"),
                "priority_key": list(payload.get("original_priority") or []),
                "sort_key": str(payload.get("sort_key") or ""),
            })
        summary = dict(current_scope.summary or {})
        if "total_rows" not in summary or "total_queue_qty" not in summary:
            raise HTTPException(
                status_code=503,
                detail={"code": "assembly_queue_unavailable", "reason": "current queue summary is missing"},
            )
        return AssemblyQueueResponse.model_validate({
            "rows": payload_rows[offset:offset + limit],
            "total_rows": int(summary["total_rows"]),
            "total_queue_qty": float(summary["total_queue_qty"]),
            "limit": limit,
            "offset": offset,
            "truth_meta": build_truth_meta(truth),
        })
@router.get("/assembly-readiness", response_model=AssemblyReadinessListResponse)
def get_assembly_readiness(
    resource_id: Optional[int] = None,
    limit: Annotated[int, Query(ge=1, le=DBR_PAGE_MAX)] = DBR_PAGE_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
    db: Session = Depends(get_db),
) -> AssemblyReadinessListResponse:
    """Read the persisted release recommendation; never calculate readiness in GET."""
    from ..services.item_ledger.current_execution import (
        CurrentExecutionUnavailable,
        get_current_execution_scope,
        load_current_execution_rows,
        require_current_execution_scope,
    )
    try:
        current_scope = require_current_execution_scope(
            db, entity_kind="assembly_readiness", scope_key="assembly:all-live-plans"
        )
    except CurrentExecutionUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "assembly_readiness_unavailable", "reason": str(exc)}) from exc
    current_rows = load_current_execution_rows(db, entity_kind="assembly_readiness")
    if current_scope is not None:
        if not bool(current_scope.result_ready):
            raise HTTPException(status_code=503, detail={"code": "assembly_readiness_unavailable", "reason": "current readiness result is not ready"})
        try:
            truth = planning_truth.require_accepted_truth(
                db,
                "assembly_readiness",
                required_capabilities=(
                    planning_truth.CAPABILITY_PHYSICAL_LEDGER,
                    planning_truth.CAPABILITY_ASSEMBLY_QUEUE,
                    planning_truth.CAPABILITY_ASSEMBLY_READINESS,
                ),
            )
        except planning_truth.PlanningTruthUnavailable as exc:
            raise HTTPException(status_code=503, detail=jsonable_encoder(exc.as_dict())) from exc
        item_ids = {int(row.payload.get("item_id")) for row in current_rows if row.payload.get("item_id") is not None}
        items = _items_by_id(db, item_ids)
        rates = {
            int(row.item_id): int(row.resource_id)
            for row in db.query(models.AssemblyRate).filter(models.AssemblyRate.item_id.in_(item_ids or {0})).all()
        }
        ordered = sorted(
            current_rows,
            key=lambda row: (
                str(row.payload.get("original_priority") or ""),
                int(row.payload.get("plan_id") or 0),
                int(row.payload.get("plan_line_id") or 0),
            ),
        )
        if resource_id is not None:
            ordered = [row for row in ordered if rates.get(int(row.payload.get("item_id") or 0)) == int(resource_id)]
        result_rows = []
        for row in ordered[offset:offset + limit]:
            payload = dict(row.payload or {})
            item = items.get(int(payload.get("item_id")))
            result_rows.append({
                "queue_line_id": int(payload.get("queue_line_id") or 0),
                "plan_id": int(payload["plan_id"]),
                "plan_line_id": int(payload["plan_line_id"]),
                "run_id": int(payload.get("run_id") or 0),
                "item_id": int(payload["item_id"]),
                "item_code": str(item.item_code or "") if item is not None else "",
                "item_name": str(item.item_name or "") if item is not None else "",
                "resource_id": rates.get(int(payload["item_id"])),
                "status": str(payload["status"]),
                "open_qty": float(payload["open_qty"]),
                "ready_qty": float(payload["ready_qty"]),
                "transferable_qty": float(payload.get("transferable_qty") or 0),
                "kitting_qty": float(payload.get("kitting_qty") or 0),
                "committed_qty": float(payload.get("committed_qty") or 0),
                "launchable_qty": float(payload.get("launchable_qty") or 0),
                "readiness_date": payload.get("readiness_date"),
                "readiness_curve": list(payload.get("readiness_curve") or []),
                "action_manifest": list(payload.get("action_manifest") or []),
                "unavailable_reasons": list(payload.get("unavailable_reasons") or []),
                "blocker_count": int(payload.get("blocker_count") or 0),
                "blockers": list(payload.get("blocking_manifest") or []),
                "original_priority": list(payload.get("original_priority") or []),
            })
        return AssemblyReadinessListResponse.model_validate({
            "rows": result_rows,
            "total": len(ordered),
            "limit": limit,
            "offset": offset,
            "truth_meta": build_truth_meta(truth),
        })
@router.get("/drum", response_model=DrumScheduleResponse)
def get_drum_schedule(
    limit: Annotated[int, Query(ge=1, le=DBR_PAGE_MAX)] = DBR_PAGE_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
    db: Session = Depends(get_db),
) -> DrumScheduleResponse:
    """Read the persisted drum of the exact accepted generation."""
    from ..services.item_ledger.current_execution import (
        CurrentExecutionUnavailable,
        get_current_execution_scope,
        load_current_execution_rows,
        require_current_execution_scope,
    )
    try:
        current_scope = require_current_execution_scope(
            db, entity_kind="drum_schedule", scope_key="drum:all-live-plans"
        )
    except CurrentExecutionUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "drum_schedule_unavailable", "reason": str(exc)}) from exc
    current_schedule = load_current_execution_rows(db, entity_kind="drum_schedule", scope_key="drum:all-live-plans")
    if current_scope is not None:
        if not bool(current_scope.result_ready):
            raise HTTPException(status_code=503, detail={"code": "drum_schedule_unavailable", "reason": "current drum result is not ready"})
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
            raise HTTPException(status_code=503, detail=jsonable_encoder(exc.as_dict())) from exc
        schedule_payload = dict(current_schedule[0].payload or {}) if current_schedule else {
            "schedule_from": truth.cutoff.date().isoformat() if truth.cutoff else "",
            "schedule_to": truth.cutoff.date().isoformat() if truth.cutoff else "",
            "working_days": [],
            "metrics": {},
        }
        def _typed_priority(value: object) -> tuple[int, object]:
            if isinstance(value, bool):
                return (0, int(value))
            if isinstance(value, (int, float, Decimal)):
                return (0, Decimal(str(value)))
            return (1, str(value))

        def _priority_key(payload: dict) -> tuple[tuple[int, object], ...]:
            return tuple(_typed_priority(value) for value in (payload.get("original_priority") or []))

        slots = sorted(
            load_current_execution_rows(db, entity_kind="drum_slot", scope_key="drum:all-live-plans"),
            key=lambda row: (
                str((row.payload or {}).get("slot_date") or ""),
                int((row.payload or {}).get("resource_id") or 0),
                _priority_key(row.payload or {}),
                int((row.payload or {}).get("slot_ordinal") or 0),
                str(row.business_identity),
            ),
        )
        gaps = sorted(
            load_current_execution_rows(db, entity_kind="drum_gap", scope_key="drum:all-live-plans"),
            key=lambda row: (
                str((row.payload or {}).get("gap_date") or ""),
                int((row.payload or {}).get("resource_id") or 0),
                _priority_key(row.payload or {}),
                str(row.business_identity),
            ),
        )
        excluded_rows = sorted(
            load_current_execution_rows(db, entity_kind="drum_excluded", scope_key="drum:all-live-plans"),
            key=lambda row: (
                str((row.payload or {}).get("period_from") or ""),
                _priority_key(row.payload or {}),
                str(row.business_identity),
            ),
        )
        item_ids = {
            int(row.payload.get("item_id"))
            for row in (*slots, *gaps, *excluded_rows)
            if row.payload.get("item_id") is not None
        }
        items = _items_by_id(db, item_ids)
        resource_ids = {
            int(row.payload.get("resource_id"))
            for row in (*slots, *gaps)
            if row.payload.get("resource_id") is not None
        }
        resources = {
            int(row.resource_id): row
            for row in db.query(models.ProductionResource).filter(
                models.ProductionResource.resource_id.in_(resource_ids or {0})
            ).all()
        }
        slot_rows = []
        for row in slots[offset:offset + limit]:
            payload = dict(row.payload or {})
            item = items.get(int(payload.get("item_id")))
            slot_rows.append({
                "slot_id": int(row.id),
                "queue_line_id": int(payload.get("queue_line_id") or 0),
                "run_id": int(payload.get("run_id")) if payload.get("run_id") is not None else None,
                "period_from": payload.get("period_from"),
                "period_to": payload.get("period_to"),
                "plan_id": int(payload.get("plan_id") or 0),
                "plan_line_id": int(payload.get("plan_line_id") or 0),
                "item_id": int(payload.get("item_id") or 0),
                "item_code": item.item_code if item else None,
                "item_name": item.item_name if item else None,
                "resource_id": int(payload.get("resource_id") or 0),
                "slot_date": payload.get("slot_date"),
                "auto_slot_date": payload.get("auto_slot_date"),
                "slot_qty": float(payload.get("slot_qty") or 0),
                "planned_output_qty": float(payload.get("planned_output_qty")) if payload.get("planned_output_qty") is not None else None,
                "accepted_plan_output_qty": float(payload.get("accepted_plan_output_qty")) if payload.get("accepted_plan_output_qty") is not None else None,
                "assembly_remaining_qty": float(payload.get("assembly_remaining_qty")) if payload.get("assembly_remaining_qty") is not None else None,
                "current_identity": str(row.business_identity),
                "source_revision": str(row.source_revision),
                "slot_ordinal": int(payload.get("slot_ordinal") or 0),
                "readiness_phase": str(payload.get("readiness_phase") or "unavailable"),
                "readiness_date": payload.get("readiness_date"),
                "readiness_curve": list(payload.get("readiness_curve") or []),
                "action_manifest": list(payload.get("action_manifest") or []),
                "unavailable_reasons": list(payload.get("unavailable_reasons") or []),
                "blocking_manifest": list(payload.get("blocking_manifest") or []),
                "manual_override": bool(row.manual_input),
                "manual_moved_at": row.manual_input.get("moved_at") if row.manual_input else None,
                "manual_moved_by": row.manual_input.get("moved_by") if row.manual_input else None,
                "original_priority": list(payload.get("original_priority") or []),
            })
        gap_rows = []
        for row in gaps[offset:offset + limit]:
            payload = dict(row.payload or {})
            item = items.get(int(payload.get("item_id")))
            gap_rows.append({
                "gap_id": int(row.id),
                "queue_line_id": int(payload.get("queue_line_id") or 0),
                "run_id": int(payload.get("run_id")) if payload.get("run_id") is not None else None,
                "period_from": payload.get("period_from"),
                "period_to": payload.get("period_to"),
                "plan_id": int(payload.get("plan_id") or 0),
                "plan_line_id": int(payload.get("plan_line_id") or 0),
                "item_id": int(payload.get("item_id") or 0),
                "item_code": item.item_code if item else None,
                "item_name": item.item_name if item else None,
                "resource_id": int(payload.get("resource_id") or 0),
                "gap_date": payload.get("gap_date"),
                "required_qty": float(payload.get("required_qty") or 0),
                "available_capacity": float(payload.get("available_capacity") or 0),
                "gap_qty": float(payload.get("gap_qty") or 0),
                "planned_output_qty": float(payload.get("planned_output_qty")) if payload.get("planned_output_qty") is not None else None,
                "accepted_plan_output_qty": float(payload.get("accepted_plan_output_qty")) if payload.get("accepted_plan_output_qty") is not None else None,
                "assembly_remaining_qty": float(payload.get("assembly_remaining_qty")) if payload.get("assembly_remaining_qty") is not None else None,
                "current_identity": str(row.business_identity),
                "source_revision": str(row.source_revision),
                "readiness_phase": str(payload.get("readiness_phase") or "unavailable"),
                "readiness_date": payload.get("readiness_date"),
                "readiness_curve": list(payload.get("readiness_curve") or []),
                "action_manifest": list(payload.get("action_manifest") or []),
                "unavailable_reasons": list(payload.get("unavailable_reasons") or []),
                "blocking_manifest": list(payload.get("blocking_manifest") or []),
                "original_priority": list(payload.get("original_priority") or []),
            })
        excluded_response_rows = []
        for row in excluded_rows[offset:offset + limit]:
            payload = dict(row.payload or {})
            item = items.get(int(payload.get("item_id")))
            excluded_response_rows.append({
                "queue_line_id": int(payload.get("queue_line_id") or 0),
                "plan_id": int(payload.get("plan_id") or 0),
                "plan_line_id": int(payload.get("plan_line_id") or 0),
                "run_id": int(payload.get("run_id") or 0),
                "item_id": int(payload.get("item_id") or 0),
                "period_from": payload.get("period_from"),
                "period_to": payload.get("period_to"),
                "item_code": item.item_code if item else None,
                "item_name": item.item_name if item else None,
                "planned_output_qty": float(payload.get("planned_output_qty") or 0),
                "accepted_plan_output_qty": float(payload.get("accepted_plan_output_qty") or 0),
                "assembly_remaining_qty": float(payload.get("assembly_remaining_qty") or 0),
                "reason": str(payload.get("reason") or "ASSEMBLY_RATE_MISSING"),
                "readiness_status": str(payload.get("readiness_status") or "unavailable"),
                "readiness_date": payload.get("readiness_date"),
                "readiness_curve": list(payload.get("readiness_curve") or []),
                "action_manifest": list(payload.get("action_manifest") or []),
                "unavailable_reasons": list(payload.get("unavailable_reasons") or []),
                "blocking_manifest": list(payload.get("blocking_manifest") or []),
                "original_priority": list(payload.get("original_priority") or []),
            })
        return DrumScheduleResponse.model_validate({
            "schedule_from": schedule_payload.get("schedule_from"),
            "schedule_to": schedule_payload.get("schedule_to"),
            "days": list(schedule_payload.get("working_days") or []),
            "resources": [
                {"resource_id": int(resource_id), "resource_name": str(row.resource_name or f"Участок #{resource_id}")}
                for resource_id, row in sorted(resources.items())
            ],
            "slots": slot_rows,
            "gaps": gap_rows,
            "excluded": excluded_response_rows,
            "total_open_qty": float(schedule_payload.get("metrics", {}).get("total_open_qty") or 0),
            "total_slot_qty": float(schedule_payload.get("metrics", {}).get("total_slot_qty") or 0),
            "total_gap_qty": float(schedule_payload.get("metrics", {}).get("total_gap_qty") or 0),
            "total_slots": len(slots),
            "total_gaps": len(gaps),
            "total_excluded": int(schedule_payload.get("metrics", {}).get("excluded_lines") or len(excluded_rows)),
            "total_excluded_open_qty": float(schedule_payload.get("metrics", {}).get("excluded_open_qty") or 0),
            "limit": limit,
            "offset": offset,
            "truth_meta": build_truth_meta(truth),
        })
@router.post("/drum/slots/{slot_id}/move", response_model=DrumSlotMoveResponse)
def post_move_drum_slot(
    slot_id: int,
    payload: DrumSlotMoveRequest,
    db: Session = Depends(get_db),
) -> DrumSlotMoveResponse:
    try:
        target_date = date.fromisoformat(payload.new_date)
        if payload.current_identity:
            if not payload.expected_source_revision:
                raise ValueError("current drum move requires expected_source_revision")
            from ..services.item_ledger.drum_manual_move import move_current_drum_slot
            moved = move_current_drum_slot(
                db,
                business_identity=str(payload.current_identity),
                expected_source_revision=str(payload.expected_source_revision),
                new_date=target_date,
                new_resource_id=payload.new_resource_id,
                moved_by=payload.moved_by,
            )
            db.commit()
            return DrumSlotMoveResponse.model_validate({
                "ok": True,
                "moved": bool(moved.get("moved")),
                "slot_id": int(moved.get("slot_id") or slot_id),
                "from_date": str(moved.get("from_date") or moved.get("to_date") or target_date.isoformat()),
                "to_date": str(moved.get("to_date") or target_date.isoformat()),
                "resource_id": int(moved.get("resource_id") or payload.new_resource_id or 0),
                "manual_moved_at": None,
                "manual_moved_by": payload.moved_by,
            })
        result = move_drum_slot(
            db,
            int(slot_id),
            target_date,
            new_resource_id=payload.new_resource_id,
            moved_by=payload.moved_by,
        )
        return DrumSlotMoveResponse.model_validate(result)
    except planning_truth.PlanningTruthUnavailable as exc:
        raise HTTPException(status_code=503, detail=jsonable_encoder(exc.as_dict())) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/shelves", response_model=ShelfProjectionResponse)
def get_shelf_projections(
    limit: Annotated[int, Query(ge=1, le=DBR_PAGE_MAX)] = DBR_PAGE_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
    db: Session = Depends(get_db),
) -> ShelfProjectionResponse:
    """Read persisted shelf pull priorities of the accepted generation."""
    from ..services.item_ledger.current_execution import (
        CurrentExecutionUnavailable,
        get_current_execution_scope,
        load_current_execution_rows,
        require_current_execution_scope,
    )
    try:
        current_scope = require_current_execution_scope(
            db, entity_kind="shelf_projection", scope_key="shelf:all-live-mrps"
        )
    except CurrentExecutionUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "shelf_projection_unavailable", "reason": str(exc)}) from exc
    current_rows = load_current_execution_rows(db, entity_kind="shelf_projection", scope_key="shelf:all-live-mrps")
    if current_scope is not None:
        if not bool(current_scope.result_ready):
            raise HTTPException(status_code=503, detail={"code": "shelf_projection_unavailable", "reason": "current shelf result is not ready"})
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


class OrderLineQuantityPayload(BaseModel):
    quantity: float = Field(gt=0)
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
    partial: bool = False
    request_key: Optional[str] = None
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


class ProtectedDrumSlotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    drum_slot_id: int
    root_item_id: int
    slot_date: str
    slot_qty: str
    readiness_phase: Literal["now", "transfer", "kitting", "committed", "launch", "blocked", "unavailable"]


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
    current_identity: Optional[str] = None
    source_revision: Optional[str] = None
    explanations: list[str] = Field(default_factory=list)
    selection_disabled_reason: Optional[str] = None
    # DBR shelf pull: what drives this launch, how much and onto which shelf.
    launch_source: str = "mrp_remaining"
    shelf_warehouse_ref1c: Optional[str] = None
    shelf_pull_qty: Optional[float] = None
    shelf_materialized_qty: Optional[float] = None
    shelf_latest_start_date: Optional[str] = None
    readiness_required_qty: Optional[float] = None
    readiness_need_date: Optional[str] = None
    readiness_action_date: Optional[str] = None
    readiness_priority_key: Optional[str] = None
    protected_drum_slots: list[ProtectedDrumSlotResponse] = Field(default_factory=list)
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


def _canonical_journal_sort(
    rows: list[dict],
    *,
    field: Optional[str],
    descending: bool,
) -> None:
    """Sort journal rows with NULLS LAST and ascending business ties."""

    primary_field = str(field or "order_date").strip() or "order_date"

    def _tie_key(row: dict) -> tuple[str, int, str]:
        raw_line = row.get("line_number")
        try:
            line_number = int(raw_line) if raw_line is not None else 0
        except (TypeError, ValueError):
            line_number = 0
        return (
            str(row.get("order_number") or ""),
            line_number,
            str(raw_line or ""),
        )

    rows.sort(key=_tie_key)
    present = [row for row in rows if row.get(primary_field) not in (None, "")]
    missing = [row for row in rows if row.get(primary_field) in (None, "")]
    present.sort(
        key=lambda row: str(row.get(primary_field) or ""),
        reverse=descending,
    )
    rows[:] = present + missing


@router.get("/orders/root-products", response_model=ProductionControlRootProductOptionsResponse)
def list_root_products(
    db: Session = Depends(get_db),
):
    try:
        options = list_root_product_options(db)
        return {"rows": options, "total": len(options)}
    except ProductionControlJournalSnapshotUnavailable as exc:
        raise HTTPException(status_code=503, detail=jsonable_encoder(exc.as_dict())) from exc
    except planning_truth.PlanningTruthUnavailable as exc:
        raise HTTPException(status_code=503, detail=jsonable_encoder(exc.as_dict())) from exc
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
    launch_source: Optional[str] = Query(
        None,
        description="Источник запуска: drum_readiness, shelf_pull или mrp_remaining.",
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
        # R9 current owner.  Rows are served verbatim from the compact current
        # scope; no snapshot replay, generation guessing or client-side totals
        # are involved.
        from ..services.item_ledger.current_execution import (
            CurrentExecutionUnavailable,
            load_current_execution_rows,
            require_current_execution_scope,
        )
        current_manifest = require_current_execution_scope(
            db,
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
        )
        current_records = load_current_execution_rows(
            db,
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
        )
        rows = []
        for current_row in current_records:
            payload = dict(current_row.payload or {})
            payload["__r9_root_item_ids"] = list(payload.get("root_item_ids") or [])
            payload.pop("root_item_ids", None)
            payload["current_identity"] = str(current_row.business_identity)
            payload["source_revision"] = str(current_row.source_revision)
            rows.append(_public_journal_row(payload))
        source_generation = db.get(models.LedgerGeneration, int(current_manifest.source_generation_id or 0))
        if source_generation is None or source_generation.cutoff is None:
            raise CurrentExecutionUnavailable("production current source cutoff is missing")
        overlay_launch_facts(db, rows, cutoff=source_generation.cutoff)
        overlay_execution_state(db, rows)
        if product_id is not None:
            rows = [row for row in rows if row.get("product_id") == int(product_id)]
        if order_id is not None:
            rows = [row for row in rows if row.get("order_id") == int(order_id)]
        if root_item_id is not None:
            rows = [row for row in rows if int(root_item_id) in {
                int(value) for value in row.get("__r9_root_item_ids") or []
            }]
        if workshop_id is not None:
            rows = [row for row in rows if row.get("workshop_id") == int(workshop_id)]
        if status:
            values = STATUS_FILTER_GROUPS.get(str(status), (str(status),))
            rows = [row for row in rows if str(row.get("status") or "") in values]
        if coverage_status:
            rows = [row for row in rows if str(row.get("coverage_status") or "") == str(coverage_status)]
        if planning_contour:
            contour = str(planning_contour).strip().lower()
            if contour not in {"mrp", "1c"}:
                raise ValueError("unknown planning_contour")
            rows = [row for row in rows if str(row.get("order_source") or "") == contour]
        if launch_source:
            rows = [row for row in rows if str(row.get("launch_source") or "") == str(launch_source)]
        if date_from:
            rows = [row for row in rows if row.get("order_date") is not None and str(row["order_date"]) >= str(date_from)]
        if date_to:
            rows = [row for row in rows if row.get("order_date") is not None and str(row["order_date"]) <= str(date_to)]
        if search:
            needle = str(search).casefold()
            rows = [row for row in rows if needle in " ".join(
                str(row.get(key) or "") for key in ("order_number", "item_name", "item_code", "item_article")
            ).casefold()]
        sort_field = str(sort_by or "").strip().lower()
        descending = str(sort_dir or "").strip().lower() == "desc"
        if sort_field in {"order_date", "planned_start_date", "planned_finish_date", "readiness_need_date", "readiness_action_date", "readiness_priority_key"}:
            _canonical_journal_sort(rows, field=sort_field, descending=descending)
        else:
            _canonical_journal_sort(rows, field="order_date", descending=True)
        for row in rows:
            row.pop("__r9_root_item_ids", None)
        effective_limit = max(1, min(int(limit or 100), 500))
        requested_offset = max(0, int(offset or 0))
        max_offset = max(0, ((len(rows) - 1) // effective_limit) * effective_limit) if rows else 0
        effective_offset = min(requested_offset, max_offset)
        saved = dict(current_manifest.summary or {})
        return ProductionOrderJournalResponse.model_validate({
            "rows": rows[effective_offset:effective_offset + effective_limit],
            "total": len(rows),
            "limit": effective_limit,
            "offset": effective_offset,
            "latest_run_id": saved.get("latest_run_id"),
            "latest_source_plan_id": saved.get("latest_source_plan_id"),
            "truth_meta": build_truth_meta(truth),
        })
    except planning_truth.PlanningTruthUnavailable as exc:
        raise HTTPException(status_code=503, detail=jsonable_encoder(exc.as_dict())) from exc
    except ProductionControlJournalSnapshotUnavailable as exc:
        raise HTTPException(status_code=503, detail=jsonable_encoder(exc.as_dict())) from exc
    except CurrentExecutionUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "production_control_current_unavailable", "reason": str(exc)}) from exc
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


@router.patch("/orders/{product_id}/quantity", response_model=dict)
def patch_order_line_quantity(
    product_id: int,
    payload: OrderLineQuantityPayload,
    db: Session = Depends(get_db),
):
    """Изменить количество к запуску у локального заказа, ещё не открытого в 1С.

    Пересчитывает потребность компонентов: уже созданные локальные заявки на
    перемещение приводятся к новому количеству, выгруженные в 1С — возвращаются
    как заблокированные, их правит отдельная корректировка.
    """
    try:
        return update_local_order_quantity(
            db,
            int(product_id),
            float(payload.quantity),
            initiated_by=payload.initiated_by,
        )
    except planning_truth.PlanningTruthUnavailable as exc:
        raise HTTPException(status_code=503, detail=jsonable_encoder(exc.as_dict())) from exc
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=503, detail=jsonable_encoder(exc.as_dict())) from exc
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
    ledger_generation_id: Optional[int] = Query(default=None, gt=0),
    db: Session = Depends(get_db),
):
    """Preview BOM coverage for a saved MRP row without creating an order.

    The journal row and its detail request must use the same immutable Ledger
    generation.  A newer accepted generation may be published between the two
    requests, so an explicitly pinned, previously published journal snapshot
    remains readable.
    """
    try:
        truth = planning_truth.require_accepted_truth(
            db, "production_control.work_item_materials"
        )
        work = db.get(models.ReplenishmentWorkItem, int(work_item_id))
        requested_generation_id = int(
            ledger_generation_id
            if ledger_generation_id is not None
            else truth.generation_id
        )
        if work is None or int(work.ledger_generation_id) != requested_generation_id:
            raise HTTPException(status_code=404, detail="Актуальная расчётная строка не найдена")
        journal_snapshot = (
            db.query(models.PlanningReadSnapshot)
            .filter(
                models.PlanningReadSnapshot.consumer == PRODUCTION_JOURNAL_CONSUMER,
                models.PlanningReadSnapshot.snapshot_key == PRODUCTION_JOURNAL_SNAPSHOT_KEY,
                models.PlanningReadSnapshot.ledger_generation_id == requested_generation_id,
                models.PlanningReadSnapshot.truth_status == "accepted",
            )
            .one_or_none()
        )
        published_row = None if journal_snapshot is None else (
            db.query(models.PlanningReadRow)
            .filter(
                models.PlanningReadRow.snapshot_id == int(journal_snapshot.id),
                models.PlanningReadRow.row_kind == PRODUCTION_JOURNAL_PROPOSAL_ROW_KIND,
                models.PlanningReadRow.row_key == f"work-item:{int(work.id)}",
            )
            .one_or_none()
        )
        if journal_snapshot is None or published_row is None:
            raise HTTPException(
                status_code=404,
                detail="Опубликованный снимок строки расчёта не найден",
            )
        launch_qty = float(qty if qty is not None else work.replenishment_remaining_qty)
        if launch_qty <= 0 or launch_qty > float(work.replenishment_remaining_qty) + 1e-6:
            raise HTTPException(status_code=400, detail="Количество запуска вне доступного остатка")
        payload = preview_make_work_item_materials(
            db,
            work_item_id=int(work.id),
            item_id=int(work.item_id),
            quantity=launch_qty,
            spec_id=BomSpecificationResolver(db).default_spec_id(int(work.item_id)),
            ledger_generation_id=requested_generation_id,
            order_number=f"MRP-R-{int(work.requirement_id)}",
            run_id=int(work.run_id),
        )
        payload["truth_status"] = "accepted"
        payload["cutoff"] = journal_snapshot.cutoff.isoformat()
        return payload
    except HTTPException:
        raise
    except planning_truth.PlanningTruthUnavailable as exc:
        raise HTTPException(status_code=503, detail=jsonable_encoder(exc.as_dict())) from exc
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
            complete_order=not payload.partial,
            request_key=payload.request_key,
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
        from app.services.one_c_production_order_export import finalize_produced_orders_to_1c
        completion = finalize_produced_orders_to_1c(db, [int(command["order_id"])], manufacture_ids=[manufacture_id])
        return {
            **command,
            "order_completion": completion,
            "message": completion["message"],
            "resume_required": bool(completion.get("resume_required")),
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


@router.post("/sync-execution-from-1c", response_model=dict)
def post_sync_execution_from_1c(dry_run: bool = False, db: Session = Depends(get_db)):
    """Read mutable execution state from 1C without deriving completion locally.

    Production-order completion comes exclusively from
    ``СостояниеЗаказа_Key`` in 1C.  Accepted output remains a Ledger fact; it is
    refreshed by the production-order sync's canonical fact-cache step.
    """
    try:
        request = configured_production_order_sync_request(dry_run=bool(dry_run))
        orders = sync_production_orders_from_odata(db, request)
        transfers = sync_posted_transfers(db, dry_run=bool(dry_run))
        return {"orders": orders, "transfers": transfers}
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


class StandalonePieceworkPayload(BaseModel):
    qty: float = Field(gt=0, allow_inf_nan=False)
    request_key: str = Field(min_length=1, max_length=100)
    operation_executors: List[dict] = Field(min_length=1)


class StandalonePieceworkOptionsResponse(BaseModel):
    product_id: int
    item_name: str
    quantity: float
    unit: Optional[str] = None
    operations: List[ProductionOperationOptionResponse]


class StandalonePieceworkResultResponse(BaseModel):
    status: str
    message: str
    product_id: int
    command_id: int
    created: int = 0


@router.get("/orders/{product_id}/piecework-options", response_model=StandalonePieceworkOptionsResponse)
def get_standalone_piecework_options(product_id: int, db: Session = Depends(get_db)):
    from app.services.one_c_piecework_export import standalone_piecework_product
    try:
        product = standalone_piecework_product(db, product_id)
        operations = get_order_line_operations(product.product_id, db)
        return {"product_id": product.product_id, "item_name": product.item.item_name,
                "quantity": float(product.quantity), "unit": product.item.unit,
                "operations": operations["rows"]}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/orders/{product_id}/piecework", response_model=StandalonePieceworkResultResponse)
def post_standalone_piecework(product_id: int, payload: StandalonePieceworkPayload, db: Session = Depends(get_db)):
    from app.services.one_c_piecework_export import create_standalone_piecework
    try:
        return create_standalone_piecework(db, product_id, qty=payload.qty,
            operation_executors=payload.operation_executors, request_key=payload.request_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

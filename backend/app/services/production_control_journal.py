from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from ..models import (
    DefaultSpecification,
    Item,
    MrpRequirement,
    MrpFreezeComponent,
    PaintWeldChainLink,
    PaintWeldPair,
    PlannedOrder,
    PlanningRun,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionMaterialIssue,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ReservationEntry,
    ReplenishmentWorkItem,
    ResourceProductionKind,
    ShelfPolicy,
    ShelfProjection,
    SpecComponent,
    Specification,
    SyncLink,
    Unit,
)
from .production_control_common import (
    DONE_STATE_KEY,
    date_to_iso as _date_to_iso,
    looks_like_guid as _looks_like_guid,
    parse_date as _parse_date,
    to_float as _to_float,
)
from .production_control_domain import ensure_state as _ensure_state
from .forecast import forecast_payload as _forecast_payload
from .planning_truth import PlanningTruthReadiness, require_accepted_truth
from .paint_weld_pairs import is_welded_blocked
from .mrp_mutation_guard import (
    MrpMutationLineageError,
)
from .bom_specification_resolver import BomSpecificationResolver
from .item_ledger.production_output_cache import (
    accepted_product_output,
)
from .production_material_custody_events import append_material_issue_custody_event


# Plan: line statuses shown in the production journal.
# Legacy technical states are kept for compatibility and mapped to the compact
# workshop-facing labels below.
LINE_STATUSES = {
    "created",
    "shortage",
    "partial",
    "ready",
    "to_move",
    "assembled",
    "in_progress",
    "done",
    "produced_partial",
    "produced",
    "production_error",
    "completed",
    "cancelled",
}
# 'exported' = PRODPLAN posted the draft into 1C (Posted=false there).
# 'posted'   = 1C admin провёл документ (we discovered Posted=true on sync).
ISSUE_STATUSES = {"not_requested", "requested", "issued", "exported", "posted", "error"}
PRODUCTION_ORDER_ENTITY = "Document_ЗаказНаПроизводство"
COVERAGE_LABELS = {
    "unknown": "Недоступно",
    "shortage": "Дефицит",
    "partial": "Частично",
    "ready": "Обеспечен",
    "to_move": "К перемещению",
    "assembled": "Собрано",
    "in_progress": "В работе",
    "done": "Готов",
    "produced_partial": "Готов",
    "produced": "Готов",
    "production_error": "Ошибка выпуска",
    "completed": "Завершён",
    "cancelled": "Отменен",
}
STATUS_FILTER_GROUPS = {
    "created": ("created", "shortage", "partial"),
    "shortage": ("shortage",),
    "partial": ("partial",),
    "ready": ("ready",),
    "assembled": ("assembled",),
    "done": ("done", "produced_partial", "produced"),
}

ACTIVE_COVERAGE_STATUSES = {"shortage", "partial", "ready"}

# Where a journal line takes its launch quantity and launch date from.
# 'shelf_pull'    — DBR shelf projection of the accepted generation drives it;
# 'mrp_remaining' — no shelf for this item, legacy MRP remainder drives it.
LAUNCH_SOURCE_SHELF = "shelf_pull"
LAUNCH_SOURCE_MRP = "mrp_remaining"


@dataclass(frozen=True)
class _ShelfPull:
    """One saved shelf projection row, read never recomputed (CANON §Правила 1)."""

    item_id: int
    warehouse_ref1c: str
    pull_qty: float
    materialized_qty: float
    first_shortage_date: Optional[date]
    latest_start_date: Optional[date]


def _shelf_pull_by_item(
    db: Session,
    *,
    ledger_generation_id: Optional[int],
    item_ids: Optional[Sequence[int]] = None,
) -> Dict[int, _ShelfPull]:
    """Saved shelf pull of the accepted generation, keyed by produced item.

    The journal reads ``pull_qty`` / ``materialized_qty`` / ``latest_start_date``
    from the persisted projection.  Recomputing the shelf formulas here would
    create a second owner of the величина, which the canon forbids.
    """
    if ledger_generation_id is None:
        return {}
    query = (
        db.query(ShelfProjection, ShelfPolicy)
        .join(ShelfPolicy, ShelfPolicy.id == ShelfProjection.shelf_policy_id)
        .filter(ShelfProjection.ledger_generation_id == int(ledger_generation_id))
        .filter(ShelfPolicy.active.is_(True))
    )
    ids = sorted({int(value) for value in (item_ids or []) if value is not None})
    if item_ids is not None:
        if not ids:
            return {}
        query = query.filter(ShelfProjection.item_id.in_(ids))
    result: Dict[int, _ShelfPull] = {}
    for projection, _policy in query.order_by(
        ShelfProjection.item_id.asc(),
        ShelfProjection.shelf_policy_id.asc(),
    ).all():
        # An item with several shelves is resolved by the lowest policy id so
        # the journal stays deterministic across pages and reruns.
        result.setdefault(
            int(projection.item_id),
            _ShelfPull(
                item_id=int(projection.item_id),
                warehouse_ref1c=str(projection.warehouse_ref1c or ""),
                pull_qty=_to_float(projection.pull_qty),
                materialized_qty=_to_float(projection.materialized_qty),
                first_shortage_date=projection.first_shortage_date,
                latest_start_date=projection.latest_start_date,
            ),
        )
    return result


def _journal_work_status(line_status: str) -> str:
    # Legacy ``shortage`` / ``partial`` values were material-coverage bands
    # persisted in the workflow state.  They do not describe order lifecycle.
    # A materialized executor with either value is simply created/not started.
    if line_status in {"shortage", "partial"}:
        return "created"
    # "assembled" describes material coverage in the journal, not the workshop
    # action state. Keep the row actionable as "В работу".
    if line_status == "assembled":
        return "ready"
    return line_status


def _journal_coverage_status(
    line_status: str,
    issue_status: str,
    material_coverage_status: Optional[str] = None,
) -> str:
    if material_coverage_status in ACTIVE_COVERAGE_STATUSES:
        return material_coverage_status
    return "unknown"


def _active_mrp_products_for_requirement(db: Session, req: MrpRequirement) -> List[Tuple[ProductionProduct, ProductionOrder]]:
    """Active local materializations for the exact frozen requirement.

    Physical Ledger generations advance while the frozen MRP requirement stays
    immutable. Matching the exact requirement prevents duplicate executors
    without rewriting their creation-generation provenance.
    """
    return [
        (product, order)
        for product, order in (
        db.query(ProductionProduct, ProductionOrder)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .filter(ProductionProduct.source_mrp_requirement_id == int(req.id))
        .filter(ProductionOrder.source == "mrp")
        .filter(ProductionOrder.deletion_mark.is_(False))
        .all()
        )
    ]


def _reused_product_payload(
    product: ProductionProduct,
    order: Optional[ProductionOrder],
    *,
    requirement_id: int,
) -> Dict[str, Any]:
    item = getattr(product, "item", None)
    output = accepted_product_output(product)
    return {
        "requirement_id": int(requirement_id),
        "product_id": int(product.product_id),
        "order_id": int(product.order_id),
        "order_number": str(order.order_number or "") if order else None,
        "item_id": int(product.item_id),
        "item_name": str(item.item_name or "") if item else "",
        "qty": _to_float(output.remaining_qty),
    }


def _prodplan_order_display_number(product: ProductionProduct, order: ProductionOrder) -> str:
    order_source = str(order.source or "1c")
    if order_source != "mrp":
        return str(order.order_number or "")

    run_id = int(order.source_run_id) if order.source_run_id is not None else None
    planned_order_id = (
        int(product.source_planned_order_id)
        if getattr(product, "source_planned_order_id", None) is not None
        else None
    )
    if run_id is not None and planned_order_id is not None:
        return f"MRP-{run_id}-{planned_order_id}"

    requirement_id = (
        int(product.source_mrp_requirement_id)
        if getattr(product, "source_mrp_requirement_id", None) is not None
        else None
    )
    allocation_key = str(product.source_mrp_allocation_key or "")
    if requirement_id is not None and allocation_key.startswith(f"mrp_requirement:{requirement_id}:order:"):
        try:
            seq = int(allocation_key.rsplit(":", 1)[-1])
        except Exception:
            seq = 1
        return f"MRP-R-{requirement_id}" if seq <= 1 else f"MRP-R-{requirement_id}-{seq}"

    if run_id is not None and product.item_id is not None:
        return f"MRP-RC-{run_id}-{int(product.item_id)}-{int(order.order_id)}"

    return str(order.order_number or "")


def _bom_descendant_ids_for_root(db: Session, root_item_id: int) -> set[int]:
    return BomSpecificationResolver(db).descendant_ids_by_root(
        [int(root_item_id)]
    )[int(root_item_id)]


def _accepted_plan_snapshot_run_ids_for_root(
    db: Session,
    root_item_id: int,
    *,
    ledger_generation_id: int,
) -> set[int]:
    """Return fixed-plan snapshots from exactly the published Ledger generation.

    No ``max(run_id)`` is used here.  Publication owns the generation pointer;
    a fixed run from another (including newer) generation is not UI truth.
    """
    rows = (
        db.query(PlanningRun.run_id)
        .join(ProductionPlanHeader, ProductionPlanHeader.id == PlanningRun.source_plan_id)
        .join(ProductionPlanLine, ProductionPlanLine.plan_id == ProductionPlanHeader.id)
        .filter(ProductionPlanHeader.status == "fixed")
        .filter(ProductionPlanLine.item_id == int(root_item_id))
        .filter(PlanningRun.status == "FIXED_SNAPSHOT")
        .filter(PlanningRun.ledger_generation_id == int(ledger_generation_id))
        .all()
    )
    return {int(row[0]) for row in rows if row[0] is not None}


def _accepted_fixed_run_ids(db: Session, *, ledger_generation_id: int) -> List[int]:
    rows = (
        db.query(PlanningRun.run_id)
        .join(ProductionPlanHeader, ProductionPlanHeader.id == PlanningRun.source_plan_id)
        .filter(PlanningRun.status == "FIXED_SNAPSHOT")
        .filter(PlanningRun.ledger_generation_id == int(ledger_generation_id))
        .filter(ProductionPlanHeader.status == "fixed")
        .order_by(PlanningRun.run_id.asc())
        .all()
    )
    return [int(row[0]) for row in rows]


def _default_spec_ids_by_item(db: Session, item_ids: Sequence[int]) -> Dict[int, int]:
    from .workshop_resolution import default_spec_ids_for_items

    return default_spec_ids_for_items(db, item_ids)


def _main_workshops_for_specs(
    db: Session,
    spec_ids: Sequence[int],
) -> Dict[int, Tuple[Optional[int], Optional[str], Optional[int], Optional[str]]]:
    from .workshop_resolution import main_stages_for_specs

    ids = sorted({int(spec_id) for spec_id in spec_ids if spec_id})
    if not ids:
        return {}

    # Stage is display-only (the journal's "этап" column). The workshop comes
    # exclusively from the spec's production kind — the legacy stage->resource
    # fallback used to mask specs with an unfilled kind.
    stage_by_spec = main_stages_for_specs(db, ids)

    resource_by_spec: Dict[int, Tuple[int, str]] = {}
    for row in (
        db.query(Specification.spec_id, ResourceProductionKind.resource_id, ProductionResource.resource_name)
        .join(
            ResourceProductionKind,
            ResourceProductionKind.production_kind_id == Specification.production_kind_id,
        )
        .join(ProductionResource, ProductionResource.resource_id == ResourceProductionKind.resource_id)
        .filter(Specification.spec_id.in_(ids))
        .order_by(ResourceProductionKind.id.asc())
        .all()
    ):
        resource_by_spec.setdefault(
            int(row.spec_id),
            (int(row.resource_id), str(row.resource_name or "")),
        )

    result: Dict[int, Tuple[Optional[int], Optional[str], Optional[int], Optional[str]]] = {}
    for spec_id in ids:
        stage_id, stage_name = stage_by_spec.get(spec_id, (None, None))
        resource_id, resource_name = resource_by_spec.get(spec_id, (None, None))
        result[spec_id] = (resource_id, resource_name, stage_id, stage_name)
    return result


def _unit_display_by_raw(db: Session, raw_units: Sequence[Any]) -> Dict[str, str]:
    raw_values = {str(raw or "").strip() for raw in raw_units if str(raw or "").strip()}
    if not raw_values:
        return {}
    result: Dict[str, str] = {}
    guid_values = {raw for raw in raw_values if _looks_like_guid(raw)}
    if guid_values:
        for unit in db.query(Unit).filter(Unit.unit_ref1c.in_(list(guid_values))).all():
            result[str(unit.unit_ref1c or "").strip()] = str(
                unit.short_name or unit.unit_name or unit.unit_code or ""
            ).strip()
    for raw in raw_values:
        result.setdefault(raw, "" if _looks_like_guid(raw) else raw)
    return result


def materialize_make_work_items(
    db: Session,
    work_item_ids: Sequence[int],
    *,
    initiated_by: Optional[str] = None,
    launch_requests: Optional[Mapping[int, Mapping[str, float]]] = None,
) -> Dict[str, Any]:
    """
    Materialize canonical make work items into internal production orders.

    Idempotent: if a ProductionProduct already links to this requirement via
    source_mrp_requirement_id, the existing order is returned in `reused` and
    no duplicate is created.

    The frozen requirement and its Ledger reservation are never mutated.
    """
    selected_ids = [int(value) for value in work_item_ids]
    selected = (
        db.query(ReplenishmentWorkItem)
        .filter(ReplenishmentWorkItem.id.in_(selected_ids))
        .all()
    )
    if len({int(row.id) for row in selected}) != len(set(selected_ids)):
        raise MrpMutationLineageError("one or more selected work items do not exist")
    run_ids = {int(row.run_id) for row in selected}
    if len(run_ids) != 1:
        raise MrpMutationLineageError("selected work items have mixed or empty runs")
    selected_run_id = run_ids.pop()
    truth = require_accepted_truth(
        db,
        "production_control.materialize_make_work_items",
    )
    generation_id = int(truth.generation_id)
    run = db.get(PlanningRun, selected_run_id)
    if run is None or str(run.status or "").upper() != "FIXED_SNAPSHOT":
        raise MrpMutationLineageError(
            f"planning run {selected_run_id} is not a FIXED_SNAPSHOT"
        )
    if run.active_freeze_version is None:
        raise MrpMutationLineageError(
            f"planning run {selected_run_id} has no active freeze"
        )
    if any(
        int(row.ledger_generation_id) != generation_id
        or row.replenishment_method != "make"
        for row in selected
    ):
        raise MrpMutationLineageError(
            "selected work items are not current-generation make obligations"
        )
    selected_reservations = [
        db.get(ReservationEntry, int(row.reservation_id)) for row in selected
    ]
    if any(
        reservation is None
        or int(reservation.ledger_generation_id) != generation_id
        or str(reservation.lifecycle_status) != "active"
        or str(reservation.realization_mode) != "make"
        or int(reservation.requirement_id) != int(work.requirement_id)
        or int(reservation.item_id) != int(work.item_id)
        or int(reservation.run_id) != int(work.run_id)
        for work, reservation in zip(selected, selected_reservations)
    ):
        raise MrpMutationLineageError(
            "selected work items have invalid reservation lineage"
        )
    selected_requirements = [
        db.get(MrpRequirement, int(row.requirement_id)) for row in selected
    ]
    if any(
        requirement is None
        or int(requirement.run_id) != int(run.run_id)
        or requirement.freeze_version is None
        or int(requirement.freeze_version) != int(run.active_freeze_version)
        for requirement in selected_requirements
    ):
        raise MrpMutationLineageError(
            "selected work items are outside the current active freeze"
        )

    created: List[Dict[str, Any]] = []
    reused: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    errors: List[str] = []

    today = datetime.now(timezone.utc)

    # DBR owns how much of the frozen MRP remainder is pulled right now.  For a
    # shelf-managed item the launch quantity is the saved ``materialized_qty``
    # (= pull_qty rounded up to the shelf's batch_multiple, already capped by
    # the unlaunched MRP), spread across the selected requirements of that item.
    shelf_by_item = _shelf_pull_by_item(
        db,
        ledger_generation_id=generation_id,
        item_ids=[int(row.item_id) for row in selected],
    )
    shelf_allowance: Dict[int, float] = {
        item_id: float(pull.materialized_qty) for item_id, pull in shelf_by_item.items()
    }

    work_by_id = {int(row.id): row for row in selected}
    for work_id_raw in work_item_ids:
        try:
            work_id = int(work_id_raw)
        except Exception:
            errors.append(f"work_item_id={work_id_raw!r}: невалидный идентификатор")
            continue

        work = work_by_id.get(work_id)
        if work is None:
            errors.append(f"work_item_id={work_id}: рабочая строка не найдена")
            continue
        rid = int(work.requirement_id)
        req = db.get(MrpRequirement, rid)
        if not req:
            errors.append(f"work_item_id={work_id}: требование не найдено")
            continue

        item = db.query(Item).filter(Item.item_id == int(req.item_id)).first()
        if not item:
            errors.append(f"requirement_id={rid}: номенклатура {req.item_id} не найдена")
            continue

        # Сварная деталь активной пары «окраска↔сварка» не заказывается
        # самостоятельно — она пойдёт по цепочке от окрасочного заказа.
        # Сироты (нет активной пары) не блокируются — их надо заказывать вручную.
        if is_welded_blocked(db, [int(req.item_id)]):
            skipped.append({
                "requirement_id": rid,
                "item_id": int(req.item_id),
                "reason": "заказывается по цепочке от окраски",
            })
            continue

        net_qty = _to_float(work.replenishment_remaining_qty)
        if net_qty <= 1e-9:
            skipped.append(
                {
                    "work_item_id": work_id,
                    "requirement_id": rid,
                    "item_id": int(req.item_id),
                    "reason": "replenishment already fulfilled",
                }
            )
            continue
        active_products = _active_mrp_products_for_requirement(db, req)
        request = (launch_requests or {}).get(work_id)
        active_open_qty = sum(
            _to_float(accepted_product_output(product).remaining_qty)
            for product, _order in active_products
        )
        if request is None and active_products:
            for product, order in active_products:
                payload = _reused_product_payload(product, order, requirement_id=rid)
                payload["work_item_id"] = work_id
                reused.append(payload)
            skipped.append({
                "requirement_id": rid,
                "item_id": int(req.item_id),
                "reason": "already materialized for this Ledger generation",
            })
            continue
        remaining = max(0.0, net_qty - active_open_qty)
        if request is not None:
            requested_qty = _to_float(request.get("launch_qty"))
            expected_materialized_qty = _to_float(request.get("expected_materialized_qty"))
            target_qty = expected_materialized_qty + requested_qty
            if requested_qty <= 1e-9:
                errors.append(f"work_item_id={work_id}: количество запуска должно быть больше нуля")
                continue
            if abs(active_open_qty - target_qty) <= 1e-6:
                for product, order in active_products:
                    payload = _reused_product_payload(product, order, requirement_id=rid)
                    payload["work_item_id"] = work_id
                    reused.append(payload)
                continue
            if abs(active_open_qty - expected_materialized_qty) > 1e-6:
                errors.append(f"work_item_id={work_id}: состав живых заказов изменился; обновите журнал")
                continue
            if requested_qty - remaining > 1e-6:
                errors.append(f"work_item_id={work_id}: доступно к запуску {remaining:g}, запрошено {requested_qty:g}")
                continue
            remaining = requested_qty

        existing_count = (
            db.query(ProductionProduct)
            .filter(ProductionProduct.source_mrp_requirement_id == rid)
            .count()
        )
        planned_tasks = (
            db.query(PlannedOrder)
            .filter(
                PlannedOrder.run_id == int(req.run_id),
                PlannedOrder.demand_ref == f"mrp_requirement:{rid}",
            )
            .all()
        )
        planned_start = min((task.start_date for task in planned_tasks if task.start_date), default=req.period_from)
        planned_finish = max((task.finish_date or task.need_date for task in planned_tasks if task.finish_date or task.need_date), default=req.period_to)
        spec_id, spec_revision_hash = _frozen_spec_identity_for_requirement(
            db, req
        )

        shelf = shelf_by_item.get(int(req.item_id))
        launch_source = LAUNCH_SOURCE_MRP
        if shelf is None:
            # No shelf for this item: legacy MRP remainder split by optimal_batch.
            batches = _split_qty_by_optimal_batch(remaining, getattr(item, "optimal_batch", None))
        else:
            launch_source = LAUNCH_SOURCE_SHELF
            allowance = shelf_allowance.get(int(req.item_id), 0.0)
            pull_qty = min(remaining, allowance)
            if pull_qty <= 1e-9:
                skipped.append({
                    "work_item_id": work_id,
                    "requirement_id": rid,
                    "item_id": int(req.item_id),
                    "reason": "буфер полки закрыт: вытягивание не требуется",
                })
                continue
            shelf_allowance[int(req.item_id)] = allowance - pull_qty
            # materialized_qty is already a multiple of policy.batch_multiple and
            # already fits inside the unlaunched MRP, so it is launched as one
            # line; item.optimal_batch is the legacy fallback and must not
            # re-split a shelf pull.
            batches = [pull_qty]
            # Стартовать позже latest_start_date значит опоздать со сборкой.
            if shelf.latest_start_date is not None:
                planned_start = shelf.latest_start_date
            if shelf.first_shortage_date is not None:
                planned_finish = shelf.first_shortage_date
        for index, qty in enumerate(batches, start=1):
            seq = existing_count + index
            order_number = f"MRP-R-{rid}" if seq == 1 and len(batches) == 1 else f"MRP-R-{rid}-{seq}"
            order = ProductionOrder(
                order_number=order_number,
                order_date=today,
                order_ref1c=None,
                is_posted=False,
                deletion_mark=False,
                source="mrp",
                source_run_id=int(req.run_id),
            )
            db.add(order)
            db.flush()

            product = ProductionProduct(
                order_id=int(order.order_id),
                item_id=int(req.item_id),
                line_number=1,
                quantity=qty,
                produced_qty=0,
                remaining_qty=qty,
                spec_id=spec_id,
                spec_revision_hash=(
                    str(spec_revision_hash) if spec_revision_hash else None
                ),
                source_mrp_requirement_id=rid,
                source_mrp_allocation_key=f"mrp_requirement:{rid}:order:{seq}",
                ledger_generation_id=generation_id,
            )
            db.add(product)
            db.flush()

            state = ProductionOrderLineState(
                product_id=int(product.product_id),
                status="shortage",
                issue_status="not_requested",
                planned_start_date=planned_start,
                planned_finish_date=planned_finish,
            )
            db.add(state)

            created.append({
                "work_item_id": work_id,
                "requirement_id": rid,
                "product_id": int(product.product_id),
                "order_id": int(order.order_id),
                "order_number": order_number,
                "item_id": int(req.item_id),
                "item_name": str(item.item_name or ""),
                "qty": qty,
                "launch_source": launch_source,
                "shelf_warehouse_ref1c": shelf.warehouse_ref1c if shelf else None,
                "shelf_pull_qty": shelf.pull_qty if shelf else None,
                "shelf_latest_start_date": _date_to_iso(shelf.latest_start_date) if shelf else None,
            })

        # Requirement coverage/execution is derived from the Ledger.  Do not
        # authorize or stamp this mutation through legacy covered/remaining
        # caches; the generation reconciliation worker owns projections.

    db.commit()
    return {
        "status": "ok",
        "created": created,
        "reused": reused,
        "skipped": skipped,
        "errors": errors,
        "initiated_by": initiated_by,
    }


def _split_qty_by_optimal_batch(qty: float, optimal_batch: Optional[float]) -> List[float]:
    total = _to_float(qty)
    batch = _to_float(optimal_batch)
    if total <= 1e-9:
        return []
    if batch <= 1e-9 or batch >= total - 1e-9:
        return [total]

    result: List[float] = []
    remaining = total
    while remaining > 1e-9:
        part = min(batch, remaining)
        result.append(float(part))
        remaining -= part
    return result


def _default_spec_id_for_item(db: Session, item_id: int) -> Optional[int]:
    row = (
        db.query(DefaultSpecification)
        .filter(DefaultSpecification.item_id == int(item_id))
        .order_by(DefaultSpecification.id.asc())
        .first()
    )
    return int(row.spec_id) if row else None


def _frozen_spec_identity_for_requirement(
    db: Session,
    requirement: MrpRequirement,
) -> tuple[Optional[int], Optional[str]]:
    """Resolve the exact revision frozen for an MRP-backed order line.

    A logical 1C specification ref can now point at newer content. Creating an
    executor from an older frozen MRP would silently use that newer content in
    1C, so a known hash mismatch fails closed until successor-MRP is published.
    """
    spec_id = _default_spec_id_for_item(db, int(requirement.item_id))
    if spec_id is None:
        return None, None
    spec = db.get(Specification, int(spec_id))
    if spec is None:
        return spec_id, None
    run = db.get(PlanningRun, int(requirement.run_id))
    if run is None or run.active_freeze_version is None:
        return spec_id, str(spec.content_hash) if spec.content_hash else None
    spec_ref = str(spec.spec_ref1c or "").strip()
    if not spec_ref:
        return spec_id, str(spec.content_hash) if spec.content_hash else None
    versions = {
        str(version)
        for (version,) in db.query(MrpFreezeComponent.spec_version)
        .filter(
            MrpFreezeComponent.run_id == int(run.run_id),
            MrpFreezeComponent.freeze_version
            == int(run.active_freeze_version),
            MrpFreezeComponent.parent_item_id == int(requirement.item_id),
            MrpFreezeComponent.spec_ref == spec_ref,
            MrpFreezeComponent.spec_version.isnot(None),
        )
        .distinct()
        .all()
    }
    if len(versions) > 1:
        raise MrpMutationLineageError(
            f"requirement_id={int(requirement.id)}: frozen specification revision is ambiguous"
        )
    frozen_hash = next(iter(versions), None)
    current_hash = str(spec.content_hash) if spec.content_hash else None
    if frozen_hash and current_hash and frozen_hash != current_hash:
        raise MrpMutationLineageError(
            f"requirement_id={int(requirement.id)}: specification changed; "
            "successor-MRP must be published before creating new orders"
        )
    return spec_id, frozen_hash or current_hash


def _available_actions_for_journal_row(
    *,
    order: ProductionOrder,
    status: str,
    has_1c_link: bool,
) -> list[str]:
    if has_1c_link and str(order.source or "1c").lower() == "mrp":
        if status not in {"completed", "produced_partial", "produced", "cancelled", "done"}:
            return ["close_1c"]
    return []


def _paint_weld_pair_metadata_by_item(
    db: Session,
    item_ids: Sequence[int],
) -> Dict[int, Dict[str, Any]]:
    ids = sorted({int(value) for value in item_ids if value is not None})
    if not ids:
        return {}
    pairs = (
        db.query(PaintWeldPair)
        .filter(PaintWeldPair.is_active.is_(True))
        .filter(
            (PaintWeldPair.painted_item_id.in_(ids))
            | (PaintWeldPair.welded_item_id.in_(ids))
        )
        .order_by(PaintWeldPair.id.asc())
        .all()
    )
    counterpart_ids = sorted(
        {int(pair.painted_item_id) for pair in pairs}
        | {int(pair.welded_item_id) for pair in pairs}
    )
    items = {
        int(item.item_id): item
        for item in db.query(Item).filter(Item.item_id.in_(counterpart_ids)).all()
    }
    blocked_welded = is_welded_blocked(db, ids)
    result: Dict[int, Dict[str, Any]] = {}
    for pair in pairs:
        painted_id = int(pair.painted_item_id)
        welded_id = int(pair.welded_item_id)
        painted = items.get(painted_id)
        welded = items.get(welded_id)
        if painted_id in ids:
            result[painted_id] = {
                "pair_id": int(pair.id),
                "role": "painted",
                "counterpart_item_id": welded_id,
                "counterpart_item_code": str(welded.item_code or "") if welded else "",
                "counterpart_item_name": str(welded.item_name or "") if welded else "",
                "counterpart_item_article": str(welded.item_article or "") if welded else "",
                "selection_disabled_reason": None,
            }
        if welded_id in ids and welded_id not in result:
            result[welded_id] = {
                "pair_id": int(pair.id),
                "role": "welded",
                "counterpart_item_id": painted_id,
                "counterpart_item_code": str(painted.item_code or "") if painted else "",
                "counterpart_item_name": str(painted.item_name or "") if painted else "",
                "counterpart_item_article": str(painted.item_article or "") if painted else "",
                "selection_disabled_reason": (
                    "Сварная деталь входит в цепочку сварка → окраска; "
                    "запуск выполняется из окрашенной строки"
                    if welded_id in blocked_welded
                    else None
                ),
            }
    return result


def list_journal(
    db: Session,
    *,
    truth: PlanningTruthReadiness | None = None,
    _accepted_run_ids_override: Optional[Sequence[int]] = None,
    _material_coverage_by_product: Optional[Dict[int, Dict[str, Any]]] = None,
    product_id: Optional[int] = None,
    order_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    workshop_id: Optional[int] = None,
    status: Optional[str] = None,
    coverage_status: Optional[str] = None,
    planning_contour: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    truth = truth or require_accepted_truth(db, "production_control_journal")
    accepted_run_ids = (
        sorted({int(value) for value in _accepted_run_ids_override})
        if _accepted_run_ids_override is not None
        else _accepted_fixed_run_ids(
            db,
            ledger_generation_id=int(truth.generation_id),
        )
    )
    # A scalar is retained only for an unambiguous scope.  Never choose the
    # numerically latest run as that can belong to a foreign generation.
    run_id = accepted_run_ids[0] if len(accepted_run_ids) == 1 else None
    latest_run = db.query(PlanningRun).filter(PlanningRun.run_id == run_id).first() if run_id is not None else None
    requested_coverage_status = str(coverage_status) if coverage_status else None
    query = (
        db.query(ProductionProduct)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .join(Item, Item.item_id == ProductionProduct.item_id)
        .outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .filter(ProductionOrder.deletion_mark == False)
        .filter(or_(ProductionOrder.order_state_key.is_(None), func.lower(ProductionOrder.order_state_key) != DONE_STATE_KEY))
        .filter(
            func.coalesce(ProductionProduct.quantity, 0)
            > func.coalesce(ProductionProduct.produced_qty, 0)
        )
        .filter(
            func.coalesce(ProductionOrderLineState.status, "shortage").notin_(
                tuple(_TERMINAL_LINE_STATUSES)
            )
        )
        .options(
            joinedload(ProductionProduct.order),
            joinedload(ProductionProduct.item),
            joinedload(ProductionProduct.control_state).joinedload(ProductionOrderLineState.workshop),
        )
    )

    # 1C work stays visible on its own factual lineage. MRP-originated
    # work, however, is an obligation projection and is valid only when its
    # source snapshot belongs to the exact published generation and a fixed
    # production plan.  A missing source_run_id is legacy/ambiguous, not a
    # reason to show a made-up current MRP line.
    query = query.filter(
        or_(
            func.lower(func.coalesce(ProductionOrder.source, "1c")) != "mrp",
            ProductionOrder.source_run_id.in_(accepted_run_ids or [-1]),
        )
    )

    if product_id is not None:
        query = query.filter(ProductionProduct.product_id == int(product_id))
    if order_id is not None:
        query = query.filter(ProductionOrder.order_id == int(order_id))
    if root_item_id is not None:
        related_ids = _bom_descendant_ids_for_root(db, int(root_item_id))
        query = query.filter(ProductionProduct.item_id.in_(related_ids))
        active_run_ids = _accepted_plan_snapshot_run_ids_for_root(
            db,
            int(root_item_id),
            ledger_generation_id=int(truth.generation_id),
        )
        query = query.filter(ProductionOrder.source_run_id.in_(sorted(active_run_ids) or [-1]))
    if status:
        status_values = STATUS_FILTER_GROUPS.get(str(status), (str(status),))
        query = query.filter(func.coalesce(ProductionOrderLineState.status, "shortage").in_(status_values))
    if planning_contour:
        contour = str(planning_contour).strip().lower()
        if contour in {"mrp", "1c"}:
            query = query.filter(ProductionOrder.source == contour)
        else:
            raise ValueError("unknown planning_contour")
    if search:
        like = f"%{search.strip()}%"
        query = query.filter(
            or_(
                ProductionOrder.order_number.ilike(like),
                Item.item_name.ilike(like),
                Item.item_article.ilike(like),
                Item.item_code.ilike(like),
            )
        )
    start = _parse_date(date_from)
    finish = _parse_date(date_to)
    if start:
        query = query.filter(ProductionOrder.order_date >= datetime.combine(start, datetime.min.time()))
    if finish:
        query = query.filter(ProductionOrder.order_date < datetime.combine(finish, datetime.max.time()))

    sort_field = (sort_by or "").strip().lower()
    fast_page = not workshop_id and not requested_coverage_status
    if fast_page:
        total = int(query.count())
        effective_limit = max(1, min(int(limit or 100), 500))
        requested_offset = max(0, int(offset or 0))
        max_offset = max(0, ((total - 1) // effective_limit) * effective_limit) if total else 0
        effective_offset = min(requested_offset, max_offset)
        if sort_field in {"planned_start_date", "planned_finish_date"}:
            planning_dates_sq = (
                db.query(
                    PlanningRun.run_id.label("run_id"),
                    func.coalesce(PlanningRun.period_from, ProductionPlanHeader.period_from).label("start_date"),
                    func.coalesce(PlanningRun.period_to, ProductionPlanHeader.period_to).label("finish_date"),
                )
                .outerjoin(ProductionPlanHeader, ProductionPlanHeader.id == PlanningRun.source_plan_id)
                .filter(PlanningRun.run_id.in_(accepted_run_ids))
                .subquery()
            )
            query = query.outerjoin(
                planning_dates_sq, planning_dates_sq.c.run_id == ProductionOrder.source_run_id
            )
            date_expr = (
                func.coalesce(ProductionOrderLineState.planned_finish_date, planning_dates_sq.c.finish_date)
                if sort_field == "planned_finish_date"
                else func.coalesce(ProductionOrderLineState.planned_start_date, planning_dates_sq.c.start_date)
            )
            if (sort_dir or "").strip().lower() == "desc":
                query = query.order_by(date_expr.is_(None), date_expr.desc(), ProductionOrder.order_number.asc(), ProductionProduct.line_number.asc())
            else:
                query = query.order_by(date_expr.is_(None), date_expr.asc(), ProductionOrder.order_number.asc(), ProductionProduct.line_number.asc())
        else:
            query = query.order_by(ProductionOrder.order_date.desc(), ProductionOrder.order_number.asc(), ProductionProduct.line_number.asc())
        rows = query.offset(effective_offset).limit(effective_limit).all()
    else:
        rows = query.order_by(ProductionOrder.order_date.desc(), ProductionOrder.order_number.asc(), ProductionProduct.line_number.asc()).all()
        total = 0
        effective_limit = max(1, min(int(limit or 100), 500))
        effective_offset = max(0, int(offset or 0))

    page_item_ids = sorted({int(product.item_id) for product in rows if product.item_id is not None})
    # The shelf projection of the accepted generation owns the launch date of a
    # shelf-managed item; otherwise only explicit state or frozen-plan period
    # dates are used.
    shelf_by_item = _shelf_pull_by_item(
        db,
        ledger_generation_id=int(truth.generation_id) if truth.generation_id is not None else None,
        item_ids=page_item_ids,
    )
    order_ids = sorted({int(product.order_id) for product in rows})
    run_ids = sorted({
        int(product.order.source_run_id)
        for product in rows
        if product.order and product.order.source_run_id is not None
    })
    order_one_c_number_by_id: Dict[int, str] = {}
    if order_ids:
        for row in (
            db.query(SyncLink.source_id, SyncLink.target_number)
            .filter(
                SyncLink.source_system == "PRODPLAN",
                SyncLink.source_doctype == "production_order",
                SyncLink.source_id.in_(order_ids),
                SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
                SyncLink.status == "success",
                SyncLink.target_number.isnot(None),
            )
            .all()
        ):
            order_one_c_number_by_id[int(row.source_id)] = str(row.target_number or "")
    source_plan_by_run_id: Dict[int, Dict[str, Any]] = {}
    source_plan_dates_by_run_id: Dict[int, Tuple[Optional[date], Optional[date]]] = {}
    if run_ids:
        run_rows = (
            db.query(PlanningRun, ProductionPlanHeader)
            .outerjoin(ProductionPlanHeader, ProductionPlanHeader.id == PlanningRun.source_plan_id)
            .filter(PlanningRun.run_id.in_(run_ids))
            .all()
        )
        for run, plan in run_rows:
            period_from = run.period_from or (plan.period_from if plan else None)
            period_to = run.period_to or (plan.period_to if plan else None)
            source_plan_by_run_id[int(run.run_id)] = {
                "source_plan_id": int(run.source_plan_id) if run.source_plan_id is not None else None,
                "source_plan_name": str(plan.name or "") if plan else "",
                "source_plan_period_from": _date_to_iso(period_from),
                "source_plan_period_to": _date_to_iso(period_to),
            }
            source_plan_dates_by_run_id[int(run.run_id)] = (period_from, period_to)
    planned_order_ids = sorted({
        int(product.source_planned_order_id)
        for product in rows
        if getattr(product, "source_planned_order_id", None) is not None
    })
    req_ids = sorted({
        int(product.source_mrp_requirement_id)
        for product in rows
        if getattr(product, "source_mrp_requirement_id", None) is not None
    })
    planned_due_by_id: Dict[int, date] = {}
    if planned_order_ids:
        for row in (
            db.query(PlannedOrder.order_id, PlannedOrder.need_date)
            .filter(PlannedOrder.order_id.in_(planned_order_ids))
            .all()
        ):
            if row.need_date:
                planned_due_by_id[int(row.order_id)] = row.need_date
    req_meta_by_id: Dict[int, Dict[str, Any]] = {}
    if req_ids:
        work_by_requirement = {
            int(row.requirement_id): row
            for row in (
                db.query(ReplenishmentWorkItem)
                .filter(
                    ReplenishmentWorkItem.requirement_id.in_(req_ids),
                    ReplenishmentWorkItem.replenishment_method == "make",
                    ReplenishmentWorkItem.ledger_generation_id == int(truth.generation_id),
                )
                .all()
            )
        }
        for row in (
            db.query(
                MrpRequirement.id,
                MrpRequirement.period_to,
            )
            .filter(MrpRequirement.id.in_(req_ids))
            .all()
        ):
            work = work_by_requirement.get(int(row.id))
            req_meta_by_id[int(row.id)] = {
                "period_to": row.period_to,
                "net_required_qty": (
                    _to_float(work.replenishment_required_qty) if work else None
                ),
                "covered_qty": (
                    _to_float(work.replenishment_fulfilled_qty) if work else None
                ),
                "remaining_qty": (
                    _to_float(work.replenishment_remaining_qty) if work else None
                ),
            }
    item_ids = sorted({int(product.item_id) for product in rows if product.item_id is not None})
    pair_by_item = _paint_weld_pair_metadata_by_item(db, item_ids)
    product_ids = sorted({int(product.product_id) for product in rows if product.product_id is not None})
    default_spec_by_item = _default_spec_ids_by_item(db, item_ids)
    spec_ids = sorted({
        int(product.spec_id or default_spec_by_item.get(int(product.item_id)) or 0)
        for product in rows
        if product.spec_id or default_spec_by_item.get(int(product.item_id))
    })
    workshop_by_spec = _main_workshops_for_specs(db, spec_ids)
    issue_count_by_product: Dict[int, int] = {}
    if product_ids:
        for row in (
            db.query(ProductionMaterialIssue.product_id, func.count(ProductionMaterialIssue.issue_id).label("issue_count"))
            .filter(ProductionMaterialIssue.product_id.in_(product_ids))
            .group_by(ProductionMaterialIssue.product_id)
            .all()
        ):
            issue_count_by_product[int(row.product_id)] = int(row.issue_count or 0)
    unit_by_raw = _unit_display_by_raw(db, [getattr(product.item, "unit", None) for product in rows if product.item])

    # Цепочки «окраска↔сварка» (этап 4): признак роли строки и вторая сторона —
    # для кнопки «закрыть цепочку» и подсветки в журнале.
    chain_by_order_id: Dict[int, Dict[str, Any]] = {}
    page_order_ids = sorted({int(product.order_id) for product in rows if product.order_id is not None})
    if page_order_ids:
        chain_links = (
            db.query(PaintWeldChainLink)
            .filter(
                (PaintWeldChainLink.painted_order_id.in_(page_order_ids))
                | (PaintWeldChainLink.welded_order_id.in_(page_order_ids))
            )
            .all()
        )
        if chain_links:
            counterpart_order_ids = sorted(
                {int(link.painted_order_id) for link in chain_links}
                | {int(link.welded_order_id) for link in chain_links}
            )
            product_by_order: Dict[int, ProductionProduct] = {}
            for counterpart_product in (
                db.query(ProductionProduct)
                .filter(ProductionProduct.order_id.in_(counterpart_order_ids))
                .options(
                    joinedload(ProductionProduct.order),
                    joinedload(ProductionProduct.item),
                    joinedload(ProductionProduct.control_state).joinedload(ProductionOrderLineState.workshop),
                )
                .order_by(
                    ProductionProduct.order_id.asc(),
                    ProductionProduct.line_number.asc(),
                    ProductionProduct.product_id.asc(),
                )
                .all()
            ):
                product_by_order.setdefault(int(counterpart_product.order_id), counterpart_product)
            counterpart_units = _unit_display_by_raw(
                db,
                [product.item.unit for product in product_by_order.values() if product.item],
            )

            def _counterpart_payload(order_id: int) -> Dict[str, Any]:
                product = product_by_order.get(order_id)
                if product is None:
                    return {}
                output = accepted_product_output(product)
                state = getattr(product, "control_state", None)
                return {
                    "counterpart_product_id": int(product.product_id),
                    "counterpart_order_number": str(product.order.order_number or ""),
                    "counterpart_order_prodplan_number": _prodplan_order_display_number(product, product.order),
                    "counterpart_item_name": str(product.item.item_name or ""),
                    "counterpart_item_article": str(product.item.item_article or ""),
                    "counterpart_item_code": str(product.item.item_code or ""),
                    "counterpart_quantity": _to_float(output.planned_qty),
                    "counterpart_remaining_qty": _to_float(output.remaining_qty),
                    "counterpart_unit": counterpart_units.get(str(product.item.unit or "").strip(), ""),
                    "counterpart_workshop_name": (
                        state.workshop.resource_name if state and state.workshop else None
                    ),
                }
            for link in chain_links:
                painted_oid = int(link.painted_order_id)
                welded_oid = int(link.welded_order_id)
                chain_by_order_id[painted_oid] = {
                    "role": "painted",
                    "link_id": int(link.id),
                    "counterpart_order_id": welded_oid,
                    **_counterpart_payload(welded_oid),
                }
                chain_by_order_id[welded_oid] = {
                    "role": "welded",
                    "link_id": int(link.id),
                    "counterpart_order_id": painted_oid,
                    **_counterpart_payload(painted_oid),
                }

    result: List[Dict[str, Any]] = []
    for product in rows:
        state = getattr(product, "control_state", None)
        spec_id = int(product.spec_id or default_spec_by_item.get(int(product.item_id)) or 0) or None
        inferred_workshop_id, inferred_workshop_name, stage_id, stage_name = workshop_by_spec.get(
            spec_id or 0,
            (None, None, None, None),
        )
        state_workshop_id = (
            int(state.workshop_id)
            if state
            and state.workshop_id
            and str(getattr(state, "workshop_id_source", "") or "") not in {"auto", "legacy"}
            else None
        )
        resolved_workshop_id = state_workshop_id or inferred_workshop_id
        if workshop_id and resolved_workshop_id != int(workshop_id):
            continue

        planned_start = None
        planned_finish = None
        source_plan_dates = source_plan_dates_by_run_id.get(
            int(product.order.source_run_id) if product.order and product.order.source_run_id is not None else 0,
            (None, None),
        )
        if source_plan_dates:
            planned_start, planned_finish = source_plan_dates
        shelf = shelf_by_item.get(int(product.item_id))
        if shelf is not None and shelf.latest_start_date is not None:
            planned_start = shelf.latest_start_date
        if shelf is not None and shelf.first_shortage_date is not None:
            planned_finish = shelf.first_shortage_date
        if state and state.planned_start_date:
            planned_start = state.planned_start_date
        if state and state.planned_finish_date:
            planned_finish = state.planned_finish_date
        due_date = None
        source_planned_order_id = int(product.source_planned_order_id) if product.source_planned_order_id is not None else None
        source_mrp_requirement_id = int(product.source_mrp_requirement_id) if product.source_mrp_requirement_id is not None else None
        if source_planned_order_id is not None:
            due_date = planned_due_by_id.get(source_planned_order_id)
        req_meta = req_meta_by_id.get(source_mrp_requirement_id or 0, {})
        if due_date is None and source_mrp_requirement_id is not None:
            due_date = req_meta.get("period_to")
        forecast = _forecast_payload(planned_finish, due_date or planned_finish)
        order_source = str(product.order.source or "1c")
        run_id_for_source = int(product.order.source_run_id) if product.order.source_run_id is not None else None
        source_plan = source_plan_by_run_id.get(run_id_for_source or 0, {})
        order_one_c_number = order_one_c_number_by_id.get(int(product.order_id), "")
        if not order_one_c_number and product.order.order_ref1c and order_source == "1c":
            order_one_c_number = str(product.order.order_number or "")

        issue_count = issue_count_by_product.get(int(product.product_id), 0)
        line_status = str(state.status if state else "shortage")
        issue_status = str(state.issue_status if state else "not_requested")
        material_snapshot = (_material_coverage_by_product or {}).get(int(product.product_id))
        material_coverage_status = str((material_snapshot or {}).get("coverage_status") or "")
        material_coverage_label = str((material_snapshot or {}).get("coverage_label") or "")
        row_coverage_status = _journal_coverage_status(line_status, issue_status, material_coverage_status)
        work_status = _journal_work_status(line_status)
        has_1c_link = _production_order_has_1c_link(db, product.order)
        available_actions = _available_actions_for_journal_row(
            order=product.order,
            status=work_status,
            has_1c_link=has_1c_link,
        )
        pair_metadata = pair_by_item.get(int(product.item_id))
        selection_disabled_reason = (
            str(pair_metadata.get("selection_disabled_reason") or "")
            if pair_metadata
            else ""
        ) or None
        if selection_disabled_reason:
            available_actions = []
        coverage_label = (
            material_coverage_label
            if row_coverage_status == material_coverage_status and material_coverage_label
            else COVERAGE_LABELS.get(row_coverage_status, row_coverage_status)
        )
        output = accepted_product_output(product)
        result.append(
            {
                "journal_row_key": f"product:{int(product.product_id)}",
                "work_item_id": None,
                "product_id": int(product.product_id),
                "order_id": int(product.order_id),
                "order_number": str(product.order.order_number or ""),
                "order_prodplan_number": _prodplan_order_display_number(product, product.order),
                "order_date": _date_to_iso(product.order.order_date),
                # 'mrp' = generated by PRODPLAN (eligible for /orders/export-to-1c);
                # '1c'  = synced from 1C (already there, do not export).
                "order_source": order_source,
                "source": order_source,
                "order_ref1c": str(product.order.order_ref1c or "") if product.order.order_ref1c else None,
                "order_one_c_number": order_one_c_number or None,
                "line_number": product.line_number,
                "item_id": int(product.item_id),
                "item_code": str(product.item.item_code or ""),
                "item_name": str(product.item.item_name or ""),
                "item_article": str(product.item.item_article or ""),
                "optimal_batch": _to_float(product.item.optimal_batch) if product.item.optimal_batch is not None else None,
                "unit": unit_by_raw.get(str(product.item.unit or "").strip(), ""),
                "quantity": _to_float(output.planned_qty),
                "produced_qty": _to_float(output.produced_qty),
                "remaining_qty": _to_float(output.remaining_qty),
                "status": work_status,
                "coverage_status": row_coverage_status,
                "coverage_label": coverage_label,
                "issue_status": issue_status,
                "material_coverage_status": material_coverage_status or None,
                "material_coverage_label": material_coverage_label or None,
                "material_coverage_calculated_at": _date_to_iso(truth.cutoff),
                "material_coverage_snapshot": material_snapshot,
                "planned_start_date": _date_to_iso(planned_start),
                "planned_finish_date": _date_to_iso(planned_finish),
                **forecast,
                "opened_at": _date_to_iso(state.opened_at) if state else None,
                "workshop_id": resolved_workshop_id,
                "workshop_name": (state.workshop.resource_name if state and state.workshop else inferred_workshop_name),
                "stage_id": stage_id,
                "stage_name": stage_name,
                "spec_id": spec_id,
                "spec_revision_hash": (
                    str(product.spec_revision_hash)
                    if product.spec_revision_hash
                    else None
                ),
                "issue_count": int(issue_count),
                "route_sheet_printed_at": _date_to_iso(state.route_sheet_printed_at) if state else None,
                "comment": str(state.comment or "") if state else "",
                "source_run_id": int(product.order.source_run_id) if product.order.source_run_id is not None else None,
                **source_plan,
                "source_planned_order_id": source_planned_order_id,
                "source_mrp_requirement_id": source_mrp_requirement_id,
                "source_mrp_allocation_key": str(product.source_mrp_allocation_key or "") if product.source_mrp_allocation_key else None,
                "available_actions": available_actions,
                "selection_disabled_reason": selection_disabled_reason,
                "mrp_req_net_qty": req_meta.get("net_required_qty"),
                "mrp_req_covered_qty": req_meta.get("covered_qty"),
                "mrp_req_remaining_qty": req_meta.get("remaining_qty"),
                # Чем управляется запуск строки: вытягиванием полки или
                # остатком MRP. Оператор видит источник количества и даты.
                "launch_source": LAUNCH_SOURCE_SHELF if shelf else LAUNCH_SOURCE_MRP,
                "shelf_warehouse_ref1c": shelf.warehouse_ref1c if shelf else None,
                "shelf_pull_qty": shelf.pull_qty if shelf else None,
                "shelf_materialized_qty": shelf.materialized_qty if shelf else None,
                "shelf_latest_start_date": _date_to_iso(shelf.latest_start_date) if shelf else None,
                "paint_weld_chain": chain_by_order_id.get(int(product.order_id)),
                "paint_weld_pair": pair_metadata,
            }
        )

    if requested_coverage_status:
        result = [row for row in result if str(row.get("coverage_status") or "") == requested_coverage_status]

    if not fast_page and sort_field in {"planned_start_date", "planned_finish_date"}:
        descending = (sort_dir or "").strip().lower() == "desc"
        result.sort(key=lambda row: (row.get("order_number") or "", row.get("line_number") or 0))
        result.sort(key=lambda row: row.get(sort_field) or "", reverse=descending)
        result.sort(key=lambda row: row.get(sort_field) in (None, ""))

    if not fast_page:
        total = len(result)
        requested_offset = max(0, int(offset or 0))
        max_offset = max(0, ((total - 1) // effective_limit) * effective_limit) if total else 0
        effective_offset = min(requested_offset, max_offset)
        result = result[effective_offset : effective_offset + effective_limit]
    return {
        "rows": result,
        "total": total,
        "limit": effective_limit,
        "offset": effective_offset,
        "latest_run_id": run_id,
        "latest_source_plan_id": int(latest_run.source_plan_id) if latest_run and latest_run.source_plan_id is not None else None,
    }


def list_make_proposals(
    db: Session,
    *,
    ledger_generation_id: int,
    accepted_run_ids: Sequence[int],
) -> List[Dict[str, Any]]:
    """Project unmaterialized MAKE work items into the unified journal.

    These are saved MRP calculations, not executable ``ProductionOrder``
    documents.  They stay read-only until the operator explicitly materializes
    them; consequently product/order identifiers are absent and no 1C action is
    performed while the Ledger candidate is built.
    """
    generation_id = int(ledger_generation_id)
    run_ids = sorted({int(value) for value in accepted_run_ids})
    if not run_ids:
        return []

    query = (
        db.query(ReplenishmentWorkItem)
        .filter(
            ReplenishmentWorkItem.ledger_generation_id == generation_id,
            ReplenishmentWorkItem.run_id.in_(run_ids),
            ReplenishmentWorkItem.replenishment_method == "make",
            ReplenishmentWorkItem.replenishment_remaining_qty > 0,
        )
        .order_by(ReplenishmentWorkItem.run_id.asc(), ReplenishmentWorkItem.id.asc())
    )
    work_items = query.all()
    if not work_items:
        return []

    item_ids = sorted({int(row.item_id) for row in work_items})
    pair_by_item = _paint_weld_pair_metadata_by_item(db, item_ids)
    requirement_ids = sorted({int(row.requirement_id) for row in work_items})
    items = {
        int(item.item_id): item
        for item in db.query(Item).filter(Item.item_id.in_(item_ids)).all()
    }
    requirements = {
        int(row.id): row
        for row in db.query(MrpRequirement).filter(MrpRequirement.id.in_(requirement_ids)).all()
    }
    active_open_by_requirement: Dict[int, float] = {}
    for requirement_id, quantity, produced_qty in (
        db.query(
            ProductionProduct.source_mrp_requirement_id,
            ProductionProduct.quantity,
            ProductionProduct.produced_qty,
        )
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .filter(
            ProductionOrder.source == "mrp",
            ProductionOrder.deletion_mark.is_(False),
            ProductionProduct.source_mrp_requirement_id.in_(requirement_ids),
        )
        .all()
    ):
        rid = int(requirement_id)
        active_open_by_requirement[rid] = active_open_by_requirement.get(rid, 0.0) + max(
            0.0, _to_float(quantity) - _to_float(produced_qty)
        )
    run_rows = (
        db.query(PlanningRun, ProductionPlanHeader)
        .outerjoin(
            ProductionPlanHeader,
            ProductionPlanHeader.id == PlanningRun.source_plan_id,
        )
        .filter(PlanningRun.run_id.in_(run_ids))
        .all()
    )
    run_meta = {
        int(run.run_id): {
            "source_plan_id": int(run.source_plan_id) if run.source_plan_id is not None else None,
            "source_plan_name": str(plan.name or "") if plan else "",
            "source_plan_period_from": _date_to_iso(
                run.period_from or (plan.period_from if plan else None)
            ),
            "source_plan_period_to": _date_to_iso(
                run.period_to or (plan.period_to if plan else None)
            ),
        }
        for run, plan in run_rows
    }
    planned_dates: Dict[int, Tuple[Optional[date], Optional[date]]] = {}
    for demand_ref, start_date, finish_date in (
        db.query(
            PlannedOrder.demand_ref,
            func.min(PlannedOrder.start_date),
            func.max(func.coalesce(PlannedOrder.finish_date, PlannedOrder.need_date)),
        )
        .filter(PlannedOrder.run_id.in_(run_ids))
        .filter(PlannedOrder.demand_ref.isnot(None))
        .group_by(PlannedOrder.demand_ref)
        .all()
    ):
        token = str(demand_ref or "")
        if not token.startswith("mrp_requirement:"):
            continue
        try:
            requirement_id = int(token.split(":", 1)[1])
        except (TypeError, ValueError):
            continue
        planned_dates[requirement_id] = (start_date, finish_date)

    spec_by_item = _default_spec_ids_by_item(db, item_ids)
    spec_ids = sorted(set(spec_by_item.values()))
    workshop_by_spec = _main_workshops_for_specs(db, spec_ids)
    unit_by_raw = _unit_display_by_raw(
        db,
        [items[item_id].unit for item_id in item_ids if item_id in items],
    )
    shelf_by_item = _shelf_pull_by_item(
        db,
        ledger_generation_id=generation_id,
        item_ids=item_ids,
    )
    shelf_allowance = {
        item_id: float(pull.materialized_qty) for item_id, pull in shelf_by_item.items()
    }

    result: List[Dict[str, Any]] = []
    for work in work_items:
        item = items.get(int(work.item_id))
        requirement = requirements.get(int(work.requirement_id))
        if item is None or requirement is None:
            raise ValueError(
                f"MAKE work item {int(work.id)} has incomplete item/requirement lineage"
            )
        planned_start, planned_finish = planned_dates.get(
            int(requirement.id),
            (requirement.period_from, requirement.period_to),
        )
        shelf = shelf_by_item.get(int(work.item_id))
        if shelf is not None and shelf.latest_start_date is not None:
            planned_start = shelf.latest_start_date
        if shelf is not None and shelf.first_shortage_date is not None:
            planned_finish = shelf.first_shortage_date
        spec_id = spec_by_item.get(int(work.item_id))
        workshop_id, workshop_name, stage_id, stage_name = workshop_by_spec.get(
            int(spec_id or 0),
            (None, None, None, None),
        )
        required_qty = _to_float(work.replenishment_required_qty)
        fulfilled_qty = _to_float(work.replenishment_fulfilled_qty)
        remaining_qty = _to_float(work.replenishment_remaining_qty)
        materialized_qty = active_open_by_requirement.get(int(work.requirement_id), 0.0)
        launchable_qty = max(0.0, remaining_qty - materialized_qty)
        if shelf is not None:
            allowance = shelf_allowance.get(int(work.item_id), 0.0)
            launchable_qty = min(launchable_qty, allowance)
            shelf_allowance[int(work.item_id)] = max(0.0, allowance - launchable_qty)
        if launchable_qty <= 1e-9:
            continue
        pair_metadata = pair_by_item.get(int(work.item_id))
        selection_disabled_reason = (
            str(pair_metadata.get("selection_disabled_reason") or "")
            if pair_metadata
            else ""
        ) or None
        result.append(
            {
                "journal_row_key": f"work-item:{int(work.id)}",
                "work_item_id": int(work.id),
                "product_id": None,
                "order_id": None,
                "order_number": f"MRP-R-{int(requirement.id)}",
                "order_prodplan_number": f"MRP-R-{int(requirement.id)}",
                "order_date": None,
                "order_source": "mrp",
                "source": "mrp",
                "order_ref1c": None,
                "order_one_c_number": None,
                "line_number": None,
                "item_id": int(item.item_id),
                "item_code": str(item.item_code or ""),
                "item_name": str(item.item_name or ""),
                "item_article": str(item.item_article or ""),
                "optimal_batch": _to_float(item.optimal_batch) if item.optimal_batch is not None else None,
                "unit": unit_by_raw.get(str(item.unit or "").strip(), ""),
                "quantity": launchable_qty,
                "produced_qty": fulfilled_qty,
                "remaining_qty": remaining_qty,
                "status": "not_created",
                "coverage_status": "unknown",
                "coverage_label": "Недоступно",
                "issue_status": "not_requested",
                "material_coverage_status": None,
                "material_coverage_label": None,
                "material_coverage_calculated_at": None,
                "planned_start_date": _date_to_iso(planned_start),
                "planned_finish_date": _date_to_iso(planned_finish),
                **_forecast_payload(planned_finish, requirement.period_to),
                "opened_at": None,
                "workshop_id": workshop_id,
                "workshop_name": workshop_name,
                "stage_id": stage_id,
                "stage_name": stage_name,
                "spec_id": int(spec_id) if spec_id is not None else None,
                "spec_revision_hash": None,
                "issue_count": 0,
                "route_sheet_printed_at": None,
                "comment": "Расчёт MRP; исполнительный заказ ещё не создан",
                "source_run_id": int(work.run_id),
                **run_meta.get(int(work.run_id), {}),
                "source_planned_order_id": None,
                "source_mrp_requirement_id": int(work.requirement_id),
                "source_mrp_allocation_key": None,
                "available_actions": [] if selection_disabled_reason else ["materialize"],
                "selection_disabled_reason": selection_disabled_reason,
                "mrp_req_net_qty": required_qty,
                "mrp_req_covered_qty": fulfilled_qty,
                "mrp_req_remaining_qty": remaining_qty,
                "materialized_order_qty": materialized_qty,
                "launchable_qty": launchable_qty,
                "launch_source": LAUNCH_SOURCE_SHELF if shelf else LAUNCH_SOURCE_MRP,
                "shelf_warehouse_ref1c": shelf.warehouse_ref1c if shelf else None,
                "shelf_pull_qty": shelf.pull_qty if shelf else None,
                "shelf_materialized_qty": shelf.materialized_qty if shelf else None,
                "shelf_latest_start_date": _date_to_iso(shelf.latest_start_date) if shelf else None,
                "paint_weld_chain": None,
                "paint_weld_pair": pair_metadata,
            }
        )
    return result


# Terminal states that close a line: a remainder left un-produced here is never
# going to be made on this line, so its coverage must be released back to the
# requirement.
_TERMINAL_LINE_STATUSES = {"completed", "cancelled"}


def update_line_state(db: Session, product_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    product = db.query(ProductionProduct).filter(ProductionProduct.product_id == int(product_id)).first()
    if not product:
        raise ValueError("Строка заказа не найдена")

    state = _ensure_state(db, product)
    prev_status = str(state.status or "")
    if "status" in payload and payload.get("status"):
        status = str(payload.get("status")).strip()
        if status not in LINE_STATUSES:
            raise ValueError(f"Недопустимый статус: {status}")
        output = accepted_product_output(product)
        if status == "completed" and (
            _to_float(output.remaining_qty) > 1e-9
            or _to_float(output.produced_qty) <= 1e-9
        ):
            raise ValueError("Нельзя вручную завершить строку с невыпущенным остатком. Используйте выпуск.")
        state.status = status
        # First time the journal moves the line past 'shortage' / 'partial',
        # stamp opened_at вЂ” it acts as a workshop-side timestamp.
        if status in {"ready", "to_move", "assembled", "in_progress", "done", "produced_partial", "produced", "completed"} and not state.opened_at:
            state.opened_at = datetime.now(timezone.utc)
        # Closing the line with an un-produced remainder: release that remainder
        # from the requirement's coverage so the reconciliation job (and the
        # journal) can see the demand is no longer fully ordered.
        if status in _TERMINAL_LINE_STATUSES and prev_status not in _TERMINAL_LINE_STATUSES:
            released = _to_float(output.remaining_qty)
    if "issue_status" in payload and payload.get("issue_status"):
        issue_status = str(payload.get("issue_status")).strip()
        if issue_status not in ISSUE_STATUSES:
            raise ValueError(f"Недопустимый статус выдачи: {issue_status}")
        state.issue_status = issue_status
    if "workshop_id" in payload:
        state.workshop_id = int(payload["workshop_id"]) if payload.get("workshop_id") else None
        state.workshop_id_source = "manual" if state.workshop_id else None
        state.workshop_id_set_at = datetime.now(timezone.utc) if state.workshop_id else None
    if "planned_start_date" in payload:
        state.planned_start_date = _parse_date(payload.get("planned_start_date"))
    if "planned_finish_date" in payload:
        state.planned_finish_date = _parse_date(payload.get("planned_finish_date"))
    if "comment" in payload:
        state.comment = str(payload.get("comment") or "")

    db.commit()
    return {"status": "ok", "product_id": int(product_id)}


def _production_order_has_1c_link(db: Session, order: ProductionOrder) -> bool:
    if order.order_ref1c:
        return True
    return (
        db.query(SyncLink.source_id)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "production_order",
            SyncLink.source_id == int(order.order_id),
            SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
            SyncLink.status == "success",
        )
        .first()
        is not None
    )


def _material_issue_has_1c_link(db: Session, issue: ProductionMaterialIssue) -> bool:
    if issue.exported_ref1c:
        return True
    return (
        db.query(SyncLink.source_id)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "material_issue",
            SyncLink.source_id == int(issue.issue_id),
            SyncLink.target_entity == "Document_ПеремещениеЗапасов",
        )
        .first()
        is not None
    )


def cancel_local_order(db: Session, product_id: int) -> Dict[str, Any]:
    product = (
        db.query(ProductionProduct)
        .options(joinedload(ProductionProduct.order), joinedload(ProductionProduct.control_state))
        .filter(ProductionProduct.product_id == int(product_id))
        .one_or_none()
    )
    if product is None or product.order is None:
        raise ValueError("Строка заказа не найдена")
    order = product.order
    if _production_order_has_1c_link(db, order):
        raise ValueError("Заказ уже открыт в 1С, локальное удаление запрещено")

    products = (
        db.query(ProductionProduct)
        .options(joinedload(ProductionProduct.control_state))
        .filter(ProductionProduct.order_id == int(order.order_id))
        .all()
    )
    product_ids = [int(row.product_id) for row in products]
    issues = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id.in_(product_ids))
        .filter(ProductionMaterialIssue.direction == "issue")
        .all()
    )
    linked_issues = [issue for issue in issues if _material_issue_has_1c_link(db, issue)]
    if linked_issues:
        numbers = ", ".join(str(issue.document_number or issue.issue_id) for issue in linked_issues[:5])
        raise ValueError(f"Есть перемещения, уже открытые в 1С: {numbers}")

    deleted_issues = 0
    for issue in issues:
        for line in issue.lines or []:
            status = str(issue.status or "")
            if status == "posted":
                held_qty = _to_float(line.required_qty)
                warehouse = str(issue.warehouse_ref1c or "")
                location_kind = "workshop"
            else:
                held_qty = max(
                    0.0,
                    _to_float(line.required_qty) - _to_float(line.issued_qty),
                )
                warehouse = str(issue.source_warehouse_ref1c or issue.warehouse_ref1c or "")
                location_kind = "transit"

            if held_qty > 1e-9 and warehouse:
                append_material_issue_custody_event(
                    db,
                    issue=issue,
                    line=line,
                    delta_qty=-held_qty,
                    source_kind="terminal_release",
                    location_kind=location_kind,
                    warehouse_ref1c=warehouse,
                    source_ref1c=str(issue.source_warehouse_ref1c or ""),
                )

        db.delete(issue)
        deleted_issues += 1

    released_qty = 0.0
    for row in products:
        state = row.control_state or _ensure_state(db, row)
        if state.status != "cancelled":
            qty_to_release = _to_float(
                accepted_product_output(row).remaining_qty
            )
            if qty_to_release > 1e-9:
                released_qty += qty_to_release
            state.status = "cancelled"
        state.issue_status = "not_requested"

    order.deletion_mark = True
    db.commit()
    return {
        "status": "ok",
        "order_id": int(order.order_id),
        "product_ids": product_ids,
        "deleted_issues": deleted_issues,
        "released_qty": released_qty,
    }

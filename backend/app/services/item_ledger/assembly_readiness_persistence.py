"""Persist the generation-scoped readiness gate for the canonical drum."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import models
from app.services.mrp_stock_helpers import (
    apply_planning_warehouse_scope,
    planning_warehouse_scope,
)
from app.services.production_material_custody_projection import (
    load_material_custody_projection,
)
from app.services.replenishment import (
    REPLENISHMENT_FLOW_PRODUCTION,
    REPLENISHMENT_FLOW_PURCHASE,
    REPLENISHMENT_FLOW_REWORK,
    classify_replenishment_flow,
)

from .assembly_queue_snapshot import materialize_assembly_queue_lines
from .assembly_readiness_core import (
    FrozenBomEdge,
    ReadinessCurveLine,
    ReadinessSupply,
    ReplenishmentPolicy,
    allocate_readiness_curves,
)


STAGE = "assembly_readiness"
ALGORITHM_VERSION = "assembly-readiness/3-whole-piece-readiness-curve"
_NON_STOCK_TYPES = {"услуга", "работа", "операция"}


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def _signature(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def _physical_supplies(
    db: Session,
    generation_id: int,
) -> tuple[ReadinessSupply, ...] | None:
    if db.get(models.ProductionMaterialCustodyProjectionManifest, int(generation_id)) is None:
        return None
    custody = load_material_custody_projection(db, ledger_generation_id=int(generation_id))
    scope = planning_warehouse_scope(db)
    query = db.query(
        models.StockBin.item_id,
        models.StockBin.warehouse_ref1c,
        func.sum(models.StockBin.on_hand),
    ).filter(models.StockBin.ledger_generation_id == int(generation_id))
    query = apply_planning_warehouse_scope(
        query,
        scope,
        warehouse_column=models.StockBin.warehouse_ref1c,
        organization_column=models.StockBin.organization_ref,
    )
    result: list[ReadinessSupply] = []
    for item_id, warehouse_ref, physical_qty in query.group_by(
        models.StockBin.item_id, models.StockBin.warehouse_ref1c
    ).all():
        warehouse = str(warehouse_ref or "")
        free_qty = max(
            _d(physical_qty)
            - _d(custody.by_warehouse_item.get((warehouse, int(item_id)), 0)),
            Decimal("0"),
        )
        if free_qty <= 0:
            continue
        result.append(
            ReadinessSupply(
                source_key=f"stock:{warehouse}:{int(item_id)}",
                item_id=int(item_id),
                qty=free_qty,
                layer="now",
                warehouse_ref1c=warehouse,
                confidence="physical",
            )
        )
    return tuple(result)


def _future_supplies(db: Session, generation_id: int) -> tuple[ReadinessSupply, ...]:
    requirement_run = {
        int(requirement_id): int(run_id)
        for requirement_id, run_id in db.query(
            models.MrpRequirement.id, models.MrpRequirement.run_id
        ).all()
    }
    rows = (
        db.query(models.LedgerFutureSupply)
        .filter(
            models.LedgerFutureSupply.ledger_generation_id == int(generation_id),
            models.LedgerFutureSupply.evidence_status == "exact",
            models.LedgerFutureSupply.open_qty_at_cutoff > 0,
            models.LedgerFutureSupply.eta_date.is_not(None),
        )
        .order_by(models.LedgerFutureSupply.eta_date, models.LedgerFutureSupply.id)
        .all()
    )
    return tuple(
        ReadinessSupply(
            source_key=f"future:{int(row.id)}",
            item_id=int(row.item_id),
            qty=_d(row.open_qty_at_cutoff),
            layer="committed",
            warehouse_ref1c=str(row.destination_warehouse_ref1c or ""),
            available_date=row.eta_date,
            confidence="committed",
            bom_key=requirement_run.get(int(row.source_requirement_id))
            if row.source_requirement_id is not None
            else None,
        )
        for row in rows
    )


def _curve_inputs(
    db: Session,
    queue_rows: list[models.AssemblyQueueLine],
) -> tuple[
    tuple[ReadinessCurveLine, ...],
    tuple[FrozenBomEdge, ...],
    tuple[ReplenishmentPolicy, ...],
]:
    run_ids = sorted({int(row.planning_run_id) for row in queue_rows})
    runs = {
        int(row.run_id): row
        for row in db.query(models.PlanningRun)
        .filter(models.PlanningRun.run_id.in_(run_ids))
        .all()
    }
    component_rows = (
        db.query(models.MrpFreezeComponent)
        .filter(models.MrpFreezeComponent.run_id.in_(run_ids))
        .all()
        if run_ids
        else []
    )
    item_ids = {
        int(value)
        for row in component_rows
        for value in (row.parent_item_id, row.component_item_id)
    }
    item_meta = {
        int(row.item_id): row
        for row in db.query(models.Item).filter(models.Item.item_id.in_(item_ids)).all()
    }
    frozen_rows: list[models.MrpFreezeComponent] = []
    for component in component_rows:
        run = runs.get(int(component.run_id))
        if run is None or int(component.freeze_version) != int(run.active_freeze_version or 0):
            continue
        item = item_meta.get(int(component.component_item_id))
        if item is not None and (
            str(item.item_type or "").strip().lower() in _NON_STOCK_TYPES
        ):
            continue
        frozen_rows.append(component)

    edges = tuple(
        FrozenBomEdge(
            bom_key=int(row.run_id),
            parent_item_id=int(row.parent_item_id),
            component_item_id=int(row.component_item_id),
            norm_qty=_d(row.norm_qty_per_unit) * _d(row.unit_coef or 1),
        )
        for row in frozen_rows
        if _d(row.norm_qty_per_unit) * _d(row.unit_coef or 1) > 0
        and not (
            (item_meta.get(int(row.component_item_id)) is not None)
            and str(item_meta[int(row.component_item_id)].item_type or "").strip().lower()
            in _NON_STOCK_TYPES
        )
    )

    root_item_ids = sorted({int(row.item_id) for row in queue_rows})
    rate_rows = (
        db.query(models.AssemblyRate)
        .filter(models.AssemblyRate.item_id.in_(root_item_ids))
        .order_by(models.AssemblyRate.item_id, models.AssemblyRate.resource_id)
        .all()
        if root_item_ids else []
    )
    rates_by_item: dict[int, list[models.AssemblyRate]] = {}
    for rate in rate_rows:
        rates_by_item.setdefault(int(rate.item_id), []).append(rate)
    resource_ids = {int(rate.resource_id) for rate in rate_rows}
    bindings = {
        int(row.workshop_id): str(row.warehouse_ref1c or "")
        for row in db.query(models.WorkshopWarehouseBinding)
        .filter(models.WorkshopWarehouseBinding.workshop_id.in_(resource_ids))
        .all()
    } if resource_ids else {}

    lines_list: list[ReadinessCurveLine] = []
    for row in queue_rows:
        rates = rates_by_item.get(int(row.item_id), [])
        target = bindings.get(int(rates[0].resource_id), "") if len(rates) == 1 else ""
        lines_list.append(
            ReadinessCurveLine(
                queue_line_id=int(row.id),
                sort_key=str(row.sort_key),
                bom_key=int(row.planning_run_id),
                root_item_id=int(row.item_id),
                open_qty=_d(row.assembly_remaining_qty),
                target_warehouse_ref1c=target,
            )
        )

    spec_refs = {str(row.spec_ref or "") for row in frozen_rows if str(row.spec_ref or "")}
    numeric_spec_ids = {int(value) for value in spec_refs if value.isdigit()}
    specs = (
        db.query(models.Specification)
        .filter(or_(
            models.Specification.spec_ref1c.in_(spec_refs),
            models.Specification.spec_id.in_(numeric_spec_ids),
        ))
        .all()
        if spec_refs else []
    )
    spec_by_ref = {}
    for row in specs:
        if row.spec_ref1c:
            spec_by_ref[str(row.spec_ref1c)] = row
        spec_by_ref[str(row.spec_id)] = row
    kind_ids = {int(row.production_kind_id) for row in specs if row.production_kind_id is not None}
    route_rows = (
        db.query(models.ResourceProductionKind)
        .filter(models.ResourceProductionKind.production_kind_id.in_(kind_ids))
        .all()
        if kind_ids else []
    )
    resources_by_kind: dict[int, list[int]] = {}
    for row in route_rows:
        resources_by_kind.setdefault(int(row.production_kind_id), []).append(int(row.resource_id))
    route_resource_ids = {resource_id for values in resources_by_kind.values() for resource_id in values}
    resources = {
        int(row.resource_id): row
        for row in db.query(models.ProductionResource)
        .filter(models.ProductionResource.resource_id.in_(route_resource_ids))
        .all()
    } if route_resource_ids else {}
    route_bindings = {
        int(row.workshop_id): str(row.warehouse_ref1c or "")
        for row in db.query(models.WorkshopWarehouseBinding)
        .filter(models.WorkshopWarehouseBinding.workshop_id.in_(route_resource_ids))
        .all()
    } if route_resource_ids else {}
    kinds = {
        int(row.id): str(row.name or "")
        for row in db.query(models.ProductionKind).filter(models.ProductionKind.id.in_(kind_ids)).all()
    } if kind_ids else {}

    frozen_spec_by_parent: dict[tuple[int, int], tuple[str, str | None] | None] = {}
    ambiguous_frozen_specs: set[tuple[int, int]] = set()
    for row in frozen_rows:
        key = (int(row.run_id), int(row.parent_item_id))
        value = (str(row.spec_ref or ""), str(row.spec_version) if row.spec_version else None)
        if key in ambiguous_frozen_specs:
            continue
        previous = frozen_spec_by_parent.get(key)
        if previous is not None and previous != value:
            frozen_spec_by_parent[key] = None
            ambiguous_frozen_specs.add(key)
        else:
            frozen_spec_by_parent[key] = value

    policies: list[ReplenishmentPolicy] = []
    items_by_run: dict[int, set[int]] = {run_id: set() for run_id in run_ids}
    for row in frozen_rows:
        items_by_run[int(row.run_id)].update((int(row.parent_item_id), int(row.component_item_id)))
    for run_id, run_item_ids in items_by_run.items():
        for item_id in sorted(run_item_ids):
            item = item_meta.get(item_id)
            flow = classify_replenishment_flow(item.replenishment_method if item is not None else None)
            mode = {
                REPLENISHMENT_FLOW_PRODUCTION: "make",
                REPLENISHMENT_FLOW_PURCHASE: "buy",
                REPLENISHMENT_FLOW_REWORK: "rework",
            }.get(flow, "unavailable")
            lead_days = int(item.replenishment_time) if item is not None and item.replenishment_time is not None else None
            resource_id = None
            output_warehouse = ""
            route_kind = ""
            unavailable_reason = ""
            if mode in {"make", "rework"}:
                frozen_spec = frozen_spec_by_parent.get((run_id, item_id))
                if (run_id, item_id) in ambiguous_frozen_specs:
                    unavailable_reason = "FROZEN_SPEC_AMBIGUOUS"
                spec = spec_by_ref.get(frozen_spec[0]) if frozen_spec else None
                spec_is_current = bool(
                    spec is not None
                    and (not frozen_spec[1] or str(spec.content_hash or "") == frozen_spec[1])
                )
                routed = resources_by_kind.get(int(spec.production_kind_id), []) if spec_is_current and spec.production_kind_id is not None else []
                if len(routed) == 1:
                    resource_id = int(routed[0])
                    resource = resources.get(resource_id)
                    output_warehouse = route_bindings.get(resource_id, "")
                    lead_days = (
                        int(resource.buffer_days)
                        if resource is not None and resource.buffer_days is not None
                        else None
                    )
                    route_label = f"{resource.resource_name if resource is not None else ''} {kinds.get(int(spec.production_kind_id), '')}".lower()
                    route_kind = "kitting" if "комплект" in route_label or "склад сборки" in route_label else "production"
            policies.append(
                ReplenishmentPolicy(
                    bom_key=run_id,
                    item_id=item_id,
                    mode=mode,
                    lead_days=lead_days,
                    route_kind=route_kind,
                    resource_id=resource_id,
                    output_warehouse_ref1c=output_warehouse,
                    unavailable_reason=unavailable_reason,
                )
            )
    return tuple(lines_list), edges, tuple(policies)


def materialize_assembly_readiness(
    db: Session,
    ledger_generation_id: int,
) -> dict[str, Any]:
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise ValueError(f"LedgerGeneration {ledger_generation_id} not found")
    if str(generation.status) != "building":
        raise ValueError("assembly readiness requires a BUILDING generation")

    existing = (
        db.query(models.AssemblyReadiness)
        .filter(models.AssemblyReadiness.ledger_generation_id == int(generation.id))
        .order_by(models.AssemblyReadiness.assembly_queue_line_id)
        .all()
    )
    batch_key = f"g{int(generation.id)}:{STAGE}:{ALGORITHM_VERSION}"
    batch = (
        db.query(models.LedgerBuildBatch)
        .filter(
            models.LedgerBuildBatch.ledger_generation_id == int(generation.id),
            models.LedgerBuildBatch.stage == STAGE,
            models.LedgerBuildBatch.batch_key == batch_key,
        )
        .one_or_none()
    )
    if batch is not None:
        if str(batch.status) != "completed":
            raise ValueError("partial assembly readiness checkpoint exists")
        if int(batch.metrics.get("rows", -1)) != len(existing):
            raise ValueError("assembly readiness checkpoint row count mismatch")
        return {"ledger_generation_id": int(generation.id), **dict(batch.metrics or {})}
    if existing:
        raise ValueError("partial assembly readiness checkpoint exists")

    queue_rows = [
        row
        for row in materialize_assembly_queue_lines(db, int(generation.id))
        if _d(row.assembly_remaining_qty) > 0
    ]
    lines, edges, policies = _curve_inputs(db, queue_rows)
    physical = _physical_supplies(db, int(generation.id))
    results = allocate_readiness_curves(
        lines,
        edges,
        tuple(physical or ()) + _future_supplies(db, int(generation.id)),
        policies,
        as_of=generation.cutoff.date(),
        global_unavailable_reasons=("CUSTODY_SNAPSHOT_MISSING",) if physical is None else (),
    )
    item_ids = {
        int(action.item_id)
        for result in results
        for point in result.points
        for action in point.actions
    } | {
        int(blocker.item_id)
        for result in results
        for blocker in result.blockers
    }
    labels = {
        int(row.item_id): row
        for row in db.query(models.Item).filter(models.Item.item_id.in_(item_ids)).all()
    }
    status_counts: dict[str, int] = {}
    for result in results:
        def action_payload(action):
            item = labels.get(int(action.item_id))
            return {
                "action_kind": action.action_kind,
                "item_id": int(action.item_id),
                "item_code": str(item.item_code or "") if item is not None else "",
                "item_article": str(item.item_article or "") if item is not None else "",
                "item_name": str(item.item_name or "") if item is not None else "",
                "qty": str(action.qty),
                "available_date": action.available_date.isoformat() if action.available_date else None,
                "confidence": action.confidence,
                "source_key": action.source_key,
                "source_warehouse_ref1c": action.source_warehouse_ref1c,
                "destination_warehouse_ref1c": action.destination_warehouse_ref1c,
                "resource_id": action.resource_id,
                "path": list(action.path),
            }
        curve = []
        for point in result.points:
            curve.append({
                "horizon": point.horizon,
                "cumulative_qty": str(point.cumulative_qty),
                "available_date": point.available_date.isoformat() if point.available_date else None,
                "actions": [action_payload(action) for action in point.actions],
            })
        launch_point = result.points[-1]
        manifest = [action_payload(action) for action in launch_point.actions]
        blocker_manifest = []
        for blocker in result.blockers:
            item = labels.get(int(blocker.item_id))
            blocker_manifest.append({
                "item_id": int(blocker.item_id),
                "item_code": str(item.item_code or "") if item is not None else "",
                "item_article": str(item.item_article or "") if item is not None else "",
                "item_name": str(item.item_name or "") if item is not None else "",
                "required_qty": str(blocker.required_qty),
                "available_qty": str(blocker.available_qty),
                "shortage_qty": str(blocker.shortage_qty),
                "reason": blocker.reason,
                "destination_warehouse_ref1c": blocker.destination_warehouse_ref1c,
                "path": list(blocker.path),
            })
        blocker_manifest.extend(
            {"reason": reason}
            for reason in result.unavailable_reasons
        )
        by_horizon = {point.horizon: point for point in result.points}
        evidence = {
            "queue_line_id": int(result.queue_line_id),
            "status": result.status,
            "open_qty": str(result.open_qty),
            "curve": curve,
            "actions": manifest,
            "blockers": blocker_manifest,
            "unavailable_reasons": list(result.unavailable_reasons),
        }
        db.add(
            models.AssemblyReadiness(
                ledger_generation_id=int(generation.id),
                assembly_queue_line_id=int(result.queue_line_id),
                status=result.status,
                open_qty=result.open_qty,
                ready_qty=by_horizon["now"].cumulative_qty,
                transferable_qty=by_horizon["transfer"].cumulative_qty,
                kitting_qty=by_horizon["kitting"].cumulative_qty,
                committed_qty=by_horizon["committed"].cumulative_qty,
                launchable_qty=by_horizon["launch"].cumulative_qty,
                readiness_date=launch_point.available_date,
                readiness_curve=curve,
                action_manifest=manifest,
                unavailable_reasons=list(result.unavailable_reasons),
                blocker_count=len(blocker_manifest),
                blocking_manifest=blocker_manifest,
                evidence_signature=_signature(evidence),
            )
        )
        status_counts[result.status] = status_counts.get(result.status, 0) + 1

    metrics = {
        "rows": len(results),
        "ready_rows": status_counts.get("ready", 0),
        "recoverable_rows": status_counts.get("recoverable", 0),
        "partial_rows": status_counts.get("partial", 0),
        "blocked_rows": status_counts.get("blocked", 0),
        "unavailable_rows": status_counts.get("unavailable", 0),
        "ready_qty": str(sum((row.points[0].cumulative_qty for row in results), Decimal("0"))),
        "launchable_qty": str(sum((row.points[-1].cumulative_qty for row in results), Decimal("0"))),
    }
    db.add(
        models.LedgerBuildBatch(
            ledger_generation_id=int(generation.id),
            stage=STAGE,
            batch_key=batch_key,
            status="completed",
            algorithm_version=ALGORITHM_VERSION,
            metrics=metrics,
            completed_at=datetime.now(timezone.utc),
        )
    )
    db.flush()
    return {"ledger_generation_id": int(generation.id), **metrics}

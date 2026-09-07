"""Persist the generation-scoped readiness gate for the canonical drum."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.services.mrp_stock_helpers import (
    apply_planning_warehouse_scope,
    planning_warehouse_scope,
)
from app.services.production_material_custody_projection import (
    load_material_custody_projection,
)
from app.services.production_control_material_issues import _source_warehouse_options

from .assembly_queue_snapshot import materialize_assembly_queue_lines
from .assembly_readiness_core import (
    FrozenBomEdge,
    ReadinessCurveLine,
    ReadinessSupply,
    ReplenishmentPolicy,
    allocate_readiness_curves,
)


STAGE = "assembly_readiness"
ALGORITHM_VERSION = "assembly-readiness/10-addressed-stock-transfers"


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def _signature(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def _physical_supplies(
    db: Session,
    generation_id: int,
    queue_rows: list[models.AssemblyQueueLine],
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
    # Use the same eligible source candidates as material issuing. Readiness
    # proposes addressed transfers; it does not issue documents or require an
    # operator to pick one warehouse before showing that material is available.
    # The destination belongs to the frozen consuming node.
    run_ids = sorted({int(row.planning_run_id) for row in queue_rows})
    consuming_nodes = (
        db.query(models.MrpFreezeComponent.component_item_id,
                 models.MrpFreezeBomNode.material_warehouse_ref1c)
        .join(models.PlanningRun,
              models.PlanningRun.run_id == models.MrpFreezeComponent.run_id)
        .join(models.MrpFreezeBomNode,
              (models.MrpFreezeBomNode.run_id == models.MrpFreezeComponent.run_id)
              & (models.MrpFreezeBomNode.freeze_version == models.MrpFreezeComponent.freeze_version)
              & (models.MrpFreezeBomNode.root_item_id == models.MrpFreezeComponent.root_item_id)
              & (models.MrpFreezeBomNode.item_id == models.MrpFreezeComponent.parent_item_id)
              & (models.MrpFreezeBomNode.spec_ref == models.MrpFreezeComponent.spec_ref))
        .filter(models.MrpFreezeComponent.run_id.in_(run_ids or [0]),
                models.MrpFreezeComponent.freeze_version == models.PlanningRun.active_freeze_version)
        .distinct()
        .all()
    )
    destinations_by_item: dict[int, set[str]] = {}
    for item_id, destination in consuming_nodes:
        if destination:
            destinations_by_item.setdefault(int(item_id), set()).add(str(destination))
    source_options = _source_warehouse_options(
        db, sorted(destinations_by_item), ledger_generation_id=int(generation_id),
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
                source_kind="physical_stock",
                source_ref=warehouse,
                routable_destination_warehouse_refs=tuple(sorted(
                    destination
                    for destination in destinations_by_item.get(int(item_id), ())
                    if warehouse != destination and warehouse in {
                        str(option["ref1c"])
                        for option in source_options.get(int(item_id), ())
                    }
                )),
            )
        )

    queue_by_run_root: dict[tuple[int, int], list[int]] = {}
    for queue_row in queue_rows:
        queue_by_run_root.setdefault(
            (int(queue_row.planning_run_id), int(queue_row.item_id)), []
        ).append(int(queue_row.id))
    run_ids = sorted({int(row.planning_run_id) for row in queue_rows})
    active_version_by_run = {
        int(run.run_id): int(run.active_freeze_version or 0)
        for run in db.query(models.PlanningRun)
        .filter(models.PlanningRun.run_id.in_(run_ids or [0]))
        .all()
    }
    roots_by_run_item: dict[tuple[int, int], set[int]] = {}
    nodes_by_run_item: dict[
        tuple[int, int], list[models.MrpFreezeBomNode]
    ] = {}
    for node in (
        db.query(models.MrpFreezeBomNode)
        .filter(models.MrpFreezeBomNode.run_id.in_(run_ids or [0]))
        .all()
    ):
        if int(node.freeze_version) != active_version_by_run.get(
            int(node.run_id), -1
        ):
            continue
        roots_by_run_item.setdefault(
            (int(node.run_id), int(node.item_id)), set()
        ).add(int(node.root_item_id))
        nodes_by_run_item.setdefault(
            (int(node.run_id), int(node.item_id)), []
        ).append(node)
    custody_rows = (
        db.query(
            models.ProductionMaterialCustodyProjection,
            models.ProductionProduct.item_id.label("product_item_id"),
            models.MrpRequirement.run_id.label("run_id"),
            models.ProductionOrder.order_number.label("source_number"),
        )
        .join(
            models.ProductionProduct,
            models.ProductionMaterialCustodyProjection.product_id
            == models.ProductionProduct.product_id,
        )
        .outerjoin(
            models.MrpRequirement,
            models.ProductionProduct.source_mrp_requirement_id
            == models.MrpRequirement.id,
        )
        .join(
            models.ProductionOrder,
            models.ProductionProduct.order_id == models.ProductionOrder.order_id,
        )
        .filter(
            models.ProductionMaterialCustodyProjection.ledger_generation_id
            == int(generation_id),
            models.ProductionMaterialCustodyProjection.location_kind.in_(
                ("workshop", "transit")
            ),
            models.ProductionMaterialCustodyProjection.reserved_qty > 0,
        )
        .order_by(models.ProductionMaterialCustodyProjection.id)
        .all()
    )
    for custody_row, product_item_id, run_id, source_number in custody_rows:
        if run_id is None:
            continue
        root_item_ids = tuple(
            sorted(
                roots_by_run_item.get(
                    (int(run_id), int(product_item_id)), set()
                )
            )
        )
        if not root_item_ids:
            continue
        route_destinations = {
            str(node.material_warehouse_ref1c or "").strip()
            for node in nodes_by_run_item.get(
                (int(run_id), int(product_item_id)), []
            )
            if str(node.material_warehouse_ref1c or "").strip()
        }
        is_transit = str(custody_row.location_kind) == "transit"
        if is_transit and len(route_destinations) != 1:
            # A transit reservation is a promise only when the frozen route
            # names one exact point of use.  The live issue document is not an
            # allowed candidate input and cannot resolve an ambiguous freeze.
            continue
        transfer_destination = (
            next(iter(route_destinations)) if is_transit else ""
        )
        candidates = [
            queue_line_id
            for root_item_id in root_item_ids
            for queue_line_id in queue_by_run_root.get(
                (int(run_id), int(root_item_id)), []
            )
        ]
        result.append(
            ReadinessSupply(
                source_key=f"custody:{int(custody_row.id)}",
                item_id=int(custody_row.component_item_id),
                qty=_d(custody_row.reserved_qty),
                layer="transfer" if is_transit else "now",
                warehouse_ref1c=str(custody_row.warehouse_ref1c or ""),
                confidence="custody",
                bom_key=int(run_id),
                queue_line_id=(int(candidates[0]) if len(candidates) == 1 else None),
                root_item_ids=root_item_ids,
                custody_owner_item_id=int(product_item_id),
                transfer_destination_warehouse_ref1c=transfer_destination,
                source_kind=(
                    "custody_transit" if is_transit else "custody_workshop"
                ),
                source_ref=str(source_number or int(custody_row.product_id)),
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
            source_kind=str(row.supply_kind or ""),
            source_ref=str(
                row.source_ref or row.source_local_id or row.id
            ),
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
    node_rows = (
        db.query(models.MrpFreezeBomNode)
        .filter(models.MrpFreezeBomNode.run_id.in_(run_ids))
        .all()
        if run_ids
        else []
    )
    active_version_by_run = {
        run_id: int(run.active_freeze_version or 0) for run_id, run in runs.items()
    }
    frozen_rows: list[models.MrpFreezeComponent] = []
    for component in component_rows:
        if int(component.freeze_version) != active_version_by_run.get(
            int(component.run_id), -1
        ):
            continue
        frozen_rows.append(component)
    frozen_nodes = [
        row
        for row in node_rows
        if int(row.freeze_version)
        == active_version_by_run.get(int(row.run_id), -1)
    ]

    issues_by_scope: dict[tuple[int, int], set[str]] = {}
    legacy_runs: set[int] = set()
    edges_list: list[FrozenBomEdge] = []
    for row in frozen_rows:
        if row.root_item_id is None:
            legacy_runs.add(int(row.run_id))
            continue
        scope = (int(row.run_id), int(row.root_item_id))
        norm = _d(row.norm_qty_per_unit) * _d(row.unit_coef or 1)
        if norm <= 0:
            issues_by_scope.setdefault(scope, set()).add("INVALID_COMPONENT_NORM")
            continue
        edges_list.append(
            FrozenBomEdge(
                bom_key=int(row.run_id),
                root_item_id=int(row.root_item_id),
                parent_item_id=int(row.parent_item_id),
                component_item_id=int(row.component_item_id),
                parent_spec_ref=str(row.spec_ref or ""),
                child_spec_ref=str(row.child_spec_ref or ""),
                norm_qty=norm,
            )
        )

    policies = tuple(
        ReplenishmentPolicy(
            bom_key=int(row.run_id),
            root_item_id=int(row.root_item_id),
            item_id=int(row.item_id),
            spec_ref=str(row.spec_ref or ""),
            mode=str(row.replenishment_mode or "unavailable"),
            lead_days=(
                int(row.replenishment_time_days)
                if row.replenishment_time_days is not None
                else None
            ),
            route_kind="kitting" if bool(row.is_kitting) else "production",
            resource_id=int(row.resource_id) if row.resource_id is not None else None,
            material_warehouse_ref1c=str(row.material_warehouse_ref1c or ""),
            output_warehouse_ref1c=str(row.output_warehouse_ref1c or ""),
            unavailable_reason=(
                "NON_STOCK_ITEM"
                if not bool(row.is_stock_item)
                else str(row.route_reason or "")
            ),
        )
        for row in frozen_nodes
    )

    root_nodes: dict[tuple[int, int], list[models.MrpFreezeBomNode]] = {}
    for row in frozen_nodes:
        if int(row.item_id) == int(row.root_item_id):
            root_nodes.setdefault((int(row.run_id), int(row.root_item_id)), []).append(row)

    lines_list: list[ReadinessCurveLine] = []
    for row in queue_rows:
        scope = (int(row.planning_run_id), int(row.item_id))
        issues = set(issues_by_scope.get(scope, set()))
        if int(row.planning_run_id) in legacy_runs:
            issues.add("FROZEN_BOM_SCHEMA_OUTDATED")
        candidates = root_nodes.get(scope, [])
        if not candidates:
            issues.add("FROZEN_BOM_NODE_MISSING")
            root_spec_ref = ""
            target = ""
        elif len(candidates) > 1:
            issues.add("FROZEN_ROOT_SPEC_AMBIGUOUS")
            root_spec_ref = ""
            target = ""
        else:
            root = candidates[0]
            root_spec_ref = str(root.spec_ref or "")
            target = str(root.material_warehouse_ref1c or "")
            if str(root.route_reason or ""):
                issues.add(str(root.route_reason))
        lines_list.append(
            ReadinessCurveLine(
                queue_line_id=int(row.id),
                sort_key=str(row.sort_key),
                bom_key=int(row.planning_run_id),
                root_item_id=int(row.item_id),
                root_spec_ref=root_spec_ref,
                open_qty=_d(row.assembly_remaining_qty),
                target_warehouse_ref1c=target,
                unavailable_reasons=tuple(sorted(issues)),
            )
        )

    return tuple(lines_list), tuple(edges_list), policies


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
    physical = _physical_supplies(db, int(generation.id), queue_rows)
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
        for action in (*point.actions, *point.required_actions)
    } | {
        int(blocker.item_id)
        for result in results
        for point in result.points
        for blocker in point.blockers
    }
    labels = {
        int(row.item_id): row
        for row in db.query(models.Item).filter(models.Item.item_id.in_(item_ids)).all()
    }
    warehouse_refs = {
        str(value).strip()
        for result in results
        for point in result.points
        for action in (*point.actions, *point.required_actions)
        for value in (
            action.source_warehouse_ref1c,
            action.destination_warehouse_ref1c,
        )
        if str(value or "").strip()
    } | {
        str(value).strip()
        for result in results
        for point in result.points
        for blocker in point.blockers
        for value in (
            blocker.destination_warehouse_ref1c,
            *(
                ref
                for source in blocker.coverage_sources
                for ref in (
                    source.warehouse_ref1c,
                    source.destination_warehouse_ref1c,
                )
            ),
        )
        if str(value or "").strip()
    }
    warehouse_names = {
        str(row.warehouse_ref1c): str(row.warehouse_name or "")
        for row in db.query(models.StockWarehouse)
        .filter(models.StockWarehouse.warehouse_ref1c.in_(warehouse_refs or {""}))
        .all()
    }
    resource_ids = {
        int(action.resource_id)
        for result in results
        for point in result.points
        for action in (*point.actions, *point.required_actions)
        if action.resource_id is not None
    }
    resource_names = {
        int(row.resource_id): str(row.resource_name or "")
        for row in db.query(models.ProductionResource)
        .filter(models.ProductionResource.resource_id.in_(resource_ids or {0}))
        .all()
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
                "source_warehouse_name": warehouse_names.get(
                    str(action.source_warehouse_ref1c or ""), ""
                ),
                "destination_warehouse_ref1c": action.destination_warehouse_ref1c,
                "destination_warehouse_name": warehouse_names.get(
                    str(action.destination_warehouse_ref1c or ""), ""
                ),
                "resource_id": action.resource_id,
                "resource_name": (
                    resource_names.get(int(action.resource_id), "")
                    if action.resource_id is not None
                    else ""
                ),
                "path": list(action.path),
            }

        def blocker_payload(blocker):
            item = labels.get(int(blocker.item_id))
            return {
                "item_id": int(blocker.item_id),
                "item_code": str(item.item_code or "") if item is not None else "",
                "item_article": str(item.item_article or "") if item is not None else "",
                "item_name": str(item.item_name or "") if item is not None else "",
                "required_qty": str(blocker.required_qty),
                "available_qty": str(blocker.available_qty),
                "shortage_qty": str(blocker.shortage_qty),
                "reason": blocker.reason,
                "destination_warehouse_ref1c": blocker.destination_warehouse_ref1c,
                "destination_warehouse_name": warehouse_names.get(
                    str(blocker.destination_warehouse_ref1c or ""), ""
                ),
                "path": list(blocker.path),
                "point_of_use_qty": str(blocker.point_of_use_qty),
                "custody_qty": str(blocker.custody_qty),
                "transit_qty": str(blocker.transit_qty),
                "wip_qty": str(blocker.wip_qty),
                "supplier_qty": str(blocker.supplier_qty),
                "other_stock_qty": str(blocker.other_stock_qty),
                "coverage_sources": [
                    {
                        "coverage_kind": source.coverage_kind,
                        "qty": str(source.qty),
                        "source_key": source.source_key,
                        "warehouse_ref1c": source.warehouse_ref1c,
                        "warehouse_name": warehouse_names.get(
                            str(source.warehouse_ref1c or ""), ""
                        ),
                        "destination_warehouse_ref1c": (
                            source.destination_warehouse_ref1c
                        ),
                        "destination_warehouse_name": warehouse_names.get(
                            str(source.destination_warehouse_ref1c or ""), ""
                        ),
                        "available_date": (
                            source.available_date.isoformat()
                            if source.available_date
                            else None
                        ),
                        "confidence": source.confidence,
                        "source_kind": source.source_kind,
                        "source_ref": source.source_ref,
                    }
                    for source in blocker.coverage_sources
                ],
            }

        curve = []
        for point in result.points:
            curve.append({
                "horizon": point.horizon,
                "cumulative_qty": str(point.cumulative_qty),
                "available_date": point.available_date.isoformat() if point.available_date else None,
                "actions": [action_payload(action) for action in point.actions],
                "required_actions": [
                    action_payload(action) for action in point.required_actions
                ],
                "blockers": [
                    blocker_payload(blocker) for blocker in point.blockers
                ],
            })
        launch_point = result.points[-1]
        manifest = [
            action_payload(action)
            for action in (*launch_point.actions, *launch_point.required_actions)
        ]
        blocker_manifest = [
            blocker_payload(blocker) for blocker in launch_point.blockers
        ]
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

"""Stable current execution owner for the R8 queue/readiness/drum/shelf contour.

Generation-scoped builders remain useful as staging evidence.  Only this
module publishes the compact current rows consumed by current execution reads.
Technical generation/cutoff changes are provenance and do not create a
business change when the saved result is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app import models


class CurrentExecutionUnavailable(ValueError):
    """The current execution result is absent, stale, incomplete or unsafe."""


@dataclass(frozen=True)
class CurrentExecutionPublishResult:
    changed_rows: int
    closed_rows: int
    idempotent: bool


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _hash(payload: Any) -> str:
    encoded = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _semantic_payload(row: dict[str, Any], existing_manual: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = dict(row.get("payload") or {})
    supplied_manual = row.get("manual_input")
    manual = dict(existing_manual or {}) if supplied_manual is None else dict(supplied_manual or {})
    return payload, manual


def order_execution_queue(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the canonical oldest-first order with a complete deterministic tie-break."""

    normalized = [dict(row) for row in rows]
    for row in normalized:
        identity = str(row.get("business_identity") or "").strip()
        if not identity:
            raise CurrentExecutionUnavailable("queue row lacks business identity")
        payload = row.get("payload") or {}
        qty = Decimal(str(payload.get("assembly_remaining_qty") or "0"))
        if qty < 0:
            raise CurrentExecutionUnavailable(f"negative queue remainder for {identity}")

    def key(row: dict[str, Any]) -> tuple[str, str, int, int, str]:
        payload = row.get("payload") or {}
        return (
            str(payload.get("period_from") or ""),
            str(payload.get("period_to") or ""),
            int(payload.get("plan_id") or 0),
            int(payload.get("plan_line_id") or 0),
            str(row.get("business_identity") or ""),
        )

    return sorted(normalized, key=key)


def _source_generation(db: Session, source_generation_id: int | None) -> models.LedgerGeneration | None:
    if source_generation_id is None:
        return None
    generation = db.get(models.LedgerGeneration, int(source_generation_id))
    if generation is None or str(generation.status or "") != "accepted":
        raise CurrentExecutionUnavailable("source generation is not accepted")
    return generation


def publish_current_execution_scope(
    db: Session,
    *,
    source_revision: str,
    scope_key: str,
    rows: Iterable[dict[str, Any]],
    source_generation_id: int | None = None,
    result_ready: bool = True,
    complete_scope: bool = True,
) -> CurrentExecutionPublishResult:
    """Publish one complete current scope with stable IDs and no-op semantics."""

    revision = str(source_revision or "").strip()
    scope = str(scope_key or "").strip()
    if not revision:
        raise CurrentExecutionUnavailable("source revision is required")
    if not scope:
        raise CurrentExecutionUnavailable("execution scope is required")
    if not result_ready:
        raise CurrentExecutionUnavailable("execution result is not ready")
    _source_generation(db, source_generation_id)

    incoming: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any], str]] = {}
    for raw in rows:
        row = dict(raw)
        entity_kind = str(row.get("entity_kind") or "").strip()
        identity = str(row.get("business_identity") or "").strip()
        row_scope = str(row.get("scope_key") or scope).strip()
        if not entity_kind or not identity:
            raise CurrentExecutionUnavailable("current execution row lacks stable identity")
        if row_scope != scope:
            raise CurrentExecutionUnavailable("current execution row is outside complete scope")
        payload, manual = _semantic_payload(row)
        key = (entity_kind, identity)
        if key in incoming:
            raise CurrentExecutionUnavailable(f"duplicate current execution identity {entity_kind}:{identity}")
        incoming[key] = (payload, manual, _hash({"payload": payload, "manual_input": manual}))

    entity_kinds = {kind for kind, _identity in incoming}
    existing_query = db.query(models.CurrentExecutionRow).filter(
        models.CurrentExecutionRow.scope_key == scope,
    )
    if entity_kinds:
        existing_query = existing_query.filter(models.CurrentExecutionRow.entity_kind.in_(sorted(entity_kinds)))
    existing = existing_query.with_for_update().all()
    existing_by_key = {(str(row.entity_kind), str(row.business_identity)): row for row in existing}

    changed = 0
    closed = 0
    for key, (payload, manual, content_hash) in incoming.items():
        row = existing_by_key.get(key)
        if row is not None and (
            str(row.result_status) == "accepted"
            and bool(row.result_ready)
            and str(row.content_hash) == content_hash
        ):
            continue
        if row is None:
            row = models.CurrentExecutionRow(
                entity_kind=key[0],
                business_identity=key[1],
                scope_key=scope,
                source_revision=revision,
                source_generation_id=source_generation_id,
                result_status="accepted",
                result_ready=True,
                content_hash=content_hash,
                payload=_jsonable(payload),
                manual_input=_jsonable(manual),
            )
            db.add(row)
            db.flush()
            operation = "insert"
            before = None
        else:
            before = dict(row.payload or {})
            row.source_revision = revision
            row.source_generation_id = source_generation_id
            row.result_status = "accepted"
            row.result_ready = True
            row.content_hash = content_hash
            row.payload = _jsonable(payload)
            row.manual_input = _jsonable(manual)
            operation = "update"
        db.add(models.CurrentExecutionChange(
            current_row_id=int(row.id),
            entity_kind=key[0],
            business_identity=key[1],
            scope_key=scope,
            source_revision=revision,
            operation=operation,
            reason="recalculation",
            before_payload=before,
            after_payload=_jsonable(payload),
        ))
        changed += 1

    if complete_scope:
        incoming_keys = set(incoming)
        for row in existing:
            key = (str(row.entity_kind), str(row.business_identity))
            if key in incoming_keys or str(row.result_status) == "closed":
                continue
            before = dict(row.payload or {})
            row.result_status = "closed"
            row.result_ready = False
            row.source_revision = revision
            db.add(models.CurrentExecutionChange(
                current_row_id=int(row.id),
                entity_kind=str(row.entity_kind),
                business_identity=str(row.business_identity),
                scope_key=scope,
                source_revision=revision,
                operation="close",
                reason="scope_rebuild",
                before_payload=before,
                after_payload=None,
            ))
            closed += 1
    db.flush()
    return CurrentExecutionPublishResult(
        changed_rows=changed,
        closed_rows=closed,
        idempotent=(changed == 0 and closed == 0),
    )


def load_current_execution_rows(
    db: Session,
    *,
    entity_kind: str,
    scope_key: str | None = None,
) -> list[models.CurrentExecutionRow]:
    query = db.query(models.CurrentExecutionRow).filter(
        models.CurrentExecutionRow.entity_kind == str(entity_kind),
        models.CurrentExecutionRow.result_status == "accepted",
        models.CurrentExecutionRow.result_ready.is_(True),
    )
    if scope_key is not None:
        query = query.filter(models.CurrentExecutionRow.scope_key == str(scope_key))
    return query.order_by(
        models.CurrentExecutionRow.business_identity.asc(),
        models.CurrentExecutionRow.id.asc(),
    ).all()


def publish_current_execution_from_generation(
    db: Session,
    generation_id: int,
) -> dict[str, CurrentExecutionPublishResult]:
    """Promote the four staged R8 contours at the accepted publication boundary."""

    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or str(generation.status or "") != "accepted":
        raise CurrentExecutionUnavailable("current execution requires an accepted generation")
    revision = f"accepted:g{int(generation.id)}"

    queue_rows = db.query(models.AssemblyQueueLine).filter(
        models.AssemblyQueueLine.ledger_generation_id == int(generation.id),
        models.AssemblyQueueLine.line_status == "open",
        models.AssemblyQueueLine.assembly_remaining_qty > 0,
    ).order_by(
        models.AssemblyQueueLine.sort_key.asc(),
        models.AssemblyQueueLine.plan_line_id.asc(),
    ).all()
    queue_payload = []
    for row in queue_rows:
        queue_payload.append({
            "entity_kind": "assembly_queue",
            "business_identity": f"plan-line:{int(row.plan_line_id)}",
            "scope_key": "assembly:all-live-plans",
            "payload": {
                "plan_id": int(row.plan_id),
                "plan_line_id": int(row.plan_line_id),
                "run_id": int(row.planning_run_id),
                "item_id": int(row.item_id),
                "bucket_date": row.bucket_date.isoformat(),
                "period_from": row.period_from.isoformat(),
                "period_to": row.period_to.isoformat(),
                "planned_output_qty": str(row.planned_output_qty),
                "accepted_plan_output_qty": str(row.accepted_plan_output_qty),
                "assembly_remaining_qty": str(row.assembly_remaining_qty),
                "eligible_from": row.eligible_from.isoformat() if row.eligible_from else None,
                "original_priority": list(row.original_priority or []),
                "sort_key": str(row.sort_key),
            },
        })
    queue_result = publish_current_execution_scope(
        db,
        source_revision=revision,
        source_generation_id=int(generation.id),
        scope_key="assembly:all-live-plans",
        rows=queue_payload,
    )

    readiness_payload = []
    readiness_rows = db.query(models.AssemblyReadiness, models.AssemblyQueueLine).join(
        models.AssemblyQueueLine,
        models.AssemblyQueueLine.id == models.AssemblyReadiness.assembly_queue_line_id,
    ).filter(
        models.AssemblyReadiness.ledger_generation_id == int(generation.id),
    ).all()
    for readiness, queue in readiness_rows:
        readiness_payload.append({
            "entity_kind": "assembly_readiness",
            "business_identity": f"plan-line:{int(queue.plan_line_id)}",
            "scope_key": "assembly:all-live-plans",
            "payload": {
                "queue_line_id": int(readiness.assembly_queue_line_id),
                "plan_id": int(queue.plan_id),
                "plan_line_id": int(queue.plan_line_id),
                "run_id": int(queue.planning_run_id),
                "item_id": int(queue.item_id),
                "status": str(readiness.status),
                "open_qty": str(readiness.open_qty),
                "ready_qty": str(readiness.ready_qty),
                "transferable_qty": str(readiness.transferable_qty),
                "kitting_qty": str(readiness.kitting_qty),
                "committed_qty": str(readiness.committed_qty),
                "launchable_qty": str(readiness.launchable_qty),
                "readiness_date": readiness.readiness_date.isoformat() if readiness.readiness_date else None,
                "readiness_curve": list(readiness.readiness_curve or []),
                "action_manifest": list(readiness.action_manifest or []),
                "unavailable_reasons": list(readiness.unavailable_reasons or []),
                "blocker_count": int(readiness.blocker_count),
                "blocking_manifest": list(readiness.blocking_manifest or []),
                "original_priority": list(queue.original_priority or []),
            },
        })
    readiness_result = publish_current_execution_scope(
        db,
        source_revision=revision,
        source_generation_id=int(generation.id),
        scope_key="assembly:all-live-plans",
        rows=readiness_payload,
    )

    drum_result = CurrentExecutionPublishResult(0, 0, True)
    schedule = db.query(models.DrumSchedule).filter(
        models.DrumSchedule.ledger_generation_id == int(generation.id),
    ).one_or_none()
    if schedule is not None:
        drum_rows = [{
            "entity_kind": "drum_schedule",
            "business_identity": "drum:all-live-plans",
            "scope_key": "drum:all-live-plans",
            "payload": {
                "schedule_from": schedule.schedule_from.isoformat(),
                "schedule_to": schedule.schedule_to.isoformat(),
                "working_days": list(schedule.working_days or []),
                "resource_horizon_ends": dict(schedule.resource_horizon_ends or {}),
                "resource_daily_capacities": dict(schedule.resource_daily_capacities or {}),
                "metrics": dict(schedule.metrics or {}),
            },
        }]
        drum_result = publish_current_execution_scope(
            db,
            source_revision=revision,
            source_generation_id=int(generation.id),
            scope_key="drum:all-live-plans",
            rows=drum_rows,
        )

    shelf_result = CurrentExecutionPublishResult(0, 0, True)
    shelf_rows = []
    for row in db.query(models.ShelfProjection).filter(
        models.ShelfProjection.ledger_generation_id == int(generation.id),
    ).all():
        shelf_rows.append({
            "entity_kind": "shelf_projection",
            "business_identity": f"shelf-policy:{int(row.shelf_policy_id)}",
            "scope_key": "shelf:all-live-mrps",
            "payload": {
                "policy_id": int(row.shelf_policy_id),
                "item_id": int(row.item_id),
                "warehouse_ref1c": str(row.warehouse_ref1c),
                "as_of_date": row.as_of_date.isoformat(),
                "protection_until": row.protection_until.isoformat(),
                "target_qty": str(row.target_qty),
                "shelf_physical_qty": str(row.shelf_physical_qty),
                "other_stock_qty": str(row.other_stock_qty),
                "projected_qty": str(row.projected_qty),
                "gap_qty": str(row.gap_qty),
                "transfer_qty": str(row.transfer_qty),
                "unlaunched_mrp_qty": str(row.unlaunched_mrp_qty),
                "pull_qty": str(row.pull_qty),
                "materialized_qty": str(row.materialized_qty),
                "first_shortage_date": row.first_shortage_date.isoformat() if row.first_shortage_date else None,
                "latest_start_date": row.latest_start_date.isoformat() if row.latest_start_date else None,
                "demand_manifest": list(row.demand_manifest or []),
            },
        })
    shelf_result = publish_current_execution_scope(
        db,
        source_revision=revision,
        source_generation_id=int(generation.id),
        scope_key="shelf:all-live-mrps",
        rows=shelf_rows,
    )
    return {
        "assembly_queue": queue_result,
        "assembly_readiness": readiness_result,
        "drum": drum_result,
        "shelf": shelf_result,
    }

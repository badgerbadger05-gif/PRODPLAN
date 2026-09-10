"""Stable current execution owner for the R8 queue/readiness/drum/shelf contour.

Generation-scoped builders remain useful as staging evidence.  Only this
module publishes the compact current rows consumed by current execution reads.
Technical generation/cutoff changes are provenance and do not create a
business change when the saved result is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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


def drum_slot_identity(plan_line_id: int, slot_ordinal: int) -> str:
    return f"slot:plan-line:{int(plan_line_id)}:ordinal:{int(slot_ordinal)}"


def drum_gap_identity(plan_line_id: int, gap_date: date | str) -> str:
    value = gap_date.isoformat() if isinstance(gap_date, date) else str(gap_date)
    return f"gap:plan-line:{int(plan_line_id)}:date:{value}"


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
    entity_kinds: Iterable[str] | None = None,
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

    expected_kinds = {
        str(value).strip() for value in (entity_kinds or ()) if str(value).strip()
    }
    incoming: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any], str, bool]] = {}
    for raw in rows:
        row = dict(raw)
        entity_kind = str(row.get("entity_kind") or "").strip()
        identity = str(row.get("business_identity") or "").strip()
        row_scope = str(row.get("scope_key") or scope).strip()
        if not entity_kind or not identity:
            raise CurrentExecutionUnavailable("current execution row lacks stable identity")
        if expected_kinds and entity_kind not in expected_kinds:
            raise CurrentExecutionUnavailable("current execution row has unexpected entity kind")
        if row_scope != scope:
            raise CurrentExecutionUnavailable("current execution row is outside complete scope")
        payload, manual = _semantic_payload(row)
        key = (entity_kind, identity)
        if key in incoming:
            raise CurrentExecutionUnavailable(f"duplicate current execution identity {entity_kind}:{identity}")
        incoming[key] = (
            payload,
            manual,
            _hash({"payload": payload, "manual_input": manual}),
            "manual_input" in row,
        )

    actual_kinds = {kind for kind, _identity in incoming}
    if not expected_kinds:
        expected_kinds = set(actual_kinds)
    if not expected_kinds:
        raise CurrentExecutionUnavailable("empty complete scope requires explicit entity kinds")
    for kind in sorted(expected_kinds):
        entries = [
            (identity, payload, manual)
            for (entry_kind, identity), (payload, manual, _content_hash, _manual_supplied)
            in incoming.items()
            if entry_kind == kind
        ]
        scope_hash = _hash(entries)
        manifest = db.query(models.CurrentExecutionScope).filter(
            models.CurrentExecutionScope.entity_kind == kind,
            models.CurrentExecutionScope.scope_key == scope,
        ).with_for_update().one_or_none()
        if manifest is None:
            db.add(models.CurrentExecutionScope(
                entity_kind=kind,
                scope_key=scope,
                source_revision=revision,
                source_generation_id=source_generation_id,
                result_ready=True,
                content_hash=scope_hash,
            ))
        else:
            # The manifest is both the valid-empty marker and the accepted
            # semantic pointer.  Technical generation/revision provenance is
            # advanced even when the business payload is unchanged; current
            # rows and their change audit remain byte-stable in that case.
            manifest.source_revision = revision
            manifest.source_generation_id = source_generation_id
            manifest.result_ready = True
            manifest.content_hash = scope_hash
    existing_query = db.query(models.CurrentExecutionRow).filter(
        models.CurrentExecutionRow.scope_key == scope,
    )
    existing_query = existing_query.filter(
        models.CurrentExecutionRow.entity_kind.in_(sorted(expected_kinds))
    )
    existing = existing_query.with_for_update().all()
    existing_by_key = {(str(row.entity_kind), str(row.business_identity)): row for row in existing}

    changed = 0
    closed = 0
    for key, (payload, manual, content_hash, manual_supplied) in incoming.items():
        row = existing_by_key.get(key)
        if row is not None and not manual_supplied:
            manual = dict(row.manual_input or {})
            content_hash = _hash({"payload": payload, "manual_input": manual})
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


def get_current_execution_scope(
    db: Session,
    *,
    entity_kind: str,
    scope_key: str,
) -> models.CurrentExecutionScope | None:
    """Return the persisted scope manifest; ``None`` means never published."""
    return db.query(models.CurrentExecutionScope).filter(
        models.CurrentExecutionScope.entity_kind == str(entity_kind),
        models.CurrentExecutionScope.scope_key == str(scope_key),
    ).one_or_none()


def require_current_execution_scope(
    db: Session,
    *,
    entity_kind: str,
    scope_key: str,
) -> models.CurrentExecutionScope:
    """Return only a scope whose manifest matches the accepted semantic pointer."""
    manifest = get_current_execution_scope(db, entity_kind=entity_kind, scope_key=scope_key)
    if manifest is None:
        raise CurrentExecutionUnavailable("current execution manifest is missing")
    if not bool(manifest.result_ready):
        raise CurrentExecutionUnavailable("current execution manifest is not ready")
    if manifest.source_generation_id is not None:
        truth_pointer = db.get(models.PlanningTruthState, 1)
        if truth_pointer is None or int(truth_pointer.current_generation_id or 0) != int(manifest.source_generation_id):
            raise CurrentExecutionUnavailable("current execution manifest is stale for accepted truth")
        generation = db.get(models.LedgerGeneration, int(manifest.source_generation_id))
        if generation is None or str(generation.status or "") != "accepted":
            raise CurrentExecutionUnavailable("current execution semantic pointer is not accepted")
    return manifest


def invalidate_current_execution_scope(
    db: Session,
    *,
    entity_kind: str,
    scope_key: str,
    source_revision: str,
    reason: str,
) -> bool:
    """Mark a persisted result unavailable until the worker republishes it."""
    manifest = db.query(models.CurrentExecutionScope).filter(
        models.CurrentExecutionScope.entity_kind == str(entity_kind),
        models.CurrentExecutionScope.scope_key == str(scope_key),
    ).with_for_update().one_or_none()
    if manifest is None:
        return False
    if not bool(manifest.result_ready):
        return False
    manifest.result_ready = False
    manifest.source_revision = str(source_revision)
    db.query(models.CurrentExecutionRow).filter(
        models.CurrentExecutionRow.entity_kind == str(entity_kind),
        models.CurrentExecutionRow.scope_key == str(scope_key),
        models.CurrentExecutionRow.result_status == "accepted",
    ).update({"result_ready": False}, synchronize_session=False)
    db.flush()
    return True


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
        entity_kinds=("assembly_queue",),
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
        entity_kinds=("assembly_readiness",),
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
        prior_manual = {
            str(row.business_identity): dict(row.manual_input or {})
            for row in db.query(models.CurrentExecutionRow).filter(
                models.CurrentExecutionRow.entity_kind == "drum_slot",
                models.CurrentExecutionRow.result_status == "accepted",
            ).all()
            if row.manual_input
        }
        for slot, queue in db.query(models.DrumSlot, models.AssemblyQueueLine).join(
            models.AssemblyQueueLine,
            models.AssemblyQueueLine.id == models.DrumSlot.assembly_queue_line_id,
        ).filter(
            models.DrumSlot.drum_schedule_id == int(schedule.id),
        ).order_by(
            models.DrumSlot.slot_date.asc(),
            models.DrumSlot.resource_id.asc(),
            models.DrumSlot.slot_ordinal.asc(),
            models.DrumSlot.id.asc(),
        ).all():
            identity = drum_slot_identity(int(slot.plan_line_id), int(slot.slot_ordinal))
            manual = prior_manual.get(identity)
            payload = {
                "queue_line_id": int(slot.assembly_queue_line_id),
                "plan_id": int(slot.plan_id),
                "plan_line_id": int(slot.plan_line_id),
                "run_id": int(queue.planning_run_id),
                "period_from": queue.period_from.isoformat(),
                "period_to": queue.period_to.isoformat(),
                "item_id": int(slot.item_id),
                "resource_id": int(slot.resource_id),
                "slot_date": slot.slot_date.isoformat(),
                "auto_slot_date": slot.auto_slot_date.isoformat() if slot.auto_slot_date else None,
                "slot_qty": str(slot.slot_qty),
                "capacity_load": str(slot.capacity_load) if slot.capacity_load is not None else None,
                "planned_output_qty": str(slot.planned_output_qty) if slot.planned_output_qty is not None else None,
                "accepted_plan_output_qty": str(slot.accepted_plan_output_qty) if slot.accepted_plan_output_qty is not None else None,
                "assembly_remaining_qty": str(slot.assembly_remaining_qty) if slot.assembly_remaining_qty is not None else None,
                "slot_ordinal": int(slot.slot_ordinal),
                "readiness_phase": str(slot.readiness_phase),
                "readiness_date": slot.readiness_date.isoformat() if slot.readiness_date else None,
                "readiness_curve": list(slot.readiness_curve or []),
                "action_manifest": list(slot.action_manifest or []),
                "unavailable_reasons": list(slot.unavailable_reasons or []),
                "blocking_manifest": list(slot.blocking_manifest or []),
                "original_priority": list(slot.original_priority or []),
            }
            if manual:
                if manual.get("slot_date"):
                    payload["slot_date"] = str(manual["slot_date"])
                if manual.get("resource_id") is not None:
                    payload["resource_id"] = int(manual["resource_id"])
            legacy_manual = manual
            if not legacy_manual and slot.manual_moved_at is not None:
                legacy_manual = {
                    "slot_date": slot.slot_date.isoformat(),
                    "resource_id": int(slot.resource_id),
                    "moved_at": slot.manual_moved_at.isoformat(),
                    "moved_by": str(slot.manual_moved_by or "operator"),
                }
            if legacy_manual:
                if legacy_manual.get("slot_date"):
                    payload["slot_date"] = str(legacy_manual["slot_date"])
                if legacy_manual.get("resource_id") is not None:
                    payload["resource_id"] = int(legacy_manual["resource_id"])
            drum_rows.append({
                "entity_kind": "drum_slot",
                "business_identity": identity,
                "scope_key": "drum:all-live-plans",
                "payload": payload,
                **({"manual_input": legacy_manual} if legacy_manual else {}),
            })
        for gap in db.query(models.DrumCapacityGap).filter(
            models.DrumCapacityGap.drum_schedule_id == int(schedule.id),
        ).order_by(
            models.DrumCapacityGap.gap_date.asc(),
            models.DrumCapacityGap.resource_id.asc(),
            models.DrumCapacityGap.id.asc(),
        ).all():
            identity = drum_gap_identity(int(gap.plan_line_id), gap.gap_date)
            drum_rows.append({
                "entity_kind": "drum_gap",
                "business_identity": identity,
                "scope_key": "drum:all-live-plans",
                "payload": {
                    "queue_line_id": int(gap.assembly_queue_line_id),
                    "plan_id": int(gap.plan_id),
                    "plan_line_id": int(gap.plan_line_id),
                    "item_id": int(gap.item_id),
                    "resource_id": int(gap.resource_id),
                    "gap_date": gap.gap_date.isoformat(),
                    "required_qty": str(gap.required_qty),
                    "available_capacity": str(gap.available_capacity),
                    "gap_qty": str(gap.gap_qty),
                    "readiness_phase": str(gap.readiness_phase),
                    "readiness_date": gap.readiness_date.isoformat() if gap.readiness_date else None,
                    "readiness_curve": list(gap.readiness_curve or []),
                    "action_manifest": list(gap.action_manifest or []),
                    "unavailable_reasons": list(gap.unavailable_reasons or []),
                    "blocking_manifest": list(gap.blocking_manifest or []),
                    "original_priority": list(gap.original_priority or []),
                },
            })
        drum_result = publish_current_execution_scope(
            db,
            source_revision=revision,
            source_generation_id=int(generation.id),
            scope_key="drum:all-live-plans",
            rows=drum_rows,
            entity_kinds=("drum_schedule", "drum_slot", "drum_gap"),
        )
    else:
        drum_result = publish_current_execution_scope(
            db,
            source_revision=revision,
            source_generation_id=int(generation.id),
            scope_key="drum:all-live-plans",
            rows=[],
            entity_kinds=("drum_schedule", "drum_slot", "drum_gap"),
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
        entity_kinds=("shelf_projection",),
    )
    return {
        "assembly_queue": queue_result,
        "assembly_readiness": readiness_result,
        "drum": drum_result,
        "shelf": shelf_result,
    }

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
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from sqlalchemy.orm import Session

from app import models


class CurrentExecutionUnavailable(ValueError):
    """The current execution result is absent, stale, incomplete or unsafe."""


_PERIOD_EXECUTION_STATUS_LABELS = {
    "net_zero": "Покрыто складом",
    "covered": "Закрыто",
    "partial": "Частично",
    "ordered": "Оформлено",
    "none": "Не оформлено",
    "execution_unavailable": "Исполнение недоступно",
}


def _period_work_item_target(
    item: dict[str, Any], *, run_id: int | None, requirement_id: int | None,
    target_catalog: dict[tuple[Any, ...], list[str]],
) -> tuple[str | None, str | None, str | None]:
    """Return stable current target identity, href and an unavailable reason."""
    kind = str(item.get("type") or "").strip()
    run = int(run_id or item.get("run_id") or 0)
    req = int(requirement_id or item.get("source_mrp_requirement_id") or 0)
    current = str(item.get("current_identity") or "").strip()
    href = str(item.get("navigation_href") or "").strip()
    if current and href:
        return current, href, None
    keys: list[tuple[Any, ...]] = []
    if kind == "production_order":
        if item.get("order_id") is not None and item.get("product_id") is not None:
            keys.append(("production", int(item["order_id"]), int(item["product_id"])))
        if req and item.get("product_id") is not None:
            keys.append(("production-requirement", req, int(item["product_id"])))
    elif kind == "planned_purchase":
        if item.get("one_c_opened") and item.get("order_ref1c"):
            keys.append(("purchase-ref", str(item["order_ref1c"]).strip()))
        if req and item.get("item_id") is not None:
            keys.append(("mrp", int(run or 0), "purchase", req, int(item["item_id"])))
    elif kind in {"planned_order", "planned_rework"} and run and req:
        row_kind = "production" if kind == "planned_order" else "rework"
        if item.get("item_id") is not None:
            keys.append(("mrp", int(run), row_kind, req, int(item["item_id"])))
    candidates: list[str] = []
    for key in keys:
        candidates.extend(target_catalog.get(key, []))
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) == 1:
        current = candidates[0]
        tab = {"planned_order": "production", "planned_purchase": "purchases", "planned_rework": "rework"}.get(kind)
        if kind == "production_order":
            href = f"#/production-control?current_identity={quote(current, safe='')}"
        elif kind == "planned_purchase" and item.get("one_c_opened"):
            href = f"#/purchase-control?current_identity={quote(current, safe='')}"
        elif tab:
            href = f"#/mrp-runs/{int(run or 0)}?tab={tab}&current_identity={quote(current, safe='')}"
    if current and href:
        return current, href, None
    return None, None, "Current target неоднозначен" if candidates else "Точный current target не опубликован"


def _period_execution_work_item(
    raw: dict[str, Any], *, run_id: int | None, requirement_id: int | None,
    target_catalog: dict[tuple[Any, ...], list[str]],
) -> dict[str, Any]:
    item = dict(raw)
    current_identity, href, reason = _period_work_item_target(
        item, run_id=run_id, requirement_id=requirement_id,
        target_catalog=target_catalog,
    )
    qty = float(item.get("qty") or 0.0)
    opened = bool(item.get("one_c_opened"))
    assigned = qty if str(item.get("type") or "") in {"production_order", "planned_rework"} or (
        str(item.get("type") or "") == "planned_purchase" and opened
    ) else 0.0
    unassigned = qty - assigned if str(item.get("type") or "") in {"planned_order", "planned_purchase"} else 0.0
    item.update({
        "assigned_qty": assigned,
        "unassigned_qty": max(0.0, unassigned),
        "current_identity": current_identity,
        "navigation_href": href,
        "navigation_reason": reason,
    })
    return item


def _period_execution_row_payload(
    payload: dict[str, Any], *, business_identity: str,
    target_catalog: dict[tuple[Any, ...], list[str]],
) -> dict[str, Any]:
    result = dict(payload)
    result["current_identity"] = str(result.get("current_identity") or business_identity)
    status = str(result.get("status") or "execution_unavailable")
    result["status_label"] = str(result.get("status_label") or _PERIOD_EXECUTION_STATUS_LABELS.get(status, status))
    result["explanations"] = list(result.get("explanations") or [])
    result["work_items"] = [
        _period_execution_work_item(
            dict(item),
            run_id=result.get("run_id"),
            requirement_id=result.get("req_id"),
            target_catalog=target_catalog,
        )
        for item in list(result.get("work_items") or [])
        if isinstance(item, dict)
    ]
    return result


def _period_execution_summary_for_current(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist the public execution-summary shape without recalculating it.

    Period snapshots retain root-output bookkeeping names for the historical
    snapshot service. The current HTTP DTO exposes canonical execution names;
    normalize the already-persisted values at publication time so a current
    GET remains a typed read of saved data and does not call finalization.
    """
    summary = dict(payload.get("summary") or {})
    for public_name, snapshot_name in (
        ("execution_completed_qty", "root_output_completed_qty"),
        ("execution_base_qty", "root_output_base_qty"),
        ("execution_pct", "root_output_pct"),
    ):
        if public_name not in summary and snapshot_name in summary:
            summary[public_name] = summary[snapshot_name]
        summary.pop(snapshot_name, None)
    return summary


def _nav_link(
    *,
    label: str,
    href: str | None,
    available: bool,
    reason: str | None = None,
    current_identity: str | None = None,
    source_revision: str | None = None,
) -> dict[str, Any]:
    return {
        "label": label,
        "href": href,
        "available": bool(available),
        "reason": reason,
        **({"current_identity": current_identity} if current_identity is not None else {}),
        **({"source_revision": source_revision} if source_revision is not None else {}),
    }


def _build_basis_links(ledger_links: dict[str, Any] | None) -> dict[str, Any]:
    """Build persisted navigation from the accepted row's ledger evidence only."""
    links = dict(ledger_links or {})
    item_id = links.get("item_id")
    item_link = _nav_link(
        label="Ledger item",
        href=f"#/ledger/items/{int(item_id)}" if item_id is not None else None,
        available=item_id is not None,
        reason=None if item_id is not None else "ledger item basis is unavailable",
    )
    reservation_links = [
        _nav_link(
            label=f"Reservation #{int(reservation_id)}",
            href=(
                f"#/ledger/items/{int(item_id)}?tab=reservations&reservation_id={int(reservation_id)}"
            ) if item_id is not None else None,
            available=item_id is not None,
            reason=None if item_id is not None else "ledger reservation basis is unavailable",
        )
        for reservation_id in sorted({
            int(value) for value in links.get("reservation_ids", []) if value is not None
        })
    ]
    event_links = [
        _nav_link(
            label=f"Ledger event #{int(event['event_id'])}",
            href=(
                f"#/ledger/items/{int(item_id)}?tab=reservations&reservation_id={int(event['reservation_id'])}"
                f"&event_id={int(event['event_id'])}"
            ) if item_id is not None else None,
            available=item_id is not None,
            reason=None if item_id is not None else "ledger event basis is unavailable",
        )
        for event in sorted(
            (value for value in links.get("events", []) if isinstance(value, dict)),
            key=lambda value: (int(value.get("event_id") or 0), int(value.get("reservation_id") or 0)),
        )
        if event.get("event_id") is not None and event.get("reservation_id") is not None
    ]
    return {
        "item": item_link,
        "reservations": reservation_links,
        "events": event_links,
        "reason": None if item_id is not None else "ledger basis is unavailable",
    }


def _build_queue_links(
    execution_payload: dict[str, Any],
    queue_manifest: Any | None,
    queue_rows: Iterable[Any],
    *,
    expected_generation_id: int | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Resolve period roots to every exact current queue line in the same generation."""
    disabled_reason: str | None = None
    if queue_manifest is None:
        disabled_reason = "assembly queue current target is unavailable for this plan/root scope"
    elif expected_generation_id is None or int(queue_manifest.source_generation_id or 0) != int(expected_generation_id):
        disabled_reason = "assembly queue current source generation does not match period execution source generation"

    if disabled_reason is not None:
        return [
            _nav_link(label="Assembly queue", href=None, available=False, reason=disabled_reason)
        ], disabled_reason

    plan_id = execution_payload.get("plan_id")
    root_item_ids = {
        int(value) for value in execution_payload.get("root_item_ids", []) if value is not None
    }
    matches = []
    for row in queue_rows:
        if isinstance(row, dict):
            payload = dict(row.get("payload") or {})
            row_identity = row.get("business_identity")
        else:
            payload = dict(getattr(row, "payload", None) or {})
            row_identity = getattr(row, "business_identity", None)
        if plan_id is None or int(payload.get("plan_id") or 0) != int(plan_id):
            continue
        if int(payload.get("item_id") or 0) not in root_item_ids:
            continue
        identity = str(row_identity or "").strip()
        if identity.startswith("plan-line:"):
            matches.append((identity, payload))
    unique_matches: dict[str, dict[str, Any]] = {}
    for identity, payload in matches:
        unique_matches.setdefault(identity, payload)
    matches = sorted(
        unique_matches.items(),
        key=lambda value: (value[0], int(value[1].get("plan_line_id") or 0)),
    )
    if not matches:
        reason = "assembly queue current target is unavailable for this plan/root scope"
        return [_nav_link(label="Assembly queue", href=None, available=False, reason=reason)], reason

    links = [
        _nav_link(
            label="Assembly queue",
            href=f"#/production-control?view=assembly-queue&current_identity={quote(identity, safe='')}",
            available=True,
            current_identity=identity,
            source_revision=str(queue_manifest.source_revision),
        )
        for identity, _payload in matches
    ]
    return links, None


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
    summary: dict[str, Any] | None = None,
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
                summary=_jsonable(summary or {}),
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
            if summary is not None:
                manifest.summary = _jsonable(summary)
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


def load_current_execution_coherent(
    db: Session,
    *,
    entity_kind: str,
    scope_key: str,
) -> tuple[models.CurrentExecutionScope, list[models.CurrentExecutionRow]]:
    """Read one current scope and its rows under one publication boundary.

    The manifest is the publication lock.  A publisher locks it before
    replacing current rows, so this reader either locks the old complete
    scope or waits for the new complete scope; it cannot observe a manifest
    from one publication and rows from another.  The separate row query also
    preserves a valid empty scope, which is distinct from a missing/not-ready
    manifest.
    """
    scope_model = models.CurrentExecutionScope
    row_model = models.CurrentExecutionRow
    # Lock the manifest in its own statement.  At READ COMMITTED PostgreSQL
    # rechecks a row after waiting for the publisher, while an outer-join
    # statement can retain its pre-wait snapshot for the nullable rows and
    # return a new manifest with old rows.  The second statement therefore
    # starts only after the publication lock is acquired and sees one whole
    # committed version.  A row-share lock is sufficient and avoids locking
    # the nullable side of an outer join.
    manifest = (
        db.query(scope_model)
        .filter(
            scope_model.entity_kind == str(entity_kind),
            scope_model.scope_key == str(scope_key),
        )
        .with_for_update(read=True, of=scope_model)
        .one_or_none()
    )
    if manifest is None:
        raise CurrentExecutionUnavailable("current execution manifest is missing")
    if not bool(manifest.result_ready):
        raise CurrentExecutionUnavailable("current execution manifest is not ready")

    # Read the accepted-truth pointer after acquiring the publication lock.
    # Sessions in this repository intentionally use autoflush=False.  If the
    # caller has already moved the pointer in this same transaction, a
    # populate_existing query would reload the old committed value and discard
    # that pending move, allowing a stale scope to pass.  Preserve a pending
    # identity-map value; otherwise refresh the committed pointer for the
    # concurrent-session publication boundary.
    pending_truth = next(
        (
            value
            for value in tuple(db.new) + tuple(db.dirty)
            if isinstance(value, models.PlanningTruthState)
            and int(value.id or 0) == 1
        ),
        None,
    )
    if pending_truth is not None:
        truth_pointer = pending_truth
    else:
        truth_pointer = (
            db.query(models.PlanningTruthState)
            .populate_existing()
            .filter(models.PlanningTruthState.id == 1)
            .one_or_none()
        )
    expected_generation_id = int(truth_pointer.current_generation_id or 0) if truth_pointer else 0
    if expected_generation_id:
        if int(manifest.source_generation_id or 0) != expected_generation_id:
            raise CurrentExecutionUnavailable("current execution manifest is stale for accepted truth")
        generation = (
            db.query(models.LedgerGeneration)
            .populate_existing()
            .filter(models.LedgerGeneration.id == expected_generation_id)
            .one_or_none()
        )
        if generation is None or str(generation.status or "") != "accepted":
            raise CurrentExecutionUnavailable("current execution semantic pointer is not accepted")
    elif manifest.source_generation_id is not None:
        generation = (
            db.query(models.LedgerGeneration)
            .populate_existing()
            .filter(models.LedgerGeneration.id == int(manifest.source_generation_id))
            .one_or_none()
        )
        if generation is None or str(generation.status or "") != "accepted":
            raise CurrentExecutionUnavailable("current execution semantic pointer is not accepted")
    rows = (
        db.query(row_model)
        .filter(
            row_model.entity_kind == str(entity_kind),
            row_model.scope_key == str(scope_key),
            row_model.result_status == "accepted",
            row_model.result_ready.is_(True),
        )
        .order_by(row_model.business_identity.asc(), row_model.id.asc())
        .all()
    )
    return manifest, rows


def require_current_execution_scope(
    db: Session,
    *,
    entity_kind: str,
    scope_key: str,
) -> models.CurrentExecutionScope:
    """Return only a scope whose manifest matches the accepted semantic pointer."""
    manifest, _rows = load_current_execution_coherent(
        db, entity_kind=entity_kind, scope_key=scope_key,
    )
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


def invalidate_current_execution_for_calendar_change(
    db: Session,
    *,
    source_revision: str,
    reason: str = "calendar_changed",
) -> tuple[str, ...]:
    """Invalidate every calendar-dependent current result as one domain hook.

    WorkCalendarDay has no HTTP writer in this contour.  Its import/admin writer
    must call this hook in the same transaction as the calendar mutation; a
    missing hook is intentionally fail-closed rather than silently serving a
    schedule built for the previous working-day boundary.
    """
    invalidated: list[str] = []
    for entity_kind, scope_key in (
        ("assembly_readiness", "assembly:all-live-plans"),
        ("drum_schedule", "drum:all-live-plans"),
    ):
        if invalidate_current_execution_scope(
            db,
            entity_kind=entity_kind,
            scope_key=scope_key,
            source_revision=str(source_revision),
            reason=reason,
        ):
            invalidated.append(entity_kind)
    return tuple(invalidated)


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
        summary={
            "total_rows": len(queue_payload),
            "total_queue_qty": str(sum(
                (
                    Decimal(str(row["payload"].get("assembly_remaining_qty") or "0"))
                    for row in queue_payload
                ),
                Decimal("0"),
            )),
        },
    )
    # Readiness and drum are generation-scoped staging tables, but their
    # current payload must never expose those staging row ids.  Resolve the
    # stable current queue owner after the queue scope is published and use it
    # consistently for every dependent contour.
    stable_queue_ids = {
        int(row.payload.get("plan_line_id")): int(row.id)
        for row in load_current_execution_rows(
            db,
            entity_kind="assembly_queue",
            scope_key="assembly:all-live-plans",
        )
        if row.payload.get("plan_line_id") is not None
    }

    readiness_payload = []
    readiness_rows = db.query(models.AssemblyReadiness, models.AssemblyQueueLine).join(
        models.AssemblyQueueLine,
        models.AssemblyQueueLine.id == models.AssemblyReadiness.assembly_queue_line_id,
    ).filter(
        models.AssemblyReadiness.ledger_generation_id == int(generation.id),
    ).all()
    for readiness, queue in readiness_rows:
        stable_queue_id = stable_queue_ids.get(int(queue.plan_line_id))
        if stable_queue_id is None:
            raise CurrentExecutionUnavailable(
                f"readiness has no current queue owner for plan line {int(queue.plan_line_id)}"
            )
        readiness_payload.append({
            "entity_kind": "assembly_readiness",
            "business_identity": f"plan-line:{int(queue.plan_line_id)}",
            "scope_key": "assembly:all-live-plans",
            "payload": {
                "queue_line_id": stable_queue_id,
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
            stable_queue_id = stable_queue_ids.get(int(slot.plan_line_id))
            if stable_queue_id is None:
                raise CurrentExecutionUnavailable(
                    f"drum slot has no current queue owner for plan line {int(slot.plan_line_id)}"
                )
            manual = prior_manual.get(identity)
            payload = {
                "queue_line_id": stable_queue_id,
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
            stable_queue_id = stable_queue_ids.get(int(gap.plan_line_id))
            if stable_queue_id is None:
                raise CurrentExecutionUnavailable(
                    f"drum gap has no current queue owner for plan line {int(gap.plan_line_id)}"
                )
            drum_rows.append({
                "entity_kind": "drum_gap",
                "business_identity": identity,
                "scope_key": "drum:all-live-plans",
                "payload": {
                    "queue_line_id": stable_queue_id,
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
        excluded_item_ids = {
            int(item_id)
            for item_id in list((schedule.metrics or {}).get("excluded_item_ids") or [])
        }
        readiness_by_queue_id = {
            int(row.assembly_queue_line_id): row
            for row in db.query(models.AssemblyReadiness).filter(
                models.AssemblyReadiness.ledger_generation_id == int(generation.id),
                models.AssemblyReadiness.assembly_queue_line_id.in_(
                    [int(row.id) for row in queue_rows]
                ),
            ).all()
        }
        for queue in queue_rows:
            if int(queue.item_id) not in excluded_item_ids:
                continue
            readiness = readiness_by_queue_id.get(int(queue.id))
            stable_queue_id = stable_queue_ids.get(int(queue.plan_line_id))
            if readiness is None or stable_queue_id is None:
                raise CurrentExecutionUnavailable(
                    f"excluded drum row lacks saved readiness/current queue owner for plan line {int(queue.plan_line_id)}"
                )
            drum_rows.append({
                "entity_kind": "drum_excluded",
                "business_identity": f"excluded:plan-line:{int(queue.plan_line_id)}",
                "scope_key": "drum:all-live-plans",
                "payload": {
                    "queue_line_id": stable_queue_id,
                    "plan_id": int(queue.plan_id),
                    "plan_line_id": int(queue.plan_line_id),
                    "run_id": int(queue.planning_run_id),
                    "item_id": int(queue.item_id),
                    "period_from": queue.period_from.isoformat(),
                    "period_to": queue.period_to.isoformat(),
                    "planned_output_qty": str(queue.planned_output_qty),
                    "accepted_plan_output_qty": str(queue.accepted_plan_output_qty),
                    "assembly_remaining_qty": str(queue.assembly_remaining_qty),
                    "reason": "ASSEMBLY_RATE_MISSING",
                    "readiness_status": str(readiness.status),
                    "readiness_date": readiness.readiness_date.isoformat() if readiness.readiness_date else None,
                    "readiness_curve": list(readiness.readiness_curve or []),
                    "action_manifest": list(readiness.action_manifest or []),
                    "unavailable_reasons": list(readiness.unavailable_reasons or []),
                    "blocking_manifest": list(readiness.blocking_manifest or []),
                    "original_priority": list(queue.original_priority or []),
                },
            })
        drum_result = publish_current_execution_scope(
            db,
            source_revision=revision,
            source_generation_id=int(generation.id),
            scope_key="drum:all-live-plans",
            rows=drum_rows,
            entity_kinds=("drum_schedule", "drum_slot", "drum_gap", "drum_excluded"),
        )
    else:
        drum_result = publish_current_execution_scope(
            db,
            source_revision=revision,
            source_generation_id=int(generation.id),
            scope_key="drum:all-live-plans",
            rows=[],
            entity_kinds=("drum_schedule", "drum_slot", "drum_gap", "drum_excluded"),
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


def publish_current_purchase_control_from_payload(
    db: Session,
    generation_id: int,
    payload: Mapping[str, Any],
) -> CurrentExecutionPublishResult:
    """Publish the purchase journal directly from its canonical candidate payload.

    Purchase current state is owned by ``CurrentExecutionScope`` and must not
    require creating an immutable ``PlanningReadSnapshot`` first.  The caller
    supplies the already validated Ledger-native candidate payload; this
    adapter only adds the stable current-row envelope and delegates all
    identity, complete-scope, idempotency and rollback semantics to the one
    current execution publisher.
    """

    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or str(generation.status or "") != "accepted":
        raise CurrentExecutionUnavailable(
            "purchase current publication requires an accepted generation"
        )
    if not isinstance(payload, Mapping):
        raise CurrentExecutionUnavailable("purchase candidate payload is malformed")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise CurrentExecutionUnavailable("purchase candidate rows are missing")
    current_rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise CurrentExecutionUnavailable("purchase candidate row is malformed")
        # Candidate builders may retain the immutable snapshot envelope
        # (``{"row_key": ..., "payload": {...}}``), while the direct
        # current publisher owns the inner business payload.  Normalize both
        # shapes here so the runtime path never publishes a nested legacy
        # envelope as the current DTO.
        candidate = raw.get("payload") if isinstance(raw.get("payload"), Mapping) else raw
        row = dict(candidate)
        identity = str(
            raw.get("current_identity")
            or row.get("current_identity")
            or raw.get("row_key")
            or row.get("row_key")
            or ""
        ).strip()
        if not identity:
            raise CurrentExecutionUnavailable("purchase candidate row lacks stable identity")
        current_rows.append({
            "entity_kind": "purchase_control_journal",
            "business_identity": identity,
            "scope_key": "purchase:all-live-plans",
            "payload": row,
        })
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    summary = dict(meta)
    if isinstance(payload.get("summary"), Mapping):
        summary["summary"] = dict(payload["summary"])
    if isinstance(payload.get("cards"), Mapping):
        summary["cards"] = dict(payload["cards"])
    summary["total_rows"] = len(current_rows)
    return publish_current_execution_scope(
        db,
        source_revision=f"accepted:g{int(generation.id)}:purchase_control_journal",
        source_generation_id=int(generation.id),
        scope_key="purchase:all-live-plans",
        rows=current_rows,
        entity_kinds=("purchase_control_journal",),
        summary=summary,
    )


def publish_current_production_control_from_payload(
    db: Session,
    generation_id: int,
    payload: Mapping[str, Any],
) -> CurrentExecutionPublishResult:
    """Publish the production journal directly into its compact current owner."""
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or str(generation.status or "") != "accepted":
        raise CurrentExecutionUnavailable(
            "production current publication requires an accepted generation"
        )
    if not isinstance(payload, Mapping):
        raise CurrentExecutionUnavailable("production candidate payload is malformed")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise CurrentExecutionUnavailable("production candidate rows are missing")
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    expected_count = meta.get("row_count")
    if expected_count is not None:
        try:
            if int(expected_count) != len(raw_rows):
                raise CurrentExecutionUnavailable(
                    "production candidate row count is malformed"
                )
        except (TypeError, ValueError) as exc:
            raise CurrentExecutionUnavailable(
                "production candidate row count is malformed"
            ) from exc
    current_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise CurrentExecutionUnavailable("production candidate row is malformed")
        candidate = raw.get("payload") if isinstance(raw.get("payload"), Mapping) else raw
        row = dict(candidate)
        identity = str(
            raw.get("current_identity")
            or row.get("current_identity")
            or ""
        ).strip()
        if not identity:
            identity = _production_snapshot_identity(row)
        if not identity:
            raise CurrentExecutionUnavailable(
                "production candidate row lacks stable business identity"
            )
        if identity in seen:
            raise CurrentExecutionUnavailable(
                "production candidate contains duplicate identity"
            )
        seen.add(identity)
        roots = row.get("root_item_ids")
        if roots is not None:
            if not isinstance(roots, (list, tuple)):
                raise CurrentExecutionUnavailable(
                    "production candidate root membership is malformed"
                )
            try:
                row["root_item_ids"] = sorted({int(value) for value in roots})
            except (TypeError, ValueError) as exc:
                raise CurrentExecutionUnavailable(
                    "production candidate root membership is malformed"
                ) from exc
        row = _production_semantic_payload(row)
        current_rows.append({
            "entity_kind": "production_control_journal",
            "business_identity": identity,
            "scope_key": "production:all-live-orders",
            "payload": row,
        })
    summary = dict(meta)
    if isinstance(payload.get("summary"), Mapping):
        summary["summary"] = dict(payload["summary"])
    summary["total_rows"] = len(current_rows)
    return publish_current_execution_scope(
        db,
        source_revision=f"accepted:g{int(generation.id)}:production_control_journal",
        source_generation_id=int(generation.id),
        scope_key="production:all-live-orders",
        rows=current_rows,
        entity_kinds=("production_control_journal",),
        summary=summary,
    )


def _snapshot_row_identity(payload: dict[str, Any], fallback: str) -> str:
    """Resolve a required business key; technical row ids are never valid."""
    for key in (
        "row_key", "journal_row_key", "business_identity", "requirement_id",
        "source_mrp_requirement_id", "order_id", "product_id", "item_id",
    ):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value) if key in {"row_key", "journal_row_key", "business_identity"} else f"{key}:{value}"
    raise CurrentExecutionUnavailable(
        "accepted snapshot row lacks a stable business identity"
    )


def _mrp_current_identity(payload: dict[str, Any], *, run_id: int, row_kind: str) -> str:
    """Build an MRP identity from the actual aggregate's semantic key."""
    kind = str(row_kind).strip().lower()
    item_id = payload.get("item_id")
    unit = str(payload.get("unit") or "").strip()
    bucket = (
        payload.get("bucket_date") or payload.get("need_date")
        or payload.get("start_date") or payload.get("date")
        or str(payload.get("sort_key") or "").split("|", 1)[0]
    )
    if kind == "capacity":
        area = payload.get("area_id") or payload.get("resource_id")
        if area in (None, "") or bucket in (None, ""):
            raise CurrentExecutionUnavailable("MRP capacity row lacks stable area/date identity")
        return f"mrp-run:{int(run_id)}:capacity:area:{area}:bucket:{bucket}"
    explicit_requirement = (
        payload.get("source_mrp_requirement_id")
        or payload.get("requirement_id")
        or payload.get("req_id")
    )
    if explicit_requirement not in (None, "") and item_id not in (None, ""):
        discriminator = (
            payload.get("source_mrp_allocation_key")
            or payload.get("source_mrp_allocation_id")
            or payload.get("supplier_ref1c")
            or payload.get("bucket_date")
            or "default"
        )
        return (
            f"mrp-run:{int(run_id)}:{kind}:requirement:{explicit_requirement}:"
            f"item:{int(item_id)}:allocation:{str(discriminator)}"
        )
    if item_id not in (None, ""):
        if kind == "production":
            semantic = payload.get("agg_key") or payload.get("demand_ref")
            if semantic in (None, "") and bucket in (None, ""):
                raise CurrentExecutionUnavailable("MRP production row lacks start-date aggregate identity")
            semantic = semantic or f"item:{int(item_id)}|start:{bucket or ''}|unit:{unit}"
        elif kind == "purchase":
            semantic = payload.get("agg_key")
            if semantic in (None, "") and not unit:
                raise CurrentExecutionUnavailable("MRP purchase row lacks unit aggregate identity")
            semantic = semantic or f"item:{int(item_id)}|unit:{unit}"
        elif kind == "rework":
            semantic = payload.get("agg_key") or (
                f"item:{int(item_id)}|bucket:{bucket or ''}|unit:{unit}|"
                f"spec:{payload.get('spec_id') or ''}"
            )
        else:
            semantic = payload.get("agg_key") or payload.get("demand_ref")
        if semantic not in (None, ""):
            return f"mrp-run:{int(run_id)}:{kind}:{str(semantic)}"
    raise CurrentExecutionUnavailable(f"MRP {row_kind} row lacks stable aggregate identity")


def _mrp_current_payload(payload: dict[str, Any], *, business_identity: str) -> dict[str, Any]:
    """Keep only current MRP business data in the compact row.

    Snapshot row keys, ordinal sort indexes and proposal primary keys are
    generation-local locators.  They must not leak into the current DTO or
    churn its owner when an equivalent accepted generation is republished.
    The persisted sort key is rebuilt from the semantic date/item/identity.
    """
    result = dict(payload)
    for key in (
        "row_key", "journal_row_key", "sort_key", "purchase_id",
        "planned_purchase_id", "order_id", "rework_id", "work_item_id",
    ):
        result.pop(key, None)
    original_sort_key = str(payload.get("sort_key") or "")
    bucket = (
        result.get("bucket_date") or result.get("need_date")
        or result.get("start_date") or result.get("date")
        or original_sort_key.split("|", 1)[0] or ""
    )
    item_id = result.get("item_id")
    result["sort_key"] = f"{bucket}|{int(item_id) if item_id is not None else 0:012d}"
    return result


def _production_snapshot_identity(payload: dict[str, Any]) -> str:
    """Resolve production journal identity without generation-local work IDs."""

    requirement_id = payload.get("source_mrp_requirement_id") or payload.get("requirement_id")
    if requirement_id not in (None, ""):
        discriminator = (
            payload.get("source_mrp_allocation_key")
            or payload.get("source_mrp_allocation_id")
            or payload.get("item_id")
            or "default"
        )
        return f"production-mrp-requirement:{int(requirement_id)}:{discriminator}"
    order_id = payload.get("order_id")
    if order_id not in (None, ""):
        line = payload.get("line_number") or payload.get("product_id") or payload.get("item_id")
        if line in (None, ""):
            raise CurrentExecutionUnavailable("production order row lacks stable line identity")
        return f"production-order-line:{int(order_id)}:{line}"
    journal_key = str(payload.get("journal_row_key") or payload.get("row_key") or "")
    if journal_key.startswith("work-item:"):
        raise CurrentExecutionUnavailable(
            "production proposal lacks stable MRP requirement identity"
        )
    return _snapshot_row_identity(payload, "production")


def _production_semantic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop generation-local proposal locators from the current business row."""

    result = dict(payload)
    if result.get("source_mrp_requirement_id") not in (None, "") or str(
        result.get("journal_row_key") or result.get("row_key") or ""
    ).startswith("work-item:"):
        result.pop("work_item_id", None)
        result.pop("journal_row_key", None)
        result.pop("row_key", None)
    return result


def _normalize_mrp_current_payloads(
    mrp_payloads: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate and normalize one canonical MRP payload boundary."""
    if not isinstance(mrp_payloads, Mapping):
        raise CurrentExecutionUnavailable("runtime MRP publication requires explicit mrp_payloads")
    rows: list[dict[str, Any]] = []
    runs: dict[str, Any] = {}
    seen: set[str] = set()
    for marker, candidate in sorted(mrp_payloads.items(), key=lambda pair: str(pair[0])):
        if not isinstance(candidate, Mapping):
            raise CurrentExecutionUnavailable("MRP current payload is malformed")
        try:
            run_id = int(candidate.get("run_id", marker))
        except (TypeError, ValueError) as exc:
            raise CurrentExecutionUnavailable("MRP current payload run identity is malformed") from exc
        raw_rows = candidate.get("rows")
        if not isinstance(raw_rows, list):
            raise CurrentExecutionUnavailable("MRP current payload rows are missing")
        counts = dict(candidate.get("row_counts") or {})
        totals = dict(candidate.get("total_qty") or {})
        required_kinds = {"production", "purchase", "rework", "capacity"}
        if set(counts) != required_kinds or any(int(counts.get(kind, -1)) != sum(
            1 for row in raw_rows
            if isinstance(row, Mapping)
            and str((row.get("payload") if isinstance(row.get("payload"), Mapping) else row).get("row_kind") or "").lower() == kind
        ) for kind in required_kinds):
            raise CurrentExecutionUnavailable("MRP current payload row counts are malformed")
        runs[str(run_id)] = {
            "summary": dict(candidate.get("summary") or {}),
            "row_counts": counts,
            "total_qty": totals,
        }
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                raise CurrentExecutionUnavailable("MRP current payload row is malformed")
            payload_source = raw.get("payload") if isinstance(raw.get("payload"), Mapping) else raw
            payload = dict(payload_source)
            kind = str(payload.get("row_kind") or raw.get("row_kind") or "").strip().lower()
            if kind not in {"production", "purchase", "rework", "capacity"}:
                raise CurrentExecutionUnavailable("MRP current payload row kind is malformed")
            payload["run_id"] = run_id
            payload["row_kind"] = kind
            identity = str(raw.get("current_identity") or payload.get("current_identity") or "").strip()
            if not identity:
                identity = _mrp_current_identity(payload, run_id=run_id, row_kind=kind)
            if identity in seen:
                raise CurrentExecutionUnavailable("MRP current payload contains duplicate identity")
            seen.add(identity)
            roots = payload.get("root_item_ids")
            if roots is not None:
                if not isinstance(roots, (list, tuple)):
                    raise CurrentExecutionUnavailable("MRP current payload root membership is malformed")
                try:
                    payload["root_item_ids"] = sorted({int(value) for value in roots})
                except (TypeError, ValueError) as exc:
                    raise CurrentExecutionUnavailable("MRP current payload root membership is malformed") from exc
            rows.append({
                "entity_kind": "mrp_result",
                "business_identity": identity,
                "scope_key": "mrp:all-live-plans",
                "payload": _mrp_current_payload(payload, business_identity=identity),
            })
    return rows, {"total_rows": len(rows), "runs": runs}


def publish_current_mrp_results_from_payloads(
    db: Session,
    generation_id: int,
    mrp_payloads: Mapping[str, Any],
) -> CurrentExecutionPublishResult:
    """Publish all MRP result rows directly into the current owner."""
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or str(generation.status or "") != "accepted":
        raise CurrentExecutionUnavailable("current MRP publication requires an accepted generation")
    rows, summary = _normalize_mrp_current_payloads(mrp_payloads)
    return publish_current_execution_scope(
        db,
        source_revision=f"accepted:g{int(generation.id)}:mrp_result",
        source_generation_id=int(generation.id),
        scope_key="mrp:all-live-plans",
        rows=rows,
        entity_kinds=("mrp_result",),
        summary=summary,
    )


def _publish_current_obligation_views(
    db: Session,
    generation_id: int,
    *,
    purchase_payload: Mapping[str, Any],
    production_payload: Mapping[str, Any],
    mrp_payloads: Mapping[str, Any],
) -> dict[str, CurrentExecutionPublishResult]:
    """Promote accepted obligation/read-model snapshots to compact current rows.

    The immutable snapshots remain publication evidence.  User reads use these
    rows, so a technical generation id is provenance only and never a current
    identity.  The operation is deliberately a no-op when the same semantic
    payload is published again.
    """
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or str(generation.status or "") != "accepted":
        raise CurrentExecutionUnavailable("current obligation views require an accepted generation")

    results: dict[str, CurrentExecutionPublishResult] = {}

    def _publish(
        *,
        consumer: str,
        entity_kind: str,
        scope_key: str,
        rows: list[dict[str, Any]],
        summary: dict[str, Any] | None = None,
    ) -> None:
        results[consumer] = publish_current_execution_scope(
            db,
            source_revision=f"accepted:g{int(generation.id)}:{consumer}",
            source_generation_id=int(generation.id),
            scope_key=scope_key,
            rows=rows,
            entity_kinds=(entity_kind,),
            summary=summary,
        )

    results["production_control_journal"] = publish_current_production_control_from_payload(
        db,
        int(generation.id),
        production_payload,
    )

    results["purchase_control_journal"] = publish_current_purchase_control_from_payload(
        db,
        int(generation.id),
        purchase_payload,
    )
    purchase_rows = [
        {
            "entity_kind": "purchase_control_journal",
            "business_identity": _snapshot_row_identity(
                dict(row.get("payload") or row) if isinstance(row, dict) else {},
                "purchase",
            ),
            "scope_key": "purchase:all-live-plans",
            "payload": dict(row.get("payload") or row) if isinstance(row, dict) else {},
        }
        for row in list(purchase_payload.get("rows") or [])
        if isinstance(row, dict)
    ]

    mrp_rows, mrp_metadata = _normalize_mrp_current_payloads(mrp_payloads)
    _publish(
        consumer="mrp_result",
        entity_kind="mrp_result",
        scope_key="mrp:all-live-plans",
        rows=mrp_rows,
        summary=mrp_metadata,
    )

    # Period work-item links resolve against the identities that this same
    # publication is about to expose.  Technical product/order/purchase IDs
    # are locators only; no identity is synthesized from them.
    target_catalog: dict[tuple[Any, ...], list[str]] = {}

    def _catalog(key: tuple[Any, ...], identity: str) -> None:
        target_catalog.setdefault(key, []).append(str(identity))

    production_rows = load_current_execution_rows(
        db,
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    )
    for entry in production_rows:
        payload = dict(entry.payload or {})
        identity = str(entry.business_identity or "")
        if payload.get("order_id") is not None and payload.get("product_id") is not None:
            _catalog(("production", int(payload["order_id"]), int(payload["product_id"])), identity)
        if payload.get("source_mrp_requirement_id") is not None and payload.get("product_id") is not None:
            _catalog(("production-requirement", int(payload["source_mrp_requirement_id"]), int(payload["product_id"])), identity)
    for entry in purchase_rows:
        payload = dict(entry.get("payload") or {})
        identity = str(entry.get("business_identity") or "")
        if payload.get("order_ref1c"):
            _catalog(("purchase-ref", str(payload["order_ref1c"]).strip()), identity)
    for entry in mrp_rows:
        payload = dict(entry.get("payload") or {})
        identity = str(entry.get("business_identity") or "")
        run_id = payload.get("run_id")
        row_kind = str(payload.get("row_kind") or "")
        req_id = payload.get("req_id") or payload.get("requirement_id") or payload.get("source_mrp_requirement_id")
        item_id = payload.get("item_id")
        if run_id is not None and req_id is not None and item_id is not None:
            _catalog(("mrp", int(run_id), row_kind, int(req_id), int(item_id)), identity)

    execution_rows: list[dict[str, Any]] = []
    execution_metadata: dict[str, Any] = {}
    queue_manifest = get_current_execution_scope(
        db,
        entity_kind="assembly_queue",
        scope_key="assembly:all-live-plans",
    )
    queue_rows = load_current_execution_rows(
        db,
        entity_kind="assembly_queue",
        scope_key="assembly:all-live-plans",
    )
    execution_snapshots = db.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == "period_plan_execution",
        models.PlanningReadSnapshot.ledger_generation_id == int(generation.id),
        models.PlanningReadSnapshot.truth_status == "accepted",
    ).order_by(models.PlanningReadSnapshot.snapshot_key.asc(), models.PlanningReadSnapshot.id.asc()).all()
    for snapshot in execution_snapshots:
        payload = dict(snapshot.payload or {})
        plan = dict(payload.get("plan") or {})
        run_id = payload.get("run_id")
        execution_metadata[str(snapshot.snapshot_key)] = {
            "snapshot_key": str(snapshot.snapshot_key),
            "plan": plan,
            "run_id": run_id,
            "summary": _period_execution_summary_for_current(payload),
            "plan_output_rows": list(payload.get("plan_output_rows") or []),
            "truth_status": payload.get("truth_status"),
            "truth_generation_id": payload.get("truth_generation_id"),
            "cutoff": payload.get("cutoff"),
            "truth_cutoff": payload.get("truth_cutoff"),
            "truth_reason": payload.get("truth_reason"),
            "facets": dict(payload.get("facets") or {}),
        }
        source_rows = payload.get("rows")
        if not isinstance(source_rows, list):
            continue
        for index, row in enumerate(source_rows):
            if not isinstance(row, dict):
                continue
            row_payload = dict(row)
            row_payload.setdefault("run_id", run_id)
            row_payload.setdefault("plan_id", plan.get("id"))
            identity = _snapshot_row_identity(row_payload, "period-plan")
            business_identity = f"plan:{plan.get('id')}:{identity}"
            row_payload = _period_execution_row_payload(
                row_payload,
                business_identity=business_identity,
                target_catalog=target_catalog,
            )
            row_payload["basis_links"] = _build_basis_links(
                row_payload.get("ledger_links")
            )
            queue_links, queue_link_reason = _build_queue_links(
                row_payload,
                queue_manifest,
                queue_rows,
                expected_generation_id=int(generation.id),
            )
            row_payload["queue_links"] = queue_links
            row_payload["queue_link_reason"] = queue_link_reason
            execution_rows.append({
                "entity_kind": "period_plan_execution",
                "business_identity": business_identity,
                "scope_key": "period-plan:all-live-plans",
                "payload": row_payload,
            })
    _publish(
        consumer="period_plan_execution",
        entity_kind="period_plan_execution",
        scope_key="period-plan:all-live-plans",
        rows=execution_rows,
        summary={"total_rows": len(execution_rows), "snapshots": execution_metadata},
    )
    return results


def publish_current_obligation_views_from_generation(
    db: Session,
    generation_id: int,
    *,
    purchase_payload: Mapping[str, Any] | None = None,
    production_payload: Mapping[str, Any] | None = None,
    mrp_payloads: Mapping[str, Any] | None = None,
) -> dict[str, CurrentExecutionPublishResult]:
    """Publish runtime current views with explicit obligation candidates.

    Purchase and production payloads are mandatory by design.  Runtime
    publication cannot discover or synthesize them from historical
    ``PlanningReadSnapshot`` rows.
    """
    if not isinstance(purchase_payload, Mapping):
        raise CurrentExecutionUnavailable(
            "runtime purchase publication requires an explicit purchase payload"
        )
    if not isinstance(production_payload, Mapping):
        raise CurrentExecutionUnavailable(
            "runtime production publication requires an explicit production payload"
        )
    if not isinstance(mrp_payloads, Mapping):
        raise CurrentExecutionUnavailable(
            "runtime MRP publication requires explicit mrp_payloads"
        )
    return _publish_current_obligation_views(
        db,
        generation_id,
        purchase_payload=purchase_payload,
        production_payload=production_payload,
        mrp_payloads=mrp_payloads,
    )


def publish_current_obligation_views_from_snapshots(
    db: Session,
    generation_id: int,
) -> dict[str, CurrentExecutionPublishResult]:
    """Migration-only adapter for pre-R10 snapshot evidence.

    Production runtime callers must use
    :func:`publish_current_obligation_views_from_generation`; this explicitly
    named adapter is retained solely for local migration rehearsal and legacy
    fixture conversion.
    """
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or str(generation.status or "") != "accepted":
        raise CurrentExecutionUnavailable("snapshot migration requires an accepted generation")
    snapshot = db.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == "purchase_control_journal",
        models.PlanningReadSnapshot.snapshot_key == "journal:v1",
        models.PlanningReadSnapshot.ledger_generation_id == int(generation.id),
        models.PlanningReadSnapshot.truth_status == "accepted",
    ).one_or_none()
    if snapshot is None or not isinstance(snapshot.payload, Mapping):
        raise CurrentExecutionUnavailable(
            "migration purchase snapshot evidence is missing"
        )
    production = db.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == "production_control_journal",
        models.PlanningReadSnapshot.snapshot_key == "journal:v1",
        models.PlanningReadSnapshot.ledger_generation_id == int(generation.id),
        models.PlanningReadSnapshot.truth_status == "accepted",
    ).one_or_none()
    if production is None or not isinstance(production.payload, Mapping):
        raise CurrentExecutionUnavailable(
            "migration production snapshot evidence is missing"
        )
    production_rows: list[dict[str, Any]] = []
    for row in db.query(models.PlanningReadRow).filter(
        models.PlanningReadRow.snapshot_id == int(production.id),
    ).order_by(models.PlanningReadRow.sort_key.asc(), models.PlanningReadRow.id.asc()).all():
        row_payload = dict(row.payload or {})
        row_payload["root_item_ids"] = [
            int(member.root_item_id)
            for member in db.query(models.PlanningReadRootMember).filter(
                models.PlanningReadRootMember.snapshot_id == int(production.id),
                models.PlanningReadRootMember.row_id == int(row.id),
            ).order_by(models.PlanningReadRootMember.root_item_id.asc()).all()
        ]
        production_rows.append(row_payload)
    production_payload = dict(production.payload)
    production_payload["rows"] = production_rows
    mrp_payloads: dict[str, dict[str, Any]] = {}
    for legacy in db.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == "mrp_result",
        models.PlanningReadSnapshot.ledger_generation_id == int(generation.id),
        models.PlanningReadSnapshot.truth_status == "accepted",
    ).order_by(models.PlanningReadSnapshot.snapshot_key.asc()).all():
        marker = str(legacy.snapshot_key).removeprefix("run:").split(":", 1)[0]
        if not marker.isdigit():
            raise CurrentExecutionUnavailable("migration MRP snapshot key is malformed")
        rows: list[dict[str, Any]] = []
        for legacy_row in db.query(models.PlanningReadRow).filter(
            models.PlanningReadRow.snapshot_id == int(legacy.id),
        ).order_by(models.PlanningReadRow.sort_key.asc(), models.PlanningReadRow.id.asc()).all():
            payload = dict(legacy_row.payload or {})
            payload.setdefault("run_id", int(marker))
            payload.setdefault("row_kind", str(legacy_row.row_kind))
            payload.setdefault("sort_key", str(legacy_row.sort_key or ""))
            payload["root_item_ids"] = [
                int(member.root_item_id)
                for member in db.query(models.PlanningReadRootMember).filter(
                    models.PlanningReadRootMember.snapshot_id == int(legacy.id),
                    models.PlanningReadRootMember.row_id == int(legacy_row.id),
                ).order_by(models.PlanningReadRootMember.root_item_id.asc()).all()
            ]
            kind = str(payload["row_kind"]).lower()
            identity = _mrp_current_identity(payload, run_id=int(marker), row_kind=kind)
            rows.append({
                "current_identity": identity,
                "payload": _mrp_current_payload(payload, business_identity=identity),
            })
        legacy_payload = dict(legacy.payload or {})
        legacy_payload["run_id"] = int(marker)
        legacy_counts = {kind: 0 for kind in ("production", "purchase", "rework", "capacity")}
        for row in rows:
            kind = str((row.get("payload") or {}).get("row_kind") or "").lower()
            if kind in legacy_counts:
                legacy_counts[kind] += 1
        legacy_payload["row_counts"] = legacy_counts
        legacy_payload["rows"] = rows
        mrp_payloads[marker] = legacy_payload
    return _publish_current_obligation_views(
        db,
        generation_id,
        purchase_payload=dict(snapshot.payload),
        production_payload=production_payload,
        mrp_payloads=mrp_payloads,
    )

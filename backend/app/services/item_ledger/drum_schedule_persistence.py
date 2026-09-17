"""Canonical persisted assembly queue and deterministic drum schedule."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import models
from app.services.work_calendar_service import is_workday

from .assembly_queue_materialization import materialize_assembly_queue_lines
from .assembly_readiness_persistence import materialize_assembly_readiness
from .drum_saved_calendar import (
    DrumSavedCalendarError,
    saved_resource_daily_capacities,
    saved_resource_horizon_ends,
    saved_working_days,
)
from .drum_scheduler import AssemblyRateProfile, QueueLine, build_drum_plan


STAGE = "drum_schedule"
ALGORITHM_VERSION = "drum-schedule/13-build-day-calendar"


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


_READINESS_RANK = {"now": 0, "transfer": 1, "kitting": 2, "committed": 3, "launch": 4}


def _action_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        json.dumps(row.get(name), sort_keys=True, default=str)
        for name in (
            "action_kind", "item_id", "available_date", "confidence", "source_key",
            "source_warehouse_ref1c", "destination_warehouse_ref1c", "resource_id", "path",
        )
    )


def _slot_readiness_payload(
    readiness: models.AssemblyReadiness,
    phase: str,
    slot_qty: Decimal,
) -> tuple[date | None, list[dict[str, Any]], list[dict[str, Any]]]:
    curve = list(readiness.readiness_curve or [])
    phase_rank = _READINESS_RANK.get(str(phase), 99)
    tile_curve = [
        {
            "horizon": str(point.get("horizon") or ""),
            "cumulative_qty": str(slot_qty if _READINESS_RANK.get(str(point.get("horizon")), 99) >= phase_rank else Decimal("0")),
            "available_date": point.get("available_date"),
            # These are deliberately line-level explanations.  The tile owns
            # a bucket quantity, while the master also needs to see what blocks
            # the remaining saved queue line at every horizon.
            "actions": list(point.get("actions") or []),
            "required_actions": list(point.get("required_actions") or []),
            "blockers": list(point.get("blockers") or []),
        }
        for point in curve
    ]
    if phase_rank >= 99:
        return None, tile_curve, []
    current_index = next(
        (index for index, point in enumerate(curve) if str(point.get("horizon")) == phase),
        None,
    )
    if current_index is None:
        return None, tile_curve, []
    current = curve[current_index]
    previous = curve[current_index - 1] if current_index > 0 else {"cumulative_qty": "0", "actions": []}
    increment = max(_d(current.get("cumulative_qty")) - _d(previous.get("cumulative_qty")), Decimal("0"))
    if increment <= 0:
        return (
            date.fromisoformat(str(current["available_date"])) if current.get("available_date") else None,
            tile_curve,
            [],
        )
    previous_qty = {_action_key(row): _d(row.get("qty")) for row in list(previous.get("actions") or [])}
    ratio = slot_qty / increment
    actions: list[dict[str, Any]] = []
    for row in list(current.get("actions") or []):
        delta = max(_d(row.get("qty")) - previous_qty.get(_action_key(row), Decimal("0")), Decimal("0"))
        if delta <= 0:
            continue
        payload = dict(row)
        payload["qty"] = str((delta * ratio).quantize(Decimal("0.001")))
        actions.append(payload)
    return (
        date.fromisoformat(str(current["available_date"])) if current.get("available_date") else None,
        tile_curve,
        actions,
    )


def _rates_and_capacity(
    db: Session, queue_rows: list[models.AssemblyQueueLine]
) -> tuple[
    dict[int, tuple[AssemblyRateProfile, ...]],
    dict[int, Decimal],
    int,
    dict[int, int],
]:
    item_ids = sorted({int(row.item_id) for row in queue_rows})
    rate_rows = (
        db.query(models.AssemblyRate)
        .filter(models.AssemblyRate.item_id.in_(item_ids))
        .order_by(models.AssemblyRate.item_id, models.AssemblyRate.resource_id)
        .all()
        if item_ids
        else []
    )
    items = {int(item.item_id): item for item in db.query(models.Item)
             .filter(models.Item.item_id.in_(item_ids)).all()} if item_ids else {}
    rates: dict[int, list[AssemblyRateProfile]] = {}
    for row in rate_rows:
        item = items[int(row.item_id)]
        # A blank optimal batch explicitly excludes this item from the drum.
        # AssemblyRate owns the resource binding only; its legacy numeric
        # column must never override the shared item setting.
        if item.optimal_batch is None:
            continue
        rates.setdefault(int(row.item_id), []).append(
            AssemblyRateProfile(
                resource_id=int(row.resource_id),
                qty_per_capacity=_d(item.optimal_batch),
            )
        )
    normalized = {item_id: tuple(values) for item_id, values in rates.items()}
    for item_id in item_ids:
        profiles = normalized.get(item_id, ())
        # A missing rate means that the queue row is outside the drum contour.
        # It remains in AssemblyQueueLine and can still be closed by an accepted
        # physical output.  Ambiguous or invalid configured rates remain a hard
        # data error: silently choosing between two resources would be unsafe.
        if len(profiles) > 1:
            raise ValueError(f"ambiguous assembly rate for item {item_id}")
        if profiles and _d(profiles[0].qty_per_capacity) <= 0:
            raise ValueError(f"invalid assembly rate for item {item_id}")

    resource_ids = sorted(
        {profile.resource_id for values in normalized.values() for profile in values}
    )
    resources = (
        db.query(models.ProductionResource)
        .filter(models.ProductionResource.resource_id.in_(resource_ids))
        .all()
        if resource_ids
        else []
    )
    capacity = {int(row.resource_id): _d(row.capacity) for row in resources}
    if set(resource_ids) != set(capacity):
        raise ValueError("assembly rate references missing production resource")
    # Every resource keeps its own planning range; the schedule window is the
    # widest of them, but a resource never spills demand past its own horizon.
    horizon_by_resource = {
        int(row.resource_id): max(int(row.planning_range or 0), 1) for row in resources
    }
    horizon = max(list(horizon_by_resource.values()) or [1])
    return normalized, capacity, horizon, horizon_by_resource


def _rates_and_capacity_for_items(
    db: Session, item_ids: set[int]
) -> tuple[
    dict[int, tuple[AssemblyRateProfile, ...]],
    dict[int, Decimal],
    int,
    dict[int, int],
]:
    """Resolve the same rate/resource contour without queue staging rows."""

    ids = sorted({int(value) for value in item_ids})
    rate_rows = (
        db.query(models.AssemblyRate)
        .filter(models.AssemblyRate.item_id.in_(ids))
        .order_by(models.AssemblyRate.item_id, models.AssemblyRate.resource_id)
        .all()
        if ids else []
    )
    items = {
        int(item.item_id): item
        for item in (
            db.query(models.Item).filter(models.Item.item_id.in_(ids)).all()
            if ids else []
        )
    }
    rates: dict[int, list[AssemblyRateProfile]] = {}
    for row in rate_rows:
        item = items.get(int(row.item_id))
        if item is None:
            raise ValueError(f"assembly rate references missing item {int(row.item_id)}")
        if item.optimal_batch is None:
            continue
        rates.setdefault(int(row.item_id), []).append(
            AssemblyRateProfile(
                resource_id=int(row.resource_id),
                qty_per_capacity=_d(item.optimal_batch),
            )
        )
    normalized = {item_id: tuple(values) for item_id, values in rates.items()}
    for item_id, profiles in normalized.items():
        if len(profiles) > 1:
            raise ValueError(f"ambiguous assembly rate for item {item_id}")
        if profiles and _d(profiles[0].qty_per_capacity) <= 0:
            raise ValueError(f"invalid assembly rate for item {item_id}")
    resource_ids = sorted(
        {profile.resource_id for values in normalized.values() for profile in values}
    )
    resources = (
        db.query(models.ProductionResource)
        .filter(models.ProductionResource.resource_id.in_(resource_ids))
        .all()
        if resource_ids else []
    )
    capacity = {int(row.resource_id): _d(row.capacity) for row in resources}
    if set(resource_ids) != set(capacity):
        raise ValueError("assembly rate references missing production resource")
    horizon_by_resource = {
        int(row.resource_id): max(int(row.planning_range or 0), 1)
        for row in resources
    }
    horizon = max(list(horizon_by_resource.values()) or [1])
    return normalized, capacity, horizon, horizon_by_resource


def _planning_start(generation: models.LedgerGeneration) -> date:
    # The fact cutoff and the calendar epoch are independent. A retained-plan
    # refresh keeps yesterday's physical facts but starts a new calendar today.
    # Persisted generation creation time makes retries deterministic.
    def moscow_day(value: datetime) -> date:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(ZoneInfo("Europe/Moscow")).date()
    return max(moscow_day(generation.cutoff), moscow_day(generation.created_at))


def _plan(
    db: Session,
    generation: models.LedgerGeneration,
    queue_rows: list[models.AssemblyQueueLine],
):
    rates, capacity, horizon, horizon_by_resource = _rates_and_capacity(db, queue_rows)
    readiness_rows = (
        db.query(models.AssemblyReadiness)
        .filter(models.AssemblyReadiness.ledger_generation_id == int(generation.id))
        .all()
    )
    readiness_by_line = {
        int(row.assembly_queue_line_id): row for row in readiness_rows
    }
    if set(readiness_by_line) != {int(row.id) for row in queue_rows}:
        raise ValueError("assembly readiness does not cover the open assembly queue")
    scheduled_rows = [row for row in queue_rows if int(row.item_id) in rates]
    excluded_rows = [row for row in queue_rows if int(row.item_id) not in rates]
    schedule_from = _planning_start(generation)
    schedule_to = schedule_from + timedelta(days=horizon - 1)
    resource_horizon_end = {
        resource_id: schedule_from + timedelta(days=days - 1)
        for resource_id, days in horizon_by_resource.items()
    }
    calendar: dict[date, bool] = {}
    cursor = schedule_from
    while cursor <= schedule_to:
        calendar[cursor] = is_workday(db, cursor)
        cursor += timedelta(days=1)
    plan = build_drum_plan(
        tuple(
            QueueLine(
                queue_line_id=int(row.id),
                plan_id=int(row.plan_id),
                plan_line_id=int(row.plan_line_id),
                item_id=int(row.item_id),
                sort_key=row.sort_key,
                planned_output_qty=_d(row.planned_output_qty),
                accepted_plan_output_qty=_d(row.accepted_plan_output_qty),
                original_priority=tuple(row.original_priority or ()),
                assembly_remaining_qty=_d(row.assembly_remaining_qty),
                ready_qty=_d(readiness_by_line[int(row.id)].ready_qty),
                readiness_status=str(readiness_by_line[int(row.id)].status),
                readiness_curve=tuple(
                    (
                        str(point.get("horizon") or ""),
                        _d(point.get("cumulative_qty")),
                        date.fromisoformat(str(point["available_date"]))
                        if point.get("available_date") else None,
                    )
                    for point in list(readiness_by_line[int(row.id)].readiness_curve or [])
                ),
            )
            for row in scheduled_rows
        ),
        rates,
        calendar,
        schedule_from=schedule_from,
        schedule_to=schedule_to,
        resource_capacity_by_id=capacity,
        resource_horizon_end_by_id=resource_horizon_end,
    )
    excluded_open_qty = sum(
        (_d(row.assembly_remaining_qty) for row in excluded_rows), Decimal("0")
    )
    return type(plan)(
        schedule_from=plan.schedule_from,
        schedule_to=plan.schedule_to,
        working_days=plan.working_days,
        resource_horizon_ends=plan.resource_horizon_ends,
        resource_daily_capacities=plan.resource_daily_capacities,
        slots=plan.slots,
        gaps=plan.gaps,
        queue_signature=plan.queue_signature,
        slot_signature=plan.slot_signature,
        gap_signature=plan.gap_signature,
        metrics={
            **dict(plan.metrics),
            "queue_lines": len(queue_rows),
            "excluded_lines": len(excluded_rows),
            "excluded_open_qty": str(excluded_open_qty),
            "excluded_item_ids": sorted({int(row.item_id) for row in excluded_rows}),
        },
    )


def _compact_slot_readiness_payload(
    readiness: Mapping[str, Any],
    phase: str,
    slot_qty: Decimal,
) -> tuple[date | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply the persisted drum tile projection to an in-memory DTO."""

    curve = list(readiness.get("readiness_curve") or [])
    phase_rank = _READINESS_RANK.get(str(phase), 99)
    tile_curve = [
        {
            "horizon": str(point.get("horizon") or ""),
            "cumulative_qty": str(
                slot_qty
                if _READINESS_RANK.get(str(point.get("horizon")), 99) >= phase_rank
                else Decimal("0")
            ),
            "available_date": point.get("available_date"),
            "actions": list(point.get("actions") or []),
            "required_actions": list(point.get("required_actions") or []),
            "blockers": list(point.get("blockers") or []),
        }
        for point in curve
    ]
    if phase_rank >= 99:
        return None, tile_curve, []
    current_index = next(
        (index for index, point in enumerate(curve) if str(point.get("horizon")) == phase),
        None,
    )
    if current_index is None:
        return None, tile_curve, []
    current = curve[current_index]
    previous = curve[current_index - 1] if current_index > 0 else {"cumulative_qty": "0", "actions": []}
    increment = max(
        _d(current.get("cumulative_qty")) - _d(previous.get("cumulative_qty")),
        Decimal("0"),
    )
    current_date = (
        date.fromisoformat(str(current["available_date"]))
        if current.get("available_date") else None
    )
    if increment <= 0:
        return current_date, tile_curve, []
    previous_qty = {
        _action_key(row): _d(row.get("qty"))
        for row in list(previous.get("actions") or [])
    }
    ratio = slot_qty / increment
    actions: list[dict[str, Any]] = []
    for row in list(current.get("actions") or []):
        delta = max(
            _d(row.get("qty")) - previous_qty.get(_action_key(row), Decimal("0")),
            Decimal("0"),
        )
        if delta <= 0:
            continue
        action = dict(row)
        action["qty"] = str((delta * ratio).quantize(Decimal("0.001")))
        actions.append(action)
    return current_date, tile_curve, actions


# --------------------------------------------------------------------------
# One owner for the readiness curve
# --------------------------------------------------------------------------
#
# A drum tile is a *bucket of a queue line*: a date, a resource, a quantity, an
# ordinal and the readiness phase the scheduler placed it in.  Everything that
# explains *why* that phase holds — the readiness curve, the blockers, the
# required actions and the unavailable reasons — belongs to the readiness row
# of the same ``plan_line_id`` and is owned there.
#
# Until now every drum current row carried its own copy of that explanation.
# Measured on a production-size copy, the 205 accepted drum current rows
# carried 92.9 MB of JSON of which 99.98 % was those copies, re-published on
# every hourly refresh.  Worse, a drum row whose business fields did not change
# keeps its saved payload, so its embedded copy silently aged out of step with
# the readiness row the operator reads on the neighbouring screen: on that same
# copy 112 of the 205 rows were already showing an older readiness state than
# /assembly-readiness did.
#
# The drum row therefore stores only its own business fields plus
# ``readiness_ref``/``plan_line_id``, and the presentation view below is
# resolved from the readiness current row at read time.  There is exactly one
# projection formula (``_compact_slot_readiness_payload``) and exactly one
# stored copy of the curve.

DRUM_READINESS_VIEW_KEYS: dict[str, tuple[str, ...]] = {
    "drum_slot": (
        "readiness_date", "readiness_curve", "action_manifest",
        "unavailable_reasons", "blocking_manifest",
    ),
    "drum_gap": (
        "readiness_date", "readiness_curve", "action_manifest",
        "unavailable_reasons", "blocking_manifest",
    ),
    "drum_excluded": (
        "readiness_status", "readiness_date", "readiness_curve",
        "action_manifest", "unavailable_reasons", "blocking_manifest",
    ),
}

_DRUM_READINESS_QTY_KEY = {"drum_slot": "slot_qty", "drum_gap": "gap_qty"}


class DrumReadinessLinkMissing(LookupError):
    """The readiness current row that owns a drum row's curve is absent."""


def readiness_ref_for_plan_line(plan_line_id: int) -> str:
    """Stable link from a drum row to its readiness current row."""

    return f"plan-line:{int(plan_line_id)}"


def drum_readiness_view(
    readiness: Mapping[str, Any] | None,
    *,
    entity_kind: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve a drum row's readiness presentation from its readiness owner.

    ``readiness`` is the ``assembly_readiness`` current payload of the same
    ``plan_line_id``.  A missing owner is fail-closed (R8): an empty curve is
    never fabricated, because "nothing blocks this tile" and "we do not know
    what blocks this tile" are different answers.
    """

    kind = str(entity_kind)
    if kind not in DRUM_READINESS_VIEW_KEYS:
        raise ValueError(f"unknown drum entity kind: {kind or '<missing>'}")
    if readiness is None:
        raise DrumReadinessLinkMissing(
            "drum row has no assembly readiness owner for plan line "
            f"{payload.get('plan_line_id')}"
        )
    if kind == "drum_excluded":
        # A queue line without a takt never enters the calendar, so it has no
        # tile bucket to prorate: it shows the line-level readiness verbatim.
        return {
            "readiness_status": str(readiness.get("status") or "unavailable"),
            "readiness_date": readiness.get("readiness_date"),
            "readiness_curve": list(readiness.get("readiness_curve") or []),
            "action_manifest": list(readiness.get("action_manifest") or []),
            "unavailable_reasons": list(readiness.get("unavailable_reasons") or []),
            "blocking_manifest": list(readiness.get("blocking_manifest") or []),
        }
    qty = _d(payload.get(_DRUM_READINESS_QTY_KEY[kind]))
    readiness_date, curve, actions = _compact_slot_readiness_payload(
        readiness, str(payload.get("readiness_phase") or ""), qty
    )
    return {
        "readiness_date": readiness_date.isoformat() if readiness_date else None,
        "readiness_curve": curve,
        "action_manifest": actions,
        "unavailable_reasons": list(readiness.get("unavailable_reasons") or []),
        "blocking_manifest": list(readiness.get("blocking_manifest") or []),
    }


def published_readiness_plan_lines(
    db: Session, *, scope_key: str = "assembly:all-live-plans"
) -> set[int]:
    """Plan lines that have an accepted readiness current row, identities only.

    Deliberately not a payload read: the whole-scope fail-closed check must not
    drag the heavy curves of every line through the GET.
    """

    rows = db.query(models.CurrentExecutionRow.business_identity).filter(
        models.CurrentExecutionRow.entity_kind == "assembly_readiness",
        models.CurrentExecutionRow.scope_key == str(scope_key),
        models.CurrentExecutionRow.result_status == "accepted",
        models.CurrentExecutionRow.result_ready.is_(True),
    ).all()
    plan_lines: set[int] = set()
    for (identity,) in rows:
        text = str(identity or "")
        if not text.startswith("plan-line:"):
            continue
        try:
            plan_lines.add(int(text.split(":", 1)[1]))
        except (IndexError, ValueError):
            continue
    return plan_lines


def load_readiness_payloads(
    db: Session,
    plan_line_ids: Any,
    *,
    scope_key: str = "assembly:all-live-plans",
) -> dict[int, dict[str, Any]]:
    """Read the readiness current payloads of exactly the requested plan lines."""

    wanted = sorted({int(value) for value in plan_line_ids})
    if not wanted:
        return {}
    identities = [readiness_ref_for_plan_line(value) for value in wanted]
    rows = db.query(models.CurrentExecutionRow).filter(
        models.CurrentExecutionRow.entity_kind == "assembly_readiness",
        models.CurrentExecutionRow.scope_key == str(scope_key),
        models.CurrentExecutionRow.result_status == "accepted",
        models.CurrentExecutionRow.result_ready.is_(True),
        models.CurrentExecutionRow.business_identity.in_(identities),
    ).all()
    resolved: dict[int, dict[str, Any]] = {}
    for row in rows:
        payload = dict(row.payload or {})
        if payload.get("plan_line_id") is None:
            continue
        resolved[int(payload["plan_line_id"])] = payload
    return resolved


@dataclass(frozen=True)
class CompactDrumSchedulePayload:
    """Validated drum DTOs with stable plan-line identities and no staging ids."""

    target_generation_id: int
    parent_generation_id: int
    rows: tuple[dict[str, Any], ...]
    metrics: dict[str, Any]


def build_compact_current_drum_payload(
    db: Session,
    *,
    target_generation_id: int,
    parent_generation_id: int,
    assembly_payload: Any,
) -> CompactDrumSchedulePayload:
    """Build drum schedule/slot/gap DTOs from compact queue/readiness payloads.

    QueueLine ids passed to the pure scheduler are negative ephemeral keys.  No
    such key is returned: every output row carries ``plan_line_id`` and the
    stable ``queue_owner_identity``.  ``resolve_compact_queue_owner_ids`` in
    ``current_execution`` may add the actual CurrentExecutionRow id after the
    queue scope has been published.
    """

    target = db.get(models.LedgerGeneration, int(target_generation_id))
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    if target is None or str(target.status or "") != "building":
        raise ValueError("compact drum payload requires a BUILDING target")
    if parent is None or str(parent.status or "") != "accepted":
        raise ValueError("compact drum payload requires an accepted parent")
    if int(target.id) == int(parent.id) or target.cutoff is None:
        raise ValueError("compact drum payload target boundary is invalid")
    queue_rows = list(getattr(assembly_payload, "queue_rows", ()) or ())
    readiness_rows = list(getattr(assembly_payload, "readiness_rows", ()) or ())
    readiness_by_line: dict[int, Mapping[str, Any]] = {}
    for raw in readiness_rows:
        payload = raw.get("payload") if isinstance(raw, Mapping) else None
        if not isinstance(payload, Mapping):
            raise ValueError("compact readiness row is malformed")
        line_id = int(payload.get("plan_line_id") or 0)
        if line_id <= 0 or line_id in readiness_by_line:
            raise ValueError("compact readiness has duplicate plan line")
        readiness_by_line[line_id] = payload

    queue_lines: list[QueueLine] = []
    queue_by_line: dict[int, Mapping[str, Any]] = {}
    for raw in queue_rows:
        payload = raw.get("payload") if isinstance(raw, Mapping) else None
        identity = str(raw.get("business_identity") or "") if isinstance(raw, Mapping) else ""
        if not isinstance(payload, Mapping) or not identity.startswith("plan-line:"):
            raise ValueError("compact queue row is malformed")
        plan_line_id = int(payload.get("plan_line_id") or 0)
        if plan_line_id <= 0 or plan_line_id in queue_by_line:
            raise ValueError("compact queue has duplicate plan line")
        readiness = readiness_by_line.get(plan_line_id)
        if readiness is None:
            raise ValueError(f"compact drum readiness missing plan line {plan_line_id}")
        queue_by_line[plan_line_id] = payload
        queue_lines.append(
            QueueLine(
                queue_line_id=-plan_line_id,
                plan_id=int(payload["plan_id"]),
                plan_line_id=plan_line_id,
                item_id=int(payload["item_id"]),
                sort_key=str(payload["sort_key"]),
                planned_output_qty=_d(payload["planned_output_qty"]),
                accepted_plan_output_qty=_d(payload["accepted_plan_output_qty"]),
                original_priority=tuple(payload.get("original_priority") or ()),
                assembly_remaining_qty=_d(payload["assembly_remaining_qty"]),
                ready_qty=_d(readiness.get("ready_qty")),
                readiness_status=str(readiness.get("status") or "unavailable"),
                readiness_curve=tuple(
                    (
                        str(point.get("horizon") or ""),
                        _d(point.get("cumulative_qty")),
                        date.fromisoformat(str(point["available_date"]))
                        if point.get("available_date") else None,
                    )
                    for point in list(readiness.get("readiness_curve") or [])
                ),
            )
        )

    item_ids = {int(row.item_id) for row in queue_lines}
    rates, capacity, horizon, horizon_by_resource = _rates_and_capacity_for_items(db, item_ids)
    schedule_from = _planning_start(target)
    schedule_to = schedule_from + timedelta(days=horizon - 1)
    calendar: dict[date, bool] = {}
    cursor = schedule_from
    while cursor <= schedule_to:
        calendar[cursor] = is_workday(db, cursor)
        cursor += timedelta(days=1)
    resource_horizon_end = {
        resource_id: schedule_from + timedelta(days=days - 1)
        for resource_id, days in horizon_by_resource.items()
    }
    scheduled = tuple(row for row in queue_lines if int(row.item_id) in rates)
    plan = build_drum_plan(
        scheduled,
        rates,
        calendar,
        schedule_from=schedule_from,
        schedule_to=schedule_to,
        resource_capacity_by_id=capacity,
        resource_horizon_end_by_id=resource_horizon_end,
    )
    excluded = tuple(row for row in queue_lines if int(row.item_id) not in rates)
    metrics = {
        **dict(plan.metrics),
        "queue_lines": len(queue_lines),
        "excluded_lines": len(excluded),
        "excluded_open_qty": str(sum((_d(row.assembly_remaining_qty) for row in excluded), Decimal("0"))),
        "excluded_item_ids": sorted({int(row.item_id) for row in excluded}),
    }
    rows: list[dict[str, Any]] = [{
        "entity_kind": "drum_schedule",
        "business_identity": "drum:all-live-plans",
        "scope_key": "drum:all-live-plans",
        "payload": {
            "schedule_from": plan.schedule_from.isoformat(),
            "schedule_to": plan.schedule_to.isoformat(),
            "working_days": [value.isoformat() for value in plan.working_days],
            "resource_horizon_ends": {str(key): value.isoformat() for key, value in plan.resource_horizon_ends},
            "resource_daily_capacities": {str(key): str(value) for key, value in plan.resource_daily_capacities},
            "queue_signature": plan.queue_signature,
            "slot_signature": plan.slot_signature,
            "gap_signature": plan.gap_signature,
            "metrics": metrics,
        },
    }]
    manual_rows = {
        str(row.business_identity): dict(row.manual_input or {})
        for row in db.query(models.CurrentExecutionRow).filter(
            models.CurrentExecutionRow.entity_kind == "drum_slot",
            models.CurrentExecutionRow.result_status == "accepted",
        ).all()
        if row.manual_input
    }
    for slot in plan.slots:
        line = int(slot.plan_line_id)
        identity = f"slot:plan-line:{line}:ordinal:{int(slot.slot_ordinal)}"
        payload = {
            "queue_owner_identity": f"plan-line:{line}",
            "readiness_ref": readiness_ref_for_plan_line(line),
            "plan_id": int(slot.plan_id), "plan_line_id": line,
            "run_id": int(queue_by_line[line]["run_id"]),
            "item_id": int(slot.item_id), "resource_id": int(slot.resource_id),
            "slot_date": slot.slot_date.isoformat(), "auto_slot_date": slot.slot_date.isoformat(),
            "slot_qty": str(slot.slot_qty), "capacity_load": str(slot.capacity_load),
            "planned_output_qty": str(slot.planned_output_qty),
            "accepted_plan_output_qty": str(slot.accepted_plan_output_qty),
            "assembly_remaining_qty": str(slot.assembly_remaining_qty),
            "slot_ordinal": int(slot.slot_ordinal),
            "readiness_phase": str(slot.readiness_phase),
            "original_priority": list(slot.original_priority),
        }
        manual = manual_rows.get(identity)
        if manual:
            if manual.get("slot_date"):
                payload["slot_date"] = str(manual["slot_date"])
            if manual.get("resource_id") is not None:
                payload["resource_id"] = int(manual["resource_id"])
        rows.append({
            "entity_kind": "drum_slot", "business_identity": identity,
            "scope_key": "drum:all-live-plans", "payload": payload,
            **({"manual_input": manual} if manual else {}),
        })
    for gap in plan.gaps:
        line = int(gap.plan_line_id)
        rows.append({
            "entity_kind": "drum_gap",
            "business_identity": f"gap:plan-line:{line}:date:{gap.gap_date.isoformat()}",
            "scope_key": "drum:all-live-plans",
            "payload": {
                "queue_owner_identity": f"plan-line:{line}",
                "readiness_ref": readiness_ref_for_plan_line(line),
                "plan_id": int(gap.plan_id), "plan_line_id": line,
                "run_id": int(queue_by_line[line]["run_id"]),
                "item_id": int(gap.item_id), "resource_id": int(gap.resource_id),
                "gap_date": gap.gap_date.isoformat(), "required_qty": str(gap.required_qty),
                "available_capacity": str(gap.available_capacity), "gap_qty": str(gap.gap_qty),
                "readiness_phase": str(gap.readiness_phase),
                "original_priority": list(gap.original_priority),
            },
        })
    for queue in excluded:
        line = int(queue.plan_line_id)
        rows.append({
            "entity_kind": "drum_excluded",
            "business_identity": f"excluded:plan-line:{line}",
            "scope_key": "drum:all-live-plans",
            "payload": {
                "queue_owner_identity": f"plan-line:{line}",
                "readiness_ref": readiness_ref_for_plan_line(line),
                "plan_id": int(queue.plan_id), "plan_line_id": line,
                "run_id": int(queue_by_line[line]["run_id"]), "item_id": int(queue.item_id),
                "period_from": str(queue_by_line[line]["period_from"]),
                "period_to": str(queue_by_line[line]["period_to"]),
                "planned_output_qty": str(queue.planned_output_qty),
                "accepted_plan_output_qty": str(queue.accepted_plan_output_qty),
                "assembly_remaining_qty": str(queue.assembly_remaining_qty),
                "reason": "ASSEMBLY_RATE_MISSING",
                "original_priority": list(queue.original_priority),
            },
        })
    return CompactDrumSchedulePayload(
        target_generation_id=int(target.id), parent_generation_id=int(parent.id),
        rows=tuple(rows), metrics=metrics,
    )


def _validate_persisted_checkpoint(
    db: Session,
    schedule: models.DrumSchedule,
    batch: models.LedgerBuildBatch,
) -> None:
    slots = db.query(models.DrumSlot).filter(
        models.DrumSlot.drum_schedule_id == int(schedule.id)
    ).all()
    slot_count = len(slots)
    gap_count = db.query(models.DrumCapacityGap).filter(
        models.DrumCapacityGap.drum_schedule_id == int(schedule.id)
    ).count()
    try:
        saved_working_days(schedule)
        resource_horizons = saved_resource_horizon_ends(schedule)
        resource_capacities = saved_resource_daily_capacities(schedule)
    except DrumSavedCalendarError as exc:
        raise ValueError(
            "persisted drum checkpoint has invalid saved inputs"
        ) from exc
    invalid_saved_slot = any(
        row.capacity_load is None
        or _d(row.capacity_load) <= 0
        or int(row.resource_id) not in resource_horizons
        or int(row.resource_id) not in resource_capacities
        for row in slots
    )
    if (
        schedule.status != "completed"
        or batch.status != "completed"
        or schedule.algorithm_version != ALGORITHM_VERSION
        or batch.algorithm_version != ALGORITHM_VERSION
        or slot_count != int(schedule.slot_row_count)
        or gap_count != int(schedule.gap_row_count)
        or invalid_saved_slot
        or dict(batch.metrics or {}) != dict(schedule.metrics or {})
        or _d(schedule.total_open_qty)
        != _d(schedule.total_slot_qty) + _d(schedule.total_gap_qty)
    ):
        raise ValueError("persisted drum checkpoint is incomplete or inconsistent")


def materialize_drum_schedule(
    db: Session, ledger_generation_id: int
) -> dict[str, Any]:
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise ValueError(f"LedgerGeneration {ledger_generation_id} not found")
    if generation.status != "building":
        raise ValueError("drum schedule requires a BUILDING generation")
    if generation.cutoff is None:
        raise ValueError("drum schedule requires generation cutoff")

    existing = (
        db.query(models.DrumSchedule)
        .filter(models.DrumSchedule.ledger_generation_id == int(generation.id))
        .one_or_none()
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
    if existing is not None or batch is not None:
        if existing is None or batch is None:
            raise ValueError("partial drum checkpoint exists")
        _validate_persisted_checkpoint(db, existing, batch)
        return {
            "ledger_generation_id": int(generation.id),
            "schedule_id": int(existing.id),
            "batch_id": int(batch.id),
            **dict(existing.metrics or {}),
        }

    queue_rows = [
        row
        for row in materialize_assembly_queue_lines(db, int(generation.id))
        if _d(row.assembly_remaining_qty) > 0
    ]
    materialize_assembly_readiness(db, int(generation.id))
    plan = _plan(db, generation, queue_rows)

    schedule = models.DrumSchedule(
        ledger_generation_id=int(generation.id),
        status="completed",
        algorithm_version=ALGORITHM_VERSION,
        schedule_from=plan.schedule_from,
        schedule_to=plan.schedule_to,
        working_days=[value.isoformat() for value in plan.working_days],
        resource_horizon_ends={
            str(resource_id): end_date.isoformat()
            for resource_id, end_date in plan.resource_horizon_ends
        },
        resource_daily_capacities={
            str(resource_id): str(capacity)
            for resource_id, capacity in plan.resource_daily_capacities
        },
        queue_signature=plan.queue_signature,
        slot_signature=plan.slot_signature,
        gap_signature=plan.gap_signature,
        slot_row_count=len(plan.slots),
        gap_row_count=len(plan.gaps),
        total_open_qty=_d(plan.metrics["total_open_qty"]),
        total_slot_qty=_d(plan.metrics["total_slot_qty"]),
        total_gap_qty=_d(plan.metrics["total_gap_qty"]),
        metrics=dict(plan.metrics),
    )
    db.add(schedule)
    db.flush()
    readiness_by_line = {
        int(row.assembly_queue_line_id): row
        for row in db.query(models.AssemblyReadiness)
        .filter(models.AssemblyReadiness.ledger_generation_id == int(generation.id))
        .all()
    }
    for slot in plan.slots:
        readiness = readiness_by_line[int(slot.queue_line_id)]
        readiness_date, tile_curve, tile_actions = _slot_readiness_payload(
            readiness, slot.readiness_phase, slot.slot_qty
        )
        db.add(
            models.DrumSlot(
                drum_schedule_id=int(schedule.id),
                assembly_queue_line_id=int(slot.queue_line_id),
                plan_id=int(slot.plan_id),
                plan_line_id=int(slot.plan_line_id),
                item_id=int(slot.item_id),
                resource_id=int(slot.resource_id),
                slot_date=slot.slot_date,
                auto_slot_date=slot.slot_date,
                auto_resource_id=int(slot.resource_id),
                slot_qty=slot.slot_qty,
                capacity_load=slot.capacity_load,
                planned_output_qty=slot.planned_output_qty,
                accepted_plan_output_qty=slot.accepted_plan_output_qty,
                assembly_remaining_qty=slot.assembly_remaining_qty,
                slot_ordinal=int(slot.slot_ordinal),
                original_priority=list(slot.original_priority),
                readiness_phase=slot.readiness_phase,
                readiness_date=readiness_date,
                readiness_curve=tile_curve,
                action_manifest=tile_actions,
                unavailable_reasons=list(readiness.unavailable_reasons or []),
                blocking_manifest=(
                    list(readiness.blocking_manifest or [])
                    if slot.readiness_phase in {"blocked", "unavailable"}
                    else []
                ),
            )
        )
    for gap in plan.gaps:
        readiness = readiness_by_line[int(gap.queue_line_id)]
        readiness_date, gap_curve, gap_actions = _slot_readiness_payload(
            readiness, gap.readiness_phase, gap.gap_qty
        )
        if gap.readiness_phase in {"blocked", "unavailable", "mixed"}:
            readiness_date = readiness.readiness_date
            gap_curve = list(readiness.readiness_curve or [])
            gap_actions = list(readiness.action_manifest or [])
        db.add(
            models.DrumCapacityGap(
                drum_schedule_id=int(schedule.id),
                assembly_queue_line_id=int(gap.queue_line_id),
                plan_id=int(gap.plan_id),
                plan_line_id=int(gap.plan_line_id),
                item_id=int(gap.item_id),
                resource_id=int(gap.resource_id),
                gap_date=gap.gap_date,
                required_qty=gap.required_qty,
                available_capacity=gap.available_capacity,
                gap_qty=gap.gap_qty,
                planned_output_qty=gap.planned_output_qty,
                accepted_plan_output_qty=gap.accepted_plan_output_qty,
                assembly_remaining_qty=gap.assembly_remaining_qty,
                original_priority=list(gap.original_priority),
                readiness_phase=gap.readiness_phase,
                readiness_date=readiness_date,
                readiness_curve=gap_curve,
                action_manifest=gap_actions,
                unavailable_reasons=list(readiness.unavailable_reasons or []),
                blocking_manifest=list(readiness.blocking_manifest or []),
            )
        )
    batch = models.LedgerBuildBatch(
        ledger_generation_id=int(generation.id),
        stage=STAGE,
        batch_key=batch_key,
        status="completed",
        algorithm_version=ALGORITHM_VERSION,
        metrics=dict(plan.metrics),
        completed_at=datetime.now(timezone.utc),
    )
    db.add(batch)
    db.flush()
    return {
        "ledger_generation_id": int(generation.id),
        "schedule_id": int(schedule.id),
        "batch_id": int(batch.id),
        **dict(plan.metrics),
    }

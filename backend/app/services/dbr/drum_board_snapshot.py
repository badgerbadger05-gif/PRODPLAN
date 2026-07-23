"""Immutable, Ledger-bound DBR drum board read model.

The legacy board projection reads the mutable active schedule and item master
on every GET.  This module is deliberately split into a candidate writer and
an accepted-generation reader: the HTTP route never imports ``board_service``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app import models
from app.services.planning_truth import (
    CAPABILITY_DBR_DRUM_BOARD,
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_RESERVATION_REPLAY,
    PlanningTruthUnavailable,
    get_latest_read_snapshot,
    get_truth_state,
)


CONSUMER = "dbr_drum_board"
SNAPSHOT_KEY_PREFIX = "board:"
REQUIRED_CAPABILITIES = (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_RESERVATION_REPLAY,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_DBR_DRUM_BOARD,
)


class DbrDrumBoardCandidateError(RuntimeError):
    """The building generation has no single exact active drum board."""


class DbrDrumBoardSnapshotUnavailable(RuntimeError):
    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(str(detail.get("reason") or "DBR drum board unavailable"))

    def as_dict(self) -> dict[str, Any]:
        return dict(self.detail)


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json(val) for key, val in sorted(value.items(), key=lambda row: str(row[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_json(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _exact_runs(db: Session, generation_id: int, *, accepted: bool) -> dict[int, int]:
    rows = db.query(models.PlanningRun).filter(
        models.PlanningRun.ledger_generation_id == int(generation_id),
        models.PlanningRun.status == ("FIXED_SNAPSHOT" if accepted else "BUILDING_SNAPSHOT"),
    ).all()
    result: dict[int, int] = {}
    for row in rows:
        if row.active_freeze_version is None:
            raise DbrDrumBoardCandidateError("fixed run has no freeze_version")
        result[int(row.run_id)] = int(row.active_freeze_version)
    if not result:
        raise DbrDrumBoardCandidateError("generation has no fixed MRP run lineage")
    return result


def _exact_active_schedule(
    db: Session, generation: models.LedgerGeneration, runs: dict[int, int],
) -> models.DbrDrumSchedule | None:
    rows = db.query(models.DbrDrumSchedule).filter(
        models.DbrDrumSchedule.status == "active",
        models.DbrDrumSchedule.ledger_generation_id == int(generation.id),
    ).all()
    if not rows:
        return None
    if len(rows) != 1:
        raise DbrDrumBoardCandidateError("generation has multiple active drum schedules")
    schedule = rows[0]
    markers = list(schedule.covered_programs)
    if not markers:
        raise DbrDrumBoardCandidateError("active drum schedule has no frozen program lineage")
    for marker in markers:
        if (
            marker.source_run_id is None
            or marker.ledger_generation_id != generation.id
            or marker.freeze_version is None
            or runs.get(int(marker.source_run_id)) != int(marker.freeze_version)
        ):
            raise DbrDrumBoardCandidateError("active drum program has foreign or stale lineage")
    return schedule


def build_drum_board_candidate_snapshot(
    db: Session, generation_id: int,
) -> models.PlanningReadSnapshot | None:
    """Capture one active board with exact Ledger/run/freeze lineage.

    ``None`` is an intentional, non-error result: Ledger/MRP publication must
    remain possible before a schedule for the new generation is built.
    """
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or str(generation.status) not in {"building", "accepted"} or generation.cutoff is None:
        raise DbrDrumBoardCandidateError("drum board snapshot requires a Ledger generation with cutoff")
    accepted = str(generation.status) == "accepted"
    # Absence is expected during a refresh: a schedule cannot normally be
    # activated for an unpublished generation.  Do not make Ledger/MRP
    # publication depend on the run query in this branch.
    schedule_rows = db.query(models.DbrDrumSchedule).filter(
        models.DbrDrumSchedule.status == "active",
        models.DbrDrumSchedule.ledger_generation_id == int(generation.id),
    ).all()
    if not schedule_rows:
        return None
    runs = _exact_runs(db, int(generation.id), accepted=accepted)
    schedule = _exact_active_schedule(db, generation, runs)
    if schedule is None:
        return None

    resource_rows = {int(row.resource_id): str(row.resource_name or "") for row in db.query(models.ProductionResource).all()}
    item_rows = {int(row.item_id): (row.item_code, row.item_name) for row in db.query(models.Item).all()}
    slots: list[dict[str, Any]] = []
    days: set[date] = set()
    resources: dict[int, str] = {}
    # ``kit_status`` and ``produced_qty`` on the old schedule are mutable
    # mirrors fed by legacy gate/feedback code.  They are not Ledger facts and
    # must not become seemingly exact values merely by freezing this board.
    # Keep the schedule as a plan-only projection until slot-level Ledger
    # allocation exists.
    kpi = {
        "green": None, "yellow": None, "red": None, "unknown": None,
        "slots": 0, "plan_qty": 0.0, "fact_qty": None,
        "kit_gate_status": "unavailable", "execution_status": "unavailable",
    }
    for slot in schedule.slots:
        if (
            slot.ledger_generation_id != generation.id
            or slot.source_run_id is None
            or slot.freeze_version is None
            or runs.get(int(slot.source_run_id)) != int(slot.freeze_version)
        ):
            raise DbrDrumBoardCandidateError("active drum slot has foreign or stale lineage")
        code, name = item_rows.get(int(slot.item_id), (None, None))
        resource_name = resource_rows.get(int(slot.resource_id))
        if code is None or resource_name is None:
            raise DbrDrumBoardCandidateError("active drum slot references missing display master")
        days.add(slot.slot_date)
        resources[int(slot.resource_id)] = resource_name
        kpi["slots"] += 1
        kpi["plan_qty"] += float(slot.qty or 0)
        slots.append({
            "id": int(slot.id), "source_run_id": int(slot.source_run_id),
            "ledger_generation_id": int(slot.ledger_generation_id), "freeze_version": int(slot.freeze_version),
            "date": slot.slot_date, "planned_date": slot.planned_date,
            "resource_id": int(slot.resource_id), "resource_name": resource_name,
            "item_id": int(slot.item_id), "item_code": code, "item_name": name,
            "qty": float(slot.qty or 0), "produced_qty": None,
            "kit_status": "unknown", "kit_gate_status": "unavailable",
            "execution_status": "unavailable", "release_status": slot.release_status,
            "one_c_order_number": slot.one_c_order_number, "shortage": slot.shortage_json,
            "position": int(slot.position or 0),
        })
    gaps: list[dict[str, Any]] = []
    for gap in schedule.capacity_gaps:
        code, name = item_rows.get(int(gap.item_id), (None, None)) if gap.item_id is not None else (None, None)
        resource_name = resource_rows.get(int(gap.resource_id)) if gap.resource_id is not None else None
        gaps.append({
            "id": int(gap.id), "date": gap.gap_date, "resource_id": gap.resource_id,
            "resource_name": resource_name, "item_id": gap.item_id, "item_code": code, "item_name": name,
            "required_qty": float(gap.required_qty or 0), "takt_qty": float(gap.takt_qty or 0),
            "gap_qty": float(gap.gap_qty or 0), "resolution": gap.resolution,
        })
    slots.sort(key=lambda row: (row["date"], row["resource_name"], row["position"], row["id"]))
    gaps.sort(key=lambda row: (row["date"] or date.min, row["resource_name"] or "", row["id"]))
    payload = _json({
        "meta": {
            "ledger_generation": int(generation.id), "ledger_generation_id": int(generation.id),
            "cutoff": generation.cutoff, "truth_status": "accepted" if accepted else "building", "read_only": True,
            "runs": [{"run_id": run_id, "freeze_version": freeze} for run_id, freeze in sorted(runs.items())],
            "source_schedule_id": int(schedule.id),
        },
        "schedule": {"id": int(schedule.id), "ledger_generation_id": int(schedule.ledger_generation_id),
                     "period_from": schedule.period_from, "period_to": schedule.period_to,
                     "status": schedule.status, "source_program_id": schedule.source_program_id,
                     "covered_programs": [{"program_id": row.program_id, "source_run_id": row.source_run_id,
                                            "ledger_generation_id": row.ledger_generation_id,
                                            "freeze_version": row.freeze_version} for row in schedule.covered_programs]},
        "days": sorted(days), "resources": [{"id": key, "name": resources[key]} for key in sorted(resources, key=lambda key: (resources[key], key))],
        "slots": slots, "gaps": gaps, "kpi": kpi,
        "calendar_fallback": bool((schedule.config_snapshot or {}).get("calendar_fallback", False)),
    })
    existing = db.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == CONSUMER,
        models.PlanningReadSnapshot.snapshot_key == f"{SNAPSHOT_KEY_PREFIX}{int(schedule.id)}:v1",
        models.PlanningReadSnapshot.ledger_generation_id == int(generation.id),
    ).one_or_none()
    if existing is not None:
        if str(existing.truth_status) != ("accepted" if accepted else "building") or existing.cutoff != generation.cutoff or _canonical(existing.payload) != _canonical(payload):
            raise DbrDrumBoardCandidateError("candidate drum board conflicts with persisted immutable snapshot")
        return existing
    snapshot = models.PlanningReadSnapshot(
        consumer=CONSUMER, snapshot_key=f"{SNAPSHOT_KEY_PREFIX}{int(schedule.id)}:v1", ledger_generation_id=int(generation.id),
        cutoff=generation.cutoff, truth_status="accepted" if accepted else "building",
        reason=None if accepted else "unpublished Ledger-native DBR drum board",
        payload=payload, published_at=datetime.now(timezone.utc),
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _unavailable(db: Session, reason: str, truth_detail: dict[str, Any] | None = None) -> DbrDrumBoardSnapshotUnavailable:
    state = get_truth_state(db)
    detail = {"code": "dbr_drum_board_snapshot_unavailable", "consumer": CONSUMER,
              "status": "unavailable", "truth_status": state.status,
              "ledger_generation": state.generation_id,
              "cutoff": state.cutoff.isoformat() if state.cutoff else None, "reason": reason}
    if truth_detail:
        detail["truth"] = jsonable_encoder(truth_detail)
    return DbrDrumBoardSnapshotUnavailable(detail)


def read_drum_board_snapshot(db: Session, *, date_from: date | None = None, date_to: date | None = None) -> dict[str, Any]:
    try:
        snapshot = get_latest_read_snapshot(db, consumer=CONSUMER, required_capabilities=REQUIRED_CAPABILITIES)
    except PlanningTruthUnavailable as exc:
        raise _unavailable(db, str(exc), exc.as_dict()) from exc
    if snapshot is None or not isinstance(snapshot.payload, dict):
        raise _unavailable(db, "No DBR drum board snapshot for current accepted Ledger")
    required = {"schedule", "days", "resources", "slots", "gaps", "kpi", "meta"}
    if not required.issubset(snapshot.payload) or not isinstance(snapshot.payload.get("slots"), list):
        raise _unavailable(db, f"DBR drum board snapshot {snapshot.id} has invalid payload")
    result = dict(snapshot.payload)
    slots = [dict(row) for row in result["slots"] if (date_from is None or str(row.get("date")) >= date_from.isoformat()) and (date_to is None or str(row.get("date")) <= date_to.isoformat())]
    result["slots"] = slots
    result["days"] = sorted({row.get("date") for row in slots if row.get("date") is not None})
    result["gaps"] = [dict(row) for row in result["gaps"] if (row.get("date") is None or (date_from is None or str(row.get("date")) >= date_from.isoformat()) and (date_to is None or str(row.get("date")) <= date_to.isoformat()))]
    meta = dict(result["meta"])
    meta.update({"snapshot_id": int(snapshot.id), "ledger_generation": int(snapshot.ledger_generation_id),
                 "cutoff": snapshot.cutoff.isoformat(), "truth_status": str(snapshot.truth_status), "truth_reason": snapshot.reason})
    result["meta"] = meta
    return result

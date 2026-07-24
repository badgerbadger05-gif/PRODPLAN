"""Replay physical facts into one sealed obligation-refresh candidate.

The historical replay core is deliberately generation-scoped.  This adapter
only establishes the closed candidate boundary and its deterministic time
window; it never copies or touches the accepted generation's reservations.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models

from .historical_replay_persistence import run_historical_replay


class CandidateRealizationReplayError(RuntimeError):
    """A candidate is not a sealed, isolated replay target."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _manifest_candidate_runs(
    db: Session,
    target: models.LedgerGeneration,
) -> list[models.PlanningRun]:
    marks = dict(target.source_watermarks or {})
    manifest = marks.get("obligation_refresh_manifest")
    content_hash = marks.get("obligation_refresh_manifest_hash")
    if not isinstance(manifest, dict) or not isinstance(content_hash, str):
        raise CandidateRealizationReplayError("target lacks sealed obligation_refresh_manifest")
    if sha256(_canonical(manifest).encode("utf-8")).hexdigest() != content_hash:
        raise CandidateRealizationReplayError("obligation_refresh_manifest hash conflicts")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise CandidateRealizationReplayError("obligation_refresh_manifest must have entries")

    candidate_ids: set[int] = set()
    plan_ids: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise CandidateRealizationReplayError("obligation_refresh_manifest entry is malformed")
        try:
            candidate_id = int(entry["candidate_run_id"])
            plan_id = int(entry["plan_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CandidateRealizationReplayError(
                "obligation_refresh_manifest entry identity is malformed"
            ) from exc
        if candidate_id <= 0 or plan_id <= 0 or candidate_id in candidate_ids or plan_id in plan_ids:
            raise CandidateRealizationReplayError("obligation_refresh_manifest has duplicate candidates")
        candidate_ids.add(candidate_id)
        plan_ids.add(plan_id)

    runs = db.query(models.PlanningRun).filter(
        models.PlanningRun.run_id.in_(sorted(candidate_ids))
    ).all()
    by_id = {int(row.run_id): row for row in runs}
    if set(by_id) != candidate_ids:
        raise CandidateRealizationReplayError("manifest names missing candidate run")
    for entry in entries:
        run = by_id[int(entry["candidate_run_id"])]
        if (
            str(run.status) != "BUILDING_SNAPSHOT"
            or int(run.ledger_generation_id or 0) != int(target.id)
            or int(run.source_plan_id or 0) != int(entry["plan_id"])
            or run.period_from is None
            or run.period_to is None
        ):
            raise CandidateRealizationReplayError("manifest candidate lineage or period conflicts")

    # The target must have no unsealed candidate and no ReservationEntry that
    # could make an unlisted plan consume the shared physical prefix.
    target_candidates = {
        int(run_id)
        for (run_id,) in db.query(models.PlanningRun.run_id).filter(
            models.PlanningRun.ledger_generation_id == int(target.id),
            models.PlanningRun.status == "BUILDING_SNAPSHOT",
        ).all()
    }
    if target_candidates != candidate_ids:
        raise CandidateRealizationReplayError("target has missing or extra manifest candidates")
    reservation_rows = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.run_id.in_(sorted(candidate_ids))
    ).all()
    if any(int(row.ledger_generation_id) != int(target.id) for row in reservation_rows):
        raise CandidateRealizationReplayError("candidate reservation belongs to another generation")
    outside = db.query(models.ReservationEntry.id).filter(
        models.ReservationEntry.ledger_generation_id == int(target.id),
        or_(
            models.ReservationEntry.run_id.is_(None),
            ~models.ReservationEntry.run_id.in_(sorted(candidate_ids)),
        ),
    ).first()
    if outside is not None:
        raise CandidateRealizationReplayError("target reservation run is outside sealed manifest")
    return sorted(by_id.values(), key=lambda row: (row.period_from, row.period_to, row.run_id))


def _replay_from(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise CandidateRealizationReplayError("target generation lacks accepted replay_from")


def replay_candidate_realizations(
    db: Session,
    target_generation_id: int,
) -> dict[str, Any]:
    """Run FIFO realization for the exact candidates in a sealed refresh.

    Use the immutable obligation replay_from from the accepted parent lineage to
    preserve replay cut boundaries across refresh generations. Transaction
    ownership remains with the refresh workflow.
    """
    target = db.get(models.LedgerGeneration, int(target_generation_id))
    if target is None or str(target.status) != "building":
        raise CandidateRealizationReplayError("target generation must be BUILDING")
    marks = dict(target.source_watermarks or {})
    if marks.get("generation_kind") != "obligation_refresh":
        raise CandidateRealizationReplayError("target generation is not an obligation_refresh")
    if target.cutoff is None or target.physical_import_batch_id is None:
        raise CandidateRealizationReplayError("target generation lacks physical lineage")
    try:
        parent_id = int(marks["parent_generation_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateRealizationReplayError(
            "target generation lacks accepted parent lineage"
        ) from exc
    try:
        replay_from = _replay_from(marks["replay_from"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateRealizationReplayError(
            "target generation lacks replay_from"
        ) from exc
    parent = db.get(models.LedgerGeneration, parent_id)
    pointer = db.get(models.PlanningTruthState, 1)
    if (
        parent is None
        or str(parent.status) != "accepted"
        or pointer is None
        or int(pointer.current_generation_id or -1) != parent_id
        or int(parent.physical_import_batch_id or -1)
        != int(target.physical_import_batch_id)
        or parent.cutoff != target.cutoff
    ):
        raise CandidateRealizationReplayError(
            "target does not reuse the current accepted physical prefix"
        )
    runs = _manifest_candidate_runs(db, target)
    lower_bound = replay_from
    result = run_historical_replay(
        db, int(target.id), replay_from=lower_bound,
    )
    return {
        **result,
        "candidate_run_ids": [int(row.run_id) for row in runs],
        "replay_from": lower_bound.isoformat(),
    }

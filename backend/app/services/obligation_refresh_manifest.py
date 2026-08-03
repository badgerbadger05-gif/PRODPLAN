"""Seal the complete set of planning runs in an obligation-refresh build.

The manifest is deliberately created before requirements, reservations or MRP
snapshots.  It makes a refresh a closed transaction: every currently published
plan is retained, requested fixed plans without a current run are added, and
later retries cannot quietly change that set.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

from sqlalchemy.orm import Session

from app import models
from app.services.planning_run_candidate import (
    PlanningRunCandidateError,
    create_added_candidate_run,
    _resolve_parent_generation_id,
)


MANIFEST_KEY = "obligation_refresh_manifest"
MANIFEST_HASH_KEY = "obligation_refresh_manifest_hash"
MANIFEST_VERSION = 1


class ObligationRefreshManifestError(RuntimeError):
    """The immutable refresh set cannot be safely created or retried."""


@dataclass(frozen=True)
class ObligationRefreshManifestResult:
    ledger_generation_id: int
    entries: tuple[dict[str, int | str | None], ...]
    content_hash: str
    created: bool


def _utc(value: datetime | None, field: str) -> datetime:
    if value is None:
        raise ObligationRefreshManifestError(f"{field} is required")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _normalise_add_ids(plan_ids: Iterable[int]) -> list[int]:
    try:
        result = [int(value) for value in plan_ids]
    except (TypeError, ValueError) as exc:
        raise ObligationRefreshManifestError("add_plan_ids must contain integers") from exc
    if any(value <= 0 for value in result) or len(result) != len(set(result)):
        raise ObligationRefreshManifestError("add_plan_ids must be unique positive ids")
    return sorted(result)


def _require_target(
    db: Session, parent_generation_id: int, target_generation_id: int
) -> tuple[models.LedgerGeneration, models.LedgerGeneration]:
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    pointer = db.get(models.PlanningTruthState, 1)
    if (
        parent is None
        or str(parent.status) != "accepted"
        or pointer is None
        or pointer.current_generation_id is None
        or int(pointer.current_generation_id) != int(parent.id)
    ):
        raise ObligationRefreshManifestError(
            "parent generation must be the current ACCEPTED planning truth"
        )
    target = db.get(models.LedgerGeneration, int(target_generation_id))
    if target is None or str(target.status) != "building":
        raise ObligationRefreshManifestError("target generation must be BUILDING")
    watermarks = dict(target.source_watermarks or {})
    if (
        watermarks.get("generation_kind") != "obligation_refresh"
        or watermarks.get("parent_generation_id") != int(parent.id)
        or int(target.physical_import_batch_id or -1)
        != int(parent.physical_import_batch_id or -1)
        or _utc(target.cutoff, "target cutoff") != _utc(parent.cutoff, "parent cutoff")
    ):
        raise ObligationRefreshManifestError("target does not descend from parent generation")
    return parent, target


def _current_parents(db: Session, parent_generation_id: int) -> list[models.PlanningRun]:
    parent_generation_id = int(parent_generation_id)
    rows = db.query(models.PlanningRun).filter(
        models.PlanningRun.status == "FIXED_SNAPSHOT",
        models.PlanningRun.source_plan_id.isnot(None),
    ).order_by(models.PlanningRun.source_plan_id, models.PlanningRun.run_id).all()
    seen: set[int] = set()
    selected: list[models.PlanningRun] = []
    for row in rows:
        plan_id = int(row.source_plan_id)
        resolved_generation = _resolve_parent_generation_id(db, row)
        if resolved_generation != parent_generation_id:
            continue
        if plan_id in seen:
            raise ObligationRefreshManifestError(
                f"current generation has multiple FIXED_SNAPSHOT runs for plan {plan_id}"
            )
        seen.add(plan_id)
        selected.append(row)
    return selected


def _payload(
    entries: list[dict[str, int | str | None]],
    *,
    add_plan_ids: list[int],
    retire_plan_ids: list[int],
    horizon_days: int | None,
    config_version_id: int | None,
    config_snapshot: dict[str, Any],
    planning_pool_by_warehouse: dict[str, str],
) -> dict[str, Any]:
    return {
        "version": MANIFEST_VERSION,
        "entries": entries,
        "add_request": {
            "plan_ids": add_plan_ids,
            "retire_plan_ids": retire_plan_ids,
            "horizon_days": horizon_days,
            "config_version_id": config_version_id,
            "config_snapshot": config_snapshot,
            "planning_pool_by_warehouse": planning_pool_by_warehouse,
        },
    }


def _normalise_pool_mapping(
    value: Mapping[str, str] | None,
) -> dict[str, str]:
    if value is None:
        return {}
    result: dict[str, str] = {}
    for raw_warehouse, raw_pool in value.items():
        warehouse = str(raw_warehouse).strip()
        pool = str(raw_pool).strip()
        if not warehouse or not pool:
            raise ObligationRefreshManifestError(
                "planning_pool_by_warehouse must contain non-empty names"
            )
        if warehouse in result and result[warehouse] != pool:
            raise ObligationRefreshManifestError(
                f"warehouse {warehouse!r} has conflicting planning pool mappings"
            )
        result[warehouse] = pool
    return {key: result[key] for key in sorted(result)}


def _existing_result(
    db: Session,
    target: models.LedgerGeneration,
) -> ObligationRefreshManifestResult | None:
    watermarks = dict(target.source_watermarks or {})
    payload = watermarks.get(MANIFEST_KEY)
    content_hash = watermarks.get(MANIFEST_HASH_KEY)
    if payload is None and content_hash is None:
        return None
    if not isinstance(payload, dict) or not isinstance(content_hash, str):
        raise ObligationRefreshManifestError("target has malformed refresh manifest")
    if _hash(payload) != content_hash:
        raise ObligationRefreshManifestError("target refresh manifest hash conflicts")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ObligationRefreshManifestError("target refresh manifest entries are malformed")
    declared_ids: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ObligationRefreshManifestError("target refresh manifest entry is malformed")
        try:
            plan_id = int(entry["plan_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ObligationRefreshManifestError(
                "target refresh manifest entry identity is malformed"
            ) from exc
        action = entry.get("action")
        expected_parent = entry.get("parent_run_id")
        if action in {"retain", "retire"}:
            # Both actions name a *current* parent run and never a candidate:
            # a retire only changes what the publisher does with that parent
            # (CLOSED instead of carried forward), so the resume-time lineage
            # proof is identical.  Omitting the retire branch here used to make
            # every resume of an interrupted plan closure fail on ``int(None)``.
            try:
                parent_id = int(expected_parent)
            except (TypeError, ValueError) as exc:
                raise ObligationRefreshManifestError(
                    f"target {action} manifest parent identity is malformed"
                ) from exc
            parent = db.get(models.PlanningRun, parent_id)
            if (
                entry.get("candidate_run_id") is not None
                or parent is None
                or str(parent.status) != "FIXED_SNAPSHOT"
                or int(parent.source_plan_id or -1) != plan_id
            ):
                raise ObligationRefreshManifestError(
                    f"target {action} manifest lineage conflicts"
                )
            continue
        if action != "add":
            raise ObligationRefreshManifestError(
                "target refresh manifest contains unsupported action"
            )
        try:
            candidate_id = int(entry["candidate_run_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ObligationRefreshManifestError(
                "target refresh manifest candidate identity is malformed"
            ) from exc
        candidate = db.get(models.PlanningRun, candidate_id)
        if (
            candidate is None
            or str(candidate.status) != "BUILDING_SNAPSHOT"
            or int(candidate.ledger_generation_id or -1) != int(target.id)
            or int(candidate.source_plan_id or -1) != plan_id
            or candidate.prior_run_id is not None
        ):
            raise ObligationRefreshManifestError(
                "target refresh manifest candidate lineage conflicts"
            )
        declared_ids.add(candidate_id)
    actual_ids = {
        int(run_id)
        for (run_id,) in db.query(models.PlanningRun.run_id).filter(
            models.PlanningRun.ledger_generation_id == int(target.id),
            models.PlanningRun.status == "BUILDING_SNAPSHOT",
        ).all()
    }
    if actual_ids != declared_ids:
        raise ObligationRefreshManifestError(
            "target refresh manifest has missing or extra candidates"
        )
    return ObligationRefreshManifestResult(
        ledger_generation_id=int(target.id), entries=tuple(entries),
        content_hash=content_hash, created=False,
    )


def create_obligation_refresh_manifest(
    db: Session,
    parent_generation_id: int,
    target_generation_id: int,
    add_plan_ids: Iterable[int],
    retire_plan_ids: Iterable[int] = (),
    *,
    started_by: str | None,
    horizon_days: int | None,
    config_version_id: int | None,
    config_snapshot: dict[str, Any],
    planning_pool_by_warehouse: Mapping[str, str] | None = None,
) -> ObligationRefreshManifestResult:
    """Create or exactly retry the sealed refresh/add/retain/retire run set.

    ``retire`` names a current parent whose plan is being closed by this build:
    the entry carries ``parent_run_id`` and never a candidate, exactly like
    ``retain``.  This helper owns neither transaction commit nor rollback.
    """
    if not isinstance(config_snapshot, dict):
        raise ObligationRefreshManifestError("config_snapshot must be a mapping")
    add_ids = _normalise_add_ids(add_plan_ids)
    retire_ids = _normalise_add_ids(retire_plan_ids)
    if set(add_ids).intersection(retire_ids):
        raise ObligationRefreshManifestError("a plan cannot be added and retired together")
    pool_mapping = _normalise_pool_mapping(planning_pool_by_warehouse)
    parent, target = _require_target(db, parent_generation_id, target_generation_id)
    existing = _existing_result(db, target)
    if existing is not None:
        # Rebuild the request shape before returning.  This is intentionally
        # stricter than just checking the hash: a caller may not add/omit a
        # plan or alter sealed first-plan settings on a retry.
        expected = _payload(
            list(existing.entries), add_plan_ids=add_ids,
            retire_plan_ids=retire_ids,
            horizon_days=horizon_days, config_version_id=config_version_id,
            config_snapshot=deepcopy(config_snapshot),
            planning_pool_by_warehouse=pool_mapping,
        )
        if _hash(expected) != existing.content_hash:
            raise ObligationRefreshManifestError("conflicting retry of refresh manifest")
        return existing

    watermarks = dict(target.source_watermarks or {})
    allowed = {"generation_kind", "parent_generation_id", "replay_from"}
    if set(watermarks) - allowed:
        raise ObligationRefreshManifestError("target source_watermarks are already sealed")

    parents = _current_parents(db, int(parent.id))
    current_plan_ids = {int(row.source_plan_id) for row in parents}
    missing_retire = set(retire_ids) - current_plan_ids
    if missing_retire:
        raise ObligationRefreshManifestError(
            f"retire plan has no current FIXED_SNAPSHOT: {min(missing_retire)}"
        )
    overlap = current_plan_ids.intersection(add_ids)
    if overlap:
        raise ObligationRefreshManifestError(
            f"add plan already has current FIXED_SNAPSHOT: {min(overlap)}"
        )

    entries: list[dict[str, int | str | None]] = []
    try:
        for parent_run in parents:
            entries.append({
                "action": (
                    "retire"
                    if int(parent_run.source_plan_id) in set(retire_ids)
                    else "retain"
                ),
                "plan_id": int(parent_run.source_plan_id),
                "parent_run_id": int(parent_run.run_id),
                "candidate_run_id": None,
            })
        for plan_id in add_ids:
            candidate = create_added_candidate_run(
                db, plan_id, int(target.id), started_by,
                horizon_days=horizon_days, config_version_id=config_version_id,
                config_snapshot=deepcopy(config_snapshot),
            )
            entries.append({
                "action": "add", "plan_id": int(plan_id), "parent_run_id": None,
                "candidate_run_id": int(candidate.run_id),
            })
    except PlanningRunCandidateError as exc:
        raise ObligationRefreshManifestError(str(exc)) from exc

    entries.sort(key=lambda row: (int(row["plan_id"]), str(row["action"])))
    payload = _payload(
        entries, add_plan_ids=add_ids, retire_plan_ids=retire_ids,
        horizon_days=horizon_days,
        config_version_id=config_version_id, config_snapshot=deepcopy(config_snapshot),
        planning_pool_by_warehouse=pool_mapping,
    )
    content_hash = _hash(payload)
    target.source_watermarks = {
        **watermarks,
        MANIFEST_KEY: payload,
        MANIFEST_HASH_KEY: content_hash,
    }
    db.flush()
    return ObligationRefreshManifestResult(
        ledger_generation_id=int(target.id), entries=tuple(entries),
        content_hash=content_hash, created=True,
    )

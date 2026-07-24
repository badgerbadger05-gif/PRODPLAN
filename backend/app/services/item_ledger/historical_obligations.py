"""Select and materialize frozen historical planning obligations.

This path deliberately reads only persisted plan snapshots.  It never
re-explodes a BOM, refreezes a run, or consults mutable execution caches.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app import models

from .reservation_ledger import (
    CONSUME,
    _append_event,
    _fold_entry,
    _get_or_create_entry,
    _load_items,
    mode_targets,
)


ALGORITHM_VERSION = "historical-obligations/1"
BUILD_STAGE = "reservation_materialize"


class HistoricalObligationAmbiguity(ValueError):
    """Persisted legacy lineage cannot identify one safe frozen snapshot."""


def _checksum(payload: Any) -> str:
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_run_period(
    run: models.PlanningRun, plan: models.ProductionPlanHeader
) -> None:
    if (
        run.period_from is None
        or run.period_to is None
        or run.period_from != plan.period_from
        or run.period_to != plan.period_to
    ):
        raise HistoricalObligationAmbiguity(
            f"plan {plan.id} has ambiguous run {run.run_id} period lineage"
        )


def select_historical_obligation_runs(
    db: Session, cutoff: datetime
) -> list[models.PlanningRun]:
    """Return one canonical frozen run per eligible source plan."""
    plans = (
        db.query(models.ProductionPlanHeader)
        .filter(
            models.ProductionPlanHeader.status.in_(("fixed", "archived")),
            models.ProductionPlanHeader.fixed_at.isnot(None),
            models.ProductionPlanHeader.fixed_at <= cutoff,
        )
        .order_by(models.ProductionPlanHeader.id.asc())
        .all()
    )
    selected: list[models.PlanningRun] = []
    for plan in plans:
        candidates = (
            db.query(models.PlanningRun)
            .filter(
                models.PlanningRun.source_plan_id == int(plan.id),
                models.PlanningRun.status.in_(("FIXED_SNAPSHOT", "CLOSED")),
                models.PlanningRun.fixed_at.isnot(None),
                models.PlanningRun.fixed_at <= cutoff,
            )
            .order_by(models.PlanningRun.run_id.desc())
            .all()
        )
        fixed = [
            run for run in candidates
            if str(run.status) == "FIXED_SNAPSHOT"
        ]
        if fixed:
            chosen = fixed[0]
        else:
            closed = [
                run
                for run in candidates
                if str(run.status) == "CLOSED"
                and plan.fixed_at is not None
                and run.fixed_at is not None
            ]
            if not closed:
                continue
            chosen = closed[0]
        _validate_run_period(chosen, plan)
        selected.append(chosen)
    return selected


def _manifest(
    runs: list[models.PlanningRun],
    requirements: list[models.MrpRequirement],
    buckets: list[models.MrpRequirementBucket],
) -> dict[str, Any]:
    rows = {
        "runs": [
            {
                "run_id": int(run.run_id),
                "source_plan_id": int(run.source_plan_id),
                "status": str(run.status),
                "period_from": run.period_from,
                "period_to": run.period_to,
                "fixed_at": run.fixed_at,
            }
            for run in runs
        ],
        "requirements": [
            {
                "id": int(req.id),
                "run_id": int(req.run_id),
                "item_id": int(req.item_id),
                "gross": str(req.total_required_qty),
                "net": str(req.net_required_qty),
                "bom_level": int(req.bom_level or 0),
                "freeze_version": req.freeze_version,
                "period_from": req.period_from,
                "period_to": req.period_to,
            }
            for req in requirements
        ],
        "buckets": [
            {
                "id": int(row.id),
                "requirement_id": int(row.requirement_id),
                "run_id": int(row.run_id),
                "item_id": int(row.item_id),
                "bucket_date": row.bucket_date,
                "gross": str(row.gross_qty),
                "net": str(row.net_qty),
            }
            for row in buckets
        ],
    }
    return {**rows, "input_checksum": _checksum(rows)}


def materialize_historical_obligations(
    db: Session, ledger_generation_id: int
) -> dict[str, Any]:
    """Materialize every frozen requirement into one BUILDING generation."""
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise ValueError(f"LedgerGeneration {ledger_generation_id} not found")
    if str(generation.status) != "building":
        raise ValueError(
            "historical obligations require an explicit BUILDING LedgerGeneration"
        )
    if generation.cutoff is None:
        raise ValueError("historical obligations require generation cutoff")

    runs = select_historical_obligation_runs(db, generation.cutoff)
    run_ids = [int(run.run_id) for run in runs]
    requirements = (
        db.query(models.MrpRequirement)
        .filter(models.MrpRequirement.run_id.in_(run_ids))
        .order_by(models.MrpRequirement.run_id, models.MrpRequirement.id)
        .all()
        if run_ids
        else []
    )
    requirement_ids = [int(req.id) for req in requirements]
    buckets = (
        db.query(models.MrpRequirementBucket)
        .filter(models.MrpRequirementBucket.requirement_id.in_(requirement_ids))
        .order_by(
            models.MrpRequirementBucket.requirement_id,
            models.MrpRequirementBucket.bucket_date,
            models.MrpRequirementBucket.id,
        )
        .all()
        if requirement_ids
        else []
    )
    req_by_id = {int(req.id): req for req in requirements}
    runs_by_id = {int(run.run_id): run for run in runs}
    for req in requirements:
        run = runs_by_id[int(req.run_id)]
        if req.period_from != run.period_from or req.period_to != run.period_to:
            raise HistoricalObligationAmbiguity(
                f"requirement {req.id} has ambiguous run period lineage"
            )
    buckets_by_requirement: dict[int, list[models.MrpRequirementBucket]] = {}
    legacy_bucket_ids: list[int] = []
    for bucket in buckets:
        req = req_by_id.get(int(bucket.requirement_id))
        if (
            req is None
            or int(bucket.run_id) != int(req.run_id)
            or int(bucket.item_id) != int(req.item_id)
        ):
            raise HistoricalObligationAmbiguity(
                f"bucket {bucket.id} has ambiguous requirement lineage"
            )
        if not (req.period_from <= bucket.bucket_date <= req.period_to):
            legacy_bucket_ids.append(int(bucket.id))
        buckets_by_requirement.setdefault(int(req.id), []).append(bucket)
    for req in requirements:
        req_buckets = buckets_by_requirement.get(int(req.id), [])
        if not req_buckets:
            continue
        gross = sum(
            (Decimal(str(row.gross_qty or 0)) for row in req_buckets),
            Decimal("0"),
        )
        net = sum(
            (Decimal(str(row.net_qty or 0)) for row in req_buckets),
            Decimal("0"),
        )
        if (
            gross != Decimal(str(req.total_required_qty or 0))
            or net != Decimal(str(req.net_required_qty or 0))
        ):
            raise HistoricalObligationAmbiguity(
                f"requirement {req.id} disagrees with frozen bucket quantities"
            )

    manifest = _manifest(runs, requirements, buckets)
    batch_key = (
        f"g{generation.id}:historical-obligations:"
        f"{manifest['input_checksum'][:24]}"
    )
    prior_batches = (
        db.query(models.LedgerBuildBatch)
        .filter(
            models.LedgerBuildBatch.ledger_generation_id == generation.id,
            models.LedgerBuildBatch.stage == BUILD_STAGE,
            models.LedgerBuildBatch.algorithm_version == ALGORITHM_VERSION,
            models.LedgerBuildBatch.status == "completed",
        )
        .order_by(models.LedgerBuildBatch.id.asc())
        .all()
    )
    if prior_batches:
        existing_batch = prior_batches[0]
        prior_checksum = str(
            (existing_batch.metrics or {}).get("input_checksum") or ""
        )
        if prior_checksum != manifest["input_checksum"]:
            raise HistoricalObligationAmbiguity(
                "historical obligation inputs changed after completed manifest"
            )
        return {
            "ledger_generation_id": int(generation.id),
            "batch_id": int(existing_batch.id),
            "idempotent": True,
            **dict(existing_batch.metrics or {}),
        }

    items = _load_items(db, {int(req.item_id) for req in requirements})
    entries = events = 0
    cycle_id = f"historical-obligations:g{generation.id}"
    for req in requirements:
        targets = mode_targets(req, items.get(int(req.item_id)))
        if not targets:
            targets = [(CONSUME, Decimal(str(req.total_required_qty or 0)))]
        for mode, target in targets:
            entry = _get_or_create_entry(
                db,
                req,
                mode,
                runs_by_id[int(req.run_id)],
                int(generation.id),
            )
            entries += 1
            version = int(req.freeze_version or 0)
            inserted = _append_event(
                db,
                entry,
                event_kind="open",
                idempotency_key=(
                    f"historical-open:{int(req.id)}:{mode}:{version}"
                ),
                reserved_delta=target,
                cycle_id=cycle_id,
            )
            events += int(inserted)
            _fold_entry(db, entry)

    legacy_bucket_id_set = set(legacy_bucket_ids)
    metrics = {
        "selected_runs": len(runs),
        "requirements": len(requirements),
        "buckets": len(buckets),
        "reservation_entries": entries,
        "events_inserted": events,
        "input_checksum": manifest["input_checksum"],
        "selected_run_ids": run_ids,
        "selected_requirement_ids": requirement_ids,
        "selected_bucket_ids": [int(row.id) for row in buckets],
        "selected_bucket_dates": [
            row.bucket_date.isoformat() if row.bucket_date is not None else None
            for row in buckets
        ],
        "legacy_out_of_period_bucket_ids": legacy_bucket_ids,
        "legacy_out_of_period_bucket_dates": [
            row.bucket_date.isoformat()
            for row in buckets
            if int(row.id) in legacy_bucket_id_set
        ],
    }
    batch = models.LedgerBuildBatch(
        ledger_generation_id=int(generation.id),
        stage=BUILD_STAGE,
        batch_key=batch_key,
        status="completed",
        algorithm_version=ALGORITHM_VERSION,
        metrics=metrics,
        completed_at=datetime.now(timezone.utc),
    )
    db.add(batch)
    db.flush()
    return {
        "ledger_generation_id": int(generation.id),
        "batch_id": int(batch.id),
        "idempotent": False,
        **metrics,
    }

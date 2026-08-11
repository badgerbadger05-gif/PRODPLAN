"""Resolve the one sealed set of live planning runs for a Ledger generation."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app import models


def live_plan_run_ids(
    db: Session,
    generation: models.LedgerGeneration,
) -> tuple[int, ...]:
    """Return live run IDs, including retained parent runs in a refresh fork."""
    marks = dict(generation.source_watermarks or {})
    if str(marks.get("generation_kind") or "") == "obligation_refresh":
        manifest = marks.get("obligation_refresh_manifest")
        entries = manifest.get("entries") if isinstance(manifest, dict) else None
        if not isinstance(entries, list):
            raise ValueError("obligation refresh generation lacks sealed live-plan scope")
        run_ids: set[int] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("obligation refresh live-plan scope is malformed")
            action = str(entry.get("action") or "")
            if action == "retire":
                continue
            field = "parent_run_id" if action == "retain" else "candidate_run_id"
            try:
                run_ids.add(int(entry[field]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "obligation refresh live-plan scope is malformed"
                ) from exc
        return tuple(sorted(run_ids))
    return tuple(
        int(run_id)
        for (run_id,) in db.query(models.PlanningRun.run_id).filter(
            models.PlanningRun.ledger_generation_id == int(generation.id),
            models.PlanningRun.status == "FIXED_SNAPSHOT",
        ).order_by(models.PlanningRun.run_id.asc()).all()
    )

"""Persistent mutation gate for the crash-resumable output repair.

The repair advances planning truth through several independently committed
generations.  A transaction advisory lock protects each publication, but it
cannot protect the time between worker invocations.  The durable repair job is
therefore also the durable gate: while it is not completed, no ordinary fact
refresh or obligation mutation may publish a competing generation.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app import models


ACTIVE_REPAIR_STATUSES = (
    "pending",
    "phase1_published",
    "rebasing",
    "blocked",
    "failed",
)
_REPAIR_ACTOR = re.compile(r"^assembly-output-repair:(\d+)(?::.*)?$")


class AssemblyOutputRepairMutationBlocked(RuntimeError):
    """A competing planning mutation was refused while repair is active."""


def active_output_repair_jobs(db: Session) -> list[models.AssemblyOutputRepairJob]:
    """Return durable unfinished jobs in deterministic order."""

    return (
        db.query(models.AssemblyOutputRepairJob)
        .filter(models.AssemblyOutputRepairJob.status.in_(ACTIVE_REPAIR_STATUSES))
        .order_by(models.AssemblyOutputRepairJob.id.asc())
        .all()
    )


def _repair_actor_job_id(actor: str | None) -> int | None:
    match = _REPAIR_ACTOR.fullmatch(str(actor or "").strip())
    return int(match.group(1)) if match is not None else None


def assert_output_repair_allows(
    db: Session,
    *,
    operation: str,
    actor: str | None,
) -> None:
    """Fail closed unless the caller owns every active repair job.

    The workflow permits only one active repair job, but checking the complete
    set keeps the safety property intact even if old data predates that rule.
    Failed and blocked jobs deliberately keep the gate closed: either can have
    already published phase one and must be resumed or explicitly resolved.
    """

    active = active_output_repair_jobs(db)
    if not active:
        return
    actor_job_id = _repair_actor_job_id(actor)
    active_ids = tuple(int(job.id) for job in active)
    if actor_job_id is not None and set(active_ids) == {actor_job_id}:
        return
    ids = ",".join(str(value) for value in active_ids)
    raise AssemblyOutputRepairMutationBlocked(
        f"{str(operation or 'planning mutation')} blocked by active "
        f"assembly output repair job(s): {ids}"
    )

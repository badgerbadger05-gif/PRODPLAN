"""Crash-resumable repair for output missed by the historical rebase boundary.

The workflow deliberately uses only canonical writers:

1. a retain-only obligation refresh accepts the previously missed physical
   output and advances persisted plan execution;
2. each affected live MRP is replaced, one accepted generation at a time, from
   that corrected saved remainder.

The repair tables are durable evidence spanning all those publications.  They
contain identifiers and source hashes, but no foreign keys to rebuildable
Ledger/MRP rows, so a later administrative rebuild cannot make the audit trail
undeletable or silently cascade it away.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.services.item_ledger.live_plan_scope import live_plan_run_ids
from app.services.item_ledger.physical_refresh_candidacy import (
    has_live_physical_refresh_candidate,
)
from app.services.mrp_freeze import MRP_LEDGER_LOCK_KEY
from app.services.planning_run_candidate import _resolve_parent_generation_id

from .rebase_output_repair_audit import audit_rebase_output_repair


ALGORITHM_VERSION = "rebase-output-repair/1"
CLOSED_REQUIREMENT_REPAIR_VERSION = "closed-run-open-requirement-repair/1"
ZERO = Decimal("0")


class RebaseOutputRepairError(RuntimeError):
    """The approved repair cannot safely advance."""


class RebaseOutputRepairDeferred(RebaseOutputRepairError):
    """A concurrent canonical physical refresh must finish first."""


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def _qty(value: Any) -> str:
    number = _dec(value)
    if number == ZERO:
        return "0"
    return format(number.normalize(), "f")


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _lock(db: Session) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MRP_LEDGER_LOCK_KEY})


def _pointer_generation(db: Session) -> models.LedgerGeneration:
    pointer = db.get(models.PlanningTruthState, 1)
    generation = (
        db.get(models.LedgerGeneration, int(pointer.current_generation_id))
        if pointer is not None and pointer.current_generation_id is not None
        else None
    )
    if generation is None or str(generation.status) != "accepted":
        raise RebaseOutputRepairError("current accepted planning truth is unavailable")
    return generation


def _phase1_key(checksum: str) -> str:
    return f"assembly-output-repair-{str(checksum)[:40]}-facts"


def audit_closed_run_open_requirements(db: Session) -> dict[str, Any]:
    """Read-only evidence for the impossible OPEN-requirement/CLOSED-run state."""
    rows = (
        db.query(models.MrpRequirement)
        .join(
            models.PlanningRun,
            models.PlanningRun.run_id == models.MrpRequirement.run_id,
        )
        .filter(
            models.PlanningRun.status == "CLOSED",
            models.MrpRequirement.status == "open",
        )
        .order_by(models.MrpRequirement.run_id, models.MrpRequirement.id)
        .all()
    )
    requirements = [
        {
            "requirement_id": int(row.id),
            "run_id": int(row.run_id),
            "item_id": int(row.item_id),
            "net_required_qty": _qty(row.net_required_qty),
        }
        for row in rows
    ]
    evidence = {
        "algorithm_version": CLOSED_REQUIREMENT_REPAIR_VERSION,
        "requirements": requirements,
    }
    return {
        **evidence,
        "status": "repair_preview" if requirements else "clean",
        "requirement_count": len(requirements),
        "run_count": len({row["run_id"] for row in requirements}),
        "net_required_qty": _qty(
            sum((_dec(row["net_required_qty"]) for row in requirements), ZERO)
        ),
        "audit_checksum": _hash(evidence),
    }


def repair_closed_run_open_requirements(
    db: Session,
    *,
    audit_checksum: str,
    repaired_by: str = "cli",
) -> dict[str, Any]:
    """Close only requirements whose owning run is already durably CLOSED."""
    checksum = str(audit_checksum or "").strip()
    if len(checksum) != 64:
        raise RebaseOutputRepairError("a full 64-character audit checksum is required")
    _lock(db)
    audit = audit_closed_run_open_requirements(db)
    if audit["status"] == "clean":
        return {
            **audit,
            "status": "already_clean",
            "repaired_by": str(repaired_by or "cli"),
        }
    if str(audit["audit_checksum"]) != checksum:
        raise RebaseOutputRepairError(
            "closed-run requirement audit checksum is stale or does not match"
        )
    requirement_ids = [
        int(row["requirement_id"]) for row in audit["requirements"]
    ]
    locked = (
        db.query(models.MrpRequirement)
        .join(
            models.PlanningRun,
            models.PlanningRun.run_id == models.MrpRequirement.run_id,
        )
        .filter(
            models.MrpRequirement.id.in_(requirement_ids or (0,)),
            models.PlanningRun.status == "CLOSED",
            models.MrpRequirement.status == "open",
        )
        .order_by(models.MrpRequirement.id)
        .with_for_update()
        .all()
    )
    if {int(row.id) for row in locked} != set(requirement_ids):
        raise RebaseOutputRepairError(
            "closed-run requirement set changed while acquiring repair lock"
        )
    repaired_at = datetime.now(timezone.utc)
    for row in locked:
        row.status = "closed"
        row.closed_at = repaired_at
    db.commit()
    return {
        **audit,
        "status": "repaired",
        "repaired_by": str(repaired_by or "cli"),
        "repaired_at": repaired_at.isoformat(),
    }


def _expected_roots(
    db: Session,
    *,
    plan_id: int,
    run_id: int,
    allocations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    allocated_by_line: dict[int, Decimal] = {}
    for row in allocations:
        if int(row["run_id"]) != int(run_id):
            continue
        line_id = int(row["plan_line_id"])
        allocated_by_line[line_id] = allocated_by_line.get(line_id, ZERO) + _dec(
            row["allocated_qty"]
        )

    lines = (
        db.query(models.ProductionPlanLine)
        .filter(models.ProductionPlanLine.plan_id == int(plan_id))
        .order_by(models.ProductionPlanLine.bucket_date, models.ProductionPlanLine.id)
        .all()
    )
    if not lines:
        raise RebaseOutputRepairError(f"repair plan_id={int(plan_id)} has no root lines")
    result: list[dict[str, Any]] = []
    for line in lines:
        if line.remaining_output_qty is None:
            raise RebaseOutputRepairError(
                f"repair plan_line_id={int(line.id)} lacks saved output remainder"
            )
        planned = _dec(line.qty)
        accepted = _dec(line.accepted_output_qty)
        remaining = _dec(line.remaining_output_qty)
        repaired = allocated_by_line.get(int(line.id), ZERO)
        expected_accepted = accepted + repaired
        expected_remaining = remaining - repaired
        if (
            repaired < ZERO
            or expected_remaining < ZERO
            or planned != expected_accepted + expected_remaining
        ):
            raise RebaseOutputRepairError(
                f"repair plan_line_id={int(line.id)} violates output conservation"
            )
        result.append(
            {
                "plan_line_id": int(line.id),
                "item_id": int(line.item_id),
                "bucket_date": line.bucket_date.isoformat(),
                "planned_qty": _qty(planned),
                "expected_accepted_qty": _qty(expected_accepted),
                "expected_remaining_qty": _qty(expected_remaining),
            }
        )
    return result


def _prepare_job(
    db: Session,
    *,
    audit_checksum: str,
    approved_by: str,
) -> models.AssemblyOutputRepairJob:
    _lock(db)
    existing = (
        db.query(models.AssemblyOutputRepairJob)
        .filter(models.AssemblyOutputRepairJob.audit_checksum == str(audit_checksum))
        .one_or_none()
    )
    if existing is not None:
        return existing

    active = (
        db.query(models.AssemblyOutputRepairJob)
        .filter(models.AssemblyOutputRepairJob.status != "completed")
        .order_by(models.AssemblyOutputRepairJob.id.asc())
        .first()
    )
    if active is not None:
        raise RebaseOutputRepairError(
            f"assembly output repair job {int(active.id)} must be completed first"
        )
    if has_live_physical_refresh_candidate(db):
        raise RebaseOutputRepairDeferred(
            "physical refresh candidate is still building"
        )

    audit = audit_rebase_output_repair(db)
    if str(audit.get("audit_checksum") or "") != str(audit_checksum):
        raise RebaseOutputRepairError("repair audit checksum is stale or does not match")
    if str(audit.get("status") or "") != "repair_preview":
        raise RebaseOutputRepairError("repair audit contains no applicable output facts")
    conservation = dict(audit.get("conservation") or {})
    if conservation.get("balanced") is not True:
        raise RebaseOutputRepairError("repair audit is not conserved")
    generation = _pointer_generation(db)
    if int(audit["ledger_generation_id"]) != int(generation.id):
        raise RebaseOutputRepairError("repair audit no longer names current planning truth")

    now = datetime.now(timezone.utc)
    job = models.AssemblyOutputRepairJob(
        audit_checksum=str(audit_checksum),
        audit_algorithm_version=str(audit["algorithm_version"]),
        source_generation_id=int(generation.id),
        source_cutoff=_utc(audit["cutoff"]),
        status="pending",
        phase1_generation_key=_phase1_key(str(audit_checksum)),
        audit_payload=dict(audit),
        expected_fact_qty=_dec(conservation["recoverable_fact_qty"]),
        expected_allocated_qty=_dec(conservation["allocated_qty"]),
        expected_surplus_qty=_dec(conservation["surplus_qty"]),
        approved_by=str(approved_by or "unknown")[:255],
        approved_at=now,
        result={},
    )
    db.add(job)
    db.flush()

    allocations = list(audit.get("allocations") or [])
    for fact in list(audit.get("facts") or []):
        db.add(
            models.AssemblyOutputRepairFact(
                job_id=int(job.id),
                stock_ledger_entry_id=int(fact["stock_ledger_entry_id"]),
                source_content_hash=str(fact["source_content_hash"]),
                item_id=int(fact["item_id"]),
                posting_at=_utc(fact["posting_at"]),
                fact_qty=_dec(fact["qty"]),
                expected_surplus_qty=_dec(fact["surplus_qty"]),
                decision_status=str(fact["decision_status"]),
            )
        )
    ordinals: dict[int, int] = {}
    for allocation in allocations:
        sle_id = int(allocation["stock_ledger_entry_id"])
        ordinal = ordinals.get(sle_id, 0)
        ordinals[sle_id] = ordinal + 1
        db.add(
            models.AssemblyOutputRepairAllocation(
                job_id=int(job.id),
                stock_ledger_entry_id=sle_id,
                allocation_ordinal=ordinal,
                plan_id=int(allocation["plan_id"]),
                plan_line_id=int(allocation["plan_line_id"]),
                audited_run_id=int(allocation["run_id"]),
                item_id=int(allocation["item_id"]),
                allocated_qty=_dec(allocation["allocated_qty"]),
                match_rule=str(allocation["match_rule"]),
                requires_mrp_replacement=bool(
                    allocation.get("requires_mrp_replacement")
                ),
            )
        )

    targets = sorted(
        {
            (int(row["plan_id"]), int(row["run_id"]))
            for row in allocations
            if bool(row.get("requires_mrp_replacement"))
        }
    )
    if len({plan_id for plan_id, _run_id in targets}) != len(targets):
        raise RebaseOutputRepairError("repair audit contains multiple live runs for one plan")
    for sequence, (plan_id, run_id) in enumerate(targets, start=1):
        roots = _expected_roots(
            db,
            plan_id=plan_id,
            run_id=run_id,
            allocations=allocations,
        )
        db.add(
            models.AssemblyOutputRepairTarget(
                job_id=int(job.id),
                sequence=sequence,
                plan_id=plan_id,
                predecessor_run_id=run_id,
                status="pending",
                expected_roots=roots,
                expected_roots_checksum=_hash(roots),
                result={},
            )
        )
    db.commit()
    return db.get(models.AssemblyOutputRepairJob, int(job.id))


def _expected_allocation_rows(job: models.AssemblyOutputRepairJob) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "stock_ledger_entry_id": int(row.stock_ledger_entry_id),
                "plan_id": int(row.plan_id),
                "plan_line_id": int(row.plan_line_id),
                "run_id": int(row.audited_run_id),
                "allocated_qty": _qty(row.allocated_qty),
                "match_rule": str(row.match_rule),
            }
            for row in job.allocations
        ],
        key=lambda row: (
            row["stock_ledger_entry_id"],
            row["plan_line_id"],
            row["run_id"],
        ),
    )


def _validate_phase1(
    db: Session,
    job: models.AssemblyOutputRepairJob,
    generation_id: int,
) -> None:
    expected = _expected_allocation_rows(job)
    sle_ids = sorted({int(row.stock_ledger_entry_id) for row in job.facts})
    actual_rows = (
        db.query(models.AssemblyOutputAllocation)
        .filter(
            models.AssemblyOutputAllocation.ledger_generation_id == int(generation_id),
            models.AssemblyOutputAllocation.stock_ledger_entry_id.in_(sle_ids or [0]),
        )
        .all()
    )
    actual = sorted(
        [
            {
                "stock_ledger_entry_id": int(row.stock_ledger_entry_id),
                "plan_id": int(row.plan_id),
                "plan_line_id": int(row.plan_line_id),
                "run_id": int(row.run_id),
                "allocated_qty": _qty(row.allocated_qty),
                "match_rule": str(row.match_rule),
            }
            for row in actual_rows
        ],
        key=lambda row: (
            row["stock_ledger_entry_id"],
            row["plan_line_id"],
            row["run_id"],
        ),
    )
    if actual != expected:
        raise RebaseOutputRepairError("phase one output allocations differ from approved audit")

    execution_rows = (
        db.query(models.ProductionPlanExecutionFact)
        .filter(models.ProductionPlanExecutionFact.stock_ledger_entry_id.in_(sle_ids or [0]))
        .all()
    )
    execution = sorted(
        [
            {
                "stock_ledger_entry_id": int(row.stock_ledger_entry_id),
                "plan_id": int(row.plan_id),
                "plan_line_id": int(row.plan_line_id),
                "run_id": int(row.run_id),
                "allocated_qty": _qty(row.allocated_qty),
                "match_rule": str(row.match_rule),
            }
            for row in execution_rows
        ],
        key=lambda row: (
            row["stock_ledger_entry_id"],
            row["plan_line_id"],
            row["run_id"],
        ),
    )
    if execution != expected:
        raise RebaseOutputRepairError("persisted plan execution differs from approved audit")

    decisions = {
        int(row.stock_ledger_entry_id): row
        for row in db.query(models.AssemblyOutputFactDecision)
        .filter(
            models.AssemblyOutputFactDecision.ledger_generation_id == int(generation_id),
            models.AssemblyOutputFactDecision.stock_ledger_entry_id.in_(sle_ids or [0]),
        )
        .all()
    }
    if set(decisions) != set(sle_ids):
        raise RebaseOutputRepairError("phase one lacks an output decision for every repair fact")
    for fact in job.facts:
        decision = decisions[int(fact.stock_ledger_entry_id)]
        if (
            str(decision.source_content_hash) != str(fact.source_content_hash)
            or str(decision.decision_status) != str(fact.decision_status)
            or _dec(decision.surplus_qty) != _dec(fact.expected_surplus_qty)
        ):
            raise RebaseOutputRepairError("phase one output decision evidence changed")

    for target in job.targets:
        _validate_plan_execution(db, target)


def _validate_plan_execution(
    db: Session,
    target: models.AssemblyOutputRepairTarget,
) -> None:
    roots = list(target.expected_roots or [])
    if _hash(roots) != str(target.expected_roots_checksum):
        raise RebaseOutputRepairError("repair target root checksum changed")
    lines = {
        int(row.id): row
        for row in db.query(models.ProductionPlanLine)
        .filter(models.ProductionPlanLine.plan_id == int(target.plan_id))
        .all()
    }
    if set(lines) != {int(row["plan_line_id"]) for row in roots}:
        raise RebaseOutputRepairError("repair target plan-line set changed")
    for root in roots:
        line = lines[int(root["plan_line_id"])]
        actual = {
            "planned_qty": _qty(line.qty),
            "expected_accepted_qty": _qty(line.accepted_output_qty),
            "expected_remaining_qty": _qty(line.remaining_output_qty),
        }
        expected = {
            "planned_qty": str(root["planned_qty"]),
            "expected_accepted_qty": str(root["expected_accepted_qty"]),
            "expected_remaining_qty": str(root["expected_remaining_qty"]),
        }
        if actual != expected:
            raise RebaseOutputRepairError(
                f"repair plan_line_id={int(line.id)} execution changed"
            )


def _validate_successor(
    db: Session,
    target: models.AssemblyOutputRepairTarget,
    successor_run_id: int | None,
) -> tuple[int | None, int]:
    generation = _pointer_generation(db)
    expected_positive = {
        int(row["plan_line_id"]): _dec(row["expected_remaining_qty"])
        for row in list(target.expected_roots or [])
        if _dec(row["expected_remaining_qty"]) > ZERO
    }
    if successor_run_id is None:
        if expected_positive:
            raise RebaseOutputRepairError("repair rebase closed a plan with positive roots")
        if int(target.predecessor_run_id) in set(live_plan_run_ids(db, generation)):
            raise RebaseOutputRepairError("completed zero-root repair still has a live predecessor")
        return None, int(generation.id)

    successor = db.get(models.PlanningRun, int(successor_run_id))
    if (
        successor is None
        or int(successor.prior_run_id or -1) != int(target.predecessor_run_id)
        or int(successor.source_plan_id or -1) != int(target.plan_id)
        or str(successor.status) != "FIXED_SNAPSHOT"
        or _resolve_parent_generation_id(
            db, successor, current_generation_id=int(generation.id)
        )
        != int(generation.id)
    ):
        raise RebaseOutputRepairError("repair successor lineage is not current and exact")
    actual_roots = {
        int(row.plan_line_id): _dec(row.planned_qty)
        for row in db.query(models.MrpRunRoot)
        .filter(models.MrpRunRoot.run_id == int(successor.run_id))
        .all()
    }
    if actual_roots != expected_positive:
        raise RebaseOutputRepairError("repair successor roots differ from corrected remainder")
    return int(successor.run_id), int(generation.id)


def _phase1(
    db: Session,
    job: models.AssemblyOutputRepairJob,
) -> models.AssemblyOutputRepairJob:
    _lock(db)
    current = _pointer_generation(db)
    if int(current.id) != int(job.source_generation_id):
        raise RebaseOutputRepairError("repair source generation is no longer current")
    if has_live_physical_refresh_candidate(db):
        raise RebaseOutputRepairDeferred("physical refresh candidate is still building")
    fresh = audit_rebase_output_repair(db)
    if str(fresh.get("audit_checksum") or "") != str(job.audit_checksum):
        raise RebaseOutputRepairError("repair audit changed before phase one")

    from app.services.obligation_refresh_orchestrator import run_obligation_refresh

    report = run_obligation_refresh(
        db,
        parent_generation_id=int(job.source_generation_id),
        generation_key=str(job.phase1_generation_key),
        add_plan_ids=(),
        retire_plan_ids=(),
        replace_plan_ids=(),
        started_by=f"assembly-output-repair:{int(job.id)}:facts",
    )
    _validate_phase1(db, job, int(report.target_generation_id))
    job.phase1_generation_id = int(report.target_generation_id)
    job.status = "phase1_published"
    job.attempt_count = int(job.attempt_count or 0) + 1
    job.last_error = None
    job.result = {
        **dict(job.result or {}),
        "phase1": {
            "source_generation_id": int(job.source_generation_id),
            "published_generation_id": int(report.target_generation_id),
            "allocation_checksum": _hash(_expected_allocation_rows(job)),
        },
    }
    db.commit()
    return db.get(models.AssemblyOutputRepairJob, int(job.id))


def _next_target(
    db: Session,
    job_id: int,
) -> models.AssemblyOutputRepairTarget | None:
    return (
        db.query(models.AssemblyOutputRepairTarget)
        .filter(
            models.AssemblyOutputRepairTarget.job_id == int(job_id),
            models.AssemblyOutputRepairTarget.status.in_(("pending", "running")),
        )
        .order_by(models.AssemblyOutputRepairTarget.sequence)
        .with_for_update()
        .first()
    )


def _rebase_one(
    db: Session,
    job: models.AssemblyOutputRepairJob,
    target: models.AssemblyOutputRepairTarget,
) -> None:
    _lock(db)
    if has_live_physical_refresh_candidate(db):
        raise RebaseOutputRepairDeferred("physical refresh candidate is still building")
    _validate_plan_execution(db, target)
    target.status = "running"
    target.started_at = target.started_at or datetime.now(timezone.utc)
    target.attempt_count = int(target.attempt_count or 0) + 1
    target.last_error = None
    job.status = "rebasing"
    # Keep the advisory xact lock across the canonical publication.  The
    # rebase service commits its accepted generation; that same commit also
    # durably records this target as ``running``.  If the worker dies after
    # that uncertain commit, retry discovers the already-created successor and
    # validates it before marking the target complete.  Committing here would
    # release the mutation gate and allow a physical fork to slip between the
    # checkpoint and the rebase.
    db.flush()

    from app.services.specification_mrp_rebase import (
        rebase_fixed_plan_remaining_roots,
    )

    result = rebase_fixed_plan_remaining_roots(
        db,
        int(target.predecessor_run_id),
        changed_spec_refs=(),
        started_by=f"assembly-output-repair:{int(job.id)}:run:{int(target.predecessor_run_id)}",
    )
    # The canonical rebase commits its own accepted generation.  Re-read and
    # verify lineage before advancing our independent durable checkpoint.
    db.expire_all()
    target = db.get(models.AssemblyOutputRepairTarget, int(target.id))
    job = db.get(models.AssemblyOutputRepairJob, int(job.id))
    successor_id = result.get("successor_run_id")
    successor_id, generation_id = _validate_successor(
        db,
        target,
        int(successor_id) if successor_id is not None else None,
    )
    target.successor_run_id = successor_id
    target.published_generation_id = generation_id
    target.status = "completed"
    target.completed_at = datetime.now(timezone.utc)
    target.last_error = None
    target.result = dict(result)
    db.flush()
    if _next_target(db, int(job.id)) is None:
        job.status = "completed"
        job.completed_at = datetime.now(timezone.utc)
        job.result = {
            **dict(job.result or {}),
            "completed_targets": len(job.targets),
            "final_generation_id": int(generation_id),
        }
    else:
        job.status = "rebasing"
    db.commit()


def _report(job: models.AssemblyOutputRepairJob) -> dict[str, Any]:
    return {
        "job_id": int(job.id),
        "algorithm_version": ALGORITHM_VERSION,
        "audit_checksum": str(job.audit_checksum),
        "status": str(job.status),
        "source_generation_id": int(job.source_generation_id),
        "phase1_generation_id": (
            int(job.phase1_generation_id) if job.phase1_generation_id is not None else None
        ),
        "expected_fact_qty": _qty(job.expected_fact_qty),
        "expected_allocated_qty": _qty(job.expected_allocated_qty),
        "expected_surplus_qty": _qty(job.expected_surplus_qty),
        "targets": [
            {
                "sequence": int(row.sequence),
                "plan_id": int(row.plan_id),
                "predecessor_run_id": int(row.predecessor_run_id),
                "status": str(row.status),
                "successor_run_id": (
                    int(row.successor_run_id) if row.successor_run_id is not None else None
                ),
                "published_generation_id": (
                    int(row.published_generation_id)
                    if row.published_generation_id is not None
                    else None
                ),
            }
            for row in job.targets
        ],
        "result": dict(job.result or {}),
    }


def apply_rebase_output_repair(
    db: Session,
    *,
    audit_checksum: str,
    approved_by: str = "cli",
    max_rebases: int = 1,
) -> dict[str, Any]:
    """Create/resume one approved repair and advance at most ``max_rebases``.

    The phase-one publication and its checkpoint commit atomically.  Each MRP
    replacement is independently published by the canonical rebase service and
    then validated before its target is marked complete.  A retry can therefore
    recover both before-commit and uncertain-after-commit failures.
    """

    checksum = str(audit_checksum or "").strip()
    if len(checksum) != 64:
        raise RebaseOutputRepairError("a full 64-character audit checksum is required")
    limit = int(max_rebases)
    if limit < 0:
        raise RebaseOutputRepairError("max_rebases must be nonnegative")

    try:
        job = _prepare_job(db, audit_checksum=checksum, approved_by=approved_by)
        if "closed_run_requirement_repair" not in dict(job.result or {}):
            closed_requirement_audit = audit_closed_run_open_requirements(db)
            closed_requirement_result = repair_closed_run_open_requirements(
                db,
                audit_checksum=str(closed_requirement_audit["audit_checksum"]),
                repaired_by=(
                    f"assembly-output-repair:{int(job.id)}:{approved_by or 'cli'}"
                ),
            )
            job = db.get(models.AssemblyOutputRepairJob, int(job.id))
            job.result = {
                **dict(job.result or {}),
                "closed_run_requirement_repair": closed_requirement_result,
            }
            db.commit()
            job = db.get(models.AssemblyOutputRepairJob, int(job.id))
        if str(job.status) == "failed":
            job.status = (
                "pending" if job.phase1_generation_id is None else "rebasing"
            )
            job.last_error = None
            db.commit()
            job = db.get(models.AssemblyOutputRepairJob, int(job.id))
        if str(job.status) == "pending":
            job = _phase1(db, job)
        if str(job.status) in {"phase1_published", "rebasing"}:
            for _ in range(limit):
                target = _next_target(db, int(job.id))
                if target is None:
                    job.status = "completed"
                    job.completed_at = job.completed_at or datetime.now(timezone.utc)
                    db.commit()
                    break
                _rebase_one(db, job, target)
                job = db.get(models.AssemblyOutputRepairJob, int(job.id))
                if str(job.status) == "completed":
                    break
        return _report(db.get(models.AssemblyOutputRepairJob, int(job.id)))
    except RebaseOutputRepairDeferred:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        failed = (
            db.query(models.AssemblyOutputRepairJob)
            .filter(models.AssemblyOutputRepairJob.audit_checksum == checksum)
            .one_or_none()
        )
        if failed is not None and str(failed.status) != "completed":
            failed.status = "failed"
            failed.last_error = str(exc)[:4000]
            db.commit()
        raise

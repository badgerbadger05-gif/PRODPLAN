"""Create an immutable successor MRP for unproduced top-level roots.

The predecessor plan/run is never edited structurally.  Its execution payload
is sealed in ``ClosedPlanSnapshot`` and one obligation-generation publication
retires the predecessor while adding a new fixed plan containing only the root
remainder observed at the accepted physical cutoff.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Any, Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.services.planning_run_candidate import _resolve_parent_generation_id


ZERO = Decimal("0")
REBASE_REASON = "specification_rebase"


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _existing_successor(
    db: Session, predecessor_run_id: int
) -> models.ProductionPlanHeader | None:
    return (
        db.query(models.ProductionPlanHeader)
        .filter(
            models.ProductionPlanHeader.predecessor_run_id
            == int(predecessor_run_id)
        )
        .one_or_none()
    )


def _remaining_root_rows(
    db: Session,
    *,
    plan_id: int,
    generation_id: int,
    successor_period_from,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return coalesced successor rows and an audit of predecessor execution."""
    accepted_rows = (
        db.query(
            models.AssemblyOutputAllocation.plan_line_id,
            func.coalesce(
                func.sum(models.AssemblyOutputAllocation.allocated_qty), ZERO
            ),
        )
        .filter(
            models.AssemblyOutputAllocation.ledger_generation_id
            == int(generation_id),
            models.AssemblyOutputAllocation.plan_id == int(plan_id),
        )
        .group_by(models.AssemblyOutputAllocation.plan_line_id)
        .all()
    )
    accepted_by_line = {
        int(plan_line_id): _dec(accepted_qty)
        for plan_line_id, accepted_qty in accepted_rows
    }

    combined: dict[tuple[int, Any], Decimal] = defaultdict(lambda: ZERO)
    audit: list[dict[str, Any]] = []
    lines = (
        db.query(models.ProductionPlanLine)
        .filter(models.ProductionPlanLine.plan_id == int(plan_id))
        .order_by(models.ProductionPlanLine.bucket_date, models.ProductionPlanLine.id)
        .all()
    )
    if not lines:
        raise ValueError("source production plan has no root lines")
    for line in lines:
        planned = max(_dec(line.qty), ZERO)
        accepted = max(accepted_by_line.get(int(line.id), ZERO), ZERO)
        remaining = max(planned - accepted, ZERO)
        bucket_date = max(line.bucket_date, successor_period_from)
        audit.append(
            {
                "predecessor_plan_line_id": int(line.id),
                "item_id": int(line.item_id),
                "bucket_date": line.bucket_date.isoformat(),
                "planned_qty": str(planned),
                "accepted_output_qty": str(accepted),
                "remaining_qty": str(remaining),
                "successor_bucket_date": bucket_date.isoformat(),
            }
        )
        if remaining > ZERO:
            combined[(int(line.item_id), bucket_date)] += remaining

    successor_rows = [
        {"item_id": item_id, "bucket_date": bucket_date, "qty": qty}
        for (item_id, bucket_date), qty in sorted(
            combined.items(), key=lambda row: (row[0][1], row[0][0])
        )
        if qty > ZERO
    ]
    return successor_rows, audit


def _generation_key(
    *,
    parent_generation_id: int,
    predecessor_run_id: int,
    changed_spec_refs: tuple[str, ...],
) -> str:
    digest = sha256("\n".join(changed_spec_refs).encode("utf-8")).hexdigest()[:12]
    return (
        f"spec-rebase-g{int(parent_generation_id)}-"
        f"r{int(predecessor_run_id)}-{digest}"
    )


def rebase_fixed_plan_remaining_roots(
    db: Session,
    run_id: int,
    *,
    changed_spec_refs: Iterable[str] = (),
    started_by: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Retire one fixed MRP and publish a successor for its root remainder.

    This is intentionally a full-fidelity operation.  ``dry_run`` executes the
    same generation build and rolls the transaction back, so it is suitable for
    the planned shadow benchmark but not for a polling endpoint.
    """
    predecessor = (
        db.query(models.PlanningRun)
        .filter(models.PlanningRun.run_id == int(run_id))
        .with_for_update()
        .one_or_none()
    )
    if predecessor is None:
        raise ValueError(f"run_id={int(run_id)}: прогон не найден")
    if predecessor.source_plan_id is None:
        raise ValueError("planning run is not bound to a production plan")

    prior_successor = _existing_successor(db, int(predecessor.run_id))
    if prior_successor is not None:
        successor_run = (
            db.query(models.PlanningRun)
            .filter(
                models.PlanningRun.source_plan_id == int(prior_successor.id),
                models.PlanningRun.status == "FIXED_SNAPSHOT",
            )
            .one_or_none()
        )
        return {
            "status": "already_rebased",
            "predecessor_plan_id": int(predecessor.source_plan_id),
            "predecessor_run_id": int(predecessor.run_id),
            "successor_plan_id": int(prior_successor.id),
            "successor_run_id": (
                int(successor_run.run_id) if successor_run is not None else None
            ),
            "dry_run": bool(dry_run),
        }

    if str(predecessor.status) != "FIXED_SNAPSHOT":
        raise ValueError("only a current FIXED_SNAPSHOT run can be rebased")
    predecessor_plan = (
        db.query(models.ProductionPlanHeader)
        .filter(
            models.ProductionPlanHeader.id == int(predecessor.source_plan_id)
        )
        .with_for_update()
        .one_or_none()
    )
    if predecessor_plan is None or str(predecessor_plan.status) != "fixed":
        raise ValueError("source production plan must be fixed")

    truth = db.get(models.PlanningTruthState, 1)
    if truth is None or truth.current_generation_id is None:
        raise ValueError("current accepted planning truth is unavailable")
    parent_generation_id = int(truth.current_generation_id)
    generation = db.get(models.LedgerGeneration, parent_generation_id)
    if (
        generation is None
        or str(generation.status) != "accepted"
        or generation.cutoff is None
    ):
        raise ValueError("current accepted planning truth is unavailable")
    if (
        _resolve_parent_generation_id(
            db, predecessor, current_generation_id=parent_generation_id
        )
        != parent_generation_id
    ):
        raise ValueError("planning run is not part of current accepted truth")

    # Facts at/before the accepted cutoff have already reduced the predecessor
    # remainder.  The successor may consume only facts strictly after it.
    successor_fixed_at = _utc(generation.cutoff) + timedelta(microseconds=1)
    successor_period_from = max(
        predecessor_plan.period_from, successor_fixed_at.date()
    )
    successor_period_to = max(
        predecessor_plan.period_to, successor_period_from
    )
    successor_rows, root_audit = _remaining_root_rows(
        db,
        plan_id=int(predecessor_plan.id),
        generation_id=parent_generation_id,
        successor_period_from=successor_period_from,
    )
    spec_refs = tuple(
        sorted({str(value).strip() for value in changed_spec_refs if str(value).strip()})
    )
    context = {
        "version": 1,
        "reason": REBASE_REASON,
        "predecessor_generation_id": parent_generation_id,
        "predecessor_cutoff": _utc(generation.cutoff).isoformat(),
        "changed_spec_refs": list(spec_refs),
        "root_lines": root_audit,
    }

    from app.services.period_plan_service import (
        _latest_closed_plan_snapshot,
        _read_period_plan_execution_payload_for_run,
    )

    execution_payload = _read_period_plan_execution_payload_for_run(
        db,
        plan=predecessor_plan,
        run=predecessor,
        generation_id=parent_generation_id,
    )
    closed_snapshot = _latest_closed_plan_snapshot(
        db,
        plan_id=int(predecessor_plan.id),
        run_id=int(predecessor.run_id),
    )
    if closed_snapshot is None:
        db.add(
            models.ClosedPlanSnapshot(
                plan_id=int(predecessor_plan.id),
                run_id=int(predecessor.run_id),
                ledger_generation_id=parent_generation_id,
                cutoff=generation.cutoff,
                payload=execution_payload,
                closed_at=datetime.now(timezone.utc),
            )
        )
    elif dict(closed_snapshot.payload or {}) != execution_payload:
        raise ValueError("closed plan snapshot payload conflicts with current execution")

    successor: models.ProductionPlanHeader | None = None
    if successor_rows:
        successor = models.ProductionPlanHeader(
            name=(f"{predecessor_plan.name} / остаток"[:255]),
            period_from=successor_period_from,
            period_to=successor_period_to,
            status="fixed",
            comment=f"Successor MRP для run {int(predecessor.run_id)}",
            created_by=started_by,
            fixed_by=started_by,
            fixed_at=successor_fixed_at,
            predecessor_plan_id=int(predecessor_plan.id),
            predecessor_run_id=int(predecessor.run_id),
            lineage_reason=REBASE_REASON,
            lineage_context=context,
        )
        db.add(successor)
        db.flush()
        for row in successor_rows:
            db.add(
                models.ProductionPlanLine(
                    plan_id=int(successor.id),
                    item_id=int(row["item_id"]),
                    bucket_date=row["bucket_date"],
                    qty=row["qty"],
                )
            )
        db.flush()

    predecessor_plan.closed_reason = REBASE_REASON
    predecessor_plan.lineage_context = {
        **dict(predecessor_plan.lineage_context or {}),
        "closed_by_rebase": context,
    }

    from app.services.obligation_refresh_orchestrator import run_obligation_refresh

    generation_key = _generation_key(
        parent_generation_id=parent_generation_id,
        predecessor_run_id=int(predecessor.run_id),
        changed_spec_refs=spec_refs,
    )
    try:
        report = run_obligation_refresh(
            db,
            parent_generation_id=parent_generation_id,
            generation_key=generation_key,
            add_plan_ids=((int(successor.id),) if successor is not None else ()),
            retire_plan_ids=(int(predecessor_plan.id),),
            started_by=started_by or f"specification_rebase:{int(predecessor.run_id)}",
            horizon_days=predecessor.horizon_days,
            config_version_id=predecessor.config_version_id,
            config_snapshot=dict(predecessor.config_snapshot or {}),
        )
        successor_run = None
        if successor is not None:
            successor_run = (
                db.query(models.PlanningRun)
                .filter(
                    models.PlanningRun.source_plan_id == int(successor.id),
                    models.PlanningRun.status == "FIXED_SNAPSHOT",
                )
                .one()
            )
        result = {
            "status": "rebased" if successor is not None else "closed_complete",
            "predecessor_plan_id": int(predecessor_plan.id),
            "predecessor_run_id": int(predecessor.run_id),
            "successor_plan_id": int(successor.id) if successor is not None else None,
            "successor_run_id": (
                int(successor_run.run_id) if successor_run is not None else None
            ),
            "published_generation_id": int(report.target_generation_id),
            "remaining_root_lines": [
                {
                    "item_id": int(row["item_id"]),
                    "bucket_date": row["bucket_date"].isoformat(),
                    "qty": str(row["qty"]),
                }
                for row in successor_rows
            ],
            "dry_run": bool(dry_run),
        }
        if dry_run:
            db.rollback()
        else:
            db.commit()
        return result
    except Exception:
        db.rollback()
        raise

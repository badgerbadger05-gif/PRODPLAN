"""Canonical generation-local assembly queue materialization."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, asc
from sqlalchemy.orm import Session

from app import models
from .live_plan_scope import live_plan_run_ids


ROW_KIND = "assembly_queue_line"


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _date_key(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _priority_key(
    period_from: Any,
    period_to: Any,
    plan_id: int,
    plan_line_id: int,
) -> list[Any]:
    return [_date_key(period_from), _date_key(period_to), int(plan_id), int(plan_line_id)]


def _sort_key(period_from: Any, period_to: Any, plan_id: int, plan_line_id: int) -> str:
    return (
        f"{_date_key(period_from)}|{_date_key(period_to)}|"
        f"{int(plan_id):010d}|{int(plan_line_id):010d}"
    )


def _to_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value))


def _frozen_run_period(run: Any, plan: Any) -> tuple[date, date]:
    """Use the fixed PlanningRun period and reject divergent source headers."""
    if run.period_from is None or run.period_to is None:
        raise ValueError("assembly queue PlanningRun misses frozen period")
    period_from = _to_date(run.period_from)
    period_to = _to_date(run.period_to)
    if period_from != _to_date(plan.period_from) or period_to != _to_date(plan.period_to):
        raise ValueError(
            f"assembly queue period mismatch for run_id={int(run.run_id)} "
            f"and plan_id={int(plan.id)}"
        )
    return period_from, period_to


def _build_rows(db: Session, generation_id: int) -> list[dict[str, Any]]:
    return _build_rows_by_scope(db, generation_id, include_zero=False)


def _build_rows_by_scope(
    db: Session,
    generation_id: int,
    *,
    include_zero: bool,
) -> list[dict[str, Any]]:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise ValueError("assembly queue generation not found")
    run_ids = live_plan_run_ids(db, generation)
    rows = (
        db.query(
            models.ProductionPlanLine,
            models.PlanningRun,
            models.ProductionPlanHeader,
            models.Item,
        )
        .join(
            models.ProductionPlanHeader,
            models.ProductionPlanHeader.id == models.ProductionPlanLine.plan_id,
        )
        .join(
            models.PlanningRun,
            and_(
                models.PlanningRun.source_plan_id == models.ProductionPlanHeader.id,
                models.PlanningRun.run_id.in_(run_ids),
            ),
        )
        .join(models.Item, models.Item.item_id == models.ProductionPlanLine.item_id)
        .filter(
            models.ProductionPlanHeader.status == "fixed",
            models.ProductionPlanLine.qty > 0,
        )
        .order_by(
            asc(models.PlanningRun.period_from),
            asc(models.PlanningRun.period_to),
            asc(models.ProductionPlanHeader.id),
            asc(models.ProductionPlanLine.id),
        )
        .all()
    )

    lines: list[dict[str, Any]] = []
    for line, run, plan, item in rows:
        # A successor MrpRunRoot owns only this run's remainder and execution
        # journal.  The assembly queue is a plan read-model, so its immutable
        # denominator and accumulated accepted output always come from the
        # original ProductionPlanLine across every specification rebase.
        if line.remaining_output_qty is None:
            raise ValueError(
                f"assembly queue plan line {int(line.id)} lacks saved output remainder"
            )
        planned_output_qty = _dec(line.qty)
        accepted_output_qty = _dec(line.accepted_output_qty)
        assembly_remaining_qty = _dec(line.remaining_output_qty)
        if (
            planned_output_qty < Decimal("0")
            or accepted_output_qty < Decimal("0")
            or assembly_remaining_qty < Decimal("0")
            or planned_output_qty
            != accepted_output_qty + assembly_remaining_qty
        ):
            raise ValueError(
                f"assembly queue plan line {int(line.id)} violates output conservation"
            )
        if not include_zero and assembly_remaining_qty <= Decimal("0"):
            continue

        plan_id = int(plan.id)
        line_id = int(line.id)
        run_id = int(run.run_id)
        # Rebase must not make a backdated, late-imported physical output
        # ineligible merely because the successor run started later.  The
        # historical boundary belongs to the immutable fixed plan.
        eligible_from = plan.fixed_at
        period_from, period_to = _frozen_run_period(run, plan)
        priority = _priority_key(period_from, period_to, plan_id, line_id)
        sort_key = _sort_key(period_from, period_to, plan_id, line_id)
        payload: dict[str, Any] = {
            "plan_id": plan_id,
            "plan_line_id": line_id,
            "run_id": run_id,
            "item_id": int(item.item_id),
            "item_code": str(item.item_code or ""),
            "item_name": str(item.item_name or ""),
            "bucket_date": _date_key(line.bucket_date),
            "period_from": _date_key(period_from),
            "period_to": _date_key(period_to),
            "planned_output_qty": float(planned_output_qty),
            "accepted_plan_output_qty": float(accepted_output_qty),
            "assembly_remaining_qty": float(assembly_remaining_qty),
            "eligible_from": eligible_from,
            "priority_key": priority,
        }
        lines.append(
            {
                "row_key": f"plan-line:{line_id}",
                "row_kind": ROW_KIND,
                "item_id": int(item.item_id),
                "sort_key": sort_key,
                "payload": payload,
            }
        )

    return lines


def materialize_assembly_queue_lines(
    db: Session,
    generation_id: int,
) -> list[models.AssemblyQueueLine]:
    """Build or validate saved assembly-queue rows for a generation."""
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise ValueError("assembly queue requires a BUILDING generation")
    if str(generation.status or "") != "building":
        raise ValueError("assembly queue requires a BUILDING generation")

    rows = (
        db.query(models.AssemblyQueueLine)
        .filter(
            models.AssemblyQueueLine.ledger_generation_id == int(generation.id),
        )
        .order_by(models.AssemblyQueueLine.sort_key.asc(), models.AssemblyQueueLine.id.asc())
        .all()
    )
    if rows:
        missing = [int(row.plan_line_id) for row in rows if row.eligible_from is None]
        if missing:
            raise ValueError(
                "persisted assembly queue lacks frozen eligible_from for plan lines "
                + ",".join(str(value) for value in missing)
            )
        return [row for row in rows if str(row.line_status or "") == "open"]

    payload_rows: list[models.AssemblyQueueLine] = []
    for row in _build_rows(db, int(generation.id)):
        payload = row["payload"]
        bucket_date = _to_date(payload["bucket_date"])
        period_from = _to_date(payload["period_from"])
        period_to = _to_date(payload["period_to"])
        eligible_from = payload.get("eligible_from")
        if eligible_from is None:
            raise ValueError(
                f"assembly queue line {int(payload['plan_line_id'])} lacks fixed_at"
            )
        payload_rows.append(models.AssemblyQueueLine(
            ledger_generation_id=int(generation.id),
            planning_run_id=int(payload["run_id"]),
            plan_id=int(payload["plan_id"]),
            plan_line_id=int(payload["plan_line_id"]),
            item_id=int(row["item_id"]),
            bucket_date=bucket_date,
            period_from=period_from,
            period_to=period_to,
            planned_output_qty=_dec(payload["planned_output_qty"]),
            accepted_plan_output_qty=_dec(payload["accepted_plan_output_qty"]),
            assembly_remaining_qty=_dec(payload["assembly_remaining_qty"]),
            original_priority=payload["priority_key"],
            eligible_from=eligible_from,
            sort_key=str(row["sort_key"]),
            line_status="open",
        ))
    db.add_all(payload_rows)
    db.flush()
    return payload_rows

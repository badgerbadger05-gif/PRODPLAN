"""Canonical immutable assembly-queue snapshot builder for current planning generation."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, asc, func
from sqlalchemy.orm import Session

from app import models
from .live_plan_scope import live_plan_run_ids


CONSUMER = "assembly_queue"
SNAPSHOT_KEY = "current:v1"
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


def _datetime_key(value: datetime) -> str:
    normalized = value
    if normalized.tzinfo is not None:
        normalized = normalized.astimezone(timezone.utc).replace(tzinfo=None)
    return normalized.isoformat(timespec="microseconds") + "Z"


def _snapshot_row_from_assembly_queue_line(
    row: models.AssemblyQueueLine,
) -> dict[str, Any]:
    return {
        "plan_id": int(row.plan_id),
        "plan_line_id": int(row.plan_line_id),
        "run_id": int(row.planning_run_id),
        "item_id": int(row.item_id),
        "item_code": str(row.item.item_code or ""),
        "item_name": str(row.item.item_name or ""),
        "bucket_date": _date_key(row.bucket_date),
        "period_from": _date_key(row.period_from),
        "period_to": _date_key(row.period_to),
        "planned_output_qty": float(_dec(row.planned_output_qty)),
        "accepted_plan_output_qty": float(_dec(row.accepted_plan_output_qty)),
        "assembly_remaining_qty": float(_dec(row.assembly_remaining_qty)),
        "eligible_from": _datetime_key(row.eligible_from) if row.eligible_from is not None else None,
        "priority_key": [
            _date_key(row.period_from),
            _date_key(row.period_to),
            int(row.plan_id),
            int(row.plan_line_id),
        ],
        "sort_key": str(row.sort_key),
    }


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
        raise ValueError("assembly queue snapshot generation not found")
    run_ids = live_plan_run_ids(db, generation)
    accepted_allocations = (
        db.query(
            models.AssemblyOutputAllocation.plan_line_id.label("plan_line_id"),
            func.coalesce(
                func.sum(models.AssemblyOutputAllocation.allocated_qty),
                Decimal("0"),
            ).label("accepted_plan_output_qty"),
        )
        .filter(
            models.AssemblyOutputAllocation.ledger_generation_id == int(generation_id),
        )
        .group_by(models.AssemblyOutputAllocation.plan_line_id)
        .subquery()
    )

    rows = (
        db.query(
            models.ProductionPlanLine,
            models.PlanningRun,
            models.ProductionPlanHeader,
            models.Item,
            func.coalesce(
                accepted_allocations.c.accepted_plan_output_qty,
                Decimal("0"),
            ).label("accepted_plan_output_qty"),
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
        .outerjoin(
            accepted_allocations,
            accepted_allocations.c.plan_line_id == models.ProductionPlanLine.id,
        )
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
    for line, run, plan, item, accepted_plan_output_qty in rows:
        planned_output_qty = _dec(line.qty)
        accepted_output_qty = _dec(accepted_plan_output_qty)
        assembly_remaining_qty = max(planned_output_qty - accepted_output_qty, Decimal("0"))
        if not include_zero and assembly_remaining_qty <= Decimal("0"):
            continue

        plan_id = int(plan.id)
        line_id = int(line.id)
        run_id = int(run.run_id)
        eligible_from = plan.fixed_at or run.fixed_at
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


def _snapshot_rows_from_queue(
    queue_rows: list[models.AssemblyQueueLine],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in queue_rows:
        linespec = _snapshot_row_from_assembly_queue_line(row)
        rows.append(
            {
                "row_key": f"plan-line:{int(row.plan_line_id)}",
                "row_kind": ROW_KIND,
                "item_id": int(row.item_id),
                "sort_key": _date_key(row.sort_key),
                "payload": linespec,
            }
        )
    return rows


def _payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = Decimal("0")
    for row in rows:
        total += _dec(row["payload"].get("assembly_remaining_qty"))
    return {
        "rows": [line["payload"] for line in rows],
        "total_rows": len(rows),
        "total_queue_qty": float(total),
    }


def _snapshot_row_specs(rows: list[dict[str, Any]]) -> list[tuple[str, str, int | None, str, dict[str, Any]]]:
    specs: list[tuple[str, str, int | None, str, dict[str, Any]]] = []
    for row in rows:
        specs.append(
            (
                str(row["row_key"]),
                str(row["row_kind"]),
                int(row["item_id"]) if row["item_id"] is not None else None,
                str(row["sort_key"]),
                dict(row["payload"]),
            ),
        )
    return specs


def _snapshot_matches(
    db: Session,
    snapshot: models.PlanningReadSnapshot,
    *,
    payload: dict[str, Any],
    row_specs: list[tuple[str, str, int | None, str, dict[str, Any]]],
) -> bool:
    if dict(snapshot.payload or {}) != payload:
        return False
    generation = snapshot.ledger_generation
    if generation is None or snapshot.cutoff != generation.cutoff:
        return False
    if snapshot.reason is not None:
        return False
    if str(snapshot.truth_status or "") != "accepted":
        return False

    rows = (
        db.query(models.PlanningReadRow)
        .filter(models.PlanningReadRow.snapshot_id == int(snapshot.id))
        .order_by(models.PlanningReadRow.sort_key.asc(), models.PlanningReadRow.id.asc())
        .all()
    )
    if len(rows) != len(row_specs):
        return False

    for row, spec in zip(rows, row_specs):
        expected_row_key, expected_kind, expected_item_id, expected_sort_key, expected_payload = spec
        if (
            row.row_key != expected_row_key
            or row.row_kind != expected_kind
            or row.item_id != expected_item_id
            or row.sort_key != expected_sort_key
            or dict(row.payload or {}) != expected_payload
        ):
            return False
    return True


def build_assembly_queue_snapshot(db: Session, generation_id: int) -> models.PlanningReadSnapshot:
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise ValueError("assembly queue snapshot generation not found")
    if str(generation.status or "") != "building":
        raise ValueError("assembly queue snapshot requires a BUILDING generation")

    queue_rows = materialize_assembly_queue_lines(db, int(generation.id))
    rows = _snapshot_rows_from_queue(queue_rows)
    payload = _payload(rows)
    row_specs = _snapshot_row_specs(rows)

    existing = (
        db.query(models.PlanningReadSnapshot)
        .filter(
            models.PlanningReadSnapshot.consumer == CONSUMER,
            models.PlanningReadSnapshot.snapshot_key == SNAPSHOT_KEY,
            models.PlanningReadSnapshot.ledger_generation_id == int(generation.id),
        )
        .one_or_none()
    )
    if existing is not None:
        if _snapshot_matches(db, existing, payload=payload, row_specs=row_specs):
            return existing
        raise ValueError("assembly queue snapshot conflicts with persisted data")

    with db.begin_nested():
        snapshot = models.PlanningReadSnapshot(
            consumer=CONSUMER,
            snapshot_key=SNAPSHOT_KEY,
            ledger_generation_id=int(generation.id),
            cutoff=generation.cutoff,
            truth_status="accepted",
            reason=None,
            payload=payload,
            published_at=datetime.now(timezone.utc),
        )
        db.add(snapshot)
        db.flush()

        for row in rows:
            db.add(
                models.PlanningReadRow(
                    snapshot_id=int(snapshot.id),
                    row_key=str(row["row_key"]),
                    row_kind=str(row["row_kind"]),
                    item_id=row["item_id"],
                    sort_key=str(row["sort_key"]),
                    payload=dict(row["payload"]),
                )
            )
        db.flush()

    return snapshot

"""Explicit, audited correction of a fixed period-plan group.

This is the sole exception to the ordinary immutable plan-matrix rule.  It
keeps stable plan-line and fact ownership, saves the original matrix, and
replaces every affected live MRP in one obligation generation built only from
the corrected saved remainder.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Any, Iterable, Mapping

from sqlalchemy.orm import Session

from app import models
from app.services.item_ledger.live_plan_scope import live_plan_run_ids
from app.services.plan_output_quantities import (
    expected_plan_output_quantities,
    validate_saved_plan_output,
)


ZERO = Decimal("0")
QUANTUM = Decimal("0.001")
MAX_QTY = Decimal("999999999999.999")
SOURCE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class FixedPlanCorrectionError(ValueError):
    """The requested correction is not safe at the current truth revision."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _hash(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _parse_qty(value: Any) -> Decimal:
    try:
        qty = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FixedPlanCorrectionError("target qty must be a decimal number") from exc
    if (
        not qty.is_finite()
        or qty < ZERO
        or qty > MAX_QTY
        or qty.quantize(QUANTUM) != qty
    ):
        raise FixedPlanCorrectionError(
            "target qty must be finite, non-negative and have at most 3 decimals"
        )
    return qty


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise FixedPlanCorrectionError("target bucket_date must be an ISO date") from exc


def _line_payload(line: models.ProductionPlanLine) -> dict[str, Any]:
    if line.remaining_output_qty is None:
        raise FixedPlanCorrectionError(
            f"plan line {int(line.id)} has no persisted execution remainder"
        )
    quantities = validate_saved_plan_output(
        line.qty,
        line.accepted_output_qty,
        line.remaining_output_qty,
        context=f"plan line {int(line.id)}",
    )
    return {
        "plan_line_id": int(line.id),
        "plan_id": int(line.plan_id),
        "item_id": int(line.item_id),
        "bucket_date": line.bucket_date.isoformat(),
        "planned_output_qty": str(quantities.planned),
        "accepted_plan_output_qty": str(quantities.accepted),
        "assembly_remaining_qty": str(quantities.remaining),
        "overproduction_qty": str(quantities.overproduction),
        "locked_by_run_id": (
            int(line.locked_by_run_id)
            if line.locked_by_run_id is not None
            else None
        ),
    }


def _matrix_payload(
    plans: Iterable[models.ProductionPlanHeader],
    lines: Iterable[models.ProductionPlanLine],
    runs_by_plan: Mapping[int, models.PlanningRun],
) -> dict[str, Any]:
    ordered_lines = sorted(
        lines,
        key=lambda line: (
            int(line.plan_id),
            line.bucket_date,
            int(line.item_id),
            int(line.id),
        ),
    )
    roots: list[dict[str, Any]] = []
    run_ids = [int(run.run_id) for run in runs_by_plan.values()]
    if run_ids:
        # Filled by the caller after attaching a Session-bound query result.
        session = next(iter(runs_by_plan.values()))._sa_instance_state.session
        if session is None:
            raise FixedPlanCorrectionError("correction run is detached")
        for root in session.query(models.MrpRunRoot).filter(
            models.MrpRunRoot.run_id.in_(run_ids)
        ).order_by(models.MrpRunRoot.run_id, models.MrpRunRoot.plan_line_id).all():
            roots.append({
                "mrp_run_root_id": int(root.id),
                "run_id": int(root.run_id),
                "plan_line_id": int(root.plan_line_id),
                "planned_qty": str(root.planned_qty),
                "accepted_qty": str(root.accepted_qty),
                "remaining_qty": str(root.remaining_qty),
            })
    return {
        "plans": [
            {
                "plan_id": int(plan.id),
                "name": str(plan.name),
                "period_from": plan.period_from.isoformat(),
                "period_to": plan.period_to.isoformat(),
                "status": str(plan.status),
                "current_run_id": int(runs_by_plan[int(plan.id)].run_id),
            }
            for plan in sorted(plans, key=lambda row: int(row.id))
        ],
        "lines": [_line_payload(line) for line in ordered_lines],
        "run_roots": roots,
    }


def _normalise_targets(
    entries: Iterable[Mapping[str, Any]],
    *,
    month_key: tuple[int, int],
) -> tuple[dict[tuple[int, date], Decimal], list[dict[str, Any]]]:
    targets: dict[tuple[int, date], Decimal] = {}
    audit: list[dict[str, Any]] = []
    for raw in entries:
        try:
            item_id = int(raw["item_id"])
            bucket_date = _parse_date(raw["bucket_date"])
            qty = _parse_qty(raw["qty"])
        except KeyError as exc:
            raise FixedPlanCorrectionError(
                f"target entry lacks {exc.args[0]}"
            ) from exc
        if item_id <= 0:
            raise FixedPlanCorrectionError("target item_id must be positive")
        if (bucket_date.year, bucket_date.month) != month_key:
            raise FixedPlanCorrectionError(
                "target bucket_date is outside the corrected plan month"
            )
        key = (item_id, bucket_date)
        if key in targets:
            raise FixedPlanCorrectionError(
                f"duplicate target cell item_id={item_id}, bucket_date={bucket_date}"
            )
        targets[key] = qty
        audit.append({
            "item_id": item_id,
            "bucket_date": bucket_date.isoformat(),
            "qty": str(qty),
            **{
                name: raw[name]
                for name in ("item_code", "article", "source_row", "week")
                if raw.get(name) is not None
            },
        })
    audit.sort(key=lambda row: (row["bucket_date"], row["item_id"]))
    return targets, audit


def _current_scope_guard(
    db: Session,
    *,
    expected_generation_id: int,
    expected_source_revision: str,
) -> tuple[models.PlanningTruthState, models.LedgerGeneration]:
    truth = (
        db.query(models.PlanningTruthState)
        .filter(models.PlanningTruthState.id == 1)
        .with_for_update()
        .one_or_none()
    )
    if truth is None or truth.current_generation_id is None:
        raise FixedPlanCorrectionError("current accepted planning truth is unavailable")
    if int(truth.current_generation_id) != int(expected_generation_id):
        raise FixedPlanCorrectionError(
            "expected generation no longer matches current planning truth"
        )
    generation = db.get(models.LedgerGeneration, int(truth.current_generation_id))
    if generation is None or str(generation.status) != "accepted":
        raise FixedPlanCorrectionError("current accepted planning truth is unavailable")
    scope = db.query(models.CurrentExecutionScope).filter_by(
        entity_kind="period_plan_execution",
        scope_key="period-plan:all-live-plans",
    ).one_or_none()
    if (
        scope is None
        or not bool(scope.result_ready)
        or int(scope.source_generation_id or -1) != int(generation.id)
    ):
        raise FixedPlanCorrectionError(
            "current period-plan execution revision is unavailable"
        )
    if str(scope.source_revision) != str(expected_source_revision):
        raise FixedPlanCorrectionError(
            "expected source revision no longer matches current period-plan execution"
        )
    return truth, generation


def correct_fixed_plan_groups(
    db: Session,
    *,
    groups: Iterable[Mapping[str, Any]],
    source_id: str,
    source_hash: str,
    expected_generation_id: int,
    expected_source_revision: str,
    corrected_by: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Correct independent same-month groups in one atomic generation."""
    group_requests: list[dict[str, Any]] = []
    all_plan_ids: list[int] = []
    for raw_group in groups:
        try:
            ids = tuple(sorted(int(value) for value in raw_group["plan_ids"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise FixedPlanCorrectionError(
                "each group must contain integer plan_ids"
            ) from exc
        if not ids or any(value <= 0 for value in ids) or len(ids) != len(set(ids)):
            raise FixedPlanCorrectionError(
                "each group plan_ids must be unique positive ids"
            )
        raw_targets: list[dict[str, Any]] = []
        seen_target_cells: set[tuple[int, date]] = set()
        for raw_target in raw_group.get("target_entries", ()):
            row = dict(raw_target)
            try:
                item_id = int(row["item_id"])
                bucket_date = _parse_date(row["bucket_date"])
                qty = _parse_qty(row["qty"])
            except KeyError as exc:
                raise FixedPlanCorrectionError(
                    f"target entry lacks {exc.args[0]}"
                ) from exc
            if item_id <= 0:
                raise FixedPlanCorrectionError("target item_id must be positive")
            cell = (item_id, bucket_date)
            if cell in seen_target_cells:
                raise FixedPlanCorrectionError(
                    f"duplicate target cell item_id={item_id}, bucket_date={bucket_date}"
                )
            seen_target_cells.add(cell)
            raw_targets.append({
                "item_id": item_id,
                "bucket_date": bucket_date.isoformat(),
                "qty": str(qty),
                **{
                    name: row[name]
                    for name in ("item_code", "article", "source_row", "week")
                    if row.get(name) is not None
                },
            })
        raw_targets.sort(key=lambda row: (row["bucket_date"], row["item_id"]))
        group_requests.append({"plan_ids": ids, "raw_targets": raw_targets})
        all_plan_ids.extend(ids)
    if not group_requests:
        raise FixedPlanCorrectionError("at least one correction group is required")
    if len(all_plan_ids) != len(set(all_plan_ids)):
        raise FixedPlanCorrectionError("a plan_id may belong to only one correction group")
    group_requests.sort(key=lambda group: tuple(group["plan_ids"]))
    all_ids = tuple(sorted(all_plan_ids))
    clean_source_id = str(source_id or "").strip()
    clean_source_hash = str(source_hash or "").strip().lower()
    clean_revision = str(expected_source_revision or "").strip()
    if not clean_source_id:
        raise FixedPlanCorrectionError("source_id is required")
    if not SOURCE_HASH_RE.fullmatch(clean_source_hash):
        raise FixedPlanCorrectionError("source_hash must be a lowercase SHA-256 hex digest")
    if not clean_revision:
        raise FixedPlanCorrectionError("expected_source_revision is required")

    group_scope = [list(group["plan_ids"]) for group in group_requests]
    group_scope_hash = _hash(group_scope)
    request_basis = {
        "version": 1,
        "source_id": clean_source_id,
        "source_hash": clean_source_hash,
        "groups": [
            {
                "plan_ids": list(group["plan_ids"]),
                "target_entries": group["raw_targets"],
            }
            for group in group_requests
        ],
    }
    request_hash = _hash(request_basis)

    try:
        existing = db.query(models.ProductionPlanCorrection).filter_by(
            source_id=clean_source_id,
            source_hash=clean_source_hash,
        ).one_or_none()
        if existing is not None:
            if (
                str(existing.request_hash) != request_hash
                or str(existing.group_scope_hash) != group_scope_hash
            ):
                raise FixedPlanCorrectionError(
                    "source correction key already exists with a different target payload"
                )
            result = dict(existing.result_payload or {})
            result["status"] = "already_applied"
            result["correction_id"] = int(existing.id)
            return result

        _truth, generation = _current_scope_guard(
            db,
            expected_generation_id=int(expected_generation_id),
            expected_source_revision=clean_revision,
        )

        plans = (
            db.query(models.ProductionPlanHeader)
            .filter(models.ProductionPlanHeader.id.in_(all_ids))
            .with_for_update()
            .order_by(models.ProductionPlanHeader.id)
            .all()
        )
        if {int(plan.id) for plan in plans} != set(all_ids):
            raise FixedPlanCorrectionError("one or more correction plans do not exist")
        if any(str(plan.status) != "fixed" for plan in plans):
            raise FixedPlanCorrectionError("every correction plan must be fixed")
        plans_by_id = {int(plan.id): plan for plan in plans}
        target_item_ids: set[int] = set()
        target_payload_groups: list[dict[str, Any]] = []
        for group in group_requests:
            group_plans = [plans_by_id[plan_id] for plan_id in group["plan_ids"]]
            month_keys = {
                (plan.period_from.year, plan.period_from.month)
                for plan in group_plans
            } | {
                (plan.period_to.year, plan.period_to.month)
                for plan in group_plans
            }
            if len(month_keys) != 1:
                raise FixedPlanCorrectionError(
                    "every plan inside one correction group must belong to the same calendar month"
                )
            targets, target_payload = _normalise_targets(
                group["raw_targets"],
                month_key=next(iter(month_keys)),
            )
            period_from = min(plan.period_from for plan in group_plans)
            period_to = max(plan.period_to for plan in group_plans)
            if any(
                bucket_date < period_from or bucket_date > period_to
                for _item_id, bucket_date in targets
            ):
                raise FixedPlanCorrectionError(
                    "target bucket_date is outside the corrected group period"
                )
            group["targets"] = targets
            group["primary_plan_id"] = min(group["plan_ids"])
            group["target_payload"] = target_payload
            target_item_ids.update(item_id for item_id, _bucket in targets)
            target_payload_groups.append({
                "plan_ids": list(group["plan_ids"]),
                "primary_plan_id": int(group["primary_plan_id"]),
                "entries": target_payload,
            })
        if target_item_ids:
            known_item_ids = {
                int(item_id)
                for (item_id,) in db.query(models.Item.item_id).filter(
                    models.Item.item_id.in_(target_item_ids)
                ).all()
            }
            missing = target_item_ids - known_item_ids
            if missing:
                raise FixedPlanCorrectionError(
                    f"target item_id does not exist: {min(missing)}"
                )

        live_ids = live_plan_run_ids(db, generation)
        live_runs = (
            db.query(models.PlanningRun)
            .filter(
                models.PlanningRun.run_id.in_(live_ids or (0,)),
                models.PlanningRun.source_plan_id.in_(all_ids),
            )
            .with_for_update()
            .all()
        )
        runs_by_plan = {
            int(run.source_plan_id): run
            for run in live_runs
            if run.source_plan_id is not None
        }
        if set(runs_by_plan) != set(all_ids) or any(
            str(run.status) != "FIXED_SNAPSHOT" for run in live_runs
        ):
            raise FixedPlanCorrectionError(
                "every correction plan must have exactly one current fixed MRP run"
            )
        run_settings = {
            (
                run.horizon_days,
                run.config_version_id,
                _canonical(dict(run.config_snapshot or {})),
            )
            for run in live_runs
        }
        if len(run_settings) != 1:
            raise FixedPlanCorrectionError(
                "batch correction plans have different MRP settings"
            )
        horizon_days, config_version_id, config_json = next(iter(run_settings))
        config_snapshot = json.loads(config_json)

        lines = (
            db.query(models.ProductionPlanLine)
            .filter(models.ProductionPlanLine.plan_id.in_(all_ids))
            .with_for_update()
            .order_by(
                models.ProductionPlanLine.plan_id,
                models.ProductionPlanLine.bucket_date,
                models.ProductionPlanLine.item_id,
                models.ProductionPlanLine.id,
            )
            .all()
        )
        if not lines:
            raise FixedPlanCorrectionError("correction plan group has no lines")
        for line in lines:
            expected_run_id = int(runs_by_plan[int(line.plan_id)].run_id)
            if int(line.locked_by_run_id or -1) != expected_run_id:
                raise FixedPlanCorrectionError(
                    f"plan line {int(line.id)} is not locked by its current MRP run"
                )

        before_payload = _matrix_payload(plans, lines, runs_by_plan)
        from app.services.period_plan_service import (
            _read_period_plan_execution_payload_for_run,
        )

        for plan in plans:
            run = runs_by_plan[int(plan.id)]
            existing_snapshot = db.query(models.ClosedPlanSnapshot).filter_by(
                plan_id=int(plan.id),
                run_id=int(run.run_id),
            ).one_or_none()
            payload = _read_period_plan_execution_payload_for_run(
                db,
                plan=plan,
                run=run,
                generation_id=int(generation.id),
            )
            if existing_snapshot is None:
                db.add(models.ClosedPlanSnapshot(
                    plan_id=int(plan.id),
                    run_id=int(run.run_id),
                    ledger_generation_id=int(generation.id),
                    cutoff=generation.cutoff,
                    payload=payload,
                    closed_at=datetime.now(timezone.utc),
                ))
            elif dict(existing_snapshot.payload or {}) != payload:
                raise FixedPlanCorrectionError(
                    f"closed snapshot for run {int(run.run_id)} conflicts with current execution"
                )

        existed_before: dict[int, bool] = {int(line.id): True for line in lines}
        allocation_roles: dict[int, str] = {}
        target_by_line: dict[int, Decimal] = {}
        before_by_line: dict[int, dict[str, Any]] = {
            int(line.id): _line_payload(line) for line in lines
        }

        for group in group_requests:
            group_ids = set(group["plan_ids"])
            targets = group["targets"]
            primary_plan_id = int(group["primary_plan_id"])
            primary_run_id = int(runs_by_plan[primary_plan_id].run_id)
            by_cell: dict[
                tuple[int, date], list[models.ProductionPlanLine]
            ] = defaultdict(list)
            for line in lines:
                if int(line.plan_id) in group_ids:
                    by_cell[(int(line.item_id), line.bucket_date)].append(line)

            all_cells = sorted(
                set(by_cell) | set(targets),
                key=lambda value: (value[1], value[0]),
            )
            for item_id, bucket_date in all_cells:
                target = targets.get((item_id, bucket_date), ZERO)
                cell_lines = sorted(
                    by_cell.get((item_id, bucket_date), []),
                    key=lambda line: (int(line.plan_id), int(line.id)),
                )
                primary = next(
                    (
                        line
                        for line in cell_lines
                        if int(line.plan_id) == primary_plan_id
                    ),
                    None,
                )
                if primary is None and target > ZERO:
                    primary = models.ProductionPlanLine(
                        plan_id=primary_plan_id,
                        item_id=item_id,
                        bucket_date=bucket_date,
                        qty=ZERO,
                        accepted_output_qty=ZERO,
                        remaining_output_qty=ZERO,
                        locked_by_run_id=primary_run_id,
                    )
                    db.add(primary)
                    db.flush()
                    cell_lines.append(primary)
                    cell_lines.sort(
                        key=lambda line: (int(line.plan_id), int(line.id))
                    )
                    by_cell[(item_id, bucket_date)].append(primary)
                    lines.append(primary)
                    existed_before[int(primary.id)] = False
                    before_by_line[int(primary.id)] = {
                        "plan_line_id": int(primary.id),
                        "plan_id": primary_plan_id,
                        "item_id": item_id,
                        "bucket_date": bucket_date.isoformat(),
                        "planned_output_qty": "0",
                        "accepted_plan_output_qty": "0",
                        "assembly_remaining_qty": "0",
                        "overproduction_qty": "0",
                        "locked_by_run_id": primary_run_id,
                    }

                remaining_target = target
                allocation_by_line: dict[int, Decimal] = {
                    int(line.id): ZERO for line in cell_lines
                }
                for line in cell_lines:
                    accepted = Decimal(str(line.accepted_output_qty or 0))
                    if accepted <= ZERO or remaining_target <= ZERO:
                        continue
                    assigned = min(accepted, remaining_target)
                    allocation_by_line[int(line.id)] += assigned
                    remaining_target -= assigned
                    allocation_roles[int(line.id)] = "accepted_owner"
                if remaining_target > ZERO:
                    if primary is None:
                        raise FixedPlanCorrectionError(
                            "positive target cell has no deterministic primary line"
                        )
                    allocation_by_line[int(primary.id)] += remaining_target
                    allocation_roles[int(primary.id)] = "primary_remainder"
                if sum(allocation_by_line.values(), ZERO) != target:
                    raise FixedPlanCorrectionError(
                        "target cell allocation is not conservative"
                    )
                for line in cell_lines:
                    assigned = allocation_by_line[int(line.id)]
                    quantities = expected_plan_output_quantities(
                        assigned,
                        line.accepted_output_qty or ZERO,
                    )
                    line.qty = quantities.planned
                    line.remaining_output_qty = quantities.remaining
                    target_by_line[int(line.id)] = target
                    allocation_roles.setdefault(
                        int(line.id),
                        "removed" if target == ZERO else "zero_share",
                    )

        db.flush()

        generation_key = (
            f"plan-correction-g{int(generation.id)}-"
            f"{group_scope_hash[:10]}-{clean_source_hash[:10]}"
        )
        from app.services.obligation_refresh_orchestrator import run_obligation_refresh

        report = run_obligation_refresh(
            db,
            parent_generation_id=int(generation.id),
            generation_key=generation_key,
            replace_plan_ids=all_ids,
            started_by=corrected_by or f"plan_correction:{clean_source_id}",
            horizon_days=horizon_days,
            config_version_id=config_version_id,
            config_snapshot=config_snapshot,
            local_order_reconciliation_plan_ids=all_ids,
        )
        successor_run_rows = db.query(models.PlanningRun).filter(
                models.PlanningRun.source_plan_id.in_(all_ids),
                models.PlanningRun.ledger_generation_id
                == int(report.target_generation_id),
                models.PlanningRun.status == "FIXED_SNAPSHOT",
            ).all()
        successor_models = {
            int(run.source_plan_id): run
            for run in successor_run_rows
            if run.source_plan_id is not None
        }
        successor_runs = {
            plan_id: int(run.run_id)
            for plan_id, run in successor_models.items()
        }
        if set(successor_runs) != set(all_ids):
            raise FixedPlanCorrectionError(
                "correction publication did not create every successor MRP"
            )
        snapshot_batch = db.query(models.LedgerBuildBatch).filter_by(
            ledger_generation_id=int(report.target_generation_id),
            stage="snapshot_build",
            status="completed",
        ).one_or_none()
        local_order_reconciliation = (
            dict((snapshot_batch.metrics or {}).get("local_order_reconciliation") or {})
            if snapshot_batch is not None
            else {}
        )
        if not local_order_reconciliation:
            raise FixedPlanCorrectionError(
                "correction publication lacks local order reconciliation audit"
            )
        after_payload = _matrix_payload(plans, lines, successor_models)

        after_lines = {
            int(line.id): _line_payload(line) for line in lines
        }
        totals = {
            "planned_output_qty": str(
                sum((Decimal(row["planned_output_qty"]) for row in after_lines.values()), ZERO)
            ),
            "accepted_plan_output_qty": str(
                sum((Decimal(row["accepted_plan_output_qty"]) for row in after_lines.values()), ZERO)
            ),
            "assembly_remaining_qty": str(
                sum((Decimal(row["assembly_remaining_qty"]) for row in after_lines.values()), ZERO)
            ),
            "overproduction_qty": str(
                sum((Decimal(row["overproduction_qty"]) for row in after_lines.values()), ZERO)
            ),
        }
        result = {
            "status": "dry_run" if dry_run else "applied",
            "correction_id": None,
            "source_id": clean_source_id,
            "source_hash": clean_source_hash,
            "request_hash": request_hash,
            "plan_ids": list(all_ids),
            "groups": [
                {
                    "plan_ids": list(group["plan_ids"]),
                    "primary_plan_id": int(group["primary_plan_id"]),
                }
                for group in group_requests
            ],
            "previous_generation_id": int(generation.id),
            "published_generation_id": int(report.target_generation_id),
            "previous_run_ids": {
                str(plan_id): int(run.run_id)
                for plan_id, run in sorted(runs_by_plan.items())
            },
            "successor_run_ids": {
                str(plan_id): run_id
                for plan_id, run_id in sorted(successor_runs.items())
            },
            "local_order_reconciliation": local_order_reconciliation,
            "totals": totals,
            "dry_run": bool(dry_run),
        }
        correction = models.ProductionPlanCorrection(
            source_id=clean_source_id,
            source_hash=clean_source_hash,
            group_scope_hash=group_scope_hash,
            request_hash=request_hash,
            plan_ids=list(all_ids),
            primary_plan_id=min(all_ids),
            expected_generation_id=int(generation.id),
            expected_source_revision=clean_revision,
            published_generation_id=int(report.target_generation_id),
            target_payload={"version": 1, "groups": target_payload_groups},
            before_payload=before_payload,
            after_payload=after_payload,
            result_payload=result,
            corrected_by=(str(corrected_by).strip() if corrected_by else None),
        )
        db.add(correction)
        db.flush()
        result["correction_id"] = int(correction.id)
        correction.result_payload = dict(result)

        for line in sorted(lines, key=lambda row: int(row.id)):
            before = before_by_line[int(line.id)]
            after = after_lines[int(line.id)]
            db.add(models.ProductionPlanCorrectionLine(
                correction_id=int(correction.id),
                plan_id=int(line.plan_id),
                plan_line_id=int(line.id),
                item_id=int(line.item_id),
                bucket_date=line.bucket_date,
                existed_before=bool(existed_before[int(line.id)]),
                allocation_role=allocation_roles[int(line.id)],
                before_qty=Decimal(before["planned_output_qty"]),
                after_qty=Decimal(after["planned_output_qty"]),
                accepted_qty=Decimal(after["accepted_plan_output_qty"]),
                before_remaining_qty=Decimal(before["assembly_remaining_qty"]),
                after_remaining_qty=Decimal(after["assembly_remaining_qty"]),
                after_overproduction_qty=Decimal(after["overproduction_qty"]),
                target_cell_qty=target_by_line[int(line.id)],
            ))
        db.flush()
        if dry_run:
            result["correction_id"] = None
            db.rollback()
        else:
            db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def correct_fixed_plan_group(
    db: Session,
    *,
    plan_ids: Iterable[int],
    target_entries: Iterable[Mapping[str, Any]],
    source_id: str,
    source_hash: str,
    expected_generation_id: int,
    expected_source_revision: str,
    corrected_by: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Compatibility wrapper for one same-month group."""
    return correct_fixed_plan_groups(
        db,
        groups=({
            "plan_ids": list(plan_ids),
            "target_entries": list(target_entries),
        },),
        source_id=source_id,
        source_hash=source_hash,
        expected_generation_id=expected_generation_id,
        expected_source_revision=expected_source_revision,
        corrected_by=corrected_by,
        dry_run=dry_run,
    )

"""Build and verify a guarded fixed-plan correction rehearsal request.

The builder is read-only.  It binds ``correction-target.json`` to the current
accepted generation and period-plan revision of the selected database.  The
verifier is also read-only and proves the published matrix, audit, facts,
historical roots, successor roots, and explicitly untouched plans.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

from sqlalchemy import create_engine
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import models  # noqa: E402
from app.services.item_ledger.r3_contract import current_live_run  # noqa: E402
from app.services.plan_output_quantities import (  # noqa: E402
    validate_saved_plan_output,
)


DEFAULT_GROUPS = "1;3;5,11;8,9"
DEFAULT_UNTOUCHED = "2,6,7,24,25,26,27"
ZERO = Decimal("0")


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


def _groups(value: str) -> list[list[int]]:
    result = [
        sorted({int(item) for item in group.split(",") if item.strip()})
        for group in value.split(";")
        if group.strip()
    ]
    if not result or any(not group or any(plan_id <= 0 for plan_id in group) for group in result):
        raise ValueError("groups must contain positive plan ids")
    flattened = [plan_id for group in result for plan_id in group]
    if len(flattened) != len(set(flattened)):
        raise ValueError("a plan id may occur in only one group")
    return sorted(result, key=lambda group: tuple(group))


def _ids(value: str) -> list[int]:
    result = sorted({int(item) for item in value.split(",") if item.strip()})
    if any(item <= 0 for item in result):
        raise ValueError("plan ids must be positive")
    return result


def _qty(value: Any) -> Decimal:
    qty = Decimal(str(value))
    if not qty.is_finite() or qty < ZERO or qty.quantize(Decimal("0.001")) != qty:
        raise ValueError(f"invalid target quantity {value!r}")
    return qty


def _month_key(plan: models.ProductionPlanHeader) -> tuple[int, int]:
    keys = {
        (plan.period_from.year, plan.period_from.month),
        (plan.period_to.year, plan.period_to.month),
    }
    if len(keys) != 1:
        raise ValueError(f"plan {int(plan.id)} spans more than one calendar month")
    return next(iter(keys))


def _month_marker(value: Any) -> tuple[int | None, int]:
    if isinstance(value, int):
        if 1 <= value <= 12:
            return None, value
        raise ValueError(f"invalid month marker {value!r}")
    text = str(value or "").strip()
    if text.isdigit() and 1 <= int(text) <= 12:
        return None, int(text)
    for separator in ("-", "/", "."):
        parts = text.split(separator)
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            first, second = (int(part) for part in parts)
            if first > 31 and 1 <= second <= 12:
                return first, second
            if second > 31 and 1 <= first <= 12:
                return second, first
    raise ValueError(f"invalid month marker {value!r}")


def _normalise_month_record(record: Mapping[str, Any]) -> dict[str, Any]:
    cells = record.get("cells")
    if not isinstance(cells, list):
        raise ValueError("each target month must contain cells")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[int, date]] = set()
    cell_months: set[tuple[int, int]] = set()
    for raw in cells:
        if not isinstance(raw, Mapping):
            raise ValueError("target cells must be records")
        item_id = int(raw["item_id"])
        bucket_date = date.fromisoformat(str(raw["bucket_date"]))
        qty = _qty(raw["qty"])
        key = (item_id, bucket_date)
        if item_id <= 0 or key in seen:
            raise ValueError(f"invalid or duplicate target cell {key}")
        seen.add(key)
        cell_months.add((bucket_date.year, bucket_date.month))
        normalized.append({
            "item_id": item_id,
            "bucket_date": bucket_date.isoformat(),
            "qty": str(qty),
            **{
                name: raw[name]
                for name in ("item_code", "article", "source_row", "week")
                if raw.get(name) is not None
            },
        })
    if len(cell_months) > 1:
        raise ValueError("one target month contains cells from multiple months")
    marker = _month_marker(record.get("month"))
    if cell_months:
        cell_key = next(iter(cell_months))
        if marker[1] != cell_key[1] or marker[0] not in (None, cell_key[0]):
            raise ValueError("target month marker conflicts with its cells")
        key = cell_key
    else:
        key = marker
    normalized.sort(key=lambda row: (row["bucket_date"], row["item_id"]))
    total = sum((Decimal(row["qty"]) for row in normalized), ZERO)
    if "total_qty" in record and _qty(record["total_qty"]) != total:
        raise ValueError(f"target month {key} total_qty conflicts with its cells")
    return {"month_key": key, "total_qty": str(total), "cells": normalized}


def _load_target(
    path: Path,
) -> tuple[str, dict[tuple[int | None, int], dict[str, Any]]]:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8-sig"))
    months = payload.get("months") if isinstance(payload, Mapping) else None
    if not isinstance(months, list):
        raise ValueError("target document must contain months[]")
    by_month: dict[tuple[int | None, int], dict[str, Any]] = {}
    for raw_month in months:
        if not isinstance(raw_month, Mapping):
            raise ValueError("target months must be records")
        month = _normalise_month_record(raw_month)
        key = tuple(month["month_key"])
        if key in by_month:
            raise ValueError(f"duplicate target month {key}")
        by_month[key] = month
    return sha256(raw).hexdigest(), by_month


def _line_state(db: Session, plan_ids: Iterable[int]) -> list[dict[str, Any]]:
    ids = tuple(sorted({int(value) for value in plan_ids}))
    rows = db.query(models.ProductionPlanLine).filter(
        models.ProductionPlanLine.plan_id.in_(ids or (0,))
    ).order_by(
        models.ProductionPlanLine.plan_id,
        models.ProductionPlanLine.bucket_date,
        models.ProductionPlanLine.item_id,
        models.ProductionPlanLine.id,
    ).all()
    return [
        {
            "plan_line_id": int(row.id),
            "plan_id": int(row.plan_id),
            "item_id": int(row.item_id),
            "bucket_date": row.bucket_date.isoformat(),
            "qty": str(row.qty),
            "accepted_output_qty": str(row.accepted_output_qty),
            "remaining_output_qty": str(row.remaining_output_qty),
            "locked_by_run_id": int(row.locked_by_run_id or 0),
        }
        for row in rows
    ]


def _execution_facts(db: Session, plan_ids: Iterable[int]) -> list[dict[str, Any]]:
    ids = tuple(sorted({int(value) for value in plan_ids}))
    rows = db.query(models.ProductionPlanExecutionFact).filter(
        models.ProductionPlanExecutionFact.plan_id.in_(ids or (0,))
    ).order_by(
        models.ProductionPlanExecutionFact.stock_ledger_entry_id,
        models.ProductionPlanExecutionFact.plan_line_id,
    ).all()
    return [
        {
            "stock_ledger_entry_id": int(row.stock_ledger_entry_id),
            "plan_id": int(row.plan_id),
            "plan_line_id": int(row.plan_line_id),
            "run_id": int(row.run_id),
            "allocated_qty": str(row.allocated_qty),
            "match_rule": str(row.match_rule),
        }
        for row in rows
    ]


def build_request(
    db: Session,
    *,
    target_path: Path,
    groups: list[list[int]],
    untouched_plan_ids: list[int],
    source_id: str,
    corrected_by: str | None,
    dry_run: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_hash, months = _load_target(target_path)
    all_ids = sorted({plan_id for group in groups for plan_id in group})
    plans = {
        int(plan.id): plan
        for plan in db.query(models.ProductionPlanHeader).filter(
            models.ProductionPlanHeader.id.in_(all_ids)
        ).all()
    }
    if set(plans) != set(all_ids):
        raise ValueError("one or more correction plans are missing")
    request_groups: list[dict[str, Any]] = []
    item_ids: set[int] = set()
    for group in groups:
        keys = {_month_key(plans[plan_id]) for plan_id in group}
        if len(keys) != 1 or any(str(plans[plan_id].status) != "fixed" for plan_id in group):
            raise ValueError(f"group {group} is not one fixed calendar month")
        month_key = next(iter(keys))
        candidates = [
            target
            for target in (months.get(month_key), months.get((None, month_key[1])))
            if target is not None
        ]
        if not candidates:
            raise ValueError(f"target has no month {month_key} for group {group}")
        if len(candidates) > 1:
            raise ValueError(f"target month {month_key} is ambiguous for group {group}")
        target = candidates[0]
        cells = list(target["cells"])
        item_ids.update(int(cell["item_id"]) for cell in cells)
        request_groups.append({"plan_ids": group, "target_entries": cells})
    items = {
        int(item.item_id): item
        for item in db.query(models.Item).filter(models.Item.item_id.in_(item_ids or {0})).all()
    }
    if set(items) != item_ids:
        raise ValueError(f"target contains unknown item ids: {sorted(item_ids - set(items))}")
    for group in request_groups:
        for cell in group["target_entries"]:
            item = items[int(cell["item_id"])]
            if cell.get("item_code") is not None and str(cell["item_code"]) != str(item.item_code or ""):
                raise ValueError(f"item_code conflicts for item_id={int(item.item_id)}")
            if cell.get("article") is not None and str(cell["article"]) != str(item.item_article or ""):
                raise ValueError(f"article conflicts for item_id={int(item.item_id)}")

    truth = db.get(models.PlanningTruthState, 1)
    if truth is None or truth.current_generation_id is None:
        raise ValueError("current accepted generation is unavailable")
    generation = db.get(models.LedgerGeneration, int(truth.current_generation_id))
    scope = db.query(models.CurrentExecutionScope).filter_by(
        entity_kind="period_plan_execution",
        scope_key="period-plan:all-live-plans",
    ).one_or_none()
    if (
        generation is None
        or str(generation.status) != "accepted"
        or scope is None
        or not bool(scope.result_ready)
        or int(scope.source_generation_id or -1) != int(generation.id)
    ):
        raise ValueError("current period-plan execution scope is unavailable")
    run_ids = {}
    for plan_id in all_ids:
        run = current_live_run(db, plan_id)
        run_ids[str(plan_id)] = int(run.run_id)
        locked = {
            int(value or 0)
            for (value,) in db.query(models.ProductionPlanLine.locked_by_run_id).filter_by(
                plan_id=plan_id
            ).all()
        }
        if locked != {int(run.run_id)}:
            raise ValueError(f"plan {plan_id} line locks do not match current run")

    request = {
        "groups": request_groups,
        "source_id": source_id,
        "source_hash": source_hash,
        "expected_generation_id": int(generation.id),
        "expected_source_revision": str(scope.source_revision),
        "corrected_by": corrected_by,
        "dry_run": bool(dry_run),
    }
    untouched = _line_state(db, untouched_plan_ids)
    evidence = {
        "version": 1,
        "target_path": str(target_path.resolve()),
        "source_hash": source_hash,
        "request_hash": _hash(request),
        "expected_generation_id": int(generation.id),
        "expected_source_revision": str(scope.source_revision),
        "current_run_ids": run_ids,
        "corrected_plan_ids": all_ids,
        "untouched_plan_ids": untouched_plan_ids,
        "untouched_lines": untouched,
        "untouched_lines_hash": _hash(untouched),
        "execution_facts": _execution_facts(db, all_ids),
        "execution_facts_hash": _hash(_execution_facts(db, all_ids)),
        "source_lines": _line_state(db, all_ids),
    }
    return request, evidence


def verify_result(
    db: Session,
    *,
    request: Mapping[str, Any],
    evidence: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    if str(result.get("status")) not in {"applied", "already_applied"}:
        raise ValueError("correction result is not applied")
    published_generation_id = int(result["published_generation_id"])
    truth = db.get(models.PlanningTruthState, 1)
    if truth is None or int(truth.current_generation_id or -1) != published_generation_id:
        raise ValueError("published correction generation is not current")
    audit = db.query(models.ProductionPlanCorrection).filter_by(
        source_id=str(request["source_id"]),
        source_hash=str(request["source_hash"]),
    ).one_or_none()
    if audit is None or int(audit.published_generation_id) != published_generation_id:
        raise ValueError("durable correction audit is missing or stale")

    all_plan_ids = sorted(
        {int(plan_id) for group in request["groups"] for plan_id in group["plan_ids"]}
    )
    local_order_reconciliation = dict(result.get("local_order_reconciliation") or {})
    local_entries = list(local_order_reconciliation.get("entries") or [])
    if any(int(entry.get("plan_id") or -1) not in set(all_plan_ids) for entry in local_entries):
        raise ValueError("local order reconciliation escaped the correction plan scope")
    actual_by_group: list[dict[str, Any]] = []
    for group in request["groups"]:
        plan_ids = [int(value) for value in group["plan_ids"]]
        expected = {
            (int(cell["item_id"]), str(cell["bucket_date"])): Decimal(str(cell["qty"]))
            for cell in group["target_entries"]
        }
        actual: dict[tuple[int, str], Decimal] = defaultdict(lambda: ZERO)
        for line in db.query(models.ProductionPlanLine).filter(
            models.ProductionPlanLine.plan_id.in_(plan_ids)
        ).all():
            quantities = validate_saved_plan_output(
                line.qty,
                line.accepted_output_qty,
                line.remaining_output_qty,
                context=f"plan line {int(line.id)}",
            )
            actual[(int(line.item_id), line.bucket_date.isoformat())] += quantities.planned
            run = current_live_run(db, int(line.plan_id))
            if int(line.locked_by_run_id or -1) != int(run.run_id):
                raise ValueError(f"plan line {int(line.id)} has a stale current-run lock")
            root = db.query(models.MrpRunRoot).filter_by(
                run_id=int(run.run_id), plan_line_id=int(line.id)
            ).one_or_none()
            if root is None or Decimal(root.planned_qty) != quantities.remaining:
                raise ValueError(f"successor root for plan line {int(line.id)} is missing or wrong")
        if any(actual.get(key, ZERO) != qty for key, qty in expected.items()):
            raise ValueError(f"corrected group {plan_ids} differs from target")
        if any(qty != ZERO for key, qty in actual.items() if key not in expected):
            raise ValueError(f"corrected group {plan_ids} retains a removed target cell")
        actual_by_group.append({
            "plan_ids": plan_ids,
            "target_total": str(sum(expected.values(), ZERO)),
            "actual_total": str(sum(actual.values(), ZERO)),
        })

    facts = _execution_facts(db, all_plan_ids)
    if _hash(facts) != str(evidence["execution_facts_hash"]):
        raise ValueError("accepted plan execution facts changed during correction")
    untouched = _line_state(db, evidence.get("untouched_plan_ids") or [])
    if _hash(untouched) != str(evidence["untouched_lines_hash"]):
        raise ValueError("an explicitly untouched plan changed")
    before_roots = list((audit.before_payload or {}).get("run_roots") or [])
    if not before_roots:
        raise ValueError("correction audit did not preserve original frozen roots")
    for saved in before_roots:
        root = db.get(models.MrpRunRoot, int(saved["mrp_run_root_id"]))
        if root is None or {
            "planned_qty": str(root.planned_qty),
            "accepted_qty": str(root.accepted_qty),
            "remaining_qty": str(root.remaining_qty),
        } != {
            "planned_qty": str(saved["planned_qty"]),
            "accepted_qty": str(saved["accepted_qty"]),
            "remaining_qty": str(saved["remaining_qty"]),
        }:
            raise ValueError(f"historical root {saved['mrp_run_root_id']} changed")
    return {
        "status": "verified",
        "published_generation_id": published_generation_id,
        "correction_id": int(audit.id),
        "groups": actual_by_group,
        "local_order_reconciliation": local_order_reconciliation,
        "execution_facts_unchanged": True,
        "historical_roots_unchanged": True,
        "untouched_plans_unchanged": True,
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--target", required=True, type=Path)
    build.add_argument("--request-output", required=True, type=Path)
    build.add_argument("--evidence-output", required=True, type=Path)
    build.add_argument("--groups", default=DEFAULT_GROUPS)
    build.add_argument("--untouched-plan-ids", default=DEFAULT_UNTOUCHED)
    build.add_argument("--source-id", default="google-plan-correction-target")
    build.add_argument("--corrected-by")
    build.add_argument("--dry-run", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("--request", required=True, type=Path)
    verify.add_argument("--evidence", required=True, type=Path)
    verify.add_argument("--result", required=True, type=Path)
    verify.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.database_url:
        raise SystemExit("--database-url or DATABASE_URL is required")
    engine = create_engine(args.database_url, pool_pre_ping=True)
    try:
        with Session(engine) as db:
            if args.command == "build":
                request, evidence = build_request(
                    db,
                    target_path=args.target,
                    groups=_groups(args.groups),
                    untouched_plan_ids=_ids(args.untouched_plan_ids),
                    source_id=args.source_id,
                    corrected_by=args.corrected_by,
                    dry_run=args.dry_run,
                )
                _write_json(args.request_output, request)
                _write_json(args.evidence_output, evidence)
                output = {
                    "status": "built",
                    "request_output": str(args.request_output.resolve()),
                    "evidence_output": str(args.evidence_output.resolve()),
                    "expected_generation_id": request["expected_generation_id"],
                    "expected_source_revision": request["expected_source_revision"],
                    "source_hash": request["source_hash"],
                }
            else:
                output = verify_result(
                    db,
                    request=json.loads(args.request.read_text(encoding="utf-8")),
                    evidence=json.loads(args.evidence.read_text(encoding="utf-8")),
                    result=json.loads(args.result.read_text(encoding="utf-8")),
                )
                if args.output:
                    _write_json(args.output, output)
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

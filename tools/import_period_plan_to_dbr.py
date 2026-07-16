#!/usr/bin/env python3
"""Safely import one fixed legacy period plan into the parallel DBR module."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "backend"))
sys.path.insert(0, str(_REPO_ROOT))

from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    DbrAssemblyRate,
    DbrProductionProgram,
    Item,
    ProductionPlanHeader,
    ProductionPlanLine,
)
from app.services.dbr import program_service  # noqa: E402


def _report(plan_id: int, *, dry_run: bool, approve: bool) -> dict[str, Any]:
    return {
        "ok": False,
        "mode": "dry-run" if dry_run else "commit",
        "plan_id": plan_id,
        "approve_requested": approve,
        "existing": False,
        "approved": False,
        "counts": {
            "positive_source_rows": 0,
            "ignored_non_positive_rows": 0,
            "distinct_items": 0,
            "created_programs": 0,
        },
        "missing": {"item_ids": [], "assembly_rate_item_codes": []},
        "warnings": [],
        "errors": [],
    }


def _fail(report: dict[str, Any], message: str) -> dict[str, Any]:
    report["errors"].append(message)
    return report


def _source_rows(lines: list[ProductionPlanLine]) -> list[dict[str, Any]]:
    return [
        {
            "item_id": int(line.item_id),
            "program_date": line.bucket_date,
            "qty": Decimal(str(line.qty)),
            "comment": None,
        }
        for line in lines
    ]


def _row_signature(rows: list[dict[str, Any]]) -> list[tuple[int, Any, Decimal]]:
    return sorted(
        (
            int(row["item_id"]),
            row["program_date"],
            Decimal(str(row["qty"])),
        )
        for row in rows
    )


def _program_signature(program: DbrProductionProgram) -> list[tuple[int, Any, Decimal]]:
    return sorted(
        (int(row.item_id), row.program_date, Decimal(str(row.qty)))
        for row in program.items
    )


def import_period_plan(
    db: Session, *, plan_id: int, dry_run: bool = False, approve: bool = False
) -> dict[str, Any]:
    """Import a fixed plan, owning commit/rollback and returning a JSON-safe report."""
    report = _report(plan_id, dry_run=dry_run, approve=approve)
    marker = f"shadow-import:period-plan:{plan_id}"
    try:
        plan = db.get(ProductionPlanHeader, plan_id)
        if plan is None:
            db.rollback()
            return _fail(report, "period plan not found")
        if plan.status != "fixed":
            db.rollback()
            return _fail(report, f"period plan status must be fixed, got {plan.status!r}")
        if plan.period_from is None or plan.period_to is None or plan.period_from > plan.period_to:
            db.rollback()
            return _fail(report, "period plan has invalid dates")

        all_lines = (
            db.query(ProductionPlanLine)
            .filter(ProductionPlanLine.plan_id == plan_id)
            .order_by(ProductionPlanLine.item_id, ProductionPlanLine.bucket_date)
            .all()
        )
        positive_lines = [line for line in all_lines if Decimal(str(line.qty)) > 0]
        ignored = len(all_lines) - len(positive_lines)
        report["counts"]["positive_source_rows"] = len(positive_lines)
        report["counts"]["ignored_non_positive_rows"] = ignored
        if ignored:
            report["warnings"].append(f"ignored {ignored} non-positive source rows")
        if not positive_lines:
            db.rollback()
            return _fail(report, "period plan has no positive rows")

        seen: set[tuple[int, Any]] = set()
        duplicate_keys: list[str] = []
        invalid_dates: list[str] = []
        for line in positive_lines:
            if line.bucket_date is None or not plan.period_from <= line.bucket_date <= plan.period_to:
                invalid_dates.append(f"item_id={line.item_id}, date={line.bucket_date}")
            key = (int(line.item_id), line.bucket_date)
            if key in seen:
                duplicate_keys.append(f"item_id={line.item_id}, date={line.bucket_date}")
            seen.add(key)
        if invalid_dates:
            db.rollback()
            return _fail(report, "source rows outside plan period: " + "; ".join(invalid_dates))
        if duplicate_keys:
            db.rollback()
            return _fail(report, "duplicate source item/date rows: " + "; ".join(duplicate_keys))

        item_ids = sorted({int(line.item_id) for line in positive_lines})
        report["counts"]["distinct_items"] = len(item_ids)
        items = db.query(Item).filter(Item.item_id.in_(item_ids)).all()
        items_by_id = {int(item.item_id): item for item in items}
        missing_item_ids = [item_id for item_id in item_ids if item_id not in items_by_id]
        report["missing"]["item_ids"] = missing_item_ids
        if missing_item_ids:
            db.rollback()
            return _fail(report, "source rows reference missing items")

        rated_item_ids = {
            int(item_id)
            for (item_id,) in db.query(DbrAssemblyRate.item_id)
            .filter(DbrAssemblyRate.item_id.in_(item_ids))
            .distinct()
            .all()
        }
        missing_rate_ids = [item_id for item_id in item_ids if item_id not in rated_item_ids]
        missing_codes = [items_by_id[item_id].item_code for item_id in missing_rate_ids]
        report["missing"]["assembly_rate_item_codes"] = missing_codes
        if missing_codes:
            db.rollback()
            return _fail(report, "missing DBR assembly rates: " + ", ".join(missing_codes))

        source_rows = _source_rows(positive_lines)
        existing = (
            db.query(DbrProductionProgram)
            .filter(DbrProductionProgram.created_by == marker)
            .order_by(DbrProductionProgram.id)
            .all()
        )
        if len(existing) > 1:
            db.rollback()
            return _fail(report, "multiple DBR programs exist for this source marker")
        if existing:
            program = existing[0]
            report["existing"] = True
            same_header = (
                program.title == plan.name
                and program.from_date == plan.period_from
                and program.to_date == plan.period_to
            )
            if not same_header or _program_signature(program) != _row_signature(source_rows):
                db.rollback()
                return _fail(report, "existing DBR program drifted from the source period plan")
            if approve and program.status == program_service.DRAFT:
                program_service.approve_program(db, program.id)
            report["approved"] = program.status == program_service.APPROVED
        else:
            program = program_service.create_program(
                db,
                from_date=plan.period_from,
                to_date=plan.period_to,
                title=plan.name,
                created_by=marker,
                items=source_rows,
            )
            report["counts"]["created_programs"] = 1
            if approve:
                program_service.approve_program(db, program.id)
            report["approved"] = program.status == program_service.APPROVED

        report["program_id"] = program.id
        report["ok"] = True
        if dry_run:
            db.rollback()
            report["warnings"].append("transaction rolled back by dry-run")
        else:
            db.commit()
        return report
    except Exception:
        db.rollback()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Import one fixed period plan into DBR")
    parser.add_argument("--plan-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = import_period_plan(
            db, plan_id=args.plan_id, dry_run=args.dry_run, approve=args.approve
        )
    except Exception as exc:
        report = _report(args.plan_id, dry_run=args.dry_run, approve=args.approve)
        report["errors"].append(f"unexpected {type(exc).__name__}")
    finally:
        db.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

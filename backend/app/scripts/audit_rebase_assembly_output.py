"""Print the dry-run audit for assembly output missed after an MRP rebase."""

from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.services.item_ledger.rebase_output_repair import (
    apply_rebase_output_repair,
    audit_closed_run_open_requirements,
    repair_closed_run_open_requirements,
)
from app.services.item_ledger.rebase_output_repair_audit import (
    audit_rebase_output_repair,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create or resume the approved durable repair job",
    )
    parser.add_argument("--audit-checksum")
    parser.add_argument("--approved-by", default="cli")
    parser.add_argument("--max-rebases", type=int, default=1)
    parser.add_argument(
        "--closed-run-requirements",
        action="store_true",
        help="audit only OPEN requirements owned by already CLOSED MRP runs",
    )
    parser.add_argument(
        "--apply-closed-run-requirements",
        action="store_true",
        help="close only checksum-approved OPEN requirements of CLOSED runs",
    )
    args = parser.parse_args()
    db = SessionLocal()
    try:
        if args.apply_closed_run_requirements:
            report = repair_closed_run_open_requirements(
                db,
                audit_checksum=str(args.audit_checksum or ""),
                repaired_by=str(args.approved_by or "cli"),
            )
        elif args.closed_run_requirements:
            report = audit_closed_run_open_requirements(db)
        elif args.apply:
            report = apply_rebase_output_repair(
                db,
                audit_checksum=str(args.audit_checksum or ""),
                approved_by=str(args.approved_by or "cli"),
                max_rebases=int(args.max_rebases),
            )
        else:
            report = audit_rebase_output_repair(db)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

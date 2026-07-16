#!/usr/bin/env python3
"""
CLI wrapper for the one-off DBR seed import (settings / assembly rates /
category supply-risk) from ERPNext prodflow TSV dumps in .docs/dbr_seed/.

Usage:
    python tools/import_dbr_seed.py [--seed-dir .docs/dbr_seed] [--dry-run]

--dry-run runs the whole import inside a transaction and rolls it back at the
end, printing the same summary — useful to see how many assembly takts resolve
against the live database without persisting anything.

DATABASE_URL env var selects the target DB (defaults to the app default in
backend/app/database.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make `app` importable (repo layout: backend/ on path, mirroring pytest.ini).
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "backend"))
sys.path.insert(0, str(_REPO_ROOT))

from app.database import SessionLocal  # noqa: E402
from app.services.dbr import seed_import  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Import DBR seed data from ERPNext TSV dumps")
    parser.add_argument(
        "--seed-dir",
        default=str(_REPO_ROOT / ".docs" / "dbr_seed"),
        help="Directory holding the erpnext_*.tsv files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run inside a transaction and roll back (report only)",
    )
    args = parser.parse_args()

    seed_dir = Path(args.seed_dir)
    if not seed_dir.is_dir():
        print(f"seed dir not found: {seed_dir}", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        summary = seed_import.import_all(db, seed_dir)
        if args.dry_run:
            db.rollback()
            summary["_mode"] = "dry-run (rolled back)"
        else:
            db.commit()
            summary["_mode"] = "committed"
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

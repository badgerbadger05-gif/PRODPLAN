from __future__ import annotations

import json
import os

from app.database import SessionLocal
from app.services.production_control_journal import dedupe_mrp_production_orders


def _dry_run() -> bool:
    raw = os.getenv("MRP_DEDUPE_APPLY", "").strip().lower()
    return raw not in {"1", "true", "yes", "apply"}


def main() -> int:
    db = SessionLocal()
    try:
        result = dedupe_mrp_production_orders(db, dry_run=_dry_run())
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

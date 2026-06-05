from __future__ import annotations

import json
import os

from app.database import SessionLocal
from app.services.production_control_material_availability import recalculate_production_coverage


def _limit() -> int:
    raw = os.getenv("PRODUCTION_COVERAGE_RECALC_LIMIT", "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def main() -> int:
    db = SessionLocal()
    try:
        result = recalculate_production_coverage(db, limit=_limit())
    finally:
        db.close()

    print(json.dumps(result, ensure_ascii=False))
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())

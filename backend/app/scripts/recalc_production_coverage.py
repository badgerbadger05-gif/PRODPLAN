from __future__ import annotations

import json
import os
from collections import Counter
from typing import Any

from sqlalchemy import func, or_

from app.database import SessionLocal
from app.models import ProductionOrder, ProductionProduct
from app.services.production_control_journal import DONE_STATE_KEY
from app.services.production_control_material_availability import preview_materials


def _limit() -> int:
    raw = os.getenv("PRODUCTION_COVERAGE_RECALC_LIMIT", "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _active_product_ids(db: Any) -> list[int]:
    query = (
        db.query(ProductionProduct.product_id)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .filter(ProductionOrder.deletion_mark == False)  # noqa: E712
        .filter(or_(ProductionOrder.order_state_key.is_(None), func.lower(ProductionOrder.order_state_key) != DONE_STATE_KEY))
        .filter(func.coalesce(ProductionProduct.remaining_qty, ProductionProduct.quantity) > 0)
        .order_by(ProductionOrder.order_date.desc(), ProductionOrder.order_number.asc(), ProductionProduct.line_number.asc())
    )
    limit = _limit()
    if limit:
        query = query.limit(limit)
    return [int(row[0]) for row in query.all()]


def main() -> int:
    db = SessionLocal()
    statuses: Counter[str] = Counter()
    errors: list[dict[str, Any]] = []
    total = 0
    try:
        product_ids = _active_product_ids(db)
        for product_id in product_ids:
            try:
                result = preview_materials(db, product_id, refresh_state=True)
                statuses[str(result.get("coverage_status") or "unknown")] += 1
                total += 1
            except Exception as exc:  # pragma: no cover - operational safety net
                db.rollback()
                errors.append({"product_id": product_id, "error": str(exc)})
    finally:
        db.close()

    print(
        json.dumps(
            {
                "status": "ok" if not errors else "partial",
                "processed": total,
                "errors": len(errors),
                "coverage": dict(statuses),
                "sample_errors": errors[:20],
            },
            ensure_ascii=False,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""
Проверка выпуска — что мешает выпустить изделие в заданном количестве.

Endpoints:
  GET /api/v1/release-feasibility/search   — поиск изделия по артикулу/коду/названию
  GET /api/v1/release-feasibility/analyze  — блокирующие позиции (и BOM по запросу)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.release_feasibility import (
    DEFAULT_MAX_DEPTH,
    analyze_release,
    find_items,
    resolve_item,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/release-feasibility", tags=["release-feasibility"])


@router.get("/search")
def search_items(
    q: str = Query("", description="Артикул, код, название или GUID изделия"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    items = find_items(db, q, limit=int(limit))
    return {"items": items, "meta": {"q": str(q or "").strip(), "count": len(items)}}


@router.get("/analyze")
def analyze(
    item_id: Optional[int] = Query(None, description="ID изделия (приоритетнее артикула)"),
    article: Optional[str] = Query(None, description="Артикул изделия"),
    qty: float = Query(..., gt=0, description="Количество к выпуску"),
    max_depth: int = Query(DEFAULT_MAX_DEPTH, ge=1, le=50, description="Максимальная глубина разворота"),
    include_tree: bool = Query(False, description="Вернуть полный BOM (по кнопке «Развернуть»)"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    if item_id is None and not str(article or "").strip():
        raise HTTPException(status_code=400, detail="Укажите item_id или article")

    item, candidates = resolve_item(db, item_id=item_id, article=article)
    if item is None:
        if candidates:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Найдено несколько изделий — уточните артикул",
                    "candidates": [
                        {
                            "item_id": int(row.item_id),
                            "item_code": str(row.item_code or ""),
                            "item_article": str(row.item_article or ""),
                            "item_name": str(row.item_name or ""),
                        }
                        for row in candidates[:50]
                    ],
                },
            )
        raise HTTPException(status_code=404, detail="Изделие не найдено")

    try:
        return analyze_release(
            db,
            item,
            float(qty),
            max_depth=int(max_depth),
            include_tree=bool(include_tree),
        )
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - защитный контур
        logger.exception(f"[release-feasibility] analyze failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Ошибка расчёта: {exc}")

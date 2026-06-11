"""API for the "Разбор привязок" page: parts whose workshop binding does not
resolve through the production-kind chain, with reasons and recommendations."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.workshop_binding_review import list_item_lines, list_review_items

router = APIRouter(prefix="/v1/workshop-binding-review", tags=["workshop-binding-review"])


@router.get("/items", response_model=dict)
async def get_review_items(
    scope: str = "active",
    search: Optional[str] = None,
    reason_code: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    try:
        return list_review_items(
            db,
            scope=scope,
            search=search,
            reason_code=reason_code,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/items/{item_id}/lines", response_model=dict)
async def get_review_item_lines(item_id: int, db: Session = Depends(get_db)):
    return list_item_lines(db, int(item_id))

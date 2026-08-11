"""API for the "Разбор привязок" page: parts whose workshop binding does not
resolve through the production-kind chain, with reasons and recommendations."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.workshop_binding_review import list_item_lines, list_review_items

router = APIRouter(prefix="/v1/workshop-binding-review", tags=["workshop-binding-review"])

BindingReviewReason = Literal[
    "NO_SPEC",
    "NO_PRODUCTION_KIND",
    "KIND_NOT_BOUND",
    "NO_WAREHOUSE_BINDING",
]


class BindingReviewItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int
    item_code: str
    item_name: str
    item_article: str
    active_lines: int
    reason_code: BindingReviewReason
    reason_text: str
    recommendation: str
    workshop_id: int | None = None
    spec_id: int | None = None
    spec_name: str | None = None
    production_kind_id: int | None = None
    production_kind_name: str | None = None
    suggested_resource_id: int | None = None
    suggested_resource_name: str | None = None
    suggested_stage_id: int | None = None
    suggested_stage_name: str | None = None


class BindingReviewItemsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BindingReviewItemResponse]
    total: int
    limit: int
    offset: int
    scope: Literal["active", "catalog"]
    counts_by_reason: dict[BindingReviewReason, int]


class BindingReviewLineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int
    order_id: int
    order_number: str
    quantity: float
    remaining_qty: float
    status: str
    workshop_id: int | None = None
    planned_start_date: str | None = None


class BindingReviewLinesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int
    rows: list[BindingReviewLineResponse]
    total: int


@router.get("/items", response_model=BindingReviewItemsResponse)
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


@router.get("/items/{item_id}/lines", response_model=BindingReviewLinesResponse)
async def get_review_item_lines(item_id: int, db: Session = Depends(get_db)):
    return list_item_lines(db, int(item_id))

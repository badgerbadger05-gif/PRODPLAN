"""Admin CRUD for the drum/shelf reference data (`.docs/assembly-queue-and-drum.md`,
`.docs/shelves-buffers-and-mechshop-pull.md`).

Both contours fail closed on missing reference rows and until now nothing could
fill them:

* ``AssemblyRate`` (такт сборки) — its absence aborts generation acceptance with
  ``missing assembly rate for item ...``;
* ``ShelfPolicy`` (полка) — without a row the shelf projection stays empty
  forever, so the mech-shop pull never appears;
* ``ProductionResource.capacity`` — the drum divides by it.

This router is deliberately reference-data only: it never writes a plan, a
requirement, a slot or a projection, and it never touches an accepted
generation. Reference edits take effect on the *next* generation build.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db


router = APIRouter(prefix="/v1/planning-rates", tags=["planning-rates"])


MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 200


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AssemblyRateRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    item_id: int
    item_code: Optional[str] = None
    item_name: Optional[str] = None
    resource_id: int
    resource_name: Optional[str] = None
    qty_per_capacity: float


class AssemblyRateListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[AssemblyRateRow]
    total: int
    limit: int
    offset: int


class AssemblyRateUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int = Field(ge=1)
    resource_id: int = Field(ge=1)
    qty_per_capacity: Decimal = Field(gt=0)


class AssemblyRateUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[AssemblyRateUpsert] = Field(min_length=1)


class AssemblyRateUpsertResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[AssemblyRateRow]
    created: int
    updated: int


class ShelfPolicyRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    item_id: int
    item_code: Optional[str] = None
    item_name: Optional[str] = None
    warehouse_ref1c: str
    warehouse_name: Optional[str] = None
    replenishment_time_days: int
    review_cycle_days: int
    safety_days: int
    batch_multiple: float
    active: bool


class ShelfPolicyListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[ShelfPolicyRow]
    total: int
    limit: int
    offset: int


class ShelfPolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int = Field(ge=1)
    warehouse_ref1c: str = Field(min_length=1, max_length=36)
    replenishment_time_days: int = Field(default=0, ge=0)
    review_cycle_days: int = Field(default=0, ge=0)
    safety_days: int = Field(default=0, ge=0)
    batch_multiple: Decimal = Field(default=Decimal("1"), gt=0)
    active: bool = True


class ShelfPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warehouse_ref1c: Optional[str] = Field(default=None, min_length=1, max_length=36)
    replenishment_time_days: Optional[int] = Field(default=None, ge=0)
    review_cycle_days: Optional[int] = Field(default=None, ge=0)
    safety_days: Optional[int] = Field(default=None, ge=0)
    batch_multiple: Optional[Decimal] = Field(default=None, gt=0)
    active: Optional[bool] = None


class ProductionResourceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: int
    resource_name: str
    capacity: float
    planning_range: int
    shift_offset: int
    assembly_rate_count: int


class ProductionResourceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[ProductionResourceRow]
    total: int
    limit: int
    offset: int


class ProductionResourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity: Optional[Decimal] = Field(default=None, ge=0)
    planning_range: Optional[int] = Field(default=None, ge=1, le=3650)


class DeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: bool
    id: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _f(value: Any) -> float:
    return float(value or 0)


def _require_item(db: Session, item_id: int) -> models.Item:
    item = db.get(models.Item, int(item_id))
    if item is None:
        raise HTTPException(status_code=422, detail=f"item {int(item_id)} does not exist")
    return item


def _require_resource(db: Session, resource_id: int) -> models.ProductionResource:
    resource = db.get(models.ProductionResource, int(resource_id))
    if resource is None:
        raise HTTPException(
            status_code=422,
            detail=f"production resource {int(resource_id)} does not exist",
        )
    return resource


def _item_labels(db: Session, item_ids: set[int]) -> dict[int, models.Item]:
    if not item_ids:
        return {}
    rows = (
        db.query(models.Item)
        .filter(models.Item.item_id.in_(sorted(item_ids)))
        .all()
    )
    return {int(row.item_id): row for row in rows}


def _resource_labels(db: Session, resource_ids: set[int]) -> dict[int, str]:
    if not resource_ids:
        return {}
    rows = (
        db.query(
            models.ProductionResource.resource_id,
            models.ProductionResource.resource_name,
        )
        .filter(models.ProductionResource.resource_id.in_(sorted(resource_ids)))
        .all()
    )
    return {int(resource_id): str(name) for resource_id, name in rows}


def _warehouse_labels(db: Session, refs: set[str]) -> dict[str, str]:
    if not refs:
        return {}
    rows = (
        db.query(
            models.StockWarehouse.warehouse_ref1c,
            models.StockWarehouse.warehouse_name,
        )
        .filter(models.StockWarehouse.warehouse_ref1c.in_(sorted(refs)))
        .all()
    )
    return {str(ref): str(name) for ref, name in rows}


def _rate_payload(
    row: models.AssemblyRate,
    items: dict[int, models.Item],
    resources: dict[int, str],
) -> dict[str, Any]:
    item = items.get(int(row.item_id))
    return {
        "id": int(row.id),
        "item_id": int(row.item_id),
        "item_code": item.item_code if item is not None else None,
        "item_name": item.item_name if item is not None else None,
        "resource_id": int(row.resource_id),
        "resource_name": resources.get(int(row.resource_id)),
        "qty_per_capacity": _f(row.qty_per_capacity),
    }


def _policy_payload(
    row: models.ShelfPolicy,
    items: dict[int, models.Item],
    warehouses: dict[str, str],
) -> dict[str, Any]:
    item = items.get(int(row.item_id))
    return {
        "id": int(row.id),
        "item_id": int(row.item_id),
        "item_code": item.item_code if item is not None else None,
        "item_name": item.item_name if item is not None else None,
        "warehouse_ref1c": str(row.warehouse_ref1c),
        "warehouse_name": warehouses.get(str(row.warehouse_ref1c)),
        "replenishment_time_days": int(row.replenishment_time_days or 0),
        "review_cycle_days": int(row.review_cycle_days or 0),
        "safety_days": int(row.safety_days or 0),
        "batch_multiple": _f(row.batch_multiple),
        "active": bool(row.active),
    }


# ---------------------------------------------------------------------------
# Assembly rates (такт сборки)
# ---------------------------------------------------------------------------


@router.get("/assembly-rates", response_model=AssemblyRateListResponse)
def list_assembly_rates(
    item_id: Optional[int] = Query(default=None, ge=1),
    resource_id: Optional[int] = Query(default=None, ge=1),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> AssemblyRateListResponse:
    """List configured assembly takts, newest filter-first, paginated."""
    query = db.query(models.AssemblyRate)
    if item_id is not None:
        query = query.filter(models.AssemblyRate.item_id == int(item_id))
    if resource_id is not None:
        query = query.filter(models.AssemblyRate.resource_id == int(resource_id))
    total = int(query.count() or 0)
    rows = (
        query.order_by(
            models.AssemblyRate.resource_id.asc(),
            models.AssemblyRate.item_id.asc(),
            models.AssemblyRate.id.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = _item_labels(db, {int(row.item_id) for row in rows})
    resources = _resource_labels(db, {int(row.resource_id) for row in rows})
    return AssemblyRateListResponse.model_validate(
        {
            "rows": [_rate_payload(row, items, resources) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.put("/assembly-rates", response_model=AssemblyRateUpsertResponse)
def upsert_assembly_rates(
    payload: AssemblyRateUpsertRequest,
    db: Session = Depends(get_db),
) -> AssemblyRateUpsertResponse:
    """Upsert takts by (resource_id, item_id); the pair is unique by model."""
    seen: set[tuple[int, int]] = set()
    for row in payload.rows:
        key = (int(row.resource_id), int(row.item_id))
        if key in seen:
            raise HTTPException(
                status_code=422,
                detail=(
                    "duplicate (resource_id, item_id) pair in payload: "
                    f"{key[0]}/{key[1]}"
                ),
            )
        seen.add(key)

    created = 0
    updated = 0
    touched: list[models.AssemblyRate] = []
    for row in payload.rows:
        _require_item(db, int(row.item_id))
        _require_resource(db, int(row.resource_id))
        existing = (
            db.query(models.AssemblyRate)
            .filter(
                models.AssemblyRate.resource_id == int(row.resource_id),
                models.AssemblyRate.item_id == int(row.item_id),
            )
            .one_or_none()
        )
        if existing is None:
            existing = models.AssemblyRate(
                resource_id=int(row.resource_id),
                item_id=int(row.item_id),
                qty_per_capacity=row.qty_per_capacity,
            )
            db.add(existing)
            created += 1
        else:
            existing.qty_per_capacity = row.qty_per_capacity
            updated += 1
        touched.append(existing)
    db.flush()
    db.commit()
    for row in touched:
        db.refresh(row)

    items = _item_labels(db, {int(row.item_id) for row in touched})
    resources = _resource_labels(db, {int(row.resource_id) for row in touched})
    return AssemblyRateUpsertResponse.model_validate(
        {
            "rows": [_rate_payload(row, items, resources) for row in touched],
            "created": created,
            "updated": updated,
        }
    )


@router.delete("/assembly-rates/{rate_id}", response_model=DeleteResponse)
def delete_assembly_rate(
    rate_id: int,
    db: Session = Depends(get_db),
) -> DeleteResponse:
    row = db.get(models.AssemblyRate, int(rate_id))
    if row is None:
        raise HTTPException(status_code=404, detail=f"assembly rate {rate_id} not found")
    db.delete(row)
    db.commit()
    return DeleteResponse(deleted=True, id=int(rate_id))


# ---------------------------------------------------------------------------
# Shelf policies (полки)
# ---------------------------------------------------------------------------


@router.get("/shelf-policies", response_model=ShelfPolicyListResponse)
def list_shelf_policies(
    item_id: Optional[int] = Query(default=None, ge=1),
    active: Optional[bool] = Query(default=None),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ShelfPolicyListResponse:
    query = db.query(models.ShelfPolicy)
    if item_id is not None:
        query = query.filter(models.ShelfPolicy.item_id == int(item_id))
    if active is not None:
        query = query.filter(models.ShelfPolicy.active.is_(bool(active)))
    total = int(query.count() or 0)
    rows = (
        query.order_by(
            models.ShelfPolicy.item_id.asc(),
            models.ShelfPolicy.id.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = _item_labels(db, {int(row.item_id) for row in rows})
    warehouses = _warehouse_labels(db, {str(row.warehouse_ref1c) for row in rows})
    return ShelfPolicyListResponse.model_validate(
        {
            "rows": [_policy_payload(row, items, warehouses) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.post("/shelf-policies", response_model=ShelfPolicyRow, status_code=201)
def create_shelf_policy(
    payload: ShelfPolicyCreate,
    db: Session = Depends(get_db),
) -> ShelfPolicyRow:
    _require_item(db, int(payload.item_id))
    duplicate = (
        db.query(models.ShelfPolicy)
        .filter(
            models.ShelfPolicy.item_id == int(payload.item_id),
            models.ShelfPolicy.warehouse_ref1c == payload.warehouse_ref1c,
        )
        .one_or_none()
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "shelf policy already exists for item "
                f"{int(payload.item_id)} on warehouse {payload.warehouse_ref1c}"
            ),
        )
    row = models.ShelfPolicy(
        item_id=int(payload.item_id),
        warehouse_ref1c=payload.warehouse_ref1c,
        replenishment_time_days=int(payload.replenishment_time_days),
        review_cycle_days=int(payload.review_cycle_days),
        safety_days=int(payload.safety_days),
        batch_multiple=payload.batch_multiple,
        active=bool(payload.active),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    items = _item_labels(db, {int(row.item_id)})
    warehouses = _warehouse_labels(db, {str(row.warehouse_ref1c)})
    return ShelfPolicyRow.model_validate(_policy_payload(row, items, warehouses))


@router.put("/shelf-policies/{policy_id}", response_model=ShelfPolicyRow)
def update_shelf_policy(
    policy_id: int,
    payload: ShelfPolicyUpdate,
    db: Session = Depends(get_db),
) -> ShelfPolicyRow:
    row = db.get(models.ShelfPolicy, int(policy_id))
    if row is None:
        raise HTTPException(status_code=404, detail=f"shelf policy {policy_id} not found")
    if payload.warehouse_ref1c is not None and payload.warehouse_ref1c != row.warehouse_ref1c:
        duplicate = (
            db.query(models.ShelfPolicy)
            .filter(
                models.ShelfPolicy.item_id == int(row.item_id),
                models.ShelfPolicy.warehouse_ref1c == payload.warehouse_ref1c,
                models.ShelfPolicy.id != int(policy_id),
            )
            .one_or_none()
        )
        if duplicate is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "shelf policy already exists for item "
                    f"{int(row.item_id)} on warehouse {payload.warehouse_ref1c}"
                ),
            )
        row.warehouse_ref1c = payload.warehouse_ref1c
    if payload.replenishment_time_days is not None:
        row.replenishment_time_days = int(payload.replenishment_time_days)
    if payload.review_cycle_days is not None:
        row.review_cycle_days = int(payload.review_cycle_days)
    if payload.safety_days is not None:
        row.safety_days = int(payload.safety_days)
    if payload.batch_multiple is not None:
        row.batch_multiple = payload.batch_multiple
    if payload.active is not None:
        row.active = bool(payload.active)
    db.commit()
    db.refresh(row)
    items = _item_labels(db, {int(row.item_id)})
    warehouses = _warehouse_labels(db, {str(row.warehouse_ref1c)})
    return ShelfPolicyRow.model_validate(_policy_payload(row, items, warehouses))


@router.delete("/shelf-policies/{policy_id}", response_model=DeleteResponse)
def delete_shelf_policy(
    policy_id: int,
    db: Session = Depends(get_db),
) -> DeleteResponse:
    row = db.get(models.ShelfPolicy, int(policy_id))
    if row is None:
        raise HTTPException(status_code=404, detail=f"shelf policy {policy_id} not found")
    referencing = (
        db.query(models.ShelfProjection)
        .filter(models.ShelfProjection.shelf_policy_id == int(policy_id))
        .count()
    )
    if referencing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"shelf policy {policy_id} is referenced by {referencing} persisted "
                "projection(s); deactivate it instead of deleting"
            ),
        )
    db.delete(row)
    db.commit()
    return DeleteResponse(deleted=True, id=int(policy_id))


# ---------------------------------------------------------------------------
# Production resources (мощность участка)
# ---------------------------------------------------------------------------


@router.get("/resources", response_model=ProductionResourceListResponse)
def list_planning_resources(
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ProductionResourceListResponse:
    """Resources with the takt count that already points at them."""
    query = db.query(models.ProductionResource)
    total = int(query.count() or 0)
    rows = (
        query.order_by(models.ProductionResource.resource_id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    rate_counts: dict[int, int] = {}
    if rows:
        counted = (
            db.query(models.AssemblyRate.resource_id)
            .filter(
                models.AssemblyRate.resource_id.in_(
                    [int(row.resource_id) for row in rows]
                )
            )
            .all()
        )
        for (resource_id,) in counted:
            rate_counts[int(resource_id)] = rate_counts.get(int(resource_id), 0) + 1
    return ProductionResourceListResponse.model_validate(
        {
            "rows": [
                {
                    "resource_id": int(row.resource_id),
                    "resource_name": str(row.resource_name),
                    "capacity": _f(row.capacity),
                    "planning_range": int(row.planning_range or 0),
                    "shift_offset": int(row.shift_offset or 0),
                    "assembly_rate_count": rate_counts.get(int(row.resource_id), 0),
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.patch("/resources/{resource_id}", response_model=ProductionResourceRow)
def patch_planning_resource(
    resource_id: int,
    payload: ProductionResourcePatch,
    db: Session = Depends(get_db),
) -> ProductionResourceRow:
    row = db.get(models.ProductionResource, int(resource_id))
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"production resource {resource_id} not found"
        )
    if payload.capacity is None and payload.planning_range is None:
        raise HTTPException(status_code=422, detail="nothing to patch")
    if payload.capacity is not None:
        row.capacity = payload.capacity
    if payload.planning_range is not None:
        row.planning_range = int(payload.planning_range)
    db.commit()
    db.refresh(row)
    rate_count = (
        db.query(models.AssemblyRate)
        .filter(models.AssemblyRate.resource_id == int(resource_id))
        .count()
    )
    return ProductionResourceRow.model_validate(
        {
            "resource_id": int(row.resource_id),
            "resource_name": str(row.resource_name),
            "capacity": _f(row.capacity),
            "planning_range": int(row.planning_range or 0),
            "shift_offset": int(row.shift_offset or 0),
            "assembly_rate_count": int(rate_count or 0),
        }
    )

"""
DBR module — settings / assembly-rate / category-risk storage service.

Thin persistence layer over the module-owned dbr_* tables. All functions take
an active SQLAlchemy Session and never commit (the caller owns the
transaction), matching the convention of other services in this project.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from sqlalchemy.orm import Session

from ...models import (
    DbrAssemblyRate,
    DbrCategorySupplyRisk,
    DbrSettings,
    Item,
    ProductionResource,
)

SETTINGS_ID = 1
DEFAULT_SETTINGS: dict[str, Any] = {
    "frozen_days": 3,
    "gate_horizon_workdays": 10,
    "shelf_threshold_qty": Decimal("5"),
    "rt_machining_days": 7,
    "rt_welding_days": 15,
    "rt_painting_days": 21,
    "batch_days_turning": 10,
    "batch_days_bending": 7,
    "batch_days_welding": 5,
    "batch_days_paint_black": 2,
    "batch_days_paint_color": 3,
    "feeder_chain_enabled": False,
    "feeder_load_horizon_weeks": 4,
    "rt_processing_days": 25,
    "processing_trip_interval_days": 7,
    "processing_roundtrip_days": 14,
    "fastener_categories": [],
}

# Scalar fields that update_settings accepts. warehouse refs and numeric/bool
# fields are all plain assignments; unknown keys are ignored.
_SETTINGS_FIELDS = {
    "frozen_days",
    "gate_horizon_workdays",
    "shelf_threshold_qty",
    "rt_machining_days",
    "rt_welding_days",
    "rt_painting_days",
    "batch_days_turning",
    "batch_days_bending",
    "batch_days_welding",
    "batch_days_paint_black",
    "batch_days_paint_color",
    "feeder_chain_enabled",
    "feeder_load_horizon_weeks",
    "rt_processing_days",
    "processing_trip_interval_days",
    "processing_roundtrip_days",
    "w2_warehouse_ref1c",
    "w3_warehouse_ref1c",
    "w4_warehouse_ref1c",
    "fastener_categories",
}


# --------------------------------------------------------------------------
# Settings (singleton)
# --------------------------------------------------------------------------


def get_or_create_settings(db: Session) -> DbrSettings:
    """Return the singleton settings row (id=1), creating it with model
    defaults if it does not yet exist."""
    settings = db.get(DbrSettings, SETTINGS_ID)
    if settings is None:
        settings = DbrSettings(id=SETTINGS_ID)
        db.add(settings)
        db.flush()
    return settings


def read_settings(db: Session) -> DbrSettings:
    """Pure read for GET/board consumers; transient defaults are not persisted."""
    settings = db.get(DbrSettings, SETTINGS_ID)
    return (
        settings
        if settings is not None
        else DbrSettings(id=SETTINGS_ID, **{**DEFAULT_SETTINGS, "fastener_categories": []})
    )


def update_settings(db: Session, payload: dict[str, Any]) -> DbrSettings:
    """Patch the singleton settings row with the provided fields. Only known
    fields are applied; ``None`` values are written (to allow clearing
    warehouse roles)."""
    settings = get_or_create_settings(db)
    for key, value in payload.items():
        if key in _SETTINGS_FIELDS:
            setattr(settings, key, value)
    db.flush()
    return settings


# --------------------------------------------------------------------------
# Assembly rates
# --------------------------------------------------------------------------


def list_assembly_rates(db: Session) -> list[dict[str, Any]]:
    """List assembly rates joined with resource and item display names."""
    rows = (
        db.query(DbrAssemblyRate, ProductionResource, Item)
        .join(ProductionResource, DbrAssemblyRate.resource_id == ProductionResource.resource_id)
        .join(Item, DbrAssemblyRate.item_id == Item.item_id)
        .order_by(ProductionResource.resource_name, Item.item_code)
        .all()
    )
    result: list[dict[str, Any]] = []
    for rate, resource, item in rows:
        result.append(
            {
                "id": rate.id,
                "resource_id": rate.resource_id,
                "resource_name": resource.resource_name,
                "item_id": rate.item_id,
                "item_code": item.item_code,
                "item_name": item.item_name,
                "qty_per_capacity": rate.qty_per_capacity,
            }
        )
    return result


def upsert_assembly_rate(
    db: Session,
    resource_id: int,
    item_id: int,
    qty_per_capacity: Any,
) -> DbrAssemblyRate:
    """Insert or update the takt for a (resource_id, item_id) pair."""
    try:
        normalized_qty = Decimal(str(qty_per_capacity))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("qty_per_capacity must be a positive number") from exc
    if not normalized_qty.is_finite() or normalized_qty <= 0:
        raise ValueError("qty_per_capacity must be greater than zero")

    rate = (
        db.query(DbrAssemblyRate)
        .filter(
            DbrAssemblyRate.resource_id == resource_id,
            DbrAssemblyRate.item_id == item_id,
        )
        .one_or_none()
    )
    if rate is None:
        rate = DbrAssemblyRate(
            resource_id=resource_id,
            item_id=item_id,
            qty_per_capacity=normalized_qty,
        )
        db.add(rate)
    else:
        rate.qty_per_capacity = normalized_qty
    db.flush()
    return rate


def delete_assembly_rate(db: Session, rate_id: int) -> bool:
    """Delete a takt by primary key. Returns True if a row was removed."""
    rate = db.get(DbrAssemblyRate, rate_id)
    if rate is None:
        return False
    db.delete(rate)
    db.flush()
    return True


# --------------------------------------------------------------------------
# Category supply-risk
# --------------------------------------------------------------------------


def list_category_risks(db: Session) -> list[DbrCategorySupplyRisk]:
    """List all category supply-risk rows ordered by item group."""
    return (
        db.query(DbrCategorySupplyRisk)
        .order_by(DbrCategorySupplyRisk.item_group)
        .all()
    )


def replace_category_risks(
    db: Session, rows: Iterable[dict[str, Any]]
) -> list[DbrCategorySupplyRisk]:
    """Replace category supply-risk rows, preserving IDs for retained groups."""
    existing = {r.item_group: r for r in db.query(DbrCategorySupplyRisk).all()}
    touched: list[DbrCategorySupplyRisk] = []
    retained_groups: set[str] = set()
    for row in rows:
        group = row.get("item_group")
        if not group:
            continue
        retained_groups.add(group)
        receipt = row.get("receipt_warehouse_ref1c")
        risk = row.get("supply_risk_pct")
        obj = existing.get(group)
        if obj is None:
            obj = DbrCategorySupplyRisk(
                item_group=group,
                receipt_warehouse_ref1c=receipt,
                supply_risk_pct=risk,
            )
            db.add(obj)
            existing[group] = obj
        else:
            obj.receipt_warehouse_ref1c = receipt
            obj.supply_risk_pct = risk
        touched.append(obj)
    for group, obj in existing.items():
        if group not in retained_groups:
            db.delete(obj)
    db.flush()
    return touched

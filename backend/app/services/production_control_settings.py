from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session, joinedload

from ..models import IgnoredWarehouse, ProductionResource, WorkshopWarehouseBinding


def _binding_payload(binding: WorkshopWarehouseBinding) -> Dict[str, Any]:
    workshop_name = None
    try:
        if binding.workshop:
            workshop_name = str(binding.workshop.resource_name or "")
    except Exception:
        workshop_name = None

    return {
        "binding_id": int(binding.binding_id),
        "workshop_id": int(binding.workshop_id),
        "workshop_name": workshop_name,
        "warehouse_ref1c": str(binding.warehouse_ref1c or ""),
    }


def _ignored_payload(row: IgnoredWarehouse) -> Dict[str, Any]:
    return {
        "warehouse_ref1c": str(row.warehouse_ref1c),
        "warehouse_name": str(row.warehouse_name or "") if row.warehouse_name else None,
        "reason": str(row.reason or "") if row.reason else None,
    }


def list_settings(db: Session) -> Dict[str, Any]:
    bindings = (
        db.query(WorkshopWarehouseBinding)
        .options(joinedload(WorkshopWarehouseBinding.workshop))
        .order_by(WorkshopWarehouseBinding.workshop_id.asc())
        .all()
    )
    ignored = (
        db.query(IgnoredWarehouse)
        .order_by(IgnoredWarehouse.warehouse_ref1c.asc())
        .all()
    )
    return {
        "workshop_warehouse_bindings": [_binding_payload(binding) for binding in bindings],
        "ignored_warehouses": [_ignored_payload(row) for row in ignored],
    }


def upsert_workshop_binding(db: Session, workshop_id: int, warehouse_ref1c: str) -> Dict[str, Any]:
    workshop_id_int = int(workshop_id)
    wh = str(warehouse_ref1c or "").strip()
    if not wh:
        raise ValueError("warehouse_ref1c is required")

    workshop = (
        db.query(ProductionResource)
        .filter(ProductionResource.resource_id == workshop_id_int)
        .first()
    )
    if not workshop:
        raise ValueError(
            f"workshop_id={workshop_id_int}: \u0443\u0447\u0430\u0441\u0442\u043e\u043a "
            "\u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d"
        )

    binding = (
        db.query(WorkshopWarehouseBinding)
        .filter(WorkshopWarehouseBinding.workshop_id == workshop_id_int)
        .first()
    )
    if binding is None:
        binding = WorkshopWarehouseBinding(workshop_id=workshop_id_int, warehouse_ref1c=wh)
        db.add(binding)
    else:
        binding.warehouse_ref1c = wh
    db.commit()

    binding = (
        db.query(WorkshopWarehouseBinding)
        .options(joinedload(WorkshopWarehouseBinding.workshop))
        .filter(WorkshopWarehouseBinding.workshop_id == workshop_id_int)
        .first()
    )
    return _binding_payload(binding)


def delete_workshop_binding(db: Session, workshop_id: int) -> Dict[str, Any]:
    workshop_id_int = int(workshop_id)
    deleted = (
        db.query(WorkshopWarehouseBinding)
        .filter(WorkshopWarehouseBinding.workshop_id == workshop_id_int)
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"deleted": int(deleted), "workshop_id": workshop_id_int}


def upsert_ignored_warehouse(
    db: Session,
    warehouse_ref1c: str,
    *,
    warehouse_name: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    wh = str(warehouse_ref1c or "").strip()
    if not wh:
        raise ValueError("warehouse_ref1c is required")

    row = db.query(IgnoredWarehouse).filter(IgnoredWarehouse.warehouse_ref1c == wh).first()
    if row is None:
        row = IgnoredWarehouse(
            warehouse_ref1c=wh,
            warehouse_name=warehouse_name or None,
            reason=reason or None,
        )
        db.add(row)
    else:
        if warehouse_name is not None:
            row.warehouse_name = warehouse_name or None
        if reason is not None:
            row.reason = reason or None
    db.commit()
    return _ignored_payload(row)


def delete_ignored_warehouse(db: Session, warehouse_ref1c: str) -> Dict[str, Any]:
    wh = str(warehouse_ref1c or "").strip()
    if not wh:
        raise ValueError("warehouse_ref1c is required")

    deleted = (
        db.query(IgnoredWarehouse)
        .filter(IgnoredWarehouse.warehouse_ref1c == wh)
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"deleted": int(deleted), "warehouse_ref1c": wh}

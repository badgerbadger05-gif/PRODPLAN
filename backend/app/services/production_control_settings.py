from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session, joinedload

from ..models import IgnoredWarehouse, ProductionResource, StockWarehouse, WorkshopWarehouseBinding


def _binding_payload(binding: WorkshopWarehouseBinding) -> Dict[str, Any]:
    workshop_name = None
    try:
        if binding.workshop:
            workshop_name = str(binding.workshop.resource_name or "")
    except Exception:
        workshop_name = None

    return {
        "binding_id": int(binding.binding_id),
        "resource_id": int(binding.workshop_id),
        "workshop_id": int(binding.workshop_id),
        "workshop_name": workshop_name,
        "warehouse_ref1c": str(binding.warehouse_ref1c or ""),
        "production_warehouse_ref1c": str(binding.production_warehouse_ref1c or ""),
    }


def _ignored_payload(row: IgnoredWarehouse) -> Dict[str, Any]:
    return {
        "warehouse_ref1c": str(row.warehouse_ref1c),
        "warehouse_name": str(row.warehouse_name or "") if row.warehouse_name else None,
        "reason": str(row.reason or "") if row.reason else None,
    }


def _warehouse_payload(row: StockWarehouse) -> Dict[str, Any]:
    return {
        "warehouse_id": int(row.warehouse_id),
        "warehouse_ref1c": str(row.warehouse_ref1c or ""),
        "warehouse_code": str(row.warehouse_code or ""),
        "warehouse_name": str(row.warehouse_name or ""),
        "is_selected": bool(row.is_selected),
    }


def _is_settings_warehouse(row: StockWarehouse) -> bool:
    name = str(row.warehouse_name or "").casefold()
    if not name:
        return False
    storage_markers = (
        "склад",
        "склады",
        "складск",
        "кладов",
        "участок",
        "изолятор",
        "производственное",
        "управлен",
        "возврат",
        "ремонт",
        "технологические нужды",
        "разработка",
        "техноплюс",
        "станки",
        "металл-сборка",
    )
    return any(marker in name for marker in storage_markers)


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
    warehouses = (
        db.query(StockWarehouse)
        .order_by(StockWarehouse.warehouse_name.asc(), StockWarehouse.warehouse_code.asc())
        .all()
    )
    warehouses = [row for row in warehouses if _is_settings_warehouse(row)]
    workshop_warehouses = [_binding_payload(binding) for binding in bindings]
    return {
        "warehouses": [_warehouse_payload(row) for row in warehouses],
        "workshop_warehouses": workshop_warehouses,
        "workshop_warehouse_bindings": workshop_warehouses,
        "ignored_warehouses": [_ignored_payload(row) for row in ignored],
    }


def replace_settings(
    db: Session,
    *,
    workshop_warehouses: Iterable[Dict[str, Any]],
    ignored_warehouses: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    desired_bindings: Dict[int, str] = {}
    desired_production_bindings: Dict[int, Optional[str]] = {}
    for row in workshop_warehouses or []:
        workshop_id = int(row.get("resource_id") or row.get("workshop_id") or 0)
        warehouse_ref = str(row.get("warehouse_ref1c") or "").strip()
        production_warehouse_ref = str(row.get("production_warehouse_ref1c") or "").strip()
        if workshop_id > 0 and warehouse_ref:
            desired_bindings[workshop_id] = warehouse_ref
            desired_production_bindings[workshop_id] = production_warehouse_ref or None

    existing_bindings = {
        int(row.workshop_id): row
        for row in db.query(WorkshopWarehouseBinding).all()
    }
    for workshop_id, binding in existing_bindings.items():
        if workshop_id not in desired_bindings:
            db.delete(binding)
    valid_workshops = {
        int(row.resource_id)
        for row in db.query(ProductionResource.resource_id)
        .filter(ProductionResource.resource_id.in_(list(desired_bindings.keys()) or [0]))
        .all()
    }
    missing = sorted(set(desired_bindings.keys()) - valid_workshops)
    if missing:
        raise ValueError(f"Участки не найдены: {', '.join(map(str, missing))}")
    for workshop_id, warehouse_ref in desired_bindings.items():
        production_warehouse_ref = desired_production_bindings.get(workshop_id) or None
        binding = existing_bindings.get(workshop_id)
        if binding is None:
            db.add(
                WorkshopWarehouseBinding(
                    workshop_id=workshop_id,
                    warehouse_ref1c=warehouse_ref,
                    production_warehouse_ref1c=production_warehouse_ref,
                )
            )
        else:
            binding.warehouse_ref1c = warehouse_ref
            binding.production_warehouse_ref1c = production_warehouse_ref

    desired_ignored: Dict[str, Dict[str, Any]] = {}
    for row in ignored_warehouses or []:
        warehouse_ref = str(row.get("warehouse_ref1c") or "").strip()
        if warehouse_ref:
            desired_ignored[warehouse_ref] = row

    existing_ignored = {
        str(row.warehouse_ref1c): row
        for row in db.query(IgnoredWarehouse).all()
    }
    for warehouse_ref, row in existing_ignored.items():
        if warehouse_ref not in desired_ignored:
            db.delete(row)
    for warehouse_ref, payload in desired_ignored.items():
        row = existing_ignored.get(warehouse_ref)
        warehouse_name = payload.get("warehouse_name")
        reason = payload.get("reason")
        if row is None:
            db.add(
                IgnoredWarehouse(
                    warehouse_ref1c=warehouse_ref,
                    warehouse_name=str(warehouse_name) if warehouse_name else None,
                    reason=str(reason) if reason else None,
                )
            )
        else:
            if warehouse_name is not None:
                row.warehouse_name = str(warehouse_name) if warehouse_name else None
            if reason is not None:
                row.reason = str(reason) if reason else None

    db.commit()
    return list_settings(db)


def upsert_workshop_binding(
    db: Session,
    workshop_id: int,
    warehouse_ref1c: str,
    *,
    production_warehouse_ref1c: Optional[str] = None,
) -> Dict[str, Any]:
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
        binding = WorkshopWarehouseBinding(
            workshop_id=workshop_id_int,
            warehouse_ref1c=wh,
            production_warehouse_ref1c=str(production_warehouse_ref1c or "").strip() or None,
        )
        db.add(binding)
    else:
        binding.warehouse_ref1c = wh
        if production_warehouse_ref1c is not None:
            binding.production_warehouse_ref1c = str(production_warehouse_ref1c or "").strip() or None
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

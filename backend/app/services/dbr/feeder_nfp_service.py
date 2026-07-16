"""Read-only live NFP projection for static supermarket positions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from sqlalchemy.orm import Session

from ...models import (
    DbrDrumSchedule,
    DbrSupermarketPosition,
    IgnoredWarehouse,
    ItemWarehouseStock,
    ProductionOrder,
    ProductionProduct,
    StockWarehouse,
    SupplierOrder,
    SupplierOrderItem,
)
from ..production_control_reservations import load_reservation_state
from .core.feeder import zones


def _normalize_ref(value: Any) -> str:
    return str(value or "").strip().casefold()


def _latest(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _stock_by_position(
    db: Session, positions: Iterable[DbrSupermarketPosition]
) -> tuple[dict[int, float], dict[int, datetime | None], set[str]]:
    positions = list(positions)
    item_ids = {int(row.item_id) for row in positions}
    ignored = {
        _normalize_ref(ref) for (ref,) in db.query(IgnoredWarehouse.warehouse_ref1c).all() if ref
    }
    selected = {
        _normalize_ref(ref)
        for ref, is_selected in db.query(
            StockWarehouse.warehouse_ref1c, StockWarehouse.is_selected
        ).all()
        if ref and is_selected and _normalize_ref(ref) not in ignored
    }
    stock_rows = (
        db.query(
            ItemWarehouseStock.item_id,
            ItemWarehouseStock.warehouse_ref1c,
            ItemWarehouseStock.qty,
            ItemWarehouseStock.updated_at,
        )
        .filter(ItemWarehouseStock.item_id.in_(item_ids))
        .all()
        if item_ids
        else []
    )
    exact: dict[tuple[int, str], float] = {}
    exact_as_of: dict[tuple[int, str], datetime | None] = {}
    enterprise: dict[int, float] = {}
    enterprise_as_of: dict[int, datetime | None] = {}
    for item_id, warehouse, qty, updated_at in stock_rows:
        warehouse_key = _normalize_ref(warehouse)
        key = (int(item_id), warehouse_key)
        exact[key] = exact.get(key, 0.0) + float(qty or 0)
        exact_as_of[key] = _latest(exact_as_of.get(key), updated_at)
        if warehouse_key in selected:
            enterprise[int(item_id)] = enterprise.get(int(item_id), 0.0) + float(qty or 0)
            enterprise_as_of[int(item_id)] = _latest(
                enterprise_as_of.get(int(item_id)), updated_at
            )
    values = {
        int(position.id): (
            enterprise.get(int(position.item_id), 0.0)
            if position.supply_type == "purchase"
            else exact.get((int(position.item_id), _normalize_ref(position.warehouse_ref1c)), 0.0)
        )
        for position in positions
    }
    as_of = {
        int(position.id): (
            enterprise_as_of.get(int(position.item_id))
            if position.supply_type == "purchase"
            else exact_as_of.get(
                (int(position.item_id), _normalize_ref(position.warehouse_ref1c))
            )
        )
        for position in positions
    }
    return values, as_of, selected


def _open_supply(
    db: Session, positions: Iterable[DbrSupermarketPosition]
) -> tuple[dict[int, float], dict[int, int], dict[int, datetime | None]]:
    positions = list(positions)
    item_ids = {int(row.item_id) for row in positions}
    manufacture: dict[tuple[int, str], float] = {}
    manufacture_as_of: dict[tuple[int, str], datetime | None] = {}
    manufacture_null: dict[int, int] = {}
    manufacture_null_as_of: dict[int, datetime | None] = {}
    for product, order in (
        db.query(ProductionProduct, ProductionOrder)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .filter(
            ProductionProduct.item_id.in_(item_ids),
            ProductionProduct.remaining_qty > 0,
            ProductionOrder.deletion_mark.is_(False),
        )
        .all()
        if item_ids
        else []
    ):
        item_id = int(product.item_id)
        destination = _normalize_ref(product.destination_warehouse_ref1c)
        source_as_of = _latest(product.updated_at, order.updated_at)
        if not destination:
            manufacture_null[item_id] = manufacture_null.get(item_id, 0) + 1
            manufacture_null_as_of[item_id] = _latest(
                manufacture_null_as_of.get(item_id), source_as_of
            )
            continue
        key = (item_id, destination)
        manufacture[key] = manufacture.get(key, 0.0) + float(product.remaining_qty or 0)
        manufacture_as_of[key] = _latest(manufacture_as_of.get(key), source_as_of)

    purchase: dict[tuple[int, str], float] = {}
    purchase_as_of: dict[tuple[int, str], datetime | None] = {}
    purchase_null: dict[int, int] = {}
    purchase_null_as_of: dict[int, datetime | None] = {}
    for line, order in (
        db.query(SupplierOrderItem, SupplierOrder)
        .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
        .filter(
            SupplierOrderItem.item_id_ref.in_(item_ids),
            SupplierOrderItem.remaining_qty > 0,
            SupplierOrder.deletion_mark.is_(False),
        )
        .all()
        if item_ids
        else []
    ):
        item_id = int(line.item_id_ref)
        destination = _normalize_ref(line.destination_warehouse_ref1c)
        source_as_of = _latest(line.updated_at, order.updated_at)
        if not destination:
            purchase_null[item_id] = purchase_null.get(item_id, 0) + 1
            purchase_null_as_of[item_id] = _latest(
                purchase_null_as_of.get(item_id), source_as_of
            )
            continue
        key = (item_id, destination)
        purchase[key] = purchase.get(key, 0.0) + float(line.remaining_qty or 0)
        purchase_as_of[key] = _latest(purchase_as_of.get(key), source_as_of)

    values: dict[int, float] = {}
    null_counts: dict[int, int] = {}
    as_of: dict[int, datetime | None] = {}
    for position in positions:
        position_id = int(position.id)
        item_id = int(position.item_id)
        key = (item_id, _normalize_ref(position.warehouse_ref1c))
        if position.supply_type == "purchase":
            values[position_id] = purchase.get(key, 0.0)
            null_counts[position_id] = purchase_null.get(item_id, 0)
            as_of[position_id] = _latest(
                purchase_as_of.get(key), purchase_null_as_of.get(item_id)
            )
        else:
            values[position_id] = manufacture.get(key, 0.0)
            null_counts[position_id] = manufacture_null.get(item_id, 0)
            as_of[position_id] = _latest(
                manufacture_as_of.get(key), manufacture_null_as_of.get(item_id)
            )
    return values, null_counts, as_of


def live_nfp_rows(db: Session, positions: Iterable[DbrSupermarketPosition]) -> dict[int, dict[str, Any]]:
    positions = list(positions)
    if not positions:
        return {}
    item_ids = {int(row.item_id) for row in positions}
    stocks, stock_as_of, selected_warehouses = _stock_by_position(db, positions)
    supplies, null_supply, supply_as_of = _open_supply(db, positions)
    reservation_state = load_reservation_state(db, item_ids=item_ids)
    reservations: dict[tuple[str, int], float] = {}
    for (warehouse, item_id), qty in reservation_state.by_warehouse_item.items():
        key = (_normalize_ref(warehouse), int(item_id))
        reservations[key] = reservations.get(key, 0.0) + float(qty or 0)
    active_schedule_id = (
        db.query(DbrDrumSchedule.id)
        .filter(DbrDrumSchedule.status == "active")
        .scalar()
    )
    now = datetime.now()
    result: dict[int, dict[str, Any]] = {}
    for position in positions:
        position_id = int(position.id)
        stock = stocks.get(position_id, 0.0)
        open_supply = supplies.get(position_id, 0.0)
        item_id = int(position.item_id)
        if position.supply_type == "purchase":
            qualified_demand = sum(
                qty
                for (warehouse, reserved_item_id), qty in reservations.items()
                if reserved_item_id == item_id and warehouse in selected_warehouses
            )
        else:
            qualified_demand = reservations.get(
                (_normalize_ref(position.warehouse_ref1c), item_id), 0.0
            )
        nfp = stock + open_supply - qualified_demand
        position_zones = zones.Zones(
            red=float(position.red_qty or 0),
            yellow=float(position.yellow_qty or 0),
            green=float(position.green_qty or 0),
        )
        missing_reasons: list[str] = []
        quality = list(position.data_quality or [])
        if null_supply.get(position_id, 0):
            missing_reasons.append("open_supply_destination_missing")
            quality.append(
                f"{null_supply[position_id]} open supply line(s) have NULL destination"
            )
        if (
            position.is_stale
            or active_schedule_id is None
            or position.source_schedule_id != active_schedule_id
        ):
            missing_reasons.append("stale_schedule")
        result[position_id] = {
            "stock_qty": stock,
            "open_supply_qty": open_supply,
            "qualified_demand_qty": qualified_demand,
            "nfp": nfp,
            "zone": zones.nfp_zone(nfp, position_zones),
            "penetration": zones.penetration(nfp, position_zones),
            "is_complete": not missing_reasons,
            "missing_reasons": missing_reasons,
            "data_quality": quality,
            "formula": "stock_qty + open_supply_qty - qualified_demand_qty",
            "timestamps": {
                "stock_as_of": stock_as_of.get(position_id),
                "supply_as_of": supply_as_of.get(position_id),
                "position_calculated_at": position.calculated_at,
                "live_calculated_at": now,
            },
        }
    return result

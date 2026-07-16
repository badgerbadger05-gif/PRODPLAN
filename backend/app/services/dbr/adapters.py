"""DBR Phase 1 — read adapters over shared PRODPLAN tables.

Thin, read-only helpers that turn the shared schema (items / specifications /
stock / orders / calendar / assembly rates) into the plain-Python inputs the
drum cores expect. Every function takes a live Session, reads only, and never
writes to shared tables (module invariant, roadmap §3.1).

Kit/gate identifiers are ``item_code`` strings (leveling groups by SKU code),
so most maps translate between ``item_id`` and ``item_code``.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from ...models import (
    DbrAssemblyRate,
    DefaultSpecification,
    IgnoredWarehouse,
    Item,
    ItemWarehouseStock,
    Operation,
    ProductionStage,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    SpecComponent,
    SpecOperation,
    StockWarehouse,
    SupplierOrder,
    SupplierOrderItem,
    WorkCalendarDay,
)
from .core.drum import assembly


def item_route_text_map(db: Session) -> dict[str, str]:
    """Normalized operation + stage text for each item's default specification."""
    default_specs: dict[int, int] = {}
    for row in db.query(DefaultSpecification).order_by(DefaultSpecification.id).all():
        default_specs.setdefault(int(row.item_id), int(row.spec_id))
    if not default_specs:
        return {}
    item_codes = {
        int(item_id): str(code)
        for item_id, code in db.query(Item.item_id, Item.item_code)
        .filter(Item.item_id.in_(default_specs))
        .all()
    }
    spec_to_items: dict[int, list[int]] = {}
    for item_id, spec_id in default_specs.items():
        spec_to_items.setdefault(spec_id, []).append(item_id)
    text_by_spec: dict[int, list[str]] = {}
    rows = (
        db.query(SpecOperation.spec_id, Operation.operation_name, ProductionStage.stage_name)
        .join(Operation, Operation.operation_id == SpecOperation.operation_id)
        .outerjoin(ProductionStage, ProductionStage.stage_id == SpecOperation.stage_id)
        .filter(SpecOperation.spec_id.in_(spec_to_items))
        .order_by(SpecOperation.spec_id, SpecOperation.spec_operation_id)
        .all()
    )
    for spec_id, operation_name, stage_name in rows:
        parts = [str(value or "").strip().casefold() for value in (operation_name, stage_name)]
        text_by_spec.setdefault(int(spec_id), []).extend(part for part in parts if part)
    result: dict[str, str] = {}
    for spec_id, item_ids in spec_to_items.items():
        normalized = " ".join(" ".join(text_by_spec.get(spec_id, [])).split())
        for item_id in item_ids:
            code = item_codes.get(item_id)
            if code:
                result[code] = normalized
    return result


# --------------------------------------------------------------------------
# Item code <-> id
# --------------------------------------------------------------------------


def item_code_maps(db: Session) -> tuple[dict[int, str], dict[str, int]]:
    """(id -> code, code -> id) for all items."""
    id_to_code: dict[int, str] = {}
    code_to_id: dict[str, int] = {}
    for item_id, item_code in db.query(Item.item_id, Item.item_code).all():
        id_to_code[int(item_id)] = item_code
        code_to_id[item_code] = int(item_id)
    return id_to_code, code_to_id


def resource_name_map(db: Session) -> dict[int, str]:
    """resource_id -> resource_name."""
    return {
        int(rid): name
        for rid, name in db.query(ProductionResource.resource_id, ProductionResource.resource_name).all()
    }


# --------------------------------------------------------------------------
# Assembly takts (leveling input)
# --------------------------------------------------------------------------


def load_sku_takts(db: Session) -> tuple[dict[str, tuple[str, float]], dict[str, int]]:
    """dbr_assembly_rate -> ({item_code: (resource_name, takt)}, {resource_name: resource_id}).

    takt = qty_per_capacity × resource.capacity (assembly.effective_takt).
    Raises ValueError with a joined message when the assignments are invalid
    (mirrors prodflow schedule_service._load_assembly_takts throwing).

    Note: resource names are assumed unique (true on prod for the 16 участков);
    on a name collision the last resource_id wins for the name→id map.
    """
    rows = (
        db.query(DbrAssemblyRate, ProductionResource, Item)
        .join(ProductionResource, DbrAssemblyRate.resource_id == ProductionResource.resource_id)
        .join(Item, DbrAssemblyRate.item_id == Item.item_id)
        .all()
    )
    assignments: list[assembly.Assignment] = []
    capacities: dict[str, float | None] = {}
    name_to_rid: dict[str, int] = {}
    for rate, resource, item in rows:
        assignments.append(
            assembly.Assignment(resource.resource_name, item.item_code, float(rate.qty_per_capacity or 0))
        )
        capacities[resource.resource_name] = float(resource.capacity) if resource.capacity is not None else None
        name_to_rid[resource.resource_name] = int(resource.resource_id)

    if not assignments:
        return {}, {}

    errors = assembly.validate_assignments(assignments, capacities)
    if errors:
        raise ValueError("«Настройки сборки» (dbr_assembly_rate) заполнены неверно:\n" + "\n".join(errors))

    return assembly.build_sku_takts(assignments, capacities), name_to_rid


# --------------------------------------------------------------------------
# Working calendar
# --------------------------------------------------------------------------


def load_workdays(db: Session, period_from: date, period_to: date) -> tuple[list[date], bool]:
    """Working days of [period_from; period_to] and whether a fallback was used.

    Source is ``work_calendar_day`` (is_workday flag). Dates not covered by the
    calendar fall back to Mon–Fri. The bool is True when the calendar has no
    rows in the period at all OR any date inside it was filled by the Mon–Fri
    fallback — surfaced to the caller so the assumption is visible.
    """
    rows = (
        db.query(WorkCalendarDay)
        .filter(WorkCalendarDay.date >= period_from, WorkCalendarDay.date <= period_to)
        .all()
    )
    by_date = {r.date: bool(r.is_workday) for r in rows}
    workdays: list[date] = []
    fallback_used = not rows
    day = period_from
    while day <= period_to:
        if day in by_date:
            if by_date[day]:
                workdays.append(day)
        else:
            fallback_used = True
            if day.weekday() < 5:
                workdays.append(day)
        day += timedelta(days=1)
    return workdays, fallback_used


def horizon_workdays(db: Session, start: date, count: int) -> set[date]:
    """Nearest ``count`` working days from ``start`` (inclusive).

    work_calendar_day is authoritative; uncovered dates fall back to Mon–Fri.
    A guard caps the look-ahead so a pathological calendar cannot loop forever.
    """
    if count <= 0:
        return set()
    look_end = start + timedelta(days=count * 3 + 60)
    rows = (
        db.query(WorkCalendarDay)
        .filter(WorkCalendarDay.date >= start, WorkCalendarDay.date <= look_end)
        .all()
    )
    by_date = {r.date: bool(r.is_workday) for r in rows}
    result: set[date] = set()
    day = start
    guard = 0
    while len(result) < count and guard < count * 10 + 90:
        if by_date.get(day, day.weekday() < 5):
            result.add(day)
        day += timedelta(days=1)
        guard += 1
    return result


# --------------------------------------------------------------------------
# BOM components provider (kit.build_kit)
# --------------------------------------------------------------------------


def build_components_provider(db: Session):
    """Return get_components(item_code) -> [(component_code, qty_per_unit)].

    Uses the item's default specification (default_specifications, first row by
    id — same rule as production_control_domain.default_spec_id) and its
    spec_components. quantity is taken as per-one-parent-unit.
    """
    id_to_code, code_to_id = item_code_maps(db)

    default_spec: dict[int, int] = {}
    for ds in db.query(DefaultSpecification).order_by(DefaultSpecification.id.asc()).all():
        default_spec.setdefault(int(ds.item_id), int(ds.spec_id))

    comps_by_spec: dict[int, list[tuple[int, float]]] = {}
    for comp in db.query(SpecComponent.spec_id, SpecComponent.item_id, SpecComponent.quantity).all():
        comps_by_spec.setdefault(int(comp.spec_id), []).append((int(comp.item_id), float(comp.quantity)))

    def get_components(item_code: str) -> list[tuple[str, float]]:
        iid = code_to_id.get(item_code)
        if iid is None:
            return []
        spec_id = default_spec.get(iid)
        if spec_id is None:
            return []
        out: list[tuple[str, float]] = []
        for comp_id, qty in comps_by_spec.get(spec_id, []):
            code = id_to_code.get(comp_id)
            if code is not None:
                out.append((code, qty))
        return out

    return get_components


# --------------------------------------------------------------------------
# Stock snapshot (per item + warehouse, selected & not ignored)
# --------------------------------------------------------------------------


def stock_snapshot_by_code(
    db: Session,
    pairs: set[tuple[str, str]],
    code_to_id: dict[str, int],
) -> dict[tuple[str, str], float]:
    """Available stock keyed (item_code, warehouse_ref1c) for the needed pairs.

    Only stock_warehouses.is_selected warehouses count and ignored_warehouses
    are excluded (same policy as mrp_stock_helpers / material availability, but
    kept per-warehouse because kit lines are pinned to a shelf warehouse).
    """
    if not pairs:
        return {}
    ids = sorted({code_to_id[c] for c, _w in pairs if c in code_to_id})
    if not ids:
        return {}

    ignored = {r[0] for r in db.query(IgnoredWarehouse.warehouse_ref1c).all() if r[0]}
    wh_rows = db.query(StockWarehouse.warehouse_ref1c, StockWarehouse.is_selected).all()
    has_settings = bool(wh_rows)
    selected = {ref for ref, sel in wh_rows if ref and bool(sel)}

    id_to_code = {code_to_id[c]: c for c in code_to_id}
    result: dict[tuple[str, str], float] = {}
    rows = (
        db.query(ItemWarehouseStock.item_id, ItemWarehouseStock.warehouse_ref1c, ItemWarehouseStock.qty)
        .filter(ItemWarehouseStock.item_id.in_(ids))
        .all()
    )
    for iid, ref, qty in rows:
        if ref in ignored:
            continue
        if has_settings and ref not in selected:
            continue
        code = id_to_code.get(int(iid))
        if code is None:
            continue
        key = (code, ref)
        result[key] = result.get(key, 0.0) + float(qty or 0)
    return result


# --------------------------------------------------------------------------
# Open replenishment (inbound for the gate)
# --------------------------------------------------------------------------


def open_inbound(
    db: Session,
    pairs: set[tuple[str, str]],
    code_to_id: dict[str, int],
    id_to_code: dict[int, str],
) -> list[tuple[str, str, date, float]]:
    """Open replenishment as inbound events (item_code, warehouse, eta, qty).

    - production: production_products.remaining_qty > 0 on non-deleted orders;
      eta = line-state planned_finish_date, else order_date;
    - purchase: supplier_order_items.remaining_qty > 0; eta = delivery_date,
      else order_date.

    Only lines with an exact persisted destination are counted. Unknown
    destinations are excluded conservatively and one line is never fanned out
    across multiple requested shelves for the same item.
    """
    inbound, _diagnostics = open_inbound_with_diagnostics(
        db, pairs, code_to_id, id_to_code
    )
    return inbound


def open_inbound_with_diagnostics(
    db: Session,
    pairs: set[tuple[str, str]],
    code_to_id: dict[str, int],
    id_to_code: dict[int, str],
) -> tuple[list[tuple[str, str, date, float]], dict[str, int]]:
    """Return exact-destination inbound plus aggregated exclusion diagnostics."""
    diagnostics = {
        "included": 0,
        "excluded_null_destination": 0,
        "excluded_destination_not_needed": 0,
        "excluded_missing_eta": 0,
    }
    if not pairs:
        return [], diagnostics
    wh_by_code: dict[str, dict[str, str]] = {}
    for code, wh in pairs:
        wh_by_code.setdefault(code, {})[str(wh).strip().lower()] = wh
    ids = sorted({code_to_id[c] for c in wh_by_code if c in code_to_id})
    if not ids:
        return [], diagnostics

    inbound: list[tuple[str, str, date, float]] = []

    # --- production ---
    prod_rows = (
        db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState)
        .join(ProductionOrder, ProductionProduct.order_id == ProductionOrder.order_id)
        .outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .filter(
            ProductionProduct.item_id.in_(ids),
            ProductionProduct.remaining_qty > 0,
            ProductionOrder.deletion_mark.is_(False),
        )
        .all()
    )
    for prod, order, state in prod_rows:
        code = id_to_code.get(int(prod.item_id))
        if code is None:
            continue
        rem = float(prod.remaining_qty or 0)
        if rem <= 0:
            continue
        destination = str(prod.destination_warehouse_ref1c or "").strip()
        if not destination:
            diagnostics["excluded_null_destination"] += 1
            continue
        requested_wh = wh_by_code.get(code, {}).get(destination.lower())
        if requested_wh is None:
            diagnostics["excluded_destination_not_needed"] += 1
            continue
        eta = None
        if state is not None and state.planned_finish_date:
            eta = state.planned_finish_date
        elif order.order_date is not None:
            eta = order.order_date.date() if hasattr(order.order_date, "date") else order.order_date
        if eta is None:
            diagnostics["excluded_missing_eta"] += 1
            continue
        inbound.append((code, requested_wh, eta, rem))
        diagnostics["included"] += 1

    # --- purchase ---
    po_rows = (
        db.query(SupplierOrderItem, SupplierOrder)
        .join(SupplierOrder, SupplierOrderItem.order_id == SupplierOrder.order_id)
        .filter(
            SupplierOrderItem.item_id_ref.in_(ids),
            SupplierOrderItem.remaining_qty > 0,
            SupplierOrder.deletion_mark.is_(False),
        )
        .all()
    )
    for line, order in po_rows:
        code = id_to_code.get(int(line.item_id_ref))
        if code is None:
            continue
        rem = float(line.remaining_qty or 0)
        if rem <= 0:
            continue
        destination = str(line.destination_warehouse_ref1c or "").strip()
        if not destination:
            diagnostics["excluded_null_destination"] += 1
            continue
        requested_wh = wh_by_code.get(code, {}).get(destination.lower())
        if requested_wh is None:
            diagnostics["excluded_destination_not_needed"] += 1
            continue
        raw = line.delivery_date or order.order_date
        if raw is None:
            diagnostics["excluded_missing_eta"] += 1
            continue
        eta = raw.date() if hasattr(raw, "date") else raw
        inbound.append((code, requested_wh, eta, rem))
        diagnostics["included"] += 1

    return inbound, diagnostics

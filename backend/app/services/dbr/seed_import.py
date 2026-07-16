"""
DBR module — one-off seed import from ERPNext prodflow TSV dumps.

Consumes the three TSV files in .docs/dbr_seed/ and upserts them into the
module-owned dbr_* tables. Idempotent: re-running yields the same state and a
summary of loaded / updated / skipped rows (with reasons). Never raises on
unresolved references — they are collected into the report so the caller can
inspect them.

TSV layouts (see .docs/dbr_parallel_module_roadmap.md §5):
  * erpnext_planning_settings.tsv  — field/value pairs (singleton settings)
  * erpnext_assembly_rates.tsv     — workstation/item/qty_per_capacity
  * erpnext_child_settings.tsv     — 3 "---"-separated blocks; only the first
                                     (category supply-risk) is imported.

Warehouse cells look like ``НФ-000092 - Склад №2 - ... - ООО "ЗСМ"``; the code
(prefix before the first " - ") is resolved to warehouse_ref1c via
stock_warehouses.warehouse_code.
"""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from ...models import (
    DbrCategorySupplyRisk,
    Item,
    ProductionResource,
    StockWarehouse,
)
from . import settings_service

# ---- planning-settings field typing --------------------------------------

_INT_FIELDS = {
    "frozen_days",
    "gate_horizon_workdays",
    "rt_machining_days",
    "rt_welding_days",
    "rt_painting_days",
    "batch_days_turning",
    "batch_days_bending",
    "batch_days_welding",
    "batch_days_paint_black",
    "batch_days_paint_color",
    "feeder_load_horizon_weeks",
}
_DEC_FIELDS = {"shelf_threshold_qty"}
_BOOL_FIELDS = {"feeder_chain_enabled"}
# ERPNext field name -> DbrSettings warehouse-role column
_WAREHOUSE_FIELDS = {
    "feeder_wip_warehouse": "w2_warehouse_ref1c",
    "w3_warehouse": "w3_warehouse_ref1c",
    "w4_warehouse": "w4_warehouse_ref1c",
}
# ERPNext service/system fields that carry no planning value
_IGNORED_SETTING_FIELDS = {
    "creation",
    "docstatus",
    "idx",
    "modified",
    "modified_by",
    "name",
    "owner",
    "parent",
    "parentfield",
    "parenttype",
}


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------


def parse_warehouse_code(cell: str) -> Optional[str]:
    """Extract the 1C warehouse code (prefix before the first ' - ')."""
    if cell is None:
        return None
    cell = cell.strip()
    if not cell:
        return None
    return cell.split(" - ", 1)[0].strip() or None


def _warehouse_code_map(db: Session) -> dict[str, str]:
    """warehouse_code -> warehouse_ref1c from stock_warehouses."""
    rows = db.query(StockWarehouse.warehouse_code, StockWarehouse.warehouse_ref1c).all()
    return {code: ref for code, ref in rows if code}


def _resolve_warehouse(cell: str, code_map: dict[str, str]) -> tuple[Optional[str], Optional[str]]:
    """Return (warehouse_ref1c, error). error is None on success."""
    code = parse_warehouse_code(cell)
    if not code:
        return None, "empty warehouse cell"
    ref = code_map.get(code)
    if ref is None:
        return None, f"warehouse code {code!r} not found in stock_warehouses"
    return ref, None


def _to_decimal(value: str) -> Optional[Decimal]:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _read_tsv_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return [row for row in csv.reader(fh, delimiter="\t")]


# --------------------------------------------------------------------------
# Planning settings
# --------------------------------------------------------------------------


def import_planning_settings(db: Session, path: str | Path) -> dict[str, Any]:
    path = Path(path)
    code_map = _warehouse_code_map(db)
    rows = _read_tsv_rows(path)
    applied: dict[str, Any] = {}
    skipped: list[dict[str, str]] = []
    warnings: list[str] = []

    for idx, row in enumerate(rows):
        if not row:
            continue
        # header
        if idx == 0 and [c.strip().lower() for c in row[:2]] == ["field", "value"]:
            continue
        if len(row) < 2:
            continue
        field = row[0].strip()
        value = row[1]
        if not field or field in _IGNORED_SETTING_FIELDS:
            continue

        if field in _WAREHOUSE_FIELDS:
            ref, err = _resolve_warehouse(value, code_map)
            if err:
                warnings.append(f"{field}: {err}")
                continue
            applied[_WAREHOUSE_FIELDS[field]] = ref
        elif field in _INT_FIELDS:
            dec = _to_decimal(value)
            if dec is None:
                skipped.append({"field": field, "reason": f"not an integer: {value!r}"})
                continue
            applied[field] = int(dec)
        elif field in _DEC_FIELDS:
            dec = _to_decimal(value)
            if dec is None:
                skipped.append({"field": field, "reason": f"not a number: {value!r}"})
                continue
            applied[field] = dec
        elif field in _BOOL_FIELDS:
            applied[field] = str(value).strip() in {"1", "true", "True", "yes"}
        else:
            skipped.append({"field": field, "reason": "unknown field"})

    if applied:
        settings_service.update_settings(db, applied)
    else:
        # Ensure the singleton exists even if nothing was applied.
        settings_service.get_or_create_settings(db)

    return {
        "applied_fields": sorted(applied.keys()),
        "applied_count": len(applied),
        "skipped": skipped,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Assembly rates
# --------------------------------------------------------------------------


def import_assembly_rates(db: Session, path: str | Path) -> dict[str, Any]:
    path = Path(path)
    rows = _read_tsv_rows(path)

    resource_map = {
        r.resource_name: r.resource_id
        for r in db.query(ProductionResource.resource_id, ProductionResource.resource_name).all()
    }
    item_map = {
        i.item_code: i.item_id
        for i in db.query(Item.item_id, Item.item_code).all()
    }

    loaded = 0
    updated = 0
    unresolved: list[dict[str, str]] = []
    seen_pairs: set[tuple[int, int]] = set()

    for idx, row in enumerate(rows):
        if not row:
            continue
        if idx == 0 and [c.strip().lower() for c in row[:3]] == [
            "workstation",
            "item",
            "qty_per_capacity",
        ]:
            continue
        if len(row) < 3:
            continue
        workstation = row[0].strip()
        item_code = row[1].strip()
        qty = _to_decimal(row[2])

        resource_id = resource_map.get(workstation)
        item_id = item_map.get(item_code)
        reasons = []
        if resource_id is None:
            reasons.append(f"workstation {workstation!r} not found in production_resources")
        if item_id is None:
            reasons.append(f"item {item_code!r} not found in items")
        if qty is None:
            reasons.append(f"bad qty_per_capacity {row[2]!r}")
        if reasons:
            unresolved.append(
                {"workstation": workstation, "item": item_code, "reason": "; ".join(reasons)}
            )
            continue

        existed = (resource_id, item_id) in seen_pairs or _rate_exists(db, resource_id, item_id)
        settings_service.upsert_assembly_rate(db, resource_id, item_id, qty)
        seen_pairs.add((resource_id, item_id))
        if existed:
            updated += 1
        else:
            loaded += 1

    return {
        "loaded": loaded,
        "updated": updated,
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
    }


def _rate_exists(db: Session, resource_id: int, item_id: int) -> bool:
    from ...models import DbrAssemblyRate

    return (
        db.query(DbrAssemblyRate.id)
        .filter(
            DbrAssemblyRate.resource_id == resource_id,
            DbrAssemblyRate.item_id == item_id,
        )
        .first()
        is not None
    )


# --------------------------------------------------------------------------
# Category supply-risk (first block of child settings)
# --------------------------------------------------------------------------


def _split_blocks(rows: list[list[str]]) -> list[list[list[str]]]:
    """Split rows into blocks separated by a lone '---' line."""
    blocks: list[list[list[str]]] = []
    current: list[list[str]] = []
    for row in rows:
        if len(row) == 1 and row[0].strip() == "---":
            blocks.append(current)
            current = []
        else:
            current.append(row)
    blocks.append(current)
    return blocks


def import_category_risks(db: Session, path: str | Path) -> dict[str, Any]:
    path = Path(path)
    rows = _read_tsv_rows(path)
    blocks = _split_blocks(rows)
    # First block = category supply risk; remaining blocks (метизы / ignored
    # warehouses) are intentionally skipped.
    block = blocks[0] if blocks else []

    code_map = _warehouse_code_map(db)
    payload: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    warnings: list[str] = []

    for idx, row in enumerate(block):
        if not row:
            continue
        if idx == 0 and [c.strip().lower() for c in row[:3]] == [
            "item_group",
            "receipt_warehouse",
            "supply_risk_pct",
        ]:
            continue
        if len(row) < 3:
            continue
        item_group = row[0].strip()
        if not item_group:
            continue
        ref, err = _resolve_warehouse(row[1], code_map)
        if err:
            warnings.append(f"{item_group}: {err}")
            # keep the row, just without a resolved warehouse
        risk = _to_decimal(row[2])
        payload.append(
            {
                "item_group": item_group,
                "receipt_warehouse_ref1c": ref,
                "supply_risk_pct": risk,
            }
        )

    existing_before = {r.item_group for r in db.query(DbrCategorySupplyRisk.item_group).all()}
    settings_service.replace_category_risks(db, payload)
    loaded = sum(1 for p in payload if p["item_group"] not in existing_before)
    updated = len(payload) - loaded

    return {
        "loaded": loaded,
        "updated": updated,
        "skipped": skipped,
        "warnings": warnings,
        "count": len(payload),
    }


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def import_all(db: Session, seed_dir: str | Path) -> dict[str, Any]:
    seed_dir = Path(seed_dir)
    return {
        "planning_settings": import_planning_settings(
            db, seed_dir / "erpnext_planning_settings.tsv"
        ),
        "assembly_rates": import_assembly_rates(
            db, seed_dir / "erpnext_assembly_rates.tsv"
        ),
        "category_risks": import_category_risks(
            db, seed_dir / "erpnext_child_settings.tsv"
        ),
    }

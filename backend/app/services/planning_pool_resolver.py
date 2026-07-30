"""Canonical warehouse -> planning-stock-pool configuration.

Today's MRP has one pool (``default``). Its warehouse membership is the live
planning contour configured by ``StockWarehouse.is_selected``, excluding
finished-goods and explicitly ignored warehouses.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.orm import Session

from app import models


DEFAULT_STOCK_POOL = "default"


class PlanningPoolConfigurationError(RuntimeError):
    """Live warehouse configuration cannot qualify future supply safely."""


def resolve_planning_pool_by_warehouse(db: Session) -> dict[str, str]:
    """Return the live planning contour as ``warehouse_ref -> default``."""
    ignored = {
        str(ref).strip()
        for (ref,) in db.query(models.IgnoredWarehouse.warehouse_ref1c).all()
        if str(ref or "").strip()
    }
    rows = db.query(
        models.StockWarehouse.warehouse_ref1c,
        models.StockWarehouse.is_selected,
        models.StockWarehouse.is_finished_goods,
    ).all()
    mapping = {
        str(ref).strip(): DEFAULT_STOCK_POOL
        for ref, selected, finished_goods in rows
        if (
            str(ref or "").strip()
            and bool(selected)
            and not bool(finished_goods)
            and str(ref).strip() not in ignored
        )
    }
    if not mapping:
        raise PlanningPoolConfigurationError(
            "planning warehouse contour is empty: select at least one "
            "non-finished, non-ignored StockWarehouse"
        )
    return dict(sorted(mapping.items()))


def effective_planning_pool_by_warehouse(
    db: Session,
    override: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve production configuration, retaining an explicit test seam."""
    if override is None:
        return resolve_planning_pool_by_warehouse(db)
    mapping = {
        str(warehouse or "").strip(): str(pool or "").strip()
        for warehouse, pool in override.items()
        if str(warehouse or "").strip() and str(pool or "").strip()
    }
    if not mapping:
        raise PlanningPoolConfigurationError(
            "planning_pool_by_warehouse is empty"
        )
    return dict(sorted(mapping.items()))


def require_mapped_destination(
    mapping: Mapping[str, str],
    destination_warehouse_ref1c: object,
    *,
    source: str,
) -> str:
    """Resolve one non-empty destination or fail the generation visibly."""
    destination = str(destination_warehouse_ref1c or "").strip()
    if not destination:
        return ""
    pool = str(mapping.get(destination, "") or "").strip()
    if not pool:
        raise PlanningPoolConfigurationError(
            f"{source} destination warehouse {destination!r} is outside the "
            "live planning contour"
        )
    return pool


def validate_future_supply_destinations(
    db: Session,
    *,
    ledger_generation_id: int,
    mapping: Mapping[str, str],
) -> None:
    """Validate exact rows before a physical refresh carries them forward."""
    rows = (
        db.query(
            models.LedgerFutureSupply.supply_kind,
            models.LedgerFutureSupply.source_ref,
            models.LedgerFutureSupply.destination_warehouse_ref1c,
        )
        .filter(
            models.LedgerFutureSupply.ledger_generation_id
            == int(ledger_generation_id),
            models.LedgerFutureSupply.evidence_status == "exact",
        )
        .all()
    )
    for supply_kind, source_ref, destination in rows:
        require_mapped_destination(
            mapping,
            destination,
            source=f"{str(supply_kind)}:{str(source_ref or '')}",
        )

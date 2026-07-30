"""Canonical interpretation of accepted production output.

This module intentionally lives outside the ``item_ledger`` package so MRP
helpers can use the formula without importing that package's orchestration
graph and creating a cycle through ``mrp_freeze``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func


ZERO = Decimal("0")


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


@dataclass(frozen=True)
class AcceptedProductOutput:
    planned_qty: Decimal
    produced_qty: Decimal
    remaining_qty: Decimal


def accepted_product_output(
    product: Any,
    *,
    produced_qty: Any | None = None,
) -> AcceptedProductOutput:
    """Return the only permitted physical-output interpretation of a line."""
    planned = max(_decimal(getattr(product, "quantity", ZERO)), ZERO)
    produced = max(
        _decimal(
            getattr(product, "produced_qty", ZERO)
            if produced_qty is None
            else produced_qty
        ),
        ZERO,
    )
    return AcceptedProductOutput(
        planned_qty=planned,
        produced_qty=produced,
        remaining_qty=max(planned - produced, ZERO),
    )


def accepted_product_remaining_expr(planned_qty: Any, produced_qty: Any) -> Any:
    """SQL twin of :func:`accepted_product_output` for batched read models."""
    planned = func.coalesce(planned_qty, 0.0)
    produced = func.coalesce(produced_qty, 0.0)
    difference = planned - produced
    return case((difference > 0.0, difference), else_=0.0)


def update_accepted_product_output_cache(
    product: Any,
    *,
    produced_qty: Any,
) -> bool:
    """Refresh both compatibility columns from one accepted Ledger value."""
    expected = accepted_product_output(product, produced_qty=produced_qty)
    current_produced = _decimal(getattr(product, "produced_qty", ZERO))
    current_remaining = _decimal(getattr(product, "remaining_qty", ZERO))
    changed = (
        current_produced != expected.produced_qty
        or current_remaining != expected.remaining_qty
    )
    if changed:
        product.produced_qty = expected.produced_qty
        product.remaining_qty = expected.remaining_qty
    return changed

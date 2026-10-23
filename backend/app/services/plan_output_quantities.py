"""Canonical saved quantities for one production-plan output line.

The ordinary case is ``planned = accepted + remaining``.  An explicitly
audited correction may reduce the programme below output that the accepted
Ledger already assigned to the stable line.  Facts are not undone in that
case: remaining stays zero and the excess is exposed as overproduction.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


ZERO = Decimal("0")


@dataclass(frozen=True)
class PlanOutputQuantities:
    planned: Decimal
    accepted: Decimal
    remaining: Decimal
    overproduction: Decimal


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f"{field} is not a decimal quantity") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def expected_plan_output_quantities(
    planned: Any,
    accepted: Any,
) -> PlanOutputQuantities:
    planned_qty = _decimal(planned, field="planned output")
    accepted_qty = _decimal(accepted, field="accepted output")
    if planned_qty < ZERO or accepted_qty < ZERO:
        raise ValueError("plan output quantities must be non-negative")
    return PlanOutputQuantities(
        planned=planned_qty,
        accepted=accepted_qty,
        remaining=max(planned_qty - accepted_qty, ZERO),
        overproduction=max(accepted_qty - planned_qty, ZERO),
    )


def validate_saved_plan_output(
    planned: Any,
    accepted: Any,
    remaining: Any,
    *,
    context: str,
) -> PlanOutputQuantities:
    expected = expected_plan_output_quantities(planned, accepted)
    saved_remaining = _decimal(remaining, field="remaining output")
    if saved_remaining < ZERO or saved_remaining != expected.remaining:
        raise ValueError(
            f"{context} violates output conservation under the corrected plan rule"
        )
    return expected

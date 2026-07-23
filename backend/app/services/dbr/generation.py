"""Strict Item Ledger generation boundary for DBR projections."""

from sqlalchemy.orm import Session

from ..planning_truth import require_accepted_truth


class DbrProjectionUnavailable(RuntimeError):
    """The Ledger-native DBR projection builder is not implemented yet."""

    code = "dbr_ledger_projection_unavailable"


def require_generation(
    db: Session,
    ledger_generation_id: int | None,
    *,
    consumer: str,
) -> int:
    """Require the caller to name the currently accepted generation."""
    if ledger_generation_id is None:
        raise ValueError(f"{consumer} requires explicit ledger_generation_id")
    truth = require_accepted_truth(db, consumer)
    accepted_id = int(truth.generation_id)
    requested_id = int(ledger_generation_id)
    if requested_id != accepted_id:
        raise ValueError(
            f"{consumer} generation mismatch: "
            f"accepted={accepted_id}, requested={requested_id}"
        )
    return requested_id

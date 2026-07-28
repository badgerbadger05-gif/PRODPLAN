"""Persistence-only capture of generation-scoped future supply facts.

The caller is responsible for reading and qualifying source documents.  This
module accepts only a frozen, canonical evidence projection and writes it into
``ledger_future_supply`` while the generation is still being built.  It never
falls back to old planning tables and deliberately owns neither commit nor the
outer transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from sqlalchemy.orm import Session

from app import models
from .physical import canonical_content_hash, canonical_decimal


_KINDS = frozenset({"wip_order", "supplier_order"})
_STATUSES = frozenset({"exact", "ambiguous", "unmatched", "rejected"})
CARRY_FORWARD_ALGORITHM_VERSION = "ledger-future-supply-carry-forward/1"


class FutureSupplyCaptureError(ValueError):
    """The candidate projection cannot be stored as Ledger future supply."""


@dataclass(frozen=True)
class FutureSupplyEvidence:
    """All source-business fields of one future-supply evidence row.

    ``open_qty_at_cutoff`` is intentionally absent: it is a derived Ledger
    value.  ``source_content_hash`` is supplied by the capture boundary but is
    verified against this complete frozen business payload before persistence.
    """

    supply_kind: str
    item_id: int | None
    characteristic_ref: str = ""
    organization_ref: str = ""
    planning_stock_pool: str = ""
    destination_warehouse_ref1c: str = ""
    source_ref: str | None = None
    source_line_ref: str | None = None
    source_local_id: str | None = None
    ordered_qty_at_cutoff: Decimal | int | float | str = Decimal("0")
    realized_qty_at_cutoff: Decimal | int | float | str = Decimal("0")
    eta_date: date | None = None
    source_state_key: str = ""
    source_updated_at: datetime | None = None
    capture_cutoff: datetime | None = None
    source_content_hash: str = ""
    evidence_status: str = "exact"
    reason: str | None = None


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - Decimal's messages vary
        raise FutureSupplyCaptureError(f"{field} must be a decimal") from exc
    if not result.is_finite() or result < 0:
        raise FutureSupplyCaptureError(f"{field} must be a finite nonnegative decimal")
    return result


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _item_id(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise FutureSupplyCaptureError("future supply evidence requires item_id") from exc
    if result <= 0:
        raise FutureSupplyCaptureError("future supply evidence requires item_id")
    return result


def _canonical_payload(evidence: FutureSupplyEvidence) -> dict[str, Any]:
    """Return the complete hash payload with stable decimal/ref forms."""
    return {
        "supply_kind": _norm(evidence.supply_kind),
        "item_id": evidence.item_id,
        "characteristic_ref": _norm(evidence.characteristic_ref),
        "organization_ref": _norm(evidence.organization_ref),
        "planning_stock_pool": _norm(evidence.planning_stock_pool),
        "destination_warehouse_ref1c": _norm(evidence.destination_warehouse_ref1c),
        "source_ref": _norm(evidence.source_ref) or None,
        "source_line_ref": _norm(evidence.source_line_ref) or None,
        "source_local_id": _norm(evidence.source_local_id) or None,
        "ordered_qty_at_cutoff": canonical_decimal(evidence.ordered_qty_at_cutoff),
        "realized_qty_at_cutoff": canonical_decimal(evidence.realized_qty_at_cutoff),
        "eta_date": evidence.eta_date.isoformat() if evidence.eta_date else None,
        "source_state_key": _norm(evidence.source_state_key),
        "source_updated_at": evidence.source_updated_at.isoformat() if evidence.source_updated_at else None,
        "capture_cutoff": evidence.capture_cutoff.isoformat() if evidence.capture_cutoff else None,
        "evidence_status": _norm(evidence.evidence_status),
        "reason": evidence.reason,
    }


def future_supply_evidence_hash(evidence: FutureSupplyEvidence) -> str:
    """Canonical SHA-256 which a source adapter must attach to its evidence."""
    return canonical_content_hash(_canonical_payload(evidence))


def _row_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """One stable ordering for hashing, comparison, and persistence."""
    return (
        str(row["supply_kind"]),
        str(row["source_ref"] or ""),
        str(row["source_line_ref"] or ""),
        int(row["item_id"]),
        str(row["source_content_hash"]),
    )


def _row_hash_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """JSON-safe canonical persisted projection (not its DB identity/timestamps)."""
    return {
        key: (
            canonical_decimal(value) if isinstance(value, Decimal)
            else value.isoformat() if isinstance(value, (date, datetime))
            else value
        )
        for key, value in row.items()
    }


def _stored_row_payload(row: models.LedgerFutureSupply) -> dict[str, Any]:
    return _row_hash_payload({
        "supply_kind": row.supply_kind,
        "item_id": row.item_id,
        "characteristic_ref": row.characteristic_ref,
        "organization_ref": row.organization_ref,
        "planning_stock_pool": row.planning_stock_pool,
        "destination_warehouse_ref1c": row.destination_warehouse_ref1c,
        "source_ref": row.source_ref,
        "source_line_ref": row.source_line_ref,
        "source_local_id": row.source_local_id,
        "ordered_qty_at_cutoff": row.ordered_qty_at_cutoff,
        "realized_qty_at_cutoff": row.realized_qty_at_cutoff,
        "open_qty_at_cutoff": row.open_qty_at_cutoff,
        "eta_date": row.eta_date,
        "source_state_key": row.source_state_key,
        "source_updated_at": row.source_updated_at,
        "capture_cutoff": row.capture_cutoff,
        "source_content_hash": row.source_content_hash,
        "evidence_status": row.evidence_status,
        "reason": row.reason,
    })


def _validated_rows(
    generation: models.LedgerGeneration,
    evidence: Iterable[FutureSupplyEvidence],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if generation.cutoff is None:
        raise FutureSupplyCaptureError("generation cutoff is required")

    rows: list[dict[str, Any]] = []
    exact_identities: set[tuple[str, str, str]] = set()
    hash_identities: dict[str, tuple[str, str, str]] = {}
    total_open = Decimal("0")
    total_surplus = Decimal("0")
    rejected = 0

    for item in evidence:
        if not isinstance(item, FutureSupplyEvidence):
            raise FutureSupplyCaptureError("evidence must contain FutureSupplyEvidence rows")
        kind = _norm(item.supply_kind)
        status = _norm(item.evidence_status)
        if kind not in _KINDS:
            raise FutureSupplyCaptureError(f"unsupported supply_kind: {kind!r}")
        if status not in _STATUSES:
            raise FutureSupplyCaptureError(f"unsupported evidence_status: {status!r}")
        if item.capture_cutoff != generation.cutoff:
            raise FutureSupplyCaptureError("evidence capture_cutoff must exactly equal generation cutoff")
        ordered = _decimal(item.ordered_qty_at_cutoff, "ordered_qty_at_cutoff")
        realized = _decimal(item.realized_qty_at_cutoff, "realized_qty_at_cutoff")
        item_id = _item_id(item.item_id)
        expected_hash = future_supply_evidence_hash(item)
        if _norm(item.source_content_hash) != expected_hash:
            raise FutureSupplyCaptureError("source_content_hash does not match canonical evidence")
        source_ref = _norm(item.source_ref) or None
        source_line_ref = _norm(item.source_line_ref) or None
        identity = (kind, source_ref or "", source_line_ref or "")
        if status == "exact":
            if not source_ref or not source_line_ref:
                raise FutureSupplyCaptureError("exact evidence requires source_ref and source_line_ref")
            if not _norm(item.destination_warehouse_ref1c):
                raise FutureSupplyCaptureError("exact evidence requires destination_warehouse_ref1c")
            if not _norm(item.planning_stock_pool):
                raise FutureSupplyCaptureError("exact evidence requires planning_stock_pool")
            if not _norm(item.source_state_key):
                raise FutureSupplyCaptureError("exact evidence requires source_state_key")
            if identity in exact_identities:
                raise FutureSupplyCaptureError("duplicate exact source identity")
            exact_identities.add(identity)

        prior_identity = hash_identities.get(expected_hash)
        if prior_identity is not None and prior_identity != identity:
            raise FutureSupplyCaptureError("source_content_hash conflicts with another source identity")
        hash_identities[expected_hash] = identity

        open_qty = max(ordered - realized, Decimal("0")) if status == "exact" else Decimal("0")
        surplus_qty = max(realized - ordered, Decimal("0"))
        total_open += open_qty
        total_surplus += surplus_qty
        if status != "exact":
            rejected += 1
        rows.append({
            "supply_kind": kind,
            "item_id": item_id,
            "characteristic_ref": _norm(item.characteristic_ref),
            "organization_ref": _norm(item.organization_ref),
            "planning_stock_pool": _norm(item.planning_stock_pool),
            "destination_warehouse_ref1c": _norm(item.destination_warehouse_ref1c),
            "source_ref": source_ref,
            "source_line_ref": source_line_ref,
            "source_local_id": _norm(item.source_local_id) or None,
            "ordered_qty_at_cutoff": ordered,
            "realized_qty_at_cutoff": realized,
            "open_qty_at_cutoff": open_qty,
            "eta_date": item.eta_date,
            "source_state_key": _norm(item.source_state_key),
            "source_updated_at": item.source_updated_at,
            "capture_cutoff": item.capture_cutoff,
            "source_content_hash": expected_hash,
            "evidence_status": status,
            "reason": item.reason,
        })
    rows.sort(key=_row_sort_key)
    return rows, {
        "rows": len(rows),
        "exact_rows": len(rows) - rejected,
        "non_supply_rows": rejected,
        "open_qty": total_open,
        "surplus_qty": total_surplus,
        "content_hash": canonical_content_hash([_row_hash_payload(row) for row in rows]),
    }


_CARRY_FORWARD_FIELDS = (
    "supply_kind",
    "item_id",
    "characteristic_ref",
    "organization_ref",
    "planning_stock_pool",
    "destination_warehouse_ref1c",
    "source_ref",
    "source_line_ref",
    "source_local_id",
    "ordered_qty_at_cutoff",
    "realized_qty_at_cutoff",
    "open_qty_at_cutoff",
    "eta_date",
    "source_state_key",
    "source_updated_at",
    "capture_cutoff",
    "source_content_hash",
    "evidence_status",
    "reason",
)


def _generation_rows(db: Session, generation_id: int) -> list[models.LedgerFutureSupply]:
    return db.query(models.LedgerFutureSupply).filter(
        models.LedgerFutureSupply.ledger_generation_id == int(generation_id)
    ).all()


def _capture_summary(rows: list[models.LedgerFutureSupply]) -> dict[str, Any]:
    payloads = sorted(
        (_stored_row_payload(row) for row in rows), key=_row_sort_key
    )
    open_qty = sum(
        (Decimal(str(row.open_qty_at_cutoff or 0)) for row in rows), Decimal("0")
    )
    return {
        "rows": len(rows),
        "exact_rows": sum(1 for row in rows if str(row.evidence_status) == "exact"),
        "open_qty": canonical_decimal(open_qty),
        "content_hash": canonical_content_hash(payloads),
    }


def carry_forward_future_supply(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
) -> Mapping[str, Any]:
    """Copy the accepted parent's future-supply capture into a refresh target.

    A physical refresh advances *facts*, not obligations: it re-reads no
    supplier or WIP order, so recapturing here would fabricate evidence, while
    capturing nothing leaves the purchase journal reporting zero ordered and
    zero in transit after every three-hour cycle.  The rows are therefore
    carried verbatim, keeping the ``capture_cutoff`` at which the evidence was
    really observed, under one dedicated carry-forward batch so their origin
    stays auditable.  Idempotent: an exact repeat returns the existing summary.
    """
    target = db.get(models.LedgerGeneration, int(target_generation_id))
    if target is None or str(target.status) != "building":
        raise FutureSupplyCaptureError(
            "future supply carry-forward requires a BUILDING target generation"
        )
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    if parent is None or str(parent.status) != "accepted":
        raise FutureSupplyCaptureError(
            "future supply carry-forward requires an accepted source generation"
        )
    if int(parent.id) == int(target.id):
        raise FutureSupplyCaptureError("future supply carry-forward requires two generations")

    parent_rows = _generation_rows(db, int(parent.id))
    expected = _capture_summary(parent_rows)
    existing_rows = _generation_rows(db, int(target.id))
    if existing_rows:
        if _capture_summary(existing_rows) != expected:
            raise FutureSupplyCaptureError(
                "target already carries a conflicting future-supply capture"
            )
        return {**expected, "created": False, "source_generation_id": int(parent.id)}

    batch_key = f"future-supply-carry-forward:g{int(target.id)}"
    batch = db.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(target.id),
        models.LedgerBuildBatch.stage == "snapshot_build",
        models.LedgerBuildBatch.batch_key == batch_key,
    ).one_or_none()
    if batch is None:
        batch = models.LedgerBuildBatch(
            ledger_generation_id=int(target.id),
            stage="snapshot_build",
            batch_key=batch_key,
            status="building",
            algorithm_version=CARRY_FORWARD_ALGORITHM_VERSION,
            metrics={},
        )
        db.add(batch)
        db.flush()
    for source in parent_rows:
        db.add(models.LedgerFutureSupply(
            ledger_generation_id=int(target.id),
            capture_batch_id=int(batch.id),
            **{field: getattr(source, field) for field in _CARRY_FORWARD_FIELDS},
        ))
    db.flush()
    if _capture_summary(_generation_rows(db, int(target.id))) != expected:
        raise FutureSupplyCaptureError("carried-forward future supply conflicts")
    batch.status = "completed"
    batch.metrics = {
        **expected,
        "source_generation_id": int(parent.id),
        "carried_forward": True,
    }
    batch.completed_at = datetime.now(timezone.utc)
    db.flush()
    return {**expected, "created": True, "source_generation_id": int(parent.id)}


def replace_future_supply_capture(
    db: Session,
    generation_id: int,
    capture_batch_id: int,
    evidence: Iterable[FutureSupplyEvidence],
) -> Mapping[str, Any]:
    """Atomically replace one BUILDING snapshot batch's future-supply projection.

    A savepoint gives the caller all-or-nothing behavior without committing or
    rolling back its transaction.  The batch remains ``building``: lifecycle
    ownership stays with the generation orchestrator.
    """
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or str(generation.status) != "building":
        raise FutureSupplyCaptureError("future supply capture requires a BUILDING generation")
    batch = db.get(models.LedgerBuildBatch, int(capture_batch_id))
    if (
        batch is None
        or int(batch.ledger_generation_id) != int(generation.id)
        or str(batch.stage) != "snapshot_build"
        or str(batch.status) != "building"
    ):
        raise FutureSupplyCaptureError("capture batch must be this generation's BUILDING snapshot_build batch")

    rows, metrics = _validated_rows(generation, evidence)
    with db.begin_nested():
        # A capture may be rerun, but must not overwrite another batch's facts.
        existing = db.query(models.LedgerFutureSupply).filter(
            models.LedgerFutureSupply.ledger_generation_id == int(generation.id),
            models.LedgerFutureSupply.capture_batch_id != int(batch.id),
        ).all()
        exact_identities = {
            (str(row.supply_kind), str(row.source_ref or ""), str(row.source_line_ref or ""))
            for row in existing if str(row.evidence_status) == "exact"
        }
        hash_identities = {
            str(row.source_content_hash): (str(row.supply_kind), str(row.source_ref or ""), str(row.source_line_ref or ""))
            for row in existing
        }
        for row in rows:
            identity = (row["supply_kind"], str(row["source_ref"] or ""), str(row["source_line_ref"] or ""))
            if row["evidence_status"] == "exact" and identity in exact_identities:
                raise FutureSupplyCaptureError("duplicate exact source identity already captured")
            old_identity = hash_identities.get(row["source_content_hash"])
            if old_identity is not None and old_identity != identity:
                raise FutureSupplyCaptureError("source_content_hash conflicts with an existing capture")

        replaced_rows = db.query(models.LedgerFutureSupply).filter(
            models.LedgerFutureSupply.ledger_generation_id == int(generation.id),
            models.LedgerFutureSupply.capture_batch_id == int(batch.id),
        ).all()
        if sorted((_stored_row_payload(row) for row in replaced_rows), key=_row_sort_key) == [
            _row_hash_payload(row) for row in rows
        ]:
            return metrics
        for existing_row in replaced_rows:
            db.delete(existing_row)
        # Flush the delete before re-inserting: this keeps the SQLAlchemy
        # identity map coherent even when SQLite reuses a primary key.
        db.flush()
        for existing_row in replaced_rows:
            db.expunge(existing_row)
        for row in rows:
            db.add(models.LedgerFutureSupply(
                ledger_generation_id=int(generation.id),
                capture_batch_id=int(batch.id),
                **row,
            ))
        db.flush()
    return metrics

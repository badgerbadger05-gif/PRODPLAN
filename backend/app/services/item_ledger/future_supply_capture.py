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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from .physical import canonical_content_hash, canonical_decimal
from .physical_visibility import visible_sle_query


_KINDS = frozenset({"wip_order", "supplier_order"})
_STATUSES = frozenset({"exact", "ambiguous", "unmatched", "rejected"})
CARRY_FORWARD_ALGORITHM_VERSION = "ledger-future-supply-carry-forward/1"
FUTURE_SUPPLY_CAPTURE_STAGE = "future_supply_capture"
FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION = "ledger-future-supply-capture/1"
_SUPPLIER_REALIZATION_KINDS = frozenset({
    "supplier_receipt",
    "correction",
    "supplier_return",
})
_WIP_REALIZATION_KINDS = frozenset({"assembly_in", "transfer_in"})


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
    source_requirement_id: int | None = None
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


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str):
        raise FutureSupplyCaptureError(
            "future supply capture metrics cutoff must be an iso timestamp"
        )
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError as exc:
        raise FutureSupplyCaptureError(
            "future supply capture metrics cutoff must be an iso timestamp"
        ) from exc


def _item_id(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise FutureSupplyCaptureError("future supply evidence requires item_id") from exc
    if result <= 0:
        raise FutureSupplyCaptureError("future supply evidence requires item_id")
    return result


def _requirement_id(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        result = int(text)
    except (TypeError, ValueError) as exc:
        raise FutureSupplyCaptureError("source_requirement_id must be an integer") from exc
    if result <= 0:
        raise FutureSupplyCaptureError("source_requirement_id must be a positive integer")
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
        "source_requirement_id": (
            _requirement_id(evidence.source_requirement_id)
        ),
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
    quantity_fields = {
        "ordered_qty_at_cutoff",
        "realized_qty_at_cutoff",
        "open_qty_at_cutoff",
    }

    def _normalize_value(key: str, value: Any) -> Any:
        if key in quantity_fields and value is not None:
            # These columns are DECIMAL(15,3).  The hash represents their
            # declared storage form both before and after PostgreSQL INSERT.
            value = Decimal(str(value)).quantize(Decimal("0.001"))
        if isinstance(value, datetime):
            normalized = value.astimezone(timezone.utc) if value.tzinfo is not None else value
            return normalized.replace(tzinfo=None).isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return canonical_decimal(value)
        return value

    return {
        key: (
            _normalize_value(key, value)
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
        "source_requirement_id": row.source_requirement_id,
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
            "source_requirement_id": _requirement_id(item.source_requirement_id),
            "ordered_qty_at_cutoff": ordered,
            "realized_qty_at_cutoff": realized,
            "open_qty_at_cutoff": open_qty,
            "eta_date": item.eta_date.date() if isinstance(item.eta_date, datetime) else item.eta_date,
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
    "source_requirement_id",
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


def _supplier_receipt_realized_delta(
    db: Session,
    *,
    target_generation_cutoff: datetime,
    parent_cutoff: datetime,
    target_physical_import_batch_id: int,
    supplier_order_ref: str,
    supplier_order_line_no: str,
) -> Decimal:
    if target_generation_cutoff <= parent_cutoff:
        return Decimal("0")
    visible_sle_ids = visible_sle_query(
        db,
        physical_import_batch_id=int(target_physical_import_batch_id),
        cutoff=target_generation_cutoff,
    ).with_entities(models.StockLedgerEntry.id).subquery()
    return sum(
        (
            Decimal(str(qty))
        for (qty,) in db.query(models.StockLedgerEntry.qty).join(
            models.StockLedgerSupplierReceiptProvenance,
            models.StockLedgerEntry.id
            == models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id,
        ).filter(
            models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id.in_(
                select(visible_sle_ids.c.id),
            ),
                models.StockLedgerSupplierReceiptProvenance.operation_kind.in_(
            _SUPPLIER_REALIZATION_KINDS
            ),
            models.StockLedgerSupplierReceiptProvenance.supplier_order_ref
            == supplier_order_ref,
            models.StockLedgerSupplierReceiptProvenance.supplier_order_line_no
            == supplier_order_line_no,
            models.StockLedgerEntry.posting_at > parent_cutoff,
        )
    ),
    Decimal("0"),
)


def _wip_realized_delta(
    db: Session,
    *,
    target_generation_cutoff: datetime,
    parent_cutoff: datetime,
    target_physical_import_batch_id: int,
    source_ref: str,
    source_line_ref: str,
) -> Decimal:
    if target_generation_cutoff <= parent_cutoff:
        return Decimal("0")
    return sum(
        (
            Decimal(str(qty))
            for (qty,) in visible_sle_query(
                db,
                physical_import_batch_id=int(target_physical_import_batch_id),
                cutoff=target_generation_cutoff,
            ).filter(
                models.StockLedgerEntry.recorder_ref == source_ref,
                models.StockLedgerEntry.line_no == source_line_ref,
                models.StockLedgerEntry.movement_kind.in_(_WIP_REALIZATION_KINDS),
                models.StockLedgerEntry.qty > Decimal("0"),
                models.StockLedgerEntry.posting_at > parent_cutoff,
            ).with_entities(models.StockLedgerEntry.qty)
        ),
        Decimal("0"),
    )


def _carry_forward_rows(
    db: Session,
    parent: models.LedgerGeneration,
    target: models.LedgerGeneration,
) -> list[dict[str, Any]]:
    parent_cutoff = _as_utc(parent.cutoff)
    target_cutoff = _as_utc(target.cutoff)
    if parent_cutoff is None or target_cutoff is None:
        raise FutureSupplyCaptureError("future supply carry-forward requires parent and target cutoffs")

    carried: list[dict[str, Any]] = []
    for source in _generation_rows(db, int(parent.id)):
        row = {
            field: getattr(source, field)
            for field in _CARRY_FORWARD_FIELDS
        }
        ordered = Decimal(str(row["ordered_qty_at_cutoff"] or 0))
        realized = Decimal(str(row["realized_qty_at_cutoff"] or 0))
        if str(row["evidence_status"]) == "exact":
            if str(row["supply_kind"]) == "supplier_order":
                realized += _supplier_receipt_realized_delta(
                    db,
                    target_generation_cutoff=target_cutoff,
                    parent_cutoff=parent_cutoff,
                    target_physical_import_batch_id=int(target.physical_import_batch_id),
                    supplier_order_ref=_norm(row["source_ref"] or ""),
                    supplier_order_line_no=_norm(row["source_line_ref"] or ""),
                )
            else:
                realized += _wip_realized_delta(
                    db,
                    target_generation_cutoff=target_cutoff,
                    parent_cutoff=parent_cutoff,
                    target_physical_import_batch_id=int(target.physical_import_batch_id),
                    source_ref=_norm(row["source_ref"] or ""),
                    source_line_ref=_norm(row["source_line_ref"] or ""),
                )
            if ordered <= 0:
                raise FutureSupplyCaptureError("parent future-supply capture has invalid ordered qty")
            row["realized_qty_at_cutoff"] = realized
            row["open_qty_at_cutoff"] = max(ordered - realized, Decimal("0"))
        else:
            row["open_qty_at_cutoff"] = max(Decimal(str(row["open_qty_at_cutoff"] or 0)), Decimal("0"))
        # Persist an aware timestamp.  Passing the naive UTC helper result into
        # PostgreSQL timestamptz makes the session timezone reinterpret it and
        # shifts the evidence cutoff (Europe/Moscow used to subtract 3 hours).
        row["capture_cutoff"] = target.cutoff
        carried.append(row)
    carried.sort(key=_row_sort_key)
    return carried


def _generation_rows(db: Session, generation_id: int) -> list[models.LedgerFutureSupply]:
    return db.query(models.LedgerFutureSupply).filter(
        models.LedgerFutureSupply.ledger_generation_id == int(generation_id)
    ).all()


def _capture_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    normalized = list(rows)
    payloads = sorted(
        (_row_hash_payload(row) for row in normalized), key=_row_sort_key
    )
    open_qty = sum(
        (Decimal(str(row["open_qty_at_cutoff"] or 0)) for row in normalized),
        Decimal("0"),
    )
    return {
        "rows": len(normalized),
        "exact_rows": sum(1 for row in normalized if str(row["evidence_status"]) == "exact"),
        "open_qty": canonical_decimal(open_qty),
        "content_hash": canonical_content_hash(payloads),
    }


def _bound_capture_metrics(
    generation: models.LedgerGeneration,
    batch: models.LedgerBuildBatch,
    metrics: Mapping[str, Any],
) -> dict[str, Any]:
    if generation.cutoff is None:
        raise FutureSupplyCaptureError("future supply capture requires generation cutoff")
    return {
        **dict(metrics),
        "open_qty": canonical_decimal(metrics.get("open_qty", 0)),
        "surplus_qty": canonical_decimal(metrics.get("surplus_qty", 0)),
        "generation_id": int(generation.id),
        "cutoff": _as_utc(generation.cutoff).isoformat(),
        "algorithm_version": str(batch.algorithm_version),
    }


def _result_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Keep service quantity results numeric while stored JSON stays canonical."""
    result = dict(metrics)
    for key in ("open_qty", "surplus_qty"):
        if key in result:
            result[key] = Decimal(str(result[key] or 0))
    return result


def verify_future_supply_capture(
    db: Session,
    generation_id: int,
    *,
    capture_batch_id: int | None = None,
) -> Mapping[str, Any]:
    """Verify completed future-supply capture proof for a building generation.

    The future-supply stage must be closed and its persisted rows must match the
    stored digest and count to prevent silent proof drift.
    """
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or str(generation.status) not in {"building", "accepted"}:
        raise FutureSupplyCaptureError(
            "future supply capture verification requires a building or accepted generation"
        )
    expected_batch_id = int(capture_batch_id) if capture_batch_id is not None else None
    batches = db.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(generation.id),
        models.LedgerBuildBatch.stage == FUTURE_SUPPLY_CAPTURE_STAGE,
    ).all()
    if not batches:
        raise FutureSupplyCaptureError("future supply capture is missing")
    if len(batches) != 1:
        raise FutureSupplyCaptureError(
            "future supply generation must have exactly one future_supply_capture batch"
        )
    batch = batches[0]
    if expected_batch_id is not None and int(batch.id) != expected_batch_id:
        raise FutureSupplyCaptureError(
            "future supply capture batch id does not match manifest evidence"
        )
    if str(batch.algorithm_version) not in {
        FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION,
        CARRY_FORWARD_ALGORITHM_VERSION,
    }:
        raise FutureSupplyCaptureError(
            "future supply capture batch algorithm_version is unsupported"
        )
    if str(batch.status) != "completed":
        raise FutureSupplyCaptureError(
            "future supply capture batch must be completed before publication"
        )
    if generation.cutoff is None:
        raise FutureSupplyCaptureError("future supply capture verification requires generation cutoff")

    rows = db.query(models.LedgerFutureSupply).filter(
        models.LedgerFutureSupply.ledger_generation_id == int(generation.id),
        models.LedgerFutureSupply.capture_batch_id == int(batch.id),
    ).all()
    payloads = [_stored_row_payload(row) for row in rows]
    expected_hash = canonical_content_hash(
        sorted(payloads, key=_row_sort_key)
    )
    metrics = dict(batch.metrics or {})
    try:
        metric_generation_id = int(metrics.get("generation_id"))
    except (TypeError, ValueError) as exc:
        raise FutureSupplyCaptureError(
            "future supply capture generation_id is missing or malformed"
        ) from exc
    if metric_generation_id != int(generation.id):
        raise FutureSupplyCaptureError(
            "future supply capture generation_id does not match target generation"
        )
    try:
        metric_cutoff = _parse_utc(metrics.get("cutoff"))
    except (TypeError, ValueError) as exc:
        raise FutureSupplyCaptureError(
            "future supply capture metrics cutoff is missing or malformed"
        ) from exc
    if metric_cutoff != _as_utc(generation.cutoff):
        raise FutureSupplyCaptureError(
            "future supply capture cutoff does not match target generation"
        )
    if str(metrics.get("algorithm_version") or "") != str(batch.algorithm_version):
        raise FutureSupplyCaptureError(
            "future supply capture algorithm_version does not match batch"
        )
    try:
        expected_rows = int(metrics.get("rows", -1))
    except (TypeError, ValueError) as exc:
        raise FutureSupplyCaptureError("future supply capture metrics are malformed") from exc
    if expected_rows != len(rows):
        raise FutureSupplyCaptureError("future supply capture rows do not match persisted facts")
    if str(metrics.get("content_hash") or "") != expected_hash:
        raise FutureSupplyCaptureError("future supply capture content hash does not match persisted facts")

    generation_cutoff = _as_utc(generation.cutoff)
    for row in rows:
        if _as_utc(row.capture_cutoff) != generation_cutoff:
            raise FutureSupplyCaptureError("future supply capture rows have stale cutoff")
    return _result_metrics(metrics)


def carry_forward_future_supply(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
) -> Mapping[str, Any]:
    """Copy the accepted parent's future-supply capture into a refresh target.

    A physical refresh advances facts without re-reading mutable supplier or
    WIP orders.  Their accepted quantities are carried under one dedicated
    batch, physical realizations between the two cutoffs are applied, and every
    carried row is rebound to the target generation cutoff.  Idempotent: an
    exact repeat returns the existing summary.
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

    carried_rows = _carry_forward_rows(db, parent=parent, target=target)
    expected = _capture_summary(carry_forward_rows := carried_rows)
    existing_rows = _generation_rows(db, int(target.id))
    if existing_rows:
        if _capture_summary(
            _stored_row_payload(row) for row in existing_rows
        ) != expected:
            raise FutureSupplyCaptureError(
                "target already carries a conflicting future-supply capture"
            )
        return _result_metrics({
            **expected,
            "created": False,
            "source_generation_id": int(parent.id),
        })

    batch_key = f"future-supply-carry-forward:g{int(target.id)}"
    batch = db.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(target.id),
        models.LedgerBuildBatch.stage == FUTURE_SUPPLY_CAPTURE_STAGE,
        models.LedgerBuildBatch.batch_key == batch_key,
    ).one_or_none()
    if batch is None:
        batch = models.LedgerBuildBatch(
            ledger_generation_id=int(target.id),
            stage=FUTURE_SUPPLY_CAPTURE_STAGE,
            batch_key=batch_key,
            status="building",
            algorithm_version=CARRY_FORWARD_ALGORITHM_VERSION,
            metrics={},
        )
        db.add(batch)
        db.flush()
    for source in carry_forward_rows:
        db.add(models.LedgerFutureSupply(
            ledger_generation_id=int(target.id),
            capture_batch_id=int(batch.id),
            **{field: source[field] for field in _CARRY_FORWARD_FIELDS},
        ))
    db.flush()
    persisted_payloads = sorted(
        (_stored_row_payload(row) for row in _generation_rows(db, int(target.id))),
        key=_row_sort_key,
    )
    expected_payloads = sorted(
        (_row_hash_payload(row) for row in carry_forward_rows),
        key=_row_sort_key,
    )
    if persisted_payloads != expected_payloads:
        differing_fields: list[str] = []
        for expected_row, persisted_row in zip(expected_payloads, persisted_payloads):
            differing_fields = sorted(
                key
                for key in set(expected_row) | set(persisted_row)
                if expected_row.get(key) != persisted_row.get(key)
            )
            if differing_fields:
                break
        if len(expected_payloads) != len(persisted_payloads):
            differing_fields.append("row_count")
        raise FutureSupplyCaptureError(
            "carried-forward future supply conflicts; differing_fields="
            f"{sorted(set(differing_fields))}"
        )
    expected = _capture_summary(persisted_payloads)
    batch.status = "completed"
    batch.metrics = {
        **expected,
        "generation_id": int(target.id),
        "cutoff": _as_utc(target.cutoff).isoformat(),
        "algorithm_version": str(batch.algorithm_version),
        "source_generation_id": int(parent.id),
        "carried_forward": True,
    }
    batch.completed_at = datetime.now(timezone.utc)
    db.flush()
    return _result_metrics({
        **expected,
        "created": True,
        "source_generation_id": int(parent.id),
    })


def replace_future_supply_capture(
    db: Session,
    generation_id: int,
    capture_batch_id: int,
    evidence: Iterable[FutureSupplyEvidence],
) -> Mapping[str, Any]:
    """Atomically replace one BUILDING snapshot batch's future-supply projection.

    A savepoint gives the caller all-or-nothing behavior without committing or
    rolling back its transaction.  The batch keeps whatever status it had:
    lifecycle ownership stays with the generation orchestrator.

    A generation that is still BUILDING may be resumed after its snapshot batch
    was already sealed COMPLETED, so that batch is accepted too — but only as a
    re-read: a completed batch's rows must come out byte-identical, and any
    difference is a conflict rather than an overwrite of sealed evidence.
    """
    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None or str(generation.status) != "building":
        raise FutureSupplyCaptureError("future supply capture requires a BUILDING generation")
    batch = db.get(models.LedgerBuildBatch, int(capture_batch_id))
    if (
        batch is None
        or int(batch.ledger_generation_id) != int(generation.id)
        or str(batch.stage) != FUTURE_SUPPLY_CAPTURE_STAGE
        or str(batch.status) not in {"building", "completed"}
    ):
        raise FutureSupplyCaptureError(
            "capture batch must be this generation's BUILDING or own COMPLETED "
            "future_supply_capture batch"
        )

    if str(batch.algorithm_version) != FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION:
        raise FutureSupplyCaptureError(
            "direct future supply capture requires the canonical capture algorithm"
        )
    rows, raw_metrics = _validated_rows(generation, evidence)
    stored_metrics = _bound_capture_metrics(generation, batch, raw_metrics)
    metrics = dict(stored_metrics)
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
            if str(batch.status) != "completed":
                batch.metrics = dict(stored_metrics)
            return _result_metrics(metrics)
        if str(batch.status) == "completed":
            raise FutureSupplyCaptureError(
                "completed capture batch conflicts with the recaptured future supply"
            )
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
        # Seal the digest from the values PostgreSQL actually persisted.  The
        # quantity columns have scale=3, so hashing the higher-precision input
        # would create a manifest that can never verify after the INSERT has
        # rounded it to the declared storage representation.
        persisted_rows = db.query(models.LedgerFutureSupply).filter(
            models.LedgerFutureSupply.ledger_generation_id == int(generation.id),
            models.LedgerFutureSupply.capture_batch_id == int(batch.id),
        ).all()
        for persisted_row in persisted_rows:
            db.refresh(persisted_row)
        persisted_summary = _capture_summary(
            _stored_row_payload(row) for row in persisted_rows
        )
        stored_metrics = _bound_capture_metrics(
            generation,
            batch,
            {**raw_metrics, **persisted_summary},
        )
        metrics = dict(stored_metrics)
        batch.metrics = dict(stored_metrics)
    return _result_metrics(metrics)

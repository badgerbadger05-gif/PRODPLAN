"""Create an unpublished generation which reuses accepted physical truth.

An obligation refresh deliberately forks only the immutable physical prefix.
It does not copy reservations, future supply, or any planning result.  The
caller is responsible for serialising this operation with any other generation
lifecycle work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from sqlalchemy.orm import Session

from app import models

from .generation_lifecycle import materialize_generation_stock_bins


ALGORITHM_VERSION = "ledger-obligation-generation/1"
REPLAY_VERSION = "ledger-obligation-replay/1"
GENERATION_KIND = "obligation_refresh"


class ObligationGenerationError(RuntimeError):
    """The requested obligation-generation lineage is not safe to create."""


@dataclass(frozen=True)
class ObligationGenerationResult:
    ledger_generation_id: int
    generation_key: str
    physical_import_batch_id: int
    cutoff: datetime
    created: bool


def _utc(value: datetime | None, field: str) -> datetime:
    if value is None:
        raise ObligationGenerationError(f"{field} is missing")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _expected_watermarks_with_replay_from(
    parent_id: int,
    replay_from: datetime,
) -> dict[str, Any]:
    return {
        "parent_generation_id": int(parent_id),
        "generation_kind": GENERATION_KIND,
        "replay_from": _utc(replay_from, "replay_from").isoformat(),
    }


def _checkpoint_key(generation_key: str) -> str:
    return f"obligation-refresh:{generation_key}"


def _sealed_manifest_keys() -> tuple[str, str]:
    """The two watermark keys a *later* step of this same build may add.

    Imported lazily: the manifest module consumes this one, so an import-time
    dependency in the other direction would close the cycle.
    """
    from app.services.obligation_refresh_manifest import (
        MANIFEST_HASH_KEY,
        MANIFEST_KEY,
    )

    return str(MANIFEST_KEY), str(MANIFEST_HASH_KEY)


def _manifest_hash(payload: Any) -> str:
    """Byte-identical to ``obligation_refresh_manifest._hash``."""
    return sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _watermarks_are_own_progress(
    existing: dict[str, Any], expected: dict[str, Any]
) -> bool:
    """Is ``existing`` the fork's own lineage, possibly advanced by its build?

    A resume re-enters :func:`fork_obligation_generation` with the same
    ``generation_key`` *after* later steps have already run.  The only step
    which is allowed to grow an obligation target's watermarks is the manifest
    seal, so:

    * every key the fork itself wrote must still be byte-identical — that is
      what proves this generation belongs to the same parent, the same
      ``replay_from`` and the same generation kind, and it is what rejects a
      foreign request squatting on the key;
    * the only tolerated additions are the sealed manifest pair, and the seal
      must still hash to its own recorded content hash.

    Anything else (a missing base key, a mutated base key, an unknown extra key,
    a manifest whose hash does not match) is a conflict, not progress.  The
    *request* sealed inside that manifest is re-verified by
    :func:`create_obligation_refresh_manifest` immediately afterwards, so a
    same-lineage build with different plan ids is still refused — one layer up,
    where the request actually lives.
    """
    manifest_key, hash_key = _sealed_manifest_keys()
    base = {
        key: value
        for key, value in existing.items()
        if key not in (manifest_key, hash_key)
    }
    if base != expected:
        return False
    extra = set(existing) - set(base)
    if not extra:
        return True
    if extra != {manifest_key, hash_key}:
        return False
    manifest = existing.get(manifest_key)
    content_hash = existing.get(hash_key)
    if not isinstance(manifest, dict) or not isinstance(content_hash, str):
        return False
    return _manifest_hash(manifest) == content_hash


def _reused_metrics(
    parent: models.LedgerGeneration,
    physical: models.PhysicalImportBatch,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Record the exact reused boundary rather than manufacturing an import."""
    return {
        "reused": True,
        "parent_generation_id": int(parent.id),
        "physical_import_batch_id": int(physical.id),
        "physical_batch_key": str(physical.batch_key),
        "physical_batch_metrics": dict(physical.source_watermarks or {}),
        # Kept as explicit scalar metrics for operational audit/search, plus
        # the grouped form for callers which consume the clone summary.
        "supplier_receipt_provenance_count": int(provenance["count"]),
        "supplier_receipt_provenance_checksum": str(provenance["checksum"]),
        "supplier_receipt_provenance": dict(provenance),
    }


_PROVENANCE_FIELDS = (
    "stock_ledger_entry_id",
    "receipt_doc_type",
    "receipt_doc_ref",
    "receipt_doc_line_no",
    "supplier_order_ref",
    "supplier_order_line_no",
    "operation_kind",
    "operation_key",
    "operation_name",
    "correction_receipt_ref",
    "evidence_hash",
    "evidence_payload",
    "match_rule",
    "match_status",
    "ambiguity_count",
    "reason",
)


def _provenance_rows(db: Session, generation_id: int) -> list[models.StockLedgerSupplierReceiptProvenance]:
    return db.query(models.StockLedgerSupplierReceiptProvenance).filter(
        models.StockLedgerSupplierReceiptProvenance.ledger_generation_id == int(generation_id)
    ).order_by(
        models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id,
        models.StockLedgerSupplierReceiptProvenance.id,
    ).all()


def _provenance_value(row: models.StockLedgerSupplierReceiptProvenance) -> dict[str, Any]:
    """Canonical immutable business evidence, excluding generation-local identity."""
    return {
        field: (dict(getattr(row, field) or {}) if field == "evidence_payload" else getattr(row, field))
        for field in _PROVENANCE_FIELDS
    }


def _provenance_summary(rows: list[models.StockLedgerSupplierReceiptProvenance]) -> dict[str, Any]:
    encoded = json.dumps(
        [_provenance_value(row) for row in rows],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return {"count": len(rows), "checksum": sha256(encoded).hexdigest()}


def _clone_supplier_receipt_provenance(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
) -> dict[str, Any]:
    """Copy all immutable receipt-match decisions into an obligation fork.

    Evidence is generation scoped because later future-supply captures must be
    reproducible from the accepted generation alone.  It is not recalculated
    here: exact, ambiguous and unmatched decisions all travel with the shared
    physical prefix.
    """
    parent_rows = _provenance_rows(db, parent_generation_id)
    for source in parent_rows:
        db.add(models.StockLedgerSupplierReceiptProvenance(
            ledger_generation_id=int(target_generation_id),
            **_provenance_value(source),
        ))
    db.flush()
    target_rows = _provenance_rows(db, target_generation_id)
    expected = _provenance_summary(parent_rows)
    if _provenance_summary(target_rows) != expected:
        raise ObligationGenerationError("cloned supplier receipt provenance conflicts")
    return expected


def _require_current_accepted_parent(
    db: Session, parent_generation_id: int
) -> tuple[models.LedgerGeneration, models.PhysicalImportBatch, datetime]:
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    if parent is None or str(parent.status) != "accepted":
        raise ObligationGenerationError("parent generation must be ACCEPTED")
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or pointer.current_generation_id is None:
        raise ObligationGenerationError("planning truth pointer is not set")
    if int(pointer.current_generation_id) != int(parent.id):
        raise ObligationGenerationError(
            "parent generation is not the current planning truth pointer"
        )
    if parent.cutoff is None or parent.physical_import_batch_id is None:
        raise ObligationGenerationError("accepted parent has incomplete physical lineage")
    physical = db.get(
        models.PhysicalImportBatch, int(parent.physical_import_batch_id)
    )
    if physical is None or str(physical.status) != "completed":
        raise ObligationGenerationError("parent physical import batch is not completed")
    parent_marks = dict(parent.source_watermarks or {})
    try:
        parent_replay_from = datetime.fromisoformat(
            str(parent_marks["replay_from"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ObligationGenerationError(
            "parent generation lacks replay_from lineage"
        ) from exc
    return parent, physical, _utc(parent_replay_from, "replay_from")


def _exact_existing(
    db: Session,
    existing: models.LedgerGeneration,
    *,
    parent: models.LedgerGeneration,
    physical: models.PhysicalImportBatch,
    key: str,
    replay_from: datetime,
) -> None:
    if (
        str(existing.status) != "building"
        or existing.physical_import_batch_id != physical.id
        or _utc(existing.cutoff, "existing cutoff") != _utc(parent.cutoff, "parent cutoff")
        or not _watermarks_are_own_progress(
            dict(existing.source_watermarks or {}),
            _expected_watermarks_with_replay_from(parent.id, replay_from),
        )
        # ``capabilities`` are deliberately NOT compared: the fork creates them
        # empty, but the later build steps of this very generation declare the
        # capability snapshot they produced, and a resume must not read that as
        # a foreign lineage.  What the candidate actually claims is audited
        # where it matters — ``publish_obligation_refresh_batch`` refuses to
        # accept a target whose capabilities are empty, incomplete, or differ
        # from the snapshot the publisher was handed.
        or not isinstance(existing.capabilities or {}, dict)
        or str(existing.algorithm_version) != ALGORITHM_VERSION
        or str(existing.replay_version) != REPLAY_VERSION
    ):
        raise ObligationGenerationError(
            "generation_key already exists with different or non-BUILDING obligation lineage"
        )
    checkpoints = db.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(existing.id),
        models.LedgerBuildBatch.stage == "physical_import",
    ).all()
    parent_provenance = _provenance_rows(db, int(parent.id))
    expected_provenance = _provenance_summary(parent_provenance)
    existing_provenance = _provenance_rows(db, int(existing.id))
    if _provenance_summary(existing_provenance) != expected_provenance:
        raise ObligationGenerationError("existing obligation provenance conflicts")
    expected_metrics = _reused_metrics(parent, physical, expected_provenance)
    if len(checkpoints) != 1:
        raise ObligationGenerationError("existing obligation generation lacks one physical checkpoint")
    checkpoint = checkpoints[0]
    if (
        str(checkpoint.status) != "completed"
        or str(checkpoint.batch_key) != _checkpoint_key(key)
        or str(checkpoint.algorithm_version) != ALGORITHM_VERSION
        or dict(checkpoint.metrics or {}) != expected_metrics
    ):
        raise ObligationGenerationError("existing obligation checkpoint conflicts")


def fork_obligation_generation(
    db: Session,
    parent_generation_id: int,
    generation_key: str,
) -> ObligationGenerationResult:
    """Fork a BUILDING obligation candidate from the current accepted prefix.

    This helper deliberately takes no PostgreSQL lock and owns no transaction:
    its lifecycle caller serialises the operation and atomically commits (or
    rolls back) this candidate together with the remaining planning snapshot.
    """
    key = str(generation_key or "").strip()
    if not key:
        raise ValueError("generation_key is required")
    if len(_checkpoint_key(key)) > 128:
        raise ValueError("generation_key is too long")

    parent, physical, replay_from = _require_current_accepted_parent(
        db, parent_generation_id
    )
    existing = db.query(models.LedgerGeneration).filter(
        models.LedgerGeneration.generation_key == key
    ).one_or_none()
    if existing is not None:
        _exact_existing(
            db,
            existing,
            parent=parent,
            physical=physical,
            key=key,
            replay_from=replay_from,
        )
        return ObligationGenerationResult(
            ledger_generation_id=int(existing.id),
            generation_key=key,
            physical_import_batch_id=int(physical.id),
            cutoff=_utc(parent.cutoff, "parent cutoff"),
            created=False,
        )

    candidate = models.LedgerGeneration(
        generation_key=key,
        status="building",
        cutoff=parent.cutoff,
        source_watermarks=_expected_watermarks_with_replay_from(
            parent.id, replay_from,
        ),
        capabilities={},
        physical_import_batch_id=int(physical.id),
        algorithm_version=ALGORITHM_VERSION,
        replay_version=REPLAY_VERSION,
    )
    db.add(candidate)
    db.flush()
    materialize_generation_stock_bins(db, int(candidate.id))
    provenance = _clone_supplier_receipt_provenance(
        db,
        parent_generation_id=int(parent.id),
        target_generation_id=int(candidate.id),
    )
    db.add(models.LedgerBuildBatch(
        ledger_generation_id=int(candidate.id),
        stage="physical_import",
        batch_key=_checkpoint_key(key),
        status="completed",
        algorithm_version=ALGORITHM_VERSION,
        metrics=_reused_metrics(parent, physical, provenance),
        completed_at=datetime.now(timezone.utc),
    ))
    db.flush()

    return ObligationGenerationResult(
        ledger_generation_id=int(candidate.id),
        generation_key=key,
        physical_import_batch_id=int(physical.id),
        cutoff=_utc(parent.cutoff, "parent cutoff"),
        created=True,
    )


def carry_forward_retained_reservations(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
    retained_run_ids: tuple[int, ...],
    preserve_realization: bool = False,
) -> int:
    """Copy immutable retained obligations, but never carry physical realization.

    A realization belongs to the candidate generation's physical replay.  Copying
    it from the parent and then replaying the shared physical prefix lets one SLE
    cover both a retained reservation and a new candidate reservation.  The
    retained obligation is immutable, so seed one fresh ``open`` event and let
    the generation-wide replay rebuild all physical coverage.
    """
    run_ids = tuple(sorted({int(value) for value in retained_run_ids}))
    if not run_ids:
        return 0
    existing_count = db.query(models.ReservationEntry.id).filter(
        models.ReservationEntry.ledger_generation_id == int(target_generation_id),
        models.ReservationEntry.run_id.in_(run_ids),
    ).count()
    source_entries = db.query(models.ReservationEntry).filter(
        models.ReservationEntry.ledger_generation_id == int(parent_generation_id),
        models.ReservationEntry.run_id.in_(run_ids),
    ).order_by(models.ReservationEntry.id.asc()).all()
    if existing_count:
        if existing_count != len(source_entries):
            raise ObligationGenerationError(
                "retained reservation projection is partial on target generation"
            )
        return int(existing_count)
    new_ids: dict[int, int] = {}
    for source in source_entries:
        target = models.ReservationEntry(
            ledger_generation_id=int(target_generation_id),
            item_id=source.item_id,
            characteristic_ref=source.characteristic_ref,
            organization_ref=source.organization_ref,
            planning_stock_pool=source.planning_stock_pool,
            run_id=source.run_id,
            freeze_version=source.freeze_version,
            requirement_id=source.requirement_id,
            priority_period_from=source.priority_period_from,
            priority_period_to=source.priority_period_to,
            realization_mode=source.realization_mode,
            reserved_qty=source.reserved_qty,
            realized_qty=(source.realized_qty if preserve_realization else Decimal("0")),
            covered_from_stock_at_freeze_qty=source.covered_from_stock_at_freeze_qty,
            replenishment_required_qty=source.replenishment_required_qty,
            replenishment_received_qty=(
                source.replenishment_received_qty
                if preserve_realization
                else Decimal("0")
            ),
            lifecycle_status=source.lifecycle_status,
            opened_at=source.opened_at,
            closed_at=source.closed_at,
        )
        db.add(target)
        db.flush()
        new_ids[int(source.id)] = int(target.id)
    for source in source_entries:
        target_id = new_ids[int(source.id)]
        db.add(models.ReservationEvent(
            ledger_generation_id=int(target_generation_id),
            reservation_id=target_id,
            item_id=source.item_id,
            characteristic_ref=source.characteristic_ref,
            organization_ref=source.organization_ref,
            planning_stock_pool=source.planning_stock_pool,
            event_kind="open",
            reserved_delta=source.reserved_qty,
            realized_delta=(
                source.replenishment_received_qty
                if preserve_realization
                else Decimal("0")
            ),
            sle_id=None,
            fact_ref="",
            fact_line_ref="",
            match_rule="",
            cycle_id=f"obligation-carry:g{int(target_generation_id)}",
            idempotency_key=(
                f"carry-open:g{int(target_generation_id)}:source-r{int(source.id)}"
            ),
            event_at=source.opened_at or datetime.now(timezone.utc),
        ))
    db.flush()
    return len(source_entries)

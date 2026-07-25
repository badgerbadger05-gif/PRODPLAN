"""Create an unpublished generation which reuses accepted physical truth for refresh.

This primitive intentionally forks only immutable physical lineage from a
current accepted planning-Truth parent.  It does not generate obligations,
reservations, or future supply snapshots.  The refresh pipeline will consume
the candidate that this function creates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

from sqlalchemy.orm import Session

from app import models

from .generation_lifecycle import materialize_generation_stock_bins


ALGORITHM_VERSION = "ledger-physical-refresh-generation/1"
REPLAY_VERSION = "ledger-physical-refresh-replay/1"
GENERATION_KIND = "physical_refresh"


class PhysicalRefreshGenerationError(RuntimeError):
    """The requested physical-refresh lineage is not safe to create."""


@dataclass(frozen=True)
class PhysicalRefreshGenerationResult:
    ledger_generation_id: int
    generation_key: str
    physical_import_batch_id: int
    cutoff: datetime
    from_cutoff: datetime
    created: bool


def _utc(value: datetime | None, field: str) -> datetime:
    if value is None:
        raise PhysicalRefreshGenerationError(f"{field} is missing")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _expected_watermarks(
    *,
    parent_id: int,
    parent_physical_import_batch_id: int,
    from_cutoff: datetime,
    replay_from: datetime,
) -> dict[str, Any]:
    return {
        "parent_generation_id": int(parent_id),
        "parent_physical_import_batch_id": int(parent_physical_import_batch_id),
        "generation_kind": GENERATION_KIND,
        "from_cutoff": _utc(from_cutoff, "from_cutoff").isoformat(),
        "replay_from": _utc(replay_from, "replay_from").isoformat(),
    }


def _checkpoint_key(generation_key: str) -> str:
    return f"physical-refresh:{generation_key}"


def _reused_metrics(
    parent: models.LedgerGeneration,
    physical: models.PhysicalImportBatch,
    provenance: dict[str, Any],
    from_cutoff: datetime,
) -> dict[str, Any]:
    return {
        "reused": True,
        "parent_generation_id": int(parent.id),
        "parent_physical_import_batch_id": int(parent.physical_import_batch_id),
        "from_cutoff": _utc(from_cutoff, "from_cutoff").isoformat(),
        "physical_import_batch_id": int(physical.id),
        "physical_batch_key": str(physical.batch_key),
        "physical_batch_metrics": dict(physical.source_watermarks or {}),
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


def _provenance_rows(
    db: Session,
    generation_id: int,
) -> list[models.StockLedgerSupplierReceiptProvenance]:
    return db.query(models.StockLedgerSupplierReceiptProvenance).filter(
        models.StockLedgerSupplierReceiptProvenance.ledger_generation_id == int(generation_id)
    ).order_by(
        models.StockLedgerSupplierReceiptProvenance.stock_ledger_entry_id,
        models.StockLedgerSupplierReceiptProvenance.id,
    ).all()


def _provenance_value(row: models.StockLedgerSupplierReceiptProvenance) -> dict[str, Any]:
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
        raise PhysicalRefreshGenerationError(
            "cloned supplier receipt provenance conflicts"
        )
    return expected


def _require_current_accepted_parent(
    db: Session,
    parent_generation_id: int,
) -> tuple[models.LedgerGeneration, models.PhysicalImportBatch, datetime]:
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    if parent is None or str(parent.status) != "accepted":
        raise PhysicalRefreshGenerationError("parent generation must be ACCEPTED")
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or pointer.current_generation_id is None:
        raise PhysicalRefreshGenerationError("planning truth pointer is not set")
    if int(pointer.current_generation_id) != int(parent.id):
        raise PhysicalRefreshGenerationError(
            "parent generation is not the current planning truth pointer"
        )
    if parent.cutoff is None or parent.physical_import_batch_id is None:
        raise PhysicalRefreshGenerationError("accepted parent has incomplete physical lineage")
    physical = db.get(models.PhysicalImportBatch, int(parent.physical_import_batch_id))
    if physical is None or str(physical.status) != "completed":
        raise PhysicalRefreshGenerationError("parent physical import batch is not completed")
    parent_marks = dict(parent.source_watermarks or {})
    try:
        parent_replay_from = datetime.fromisoformat(str(parent_marks["replay_from"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PhysicalRefreshGenerationError(
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
    cutoff: datetime,
    from_cutoff: datetime,
    replay_from: datetime,
) -> None:
    expected_marks = _expected_watermarks(
        parent_id=parent.id,
        parent_physical_import_batch_id=int(parent.physical_import_batch_id),
        from_cutoff=from_cutoff,
        replay_from=replay_from,
    )
    actual_marks = dict(existing.source_watermarks or {})
    if (
        str(existing.status) != "building"
        or existing.physical_import_batch_id != physical.id
        or _utc(existing.cutoff, "existing cutoff") != _utc(cutoff, "cutoff")
        or any(actual_marks.get(name) != value for name, value in expected_marks.items())
        or dict(existing.capabilities or {}) != {}
        or str(existing.algorithm_version) != ALGORITHM_VERSION
        or str(existing.replay_version) != REPLAY_VERSION
    ):
        raise PhysicalRefreshGenerationError(
            "generation_key already exists with different or non-BUILDING physical refresh lineage"
        )
    checkpoints = db.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(existing.id),
        models.LedgerBuildBatch.stage == "physical_import",
        models.LedgerBuildBatch.batch_key == _checkpoint_key(key),
    ).all()
    expected_provenance = _provenance_summary(_provenance_rows(db, int(parent.id)))
    existing_provenance = _provenance_summary(_provenance_rows(db, int(existing.id)))
    if existing_provenance != expected_provenance:
        raise PhysicalRefreshGenerationError("existing physical refresh provenance conflicts")
    if len(checkpoints) != 1:
        raise PhysicalRefreshGenerationError(
            "existing physical refresh generation lacks one physical checkpoint"
        )
    checkpoint = checkpoints[0]
    try:
        seed_physical_id = int(
            dict(checkpoint.metrics or {})["physical_import_batch_id"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PhysicalRefreshGenerationError(
            "existing physical refresh checkpoint lacks seed boundary"
        ) from exc
    seed_physical = db.get(models.PhysicalImportBatch, seed_physical_id)
    if seed_physical is None or str(seed_physical.status) != "completed":
        raise PhysicalRefreshGenerationError(
            "existing physical refresh seed boundary is not completed"
        )
    expected_metrics = _reused_metrics(
        parent, seed_physical, expected_provenance, from_cutoff
    )
    if (
        str(checkpoint.status) != "completed"
        or str(checkpoint.batch_key) != _checkpoint_key(key)
        or str(checkpoint.algorithm_version) != ALGORITHM_VERSION
        or dict(checkpoint.metrics or {}) != expected_metrics
    ):
        raise PhysicalRefreshGenerationError("existing physical refresh checkpoint conflicts")


def fork_physical_refresh_generation(
    db: Session,
    parent_generation_id: int,
    generation_key: str,
    *,
    from_cutoff: datetime,
    target_cutoff: datetime,
) -> PhysicalRefreshGenerationResult:
    """Fork a BUILDING refresh candidate from the current accepted prefix.

    This helper deliberately takes no PostgreSQL lock and owns no transaction:
    its caller owns serialization and atomically commits this candidate together
    with the remaining planning snapshot.
    """
    key = str(generation_key or "").strip()
    if not key:
        raise ValueError("generation_key is required")
    if len(_checkpoint_key(key)) > 128:
        raise ValueError("generation_key is too long")

    target = _utc(target_cutoff, "target cutoff")
    replay_from = _utc(from_cutoff, "from_cutoff")
    parent, physical, inherited_replay_from = _require_current_accepted_parent(
        db, parent_generation_id
    )
    parent_cutoff = _utc(parent.cutoff, "parent cutoff")
    if target <= _utc(parent.cutoff, "parent cutoff"):
        raise PhysicalRefreshGenerationError("target cutoff must be after parent cutoff")
    if replay_from != parent_cutoff:
        raise PhysicalRefreshGenerationError(
            "from_cutoff must exactly equal parent cutoff"
        )

    existing = db.query(models.LedgerGeneration).filter(
        models.LedgerGeneration.generation_key == key
    ).one_or_none()
    if existing is not None:
        existing_physical = db.get(
            models.PhysicalImportBatch,
            int(existing.physical_import_batch_id or 0),
        )
        if (
            existing_physical is None
            or str(existing_physical.status) != "completed"
        ):
            raise PhysicalRefreshGenerationError(
                "existing physical refresh boundary is not completed"
            )
        _exact_existing(
            db,
            existing,
            parent=parent,
            physical=existing_physical,
            key=key,
            cutoff=target,
            from_cutoff=replay_from,
            replay_from=inherited_replay_from,
        )
        return PhysicalRefreshGenerationResult(
            ledger_generation_id=int(existing.id),
            generation_key=key,
            physical_import_batch_id=int(existing_physical.id),
            cutoff=target,
            from_cutoff=replay_from,
            created=False,
        )

    candidate = models.LedgerGeneration(
        generation_key=key,
        status="building",
        cutoff=target,
        source_watermarks=_expected_watermarks(
            parent_id=int(parent.id),
            parent_physical_import_batch_id=int(parent.physical_import_batch_id),
            from_cutoff=replay_from,
            replay_from=inherited_replay_from,
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
        metrics=_reused_metrics(parent, physical, provenance, replay_from),
        completed_at=datetime.now(timezone.utc),
    ))
    db.flush()

    return PhysicalRefreshGenerationResult(
        ledger_generation_id=int(candidate.id),
        generation_key=key,
        physical_import_batch_id=int(physical.id),
        cutoff=target,
        from_cutoff=replay_from,
        created=True,
    )

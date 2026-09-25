"""Bounded provenance handoff for compact physical-refresh owners.

The handoff is deliberately technical.  It does not copy future-supply or
custody quantities, does not emit semantic audit, and never changes the truth
pointer or generation status.  The caller must keep the operation inside the
same transaction as the later atomic publication; a rollback is required if a
subsequent publisher step fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.services.production_material_custody_projection import _same_1c_timestamp


class PhysicalRefreshProvenanceUnavailable(ValueError):
    """The current compact owner cannot be safely handed to a target."""


@dataclass(frozen=True)
class CompactProvenanceHandoffResult:
    parent_generation_id: int
    target_generation_id: int
    future_supply_rows: int
    custody_rows: int
    custody_event_watermark: int
    future_supply_idempotent: bool
    custody_idempotent: bool


def _pointer(db: Session) -> models.PlanningTruthState:
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or pointer.current_generation_id is None:
        raise PhysicalRefreshProvenanceUnavailable(
            "physical refresh provenance requires an exact planning-truth pointer"
        )
    return pointer


def _generations(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
) -> tuple[models.LedgerGeneration, models.LedgerGeneration, bool]:
    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    target = db.get(models.LedgerGeneration, int(target_generation_id))
    if parent is None or target is None:
        raise PhysicalRefreshProvenanceUnavailable(
            "physical refresh provenance generation is missing"
        )
    pointer = _pointer(db)
    target_current = (
        str(target.status or "") == "accepted"
        and int(pointer.current_generation_id) == int(target.id)
    )
    if target_current:
        if int(parent.id) == int(target.id):
            return parent, target, True
        # Exact retry after the target publication is allowed.  The parent is
        # no longer the pointer, but current owners must already name target.
        return parent, target, True
    if str(parent.status or "") != "accepted":
        raise PhysicalRefreshProvenanceUnavailable(
            "physical refresh provenance requires an accepted parent"
        )
    if int(pointer.current_generation_id) != int(parent.id):
        raise PhysicalRefreshProvenanceUnavailable(
            "physical refresh provenance parent is not exact current truth"
        )
    if str(target.status or "") != "building":
        raise PhysicalRefreshProvenanceUnavailable(
            "physical refresh provenance requires a BUILDING target"
        )
    return parent, target, False


def _require_no_future_staging(
    db: Session,
    parent: models.LedgerGeneration,
    target: models.LedgerGeneration,
) -> None:
    rows = (
        db.query(models.LedgerFutureSupply.id)
        .filter(
            models.LedgerFutureSupply.ledger_generation_id.in_(
                [int(parent.id), int(target.id)]
            )
        )
        .limit(1)
        .all()
    )
    if rows:
        raise PhysicalRefreshProvenanceUnavailable(
            "future supply has unresolved generation staging delta"
        )


def handoff_current_future_supply_provenance(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
) -> CompactProvenanceHandoffResult:
    """Rebind current future-supply row provenance without semantic DML."""

    parent, target, already_current = _generations(
        db,
        parent_generation_id=int(parent_generation_id),
        target_generation_id=int(target_generation_id),
    )
    if not already_current:
        _require_no_future_staging(db, parent, target)
    rows = (
        db.query(models.LedgerFutureSupplyCurrent)
        .order_by(models.LedgerFutureSupplyCurrent.id.asc())
        .with_for_update()
        .all()
    )
    if any(str(row.evidence_status or "") != "exact" for row in rows):
        raise PhysicalRefreshProvenanceUnavailable(
            "future supply current owner contains non-exact evidence"
        )
    if already_current:
        if any(int(row.source_generation_id) != int(target.id) for row in rows):
            raise PhysicalRefreshProvenanceUnavailable(
                "future supply current owner provenance is mixed or stale"
            )
        return CompactProvenanceHandoffResult(
            parent_generation_id=int(parent.id),
            target_generation_id=int(target.id),
            future_supply_rows=len(rows),
            custody_rows=0,
            custody_event_watermark=0,
            future_supply_idempotent=True,
            custody_idempotent=False,
        )
    if any(int(row.source_generation_id) != int(parent.id) for row in rows):
        raise PhysicalRefreshProvenanceUnavailable(
            "future supply current owner provenance is mixed or stale"
        )
    for row in rows:
        # Keep every business field, id and source identity unchanged.  This
        # technical provenance update intentionally emits no CurrentChange.
        row.source_generation_id = int(target.id)
    db.flush()
    return CompactProvenanceHandoffResult(
        parent_generation_id=int(parent.id),
        target_generation_id=int(target.id),
        future_supply_rows=len(rows),
        custody_rows=0,
        custody_event_watermark=0,
        future_supply_idempotent=False,
        custody_idempotent=False,
    )


def _ordered_1c_timestamps(
    left: datetime, right: datetime
) -> tuple[datetime, datetime]:
    """Normalise one 1C wall-clock pair exactly as the custody gate compares it."""
    if left.tzinfo is None or right.tzinfo is None:
        return left.replace(tzinfo=None), right.replace(tzinfo=None)
    return left, right


def _custody_event_watermark(db: Session) -> int:
    return int(
        db.query(func.coalesce(func.max(models.ProductionMaterialCustodyEvent.id), 0))
        .scalar()
        or 0
    )


def apply_bounded_current_material_custody_events(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
    source_sle_ids: tuple[int, ...] | list[int] | set[int],
) -> int:
    """Fold the explicit physical custody tail into the compact current owner.

    Recorder ingest can discover a transfer event while importing a physical
    row that is already present (an exact re-pull).  That event is newer than
    the accepted compact manifest even though the ledger delta itself is
    bounded by the caller.  A generation-wide custody replay here would
    reintroduce the old refresh regression, so only events linked to the
    caller's persisted SLE ids are accepted.  Local/non-SLE events and any
    unrelated tail remain unavailable and fail closed.

    The caller owns the transaction.  Rows retain their compact identity and
    are moved to the target only by the existing provenance handoff after all
    other current projections have passed their gates.
    """

    parent, target, already_current = _generations(
        db,
        parent_generation_id=int(parent_generation_id),
        target_generation_id=int(target_generation_id),
    )
    if already_current:
        return 0
    parent_manifest = db.get(
        models.ProductionMaterialCustodyProjectionManifest, int(parent.id)
    )
    if parent_manifest is None or str(parent_manifest.status or "") != "complete":
        raise PhysicalRefreshProvenanceUnavailable(
            "custody current owner manifest is missing or incomplete"
        )

    explicit_ids = tuple(int(value) for value in source_sle_ids)
    if len(set(explicit_ids)) != len(explicit_ids):
        raise PhysicalRefreshProvenanceUnavailable(
            "bounded custody source SLE ids are duplicated"
        )
    explicit = set(explicit_ids)
    if explicit:
        persisted_count = (
            db.query(models.StockLedgerEntry.id)
            .filter(models.StockLedgerEntry.id.in_(sorted(explicit)))
            .count()
        )
        if persisted_count != len(explicit):
            raise PhysicalRefreshProvenanceUnavailable(
                "bounded custody source SLE manifest references a missing row"
            )
    previous_watermark = int(parent_manifest.source_event_high_watermark_id or 0)
    observed_watermark = _custody_event_watermark(db)
    if observed_watermark < previous_watermark:
        raise PhysicalRefreshProvenanceUnavailable(
            "custody event watermark moved backwards"
        )
    tail = (
        db.query(models.ProductionMaterialCustodyEvent)
        .filter(models.ProductionMaterialCustodyEvent.id > previous_watermark)
        .order_by(models.ProductionMaterialCustodyEvent.id.asc())
        .all()
    )
    if not tail:
        return 0
    if any(
        event.source_sle_id is None or int(event.source_sle_id) not in explicit
        for event in tail
    ):
        raise PhysicalRefreshProvenanceUnavailable(
            "custody event tail is not covered by the bounded physical SLE manifest"
        )

    current_rows = (
        db.query(models.ProductionMaterialCustodyProjection)
        .filter(models.ProductionMaterialCustodyProjection.is_current.is_(True))
        .with_for_update()
        .all()
    )
    if any(int(row.ledger_generation_id) != int(parent.id) for row in current_rows):
        raise PhysicalRefreshProvenanceUnavailable(
            "custody current projection provenance is mixed or stale"
        )
    if any(
        int(row.source_event_high_watermark_id or 0) != previous_watermark
        for row in current_rows
    ):
        raise PhysicalRefreshProvenanceUnavailable(
            "custody current projection watermark is mixed or stale"
        )

    rows_by_key = {
        (
            int(row.product_id),
            int(row.component_item_id),
            str(row.location_kind or ""),
            str(row.warehouse_ref1c or ""),
        ): row
        for row in current_rows
    }
    for event in tail:
        key = (
            int(event.product_id),
            int(event.component_item_id),
            str(event.location_kind or ""),
            str(event.warehouse_ref1c or ""),
        )
        row = rows_by_key.get(key)
        current_qty = Decimal(str(row.reserved_qty or 0)) if row is not None else Decimal("0")
        next_qty = current_qty + Decimal(str(event.delta_qty or 0))
        if next_qty < 0:
            raise PhysicalRefreshProvenanceUnavailable(
                "bounded custody event would make compact current quantity negative"
            )
        if row is not None and next_qty == 0:
            db.delete(row)
            rows_by_key.pop(key, None)
        elif row is not None:
            row.reserved_qty = next_qty
        elif next_qty > 0:
            row = models.ProductionMaterialCustodyProjection(
                ledger_generation_id=int(parent.id),
                product_id=int(event.product_id),
                component_item_id=int(event.component_item_id),
                location_kind=str(event.location_kind or ""),
                warehouse_ref1c=str(event.warehouse_ref1c or ""),
                reserved_qty=next_qty,
                source_event_high_watermark_id=int(event.id),
                is_current=True,
            )
            db.add(row)
            rows_by_key[key] = row
        else:
            raise PhysicalRefreshProvenanceUnavailable(
                "bounded custody event has no current basis"
            )

    watermark = int(tail[-1].id)
    for row in rows_by_key.values():
        row.source_event_high_watermark_id = watermark
    # The manifest is the compact owner's provenance.  The handoff below
    # transfers this exact owner/manifest pair to the target; it is not a
    # historical generation replay.
    parent_manifest.source_event_high_watermark_id = watermark
    db.flush()
    return len(tail)


def handoff_current_material_custody_provenance(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
) -> CompactProvenanceHandoffResult:
    """Move compact custody ownership to target when the event tail is empty."""

    parent, target, already_current = _generations(
        db,
        parent_generation_id=int(parent_generation_id),
        target_generation_id=int(target_generation_id),
    )
    target_manifest = db.get(
        models.ProductionMaterialCustodyProjectionManifest, int(target.id)
    )
    current_rows = (
        db.query(models.ProductionMaterialCustodyProjection)
        .filter(models.ProductionMaterialCustodyProjection.is_current.is_(True))
        .with_for_update()
        .all()
    )
    if already_current:
        if target_manifest is None or str(target_manifest.status or "") != "complete":
            raise PhysicalRefreshProvenanceUnavailable(
                "custody target manifest is missing after publication"
            )
        watermark = int(target_manifest.source_event_high_watermark_id or 0)
        if _custody_event_watermark(db) != watermark:
            raise PhysicalRefreshProvenanceUnavailable(
                "custody target manifest watermark is stale"
            )
        if (
            target_manifest.cutoff is None
            or target.cutoff is None
            or not _same_1c_timestamp(target_manifest.cutoff, target.cutoff)
        ):
            # The canonical custody gate compares these two values on every
            # later candidate.  A retry must not certify a manifest the gate
            # would reject.
            raise PhysicalRefreshProvenanceUnavailable(
                "custody target manifest cutoff mismatches its Ledger generation"
            )
        if any(int(row.ledger_generation_id) != int(target.id) for row in current_rows):
            raise PhysicalRefreshProvenanceUnavailable(
                "custody current projection provenance is mixed or stale"
            )
        return CompactProvenanceHandoffResult(
            parent_generation_id=int(parent.id),
            target_generation_id=int(target.id),
            future_supply_rows=0,
            custody_rows=len(current_rows),
            custody_event_watermark=watermark,
            future_supply_idempotent=False,
            custody_idempotent=True,
        )
    parent_manifest = db.get(
        models.ProductionMaterialCustodyProjectionManifest, int(parent.id)
    )
    if parent_manifest is None or str(parent_manifest.status or "") != "complete":
        raise PhysicalRefreshProvenanceUnavailable(
            "custody current owner manifest is missing or incomplete"
        )
    watermark = int(parent_manifest.source_event_high_watermark_id or 0)
    if _custody_event_watermark(db) != watermark:
        raise PhysicalRefreshProvenanceUnavailable(
            "custody has an unpublished event tail"
        )
    if target_manifest is not None:
        raise PhysicalRefreshProvenanceUnavailable(
            "custody target manifest already exists before handoff"
        )
    parent_projection_count = (
        db.query(models.ProductionMaterialCustodyProjection.id)
        .filter(
            models.ProductionMaterialCustodyProjection.ledger_generation_id
            == int(parent.id)
        )
        .count()
    )
    if not current_rows and parent_projection_count:
        raise PhysicalRefreshProvenanceUnavailable(
            "custody current projection marker is missing or stale"
        )
    if any(int(row.ledger_generation_id) != int(parent.id) for row in current_rows):
        raise PhysicalRefreshProvenanceUnavailable(
            "custody current projection provenance is mixed or stale"
        )
    if any(
        int(row.source_event_high_watermark_id or 0) != watermark
        for row in current_rows
    ):
        # Cells and manifest are one compact owner: every reader checks that
        # the rows were folded exactly to the watermark the manifest states.
        raise PhysicalRefreshProvenanceUnavailable(
            "custody current projection watermark is mixed or stale"
        )
    if target.cutoff is None:
        raise PhysicalRefreshProvenanceUnavailable(
            "custody handoff requires a target generation cutoff"
        )
    if parent_manifest.cutoff is not None:
        manifest_cutoff, target_cutoff = _ordered_1c_timestamps(
            parent_manifest.cutoff, target.cutoff
        )
        if target_cutoff < manifest_cutoff:
            raise PhysicalRefreshProvenanceUnavailable(
                "custody manifest cutoff cannot move backwards"
            )
    # Update projection rows before moving the manifest PK.  This preserves
    # row ids and quantities while making the temporary parent-read break
    # explicit: the caller must commit only together with the target pointer.
    for row in current_rows:
        row.ledger_generation_id = int(target.id)
    parent_manifest.ledger_generation_id = int(target.id)
    # The manifest states the boundary of the generation it belongs to, so the
    # moved owner takes the target cutoff.  Carrying the parent's cutoff left
    # every later candidate failing the canonical gate
    # ``_require_manifest_cutoff`` — the compact owner then looked like a
    # snapshot of an older window and no new generation could resolve it as a
    # baseline.  The watermark is deliberately kept: it is the point the
    # compact cells were folded to, and the gate above proves it is the whole
    # event stream.  Lowering it to the cutoff-bounded watermark would hide a
    # local post-cutoff event that the cells already carry.
    parent_manifest.cutoff = target.cutoff
    parent_manifest.baseline_generation_id = (
        int(parent_manifest.baseline_generation_id)
        if parent_manifest.baseline_generation_id is not None
        else int(parent.id)
    )
    db.flush()
    return CompactProvenanceHandoffResult(
        parent_generation_id=int(parent.id),
        target_generation_id=int(target.id),
        future_supply_rows=0,
        custody_rows=len(current_rows),
        custody_event_watermark=watermark,
        future_supply_idempotent=False,
        custody_idempotent=False,
    )


def handoff_current_physical_refresh_provenance(
    db: Session,
    *,
    parent_generation_id: int,
    target_generation_id: int,
    include_future_supply: bool = True,
) -> CompactProvenanceHandoffResult:
    """Handoff both compact owners in one caller-owned transaction.

    ``include_future_supply=False`` is for a publisher which captured its own
    supplier contour for this target: that generation publishes the compact
    future-supply owner from its own capture, so rebinding the parent's rows
    as technical provenance would be a second writer of the same owner.
    """

    future = (
        handoff_current_future_supply_provenance(
            db,
            parent_generation_id=int(parent_generation_id),
            target_generation_id=int(target_generation_id),
        )
        if include_future_supply
        else CompactProvenanceHandoffResult(
            parent_generation_id=int(parent_generation_id),
            target_generation_id=int(target_generation_id),
            future_supply_rows=0,
            custody_rows=0,
            custody_event_watermark=0,
            future_supply_idempotent=False,
            custody_idempotent=False,
        )
    )
    custody = handoff_current_material_custody_provenance(
        db,
        parent_generation_id=int(parent_generation_id),
        target_generation_id=int(target_generation_id),
    )
    return CompactProvenanceHandoffResult(
        parent_generation_id=int(parent_generation_id),
        target_generation_id=int(target_generation_id),
        future_supply_rows=future.future_supply_rows,
        custody_rows=custody.custody_rows,
        custody_event_watermark=custody.custody_event_watermark,
        future_supply_idempotent=future.future_supply_idempotent,
        custody_idempotent=custody.custody_idempotent,
    )

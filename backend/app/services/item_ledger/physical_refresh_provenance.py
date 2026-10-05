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
from sqlalchemy import func, inspect
from sqlalchemy.orm import Session

from app import models
from app.services.production_material_custody_events import _custody_event_idempotency_key
from app.services.production_material_custody_projection import (
    MaterialCustodySnapshotUnavailable,
    _same_1c_timestamp,
    _visible_source_sle_for_event,
    replay_bounded_material_custody_cells,
)
from .physical_visibility import PhysicalVisibilityError, visible_sle_query


class PhysicalRefreshProvenanceUnavailable(ValueError):
    """The current compact owner cannot be safely handed to a target."""


def physical_batch_has_recorder(
    marks: dict, *, recorder_type: str, recorder_ref: str,
) -> bool:
    """Prove membership in a single-recorder or shared historical batch.

    Window imports share a batch across recorders. Their scalar recorder
    fields retain only the last pull, while the completed recorder manifest
    identifies every member. A malformed manifest cannot use scalar fallback.
    """
    if "recorders" in marks:
        manifest = marks["recorders"]
        return isinstance(manifest, list) and any(
            isinstance(row, dict)
            and row.get("recorder_type") == recorder_type
            and row.get("recorder_ref") == recorder_ref
            and row.get("status") in {"done", "empty"}
            for row in manifest
        )
    return (
        marks.get("recorder_type") == recorder_type
        and marks.get("recorder_ref") == recorder_ref
    )


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


def canonical_issue_backfill_source_ids(
    db: Session,
    *,
    events: list[models.ProductionMaterialCustodyEvent],
    physical_import_batch_id: int,
    target_cutoff: datetime,
    allowed_sle_ids: set[int] | None = None,
) -> tuple[int, ...]:
    """Prove the one local event emitted by the transfer-recorder projector.

    This is deliberately narrower than a general local-event exception.  The
    opening must name the exact exported material issue, its line and source
    warehouse, and equal all currently visible outbound physical movement for
    that component/recorder.  The canonical idempotency key binds its persisted
    revision; the physical witness must be in the caller's bounded manifest
    when one is supplied by the current publisher.
    """
    witnesses: list[int] = []
    for event in events:
        if event.source_sle_id is not None:
            continue
        if (
            event.source_kind != "issue_created"
            or event.location_kind != "transit"
            or not event.issue_id
            or not event.source_ref2c
            or not event.document_line_no
            or Decimal(str(event.delta_qty or 0)) <= 0
        ):
            raise PhysicalRefreshProvenanceUnavailable(
                f"custody tail contains a non-physical event (event_id={event.id})"
            )
        issue = db.get(models.ProductionMaterialIssue, int(event.issue_id))
        try:
            line_id = int(event.document_line_no)
        except (TypeError, ValueError):
            line_id = -1
        line = db.get(models.ProductionMaterialIssueLine, line_id)
        link = (
            db.query(models.SyncLink)
            .filter(
                models.SyncLink.source_system == "PRODPLAN",
                models.SyncLink.source_doctype == "material_issue",
                models.SyncLink.source_id == int(event.issue_id),
                models.SyncLink.target_entity == "Document_ПеремещениеЗапасов",
            )
            .order_by(models.SyncLink.link_id.desc())
            .first()
        )
        if (
            issue is None or line is None or link is None
            or int(line.issue_id) != int(issue.issue_id)
            or str(issue.direction or "") != "issue"
            or int(issue.product_id) != int(event.product_id)
            or int(line.component_item_id) != int(event.component_item_id)
            or str(link.target_ref_key or "") != str(event.source_ref2c)
            or str(issue.source_warehouse_ref1c or "") != str(event.source_ref1c or "")
            or str(issue.document_number or "") != str(event.document_number or "")
        ):
            raise PhysicalRefreshProvenanceUnavailable(
                f"custody issue backfill has foreign issue/link/line (event_id={event.id})"
            )
        revision = int(line.custody_event_revision or 0)
        if revision <= 0 or not any(
            _custody_event_idempotency_key(
                issue_id=int(issue.issue_id), line_id=int(line.line_id),
                revision=value, source_kind="issue_created",
                location_kind="transit", warehouse_ref1c=str(event.warehouse_ref1c),
                delta_qty=float(event.delta_qty), source_sle_id=None,
            ) == str(event.idempotency_key)
            for value in range(1, revision + 1)
        ):
            raise PhysicalRefreshProvenanceUnavailable(
                f"custody issue backfill idempotency key is invalid (event_id={event.id})"
            )
        try:
            outbound = visible_sle_query(
                db, physical_import_batch_id=int(physical_import_batch_id),
                cutoff=target_cutoff,
            ).filter(
                models.StockLedgerEntry.recorder_type == "Document_ПеремещениеЗапасов",
                models.StockLedgerEntry.recorder_ref == str(event.source_ref2c),
                models.StockLedgerEntry.item_id == int(event.component_item_id),
                models.StockLedgerEntry.warehouse_ref1c == str(event.warehouse_ref1c),
                models.StockLedgerEntry.movement_kind == "transfer_out",
            ).all()
        except PhysicalVisibilityError as exc:
            raise PhysicalRefreshProvenanceUnavailable(
                f"custody issue backfill has no complete physical boundary (event_id={event.id})"
            ) from exc
        if (
            not outbound
            or sum((-Decimal(str(row.qty)) for row in outbound), Decimal("0"))
            != Decimal(str(event.delta_qty))
            or not all(_same_1c_timestamp(row.posting_at, event.effective_at)
                       for row in outbound)
            or (allowed_sle_ids is not None
                and not {int(row.id) for row in outbound}.issubset(allowed_sle_ids))
        ):
            raise PhysicalRefreshProvenanceUnavailable(
                f"custody issue backfill has no exact bounded transfer-out (event_id={event.id})"
            )
        for row in outbound:
            batch = db.get(models.PhysicalImportBatch, int(row.ingest_batch_id))
            marks = dict(batch.source_watermarks or {}) if batch is not None else {}
            if (
                batch is None or batch.status != "completed" or not batch.source_complete
                or marks.get("source") != "AccumulationRegister_ЗапасыНаСкладах"
                or not physical_batch_has_recorder(
                    marks, recorder_type=row.recorder_type, recorder_ref=row.recorder_ref,
                )
                or Decimal(str(row.qty)) >= 0
                or (batch.cutoff is not None
                    and _ordered_1c_timestamps(batch.cutoff, target_cutoff)[0]
                    > _ordered_1c_timestamps(batch.cutoff, target_cutoff)[1])
            ):
                raise PhysicalRefreshProvenanceUnavailable(
                    f"custody issue backfill has malformed physical batch (event_id={event.id})"
                )
            witnesses.append(int(row.id))
    return tuple(dict.fromkeys(witnesses))


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
    caller's persisted SLE ids are accepted.  The canonical issue-opening
    backfill is admitted only with an exact visible transfer-out witness in
    those ids.  Other local events and unrelated tails fail closed.

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
    if any(
        event.source_sle_id is not None and int(event.source_sle_id) not in explicit
        for event in tail
    ):
        raise PhysicalRefreshProvenanceUnavailable(
            "custody event tail is not covered by the bounded physical SLE manifest"
        )
    canonical_issue_backfill_source_ids(
        db, events=tail,
        physical_import_batch_id=int(target.physical_import_batch_id),
        target_cutoff=target.cutoff,
        allowed_sle_ids=explicit,
    )

    parent_batch = int(parent.physical_import_batch_id)
    target_batch = int(target.physical_import_batch_id)
    edges = db.query(models.StockLedgerFactSupersession).filter(
        models.StockLedgerFactSupersession.import_batch_id > parent_batch,
        models.StockLedgerFactSupersession.import_batch_id <= target_batch,
    ).order_by(models.StockLedgerFactSupersession.id).all()
    edge_by_old: dict[int, models.StockLedgerFactSupersession] = {}
    for edge in edges:
        old_id = int(edge.old_sle_id)
        if old_id in edge_by_old:
            raise PhysicalRefreshProvenanceUnavailable(
                f"custody physical revision has two successors (old_sle_id={old_id})"
            )
        edge_by_old[old_id] = edge
    parent_event_rows = db.query(models.ProductionMaterialCustodyEvent).filter(
        models.ProductionMaterialCustodyEvent.id <= previous_watermark,
        models.ProductionMaterialCustodyEvent.source_sle_id.in_(list(edge_by_old)),
    ).all() if edge_by_old else []
    parent_visible_ids = {
        int(row.id) for row in visible_sle_query(
            db, physical_import_batch_id=parent_batch, cutoff=parent.cutoff,
        ).filter(models.StockLedgerEntry.id.in_(
            [int(event.source_sle_id) for event in parent_event_rows]
        )).all()
    } if parent_event_rows else set()
    corrected_events = [
        event for event in parent_event_rows
        if int(event.source_sle_id) in parent_visible_ids
        and _ordered_1c_timestamps(event.effective_at, parent.cutoff)[0]
        <= _ordered_1c_timestamps(event.effective_at, parent.cutoff)[1]
    ]
    correction_keys = {
        (int(event.product_id), int(event.component_item_id),
         str(event.location_kind or ""), str(event.warehouse_ref1c or ""))
        for event in corrected_events
    }
    connected_ids = {int(event.source_sle_id) for event in corrected_events}
    while True:
        successors = {
            int(edge_by_old[old_id].new_sle_id)
            for old_id in connected_ids if old_id in edge_by_old
            and edge_by_old[old_id].new_sle_id is not None
        }
        if successors <= connected_ids:
            break
        connected_ids.update(successors)
    if corrected_events and not connected_ids <= explicit:
        raise PhysicalRefreshProvenanceUnavailable(
            "custody correction chain is absent from the bounded SLE manifest"
        )
    # A replacement transfer must have a persisted edge from the accepted
    # recorder line. Without it, the new event could be appended to the old
    # compact quantity and remain nonnegative while silently counting both.
    tail_source_ids = {
        int(event.source_sle_id) for event in tail if event.source_sle_id is not None
    }
    tail_sources = db.query(models.StockLedgerEntry).filter(
        models.StockLedgerEntry.id.in_(tail_source_ids)
    ).all() if tail_source_ids else []
    new_identities = {
        str(row.business_identity)
        for row in tail_sources
        if int(row.ingest_batch_id) > parent_batch and row.business_identity
    }
    if new_identities:
        candidates = db.query(
            models.ProductionMaterialCustodyEvent,
            models.StockLedgerEntry,
        ).join(
            models.StockLedgerEntry,
            models.StockLedgerEntry.id
            == models.ProductionMaterialCustodyEvent.source_sle_id,
        ).filter(
            models.ProductionMaterialCustodyEvent.id <= previous_watermark,
            models.StockLedgerEntry.ingest_batch_id <= parent_batch,
            models.StockLedgerEntry.business_identity.in_(new_identities),
        ).all()
        candidate_ids = {int(old.id) for _, old in candidates}
        accepted_ids = {
            int(row.id) for row in visible_sle_query(
                db, physical_import_batch_id=parent_batch, cutoff=parent.cutoff,
            ).filter(models.StockLedgerEntry.id.in_(candidate_ids)).all()
        } if candidate_ids else set()
        for event, old in candidates:
            if int(old.id) in accepted_ids and int(old.id) not in connected_ids:
                raise PhysicalRefreshProvenanceUnavailable(
                    f"custody recorder correction lacks a bounded supersession "
                    f"(old_sle_id={int(old.id)}, event_id={int(event.id)})"
                )
    for event in tail:
        if event.source_sle_id is None:
            continue
        source_id = int(event.source_sle_id)
        if source_id in connected_ids:
            correction_keys.add((
                int(event.product_id), int(event.component_item_id),
                str(event.location_kind or ""), str(event.warehouse_ref1c or ""),
            ))
        elif source_id in edge_by_old:
            # A newly imported A may already have been replaced by B or NULL
            # inside the same physical import interval.
            continue
    if any(
        _ordered_1c_timestamps(event.effective_at, target.cutoff)[0]
        > _ordered_1c_timestamps(event.effective_at, target.cutoff)[1]
        for event in tail
    ):
        raise PhysicalRefreshProvenanceUnavailable(
            "custody event tail extends beyond the target cutoff"
        )
    visible_tail_ids = {
        int(row.id) for row in visible_sle_query(
            db, physical_import_batch_id=target_batch, cutoff=target.cutoff,
        ).filter(models.StockLedgerEntry.id.in_(
            [int(event.source_sle_id) for event in tail if event.source_sle_id is not None]
        )).all()
    } if tail else set()
    terminal_correction_ids = connected_ids - set(edge_by_old)
    terminal_visible_ids = {
        int(row.id) for row in visible_sle_query(
            db, physical_import_batch_id=target_batch, cutoff=target.cutoff,
        ).filter(models.StockLedgerEntry.id.in_(terminal_correction_ids)).all()
    } if terminal_correction_ids else set()
    tail_event_source_ids = {
        int(event.source_sle_id) for event in tail if event.source_sle_id is not None
    }
    # Exact recorder reimports preserve the original custody event and its
    # stable physical identity. A new technical SLE id alone requires no audit
    # churn; the canonical resolver proves that every fact field is unchanged.
    reimport_event_source_ids = {
        int(source.id)
        for event in corrected_events
        if (source := _visible_source_sle_for_event(
            db, event=event, physical_import_batch_id=target_batch,
            cutoff=target.cutoff,
        )) is not None
    }
    missing_terminal_events = (
        terminal_visible_ids - tail_event_source_ids - reimport_event_source_ids
    )
    if missing_terminal_events:
        raise PhysicalRefreshProvenanceUnavailable(
            "custody correction has no event for target-visible transfer "
            f"(sle_ids={sorted(missing_terminal_events)[:8]})"
        )
    for event in tail:
        if event.source_sle_id is not None and int(event.source_sle_id) not in visible_tail_ids:
            if int(event.source_sle_id) not in edge_by_old:
                raise PhysicalRefreshProvenanceUnavailable(
                    f"custody event has invisible source without a bounded supersession "
                    f"(event_id={int(event.id)}, sle_id={int(event.source_sle_id)})"
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
    if correction_keys:
        earliest = min(
            event.effective_at.replace(tzinfo=None)
            for event in (*corrected_events, *(
                event for event in tail
                if event.source_sle_id is not None
                and int(event.source_sle_id) in connected_ids
            ))
        )
        try:
            baseline_id, parent_cells, target_cells, target_watermark = (
                replay_bounded_material_custody_cells(
                    db, parent=parent, target=target, keys=correction_keys,
                    earliest_changed_at=earliest,
                )
            )
        except MaterialCustodySnapshotUnavailable as exc:
            raise PhysicalRefreshProvenanceUnavailable(
                f"custody correction has no canonical bounded replay basis "
                f"(old_sle_ids={sorted(int(event.source_sle_id) for event in corrected_events)})"
            ) from exc
        for key in correction_keys:
            stored = Decimal(str(rows_by_key[key].reserved_qty)) if key in rows_by_key else Decimal(0)
            expected = Decimal(str(parent_cells.get(key, 0)))
            if stored.quantize(Decimal("0.001")) != expected.quantize(Decimal("0.001")):
                raise PhysicalRefreshProvenanceUnavailable(
                    f"custody correction parent basis differs from compact owner "
                    f"(key={key}, baseline_generation_id={baseline_id}, "
                    f"stored={stored}, replayed={expected})"
                )
            result = Decimal(str(target_cells.get(key, 0)))
            row = rows_by_key.get(key)
            if result <= 0:
                if row is not None:
                    db.delete(row)
                    rows_by_key.pop(key, None)
            elif row is not None:
                row.reserved_qty = result
            else:
                row = models.ProductionMaterialCustodyProjection(
                    ledger_generation_id=int(parent.id), product_id=key[0],
                    component_item_id=key[1], location_kind=key[2],
                    warehouse_ref1c=key[3], reserved_qty=result,
                    source_event_high_watermark_id=target_watermark,
                    is_current=True,
                )
                db.add(row)
                rows_by_key[key] = row
    for event in tail:
        key = (
            int(event.product_id),
            int(event.component_item_id),
            str(event.location_kind or ""),
            str(event.warehouse_ref1c or ""),
        )
        if key in correction_keys or (
            event.source_sle_id is not None
            and int(event.source_sle_id) not in visible_tail_ids
        ):
            continue
        row = rows_by_key.get(key)
        current_qty = Decimal(str(row.reserved_qty or 0)) if row is not None else Decimal("0")
        next_qty = current_qty + Decimal(str(event.delta_qty or 0))
        if next_qty < 0:
            raise PhysicalRefreshProvenanceUnavailable(
                "bounded custody event would make compact current quantity negative"
            )
        if row is not None and next_qty == 0:
            # A canonical opening and its transfer-out can cancel in this
            # same tail.  The new compact cell has not been INSERTed yet.
            if inspect(row).pending:
                db.expunge(row)
            else:
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

    watermark = int(tail[-1].id) if tail else previous_watermark
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

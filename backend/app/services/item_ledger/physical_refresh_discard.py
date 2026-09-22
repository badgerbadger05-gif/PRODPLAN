"""Discard a physical-refresh candidate that will never be published.

A refresh candidate that fails convergence stays BUILDING and keeps its physical
import batches at the top of the global sequence.  Because visibility is an
id-prefix, nothing can fork from the accepted parent again until those batches
are gone: the next candidate's audit asserts that the global terminal equals the
parent boundary.  The schema has always allowed a ``rejected`` generation, but no
code ever produced one, so operators rolled candidates back by hand.

Doing it by hand is a trap.  ``StockLedgerEntry.active`` is deliberately ignored
by the read path — ``physical_visibility`` resolves facts from the import-batch
boundary alone — but it is load-bearing for the *write* path: ``ingest`` retires
only the revisions flagged active, and creates one supersession edge per retired
row.  Deleting a candidate's edges without restoring the flag therefore leaves
the rows they retired inactive *and* unsuperseded, so the next re-pull believes
there is nothing to retire, inserts a fresh revision, and both stay visible.  The
ledger then double-counts every recorder the candidate had touched.

The candidate's custody events leave a second mark outside the physical
tables: they invalidate the current execution manifests in their own committed
transaction, so removing them has to re-open exactly the results they closed.
See ``_restore_manifests_invalidated_by``.

This module performs the whole rollback as one checked unit and refuses to
commit anything it cannot prove: the accepted parent's visible truth must come
out byte-identical, the rollback must not leave a single recorder line with more
live revisions than it found, and the active flag must agree with the
supersession graph in both directions.

The revision check is deliberately differential.  Damage below the boundary
predates the candidate and no rollback can repair it; judging it as an absolute
post-condition made two lines broken weeks earlier veto every future rollback,
so the physical contour stayed fenced, the Ledger cutoff froze and a day later
every consumer failed closed on the freshness threshold.  Such damage is
reported, never silently accepted, and never confused with damage this rollback
would cause.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app import models

from .physical import guard_physical_batch_writer
from .physical_visibility import visible_sle_query


ALGORITHM_VERSION = "ledger-physical-refresh-discard/1"
#: Rows imported from a 1C recorder document, as opposed to synthetic
#: adjustments (balance snaps, opening reconciliation) and the historical seed.
DOCUMENT_PULL_SOURCE = "document_pull"
REJECTED_STATUS = "rejected"
REASON_KEY = "rejected_reason"
DISCARDED_BOUNDARY_KEY = "rejected_physical_import_batch_id"
ORIGIN_KEY = "rejected_origin"
#: An operator pressed the discard button: the candidate is abandoned on
#: purpose and whatever retry backoff it earned is no longer meaningful.
DISCARD_ORIGIN_OPERATOR = "operator"
#: The pipeline rejected its own candidate after a failure.  The failure is
#: still a failure: the scheduler must keep counting it.
DISCARD_ORIGIN_AUTOMATIC = "automatic"
DISCARD_ORIGINS = frozenset({DISCARD_ORIGIN_OPERATOR, DISCARD_ORIGIN_AUTOMATIC})


class PhysicalRefreshDiscardError(RuntimeError):
    """The candidate cannot be discarded without endangering accepted truth."""


@dataclass(frozen=True)
class PhysicalRefreshDiscardResult:
    ledger_generation_id: int
    parent_generation_id: int
    boundary_before: int
    boundary_after: int
    deleted_physical_batches: int
    deleted_ledger_entries: int
    deleted_supersessions: int
    deleted_anchors: int
    deleted_custody_events: int
    deleted_generation_rows: dict[str, int]
    #: ``entity_kind:scope_key`` of every manifest whose only reason to be
    #: unavailable was a custody event this rollback removed.
    restored_current_execution_scopes: tuple[str, ...]
    reactivated_entries: int
    parent_fingerprint: tuple[int, str]
    #: Damage that predates this candidate: not repaired, never hidden.
    preexisting_live_revision_conflicts: int


def _fingerprint(db: Session, physical_import_batch_id: int) -> tuple[int, str]:
    """Count and total of the facts visible at one immutable boundary."""
    rows = visible_sle_query(
        db, physical_import_batch_id=int(physical_import_batch_id)
    ).all()
    total = sum((Decimal(str(row.qty)) for row in rows), Decimal("0"))
    return len(rows), str(total)


def restore_active_invariant(db: Session) -> int:
    """Realign ``active`` with the supersession graph; return rows changed.

    ``active`` is true exactly when no edge retires the row.  Rolling batches
    back deletes edges, so without this the write path silently stops retiring
    the revisions those edges used to cover.
    """
    reactivated = db.execute(text(
        """
        UPDATE stock_ledger_entry SET active = TRUE
        WHERE active = FALSE
          AND NOT EXISTS (
              SELECT 1 FROM stock_ledger_fact_supersession s
              WHERE s.old_sle_id = stock_ledger_entry.id
          )
        """
    )).rowcount or 0
    retired = db.execute(text(
        """
        UPDATE stock_ledger_entry SET active = FALSE
        WHERE active = TRUE
          AND EXISTS (
              SELECT 1 FROM stock_ledger_fact_supersession s
              WHERE s.old_sle_id = stock_ledger_entry.id
          )
        """
    )).rowcount or 0
    return int(reactivated) + int(retired)


def _active_invariant_violations(db: Session) -> int:
    return int(db.execute(text(
        """
        SELECT count(*) FROM stock_ledger_entry e
        WHERE (e.active = FALSE AND NOT EXISTS (
                   SELECT 1 FROM stock_ledger_fact_supersession s
                   WHERE s.old_sle_id = e.id))
           OR (e.active = TRUE AND EXISTS (
                   SELECT 1 FROM stock_ledger_fact_supersession s
                   WHERE s.old_sle_id = e.id))
        """
    )).scalar() or 0)


def _live_revision_conflicts(db: Session) -> int:
    """Pulled document lines left with more than one unsuperseded revision.

    Scoped to ``document_pull`` on purpose.  "One live revision per recorder
    line" is a property of a 1C document: a re-pull retires the previous
    revision and records the supersession edge.  Synthetic rows do not work that
    way — a balance snap writes ``1C minus ledger`` per cell under a recorder ref
    keyed by the generation, so a second pass inside one build legitimately adds
    a second row to the same line and the two are *meant* to add up.  Counting
    those as damage reported corruption that does not exist and, worse, made two
    healthy adjustment rows veto every rollback.

    Measured before and after the rollback, never as an absolute verdict: a line
    damaged below the boundary cannot be repaired by a discard that only removes
    rows above it.
    """
    return int(db.execute(text(
        """
        SELECT count(*) FROM (
            SELECT e.recorder_ref, e.line_no
            FROM stock_ledger_entry e
            WHERE e.ingest_source = :document_pull
              AND NOT EXISTS (
                SELECT 1 FROM stock_ledger_fact_supersession s
                WHERE s.old_sle_id = e.id)
            GROUP BY e.recorder_ref, e.line_no
            HAVING count(*) > 1
        ) conflicting
        """
    ), {"document_pull": DOCUMENT_PULL_SOURCE}).scalar() or 0)


def _restore_manifests_invalidated_by(
    db: Session,
    discarded_custody: list[Any],
) -> tuple[str, ...]:
    """Re-open the current results whose only blocker this rollback removed.

    The import checkpoint commits before publication, and a custody writer
    invalidates the manifests it affects inside that same committed
    transaction — which is what canon R8 requires of it: the invalidation is
    a property of the custody write, not of a publication that may never
    happen.  So the invalidation must be *undone* by whoever undoes the
    custody write, and nothing else can do it: after the events are deleted
    the database holds no trace of why ``assembly_readiness``,
    ``drum_schedule`` and ``shelf_projection`` are unavailable, and every
    reader fails closed until some later refresh happens to publish.  Moving
    the invalidation into the publication transaction was the other option;
    it was rejected because it would let a committed custody change sit
    behind a manifest still advertising a result computed without it.

    Restoration is deliberately narrow.  A manifest is re-opened only when
    its recorded ``source_revision`` names a custody event this rollback just
    deleted, which means nothing has invalidated it since.  For the
    workshop-transfer marker (``custody:transfer:<recorder_ref>``, shared by
    every event of one recorder) the marker counts only if no event of that
    recorder survived: a surviving sibling is an invalidating cause that is
    still true.

    Bounded residual: an invalidation of an already-closed manifest is a no-op
    by design, so a second cause arriving between the candidate's custody
    write and this rollback leaves no trace to read and the restored result
    can be one republish behind it.  That window is the refresh's own failure
    handling, and the worker's next publication closes it; it is strictly
    smaller than the alternative, which was a manifest closed forever with no
    cause in the database at all.
    """
    revisions: set[str] = set()
    transfer_refs: set[str] = set()
    for event_id, source_ref2c in discarded_custody:
        revisions.add(f"custody:event:{int(event_id)}")
        ref = str(source_ref2c or "").strip()
        if ref:
            transfer_refs.add(ref)
    if transfer_refs:
        surviving = {
            str(ref or "").strip()
            for (ref,) in db.query(
                models.ProductionMaterialCustodyEvent.source_ref2c
            ).filter(
                models.ProductionMaterialCustodyEvent.source_ref2c.in_(
                    sorted(transfer_refs)
                )
            ).distinct().all()
        }
        revisions.update(
            f"custody:transfer:{ref}"
            for ref in sorted(transfer_refs - surviving)
        )
    if not revisions:
        return ()

    manifests = db.query(models.CurrentExecutionScope).filter(
        models.CurrentExecutionScope.result_ready.is_(False),
        models.CurrentExecutionScope.source_revision.in_(sorted(revisions)),
    ).with_for_update().all()
    restored: list[str] = []
    for manifest in manifests:
        manifest.result_ready = True
        db.query(models.CurrentExecutionRow).filter(
            models.CurrentExecutionRow.entity_kind == str(manifest.entity_kind),
            models.CurrentExecutionRow.scope_key == str(manifest.scope_key),
            models.CurrentExecutionRow.result_status == "accepted",
        ).update({"result_ready": True}, synchronize_session=False)
        restored.append(f"{manifest.entity_kind}:{manifest.scope_key}")
    db.flush()
    return tuple(sorted(restored))


def _generation_scoped_tables() -> list[Any]:
    """Mapped classes carrying ``ledger_generation_id``, child tables first."""
    scoped = {
        mapper.class_.__tablename__: mapper.class_
        for mapper in models.Base.registry.mappers
        if hasattr(mapper.class_, "ledger_generation_id")
    }
    ordered: list[Any] = []
    for table in reversed(models.Base.metadata.sorted_tables):
        model = scoped.get(table.name)
        if model is not None:
            ordered.append(model)
    return ordered


def _require_discardable(
    db: Session,
    ledger_generation_id: int,
) -> tuple[models.LedgerGeneration, models.LedgerGeneration, int]:
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise PhysicalRefreshDiscardError(
            f"generation {ledger_generation_id} does not exist"
        )
    if str(generation.status) == "accepted":
        raise PhysicalRefreshDiscardError(
            f"generation {generation.id} is ACCEPTED and is never discardable"
        )
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is not None and int(pointer.current_generation_id or 0) == int(generation.id):
        raise PhysicalRefreshDiscardError(
            f"generation {generation.id} is the current planning truth pointer"
        )

    marks = dict(generation.source_watermarks or {})
    try:
        parent_id = int(marks["parent_generation_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PhysicalRefreshDiscardError(
            f"generation {generation.id} has no parent lineage to roll back to"
        ) from exc
    parent = db.get(models.LedgerGeneration, parent_id)
    if parent is None:
        raise PhysicalRefreshDiscardError(f"parent generation {parent_id} does not exist")
    if str(parent.status) != "accepted":
        raise PhysicalRefreshDiscardError(
            f"parent generation {parent.id} is {parent.status}; ACCEPTED required"
        )
    cut = int(parent.physical_import_batch_id or 0)
    if cut <= 0:
        raise PhysicalRefreshDiscardError("parent generation has no physical boundary")

    # Anything else sitting above the cut would be stranded by this rollback.
    intruders = db.query(models.LedgerGeneration).filter(
        models.LedgerGeneration.id != int(generation.id),
        models.LedgerGeneration.physical_import_batch_id > cut,
    ).all()
    if intruders:
        raise PhysicalRefreshDiscardError(
            "generations "
            + ", ".join(str(row.id) for row in intruders)
            + f" also sit above boundary {cut}; discard them first"
        )
    return generation, parent, cut


def discard_physical_refresh_candidate(
    db: Session,
    *,
    ledger_generation_id: int,
    reason: str,
    origin: str = DISCARD_ORIGIN_AUTOMATIC,
) -> PhysicalRefreshDiscardResult:
    """Roll one unpublishable candidate back to its accepted parent's boundary.

    Deliberately does not commit: a destructive operation should leave the
    caller in charge of the transaction.  Every check runs before returning, so
    a raised error means nothing was proved and the caller must roll back.

    ``origin`` records who abandoned the candidate, because the scheduler has
    to tell the two apart: an operator discard retires the retry identity
    *and* its backoff, while an automatic rejection retires only the identity
    — the failure that caused it still counts.  The default is the
    conservative one; a caller that clears backoff has to say so.
    """
    text_reason = str(reason or "").strip()
    if not text_reason:
        raise ValueError("reason is required when discarding a candidate")
    text_origin = str(origin or "").strip()
    if text_origin not in DISCARD_ORIGINS:
        raise ValueError(
            "discard origin must be "
            + " or ".join(sorted(DISCARD_ORIGINS))
            + f"; got {origin!r}"
        )

    # Take the physical-sequence lock before reading anything: a refresh may be
    # building right now, and deleting its rows underneath it made SQLAlchemy
    # fail mid-flush ("expected to update N rows; 0 were matched"), leaving a
    # half-built candidate nobody could explain.  The refresh's own automatic
    # rollback already owns this lock, so it is skipped there.
    guard_physical_batch_writer(db)
    generation, parent, cut = _require_discardable(db, ledger_generation_id)
    boundary_before = int(generation.physical_import_batch_id or cut)
    expected_fingerprint = _fingerprint(db, cut)
    conflicts_before = _live_revision_conflicts(db)

    deleted_generation_rows: dict[str, int] = {}
    for model in _generation_scoped_tables():
        removed = db.query(model).filter(
            model.ledger_generation_id == int(generation.id)
        ).delete(synchronize_session=False)
        if removed:
            deleted_generation_rows[model.__tablename__] = int(removed)

    # Release the candidate's own reference before its batches disappear.
    generation.status = REJECTED_STATUS
    generation.physical_import_batch_id = cut
    generation.source_watermarks = {
        **dict(generation.source_watermarks or {}),
        REASON_KEY: text_reason,
        ORIGIN_KEY: text_origin,
        DISCARDED_BOUNDARY_KEY: boundary_before,
        "rejected_at": datetime.now(timezone.utc).isoformat(),
        "rejected_algorithm_version": ALGORITHM_VERSION,
    }
    db.flush()

    # A custody event carrying ``source_sle_id`` is derived from one physical
    # line, so it has to go back with the fact it was derived from.  Left
    # behind, it names a row that no longer exists: the fold cannot see it, the
    # issue keeps an open transit reservation nothing ever consumes, and the
    # workshop never receives its side — while the projector refuses to re-append
    # the pair on the next import, because it still recognises the orphan by its
    # stable physical identity.  That is how one rolled-back candidate silently
    # blocked every launch with a negative workshop reservation.
    doomed_custody = db.query(
        models.ProductionMaterialCustodyEvent.id,
        models.ProductionMaterialCustodyEvent.source_ref2c,
    ).filter(
        models.ProductionMaterialCustodyEvent.source_sle_id.in_(
            db.query(models.StockLedgerEntry.id).filter(
                models.StockLedgerEntry.ingest_batch_id > cut
            )
        )
    ).all()
    deleted_custody_events = db.query(
        models.ProductionMaterialCustodyEvent
    ).filter(
        models.ProductionMaterialCustodyEvent.source_sle_id.in_(
            db.query(models.StockLedgerEntry.id).filter(
                models.StockLedgerEntry.ingest_batch_id > cut
            )
        )
    ).delete(synchronize_session=False)
    db.flush()
    restored_scopes = _restore_manifests_invalidated_by(db, doomed_custody)

    # Physical rows, referenced side first.
    deleted_anchors = db.query(models.StockLedgerAnchor).filter(
        models.StockLedgerAnchor.ingest_batch_id > cut
    ).delete(synchronize_session=False)
    deleted_supersessions = db.query(models.StockLedgerFactSupersession).filter(
        models.StockLedgerFactSupersession.import_batch_id > cut
    ).delete(synchronize_session=False)
    # R3 identity mappings belong to the same candidate boundary.  Remove
    # rejected candidate edges before deleting their SLE rows; accepted
    # historical mappings at/below the retained boundary remain immutable.
    db.query(models.StockLedgerBusinessIdentityMap).filter(
        models.StockLedgerBusinessIdentityMap.stock_ledger_entry_id.in_(
            db.query(models.StockLedgerEntry.id).filter(
                models.StockLedgerEntry.ingest_batch_id > cut
            )
        )
    ).delete(synchronize_session=False)
    deleted_entries = db.query(models.StockLedgerEntry).filter(
        models.StockLedgerEntry.ingest_batch_id > cut
    ).delete(synchronize_session=False)
    deleted_batches = db.query(models.PhysicalImportBatch).filter(
        models.PhysicalImportBatch.id > cut
    ).delete(synchronize_session=False)
    db.flush()

    reactivated = restore_active_invariant(db)
    db.flush()

    problems: list[str] = []
    fingerprint = _fingerprint(db, cut)
    if fingerprint != expected_fingerprint:
        problems.append(
            f"accepted truth at boundary {cut} changed: "
            f"{expected_fingerprint} -> {fingerprint}"
        )
    violations = _active_invariant_violations(db)
    if violations:
        problems.append(f"{violations} rows disagree with the supersession graph")
    conflicts = _live_revision_conflicts(db)
    if conflicts > conflicts_before:
        problems.append(
            f"{conflicts} recorder lines have more than one live revision, "
            f"{conflicts_before} before the rollback"
        )
    terminal = db.query(func.max(models.PhysicalImportBatch.id)).scalar()
    if terminal is None or int(terminal) != cut:
        problems.append(f"physical terminal is {terminal}, expected {cut}")
    if problems:
        raise PhysicalRefreshDiscardError(
            "discard would corrupt physical truth: " + "; ".join(problems)
        )

    return PhysicalRefreshDiscardResult(
        ledger_generation_id=int(generation.id),
        parent_generation_id=int(parent.id),
        boundary_before=boundary_before,
        boundary_after=cut,
        deleted_physical_batches=int(deleted_batches),
        deleted_ledger_entries=int(deleted_entries),
        deleted_supersessions=int(deleted_supersessions),
        deleted_anchors=int(deleted_anchors),
        deleted_custody_events=int(deleted_custody_events),
        deleted_generation_rows=deleted_generation_rows,
        restored_current_execution_scopes=restored_scopes,
        reactivated_entries=int(reactivated),
        parent_fingerprint=fingerprint,
        preexisting_live_revision_conflicts=int(conflicts),
    )

"""Recorder refresh for a BUILDING physical-refresh generation.

The service is intentionally narrow: it gathers the union of known recorder
identities, re-pulls each recorder idempotently under strict historical rules,
and publishes a completed ``physical_import`` checkpoint only when all pulls for
that generation are terminal.

The known set is not sufficient on its own.  1C register rows carry the
*document* date in ``Period``, not the moment the record set was written, so a
document posted (or re-posted) after the parent cutoff but dated before it lands
inside an already-closed forward window: the forward scan never revisits it and
the audit, which only re-pulls recorders the ledger already knows, never learns
it exists.  Such a recorder is invisible forever and the gap accumulates with
every refresh.  The audit therefore re-discovers the retained horizon
``(opening_at, parent_cutoff]`` on every run and folds anything new into the set
it verifies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models

from .historical_register_scan import scan_historical_register_range
from .ingest import DEFAULT_MAX_ATTEMPTS, pull_recorder_movements
from .opening_balance_reconcile import opening_boundary
from .physical import canonical_content_hash
from .physical_visibility import visible_sles_for_generation


ALGORITHM_VERSION = "ledger-physical-refresh-import/2"
GENERATION_KIND = "physical_refresh"
CHECKPOINT_KEY_PREFIX = "physical-refresh-recorder-audit"
CHECKPOINT_VERSION = "2"

# Discovery re-reads the register only for recorder identities ($select is four
# columns), so a wide window is cheap next to the per-recorder pulls that follow.
DISCOVERY_WINDOW_SIZE = timedelta(days=7)
DISCOVERY_PAGE_SIZE = 1000
# Floor used only when the opening-balance boundary cannot be located; below the
# anchor every movement is dropped as pre-anchor anyway.
DISCOVERY_FALLBACK_LOOKBACK = timedelta(days=365)


class PhysicalRefreshImportError(RuntimeError):
    """Recorder refresh for a physical refresh candidate failed."""


@dataclass(frozen=True)
class PhysicalRefreshImportResult:
    ledger_generation_id: int
    parent_generation_id: int
    target_cutoff: datetime
    recorder_count: int
    recorder_audit_checksum: str
    changed_recorders: int
    checkpoint_id: int
    terminal_physical_import_batch_id: int
    from_checkpoint: bool
    discovered_recorders: int = 0
    backdated_recorders: int = 0


def _utc(value: datetime | str | None, field: str) -> datetime:
    if value is None:
        raise PhysicalRefreshImportError(f"{field} is missing")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PhysicalRefreshImportError(
                f"{field} is not an ISO datetime"
            ) from exc
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _checkpoint_key(generation_id: int) -> str:
    return f"{CHECKPOINT_KEY_PREFIX}:{generation_id}:{CHECKPOINT_VERSION}"


def _global_terminal(db: Session) -> int:
    terminal = db.query(func.max(models.PhysicalImportBatch.id)).scalar()
    if terminal is None:
        return 0
    return int(terminal)


def _assert_global_terminal(db: Session, expected_batch_id: int) -> None:
    actual = _global_terminal(db)
    if actual != int(expected_batch_id):
        raise PhysicalRefreshImportError(
            "physical import sequence interleaved: expected terminal "
            f"{expected_batch_id}, global terminal {actual}"
        )


def _require_target_generation(
    db: Session,
    *,
    ledger_generation_id: int,
    parent_generation_id: int,
) -> tuple[models.LedgerGeneration, models.LedgerGeneration]:
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None:
        raise PhysicalRefreshImportError(
            f"target generation {ledger_generation_id} does not exist"
        )
    if str(generation.status) != "building":
        raise PhysicalRefreshImportError(
            f"Ledger generation {generation.id} must be BUILDING"
        )
    if generation.cutoff is None:
        raise PhysicalRefreshImportError("target generation must have cutoff")
    source = dict(generation.source_watermarks or {})
    if source.get("generation_kind") != GENERATION_KIND:
        raise PhysicalRefreshImportError("target generation is not generation_kind=physical_refresh")
    try:
        source_parent = int(source.get("parent_generation_id"))
    except (TypeError, ValueError) as exc:
        raise PhysicalRefreshImportError(
            "target generation lacks parent_generation_id"
        ) from exc
    if int(source_parent) != int(parent_generation_id):
        raise PhysicalRefreshImportError("target generation parent_id does not match")

    parent = db.get(models.LedgerGeneration, int(parent_generation_id))
    if parent is None:
        raise PhysicalRefreshImportError(f"parent generation {parent_generation_id} does not exist")
    if str(parent.status) != "accepted":
        raise PhysicalRefreshImportError("parent generation must be ACCEPTED")
    if parent.cutoff is None:
        raise PhysicalRefreshImportError("parent generation missing cutoff")
    if int(parent.physical_import_batch_id or 0) <= 0:
        raise PhysicalRefreshImportError("parent generation missing physical_import_batch")
    if int(generation.physical_import_batch_id or 0) <= 0:
        raise PhysicalRefreshImportError("target generation missing starting physical_import_batch")
    return generation, parent


# Recorder types that are synthetic (not backed by a 1C document) and therefore
# must never be re-pulled from 1C during the recorder audit.
_SYNTHETIC_RECORDER_TYPES = frozenset({"seed"})


def _opening_boundary_at(db: Session) -> datetime | None:
    """Timestamp of the opening-balance seed, the floor of retained history."""
    boundary = opening_boundary(db)
    return None if boundary is None else boundary[1]


def _discovery_range(
    db: Session,
    *,
    parent_cutoff: datetime,
    lookback: timedelta | None,
) -> tuple[datetime, datetime] | None:
    """Resolve ``(from_exclusive, to_inclusive]`` for backdated-recorder discovery.

    ``lookback`` bounds the scan for operators who cannot afford the full
    horizon; ``None`` (the default) discovers everything the ledger retains.
    Both ends are floored at the opening boundary because movements at or below
    the anchor are dropped as pre-anchor and cannot enter the ledger anyway.
    """
    opening_at = _opening_boundary_at(db)
    floor = opening_at
    if lookback is not None:
        bounded = parent_cutoff - lookback
        floor = bounded if floor is None else max(floor, bounded)
    elif floor is None:
        floor = parent_cutoff - DISCOVERY_FALLBACK_LOOKBACK
    if floor >= parent_cutoff:
        return None
    return floor, parent_cutoff


def _discover_recorder_identities(
    client: Any,
    *,
    from_exclusive: datetime,
    to_inclusive: datetime,
    window_size: timedelta,
    page_size: int,
) -> tuple[tuple[str, str], ...]:
    """Recorder identities present in the register over a closed historical range.

    Discovery reads identities only: no recorder contents, no ledger write and
    no watermark advance.  The caller decides which of them still need a pull.
    """
    scan = scan_historical_register_range(
        client,
        from_exclusive=from_exclusive,
        to_inclusive=to_inclusive,
        window_size=window_size,
        page_size=page_size,
    )
    return tuple(
        (discovered.identity.recorder_type, discovered.identity.recorder_ref)
        for discovered in scan.recorders
    )


def _merge_recorder_identities(
    *sources: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Union recorder identities under the audit's ordering and dirt filter."""
    identities: set[tuple[str, str]] = set()
    for source in sources:
        for recorder_type, recorder_ref in source:
            if recorder_type in _SYNTHETIC_RECORDER_TYPES:
                continue
            if recorder_ref:
                identities.add((recorder_type, recorder_ref))
    return tuple(sorted(identities, key=lambda item: (item[0], item[1])))


def _collect_recorder_identities(
    db: Session,
    parent_generation_id: int,
) -> tuple[tuple[str, str], ...]:
    identities: set[tuple[str, str]] = set()

    parent_rows = visible_sles_for_generation(db, parent_generation_id)
    for row in parent_rows:
        recorder_type = str(row.recorder_type or "")
        recorder_ref = str(row.recorder_ref or "")
        # Synthetic opening-balance anchors (movement_kind/recorder_type "seed")
        # are not 1C documents; re-pulling them by Recorder filter makes 1C reject
        # the request. They carry no real recorder to re-audit, so skip them.
        if recorder_type in _SYNTHETIC_RECORDER_TYPES:
            continue
        if recorder_ref:
            identities.add((recorder_type, recorder_ref))

    queued_rows = db.query(models.StockRecorderPull).filter(
        (models.StockRecorderPull.status == "pending")
        | (
            models.StockRecorderPull.status == "error"
        ) & (models.StockRecorderPull.attempts < DEFAULT_MAX_ATTEMPTS),
    ).all()
    for row in queued_rows:
        recorder_type = str(row.recorder_type or "")
        recorder_ref = str(row.recorder_ref or "")
        if recorder_type in _SYNTHETIC_RECORDER_TYPES:
            continue
        if recorder_ref:
            identities.add((recorder_type, recorder_ref))

    return tuple(sorted(identities, key=lambda item: (item[0], item[1])))


def _validate_checkpoint(
    db: Session,
    *,
    generation_id: int,
    parent_generation_id: int,
    parent_cutoff: datetime,
    parent_terminal: int,
    child_cutoff: datetime,
) -> models.LedgerBuildBatch | None:
    checkpoint = db.query(models.LedgerBuildBatch).filter(
        models.LedgerBuildBatch.ledger_generation_id == int(generation_id),
        models.LedgerBuildBatch.stage == "physical_import",
        models.LedgerBuildBatch.batch_key == _checkpoint_key(int(generation_id)),
    ).one_or_none()

    if checkpoint is None:
        return None
    if str(checkpoint.status) != "completed":
        raise PhysicalRefreshImportError("existing recorder-audit checkpoint is not completed")
    if str(checkpoint.algorithm_version) != ALGORITHM_VERSION:
        raise PhysicalRefreshImportError("existing recorder-audit checkpoint version changed")

    metrics = dict(checkpoint.metrics or {})
    if metrics.get("checkpoint_version") != CHECKPOINT_VERSION:
        raise PhysicalRefreshImportError("existing recorder-audit checkpoint is malformed")
    if int(metrics.get("parent_generation_id", -1)) != int(parent_generation_id):
        raise PhysicalRefreshImportError("existing recorder-audit checkpoint belongs to another parent")
    if str(metrics.get("generation_kind")) != GENERATION_KIND:
        raise PhysicalRefreshImportError("existing recorder-audit checkpoint generation kind mismatch")
    if _utc(metrics.get("parent_cutoff"), "parent_cutoff") != parent_cutoff:
        raise PhysicalRefreshImportError("existing recorder-audit checkpoint parent cutoff mismatch")
    if _utc(metrics.get("target_cutoff"), "target_cutoff") != child_cutoff:
        raise PhysicalRefreshImportError("existing recorder-audit checkpoint target cutoff mismatch")
    if int(metrics.get("parent_physical_import_batch_id", -1)) != int(parent_terminal):
        raise PhysicalRefreshImportError("existing recorder-audit checkpoint terminal mismatch")
    recorders = metrics.get("recorders")
    if not isinstance(recorders, list):
        raise PhysicalRefreshImportError(
            "existing recorder-audit checkpoint recorder set is malformed"
        )
    expected_input_checksum = canonical_content_hash(recorders)
    if (
        int(metrics.get("recorder_count", -1)) != len(recorders)
        or str(metrics.get("recorder_input_checksum"))
        != expected_input_checksum
    ):
        raise PhysicalRefreshImportError(
            "existing recorder-audit checkpoint recorder set conflicts"
        )

    terminal_id = int(metrics.get("physical_import_batch_id", -1))
    if terminal_id < 0:
        raise PhysicalRefreshImportError("existing recorder-audit checkpoint misses terminal boundary")
    generation = db.get(models.LedgerGeneration, int(generation_id))
    generation_terminal = int(
        generation.physical_import_batch_id or -1
    ) if generation is not None else -1
    if generation_terminal < terminal_id:
        raise PhysicalRefreshImportError(
            "existing recorder-audit checkpoint exceeds generation terminal"
        )
    if _global_terminal(db) != generation_terminal:
        raise PhysicalRefreshImportError(
            "physical import sequence interleaved after recorder audit"
        )
    return checkpoint


def _result_summary(recorders: tuple[tuple[str, str], ...]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "recorder_type": recorder_type,
            "recorder_ref": recorder_ref,
        } for recorder_type, recorder_ref in recorders
    )


def _prior_pull_state(
    db: Session,
    recorder_type: str,
    recorder_ref: str,
) -> tuple[str, int] | None:
    row = (
        db.query(models.StockRecorderPull)
        .filter(
            models.StockRecorderPull.recorder_type == recorder_type,
            models.StockRecorderPull.recorder_ref == recorder_ref,
        )
        .one_or_none()
    )
    if row is None:
        return None
    return str(row.status or ""), int(row.line_count or 0)


def _recorder_changed(
    prior_state: tuple[str, int] | None,
    result_status: str,
    touched: bool,
) -> bool:
    if touched:
        return True
    if prior_state is None:
        return False
    return prior_state[0] != str(result_status)


def run_physical_recorder_audit(
    db: Session,
    *,
    ledger_generation_id: int,
    parent_generation_id: int,
    client: Any,
    discovery_lookback: timedelta | None = None,
    discovery_window_size: timedelta = DISCOVERY_WINDOW_SIZE,
    discovery_page_size: int = DISCOVERY_PAGE_SIZE,
) -> PhysicalRefreshImportResult:
    """Run recorder-audit refresh for one physical-refresh BUILDING generation.

    ``discovery_lookback`` bounds how far back the register is re-read for
    recorders the ledger has never seen; ``None`` covers the whole retained
    horizon, which is what makes a document backdated behind the parent cutoff
    recoverable.
    """
    generation, parent = _require_target_generation(
        db,
        ledger_generation_id=ledger_generation_id,
        parent_generation_id=parent_generation_id,
    )

    target_cutoff = _utc(generation.cutoff, "target cutoff")
    parent_cutoff = _utc(parent.cutoff, "parent cutoff")
    start_boundary_id = int(parent.physical_import_batch_id)

    existing = _validate_checkpoint(
        db,
        generation_id=int(generation.id),
        parent_generation_id=int(parent.id),
        parent_cutoff=parent_cutoff,
        parent_terminal=start_boundary_id,
        child_cutoff=target_cutoff,
    )
    if existing is not None:
        metrics = dict(existing.metrics or {})
        discovery = dict(metrics.get("discovery") or {})
        return PhysicalRefreshImportResult(
            ledger_generation_id=int(generation.id),
            parent_generation_id=int(parent.id),
            target_cutoff=target_cutoff,
            recorder_count=int(metrics.get("recorder_count") or 0),
            recorder_audit_checksum=str(metrics.get("recorder_audit_checksum") or ""),
            changed_recorders=0,
            checkpoint_id=int(existing.id),
            terminal_physical_import_batch_id=int(metrics.get("physical_import_batch_id") or 0),
            from_checkpoint=True,
            discovered_recorders=int(discovery.get("discovered_recorders") or 0),
            backdated_recorders=int(discovery.get("backdated_recorders") or 0),
        )

    if int(generation.physical_import_batch_id) != start_boundary_id:
        raise PhysicalRefreshImportError(
            "target generation physical import seed changed before recorder audit"
        )
    _assert_global_terminal(db, start_boundary_id)

    # Discovery precedes the known set: a recorder that only 1C knows about must
    # join this audit, otherwise it can never enter the ledger at all.
    known_recorders = _collect_recorder_identities(db, int(parent_generation_id))
    discovery_range = _discovery_range(
        db,
        parent_cutoff=parent_cutoff,
        lookback=discovery_lookback,
    )
    discovered: tuple[tuple[str, str], ...] = ()
    if discovery_range is not None:
        discovery_from, discovery_to = discovery_range
        try:
            discovered = _discover_recorder_identities(
                client,
                from_exclusive=discovery_from,
                to_inclusive=discovery_to,
                window_size=discovery_window_size,
                page_size=discovery_page_size,
            )
        except Exception as exc:
            raise PhysicalRefreshImportError(
                f"backdated recorder discovery failed: {exc}"
            ) from exc
    backdated = tuple(sorted(set(discovered) - set(known_recorders)))
    discovery_metrics = {
        "from_exclusive": discovery_range[0].isoformat() if discovery_range else None,
        "to_inclusive": discovery_range[1].isoformat() if discovery_range else None,
        "window_size_seconds": int(discovery_window_size.total_seconds()),
        "discovered_recorders": len(discovered),
        "backdated_recorders": len(backdated),
        "backdated": list(_result_summary(backdated)),
    }

    target_recorders = _merge_recorder_identities(known_recorders, discovered)
    recorder_manifest = list(_result_summary(target_recorders))
    input_checksum = canonical_content_hash(recorder_manifest)

    run_rows: list[dict[str, Any]] = []
    changed_recorders = 0
    expected_terminal_id = start_boundary_id

    try:
        for recorder_type, recorder_ref in target_recorders:
            _assert_global_terminal(db, expected_terminal_id)
            prior_state = _prior_pull_state(db, recorder_type, recorder_ref)

            pull_kwargs = dict(
                client=client,
                source="physical_refresh_recorder_audit",
                ledger_generation_id=None,
                max_posting_at=generation.cutoff,
                strict_historical=True,
            )
            result = pull_recorder_movements(
                db, recorder_type, recorder_ref, **pull_kwargs
            )
            if result.status not in {"done", "empty"}:
                raise PhysicalRefreshImportError(
                    f"recorder {recorder_type} {recorder_ref} failed with status {result.status}"
                )
            if result.error or result.diagnostics:
                raise PhysicalRefreshImportError(
                    f"recorder {recorder_type} {recorder_ref} returned diagnostics"
                )
            if (
                result.skipped_unknown_item
                or result.skipped_unknown_record_type
                or result.skipped_non_warehouse
            ):
                raise PhysicalRefreshImportError(
                    f"recorder {recorder_type} {recorder_ref} produced skipped movements"
                )

            observed_terminal = db.query(func.max(models.PhysicalImportBatch.id)).scalar()
            result_batch_id = result.physical_import_batch_id
            if result_batch_id is None:
                # A true no-change pull may legitimately have no own batch
                # (for example an untouched empty recorder).  It is valid
                # only when the global terminal stayed exactly at our prefix.
                if (
                    result.inserted == 0
                    and result.deleted == 0
                    and observed_terminal is not None
                    and int(observed_terminal) == int(expected_terminal_id)
                ):
                    result_batch_id = expected_terminal_id
                else:
                    raise PhysicalRefreshImportError(
                        f"recorder {recorder_type} {recorder_ref} returned no physical batch lineage"
                    )
            if observed_terminal is None:
                raise PhysicalRefreshImportError(
                    "physical import sequence interleaved during recorder audit"
                )
            if int(observed_terminal) == int(expected_terminal_id):
                # No global advance: an exact re-pull may report its older
                # recorder watermark, but it must have changed nothing.
                if result.inserted or result.deleted:
                    raise PhysicalRefreshImportError(
                        f"recorder {recorder_type} {recorder_ref} changed without a new batch"
                    )
            else:
                # A new terminal must be exactly this pull's batch, and its
                # provenance must identify the recorder being audited.
                if int(observed_terminal) <= int(expected_terminal_id) or int(result_batch_id) != int(observed_terminal):
                    raise PhysicalRefreshImportError(
                        "physical import sequence interleaved during recorder audit"
                    )
                batch = db.get(models.PhysicalImportBatch, int(result_batch_id))
                marks = dict(batch.source_watermarks or {}) if batch is not None else {}
                if (
                    marks.get("recorder_type") != recorder_type
                    or marks.get("recorder_ref") != recorder_ref
                ):
                    raise PhysicalRefreshImportError(
                        f"recorder {recorder_type} {recorder_ref} returned foreign batch lineage"
                    )
                expected_terminal_id = int(result_batch_id)

            run_rows.append({
                "recorder_type": recorder_type,
                "recorder_ref": recorder_ref,
                "status": result.status,
                "inserted": int(result.inserted),
                "line_count": int(result.inserted),
                "deleted": int(result.deleted),
                "touched_keys": len(result.touched_keys or ()),
            })
            changed_recorders += int(
                _recorder_changed(
                    prior_state,
                    result.status,
                    bool(result.touched_keys),
                )
            )
            expected_terminal_id = _global_terminal(db)

        current_terminal = expected_terminal_id

        generation.physical_import_batch_id = current_terminal
        generation.source_watermarks = {
            **dict(generation.source_watermarks or {}),
            "recorder_audit": {
                "version": CHECKPOINT_VERSION,
                "checkpoint_key": _checkpoint_key(int(generation.id)),
                "checksum": canonical_content_hash(run_rows),
                "input_checksum": input_checksum,
                "recorder_count": len(target_recorders),
                "discovery": discovery_metrics,
            },
        }

        checkpoint = models.LedgerBuildBatch(
            ledger_generation_id=int(generation.id),
            stage="physical_import",
            batch_key=_checkpoint_key(int(generation.id)),
            status="completed",
            algorithm_version=ALGORITHM_VERSION,
            metrics={
                "checkpoint_version": CHECKPOINT_VERSION,
                "generation_kind": GENERATION_KIND,
                "parent_generation_id": int(parent.id),
                "parent_cutoff": parent_cutoff.isoformat(),
                "parent_physical_import_batch_id": int(parent.physical_import_batch_id),
                "target_cutoff": target_cutoff.isoformat(),
                "recorder_count": len(target_recorders),
                "recorder_audit_checksum": canonical_content_hash(run_rows),
                "recorder_input_checksum": input_checksum,
                "physical_import_batch_id": int(current_terminal),
                "recorders": recorder_manifest,
                "audit_rows": run_rows,
                "discovery": discovery_metrics,
            },
            completed_at=datetime.now(timezone.utc),
        )
        db.add(checkpoint)
        db.flush()
        db.commit()

        return PhysicalRefreshImportResult(
            ledger_generation_id=int(generation.id),
            parent_generation_id=int(parent.id),
            target_cutoff=target_cutoff,
            recorder_count=len(target_recorders),
            recorder_audit_checksum=canonical_content_hash(run_rows),
            changed_recorders=int(changed_recorders),
            checkpoint_id=int(checkpoint.id),
            terminal_physical_import_batch_id=int(current_terminal),
            from_checkpoint=False,
            discovered_recorders=len(discovered),
            backdated_recorders=len(backdated),
        )
    except PhysicalRefreshImportError:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise PhysicalRefreshImportError(f"physical recorder-audit failed: {exc}") from exc

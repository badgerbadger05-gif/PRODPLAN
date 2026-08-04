"""Fail-closed access to the accepted Item Ledger generation.

This module deliberately has no legacy fallback.  Consumers must either receive
an accepted generation identity or stop their calculation with
``PlanningTruthUnavailable``.
"""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from collections.abc import Iterable
import os
from typing import Any, Mapping

from sqlalchemy.orm import Session
from sqlalchemy import select, text

from app import models


TRUTH_STATUSES = frozenset(
    {"uninitialized", "building", "accepted", "stale", "rejected"},
)
CAPABILITY_PHYSICAL_LEDGER = "physical_ledger"
CAPABILITY_RESERVATION_REPLAY = "reservation_replay"
CAPABILITY_EXECUTION_ALLOCATIONS = "execution_allocations"
CAPABILITY_PLANNING_SNAPSHOTS = "planning_snapshots"
CAPABILITY_ASSEMBLY_QUEUE = "assembly_queue"
CAPABILITY_DRUM_SCHEDULE = "drum_schedule"
CAPABILITY_SHELF_PROJECTION = "shelf_projection"
CAPABILITY_PURCHASE_CONTROL_JOURNAL = "purchase_control_journal"
CAPABILITY_PRODUCTION_CONTROL_JOURNAL = "production_control_journal"
CAPABILITY_FUTURE_SUPPLY = "future_supply"
CAPABILITY_RESERVATION_CONSUMPTION_ALLOCATION = "reservation_consumption_allocation"
TRUTH_MAX_AGE_SECONDS_ENV = "PLANNING_TRUTH_MAX_AGE_SECONDS"


@dataclass(frozen=True)
class PlanningTruthReadiness:
    truth_status: str
    ready: bool
    ledger_generation: int | None
    generation_key: str | None
    cutoff: datetime | None
    source_watermarks: Mapping[str, Any]
    capabilities: Mapping[str, bool]
    algorithm_version: str | None
    replay_version: str | None
    reason: str | None
    accepted_at: datetime | None

    @property
    def status(self) -> str:
        return self.truth_status

    @property
    def generation_id(self) -> int | None:
        return self.ledger_generation

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class PlanningTruthUnavailable(RuntimeError):
    """Domain error raised when a Ledger-dependent operation cannot run."""

    code = "planning_truth_unavailable"

    def __init__(
        self,
        readiness: PlanningTruthReadiness,
        *,
        consumer: str | None = None,
    ):
        self.readiness = readiness
        self.state = readiness
        self.consumer = consumer
        super().__init__(
            readiness.reason
            or f"planning truth is {readiness.truth_status}; accepted Ledger required"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "consumer": self.consumer,
            **self.readiness.as_dict(),
        }


class PlanningSnapshotConflict(RuntimeError):
    """A snapshot key was reused with different immutable content."""

    code = "planning_snapshot_conflict"


class PlanningTruthInvalidationConflict(RuntimeError):
    """The requested invalidation does not match the current truth pointer."""

    code = "planning_truth_invalidation_conflict"


class PlanningTruthPublishConflict(RuntimeError):
    """The pointer moved away from the parent this publication descends from."""

    code = "planning_truth_publish_conflict"


def _serialize_publication(db: Session) -> None:
    """Make every publisher contend for one lock, not two disjoint ones.

    ``physical_refresh_orchestrator`` holds a session-level physical-sequence
    lock while ``obligation_refresh_*`` holds ``MRP_LEDGER_LOCK_KEY``; on their
    own the two never exclude each other, so two pipelines could reach the
    pointer at once.  Both publication paths converge here, so taking the MRP
    lock for the pointer switch serialises them without a second lock order:
    the obligation path already holds it (a transaction-scoped advisory lock is
    re-entrant for the session which owns it) and the physical path takes it
    only after its own lock, never the reverse.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    from .mrp_freeze import MRP_LEDGER_LOCK_KEY

    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MRP_LEDGER_LOCK_KEY})


def _configured_max_age() -> timedelta | None:
    raw = str(os.environ.get(TRUTH_MAX_AGE_SECONDS_ENV) or "").strip()
    if not raw:
        return None
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{TRUTH_MAX_AGE_SECONDS_ENV} must be a positive integer"
        ) from exc
    if seconds <= 0:
        raise RuntimeError(
            f"{TRUTH_MAX_AGE_SECONDS_ENV} must be a positive integer"
        )
    return timedelta(seconds=seconds)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_readiness(
    db: Session,
    *,
    now: datetime | None = None,
) -> PlanningTruthReadiness:
    """Return current truth state without guessing or consulting legacy facts."""
    pointer = db.get(models.PlanningTruthState, 1)
    generation = pointer.current_generation if pointer is not None else None
    if generation is None:
        return PlanningTruthReadiness(
            truth_status="uninitialized",
            ready=False,
            ledger_generation=None,
            generation_key=None,
            cutoff=None,
            source_watermarks={},
            capabilities={},
            algorithm_version=None,
            replay_version=None,
            reason="No Item Ledger generation has been published",
            accepted_at=None,
        )

    status = generation.status
    structurally_accepted = (
        status == "accepted"
        and generation.cutoff is not None
        and generation.accepted_at is not None
    )
    reason = generation.reason
    if status == "accepted" and not structurally_accepted:
        reason = reason or "Accepted generation is missing cutoff or accepted_at"
    freshness_limit = _configured_max_age()
    if structurally_accepted and freshness_limit is not None:
        checked_at = _as_utc(now or datetime.now(timezone.utc))
        freshness_reference = min(
            _as_utc(generation.cutoff),
            _as_utc(generation.accepted_at),
        )
        age = checked_at - freshness_reference
        if age > freshness_limit:
            status = "stale"
            structurally_accepted = False
            reason = (
                "Accepted generation exceeded freshness threshold: "
                f"reference={freshness_reference.isoformat()}, "
                f"age_seconds={int(age.total_seconds())}, "
                f"max_age_seconds={int(freshness_limit.total_seconds())}"
            )
    return PlanningTruthReadiness(
        truth_status=status if structurally_accepted or status != "accepted" else "rejected",
        ready=structurally_accepted,
        ledger_generation=generation.id,
        generation_key=generation.generation_key,
        cutoff=generation.cutoff,
        source_watermarks=dict(generation.source_watermarks or {}),
        capabilities={
            str(name): bool(enabled)
            for name, enabled in (generation.capabilities or {}).items()
        },
        algorithm_version=generation.algorithm_version,
        replay_version=generation.replay_version,
        reason=reason,
        accepted_at=generation.accepted_at,
    )


def require_accepted(db: Session) -> PlanningTruthReadiness:
    """Return accepted truth or fail closed with a machine-readable error."""
    readiness = get_readiness(db)
    if not readiness.ready:
        raise PlanningTruthUnavailable(readiness)
    return readiness


def get_truth_state(db: Session) -> PlanningTruthReadiness:
    """Consumer-facing name for the current structured readiness state."""
    return get_readiness(db)


def require_accepted_truth(
    db: Session,
    consumer: str,
    required_capabilities: Iterable[str] = (),
    *,
    allow_stale: bool = False,
) -> PlanningTruthReadiness:
    """Fail closed for a named report, planner, DBR or mutation consumer."""
    readiness = get_truth_state(db)
    stale_but_explicitly_allowed = (
        bool(allow_stale) and str(readiness.truth_status) == "stale"
    )
    if not readiness.ready and not stale_but_explicitly_allowed:
        raise PlanningTruthUnavailable(readiness, consumer=consumer)
    missing = sorted({
        str(capability)
        for capability in required_capabilities
        if not readiness.capabilities.get(str(capability), False)
    })
    if missing:
        unavailable = replace(
            readiness,
            ready=False,
            reason="Accepted Ledger generation lacks capabilities: " + ", ".join(missing),
        )
        raise PlanningTruthUnavailable(unavailable, consumer=consumer)
    return readiness


def publish_generation(
    db: Session,
    generation: models.LedgerGeneration,
    *,
    expected_parent_id: int | None = None,
) -> PlanningTruthReadiness:
    """Atomically point planning reads at a structurally valid accepted build.

    ``expected_parent_id`` is a compare-and-set on the truth pointer: a build
    forked from one accepted generation must not overwrite a pointer that has
    since moved to another.  The pointer row is locked for the check, so a
    concurrent publisher either loses the race with a conflict or waits.
    """
    if generation.status not in TRUTH_STATUSES:
        raise ValueError(f"unsupported truth status: {generation.status}")
    if generation.status != "accepted":
        raise ValueError("only an accepted Ledger generation can be published")
    if generation.cutoff is None:
        raise ValueError("accepted Ledger generation requires cutoff")
    if generation.accepted_at is None:
        generation.accepted_at = datetime.now(timezone.utc)

    db.add(generation)
    db.flush()
    _serialize_publication(db)
    pointer = db.execute(
        select(models.PlanningTruthState)
        .where(models.PlanningTruthState.id == 1)
        .with_for_update(),
    ).scalar_one_or_none()
    if pointer is None:
        pointer = models.PlanningTruthState(id=1)
        db.add(pointer)
        db.flush()
    current_id = (
        int(pointer.current_generation_id)
        if pointer.current_generation_id is not None
        else None
    )
    if expected_parent_id is not None:
        expected = int(expected_parent_id)
        # Republishing the same generation is idempotent, not a conflict.
        if current_id not in {expected, int(generation.id)}:
            raise PlanningTruthPublishConflict(
                f"planning truth pointer is {current_id}, expected parent {expected}"
            )
    pointer.current_generation_id = generation.id
    db.flush()
    # A long-lived worker session may already have resolved the relationship to
    # the previous generation. Force the readiness read to follow the new FK.
    db.expire(pointer, ["current_generation"])
    return get_readiness(db)


def invalidate_current_generation(
    db: Session,
    *,
    expected_generation_id: int,
    status: str,
    reason: str,
) -> PlanningTruthReadiness:
    """Fail-close the current accepted generation without moving its pointer.

    The caller owns the surrounding transaction.  An exact repeat is
    idempotent; changing status or reason after invalidation is a conflict.
    Keeping the pointer on the invalid generation prevents accidental fallback
    to an older accepted generation while a replacement is being built.
    """
    target_status = str(status or "").strip().casefold()
    if target_status not in {"stale", "rejected"}:
        raise ValueError("invalidation status must be stale or rejected")
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise ValueError("invalidation reason must be nonblank")
    try:
        target_id = int(expected_generation_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected_generation_id must be a positive integer") from exc
    if target_id <= 0:
        raise ValueError("expected_generation_id must be a positive integer")

    pointer = db.execute(
        select(models.PlanningTruthState)
        .where(models.PlanningTruthState.id == 1)
        .with_for_update(),
    ).scalar_one_or_none()
    if pointer is None or pointer.current_generation_id is None:
        raise PlanningTruthInvalidationConflict("planning truth has no current generation")
    if int(pointer.current_generation_id) != target_id:
        raise PlanningTruthInvalidationConflict(
            f"current generation is {pointer.current_generation_id}, expected {target_id}"
        )
    generation = db.execute(
        select(models.LedgerGeneration)
        .where(models.LedgerGeneration.id == target_id)
        .with_for_update(),
    ).scalar_one_or_none()
    if generation is None:
        raise PlanningTruthInvalidationConflict("current generation row is missing")

    if generation.status in {"stale", "rejected"}:
        if generation.status == target_status and str(generation.reason or "").strip() == normalized_reason:
            db.expire(pointer, ["current_generation"])
            return get_readiness(db)
        raise PlanningTruthInvalidationConflict(
            f"generation {target_id} is already {generation.status} for a different invalidation"
        )
    if generation.status != "accepted":
        raise PlanningTruthInvalidationConflict(
            f"current generation {target_id} is {generation.status}, not accepted"
        )

    generation.status = target_status
    generation.reason = normalized_reason
    db.flush()
    db.expire(pointer, ["current_generation"])
    return get_readiness(db)


def publish_read_snapshot(
    db: Session,
    *,
    consumer: str,
    snapshot_key: str,
    payload: Mapping[str, Any],
    required_capabilities: Iterable[str] = (),
    reason: str | None = None,
    published_at: datetime | None = None,
) -> models.PlanningReadSnapshot:
    """Atomically publish an immutable read payload for current accepted truth.

    ``consumer`` + ``snapshot_key`` is idempotent. Reusing that identity with
    different content or truth lineage is rejected rather than overwritten.
    The caller owns the surrounding transaction and must commit it.
    """
    truth = require_accepted_truth(
        db,
        consumer,
        required_capabilities=required_capabilities,
    )
    existing = db.execute(
        select(models.PlanningReadSnapshot).where(
            models.PlanningReadSnapshot.consumer == consumer,
            models.PlanningReadSnapshot.snapshot_key == snapshot_key,
            models.PlanningReadSnapshot.ledger_generation_id
            == truth.generation_id,
        ),
    ).scalar_one_or_none()
    immutable_payload = dict(payload)
    if existing is not None:
        same = (
            existing.ledger_generation_id == truth.generation_id
            and existing.cutoff == truth.cutoff
            and existing.truth_status == truth.status
            and existing.payload == immutable_payload
            and existing.reason == reason
        )
        if not same:
            raise PlanningSnapshotConflict(
                f"snapshot {consumer}/{snapshot_key} already exists with different content"
            )
        return existing

    snapshot = models.PlanningReadSnapshot(
        consumer=consumer,
        snapshot_key=snapshot_key,
        ledger_generation_id=truth.generation_id,
        cutoff=truth.cutoff,
        truth_status=truth.status,
        payload=immutable_payload,
        reason=reason,
        published_at=published_at or datetime.now(timezone.utc),
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def get_latest_read_snapshot(
    db: Session,
    *,
    consumer: str,
    snapshot_key: str | None = None,
    required_capabilities: Iterable[str] = (),
) -> models.PlanningReadSnapshot | None:
    """Read the latest snapshot only for the current accepted truth lineage."""
    truth = require_accepted_truth(
        db,
        consumer,
        required_capabilities=required_capabilities,
    )
    query = select(models.PlanningReadSnapshot).where(
            models.PlanningReadSnapshot.consumer == consumer,
            models.PlanningReadSnapshot.ledger_generation_id == truth.generation_id,
            models.PlanningReadSnapshot.cutoff == truth.cutoff,
            models.PlanningReadSnapshot.truth_status == "accepted",
        )
    if snapshot_key is not None:
        query = query.where(
            models.PlanningReadSnapshot.snapshot_key == snapshot_key,
        )
    return db.execute(
        query
        .order_by(
            models.PlanningReadSnapshot.published_at.desc(),
            models.PlanningReadSnapshot.id.desc(),
        )
        .limit(1),
    ).scalar_one_or_none()

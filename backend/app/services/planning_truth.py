"""Fail-closed access to the accepted Item Ledger generation.

This module deliberately has no legacy fallback.  Consumers must either receive
an accepted generation identity or stop their calculation with
``PlanningTruthUnavailable``.
"""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from collections.abc import Iterable
from typing import Any, Mapping

from sqlalchemy.orm import Session
from sqlalchemy import select

from app import models


TRUTH_STATUSES = frozenset(
    {"uninitialized", "building", "accepted", "stale", "rejected"},
)
CAPABILITY_PHYSICAL_LEDGER = "physical_ledger"
CAPABILITY_RESERVATION_REPLAY = "reservation_replay"
CAPABILITY_EXECUTION_ALLOCATIONS = "execution_allocations"
CAPABILITY_PLANNING_SNAPSHOTS = "planning_snapshots"
CAPABILITY_DBR_FEEDER_COCKPIT = "dbr_feeder_cockpit"


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


def get_readiness(db: Session) -> PlanningTruthReadiness:
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
) -> PlanningTruthReadiness:
    """Fail closed for a named report, planner, DBR or mutation consumer."""
    readiness = get_truth_state(db)
    if not readiness.ready:
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
) -> PlanningTruthReadiness:
    """Atomically point planning reads at a structurally valid accepted build."""
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
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None:
        pointer = models.PlanningTruthState(id=1)
        db.add(pointer)
    pointer.current_generation_id = generation.id
    db.flush()
    # A long-lived worker session may already have resolved the relationship to
    # the previous generation. Force the readiness read to follow the new FK.
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

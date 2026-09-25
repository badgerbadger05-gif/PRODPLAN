"""Fail-closed access to the accepted Item Ledger generation.

This module deliberately has no legacy fallback.  Consumers must either receive
an accepted generation identity or stop their calculation with
``PlanningTruthUnavailable``.
"""

import contextvars
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from collections.abc import Iterable, Iterator
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
CAPABILITY_ASSEMBLY_READINESS = "assembly_readiness"
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
    #: Decision §57: cutoff of the last successful reconciliation with 1C that
    #: found no semantic delta for *this* pointer generation.  ``None`` when the
    #: pointer has never been verified or moved since it was.  Operators read it
    #: as "сверено до ...".
    verified_cutoff: datetime | None = None
    #: When that reconciliation was recorded.
    verified_at: datetime | None = None

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


_PUBLICATION_CONTEXT: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "prodplan_inside_publication", default=False,
)


@contextmanager
def publication_context() -> Iterator[None]:
    """Mark the calling stack as the publication that restores freshness.

    Decision §40 names exactly two entry points: the physical refresh
    publication (``publish_forward_physical_refresh_current``) and the no-op
    scope repair (``repair_current_execution_scopes_from_pointer``).  They are
    what makes the accepted pointer young again, so gating them on that
    pointer's age is a deadlock: once a stand has been quiet longer than the
    threshold, every tick fails on staleness and nothing can clear it.  An
    obligation refresh or a build acceptance is not such an entry point - an
    obligation refresh inherits its parent's cutoff and freezes MRP, which
    the contract forbids on stale truth - so neither may enter this context.

    The exemption is a property of the operation, not of one call, which is
    why it lives here and not in a keyword threaded through the builders.
    Two consumers were found by rehearsal alone - the MRP payload builder and
    the readiness custody read - and a third would have been found the same
    way.  Inside the context the *age* check is skipped and nothing else is:
    pointer coherence, required capabilities and an operator invalidation all
    still apply, and readers outside it keep the gate.
    """
    token = _PUBLICATION_CONTEXT.set(True)
    try:
        yield
    finally:
        _PUBLICATION_CONTEXT.reset(token)


def inside_publication() -> bool:
    """Whether the caller runs inside a publication (see §40)."""
    return bool(_PUBLICATION_CONTEXT.get())


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


def _pointer_verification(
    pointer: models.PlanningTruthState | None,
    generation: models.LedgerGeneration | None,
) -> tuple[datetime | None, datetime | None]:
    """The §57 verification of *this* pointer generation, or ``(None, None)``.

    A verification proves only the generation it was computed from.  A pointer
    move to a generation with *new* physical facts leaves the stored triple
    describing a generation that is no longer current, and it is simply not
    read; the next verification overwrites it.  That is why no pointer writer
    has to clear it.  The one move that keeps it is handled by
    :func:`move_pointer_to_generation`.
    """
    if pointer is None or generation is None:
        return None, None
    verified_generation_id = pointer.verified_generation_id
    if verified_generation_id is None or int(verified_generation_id) != int(generation.id):
        return None, None
    if pointer.verified_cutoff is None or pointer.verified_at is None:
        return None, None
    return _as_utc(pointer.verified_cutoff), _as_utc(pointer.verified_at)


def get_readiness(
    db: Session,
    *,
    now: datetime | None = None,
    apply_freshness_limit: bool = True,
) -> PlanningTruthReadiness:
    """Return current truth state without guessing or consulting legacy facts.

    ``apply_freshness_limit=False`` drops only the age gate, and so does
    running inside :func:`publication_context` (§40).  Every structural rule
    and an explicit operator invalidation still decide the status, so a
    generation invalidated to ``stale``/``rejected`` stays unavailable.

    Freshness is measured from the last successful reconciliation with 1C, not
    from the last publication (§57): a refresh that read 1C up to a newer
    cutoff, converged on balances and found no semantic delta proves the
    accepted generation is still true up to that cutoff, and publishing a
    successor for it would be pure churn.  Without that rule a quiet weekend
    put every HTTP reader into ``stale`` until the first posting arrived.
    """
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
    freshness_limit = (
        _configured_max_age()
        if apply_freshness_limit and not inside_publication()
        else None
    )
    verified_cutoff, verified_at = _pointer_verification(pointer, generation)
    if structurally_accepted and freshness_limit is not None:
        checked_at = _as_utc(now or datetime.now(timezone.utc))
        freshness_reference = min(
            _as_utc(generation.cutoff),
            _as_utc(generation.accepted_at),
        )
        if verified_cutoff is not None and verified_at is not None:
            # Same conservative pairing as the publication above: a cutoff can
            # only vouch for the span actually read, so a cutoff dated past the
            # moment the check was recorded cannot buy extra freshness.
            freshness_reference = max(
                freshness_reference, min(verified_cutoff, verified_at),
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
        verified_cutoff=verified_cutoff,
        verified_at=verified_at,
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
    ignore_freshness_limit: bool = False,
) -> PlanningTruthReadiness:
    """Fail closed for a named report, planner, DBR or mutation consumer.

    ``ignore_freshness_limit`` is the same exemption as
    :func:`publication_context` (§40), spelled for one call.  Prefer the
    context: the exemption belongs to the operation, not to a call site, and
    threading a keyword through every builder only finds the consumers
    somebody happened to think of.  Either way it drops the age gate and
    nothing else - structural validity, capabilities and operator
    invalidation still apply, and HTTP readers keep the gate.
    """
    readiness = get_readiness(
        db, apply_freshness_limit=not bool(ignore_freshness_limit)
    )
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


def move_pointer_to_generation(
    db: Session,
    pointer: models.PlanningTruthState,
    generation: models.LedgerGeneration,
) -> None:
    """Move the accepted pointer, keeping a §57 verification that still holds.

    Every pointer writer goes through here, because the rule belongs to the
    move and not to one publisher.  A successor which inherits its parent's
    cutoff - an obligation refresh - stands on exactly the same physical facts
    the reconciliation with 1C proved true, so the proof moves with the
    pointer: otherwise the first plan fixation after a quiet weekend (when the
    pointer is fresh only by §57) would drop the verification and put every
    reader into ``stale`` until the next posting arrives.

    A successor with a new cutoff carries new facts which nothing has verified
    yet, so it never inherits the proof.  ``verified_cutoff``/``verified_at``
    are never rewritten here: the check is not re-run by moving a pointer.
    """
    previous = (
        db.get(models.LedgerGeneration, int(pointer.current_generation_id))
        if pointer.current_generation_id is not None
        else None
    )
    verified_cutoff, verified_at = _pointer_verification(pointer, previous)
    same_physical_facts = (
        previous is not None
        and previous.cutoff is not None
        and generation.cutoff is not None
        and _as_utc(previous.cutoff) == _as_utc(generation.cutoff)
    )
    pointer.current_generation_id = int(generation.id)
    if (
        same_physical_facts
        and verified_cutoff is not None
        and verified_at is not None
    ):
        pointer.verified_generation_id = int(generation.id)
    db.flush()


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
    move_pointer_to_generation(db, pointer, generation)
    # A long-lived worker session may already have resolved the relationship to
    # the previous generation. Force the readiness read to follow the new FK.
    db.expire(pointer, ["current_generation"])
    return get_readiness(db)


def record_pointer_verification(
    db: Session,
    *,
    verified_generation_id: int,
    verified_cutoff: datetime,
    balance_convergence_valid: bool,
    verified_at: datetime | None = None,
) -> datetime | None:
    """Extend the current pointer's freshness by a proven 1C reconciliation (§57).

    A physical refresh that read 1C up to ``verified_cutoff``, converged on
    balances and found no semantic delta has proved the accepted generation
    still true up to that cutoff.  It creates no successor - an equivalent
    import has none - so the proof is recorded here, on the pointer, and never
    by mutating the accepted generation row: generation lineage is immutable.

    The rule is deliberately narrow and fail-closed in every direction:

    * only forward - a later verification never lowers the reference;
    * only for the pointer the check was computed from, re-read under the
      pointer lock, because the pointer may have moved since;
    * only with a valid balance convergence - a failed or non-converged check
      proves nothing and extends nothing.

    Returns the stored cutoff when the verification moved the reference, and
    ``None`` when it changed nothing, so an unchanged pointer produces no
    write and no WAL churn.  The caller owns the transaction.
    """
    if not balance_convergence_valid:
        return None
    if verified_cutoff is None:
        raise ValueError("verified_cutoff is required")
    try:
        target_id = int(verified_generation_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("verified_generation_id must be a positive integer") from exc
    if target_id <= 0:
        raise ValueError("verified_generation_id must be a positive integer")

    # The pointer is reached through the same serialization as a publication:
    # this call also runs after the no-op repair has locked current scopes and
    # rows, so taking the pointer without the shared lock would be a second
    # lock order against ``publish_generation``.
    _serialize_publication(db)
    pointer = db.execute(
        select(models.PlanningTruthState)
        .where(models.PlanningTruthState.id == 1)
        .with_for_update(),
    ).scalar_one_or_none()
    if pointer is None or pointer.current_generation_id is None:
        return None
    if int(pointer.current_generation_id) != target_id:
        return None
    generation = db.get(models.LedgerGeneration, target_id)
    if (
        generation is None
        or str(generation.status) != "accepted"
        or generation.cutoff is None
        or generation.accepted_at is None
    ):
        return None

    checked_at = _as_utc(verified_at or datetime.now(timezone.utc))
    cutoff = _as_utc(verified_cutoff)
    # Same conservative pairing the reader applies: a check vouches only for
    # the span it actually read.
    effective = min(cutoff, checked_at)
    reference = min(_as_utc(generation.cutoff), _as_utc(generation.accepted_at))
    stored_cutoff, stored_at = _pointer_verification(pointer, generation)
    if stored_cutoff is not None and stored_at is not None:
        reference = max(reference, min(stored_cutoff, stored_at))
    if effective <= reference:
        return None

    pointer.verified_generation_id = target_id
    pointer.verified_cutoff = cutoff
    pointer.verified_at = checked_at
    db.flush()
    return cutoff


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
# End of current truth helpers.

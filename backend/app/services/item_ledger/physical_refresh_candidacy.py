"""One owner of the question "whose candidate is this BUILDING generation?".

A physical refresh forks the accepted generation, imports facts above its
parent's physical boundary across several committed checkpoints and only then
publishes.  Between the fork and the publication the planning-truth pointer can
move on its own: a specification rebase publishes a fact-identical fork, so the
candidate keeps its parent while the pointer walks away from it.  Such a
candidate can never be published — ``publish_generation`` compares the pointer
with the parent the build descends from — but it still holds the global terminal
of the physical import sequence.

Comparing the candidate's parent with the *current* pointer by strict equality
cannot tell that ordinary case apart from a candidate of a foreign branch of
truth.  The first is traffic the pipeline must roll back by itself; the second
is a broken Ledger an operator must look at.  Treating both as "unexpected"
fenced the physical slot until a human noticed — and because a fenced slot stops
moving the cutoff, planning truth aged past its freshness threshold and every
consumer failed closed a day later.

The sealed ``parent_generation_id`` chain separates them, exactly as it does for
live runs in :mod:`live_plan_scope`: a parent inside the accepted lineage is our
own superseded candidate, anything else is foreign.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app import models

from .live_plan_scope import sealed_generation_lineage_ids


PHYSICAL_REFRESH_KIND = "physical_refresh"

#: The candidate descends from the generation the pointer currently names.
CANDIDATE_CURRENT = "current"
#: The candidate descends from a sealed ancestor: unpublishable, discardable.
CANDIDATE_SUPERSEDED = "superseded"
#: Malformed, or from another branch of truth: operator review, never automatic.
CANDIDATE_FOREIGN = "foreign"


def looks_like_physical_refresh(generation: models.LedgerGeneration) -> bool:
    """True for anything that claims to be a physical-refresh candidate.

    Deliberately wider than the well-formed marks: a malformed candidate must
    still be inventoried, not skipped into invisibility.
    """
    marks = dict(generation.source_watermarks or {})
    key = str(generation.generation_key or "")
    algorithm = str(generation.algorithm_version or "")
    return (
        marks.get("generation_kind") == PHYSICAL_REFRESH_KIND
        or key.startswith("physical-refresh:")
        or "physical-refresh" in algorithm
    )


def building_physical_refresh_candidates(
    db: Session,
) -> list[models.LedgerGeneration]:
    """Every BUILDING generation that claims to be a physical refresh."""
    return [
        generation
        for generation in (
            db.query(models.LedgerGeneration)
            .filter(models.LedgerGeneration.status == "building")
            .order_by(models.LedgerGeneration.id.asc())
            .all()
        )
        if looks_like_physical_refresh(generation)
    ]


def classify_physical_refresh_candidate(
    db: Session,
    candidate: models.LedgerGeneration,
    accepted: models.LedgerGeneration | None,
) -> str:
    """Resolve one candidate against the accepted sealed lineage.

    A broken or cyclic lineage is not silently widened into acceptance: it
    resolves to ``CANDIDATE_FOREIGN`` so the physical slot stays fenced for an
    operator instead of rolling candidates back on a lineage nobody can prove.
    """
    marks = dict(candidate.source_watermarks or {})
    if (
        marks.get("generation_kind") != PHYSICAL_REFRESH_KIND
        or candidate.cutoff is None
        or not str(candidate.generation_key or "")
        or accepted is None
    ):
        return CANDIDATE_FOREIGN
    try:
        parent_id = int(marks["parent_generation_id"])
    except (KeyError, TypeError, ValueError):
        return CANDIDATE_FOREIGN
    if parent_id == int(accepted.id):
        return CANDIDATE_CURRENT
    try:
        lineage = sealed_generation_lineage_ids(db, accepted)
    except ValueError:
        return CANDIDATE_FOREIGN
    if parent_id in lineage:
        return CANDIDATE_SUPERSEDED
    return CANDIDATE_FOREIGN


def has_live_physical_refresh_candidate(db: Session) -> bool:
    """True while a candidate of the *current* pointer is still building.

    Callers which retire live obligations (the specification rebase) use this to
    stand aside: retiring a run the in-flight candidate is carrying forward
    fails that build, and the failed build then holds the physical terminal.
    """
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or pointer.current_generation_id is None:
        return False
    accepted = db.get(
        models.LedgerGeneration, int(pointer.current_generation_id)
    )
    if accepted is None or str(accepted.status) != "accepted":
        return False
    return any(
        classify_physical_refresh_candidate(db, candidate, accepted)
        == CANDIDATE_CURRENT
        for candidate in building_physical_refresh_candidates(db)
    )

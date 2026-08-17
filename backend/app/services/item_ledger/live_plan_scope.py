"""Resolve the one sealed set of live planning runs for a Ledger generation.

Two generation kinds define this scope, and both define it *explicitly*:

* an ``obligation_refresh`` generation seals the scope in its own manifest —
  retained parent runs plus published candidates, minus retired plans;
* every other generation (``physical_refresh`` and the historical bootstrap)
  forks only immutable physical lineage and does not re-anchor obligations at
  all, so it inherits the scope of the generation it was forked from.

Inheritance is walked over the sealed ``parent_generation_id`` watermark chain,
never over wall-clock "latest FIXED_SNAPSHOT" guesswork: the lineage is
immutable once published, so the same generation resolves the same scope on
every rebuild.  A physical refresh publishes new *facts*, not new obligations —
losing the live scope there is what silently emptied the assembly queue, the
drum and plan execution while the reservations of the very same generation kept
carrying all ten runs.

The same chain answers the narrower question "is *this* run live for *this*
generation?".  ``sealed_run_anchor`` is the single owner of that answer: a run
is live when it is anchored anywhere in the sealed lineage and its own
``ledger_cutoff`` still matches the cutoff of the generation it is anchored to.
Comparing a run's anchor with the *accepted* generation id instead is the same
defect as above, one run at a time: it fails every read path after the first
physical refresh, because an obligation is deliberately never re-anchored by a
fact-only fork.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import models


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    """Compare two cutoffs as instants, never as naive/aware representations."""
    if left is None or right is None:
        return False
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return left.astimezone(timezone.utc) == right.astimezone(timezone.utc)


def _obligation_refresh_run_ids(marks: dict) -> tuple[int, ...]:
    manifest = marks.get("obligation_refresh_manifest")
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        raise ValueError("obligation refresh generation lacks sealed live-plan scope")
    run_ids: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("obligation refresh live-plan scope is malformed")
        action = str(entry.get("action") or "")
        if action == "retire":
            continue
        field = "parent_run_id" if action == "retain" else "candidate_run_id"
        try:
            run_ids.add(int(entry[field]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "obligation refresh live-plan scope is malformed"
            ) from exc
    return tuple(sorted(run_ids))


def sealed_generation_lineage_ids(
    db: Session,
    generation: models.LedgerGeneration,
) -> tuple[int, ...]:
    """Return this generation and every sealed ancestor, newest first.

    The chain is the published lineage: each fork records the generation that
    was the accepted planning-truth pointer at the moment it was created.  A
    missing or cyclic link is a broken Ledger, so it fails closed instead of
    silently truncating the inherited scope.
    """
    lineage: list[int] = []
    seen: set[int] = set()
    current: models.LedgerGeneration | None = generation
    while current is not None:
        current_id = int(current.id)
        if current_id in seen:
            raise ValueError(
                f"ledger generation {int(generation.id)} has a cyclic lineage"
            )
        seen.add(current_id)
        lineage.append(current_id)
        raw_parent = dict(current.source_watermarks or {}).get("parent_generation_id")
        if raw_parent is None:
            break
        try:
            parent_id = int(raw_parent)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"ledger generation {current_id} has a malformed sealed parent"
            ) from exc
        parent = db.get(models.LedgerGeneration, parent_id)
        if parent is None:
            raise ValueError(
                f"ledger generation {current_id} lost its sealed parent {parent_id}"
            )
        current = parent
    return tuple(lineage)


class RunAnchorError(ValueError):
    """A planning run is not a live obligation of the given Ledger generation."""


def sealed_run_anchor(
    db: Session,
    run: models.PlanningRun,
    generation: models.LedgerGeneration,
) -> models.LedgerGeneration:
    """Return the sealed generation ``run`` is anchored to, or fail closed.

    The returned generation is the one that last re-anchored the obligation —
    an ``obligation_refresh``, or the historical bootstrap.  Every fact-only
    fork after it inherits the run without touching it, so the run's frozen
    ``ledger_cutoff`` must equal that anchor's cutoff and *not* the cutoff of
    the generation being read.  A run anchored outside the lineage belongs to
    another branch of truth and is rejected rather than silently read.
    """
    anchor_id = run.ledger_generation_id
    if anchor_id is None:
        raise RunAnchorError(
            f"planning run {int(run.run_id)} has no Ledger generation"
        )
    lineage = sealed_generation_lineage_ids(db, generation)
    if int(anchor_id) not in lineage:
        raise RunAnchorError(
            f"planning run {int(run.run_id)} is anchored to Ledger generation "
            f"{int(anchor_id)}, which is outside the sealed lineage of "
            f"generation {int(generation.id)}"
        )
    anchor = db.get(models.LedgerGeneration, int(anchor_id))
    if anchor is None:
        raise RunAnchorError(
            f"planning run {int(run.run_id)} lost its anchor Ledger generation "
            f"{int(anchor_id)}"
        )
    if not _same_instant(run.ledger_cutoff, anchor.cutoff):
        raise RunAnchorError(
            f"planning run {int(run.run_id)} cutoff differs from the cutoff of "
            f"its anchor Ledger generation {int(anchor_id)}"
        )
    return anchor


def _inherited_run_ids(
    db: Session,
    generation: models.LedgerGeneration,
) -> tuple[int, ...]:
    """Live runs anchored anywhere in this generation's sealed lineage.

    An obligation refresh re-anchors every live run onto the generation it
    publishes (retained runs are moved, candidates are created there), so this
    is exactly the scope the last obligation refresh sealed, carried forward
    through the physical refreshes that followed it and reduced by whatever has
    since been closed or retired.
    """
    lineage = sealed_generation_lineage_ids(db, generation)
    rows = (
        db.query(models.PlanningRun.run_id, models.PlanningRun.source_plan_id)
        .filter(
            models.PlanningRun.ledger_generation_id.in_(lineage),
            models.PlanningRun.status == "FIXED_SNAPSHOT",
        )
        .order_by(models.PlanningRun.run_id.asc())
        .all()
    )
    by_plan: dict[int, int] = {}
    for run_id, source_plan_id in rows:
        if source_plan_id is None:
            continue
        plan_id = int(source_plan_id)
        if plan_id in by_plan:
            raise ValueError(
                f"live-plan scope has two live runs for plan {plan_id}: "
                f"{by_plan[plan_id]} and {int(run_id)}"
            )
        by_plan[plan_id] = int(run_id)
    return tuple(int(run_id) for run_id, _ in rows)


def live_plan_run_ids(
    db: Session,
    generation: models.LedgerGeneration,
) -> tuple[int, ...]:
    """Return live run IDs, including retained parent runs in a refresh fork."""
    marks = dict(generation.source_watermarks or {})
    if str(marks.get("generation_kind") or "") == "obligation_refresh":
        return _obligation_refresh_run_ids(marks)
    return _inherited_run_ids(db, generation)

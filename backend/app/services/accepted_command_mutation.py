"""Compact coordination metadata for commands committed on accepted truth."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy.orm import Session

from app import models


STATE_ID = 1


@dataclass(frozen=True)
class AcceptedMutationTail:
    through_epoch: int
    product_ids: tuple[int, ...]
    item_ids: tuple[int, ...]
    business_identities: tuple[str, ...]
    full_scope: bool


def _ids(values: Iterable[int]) -> list[int]:
    return sorted({int(value) for value in values})


def _identities(values: Iterable[str]) -> list[str]:
    return sorted({str(value) for value in values if str(value)})


def lock_accepted_mutation_state(
    db: Session,
) -> tuple[models.PlanningTruthState, models.AcceptedProductionMutationState]:
    """Lock order for command/publication boundaries: pointer, then epoch."""
    pointer = (
        db.query(models.PlanningTruthState)
        .filter_by(id=1)
        .populate_existing()
        .with_for_update()
        .one()
    )
    state = (
        db.query(models.AcceptedProductionMutationState)
        .filter_by(id=STATE_ID)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if state is None:
        state = models.AcceptedProductionMutationState(id=STATE_ID)
        db.add(state)
        db.flush()
    return pointer, state


def accepted_mutation_epoch(db: Session) -> int:
    state = (
        db.query(models.AcceptedProductionMutationState)
        .filter_by(id=STATE_ID)
        .populate_existing()
        .one_or_none()
    )
    if state is None:
        raise RuntimeError("accepted production mutation state is missing")
    return int(state.last_issued_epoch or 0)


def capture_accepted_mutation_epoch(db: Session) -> int:
    """Capture build basis; migration seeds PROD, create_all tests bootstrap here."""
    state = (
        db.query(models.AcceptedProductionMutationState)
        .filter_by(id=STATE_ID)
        .populate_existing()
        .one_or_none()
    )
    if state is None:
        state = models.AcceptedProductionMutationState(id=STATE_ID)
        db.add(state)
        db.flush()
    return int(state.last_issued_epoch or 0)


def record_locked_accepted_production_mutation(
    db: Session,
    *,
    pointer: models.PlanningTruthState,
    state: models.AcceptedProductionMutationState,
    mutation_kind: str,
    product_ids: Iterable[int] = (),
    item_ids: Iterable[int] = (),
    business_identities: Iterable[str] = (),
    full_scope: bool = False,
) -> int:
    if pointer.current_generation_id is None:
        raise RuntimeError("accepted mutation requires an exact truth pointer")
    epoch = int(state.last_issued_epoch or 0) + 1
    state.last_issued_epoch = epoch
    db.add(models.AcceptedProductionMutation(
        epoch=epoch,
        accepted_generation_id=int(pointer.current_generation_id),
        mutation_kind=str(mutation_kind),
        product_ids=_ids(product_ids),
        item_ids=_ids(item_ids),
        business_identities=_identities(business_identities),
        full_scope=bool(full_scope),
    ))
    db.flush()
    return epoch


def record_accepted_production_mutation(
    db: Session,
    *,
    mutation_kind: str,
    product_ids: Iterable[int] = (),
    item_ids: Iterable[int] = (),
    business_identities: Iterable[str] = (),
    full_scope: bool = False,
) -> int:
    pointer, state = lock_accepted_mutation_state(db)
    return record_locked_accepted_production_mutation(
        db, pointer=pointer, state=state, mutation_kind=mutation_kind,
        product_ids=product_ids, item_ids=item_ids,
        business_identities=business_identities, full_scope=full_scope,
    )


def stamp_external_production_metadata(
    db: Session,
    *,
    mutation_kind: str,
    product_ids: Iterable[int] = (),
    item_ids: Iterable[int] = (),
    business_identities: Iterable[str] = (),
    full_scope: bool = False,
) -> int | None:
    """Explicit choke point immediately before a durable external receipt commit."""
    pointer = db.get(models.PlanningTruthState, STATE_ID)
    if pointer is None or pointer.current_generation_id is None:
        return None
    return record_accepted_production_mutation(
        db,
        mutation_kind=mutation_kind,
        product_ids=product_ids,
        item_ids=item_ids,
        business_identities=business_identities,
        full_scope=full_scope,
    )


def accepted_mutation_tail(db: Session, *, after_epoch: int) -> AcceptedMutationTail:
    through = accepted_mutation_epoch(db)
    rows = (
        db.query(models.AcceptedProductionMutation)
        .filter(models.AcceptedProductionMutation.epoch > int(after_epoch))
        .filter(models.AcceptedProductionMutation.epoch <= through)
        .order_by(models.AcceptedProductionMutation.epoch.asc())
        .all()
    )
    return AcceptedMutationTail(
        through_epoch=through,
        product_ids=tuple(sorted({int(value) for row in rows for value in row.product_ids or []})),
        item_ids=tuple(sorted({int(value) for row in rows for value in row.item_ids or []})),
        business_identities=tuple(sorted({str(value) for row in rows for value in row.business_identities or []})),
        full_scope=any(bool(row.full_scope) for row in rows),
    )


def consume_accepted_mutations(db: Session, *, through_epoch: int) -> None:
    """Advance compact HWM and atomically GC consumed metadata rows."""
    _pointer, state = lock_accepted_mutation_state(db)
    through = int(through_epoch)
    if through > int(state.last_issued_epoch or 0):
        raise RuntimeError("cannot consume an unissued accepted mutation epoch")
    state.last_consumed_epoch = max(int(state.last_consumed_epoch or 0), through)
    db.query(models.AcceptedProductionMutation).filter(
        models.AcceptedProductionMutation.epoch <= int(state.last_consumed_epoch)
    ).delete(synchronize_session=False)
    db.flush()


def lock_publication_epoch(
    db: Session,
    *,
    generation: models.LedgerGeneration,
    expected_parent_id: int,
) -> tuple[models.PlanningTruthState, models.AcceptedProductionMutationState, int]:
    """Final short CAS: pointer -> epoch, after every heavy rebase."""
    pointer, state = lock_accepted_mutation_state(db)
    if int(pointer.current_generation_id or 0) != int(expected_parent_id):
        raise RuntimeError("accepted pointer moved before publication")
    captured = (generation.source_watermarks or {}).get(
        "accepted_production_mutation_epoch"
    )
    if captured is None or int(captured) != int(state.last_issued_epoch or 0):
        raise RuntimeError("accepted production mutations changed after rebase")
    return pointer, state, int(captured)


def consume_locked_publication_epoch(
    db: Session,
    *,
    state: models.AcceptedProductionMutationState,
    through_epoch: int,
) -> None:
    through = int(through_epoch)
    state.last_consumed_epoch = max(int(state.last_consumed_epoch or 0), through)
    db.query(models.AcceptedProductionMutation).filter(
        models.AcceptedProductionMutation.epoch <= int(state.last_consumed_epoch)
    ).delete(synchronize_session=False)
    db.flush()

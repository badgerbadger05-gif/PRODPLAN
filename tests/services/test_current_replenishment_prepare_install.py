from sqlalchemy import event

import pytest

from app import models
from app.services.item_ledger.current_replenishment import (
    PreparedCurrentReplenishmentBasisConflict,
    install_prepared_current_replenishment,
    prepare_current_replenishment_for_accepted_generation,
    prepare_current_replenishment,
)
from app.services.item_ledger.reservation import reservation_business_identity
from tests.services.test_current_replenishment_transaction import (
    _reserves,
    _point_at,
    _supplier_typed_world,
    _world,
)


def _prepare(db_session, world=None):
    generation_id, _item_id, reservations, facts = world or _world(db_session)
    prepared = prepare_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:r4",
        source_revision=1,
        facts=facts,
        reserves=_reserves(reservations),
        complete_scope=True,
    )
    return prepared, reservations


def test_prepare_is_read_only_and_target_view_matches_allocator(db_session):
    world = _world(db_session)
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.upper())

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        prepared, reservations = _prepare(db_session, world)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)

    assert not db_session.new
    assert not db_session.dirty
    assert all("FOR UPDATE" not in statement for statement in statements)
    assert all(
        not statement.lstrip().startswith(("INSERT", "UPDATE", "DELETE"))
        for statement in statements
    )
    target = {row.reservation_id: row for row in prepared.target_reservations}
    assert set(target) == {int(row.id) for row in reservations}
    assert sum(
        row.replenishment_received_qty for row in target.values()
    ) == prepared.plan.result.allocated_qty

    result = install_prepared_current_replenishment(db_session, prepared)
    assert result.changed_pairs == len(prepared.plan.insertions)
    for reservation in reservations:
        assert reservation.replenishment_received_qty == target[int(reservation.id)].replenishment_received_qty


def test_install_rejects_reservation_basis_change_before_dml(db_session):
    prepared, reservations = _prepare(db_session)
    reservations[0].replenishment_required_qty += 1
    db_session.flush()

    with pytest.raises(
        PreparedCurrentReplenishmentBasisConflict,
        match="reservation basis changed",
    ):
        install_prepared_current_replenishment(db_session, prepared)

    assert db_session.query(models.CurrentReplenishmentState).count() == 0
    assert db_session.query(models.ReservationConsumptionAllocation).count() == 0


def test_accepted_generation_adapter_prepares_every_scope_without_dml(db_session):
    generation_id, _item_id, _reservations, _facts = _supplier_typed_world(
        db_session, prefix="accepted-prepare"
    )
    _point_at(db_session, db_session.get(models.LedgerGeneration, int(generation_id)))
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.upper())

    event.listen(db_session.bind, "before_cursor_execute", capture)
    try:
        prepared = prepare_current_replenishment_for_accepted_generation(
            db_session, generation_id=int(generation_id)
        )
    finally:
        event.remove(db_session.bind, "before_cursor_execute", capture)

    assert prepared.generation_id == int(generation_id)
    assert prepared.source_revision == int(generation_id)
    assert prepared.changes
    assert prepared.target_reservations
    assert all("FOR UPDATE" not in statement for statement in statements)
    assert all(
        not statement.lstrip().startswith(("INSERT", "UPDATE", "DELETE"))
        for statement in statements
    )
    assert db_session.query(models.CurrentReplenishmentState).count() == 0
    assert db_session.query(models.ReservationConsumptionAllocation).count() == 0


def test_retired_owner_is_outside_r4_target_basis_and_requires_close_proof(db_session):
    generation_id, _item_id, reservations, facts = _world(db_session)
    for row in reservations:
        row.current_identity = reservation_business_identity(
            int(row.requirement_id), str(row.realization_mode)
        )
        row.owner_kind = "current"
        row.is_current = True
    db_session.flush()
    first = prepare_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:r4",
        source_revision=1,
        facts=facts,
        reserves=_reserves(reservations),
        complete_scope=True,
    )
    install_prepared_current_replenishment(db_session, first)

    live, retiring = reservations
    live_identity = str(live.current_identity)
    retiring_identity = str(retiring.current_identity)
    prepared = prepare_current_replenishment(
        db_session,
        generation_id=generation_id,
        source_key="physical:r4",
        source_revision=2,
        facts=facts,
        reserves=_reserves((live,)),
        complete_scope=True,
    )
    assert {row.current_identity for row in prepared.target_reservations} == {
        live_identity
    }
    assert dict(prepared.entry_id_by_identity) == {live_identity: int(live.id)}
    assert {int(row[0]) for row in prepared.reservation_basis} == {int(live.id)}

    retiring.lifecycle_status = "closed"
    retiring.owner_kind = "legacy"
    retiring.is_current = False
    db_session.flush()
    result = install_prepared_current_replenishment(
        db_session,
        prepared,
        reservation_owner_ids={live_identity: int(live.id)},
        closed_reservation_identities=(retiring_identity,),
    )
    assert result.source_revision == 2
    assert live.replenishment_received_qty == next(
        row.replenishment_received_qty
        for row in prepared.target_reservations
        if row.current_identity == live_identity
    )

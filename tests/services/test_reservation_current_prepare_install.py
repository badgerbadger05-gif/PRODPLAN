from decimal import Decimal
from types import SimpleNamespace

import pytest

from app import models
from app.services.item_ledger.reservation import reservation_event_identity
from app.services.item_ledger.reservation_current import (
    PreparedCurrentReservationsBasisConflict,
    install_prepared_current_reservations,
    prepare_current_reservations,
    prepared_reservation_views_with_execution,
)
from tests.r11.test_r11_reservation_current_owner import _db_reservation


def _world(db):
    old_generation, owner = _db_reservation(
        db, generation_key="prepare-old", status="accepted",
    )
    target, stage = _db_reservation(
        db, generation_key="prepare-target", status="building",
    )
    stage.item_id = owner.item_id
    stage.run_id = owner.run_id
    stage.requirement_id = owner.requirement_id
    stage.current_identity = owner.current_identity
    stage.replenishment_received_qty = Decimal("3")
    stage.realized_qty = Decimal("3")
    for row, key, current in (
        (owner, "prepare-old-open", True),
        (stage, "prepare-target-open", False),
    ):
        db.add(models.ReservationEvent(
            ledger_generation_id=row.ledger_generation_id,
            reservation_id=row.id,
            item_id=row.item_id,
            event_kind="open",
            reserved_delta=row.reserved_qty,
            realized_delta=Decimal("0"),
            idempotency_key=key,
            event_identity=reservation_event_identity(
                row, event_kind="open", reserved_delta=row.reserved_qty,
                realized_delta=Decimal("0"), sle_id=None, fact_ref="",
                fact_line_ref="", match_rule="",
            ),
            origin_kind="obligation",
            is_current=current,
        ))
    db.flush()
    return old_generation, owner, target, stage


def test_prepare_current_reservations_is_read_only_and_exposes_target_view(db_session):
    _old, owner, target, stage = _world(db_session)
    dirty_before = set(db_session.dirty)
    prepared = prepare_current_reservations(
        db_session, generation_id=target.id, retire_run_ids=(),
    )

    assert set(db_session.dirty) == dirty_before
    assert owner.is_current is True and owner.owner_kind == "current"
    assert stage.is_current is False and stage.owner_kind == "building"
    view = prepared.target_reservations[0]
    assert view.current_identity == owner.current_identity
    assert view.accepted_owner_id == owner.id
    assert view.stage_reservation_id == stage.id
    assert (view.replenishment_received_qty, view.realized_qty) == (
        Decimal("3"), Decimal("3"),
    )


def test_install_rebinds_stable_owner_and_returns_identity_map(db_session):
    _old, owner, target, stage = _world(db_session)
    prepared = prepare_current_reservations(
        db_session, generation_id=target.id, retire_run_ids=(),
    )
    installed = install_prepared_current_reservations(
        db_session, prepared, retire_closed_allocations=False,
    )

    db_session.flush()
    assert installed.owner_id_by_identity == {owner.current_identity: owner.id}
    db_session.refresh(owner)
    assert owner.ledger_generation_id == target.id
    assert owner.is_current is True and owner.owner_kind == "current"
    assert owner.replenishment_received_qty == Decimal("3")
    assert db_session.get(models.ReservationEntry, stage.id) is None


def test_install_rejects_changed_accepted_basis(db_session):
    _old, owner, target, _stage = _world(db_session)
    prepared = prepare_current_reservations(
        db_session, generation_id=target.id, retire_run_ids=(),
    )
    owner.replenishment_received_qty = Decimal("1")
    owner.realized_qty = Decimal("1")
    db_session.flush()

    with pytest.raises(
        PreparedCurrentReservationsBasisConflict,
        match="accepted reservation basis changed",
    ):
        install_prepared_current_reservations(
            db_session, prepared, retire_closed_allocations=False,
        )


def test_install_allows_prepared_target_to_be_marked_accepted_before_pointer(db_session):
    old, owner, target, _stage = _world(db_session)
    db_session.add(models.PlanningTruthState(
        id=1, current_generation_id=old.id,
    ))
    db_session.flush()
    prepared = prepare_current_reservations(
        db_session, generation_id=target.id, retire_run_ids=(),
    )
    target.status = "accepted"
    db_session.flush()

    installed = install_prepared_current_reservations(
        db_session, prepared, retire_closed_allocations=False,
    )
    assert installed.owner_id_by_identity == {owner.current_identity: owner.id}


def test_r4_execution_overlay_changes_only_execution_counters(db_session):
    _old, _owner, target, _stage = _world(db_session)
    prepared = prepare_current_reservations(
        db_session, generation_id=target.id, retire_run_ids=(),
    )
    owner = prepared.target_reservations[0]
    execution = SimpleNamespace(
        **{
            field: getattr(owner, field)
            for field in (
                "current_identity", "item_id", "run_id", "requirement_id",
                "realization_mode", "planning_stock_pool", "reserved_qty",
                "covered_from_stock_at_freeze_qty",
                "replenishment_required_qty",
            )
        },
        replenishment_received_qty=Decimal("4"),
        realized_qty=Decimal("4"),
    )
    overlaid = prepared_reservation_views_with_execution(
        prepared, [execution],
    )[0]
    assert (overlaid.replenishment_received_qty, overlaid.realized_qty) == (
        Decimal("4"), Decimal("4"),
    )
    assert (overlaid.requirement_id, overlaid.reserved_qty) == (
        owner.requirement_id, owner.reserved_qty,
    )


def test_prepare_target_views_include_retained_current_owner(db_session):
    _old, owner, target, _stage = _world(db_session)
    retained_generation, retained = _db_reservation(
        db_session, generation_key="prepare-retained", status="accepted",
    )
    prepared = prepare_current_reservations(
        db_session,
        generation_id=target.id,
        retire_run_ids=(owner.run_id,),
    )
    targets = {row.current_identity: row for row in prepared.target_reservations}
    assert set(targets) == {owner.current_identity, retained.current_identity}
    assert targets[retained.current_identity].reservation_id == retained.id

    installed = install_prepared_current_reservations(
        db_session, prepared, retire_closed_allocations=False,
    )
    assert installed.owner_id_by_identity[retained.current_identity] == retained.id
    db_session.refresh(retained)
    assert retained.ledger_generation_id == retained_generation.id
    assert retained.is_current is True


def test_prepare_target_views_exclude_owner_classified_for_close(db_session):
    _old, owner, target, _stage = _world(db_session)
    _generation, closing = _db_reservation(
        db_session, generation_key="prepare-closing", status="accepted",
    )
    closing.run_id = None
    db_session.flush()
    prepared = prepare_current_reservations(
        db_session, generation_id=target.id, retire_run_ids=None,
    )
    identities = {row.current_identity for row in prepared.target_reservations}
    assert owner.current_identity in identities
    assert closing.current_identity not in identities
    assert prepared.retiring_identities == (closing.current_identity,)
    installed = install_prepared_current_reservations(
        db_session, prepared, retire_closed_allocations=False,
    )
    assert installed.closed_identities == (closing.current_identity,)
    db_session.refresh(closing)
    assert closing.lifecycle_status == "closed"
    assert closing.is_current is False

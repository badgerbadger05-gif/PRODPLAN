from datetime import date
from decimal import Decimal

import pytest

from app.models import (
    DbrAssemblyRate,
    DbrDrumSchedule,
    DbrDrumSlot,
    Item,
    ProductionResource,
)
from app.services.dbr import settings_service, slot_service


def _slot_scenario(db):
    settings = settings_service.get_or_create_settings(db)
    settings.frozen_days = 0
    source = ProductionResource(resource_name="Сборка A", capacity=1)
    target = ProductionResource(resource_name="Сборка B", capacity=1)
    item = Item(item_code="SLED", item_name="Снегоход")
    db.add_all([source, target, item])
    db.flush()
    db.add(
        DbrAssemblyRate(
            resource_id=source.resource_id,
            item_id=item.item_id,
            qty_per_capacity=10,
        )
    )
    schedule = DbrDrumSchedule(
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="active",
    )
    db.add(schedule)
    db.flush()
    slot = DbrDrumSlot(
        schedule_id=schedule.id,
        slot_date=date(2026, 8, 10),
        planned_date=date(2026, 8, 10),
        resource_id=source.resource_id,
        item_id=item.item_id,
        qty=Decimal("2"),
    )
    db.add(slot)
    db.flush()
    return slot, source, target, item


def test_move_slot_rejects_unassigned_target_resource(db_session):
    slot, source, target, _item = _slot_scenario(db_session)

    with pytest.raises(ValueError, match="не назначено"):
        slot_service.move_slot(
            db_session,
            slot.id,
            date(2026, 8, 11),
            new_resource_id=target.resource_id,
            today=date(2026, 8, 1),
        )

    assert slot.resource_id == source.resource_id
    assert slot.slot_date == date(2026, 8, 10)


def test_move_slot_allows_assigned_target_resource(db_session):
    slot, _source, target, item = _slot_scenario(db_session)
    db_session.query(DbrAssemblyRate).filter(
        DbrAssemblyRate.item_id == item.item_id
    ).delete(synchronize_session=False)
    db_session.add(
        DbrAssemblyRate(
            resource_id=target.resource_id,
            item_id=item.item_id,
            qty_per_capacity=10,
        )
    )
    db_session.flush()

    result = slot_service.move_slot(
        db_session,
        slot.id,
        date(2026, 8, 11),
        new_resource_id=target.resource_id,
        today=date(2026, 8, 1),
    )

    assert result["moved"] is True
    assert slot.resource_id == target.resource_id


def test_move_slot_date_only_does_not_require_new_assignment(db_session):
    slot, source, _target, _item = _slot_scenario(db_session)

    result = slot_service.move_slot(
        db_session,
        slot.id,
        date(2026, 8, 11),
        today=date(2026, 8, 1),
    )

    assert result["moved"] is True
    assert slot.resource_id == source.resource_id

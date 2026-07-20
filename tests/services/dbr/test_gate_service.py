"""Tests for the DBR kit-completeness gate (gate_service.py)."""

from datetime import date
from decimal import Decimal

from app.models import (
    DbrAssemblyRate,
    DefaultSpecification,
    Item,
    ItemWarehouseStock,
    ProductionResource,
    SpecComponent,
    Specification,
    StockWarehouse,
)
from app.services.dbr import drum_service, gate_service, program_service, settings_service

W2 = "REF-W2"
W3 = "REF-W3"
W4 = "REF-W4"
BUILD_DAY = date(2026, 8, 3)


def _scenario(db, bolt_stock):
    # Settings: W4 shelf configured, no fasteners.
    settings = settings_service.get_or_create_settings(db)
    settings.w2_warehouse_ref1c = W2
    settings.w3_warehouse_ref1c = W3
    settings.w4_warehouse_ref1c = W4
    settings.gate_horizon_workdays = 10
    db.add(StockWarehouse(warehouse_ref1c=W4, warehouse_name="Склад №4", is_selected=True))

    res = ProductionResource(resource_name="Сборка", capacity=1)
    sled = Item(item_code="SLED", item_name="Снегоход")
    bolt = Item(item_code="BOLT", item_name="Болт", replenishment_method="Закупка")
    db.add_all([res, sled, bolt])
    db.flush()

    db.add(DbrAssemblyRate(resource_id=res.resource_id, item_id=sled.item_id, qty_per_capacity=10))
    spec = Specification(spec_name="Спека снегохода", spec_ref1c="S-SLED")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=sled.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=bolt.item_id, quantity=Decimal("4")))
    if bolt_stock:
        db.add(ItemWarehouseStock(item_id=bolt.item_id, warehouse_ref1c=W4, qty=bolt_stock))
    db.flush()

    program = program_service.create_program(
        db,
        from_date=date(2026, 8, 1),
        to_date=date(2026, 8, 31),
        items=[{"item_id": sled.item_id, "program_date": BUILD_DAY, "qty": Decimal("2")}],
    )
    db.flush()
    program_service.approve_program(db, program.id)
    schedule, _ = drum_service.build_schedule(db, program.id)
    drum_service.activate(db, schedule.id)
    db.flush()
    return schedule


def test_gate_green_when_stock_covers(db_session):
    db = db_session
    schedule = _scenario(db, bolt_stock=100)  # need 2 x 4 = 8
    result = gate_service.refresh_gate(db, schedule.id, today=BUILD_DAY)
    db.commit()
    assert result["green"] >= 1
    assert result["red"] == 0
    assert all(s.kit_status == "green" for s in schedule.slots)


def test_gate_red_and_shortage_when_missing(db_session):
    db = db_session
    schedule = _scenario(db, bolt_stock=0)
    result = gate_service.refresh_gate(db, schedule.id, today=BUILD_DAY)
    db.commit()
    assert result["red"] >= 1
    reds = [s for s in schedule.slots if s.kit_status == "red"]
    assert reds
    shortage = reds[0].shortage_json
    assert shortage and shortage[0]["item"] == "BOLT"


def test_gate_fails_closed_when_required_warehouse_role_is_missing(db_session):
    db = db_session
    schedule = _scenario(db, bolt_stock=100)
    settings = settings_service.get_or_create_settings(db)
    settings.w3_warehouse_ref1c = None
    statuses_before = [s.kit_status for s in schedule.slots]

    try:
        gate_service.refresh_gate(db, schedule.id, today=BUILD_DAY)
    except ValueError as exc:
        assert "склад №3 (W3)" in str(exc)
    else:
        raise AssertionError("gate must not evaluate with missing warehouse roles")

    assert [s.kit_status for s in schedule.slots] == statuses_before


def test_gate_no_slots_in_horizon_is_noop(db_session):
    db = db_session
    schedule = _scenario(db, bolt_stock=100)
    # today far before the build day -> slot outside the 10-workday horizon
    result = gate_service.refresh_gate(db, schedule.id, today=date(2026, 1, 1))
    assert result == {"updated": 0, "green": 0, "yellow": 0, "red": 0, "notes": []}

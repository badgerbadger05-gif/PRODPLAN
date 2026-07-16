from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Session

from app.models import (
    DbrDrumSchedule,
    DbrDrumSlot,
    DbrSupermarketPosition,
    DefaultSpecification,
    Item,
    ItemCategory,
    ItemWarehouseStock,
    Operation,
    ProductionStage,
    ProductionResource,
    SpecComponent,
    SpecOperation,
    Specification,
)
from app.services.dbr import adapters, feeder_position_service, settings_service

W2, W3, W4 = "W2", "W3", "W4"


def _scenario(db):
    settings = settings_service.get_or_create_settings(db)
    settings.w2_warehouse_ref1c = W2
    settings.w3_warehouse_ref1c = W3
    settings.w4_warehouse_ref1c = W4
    settings.fastener_categories = ["Метизы"]
    fasteners = ItemCategory(category_name="Метизы", category_ref1c="FAST")
    resource = ProductionResource(resource_name="Сборка", capacity=1)
    root = Item(item_code="ROOT", item_name="Изделие")
    middle = Item(item_code="MIDDLE", item_name="Узел")
    painted = Item(item_code="PAINT", item_name="Крашеная")
    bolt = Item(item_code="BOLT", item_name="Болт", category=fasteners)
    db.add_all([fasteners, resource, root, middle, painted, bolt])
    db.flush()
    root_spec = Specification(spec_name="Root", spec_ref1c="SP-ROOT")
    middle_spec = Specification(spec_name="Middle", spec_ref1c="SP-MIDDLE")
    paint_spec = Specification(spec_name="Paint", spec_ref1c="SP-PAINT")
    db.add_all([root_spec, middle_spec, paint_spec])
    db.flush()
    db.add_all(
        [
            DefaultSpecification(item_id=root.item_id, spec_id=root_spec.spec_id),
            DefaultSpecification(item_id=middle.item_id, spec_id=middle_spec.spec_id),
            DefaultSpecification(item_id=painted.item_id, spec_id=paint_spec.spec_id),
            SpecComponent(spec_id=root_spec.spec_id, item_id=middle.item_id, quantity=2),
            SpecComponent(spec_id=root_spec.spec_id, item_id=bolt.item_id, quantity=100),
            SpecComponent(spec_id=middle_spec.spec_id, item_id=painted.item_id, quantity=3),
            ItemWarehouseStock(item_id=painted.item_id, warehouse_ref1c=W3, qty=0),
        ]
    )
    schedule = DbrDrumSchedule(
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31), status="active"
    )
    db.add(schedule)
    db.flush()
    db.add_all(
        [
            DbrDrumSlot(
                schedule_id=schedule.id, slot_date=date(2026, 8, 3), planned_date=date(2026, 8, 3),
                resource_id=resource.resource_id, item_id=root.item_id, qty=4,
            ),
            DbrDrumSlot(
                schedule_id=schedule.id, slot_date=date(2026, 8, 4), planned_date=date(2026, 8, 4),
                resource_id=resource.resource_id, item_id=root.item_id, qty=6,
            ),
        ]
    )
    db.flush()
    return schedule, root, middle, painted


def test_preview_daily_rates_multilevel_fastener_and_zero_stock_w3(db_session):
    schedule, _root, _middle, _painted = _scenario(db_session)

    preview = feeder_position_service.preview_positions(db_session, schedule.id)

    assert preview["daily_rates"] == {"ROOT": 5.0}
    by_code = {row["item_code"]: row for row in preview["positions"]}
    assert "BOLT" not in by_code
    assert by_code["MIDDLE"]["warehouse_ref1c"] == W4
    assert by_code["MIDDLE"]["adu"] == 10.0
    assert by_code["PAINT"]["warehouse_ref1c"] == W3
    assert by_code["PAINT"]["adu"] == 30.0
    assert by_code["PAINT"]["route_class"] == "painting"
    assert by_code["PAINT"]["supply_risk_pct"] == 0
    assert by_code["PAINT"]["calculation_snapshot"]["schedule_id"] == schedule.id
    assert by_code["PAINT"]["calculated_at"] is not None


def test_rebuild_is_idempotent_across_new_session_and_deactivates_missing(db_session):
    schedule, root, _middle, _painted = _scenario(db_session)
    first = feeder_position_service.rebuild_positions(
        db_session, schedule.id, expected_schedule_id=schedule.id
    )
    db_session.commit()
    assert first["created"] == 2
    stale = DbrSupermarketPosition(
        item_id=root.item_id, warehouse_ref1c="OLD", supply_type="manufacture",
        mode="shelf", adu=1, commonality=1, rt_days=1, batch_days=1, q_batch=1,
        k_var=Decimal("0.5"), red_qty=1, yellow_qty=1, green_qty=1, target_qty=3,
        is_active=True, is_stale=False,
    )
    db_session.add(stale)
    db_session.commit()

    fresh = Session(bind=db_session.get_bind())
    try:
        second = feeder_position_service.rebuild_positions(
            fresh, schedule.id, expected_schedule_id=schedule.id
        )
        fresh.commit()
        assert second["created"] == 0
        assert second["updated"] == 2
        assert second["deactivated"] == 1
        rows = fresh.query(DbrSupermarketPosition).all()
        assert len(rows) == 3
        assert sum(row.is_active for row in rows) == 2
        assert next(row for row in rows if row.warehouse_ref1c == "OLD").is_stale
    finally:
        fresh.close()


def test_rebuild_expected_schedule_guard(db_session):
    schedule, *_ = _scenario(db_session)
    with pytest.raises(ValueError, match="активный график изменился"):
        feeder_position_service.rebuild_positions(
            db_session, schedule.id, expected_schedule_id=schedule.id + 1
        )


def test_preview_fails_whole_on_bom_cycle(db_session):
    schedule, root, middle, _painted = _scenario(db_session)
    middle_spec_id = db_session.query(DefaultSpecification.spec_id).filter_by(
        item_id=middle.item_id
    ).scalar()
    db_session.add(
        SpecComponent(spec_id=middle_spec_id, item_id=root.item_id, quantity=1)
    )
    db_session.flush()

    with pytest.raises(ValueError, match="цикл BOM"):
        feeder_position_service.preview_positions(db_session, schedule.id)


def test_database_rejects_duplicate_position_from_independent_session(db_session):
    schedule, *_ = _scenario(db_session)
    feeder_position_service.rebuild_positions(db_session, schedule.id)
    db_session.commit()
    original = db_session.query(DbrSupermarketPosition).first()

    fresh = Session(bind=db_session.get_bind())
    try:
        fresh.add(
            DbrSupermarketPosition(
                item_id=original.item_id,
                warehouse_ref1c=original.warehouse_ref1c,
                supply_type="manufacture",
                mode="shelf",
                adu=1,
                commonality=1,
                rt_days=1,
                batch_days=1,
                q_batch=1,
                k_var=Decimal("0.5"),
                red_qty=1,
                yellow_qty=1,
                green_qty=1,
                target_qty=3,
            )
        )
        with pytest.raises(IntegrityError):
            fresh.commit()
    finally:
        fresh.rollback()
        fresh.close()


def test_supermarket_position_declares_domain_and_safety_checks():
    names = {
        constraint.name
        for constraint in DbrSupermarketPosition.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert {
        "ck_dbr_supermarket_position_supply_type_allowed",
        "ck_dbr_supermarket_position_mode_allowed",
        "ck_dbr_supermarket_position_rt_source_allowed",
        "ck_dbr_supermarket_position_k_var_bounded",
        "ck_dbr_supermarket_position_supply_risk_nonnegative",
    } <= names


class _RouteSettings:
    batch_days_paint_black = 2
    batch_days_turning = 10
    batch_days_bending = 7
    batch_days_welding = 5


@pytest.mark.parametrize("text", ["токарная операция", "фрезеровка"])
def test_turning_and_milling_use_machining_route_and_turning_batch(text):
    assert feeder_position_service._route_class(text, is_w2=False, is_w3=False) == "machining"
    assert feeder_position_service._batch_days(text, is_w3=False, settings=_RouteSettings()) == 10


def test_welding_and_bending_route_batch_rules():
    assert feeder_position_service._route_class("сварка", is_w2=False, is_w3=False) == "welding"
    assert feeder_position_service._batch_days("сварка", is_w3=False, settings=_RouteSettings()) == 5
    assert feeder_position_service._route_class("гибка", is_w2=False, is_w3=False) == "machining"
    assert feeder_position_service._batch_days("гибка", is_w3=False, settings=_RouteSettings()) == 7


def test_painting_precedence_and_w2_override():
    mixed = "сварка порошковая окраска"
    assert feeder_position_service._route_class(mixed, is_w2=False, is_w3=False) == "painting"
    assert feeder_position_service._route_class(mixed, is_w2=True, is_w3=False) == "machining"
    assert feeder_position_service._batch_days(mixed, is_w3=True, settings=_RouteSettings()) == 2


def test_route_text_adapter_uses_operation_and_stage_and_removes_default_warning(db_session):
    schedule, _root, middle, _painted = _scenario(db_session)
    spec_id = db_session.query(DefaultSpecification.spec_id).filter_by(item_id=middle.item_id).scalar()
    stage = ProductionStage(stage_name="Сварочный участок")
    operation = Operation(operation_name="Подготовка")
    db_session.add_all([stage, operation])
    db_session.flush()
    db_session.add(SpecOperation(spec_id=spec_id, operation_id=operation.operation_id, stage_id=stage.stage_id))
    db_session.flush()

    assert "сварочный участок" in adapters.item_route_text_map(db_session)["MIDDLE"]
    preview = feeder_position_service.preview_positions(db_session, schedule.id)
    middle_row = next(row for row in preview["positions"] if row["item_code"] == "MIDDLE")
    assert middle_row["route_class"] == "welding"
    assert middle_row["rt_days"] == 15
    assert middle_row["batch_days"] == 5
    assert middle_row["data_quality"] == []


def test_absent_operation_text_keeps_explicit_default_warning(db_session):
    schedule, _root, _middle, _painted = _scenario(db_session)
    preview = feeder_position_service.preview_positions(db_session, schedule.id)
    middle_row = next(row for row in preview["positions"] if row["item_code"] == "MIDDLE")
    assert middle_row["route_class"] == "machining"
    assert middle_row["data_quality"] == ["route_class_defaulted_no_operation_route_data"]

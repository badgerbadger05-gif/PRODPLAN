"""Фаза 4 DBR: тип снабжения «Переработка» (давальческий питатель №3).

По питатель-3-гальваника-round-trip.md:
- покрытая деталь (replenishment_method='Переработка') — граница кита, полка №4;
- голая деталь под ней в ADU-спрос не разворачивается (изготавливается под сигнал);
- зоны: RT цепочки (settings.rt_processing_days), квант = ADU × рейс-интервал;
- NFP = остаток покрытой (выбранные склады) + вся труба переработчика
  (заказы поставщику, любые назначения) + голая (остаток + в работе) − резервы.
"""
from datetime import date, datetime
from decimal import Decimal

from app.models import (
    DbrDrumSchedule,
    DbrDrumSlot,
    DbrSupermarketPosition,
    DefaultSpecification,
    Item,
    ItemWarehouseStock,
    ProductionOrder,
    ProductionProduct,
    ProductionResource,
    SpecComponent,
    Specification,
    StockWarehouse,
    SupplierOrder,
    SupplierOrderItem,
)
from app.services.dbr import feeder_nfp_service, feeder_position_service, settings_service
from app.services.dbr.classify import ItemMeta, classify_meta
from app.services.dbr.core.drum import kit as kit_mod

W2, W3, W4 = "W2", "W3", "W4"


# --------------------------------------------------------------------------
# Классификатор
# --------------------------------------------------------------------------


def test_processing_is_w4_boundary():
    d, wh, note = classify_meta(ItemMeta("COATED", is_processing=True, has_spec=True), W2, W3, W4)
    assert d == kit_mod.W4 and wh == W4 and note is None


def test_processing_without_w4_role_rejected():
    try:
        classify_meta(ItemMeta("COATED", is_processing=True), W2, W3, None)
        assert False, "ожидали ValueError"
    except ValueError as exc:
        assert "склад №4" in str(exc)


# --------------------------------------------------------------------------
# Позиции супермаркета
# --------------------------------------------------------------------------


def _processing_scenario(db):
    settings = settings_service.get_or_create_settings(db)
    settings.w2_warehouse_ref1c = W2
    settings.w3_warehouse_ref1c = W3
    settings.w4_warehouse_ref1c = W4
    resource = ProductionResource(resource_name="Сборка", capacity=1)
    root = Item(item_code="ROOT", item_name="Изделие")
    coated = Item(
        item_code="COATED",
        item_name="Замок, гальваника",
        replenishment_method="Переработка",
    )
    bare = Item(item_code="BARE", item_name="Замок голый", replenishment_method="Производство")
    db.add_all([resource, root, coated, bare])
    db.flush()
    root_spec = Specification(spec_name="Root", spec_ref1c="SP-ROOT")
    coated_spec = Specification(spec_name="Coated", spec_ref1c="SP-COATED")
    db.add_all([root_spec, coated_spec])
    db.flush()
    db.add_all(
        [
            DefaultSpecification(item_id=root.item_id, spec_id=root_spec.spec_id),
            DefaultSpecification(item_id=coated.item_id, spec_id=coated_spec.spec_id),
            SpecComponent(spec_id=root_spec.spec_id, item_id=coated.item_id, quantity=2),
            # спека покрытой: голая («Сборка») + услуга покрытия («Расход»)
            SpecComponent(
                spec_id=coated_spec.spec_id,
                item_id=bare.item_id,
                quantity=1,
                component_type="Сборка",
            ),
        ]
    )
    schedule = DbrDrumSchedule(
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31), status="active"
    )
    db.add(schedule)
    db.flush()
    db.add(
        DbrDrumSlot(
            schedule_id=schedule.id, slot_date=date(2026, 8, 3), planned_date=date(2026, 8, 3),
            resource_id=resource.resource_id, item_id=root.item_id, qty=5,
        )
    )
    db.flush()
    return schedule, coated, bare


def test_processing_position_zones_and_no_bare_explosion(db_session):
    schedule, coated, bare = _processing_scenario(db_session)

    preview = feeder_position_service.preview_positions(db_session, schedule.id)

    by_code = {row["item_code"]: row for row in preview["positions"]}
    row = by_code["COATED"]
    assert row["supply_type"] == "processing"
    assert row["warehouse_ref1c"] == W4
    assert row["rt_source"] == "chain"
    assert row["route_class"] is None
    # ADU = 5/день × 2 шт = 10; RT=25 (дефолт), рейс-интервал 7 (дефолт)
    assert row["adu"] == 10.0
    assert row["rt_days"] == 25.0
    assert row["batch_days"] == 7.0
    assert row["green_qty"] == 70.0    # ADU × рейс-интервал
    assert row["yellow_qty"] == 250.0  # ADU × RT
    assert row["red_qty"] == 125.0     # ADU × RT × k_var(0.5)
    assert row["q_batch"] == 70.0      # квант = зелёная (рейс-кратность)
    # голая деталь под сигнал, а не в ADU-спрос
    assert "BARE" not in by_code


def test_processing_rt_settings_override(db_session):
    schedule, *_ = _processing_scenario(db_session)
    settings = settings_service.get_or_create_settings(db_session)
    settings.rt_processing_days = 20
    settings.processing_trip_interval_days = 5
    db_session.flush()

    preview = feeder_position_service.preview_positions(db_session, schedule.id)

    row = {r["item_code"]: r for r in preview["positions"]}["COATED"]
    assert row["rt_days"] == 20.0
    assert row["batch_days"] == 5.0
    assert row["green_qty"] == 50.0
    assert row["yellow_qty"] == 200.0


# --------------------------------------------------------------------------
# NFP
# --------------------------------------------------------------------------


def _processing_position(db, item, source_schedule):
    row = DbrSupermarketPosition(
        item_id=item.item_id,
        warehouse_ref1c=W4,
        supply_type="processing",
        mode="shelf",
        adu=10,
        commonality=1,
        rt_days=25,
        batch_days=7,
        q_batch=70,
        k_var=Decimal("0.5"),
        supply_risk_pct=0,
        red_qty=125,
        yellow_qty=250,
        green_qty=70,
        target_qty=445,
        rt_source="chain",
        source_schedule_id=source_schedule.id,
        data_quality=[],
        calculation_snapshot={},
    )
    db.add(row)
    db.flush()
    return row


def test_processing_nfp_includes_pipe_and_bare_chain(db_session):
    db = db_session
    schedule, coated, bare = _processing_scenario(db)
    position = _processing_position(db, coated, schedule)
    db.add_all(
        [
            StockWarehouse(warehouse_ref1c=W4, warehouse_name="Полка 4", is_selected=True),
            StockWarehouse(warehouse_ref1c="OTHER", warehouse_name="Другой", is_selected=False),
            # покрытая: 30 на выбранном складе, 99 на невыбранном (не считается)
            ItemWarehouseStock(item_id=coated.item_id, warehouse_ref1c=W4, qty=30),
            ItemWarehouseStock(item_id=coated.item_id, warehouse_ref1c="OTHER", qty=99),
            # голая: 40 на выбранном складе
            ItemWarehouseStock(item_id=bare.item_id, warehouse_ref1c=W4, qty=40),
        ]
    )
    # труба переработчика: 25 с назначением куда угодно + 15 без назначения
    supplier = SupplierOrder(
        order_number="ЗП-1", order_date=datetime(2026, 8, 1),
        order_ref1c="proc-1", deletion_mark=False,
    )
    db.add(supplier)
    db.flush()
    db.add_all(
        [
            SupplierOrderItem(
                order_id=supplier.order_id, item_id_ref=coated.item_id, quantity=25,
                received_qty=0, remaining_qty=25, destination_warehouse_ref1c="ANYWHERE",
            ),
            SupplierOrderItem(
                order_id=supplier.order_id, item_id_ref=coated.item_id, quantity=15,
                received_qty=0, remaining_qty=15, destination_warehouse_ref1c=None,
            ),
        ]
    )
    # голая в работе: открытый производственный заказ на 20
    wo = ProductionOrder(
        order_number="ПЗ-1", order_date=datetime(2026, 8, 1),
        order_ref1c="wo-bare-1", deletion_mark=False,
    )
    db.add(wo)
    db.flush()
    db.add(
        ProductionProduct(
            order_id=wo.order_id, item_id=bare.item_id,
            quantity=20, produced_qty=0, remaining_qty=20,
        )
    )
    db.flush()

    live = feeder_nfp_service.live_nfp_rows(db, [position])[position.id]

    assert live["stock_qty"] == 30
    assert live["open_supply_qty"] == 40  # 25 + 15, NULL-назначение не деградация
    assert live["chain_supply_qty"] == 60  # голая: 40 остаток + 20 в работе
    assert live["nfp"] == 130
    assert live["formula"] == "stock_qty + open_supply_qty + chain_supply_qty - qualified_demand_qty"
    assert live["is_complete"] is True


def test_processing_nfp_flags_unresolved_bare_component(db_session):
    db = db_session
    schedule, coated, bare = _processing_scenario(db)
    # ломаем спеку: второй компонент «Сборка» — пара не разрешается однозначно
    extra = Item(item_code="EXTRA", item_name="Лишний")
    db.add(extra)
    db.flush()
    spec_id = (
        db.query(DefaultSpecification.spec_id)
        .filter(DefaultSpecification.item_id == coated.item_id)
        .scalar()
    )
    db.add(
        SpecComponent(
            spec_id=spec_id, item_id=extra.item_id, quantity=1, component_type="Сборка"
        )
    )
    position = _processing_position(db, coated, schedule)
    db.flush()

    live = feeder_nfp_service.live_nfp_rows(db, [position])[position.id]

    assert live["chain_supply_qty"] == 0
    assert "processing_bare_component_unresolved" in live["missing_reasons"]
    assert live["is_complete"] is False

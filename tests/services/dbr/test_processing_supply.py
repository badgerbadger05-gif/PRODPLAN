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
    DbrSettings,
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
    Supplier,
    SupplierOrder,
    SupplierOrderItem,
)
from app.services.dbr import feeder_nfp_service, feeder_position_service, settings_service
from app.services.dbr.classify import ItemMeta, classify_meta
from app.services.dbr.core.drum import kit as kit_mod

W2, W3, W4 = "W2", "W3", "W4"


def test_processing_board_uses_transient_defaults_without_creating_settings(db_session):
    from app.services.dbr import processing_board_service

    board = processing_board_service.processing_board(
        db_session, today=date(2026, 8, 5)
    )

    assert board["positions"] == []
    assert db_session.query(DbrSettings).count() == 0
    assert not db_session.new


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
        supplier_ref1c="CONTRACTOR-1",
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


def _processing_supplier(db):
    supplier = Supplier(
        supplier_ref1c="CONTRACTOR-1", supplier_name="Гальванический подрядчик"
    )
    db.add(supplier)
    db.flush()
    return supplier


def test_processing_nfp_includes_pipe_and_bare_chain(db_session):
    db = db_session
    schedule, coated, bare = _processing_scenario(db)
    position = _processing_position(db, coated, schedule)
    supplier_row = _processing_supplier(db)
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
        order_ref1c="proc-1", supplier_id=supplier_row.supplier_id,
        operation_name="ЗаказНаПереработку",
        is_posted=True, deletion_mark=False,
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


def test_processing_board_overdue_roundtrip_alert(db_session):
    db = db_session
    schedule, coated, bare = _processing_scenario(db)
    position = _processing_position(db, coated, schedule)
    supplier = _processing_supplier(db)
    db.add(StockWarehouse(warehouse_ref1c=W4, warehouse_name="Полка 4", is_selected=True))
    fresh = SupplierOrder(
        order_number="ЗП-СВЕЖИЙ", order_date=datetime(2026, 8, 1),
        order_ref1c="proc-fresh", supplier_id=supplier.supplier_id,
        operation_name="ЗаказНаПереработку",
        processing_transfer_date=datetime(2026, 8, 3),
        is_posted=True, deletion_mark=False,
    )
    stale = SupplierOrder(
        order_number="ЗП-ПРОСРОЧЕН", order_date=datetime(2026, 6, 20),
        order_ref1c="proc-stale", supplier_id=supplier.supplier_id,
        operation_name="ЗаказНаПереработку",
        processing_transfer_date=datetime(2026, 7, 1),
        processing_report_date=datetime(2026, 8, 4),
        is_posted=True, deletion_mark=False,
    )
    db.add_all([fresh, stale])
    db.flush()
    db.add_all(
        [
            SupplierOrderItem(
                order_id=fresh.order_id, item_id_ref=coated.item_id, quantity=10,
                received_qty=0, remaining_qty=10, destination_warehouse_ref1c=None,
            ),
            SupplierOrderItem(
                order_id=stale.order_id, item_id_ref=coated.item_id, quantity=20,
                received_qty=5, remaining_qty=15, destination_warehouse_ref1c=None,
            ),
        ]
    )
    db.flush()

    from app.services.dbr import processing_board_service

    board = processing_board_service.processing_board(db, today=date(2026, 8, 5))

    assert board["roundtrip_limit_days"] == 14
    assert board["positions_total"] == 1
    assert board["overdue_positions"] == 1
    row = board["positions"][0]
    assert row["item_code"] == "COATED"
    assert row["has_overdue"] is True
    orders = {order["order_number"]: order for order in row["open_orders"]}
    assert orders["ЗП-СВЕЖИЙ"]["overdue"] is False
    assert orders["ЗП-СВЕЖИЙ"]["age_days"] == 2
    assert orders["ЗП-СВЕЖИЙ"]["stage"] == "transferred"
    assert orders["ЗП-СВЕЖИЙ"]["transfer_date"] == "2026-08-03T00:00:00"
    assert orders["ЗП-СВЕЖИЙ"]["report_date"] is None
    assert orders["ЗП-ПРОСРОЧЕН"]["overdue"] is True
    assert orders["ЗП-ПРОСРОЧЕН"]["age_days"] == 35
    assert orders["ЗП-ПРОСРОЧЕН"]["stage"] == "reported"
    assert orders["ЗП-ПРОСРОЧЕН"]["transfer_date"] == "2026-07-01T00:00:00"
    assert orders["ЗП-ПРОСРОЧЕН"]["report_date"] == "2026-08-04T00:00:00"
    assert isinstance(orders["ЗП-ПРОСРОЧЕН"]["order_id"], int)
    assert isinstance(orders["ЗП-ПРОСРОЧЕН"]["line_id"], int)
    # NFP-разложение присутствует на борде
    assert row["open_supply_qty"] == 25
    assert row["zone"] is not None


def test_processing_board_roundtrip_kpi_is_received_qty_weighted_proxy(db_session):
    db = db_session
    schedule, coated, _bare = _processing_scenario(db)
    _processing_position(db, coated, schedule)
    supplier = _processing_supplier(db)
    orders = [
        SupplierOrder(
            order_number="DONE-2", order_date=datetime(2026, 7, 1),
            order_ref1c="done-2", supplier_id=supplier.supplier_id,
            operation_name="ЗаказНаПереработку", is_posted=True, deletion_mark=False,
            processing_transfer_date=datetime(2026, 7, 2),
            processing_report_date=datetime(2026, 7, 4),
            order_state_name="Завершён",
        ),
        SupplierOrder(
            order_number="DONE-20", order_date=datetime(2026, 7, 1),
            order_ref1c="done-20", supplier_id=supplier.supplier_id,
            operation_name="ЗаказНаПереработку", is_posted=True, deletion_mark=False,
            processing_transfer_date=datetime(2026, 7, 2),
            processing_report_date=datetime(2026, 7, 22),
            order_state_name="Завершён",
        ),
        SupplierOrder(
            order_number="BAD-DATES", order_date=datetime(2026, 7, 1),
            order_ref1c="bad-dates", supplier_id=supplier.supplier_id,
            operation_name="ЗаказНаПереработку", is_posted=True, deletion_mark=False,
            processing_transfer_date=datetime(2026, 7, 9),
            processing_report_date=datetime(2026, 7, 8),
        ),
    ]
    db.add_all(orders)
    db.flush()
    for order, qty in zip(orders, (10, 30, 5), strict=True):
        db.add(
            SupplierOrderItem(
                order_id=order.order_id, item_id_ref=coated.item_id,
                quantity=qty, received_qty=qty, remaining_qty=0,
            )
        )
    db.flush()

    from app.services.dbr import processing_board_service

    board = processing_board_service.processing_board(db, today=date(2026, 8, 5))
    kpi = board["positions"][0]["roundtrip_kpi"]
    assert kpi["eligible_rows"] == 3
    assert kpi["completed_rows"] == 2
    assert kpi["completed_orders"] == 2
    assert kpi["completed_qty"] == 40
    assert kpi["weighted_avg_days"] == 15.5
    assert kpi["max_days"] == 20
    assert kpi["within_roundtrip_rows"] == 1
    assert kpi["within_roundtrip_qty"] == 10
    assert kpi["invalid_date_rows"] == 1
    assert board["contractors"][0]["supplier_name"] == "Гальванический подрядчик"
    assert board["contractors"][0]["roundtrip_kpi"] == kpi
    assert "Proxy" in board["roundtrip_kpi_semantics"]


def test_processing_pipe_excludes_draft_and_wrong_supplier(db_session):
    db = db_session
    schedule, coated, _bare = _processing_scenario(db)
    position = _processing_position(db, coated, schedule)
    expected_supplier = _processing_supplier(db)
    wrong_supplier = Supplier(
        supplier_ref1c="ORDINARY-SUPPLIER", supplier_name="Обычный поставщик"
    )
    db.add(wrong_supplier)
    db.flush()
    orders = [
        SupplierOrder(
            order_number="VALID", order_date=datetime(2026, 8, 1),
            order_ref1c="proc-valid", supplier_id=expected_supplier.supplier_id,
            operation_name="ЗаказНаПереработку",
            is_posted=True, deletion_mark=False,
        ),
        SupplierOrder(
            order_number="ORDINARY", order_date=datetime(2026, 8, 1),
            order_ref1c="ordinary-valid", supplier_id=expected_supplier.supplier_id,
            operation_name="ЗаказПоставщику",
            is_posted=True, deletion_mark=False,
        ),
        SupplierOrder(
            order_number="DRAFT", order_date=datetime(2026, 8, 1),
            order_ref1c="proc-draft", supplier_id=expected_supplier.supplier_id,
            operation_name="ЗаказНаПереработку",
            is_posted=False, deletion_mark=False,
        ),
        SupplierOrder(
            order_number="WRONG", order_date=datetime(2026, 8, 1),
            order_ref1c="proc-wrong", supplier_id=wrong_supplier.supplier_id,
            operation_name="ЗаказНаПереработку",
            is_posted=True, deletion_mark=False,
        ),
        SupplierOrder(
            order_number="CLOSED", order_date=datetime(2026, 8, 1),
            order_ref1c="proc-closed", supplier_id=expected_supplier.supplier_id,
            operation_name="ЗаказНаПереработку",
            is_posted=True, deletion_mark=False, order_state_name="Завершён",
        ),
    ]
    db.add_all(orders)
    db.flush()
    for index, order in enumerate(orders, start=1):
        db.add(
            SupplierOrderItem(
                order_id=order.order_id, item_id_ref=coated.item_id,
                line_number=index, quantity=10, received_qty=0, remaining_qty=10,
            )
        )
    db.flush()

    from app.services.dbr import processing_board_service

    live = feeder_nfp_service.live_nfp_rows(db, [position])[position.id]
    board = processing_board_service.processing_board(db, today=date(2026, 8, 5))

    assert live["open_supply_qty"] == 10
    assert [row["order_number"] for row in board["positions"][0]["open_orders"]] == ["VALID"]
    assert board["positions"][0]["open_orders"][0]["line_number"] == 1
    assert board["positions"][0]["open_orders"][0]["stage"] == "ordered"
    assert board["positions"][0]["open_orders"][0]["age_days"] == 4


def test_processing_pipe_without_configured_supplier_is_incomplete(db_session):
    db = db_session
    schedule, coated, _bare = _processing_scenario(db)
    coated.supplier_ref1c = None
    position = _processing_position(db, coated, schedule)
    supplier = _processing_supplier(db)
    order = SupplierOrder(
        order_number="UNATTRIBUTED", order_date=datetime(2026, 8, 1),
        order_ref1c="proc-unattributed", supplier_id=supplier.supplier_id,
        is_posted=True, deletion_mark=False,
    )
    db.add(order)
    db.flush()
    db.add(
        SupplierOrderItem(
            order_id=order.order_id, item_id_ref=coated.item_id,
            quantity=10, received_qty=0, remaining_qty=10,
        )
    )
    db.flush()

    live = feeder_nfp_service.live_nfp_rows(db, [position])[position.id]

    assert live["open_supply_qty"] == 0
    assert "processing_supplier_missing" in live["missing_reasons"]
    assert live["is_complete"] is False


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

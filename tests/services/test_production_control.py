import datetime as _dt
import json
from datetime import datetime

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app.models import (
    DefaultSpecification,
    ProductionKind,
    ProductionManufacture,
    ResourceProductionKind,
    Employee,
    Item,
    MrpRequirement,
    Operation,
    PlannedOrder,
    PlannedPurchase,
    PlanningRun,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ProductionStage,
    SpecComponent,
    SpecOperation,
    StockWarehouse,
    Specification,
    SupplierOrder,
    SupplierOrderItem,
    SyncLink,
    ItemWarehouseStock,
    IgnoredWarehouse,
    WorkshopWarehouseBinding,
)
from app.routers.production_control import list_employees
from app.services.production_control_journal import (
    create_orders_from_mrp,
    create_production_orders_from_mrp_requirements,
    cancel_local_order,
    dedupe_mrp_production_orders,
    list_journal,
    update_product_quantity,
)
from app.services.production_control_material_availability import preview_materials, recalculate_production_coverage
from app.services.production_control_material_issues import create_material_issues, delete_local_material_issue, list_material_issues
from app.services.production_control_printing import render_route_sheets_html
from app.services.one_c_production_order_export import PRODUCTION_ORDER_ENTITY


def test_list_employees_returns_active_synced_employees(db_session):
    db_session.add_all([
        Employee(
            employee_ref1c="11111111-1111-1111-1111-111111111111",
            employee_code="0001",
            employee_name="Иванов Иван",
            deletion_mark=False,
        ),
        Employee(
            employee_ref1c="22222222-2222-2222-2222-222222222222",
            employee_code="0002",
            employee_name="Петров Петр",
            deletion_mark=True,
        ),
    ])
    db_session.commit()

    result = list_employees(db=db_session)

    assert result["total"] == 1
    assert result["rows"][0]["employee_name"] == "Иванов Иван"
    assert result["rows"][0]["employee_ref1c"] == "11111111-1111-1111-1111-111111111111"


def test_route_sheet_includes_material_transfer_route(db_session):
    item = Item(
        item_code="P-ROUTE",
        item_name="Деталь маршрута",
        item_article="ART-ROUTE",
        unit="шт",
        status="active",
    )
    workshop = ProductionResource(resource_name="Сварочный участок")
    source_wh = StockWarehouse(
        warehouse_ref1c="src-route",
        warehouse_code="SRC",
        warehouse_name="Склад металла",
    )
    dest_wh = StockWarehouse(
        warehouse_ref1c="dst-route",
        warehouse_code="DST",
        warehouse_name="Склад сварки",
    )
    db_session.add_all([item, workshop, source_wh, dest_wh])
    db_session.flush()

    order = ProductionOrder(
        order_number="MRP-ROUTE",
        order_date=datetime(2026, 6, 4),
        deletion_mark=False,
        source="mrp",
    )
    db_session.add(order)
    db_session.flush()
    spec = Specification(spec_name="Спецификация маршрута", spec_ref1c="spec-route")
    stage = ProductionStage(stage_name="Заготовка", stage_order=1)
    operation = Operation(operation_ref1c="op-route", operation_name="Шлифовка, заготовка")
    db_session.add_all([spec, stage, operation])
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db_session.add(
        SpecOperation(
            spec_id=spec.spec_id,
            stage_id=stage.stage_id,
            operation_id=operation.operation_id,
            time_norm=0.005,
        )
    )
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="to_move",
            workshop_id=workshop.resource_id,
        )
    )
    db_session.add(
        ProductionMaterialIssue(
            document_number="MT000000123",
            product_id=product.product_id,
            order_id=order.order_id,
            status="requested",
            direction="issue",
            source_warehouse_ref1c="src-route",
            warehouse_ref1c="dst-route",
        )
    )
    db_session.commit()

    html = render_route_sheets_html(db_session, [product.product_id])

    assert "Маршрут перемещения материалов" in html
    assert "Сварочный участок" in html
    assert "MT000000123" in html
    assert "Склад металла" in html
    assert "Склад сварки" in html
    assert "@page { size: A4 portrait; margin: 5mm; }" in html
    assert "Материал выдан:" in html
    assert "Материал получен:" in html
    assert "ФИО, подпись, дата" in html
    assert "Предъявлено" in html
    assert "Несоотв." in html
    assert "Годн." in html
    assert "Клеймо, ФИО, подпись, дата" in html


def test_route_sheet_highlights_warehouse_conflict_and_key_values(db_session):
    product_item = Item(
        item_code="P-HL",
        item_name="Деталь подсветки",
        item_article="ART-P-HL",
        unit="шт",
        status="active",
    )
    component = Item(
        item_code="C-HL",
        item_name="Материал на двух складах",
        item_article="ART-C-HL",
        unit="шт",
        status="active",
    )
    workshop = ProductionResource(resource_name="Слесарный участок")
    source_wh = StockWarehouse(warehouse_ref1c="src-hl", warehouse_code="SRC-HL", warehouse_name="Склад основной")
    second_wh = StockWarehouse(warehouse_ref1c="src-hl-2", warehouse_code="SRC-HL-2", warehouse_name="Склад дубль")
    ignored_wh = StockWarehouse(warehouse_ref1c="ignored-hl", warehouse_code="IGN-HL", warehouse_name="Склад участка")
    dest_wh = StockWarehouse(warehouse_ref1c="dst-hl", warehouse_code="DST-HL", warehouse_name="Склад участка получатель")
    spec = Specification(spec_name="Спецификация подсветки", spec_ref1c="spec-hl")
    stage = ProductionStage(stage_name="Сборочный участок", stage_order=1)
    operation = Operation(operation_ref1c="op-hl", operation_name="Собрать")
    db_session.add_all([
        product_item,
        component,
        workshop,
        source_wh,
        second_wh,
        ignored_wh,
        dest_wh,
        spec,
        stage,
        operation,
    ])
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=product_item.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=1))
    db_session.add(SpecOperation(spec_id=spec.spec_id, stage_id=stage.stage_id, operation_id=operation.operation_id, time_norm=1))
    db_session.add(IgnoredWarehouse(warehouse_ref1c="ignored-hl", warehouse_name="Склад участка"))
    db_session.add_all([
        ItemWarehouseStock(item_id=component.item_id, warehouse_ref1c="src-hl", qty=3),
        ItemWarehouseStock(item_id=component.item_id, warehouse_ref1c="src-hl-2", qty=4),
        ItemWarehouseStock(item_id=component.item_id, warehouse_ref1c="ignored-hl", qty=5),
    ])

    order = ProductionOrder(
        order_number="MRP-HL",
        order_date=datetime(2026, 6, 4),
        deletion_mark=False,
        source="mrp",
    )
    db_session.add(order)
    db_session.flush()
    db_session.add(
        SyncLink(
            source_system="PRODPLAN",
            source_doctype="production_order",
            source_id=order.order_id,
            target_entity=PRODUCTION_ORDER_ENTITY,
            target_number="1C-HL-777",
            status="success",
        )
    )
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=product_item.item_id,
        line_number=1,
        quantity=2,
        produced_qty=0,
        remaining_qty=2,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, status="to_move", workshop_id=workshop.resource_id))
    db_session.add(
        ProductionMaterialIssue(
            document_number="MT-HL-1",
            product_id=product.product_id,
            order_id=order.order_id,
            status="requested",
            direction="issue",
            source_warehouse_ref1c="src-hl",
            warehouse_ref1c="dst-hl",
        )
    )
    db_session.commit()

    html = render_route_sheets_html(db_session, [product.product_id])

    assert '<strong class="warehouse-warning">проверь склады</strong>' in html
    assert '<strong class="strong-value">1C-HL-777</strong>' in html
    assert '<strong class="strong-value">ART-P-HL</strong>' in html
    assert "text strong-value'>ART-C-HL</td>" in html
    assert "text strong-value'>Слесарный участок</td>" in html
    assert "text strong-value'>Склад основной</td>" in html
    assert "text strong-value'>Склад участка получатель</td>" in html
    assert "text strong-value'>Сборочный участок</td>" in html


def test_route_sheet_printing_batches_multiple_products(db_session):
    item = Item(
        item_code="P-ROUTE-BATCH",
        item_name="Деталь пакетной печати",
        item_article="ART-BATCH",
        unit="шт",
        status="active",
    )
    component = Item(
        item_code="C-ROUTE-BATCH",
        item_name="Материал пакетной печати",
        item_article="MAT-BATCH",
        unit="кг",
        status="active",
    )
    spec = Specification(spec_name="Спецификация пакетной печати", spec_ref1c="spec-route-batch")
    stage = ProductionStage(stage_name="Сборка", stage_order=1)
    operation = Operation(operation_ref1c="op-route-batch", operation_name="Собрать")
    db_session.add_all([item, component, spec, stage, operation])
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=2))
    db_session.add(
        SpecOperation(
            spec_id=spec.spec_id,
            stage_id=stage.stage_id,
            operation_id=operation.operation_id,
            time_norm=1.5,
        )
    )

    product_ids = []
    for idx in range(3):
        order = ProductionOrder(
            order_number=f"MRP-BATCH-{idx + 1}",
            order_date=datetime(2026, 6, 4),
            deletion_mark=False,
            source="mrp",
        )
        db_session.add(order)
        db_session.flush()
        product = ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=idx + 1,
            produced_qty=0,
            remaining_qty=idx + 1,
        )
        db_session.add(product)
        db_session.flush()
        product_ids.append(product.product_id)
    db_session.commit()

    statements = []

    def before_cursor_execute(*_args):
        statements.append(1)

    event.listen(db_session.bind, "before_cursor_execute", before_cursor_execute)
    try:
        html = render_route_sheets_html(db_session, product_ids)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", before_cursor_execute)

    assert "Листов: 3" in html
    assert html.count("МАРШРУТНЫЙ ЛИСТ №") == 3
    assert "MRP-BATCH-1" in html
    assert "MRP-BATCH-2" in html
    assert "MRP-BATCH-3" in html
    assert "Материал пакетной печати" in html
    assert "Собрать" in html
    assert len(statements) <= 10


def _route_spec_to_workshop(db, spec, suffix: str) -> None:
    """Wire spec -> production kind -> workshop -> warehouse: since the stage
    fallback removal, material issues require this chain (or an explicit
    destination) to resolve the destination warehouse."""
    kind = ProductionKind(ref_1c=f"kind-{suffix}", name=f"Вид {suffix}")
    resource = ProductionResource(resource_name=f"Участок {suffix}")
    db.add_all([kind, resource])
    db.flush()
    spec.production_kind_id = kind.id
    db.add(ResourceProductionKind(resource_id=resource.resource_id, production_kind_id=kind.id))
    db.add(WorkshopWarehouseBinding(workshop_id=resource.resource_id, warehouse_ref1c=f"wh-{suffix}"))
    db.flush()


def test_journal_and_material_issue_are_scoped_to_order_line(db_session):
    parent = Item(
        item_code="P-001",
        item_name="Деталь",
        item_article="ART-P",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    component = Item(
        item_code="C-001",
        item_name="Комплектующее",
        item_article="ART-C",
        unit="м",
        stock_qty=10,
        status="active",
    )
    db_session.add_all([parent, component])
    db_session.flush()

    spec = Specification(spec_name="Спецификация детали", spec_ref1c="spec-001")
    db_session.add(spec)
    db_session.flush()
    _route_spec_to_workshop(db_session, spec, "spec-001")
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=2.5))

    order = ProductionOrder(
        order_number="1839",
        order_date=datetime(2026, 5, 18),
        order_ref1c="order-001",
        is_posted=True,
        deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=8,
        produced_qty=0,
        remaining_qty=8,
    )
    db_session.add(product)
    db_session.commit()

    journal = list_journal(db_session)
    assert journal["total"] == 1
    assert journal["rows"][0]["order_number"] == "1839"
    assert journal["rows"][0]["item_article"] == "ART-P"
    # Default per plan-aligned status set: 'shortage' until coverage is
    # evaluated (was 'new' under the legacy workshop-progress set).
    assert journal["rows"][0]["status"] == "shortage"
    # 1C-synced order: source defaults to '1c', order_ref1c populated.
    # Frontend uses these to hide the "Export to 1C" button on 1C rows.
    assert journal["rows"][0]["order_source"] == "1c"
    assert journal["rows"][0]["order_ref1c"] == "order-001"

    materials = preview_materials(db_session, product.product_id)
    assert materials["components"][0]["component_item_id"] == component.item_id
    assert materials["components"][0]["required_qty"] == 20

    created = create_material_issues(db_session, [product.product_id], initiated_by="кладовщик")
    assert len(created["created"]) == 1
    assert created["created"][0]["lines_count"] == 1

    journal_after = list_journal(db_session)
    assert journal_after["rows"][0]["issue_status"] == "requested"
    # Creating a material-issue draft moves the line from 'shortage' to
    # 'to_move' ("документы созданы, ждём проведения") per plan.
    assert journal_after["rows"][0]["status"] == "to_move"
    assert journal_after["rows"][0]["issue_count"] == 1


def test_journal_keeps_failed_1c_manufacture_visible(db_session):
    item = Item(
        item_code="ERR-P",
        item_name="Деталь с ошибкой выпуска",
        item_article="ERR-P",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    order = ProductionOrder(
        order_number="PP001308410",
        order_date=datetime(2026, 6, 8),
        order_ref1c="order-ref-error",
        is_posted=True,
        deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=140,
        produced_qty=140,
        remaining_qty=0,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(
        product_id=product.product_id,
        status="produced",
        issue_status="posted",
    ))
    db_session.add(ProductionManufacture(
        product_id=product.product_id,
        order_id=order.order_id,
        qty=140,
        status="error",
        exported_ref1c="manufacture-ref-error",
        export_error="Не удалось провести производство",
    ))
    db_session.commit()

    journal = list_journal(db_session, search="1308410")

    assert journal["total"] == 1
    row = journal["rows"][0]
    assert row["product_id"] == product.product_id
    assert row["status"] == "production_error"
    assert row["failed_manufacture_error"] == "Не удалось провести производство"


def test_journal_exposes_prodplan_number_separately_from_1c_number(db_session):
    item = Item(
        item_code="P-ORDER-NUMBERS",
        item_name="Order number item",
        item_article="ART-ORDER-NUMBERS",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    order = ProductionOrder(
        order_number="PP001204945",
        order_date=datetime(2026, 6, 4),
        order_ref1c="order-ref-1c",
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=12,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=50,
        produced_qty=0,
        remaining_qty=50,
        source_mrp_requirement_id=14014,
    )
    db_session.add(product)
    db_session.add(
        SyncLink(
            source_system="PRODPLAN",
            source_doctype="production_order",
            source_id=order.order_id,
            target_entity="Document_ЗаказНаПроизводство",
            target_ref_key="order-ref-1c",
            target_number="PP001204945",
            status="success",
        )
    )
    db_session.commit()

    row = list_journal(db_session)["rows"][0]

    assert row["order_number"] == "PP001204945"
    assert row["order_one_c_number"] == "PP001204945"
    assert row["order_prodplan_number"] == f"MRP-RC-12-{item.item_id}-{order.order_id}"


def test_journal_prodplan_numbers_are_unique_for_same_item_and_run(db_session):
    item = Item(
        item_code="P-DUP-PRODPLAN-NUMBERS",
        item_name="Duplicate display number item",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    orders = [
        ProductionOrder(
            order_number=f"PP00130000{idx}",
            order_date=datetime(2026, 6, 5),
            deletion_mark=False,
            source="mrp",
            source_run_id=13,
        )
        for idx in range(2)
    ]
    db_session.add_all(orders)
    db_session.flush()
    products = [
        ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=10 + idx,
            produced_qty=0,
            remaining_qty=10 + idx,
        )
        for idx, order in enumerate(orders)
    ]
    db_session.add_all(products)
    db_session.commit()

    rows = list_journal(db_session)["rows"]
    display_numbers = [row["order_prodplan_number"] for row in rows]

    assert len(display_numbers) == 2
    assert len(set(display_numbers)) == 2


def test_journal_can_filter_by_product_id(db_session):
    item_a = Item(item_code="P-FLT-A", item_name="Деталь A", unit="шт", stock_qty=0, status="active")
    item_b = Item(item_code="P-FLT-B", item_name="Деталь B", unit="шт", stock_qty=0, status="active")
    db_session.add_all([item_a, item_b])
    db_session.flush()

    order_a = ProductionOrder(order_number="FLT-A", order_date=datetime(2026, 5, 18), deletion_mark=False)
    order_b = ProductionOrder(order_number="FLT-B", order_date=datetime(2026, 5, 18), deletion_mark=False)
    db_session.add_all([order_a, order_b])
    db_session.flush()
    product_a = ProductionProduct(order_id=order_a.order_id, item_id=item_a.item_id, quantity=3, produced_qty=0, remaining_qty=3)
    product_b = ProductionProduct(order_id=order_b.order_id, item_id=item_b.item_id, quantity=7, produced_qty=0, remaining_qty=7)
    db_session.add_all([product_a, product_b])
    db_session.commit()

    result = list_journal(db_session, product_id=product_b.product_id)

    assert result["total"] == 1
    assert result["rows"][0]["product_id"] == product_b.product_id
    assert result["rows"][0]["order_number"] == "FLT-B"


def test_journal_exposes_mrp_requirement_coverage_fields(db_session):
    item = Item(
        item_code="MRP-COV",
        item_name="MRP coverage item",
        unit="шт",
        stock_qty=0,
        optimal_batch=12,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    run = PlanningRun(status="FIXED_SNAPSHOT", started_at=datetime(2026, 5, 27))
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=20,
        net_required_qty=20,
        covered_qty=8,
        remaining_qty=12,
        period_from=_dt.date(2026, 6, 1),
        period_to=_dt.date(2026, 6, 5),
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()

    order = ProductionOrder(
        order_number="MRP-R-COV",
        order_date=datetime(2026, 5, 27),
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=8,
        produced_qty=0,
        remaining_qty=8,
        source_mrp_requirement_id=req.id,
        source_mrp_allocation_key="MRP-REQ-1",
    )
    db_session.add(product)
    db_session.commit()

    row = list_journal(db_session, product_id=product.product_id)["rows"][0]

    assert row["order_source"] == "mrp"
    assert row["source"] == "mrp"
    assert row["source_mrp_requirement_id"] == req.id
    assert row["source_mrp_allocation_key"] == "MRP-REQ-1"
    assert row["optimal_batch"] == 12
    assert row["mrp_req_net_qty"] == 20
    assert row["mrp_req_covered_qty"] == 8
    assert row["mrp_req_remaining_qty"] == 12


def test_update_product_quantity_releases_mrp_requirement_coverage(db_session):
    item = Item(
        item_code="MRP-QTY-REL",
        item_name="MRP quantity release item",
        unit="шт",
        stock_qty=0,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    run = PlanningRun(status="FIXED_SNAPSHOT", started_at=datetime(2026, 6, 4))
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=20,
        net_required_qty=20,
        covered_qty=20,
        remaining_qty=0,
        period_from=_dt.date(2026, 6, 1),
        period_to=_dt.date(2026, 6, 30),
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    order = ProductionOrder(
        order_number="MRP-QTY-REL",
        order_date=datetime(2026, 6, 4),
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=20,
        produced_qty=0,
        remaining_qty=20,
        source_mrp_requirement_id=req.id,
    )
    db_session.add(product)
    db_session.commit()

    result = update_product_quantity(db_session, product.product_id, 12)

    db_session.refresh(req)
    db_session.refresh(product)
    assert float(product.quantity) == 12
    assert float(product.remaining_qty) == 12
    assert float(req.covered_qty) == 12
    assert float(req.remaining_qty) == 8
    assert result["mrp_req_net_qty"] == 20
    assert result["mrp_req_covered_qty"] == 12
    assert result["mrp_req_remaining_qty"] == 8


def test_update_product_quantity_refreshes_open_material_issue_lines(db_session):
    parent = Item(item_code="P-QTY-ISSUE", item_name="Parent qty issue", unit="шт", stock_qty=0, status="active")
    comp = Item(item_code="C-QTY-ISSUE", item_name="Component qty issue", unit="шт", stock_qty=100, status="active")
    db_session.add_all([parent, comp])
    db_session.flush()

    spec = Specification(spec_name="Qty issue spec", spec_ref1c="spec-qty-issue")
    db_session.add(spec)
    db_session.flush()
    _route_spec_to_workshop(db_session, spec, "qty-issue")
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))
    db_session.add(StockWarehouse(warehouse_ref1c="src-qty-issue", warehouse_code="SRC", warehouse_name="Source", is_selected=True))
    db_session.add(ItemWarehouseStock(item_id=comp.item_id, warehouse_ref1c="src-qty-issue", qty=100))

    order = ProductionOrder(order_number="QTY-ISSUE-001", order_date=datetime(2026, 6, 15), deletion_mark=False)
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=7,
        produced_qty=0,
        remaining_qty=7,
    )
    db_session.add(product)
    db_session.commit()

    created = create_material_issues(db_session, [product.product_id], initiated_by="op")
    issue_id = created["created"][0]["issue_id"]
    line = db_session.query(ProductionMaterialIssueLine).filter_by(issue_id=issue_id).one()
    assert float(line.required_qty) == 7.0

    result = update_product_quantity(db_session, product.product_id, 4)

    db_session.refresh(line)
    assert float(line.required_qty) == 4.0
    assert result["material_issues_refresh"]["updated"][0]["issue_id"] == issue_id
    assert result["material_issues_refresh"]["blocked"] == []


def test_update_product_quantity_reports_posted_material_issues_as_blocked(db_session):
    parent = Item(item_code="P-QTY-POSTED", item_name="Parent qty posted", unit="шт", stock_qty=0, status="active")
    comp = Item(item_code="C-QTY-POSTED", item_name="Component qty posted", unit="шт", stock_qty=100, status="active")
    db_session.add_all([parent, comp])
    db_session.flush()

    spec = Specification(spec_name="Qty posted spec", spec_ref1c="spec-qty-posted")
    db_session.add(spec)
    db_session.flush()
    _route_spec_to_workshop(db_session, spec, "qty-posted")
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))
    db_session.add(StockWarehouse(warehouse_ref1c="src-qty-posted", warehouse_code="SRC", warehouse_name="Source", is_selected=True))
    db_session.add(ItemWarehouseStock(item_id=comp.item_id, warehouse_ref1c="src-qty-posted", qty=100))

    order = ProductionOrder(order_number="QTY-POSTED-001", order_date=datetime(2026, 6, 15), deletion_mark=False)
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=7,
        produced_qty=0,
        remaining_qty=7,
    )
    db_session.add(product)
    db_session.commit()

    created = create_material_issues(db_session, [product.product_id], initiated_by="op")
    issue_id = created["created"][0]["issue_id"]
    issue = db_session.query(ProductionMaterialIssue).filter_by(issue_id=issue_id).one()
    issue.status = "posted"
    line = db_session.query(ProductionMaterialIssueLine).filter_by(issue_id=issue_id).one()
    line.issued_qty = line.required_qty
    line.line_status = "issued"
    db_session.commit()

    result = update_product_quantity(db_session, product.product_id, 4)

    db_session.refresh(line)
    assert float(line.required_qty) == 7.0
    assert result["material_issues_refresh"]["updated"] == []
    assert result["material_issues_refresh"]["blocked"][0]["issue_id"] == issue_id


def test_fill_remaining_creates_top_up_order_for_partially_covered_requirement(db_session):
    item = Item(
        item_code="MRP-TOPUP",
        item_name="MRP top-up item",
        unit="шт",
        stock_qty=0,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    run = PlanningRun(status="FIXED_SNAPSHOT", started_at=datetime(2026, 5, 27))
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=27,
        net_required_qty=27,
        covered_qty=4,
        remaining_qty=23,
        period_from=_dt.date(2026, 6, 1),
        period_to=_dt.date(2026, 6, 30),
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()

    order = ProductionOrder(
        order_number="PP-EXISTING",
        order_date=datetime(2026, 5, 27),
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db_session.add(order)
    db_session.flush()
    existing = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=4,
        produced_qty=0,
        remaining_qty=4,
        source_mrp_requirement_id=req.id,
        source_mrp_allocation_key=f"mrp_requirement:{req.id}:order:1",
    )
    db_session.add(existing)
    db_session.commit()

    result = create_production_orders_from_mrp_requirements(db_session, [req.id])

    assert len(result["created"]) == 1
    assert result["created"][0]["qty"] == 23
    assert result["reused"] == []
    db_session.refresh(req)
    assert float(req.covered_qty) == 27
    assert float(req.remaining_qty) == 0
    products = (
        db_session.query(ProductionProduct)
        .filter(ProductionProduct.source_mrp_requirement_id == req.id)
        .order_by(ProductionProduct.product_id.asc())
        .all()
    )
    assert [float(p.quantity) for p in products] == [4, 23]


def test_fill_remaining_splits_requirement_by_optimal_batch(db_session):
    item = Item(
        item_code="MRP-BATCH",
        item_name="MRP optimal batch item",
        unit="шт",
        stock_qty=0,
        optimal_batch=12,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    run = PlanningRun(status="FIXED_SNAPSHOT", started_at=datetime(2026, 6, 4))
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=38,
        net_required_qty=38,
        covered_qty=0,
        remaining_qty=38,
        period_from=_dt.date(2026, 6, 4),
        period_to=_dt.date(2026, 6, 8),
        bom_level=0,
    )
    db_session.add(req)
    db_session.commit()

    result = create_production_orders_from_mrp_requirements(db_session, [req.id])

    assert [float(row["qty"]) for row in result["created"]] == [12, 12, 12, 2]
    assert result["reused"] == []
    db_session.refresh(req)
    assert float(req.covered_qty) == 38
    assert float(req.remaining_qty) == 0

    products = (
        db_session.query(ProductionProduct)
        .filter(ProductionProduct.source_mrp_requirement_id == req.id)
        .order_by(ProductionProduct.product_id.asc())
        .all()
    )
    assert [float(p.quantity) for p in products] == [12, 12, 12, 2]
    assert [p.source_mrp_allocation_key for p in products] == [
        f"mrp_requirement:{req.id}:order:1",
        f"mrp_requirement:{req.id}:order:2",
        f"mrp_requirement:{req.id}:order:3",
        f"mrp_requirement:{req.id}:order:4",
    ]


def test_repeated_mrp_requirement_materialization_reuses_open_order_from_previous_run(db_session):
    item = Item(
        item_code="MRP-RERUN",
        item_name="MRP rerun item",
        unit="шт",
        stock_qty=0,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    run1 = PlanningRun(status="FIXED_SNAPSHOT", started_at=datetime(2026, 6, 4))
    db_session.add(run1)
    db_session.flush()
    req1 = MrpRequirement(
        run_id=run1.run_id,
        item_id=item.item_id,
        total_required_qty=25,
        net_required_qty=25,
        covered_qty=0,
        remaining_qty=25,
        period_from=_dt.date(2026, 6, 1),
        period_to=_dt.date(2026, 6, 30),
        bom_level=0,
    )
    db_session.add(req1)
    db_session.commit()

    first = create_production_orders_from_mrp_requirements(db_session, [req1.id])
    assert len(first["created"]) == 1

    run2 = PlanningRun(status="FIXED_SNAPSHOT", started_at=datetime(2026, 6, 4))
    db_session.add(run2)
    db_session.flush()
    req2 = MrpRequirement(
        run_id=run2.run_id,
        item_id=item.item_id,
        total_required_qty=25,
        net_required_qty=25,
        covered_qty=0,
        remaining_qty=25,
        period_from=_dt.date(2026, 6, 1),
        period_to=_dt.date(2026, 6, 30),
        bom_level=0,
    )
    db_session.add(req2)
    db_session.commit()

    second = create_production_orders_from_mrp_requirements(db_session, [req2.id])

    assert second["created"] == []
    assert len(second["reused"]) == 1
    db_session.refresh(req2)
    assert float(req2.covered_qty) == 25
    assert float(req2.remaining_qty) == 0
    assert db_session.query(ProductionProduct).filter(ProductionProduct.item_id == item.item_id).count() == 1


def test_dedupe_mrp_production_orders_cancels_local_excess_duplicates(db_session):
    item = Item(
        item_code="MRP-DEDUP",
        item_name="MRP dedupe item",
        unit="шт",
        stock_qty=0,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    products = []
    reqs = []
    for idx in (1, 2):
        run = PlanningRun(status="FIXED_SNAPSHOT", started_at=datetime(2026, 6, 4))
        db_session.add(run)
        db_session.flush()
        req = MrpRequirement(
            run_id=run.run_id,
            item_id=item.item_id,
            total_required_qty=25,
            net_required_qty=25,
            covered_qty=25,
            remaining_qty=0,
            period_from=_dt.date(2026, 6, 1),
            period_to=_dt.date(2026, 6, 30),
            bom_level=0,
        )
        db_session.add(req)
        db_session.flush()
        order = ProductionOrder(
            order_number=f"DEDUP-{idx}",
            order_date=datetime(2026, 6, 4, 12, idx),
            deletion_mark=False,
            source="mrp",
            source_run_id=run.run_id,
        )
        db_session.add(order)
        db_session.flush()
        product = ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=25,
            produced_qty=0,
            remaining_qty=25,
            source_mrp_requirement_id=req.id,
        )
        db_session.add(product)
        db_session.flush()
        db_session.add(ProductionOrderLineState(product_id=product.product_id, status="shortage"))
        products.append(product)
        reqs.append(req)
    db_session.commit()

    dry = dedupe_mrp_production_orders(db_session, dry_run=True)
    assert dry["cancelled_count"] == 1
    assert all(float(product.remaining_qty) == 25 for product in products)

    applied = dedupe_mrp_production_orders(db_session, dry_run=False)

    assert applied["cancelled_count"] == 1
    cancelled = (
        db_session.query(ProductionProduct)
        .join(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .filter(ProductionOrderLineState.status == "cancelled")
        .one()
    )
    kept = [product for product in products if product.product_id != cancelled.product_id][0]
    db_session.refresh(cancelled)
    db_session.refresh(kept)
    assert float(cancelled.remaining_qty) == 0
    assert float(kept.remaining_qty) == 25
    old_req = next(req for req in reqs if req.id == cancelled.source_mrp_requirement_id)
    db_session.refresh(old_req)
    assert float(old_req.covered_qty) == 0
    assert float(old_req.remaining_qty) == 25


def test_dedupe_mrp_production_orders_reduces_single_local_overcoverage(db_session):
    item = Item(
        item_code="MRP-REDUCE",
        item_name="MRP reduce item",
        unit="шт",
        stock_qty=10,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    run = PlanningRun(status="FIXED_SNAPSHOT", started_at=datetime(2026, 6, 8))
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=20,
        net_required_qty=6,
        covered_qty=12,
        remaining_qty=0,
        period_from=_dt.date(2026, 6, 1),
        period_to=_dt.date(2026, 6, 30),
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    order = ProductionOrder(
        order_number="REDUCE-1",
        order_date=datetime(2026, 6, 4),
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=12,
        produced_qty=0,
        remaining_qty=12,
        source_mrp_requirement_id=req.id,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, status="partial"))
    db_session.commit()

    dry = dedupe_mrp_production_orders(db_session, dry_run=True)
    assert dry["reduced_count"] == 1
    assert dry["cancelled_count"] == 0
    db_session.refresh(product)
    assert float(product.remaining_qty) == 12

    applied = dedupe_mrp_production_orders(db_session, dry_run=False)

    assert applied["reduced_count"] == 1
    assert applied["cancelled_count"] == 0
    db_session.refresh(product)
    db_session.refresh(req)
    assert float(product.quantity) == 6
    assert float(product.remaining_qty) == 6
    assert float(req.covered_qty) == 6
    assert float(req.remaining_qty) == 0


def test_dedupe_mrp_production_orders_cancels_single_local_when_requirement_zero(db_session):
    item = Item(
        item_code="MRP-ZERO",
        item_name="MRP zero item",
        unit="шт",
        stock_qty=30,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    run = PlanningRun(status="FIXED_SNAPSHOT", started_at=datetime(2026, 6, 8))
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=20,
        net_required_qty=0,
        covered_qty=10,
        remaining_qty=0,
        period_from=_dt.date(2026, 6, 1),
        period_to=_dt.date(2026, 6, 30),
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    order = ProductionOrder(
        order_number="ZERO-1",
        order_date=datetime(2026, 6, 4),
        deletion_mark=False,
        source="mrp",
        source_run_id=run.run_id,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=10,
        produced_qty=0,
        remaining_qty=10,
        source_mrp_requirement_id=req.id,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, status="shortage"))
    db_session.commit()

    result = dedupe_mrp_production_orders(db_session, dry_run=False)

    assert result["cancelled_count"] == 1
    assert result["reduced_count"] == 0
    db_session.refresh(product)
    db_session.refresh(req)
    state = db_session.query(ProductionOrderLineState).filter_by(product_id=product.product_id).one()
    assert state.status == "cancelled"
    assert float(product.remaining_qty) == 0
    assert float(req.covered_qty) == 0
    assert float(req.remaining_qty) == 0


def test_dedupe_mrp_production_orders_never_cancels_1c_open_order(db_session):
    item = Item(
        item_code="MRP-DEDUP-1C",
        item_name="MRP dedupe 1C item",
        unit="шт",
        stock_qty=0,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    products = []
    for idx, ref in enumerate(("11111111-1111-1111-1111-111111111111", None), start=1):
        run = PlanningRun(status="FIXED_SNAPSHOT", started_at=datetime(2026, 6, 4))
        db_session.add(run)
        db_session.flush()
        req = MrpRequirement(
            run_id=run.run_id,
            item_id=item.item_id,
            total_required_qty=25,
            net_required_qty=25,
            covered_qty=25,
            remaining_qty=0,
            period_from=_dt.date(2026, 6, 1),
            period_to=_dt.date(2026, 6, 30),
            bom_level=0,
        )
        db_session.add(req)
        db_session.flush()
        order = ProductionOrder(
            order_number=f"DEDUP-1C-{idx}",
            order_date=datetime(2026, 6, 4, 12, idx),
            order_ref1c=ref,
            deletion_mark=False,
            source="mrp",
            source_run_id=run.run_id,
        )
        db_session.add(order)
        db_session.flush()
        if ref:
            db_session.add(SyncLink(
                source_system="PRODPLAN",
                source_doctype="production_order",
                source_id=order.order_id,
                target_entity="Document_ЗаказНаПроизводство",
                target_ref_key=ref,
                status="success",
            ))
        product = ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=25,
            produced_qty=0,
            remaining_qty=25,
            source_mrp_requirement_id=req.id,
        )
        db_session.add(product)
        db_session.flush()
        db_session.add(ProductionOrderLineState(product_id=product.product_id, status="shortage"))
        products.append(product)
    db_session.commit()

    result = dedupe_mrp_production_orders(db_session, dry_run=False)

    assert result["cancelled_count"] == 1
    one_c_product, local_product = products
    db_session.refresh(one_c_product)
    db_session.refresh(local_product)
    assert float(one_c_product.remaining_qty) == 25
    assert float(local_product.remaining_qty) == 0
    one_c_state = db_session.query(ProductionOrderLineState).filter_by(product_id=one_c_product.product_id).one()
    local_state = db_session.query(ProductionOrderLineState).filter_by(product_id=local_product.product_id).one()
    assert one_c_state.status == "shortage"
    assert local_state.status == "cancelled"


def test_journal_splits_work_status_from_material_coverage(db_session):
    parent = Item(
        item_code="P-ASM",
        item_name="Собранная деталь",
        item_article="ASM",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db_session.add(parent)
    db_session.flush()

    order = ProductionOrder(
        order_number="ASM-001",
        order_date=datetime(2026, 5, 29),
        is_posted=True,
        deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=1,
        produced_qty=0,
        remaining_qty=1,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="assembled",
            issue_status="posted",
        )
    )
    db_session.commit()

    row = list_journal(db_session)["rows"][0]

    assert row["status"] == "ready"
    assert row["coverage_status"] == "assembled"
    assert row["coverage_label"] == "Собрано"


def test_journal_filters_by_workshop_and_coverage_before_paging(db_session):
    item = Item(item_code="P-FLT", item_name="Filter part", unit="шт", stock_qty=0, status="active")
    workshop_a = ProductionResource(resource_name="Сборка")
    workshop_b = ProductionResource(resource_name="Покраска")
    db_session.add_all([item, workshop_a, workshop_b])
    db_session.flush()

    rows = []
    for idx, (workshop, status, issue_status) in enumerate([
        (workshop_a, "shortage", "not_requested"),
        (workshop_a, "assembled", "posted"),
        (workshop_b, "assembled", "posted"),
    ], start=1):
        order = ProductionOrder(
            order_number=f"FLT-{idx}",
            order_date=datetime(2026, 5, 20),
            is_posted=True,
            deletion_mark=False,
        )
        db_session.add(order)
        db_session.flush()
        product = ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=1,
            produced_qty=0,
            remaining_qty=1,
        )
        db_session.add(product)
        db_session.flush()
        db_session.add(ProductionOrderLineState(
            product_id=product.product_id,
            status=status,
            issue_status=issue_status,
            workshop_id=workshop.resource_id,
        ))
        rows.append(product)
    db_session.commit()

    journal = list_journal(
        db_session,
        workshop_id=workshop_a.resource_id,
        coverage_status="assembled",
        limit=1,
        offset=0,
    )

    assert journal["total"] == 1
    assert [row["order_number"] for row in journal["rows"]] == ["FLT-2"]


def test_journal_status_shortage_filter_is_exact(db_session):
    item = Item(item_code="P-STAT", item_name="Status part", unit="шт", stock_qty=0, status="active")
    db_session.add(item)
    db_session.flush()

    for idx, status in enumerate(["shortage", "partial"], start=1):
        order = ProductionOrder(
            order_number=f"STAT-{idx}",
            order_date=datetime(2026, 5, 20),
            is_posted=True,
            deletion_mark=False,
        )
        db_session.add(order)
        db_session.flush()
        product = ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=1,
            produced_qty=0,
            remaining_qty=1,
        )
        db_session.add(product)
        db_session.flush()
        db_session.add(ProductionOrderLineState(product_id=product.product_id, status=status))
    db_session.commit()

    journal = list_journal(db_session, status="shortage")

    assert journal["total"] == 1
    assert [row["order_number"] for row in journal["rows"]] == ["STAT-1"]


def test_journal_without_coverage_filter_does_not_reuse_last_row_status(db_session):
    item = Item(item_code="P-COV", item_name="Coverage part", unit="шт", stock_qty=0, status="active")
    db_session.add(item)
    db_session.flush()

    for idx, status in enumerate(["ready", "partial"], start=1):
        order = ProductionOrder(
            order_number=f"COV-{idx}",
            order_date=datetime(2026, 5, 20),
            is_posted=True,
            deletion_mark=False,
        )
        db_session.add(order)
        db_session.flush()
        product = ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=1,
            produced_qty=0,
            remaining_qty=1,
        )
        db_session.add(product)
        db_session.flush()
        db_session.add(ProductionOrderLineState(product_id=product.product_id, status=status))
    db_session.commit()

    all_rows = list_journal(db_session)["rows"]
    partial_rows = list_journal(db_session, coverage_status="partial")["rows"]

    assert {row["order_number"] for row in all_rows} == {"COV-1", "COV-2"}
    assert [row["order_number"] for row in partial_rows] == ["COV-2"]


def test_journal_sorts_by_planned_start_date(db_session):
    item = Item(item_code="P-SORT", item_name="Sort part", unit="шт", stock_qty=0, status="active")
    db_session.add(item)
    db_session.flush()

    for order_number, planned_start in [
        ("SORT-LATE", _dt.date(2026, 6, 20)),
        ("SORT-EARLY", _dt.date(2026, 6, 4)),
        ("SORT-MID", _dt.date(2026, 6, 12)),
    ]:
        order = ProductionOrder(
            order_number=order_number,
            order_date=datetime(2026, 5, 20),
            is_posted=True,
            deletion_mark=False,
        )
        db_session.add(order)
        db_session.flush()
        product = ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=1,
            produced_qty=0,
            remaining_qty=1,
        )
        db_session.add(product)
        db_session.flush()
        db_session.add(ProductionOrderLineState(
            product_id=product.product_id,
            status="shortage",
            planned_start_date=planned_start,
        ))
    db_session.commit()

    asc_rows = list_journal(db_session, sort_by="planned_start_date", sort_dir="asc")["rows"]
    desc_rows = list_journal(db_session, sort_by="planned_start_date", sort_dir="desc")["rows"]

    assert [row["order_number"] for row in asc_rows] == ["SORT-EARLY", "SORT-MID", "SORT-LATE"]
    assert [row["order_number"] for row in desc_rows] == ["SORT-LATE", "SORT-MID", "SORT-EARLY"]


def _make_planned_order(db, item, qty=4) -> PlannedOrder:
    run = PlanningRun(
        status="DONE",
        config_snapshot=json.dumps({}),
    )
    db.add(run)
    db.flush()
    planned = PlannedOrder(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=qty,
        planned_qty=qty,
        qty=qty,
        need_date=_dt.date(2026, 6, 1),
        bucket_date=_dt.date(2026, 6, 1),
    )
    db.add(planned)
    db.flush()
    return planned


def test_production_order_carries_source_tagging(db_session):
    """
    Internal MRP-originated production orders must be distinguishable from
    1C-synced orders, and the planned_order they were generated from must be
    traceable from the production_products line.
    """
    item = Item(
        item_code="MRP-SRC",
        item_name="Source-tagged item",
        item_article="SRC",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    planned = _make_planned_order(db_session, item)

    order = ProductionOrder(
        order_number="PRODPLAN-0001",
        order_date=datetime(2026, 5, 20),
        order_ref1c=None,
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=planned.run_id,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=4,
        produced_qty=0,
        remaining_qty=4,
        source_planned_order_id=planned.order_id,
    )
    db_session.add(product)
    db_session.commit()

    refetched = db_session.query(ProductionOrder).filter_by(order_id=order.order_id).one()
    assert refetched.source == "mrp"
    assert refetched.source_run_id == planned.run_id
    refetched_product = db_session.query(ProductionProduct).filter_by(product_id=product.product_id).one()
    assert refetched_product.source_planned_order_id == planned.order_id


def test_partial_unique_source_planned_order_blocks_duplicates(db_session):
    """
    Idempotency rule from the plan: one PlannedOrder must not back more than
    one internal production line. The partial unique index on
    production_products(source_planned_order_id) enforces it at the DB layer.
    """
    item = Item(
        item_code="MRP-IDEM",
        item_name="Idempotency item",
        item_article="IDEM",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    planned = _make_planned_order(db_session, item)

    order_one = ProductionOrder(
        order_number="PRODPLAN-0010",
        order_date=datetime(2026, 5, 20),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=planned.run_id,
    )
    order_two = ProductionOrder(
        order_number="PRODPLAN-0011",
        order_date=datetime(2026, 5, 20),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
        source_run_id=planned.run_id,
    )
    db_session.add_all([order_one, order_two])
    db_session.flush()

    db_session.add(
        ProductionProduct(
            order_id=order_one.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=4,
            produced_qty=0,
            remaining_qty=4,
            source_planned_order_id=planned.order_id,
        )
    )
    db_session.commit()

    # Same planned_order again => IntegrityError from the partial unique index.
    db_session.add(
        ProductionProduct(
            order_id=order_two.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=4,
            produced_qty=0,
            remaining_qty=4,
            source_planned_order_id=planned.order_id,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    # NULL source_planned_order_id (1C-synced) must NOT be constrained.
    db_session.add(
        ProductionProduct(
            order_id=order_two.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=4,
            produced_qty=0,
            remaining_qty=4,
            source_planned_order_id=None,
        )
    )
    db_session.commit()


def test_create_material_issues_is_idempotent_per_product(db_session):
    """
    Re-clicking "prepare issue" must not create a duplicate draft document for
    the same production line. The second call should reuse the existing one
    and report it in `reused` rather than `created`. The partial unique index
    enforces this at the DB layer too.
    """
    parent = Item(
        item_code="P-IDEM",
        item_name="Parent",
        item_article="ART-P-IDEM",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    comp = Item(
        item_code="C-IDEM",
        item_name="Component",
        item_article="ART-C-IDEM",
        unit="м",
        stock_qty=10,
        status="active",
    )
    db_session.add_all([parent, comp])
    db_session.flush()

    spec = Specification(spec_name="Idem spec", spec_ref1c="spec-idem")
    db_session.add(spec)
    db_session.flush()
    _route_spec_to_workshop(db_session, spec, "spec-idem")
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))

    order = ProductionOrder(
        order_number="IDEM-001",
        order_date=datetime(2026, 5, 20),
        order_ref1c="order-idem",
        is_posted=True,
        deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
    )
    db_session.add(product)
    db_session.commit()

    first = create_material_issues(db_session, [product.product_id], initiated_by="op1")
    assert len(first["created"]) == 1
    assert first.get("reused", []) == []

    second = create_material_issues(db_session, [product.product_id], initiated_by="op2")
    # Same product, still in draft -> no new issue created, existing one
    # reported as reused.
    assert second["created"] == []
    assert len(second["reused"]) == 1
    assert second["reused"][0]["issue_id"] == first["created"][0]["issue_id"]

    # And only one row physically exists.
    from app.models import ProductionMaterialIssue
    assert (
        db_session.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .count()
        == 1
    )


def test_create_material_issues_reuses_exported_transfer_and_refreshes_qty(db_session):
    parent = Item(
        item_code="P-REEXP",
        item_name="Parent reexport",
        item_article="ART-P-REEXP",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    comp = Item(
        item_code="C-REEXP",
        item_name="Component reexport",
        item_article="ART-C-REEXP",
        unit="м",
        stock_qty=10,
        status="active",
    )
    db_session.add_all([parent, comp])
    db_session.flush()

    spec = Specification(spec_name="Reexport spec", spec_ref1c="spec-reexport")
    db_session.add(spec)
    db_session.flush()
    _route_spec_to_workshop(db_session, spec, "spec-reexport")
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))

    order = ProductionOrder(
        order_number="REEXP-001",
        order_date=datetime(2026, 5, 20),
        order_ref1c="order-reexport",
        is_posted=True,
        deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
    )
    db_session.add(product)
    db_session.commit()

    first = create_material_issues(db_session, [product.product_id], initiated_by="op1")
    issue_id = first["created"][0]["issue_id"]
    issue = db_session.query(ProductionMaterialIssue).filter_by(issue_id=issue_id).one()
    issue.status = "exported"
    issue.exported_ref1c = "existing-transfer-ref"
    product.quantity = 3
    product.remaining_qty = 3
    db_session.commit()

    second = create_material_issues(db_session, [product.product_id], initiated_by="op2")

    assert second["created"] == []
    assert len(second["reused"]) == 1
    assert second["reused"][0]["issue_id"] == issue_id
    assert (
        db_session.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .count()
        == 1
    )
    line = db_session.query(ProductionMaterialIssueLine).filter_by(issue_id=issue_id).one()
    assert float(line.required_qty) == 3.0


def test_create_material_issues_reuses_posted_transfer_without_duplicate(db_session):
    parent = Item(
        item_code="P-POSTED-REUSE",
        item_name="Parent posted reuse",
        item_article="ART-P-POSTED-REUSE",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    comp = Item(
        item_code="C-POSTED-REUSE",
        item_name="Component posted reuse",
        item_article="ART-C-POSTED-REUSE",
        unit="шт",
        stock_qty=10,
        status="active",
    )
    db_session.add_all([parent, comp])
    db_session.flush()

    spec = Specification(spec_name="Posted reuse spec", spec_ref1c="spec-posted-reuse")
    db_session.add(spec)
    db_session.flush()
    _route_spec_to_workshop(db_session, spec, "spec-posted-reuse")
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))

    order = ProductionOrder(
        order_number="POSTED-REUSE-001",
        order_date=datetime(2026, 6, 4),
        order_ref1c="order-posted-reuse",
        is_posted=True,
        deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
    )
    db_session.add(product)
    db_session.commit()

    first = create_material_issues(db_session, [product.product_id], initiated_by="op1")
    issue_id = first["created"][0]["issue_id"]
    issue = db_session.query(ProductionMaterialIssue).filter_by(issue_id=issue_id).one()
    issue.status = "posted"
    issue.exported_ref1c = "posted-transfer-ref"
    line = db_session.query(ProductionMaterialIssueLine).filter_by(issue_id=issue_id).one()
    line.issued_qty = line.required_qty
    line.line_status = "issued"
    db_session.commit()

    second = create_material_issues(db_session, [product.product_id], initiated_by="op2")

    assert second["created"] == []
    assert len(second["reused"]) == 1
    assert second["reused"][0]["issue_id"] == issue_id
    assert (
        db_session.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .count()
        == 1
    )


def test_create_material_issues_reuses_existing_issue_when_source_changes(db_session):
    parent = Item(item_code="P-SRC-REUSE", item_name="Parent source reuse", unit="шт", stock_qty=0, status="active")
    comp = Item(item_code="C-SRC-REUSE", item_name="Component source reuse", unit="шт", stock_qty=10, status="active")
    db_session.add_all([parent, comp])
    db_session.flush()
    spec = Specification(spec_name="Source reuse spec", spec_ref1c="spec-source-reuse")
    db_session.add(spec)
    db_session.flush()
    _route_spec_to_workshop(db_session, spec, "spec-source-reuse")
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))
    order = ProductionOrder(order_number="SRC-REUSE-001", order_date=datetime(2026, 6, 4), deletion_mark=False)
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(order_id=order.order_id, item_id=parent.item_id, line_number=1, quantity=5, produced_qty=0, remaining_qty=5)
    db_session.add(product)
    db_session.commit()

    first = create_material_issues(db_session, [product.product_id], source_warehouse_ref1c="src-a")
    second = create_material_issues(db_session, [product.product_id], source_warehouse_ref1c="src-b")

    assert len(first["created"]) == 1
    assert second["created"] == []
    assert len(second["reused"]) == 1
    assert (
        db_session.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .count()
        == 1
    )


def test_delete_local_material_issue_only_before_1c(db_session):
    parent = Item(item_code="P-DEL-ISSUE", item_name="Parent delete issue", unit="шт", stock_qty=0, status="active")
    comp = Item(item_code="C-DEL-ISSUE", item_name="Component delete issue", unit="шт", stock_qty=10, status="active")
    db_session.add_all([parent, comp])
    db_session.flush()
    spec = Specification(spec_name="Delete issue spec", spec_ref1c="spec-delete-issue")
    db_session.add(spec)
    db_session.flush()
    _route_spec_to_workshop(db_session, spec, "spec-delete-issue")
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))
    order = ProductionOrder(order_number="DEL-ISSUE-001", order_date=datetime(2026, 6, 4), deletion_mark=False)
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(order_id=order.order_id, item_id=parent.item_id, line_number=1, quantity=5, produced_qty=0, remaining_qty=5)
    db_session.add(product)
    db_session.commit()
    created = create_material_issues(db_session, [product.product_id], source_warehouse_ref1c="src-a")
    issue_id = created["created"][0]["issue_id"]

    result = delete_local_material_issue(db_session, issue_id)

    assert result["deleted"] is True
    assert db_session.query(ProductionMaterialIssue).filter_by(issue_id=issue_id).first() is None


def test_cancel_local_order_without_1c_marks_deleted_and_removes_local_issues(db_session):
    item = Item(item_code="P-DEL-ORDER", item_name="Parent delete order", unit="шт", stock_qty=0, status="active")
    comp = Item(item_code="C-DEL-ORDER", item_name="Component delete order", unit="шт", stock_qty=10, status="active")
    db_session.add_all([item, comp])
    db_session.flush()
    run = PlanningRun(status="DONE", config_snapshot=json.dumps({}))
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        net_required_qty=5,
        covered_qty=5,
        remaining_qty=0,
        period_from=_dt.date(2026, 6, 1),
        period_to=_dt.date(2026, 6, 30),
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    spec = Specification(spec_name="Delete order spec", spec_ref1c="spec-delete-order")
    db_session.add(spec)
    db_session.flush()
    _route_spec_to_workshop(db_session, spec, "spec-delete-order")
    db_session.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))
    order = ProductionOrder(order_number="MRP-RC-1-1", order_date=datetime(2026, 6, 4), deletion_mark=False, source="mrp", source_run_id=run.run_id)
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
        source_mrp_requirement_id=req.id,
    )
    db_session.add(product)
    db_session.commit()
    create_material_issues(db_session, [product.product_id], source_warehouse_ref1c="src-a")

    result = cancel_local_order(db_session, product.product_id)

    db_session.refresh(order)
    db_session.refresh(req)
    assert result["deleted_issues"] == 1
    assert order.deletion_mark is True
    assert float(req.covered_qty) == 0
    assert float(req.remaining_qty) == 5
    assert list_journal(db_session)["total"] == 0


def test_material_issue_journal_shows_warehouse_names_and_filters_source(db_session):
    parent = Item(
        item_code="P-WH",
        item_name="Warehouse parent",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db_session.add(parent)
    db_session.flush()
    order = ProductionOrder(
        order_number="PP001200001",
        order_date=datetime(2026, 5, 20),
        order_ref1c="order-wh-ref",
        is_posted=True,
        deletion_mark=False,
        source="mrp",
        source_run_id=12,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
    )
    db_session.add(product)
    db_session.add(
        SyncLink(
            source_system="PRODPLAN",
            source_doctype="production_order",
            source_id=order.order_id,
            target_entity="Document_ЗаказНаПроизводство",
            target_ref_key="order-wh-ref",
            target_number="PP001200001",
            status="success",
        )
    )
    db_session.add_all([
        StockWarehouse(
            warehouse_ref1c="src-a",
            warehouse_code="A",
            warehouse_name="Склад отправитель A",
            is_selected=True,
        ),
        StockWarehouse(
            warehouse_ref1c="src-b",
            warehouse_code="B",
            warehouse_name="Склад отправитель B",
            is_selected=True,
        ),
        StockWarehouse(
            warehouse_ref1c="dst-1",
            warehouse_code="D",
            warehouse_name="Склад получатель",
            is_selected=True,
        ),
    ])
    db_session.flush()
    db_session.add_all([
        ProductionMaterialIssue(
            document_number="MI-WH-A",
            product_id=product.product_id,
            order_id=order.order_id,
            status="requested",
            direction="issue",
            source_warehouse_ref1c="src-a",
            warehouse_ref1c="dst-1",
        ),
        ProductionMaterialIssue(
            document_number="MI-WH-B",
            product_id=product.product_id,
            order_id=order.order_id,
            status="requested",
            direction="issue",
            source_warehouse_ref1c="src-b",
            warehouse_ref1c="dst-1",
        ),
    ])
    db_session.commit()

    result = list_material_issues(db_session, source_warehouse_ref1c="src-a")

    assert result["total"] == 1
    assert result["rows"][0]["document_number"] == "MI-WH-A"
    assert result["rows"][0]["order_number"] == "PP001200001"
    assert result["rows"][0]["order_one_c_number"] == "PP001200001"
    assert result["rows"][0]["order_prodplan_number"] == f"MRP-RC-12-{parent.item_id}-{order.order_id}"
    assert result["rows"][0]["source_warehouse_name"] == "Склад отправитель A"
    assert result["rows"][0]["destination_warehouse_name"] == "Склад получатель"
    assert {row["warehouse_name"] for row in result["source_warehouses"]} == {
        "Склад отправитель A",
        "Склад отправитель B",
    }


def test_material_issue_journal_hides_completed_orders(db_session):
    item = Item(item_code="P-MI-DONE", item_name="Parent done issue", unit="шт", stock_qty=0, status="active")
    db_session.add(item)
    db_session.flush()
    active_order = ProductionOrder(order_number="MI-ACTIVE", order_date=datetime(2026, 6, 15), deletion_mark=False)
    done_order = ProductionOrder(
        order_number="MI-DONE",
        order_date=datetime(2026, 6, 15),
        deletion_mark=False,
        order_state_key="ad28565a-991b-11eb-e39a-fa163e61326a",
        order_state_name="Завершен",
    )
    db_session.add_all([active_order, done_order])
    db_session.flush()
    active_product = ProductionProduct(order_id=active_order.order_id, item_id=item.item_id, line_number=1, quantity=1, produced_qty=0, remaining_qty=1)
    done_product = ProductionProduct(order_id=done_order.order_id, item_id=item.item_id, line_number=1, quantity=1, produced_qty=0, remaining_qty=1)
    db_session.add_all([active_product, done_product])
    db_session.flush()
    db_session.add_all([
        ProductionMaterialIssue(
            document_number="MI-ACTIVE-ISSUE",
            product_id=active_product.product_id,
            order_id=active_order.order_id,
            status="exported",
            direction="issue",
        ),
        ProductionMaterialIssue(
            document_number="MI-DONE-ISSUE",
            product_id=done_product.product_id,
            order_id=done_order.order_id,
            status="exported",
            direction="issue",
        ),
    ])
    db_session.commit()

    result = list_material_issues(db_session)

    assert result["total"] == 1
    assert result["rows"][0]["document_number"] == "MI-ACTIVE-ISSUE"


def test_material_issue_journal_hides_produced_lines(db_session):
    item = Item(item_code="P-MI-PRODUCED", item_name="Parent produced issue", unit="шт", stock_qty=0, status="active")
    db_session.add(item)
    db_session.flush()
    active_order = ProductionOrder(order_number="MI-LINE-ACTIVE", order_date=datetime(2026, 6, 15), deletion_mark=False)
    produced_order = ProductionOrder(order_number="MI-LINE-PRODUCED", order_date=datetime(2026, 6, 15), deletion_mark=False)
    db_session.add_all([active_order, produced_order])
    db_session.flush()
    active_product = ProductionProduct(order_id=active_order.order_id, item_id=item.item_id, line_number=1, quantity=1, produced_qty=0, remaining_qty=1)
    produced_product = ProductionProduct(order_id=produced_order.order_id, item_id=item.item_id, line_number=1, quantity=1, produced_qty=1, remaining_qty=0)
    db_session.add_all([active_product, produced_product])
    db_session.flush()
    db_session.add_all([
        ProductionOrderLineState(product_id=active_product.product_id, status="assembled", issue_status="posted"),
        ProductionOrderLineState(product_id=produced_product.product_id, status="produced", issue_status="posted"),
        ProductionMaterialIssue(
            document_number="MI-LINE-ACTIVE-ISSUE",
            product_id=active_product.product_id,
            order_id=active_order.order_id,
            status="posted",
            direction="issue",
        ),
        ProductionMaterialIssue(
            document_number="MI-LINE-PRODUCED-ISSUE",
            product_id=produced_product.product_id,
            order_id=produced_order.order_id,
            status="posted",
            direction="issue",
        ),
    ])
    db_session.commit()

    result = list_material_issues(db_session)

    assert result["total"] == 1
    assert result["rows"][0]["document_number"] == "MI-LINE-ACTIVE-ISSUE"


def test_create_orders_from_mrp_materializes_planned_orders(db_session):
    """
    POST /v1/production-control/orders/from-mrp must turn selected
    planned_order rows into internal production orders tagged source='mrp',
    with the source_planned_order_id back-link and an initial line state.
    Second call for the same planned_orders is a no-op (reused).
    """
    item = Item(
        item_code="MRP-ITEM",
        item_name="Item from MRP",
        item_article="ART-MRP",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    run = PlanningRun(status="DONE", config_snapshot=json.dumps({}))
    db_session.add(run)
    db_session.flush()
    planned_a = PlannedOrder(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=10,
        planned_qty=10,
        qty=10,
        need_date=_dt.date(2026, 6, 1),
        start_date=_dt.date(2026, 5, 25),
        finish_date=_dt.date(2026, 5, 31),
        bucket_date=_dt.date(2026, 6, 1),
    )
    planned_b = PlannedOrder(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=4,
        planned_qty=4,
        qty=4,
        need_date=_dt.date(2026, 6, 5),
        bucket_date=_dt.date(2026, 6, 5),
    )
    db_session.add_all([planned_a, planned_b])
    db_session.commit()

    first = create_orders_from_mrp(
        db_session,
        [planned_a.order_id, planned_b.order_id],
        initiated_by="planner",
    )
    assert first["status"] == "ok"
    assert first["errors"] == []
    assert first["reused"] == []
    assert {row["planned_order_id"] for row in first["created"]} == {
        planned_a.order_id,
        planned_b.order_id,
    }
    for row in first["created"]:
        order = (
            db_session.query(ProductionOrder)
            .filter(ProductionOrder.order_id == row["order_id"])
            .one()
        )
        assert order.source == "mrp"
        assert order.source_run_id == run.run_id
        assert order.is_posted is False
        assert order.order_ref1c is None
        product = (
            db_session.query(ProductionProduct)
            .filter(ProductionProduct.product_id == row["product_id"])
            .one()
        )
        assert product.source_planned_order_id == row["planned_order_id"]
        assert float(product.quantity) == row["qty"]
        assert float(product.remaining_qty) == row["qty"]
        # ProductionOrderLineState seeded with status='shortage' per plan.
        from app.models import ProductionOrderLineState as POLS
        state = (
            db_session.query(POLS)
            .filter(POLS.product_id == product.product_id)
            .one()
        )
        assert state.status == "shortage"
        assert state.issue_status == "not_requested"

    # Second call must be a no-op.
    second = create_orders_from_mrp(
        db_session,
        [planned_a.order_id, planned_b.order_id],
    )
    assert second["created"] == []
    assert len(second["reused"]) == 2
    assert (
        db_session.query(ProductionOrder)
        .filter(ProductionOrder.source == "mrp", ProductionOrder.source_run_id == run.run_id)
        .count()
        == 2
    )


def test_create_orders_from_mrp_skips_invalid_inputs(db_session):
    """
    Bad planned_order ids and 0-qty rows must be reported as errors instead of
    aborting the whole batch.
    """
    item = Item(
        item_code="MRP-SKIP",
        item_name="Skip item",
        item_article="SKIP",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db_session.add(item)
    db_session.flush()
    run = PlanningRun(status="DONE", config_snapshot=json.dumps({}))
    db_session.add(run)
    db_session.flush()
    zero_qty = PlannedOrder(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=0,
        planned_qty=0,
        qty=0,
        need_date=_dt.date(2026, 6, 1),
        bucket_date=_dt.date(2026, 6, 1),
    )
    db_session.add(zero_qty)
    db_session.commit()

    result = create_orders_from_mrp(db_session, [zero_qty.order_id, 999_999])
    assert result["created"] == []
    assert result["reused"] == []
    assert len(result["errors"]) == 2
    # Nothing committed for the invalid batch.
    assert (
        db_session.query(ProductionOrder).filter(ProductionOrder.source == "mrp").count() == 0
    )


# ---------------------------------------------------------------------------
# Coverage evaluation in preview_materials
# ---------------------------------------------------------------------------


def _make_basic_spec(db, parent_name="Parent", child_specs=()):
    """Helper that wires Item + Specification + SpecComponents + DefaultSpecification."""
    parent = Item(
        item_code=f"P-{parent_name}",
        item_name=parent_name,
        item_article=f"ART-{parent_name}",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db.add(parent)
    db.flush()
    spec = Specification(spec_name=f"Spec {parent_name}", spec_ref1c=f"spec-{parent_name}")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))

    components: list[Item] = []
    for code, name, stock, qty_per_unit in child_specs:
        comp = Item(
            item_code=code,
            item_name=name,
            item_article=code,
            unit="м",
            stock_qty=stock,
            status="active",
        )
        db.add(comp)
        db.flush()
        db.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=qty_per_unit))
        components.append(comp)
    return parent, spec, components


def _make_internal_order_for(db, parent, qty=2):
    order = ProductionOrder(
        order_number=f"COV-{parent.item_id}",
        order_date=datetime(2026, 5, 20),
        is_posted=False,
        deletion_mark=False,
        source="1c",
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=qty,
        produced_qty=0,
        remaining_qty=qty,
    )
    db.add(product)
    db.commit()
    return order, product


def test_preview_materials_marks_ready_when_stock_covers_all(db_session):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="ReadyParent",
        child_specs=[
            ("RC1", "Comp A enough", 100, 2),  # need 2*2 = 4, have 100
            ("RC2", "Comp B enough", 100, 3),  # need 3*2 = 6, have 100
        ],
    )
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    preview = preview_materials(db_session, product.product_id, refresh_state=True)

    assert preview["coverage"] == "ready"
    for c in preview["components"]:
        assert c["coverage"] == "ok"
        assert c["missing_qty"] == 0
        assert c["eta_dates"] == []
    # Material coverage is cached on the line state; the workflow status is
    # bumped only while the line has not moved past the coverage band.
    state = (
        db_session.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert state.status == "ready"
    assert state.material_coverage_status == "ready"
    assert state.material_coverage_label == "Обеспечен"
    assert state.material_coverage_calculated_at is not None
    assert state.material_coverage_snapshot["components"][0]["coverage_status"] == "ready"


def test_posted_issue_coverage_uses_workshop_reservation_not_free_stock(db_session):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="PostedPartialParent",
        child_specs=[("PPC1", "Posted partial comp", 100, 2)],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)
    state = ProductionOrderLineState(
        product_id=product.product_id,
        status="assembled",
        issue_status="posted",
    )
    db_session.add(state)
    issue = ProductionMaterialIssue(
        document_number="MT-PARTIAL-POSTED",
        product_id=product.product_id,
        order_id=product.order_id,
        status="posted",
        direction="issue",
        warehouse_ref1c="WORKSHOP",
        source_warehouse_ref1c="SOURCE",
    )
    db_session.add(issue)
    db_session.flush()
    db_session.add(
        ProductionMaterialIssueLine(
            issue_id=issue.issue_id,
            component_item_id=comp.item_id,
            required_qty=2,
            issued_qty=2,
            unit="шт",
            line_status="issued",
        )
    )
    db_session.commit()

    preview = preview_materials(db_session, product.product_id, refresh_state=True)
    only = preview["components"][0]

    assert only["required_qty"] == 4
    assert only["available_qty"] > 0
    assert only["reserved_at_workshop_qty"] == 2
    assert only["missing_qty"] == 2
    assert only["coverage"] == "partial"
    assert preview["coverage_status"] == "partial"

    row = list_journal(db_session, product_id=product.product_id)["rows"][0]
    assert row["coverage_status"] == "partial"
    assert row["coverage_label"] == "Частично"


def test_journal_uses_cached_material_coverage_for_coverage_band_rows(db_session):
    parent, _spec, _comps = _make_basic_spec(
        db_session,
        parent_name="JournalReadyParent",
        child_specs=[("JRC1", "Journal component enough", 10, 1)],
    )
    _order, product = _make_internal_order_for(db_session, parent, qty=2)
    _order.source = "mrp"
    state = (
        db_session.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one_or_none()
    )
    if state is None:
        state = ProductionOrderLineState(product_id=product.product_id, status="shortage")
        db_session.add(state)
    state.status = "shortage"
    state.issue_status = "not_requested"
    state.material_coverage_status = "ready"
    state.material_coverage_label = "Обеспечен"
    db_session.commit()

    row = list_journal(db_session, product_id=product.product_id)["rows"][0]

    assert row["coverage_status"] == "ready"
    assert row["coverage_label"] == "Обеспечен"
    assert row["status"] == "ready"


def test_preview_materials_marks_shortage_and_includes_supplier_eta(db_session):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="ShortageParent",
        child_specs=[
            ("SC1", "No stock comp", 0, 1),  # need 1*2 = 2, have 0
        ],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    # An open supplier order with a future delivery date covering this component.
    sup_order = SupplierOrder(
        order_number="ЗАКП-COVER-001",
        order_date=datetime(2026, 5, 10, 10),
        order_ref1c="sup-ref-cover-001",
        order_state_key="state-in-work",
        order_state_name="В пути",
        deletion_mark=False,
    )
    db_session.add(sup_order)
    db_session.flush()
    db_session.add(
        SupplierOrderItem(
            order_id=sup_order.order_id,
            item_id_ref=comp.item_id,
            line_number=1,
            quantity=10,
            received_qty=0,
            remaining_qty=10,
            delivery_date=datetime(2026, 6, 1),
        )
    )
    db_session.commit()

    preview = preview_materials(db_session, product.product_id, refresh_state=True)
    assert preview["coverage"] == "shortage"
    only_comp = preview["components"][0]
    assert only_comp["coverage"] == "shortage"
    assert only_comp["missing_qty"] == 2
    # ETA from supplier_order pipe.
    eta = only_comp["eta_dates"]
    assert len(eta) >= 1
    sup_etas = [e for e in eta if e["source"] == "supplier_order"]
    assert sup_etas
    assert sup_etas[0]["ref"] == "ЗАКП-COVER-001"
    assert sup_etas[0]["date"] == "2026-06-01"


def test_preview_materials_includes_active_production_order_eta(db_session):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="ProductionEtaParent",
        child_specs=[
            ("PEC1", "Production ETA component", 0, 3),  # need 3*2 = 6, have 0
        ],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    supply_order = ProductionOrder(
        order_number="MRP-RC-13-PEC1",
        order_date=datetime(2026, 6, 4, 8),
        is_posted=False,
        deletion_mark=False,
        source="mrp",
    )
    db_session.add(supply_order)
    db_session.flush()
    supply_product = ProductionProduct(
        order_id=supply_order.order_id,
        item_id=comp.item_id,
        line_number=1,
        quantity=10,
        produced_qty=0,
        remaining_qty=10,
    )
    db_session.add(supply_product)
    db_session.flush()
    db_session.add(
        ProductionOrderLineState(
            product_id=supply_product.product_id,
            status="partial",
            issue_status="not_requested",
            planned_finish_date=_dt.date(2026, 6, 30),
        )
    )
    db_session.commit()

    preview = preview_materials(db_session, product.product_id, refresh_state=True)
    only_comp = preview["components"][0]
    prod_etas = [e for e in only_comp["expected_dates"] if e["source"] == "production_order"]

    assert only_comp["coverage"] == "shortage"
    assert prod_etas
    assert prod_etas[0]["order_number"] == "MRP-RC-13-PEC1"
    assert prod_etas[0]["date"] == "2026-06-30"
    assert prod_etas[0]["qty"] == 10


def test_preview_materials_marks_partial_when_some_stock(db_session):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="PartialParent",
        child_specs=[
            ("PC1", "Comp full", 100, 1),  # need 2, have 100 -> ok
            ("PC2", "Comp partial", 3, 2),  # need 4, have 3 -> partial
        ],
    )
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    preview = preview_materials(db_session, product.product_id, refresh_state=True)
    assert preview["coverage"] == "partial"
    by_name = {c["item_name"]: c for c in preview["components"]}
    assert by_name["Comp full"]["coverage"] == "ok"
    assert by_name["Comp partial"]["coverage"] == "partial"
    assert by_name["Comp partial"]["missing_qty"] == 1
    state = (
        db_session.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert state.status == "partial"
    assert state.material_coverage_status == "partial"


def test_preview_materials_does_not_override_post_coverage_status(db_session):
    """
    Once a line has progressed past coverage (to_move / assembled / produced),
    re-running preview_materials must not regress it back to shortage/partial/
    ready even if the stock numbers say so.
    """
    parent, _spec, _comps = _make_basic_spec(
        db_session,
        parent_name="StickyParent",
        child_specs=[("STC1", "Comp empty", 0, 1)],
    )
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    # Manually push the state past the coverage band.
    state = (
        db_session.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one_or_none()
    )
    if state is None:
        # Lazy-create through preview, then move forward.
        preview_materials(db_session, product.product_id, refresh_state=True)
        state = (
            db_session.query(ProductionOrderLineState)
            .filter_by(product_id=product.product_id)
            .one()
        )
    state.status = "to_move"
    db_session.commit()

    preview_materials(db_session, product.product_id, refresh_state=True)
    refreshed = (
        db_session.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert refreshed.status == "to_move"
    assert refreshed.material_coverage_status == "shortage"


def test_recalculate_production_coverage_refreshes_static_cache(db_session):
    parent, _spec, _comps = _make_basic_spec(
        db_session,
        parent_name="CachedParent",
        child_specs=[("CACHED1", "Cached component", 10, 1)],
    )
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    result = recalculate_production_coverage(db_session)

    assert result["processed"] == 1
    state = (
        db_session.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert state.material_coverage_status == "ready"
    assert state.material_coverage_label == "Обеспечен"
    assert state.material_coverage_snapshot["coverage_status"] == "ready"


# ---------------------------------------------------------------------------
# Warehouse settings
# ---------------------------------------------------------------------------


def test_workshop_warehouse_binding_lifecycle(db_session):
    from app.models import ProductionResource, WorkshopWarehouseBinding
    from app.services.production_control_settings import (
        delete_workshop_binding,
        list_settings,
        upsert_workshop_binding,
    )

    workshop = ProductionResource(resource_name="Цех сварки")
    db_session.add(workshop)
    db_session.flush()

    # Initially empty.
    settings = list_settings(db_session)
    assert settings["workshop_warehouse_bindings"] == []
    assert settings["ignored_warehouses"] == []

    # Create.
    created = upsert_workshop_binding(
        db_session,
        workshop.resource_id,
        "11111111-1111-1111-1111-111111111111",
    )
    assert created["workshop_id"] == workshop.resource_id
    assert created["warehouse_ref1c"] == "11111111-1111-1111-1111-111111111111"
    assert created["workshop_name"] == "Цех сварки"

    # Idempotent upsert: same workshop, new warehouse — should update, not create a 2nd row.
    updated = upsert_workshop_binding(
        db_session,
        workshop.resource_id,
        "22222222-2222-2222-2222-222222222222",
    )
    assert updated["warehouse_ref1c"] == "22222222-2222-2222-2222-222222222222"
    assert (
        db_session.query(WorkshopWarehouseBinding)
        .filter_by(workshop_id=workshop.resource_id)
        .count()
        == 1
    )

    # Unknown workshop -> ValueError.
    with pytest.raises(ValueError):
        upsert_workshop_binding(db_session, 999_999, "33333333-3333-3333-3333-333333333333")

    # Delete.
    result = delete_workshop_binding(db_session, workshop.resource_id)
    assert result["deleted"] == 1
    settings_after = list_settings(db_session)
    assert settings_after["workshop_warehouse_bindings"] == []


def test_replace_settings_matches_frontend_contract(db_session):
    from app.models import IgnoredWarehouse, ProductionResource, StockWarehouse, WorkshopWarehouseBinding
    from app.services.production_control_settings import list_settings, replace_settings

    workshop_a = ProductionResource(resource_name="Цех А")
    workshop_b = ProductionResource(resource_name="Цех Б")
    warehouse_a = StockWarehouse(
        warehouse_ref1c="wh-a",
        warehouse_code="A",
        warehouse_name="Склад А",
        is_selected=True,
    )
    warehouse_b = StockWarehouse(
        warehouse_ref1c="wh-b",
        warehouse_code="B",
        warehouse_name="Склад Б",
        is_selected=True,
    )
    workshop_location = StockWarehouse(
        warehouse_ref1c="wh-workshop",
        warehouse_code="W",
        warehouse_name="Участок сборки модулей",
        is_selected=True,
    )
    employee_location = StockWarehouse(
        warehouse_ref1c="wh-person",
        warehouse_code="P",
        warehouse_name="Иванов Иван Иванович",
        is_selected=True,
    )
    db_session.add_all([workshop_a, workshop_b, warehouse_a, warehouse_b, workshop_location, employee_location])
    db_session.flush()

    saved = replace_settings(
        db_session,
        workshop_warehouses=[
            {"resource_id": workshop_a.resource_id, "warehouse_ref1c": "wh-a", "production_warehouse_ref1c": "wh-workshop"},
            {"resource_id": workshop_b.resource_id, "warehouse_ref1c": "wh-b"},
        ],
        ignored_warehouses=[{"warehouse_ref1c": "wh-b"}],
    )
    assert [row["warehouse_ref1c"] for row in saved["warehouses"]] == ["wh-a", "wh-b", "wh-workshop"]
    assert "wh-person" not in {row["warehouse_ref1c"] for row in saved["warehouses"]}
    assert saved["workshop_warehouses"] == saved["workshop_warehouse_bindings"]
    assert len(saved["workshop_warehouses"]) == 2
    assert saved["workshop_warehouses"][0]["resource_id"] == saved["workshop_warehouses"][0]["workshop_id"]
    assert saved["workshop_warehouses"][0]["production_warehouse_ref1c"] == "wh-workshop"
    assert db_session.query(WorkshopWarehouseBinding).count() == 2
    assert db_session.query(IgnoredWarehouse).count() == 1

    replaced = replace_settings(
        db_session,
        workshop_warehouses=[{"resource_id": workshop_a.resource_id, "warehouse_ref1c": "wh-b"}],
        ignored_warehouses=[],
    )
    assert len(replaced["workshop_warehouses"]) == 1
    assert replaced["workshop_warehouses"][0]["warehouse_ref1c"] == "wh-b"
    assert db_session.query(WorkshopWarehouseBinding).count() == 1
    assert db_session.query(IgnoredWarehouse).count() == 0
    assert "warehouses" in list_settings(db_session)


def test_ignored_warehouse_lifecycle(db_session):
    from app.services.production_control_settings import (
        delete_ignored_warehouse,
        list_settings,
        upsert_ignored_warehouse,
    )

    settings = list_settings(db_session)
    assert settings["ignored_warehouses"] == []

    added = upsert_ignored_warehouse(
        db_session,
        "deadbeef-0000-0000-0000-deadbeefcafe",
        warehouse_name="Изолятор брака",
        reason="Бракованные комплектующие",
    )
    assert added["warehouse_ref1c"] == "deadbeef-0000-0000-0000-deadbeefcafe"
    assert added["warehouse_name"] == "Изолятор брака"
    assert added["reason"] == "Бракованные комплектующие"

    # Update existing.
    updated = upsert_ignored_warehouse(
        db_session,
        "deadbeef-0000-0000-0000-deadbeefcafe",
        warehouse_name="Изолятор брака (обновлено)",
    )
    assert updated["warehouse_name"] == "Изолятор брака (обновлено)"

    listed = list_settings(db_session)["ignored_warehouses"]
    assert len(listed) == 1

    delete_ignored_warehouse(db_session, "deadbeef-0000-0000-0000-deadbeefcafe")
    assert list_settings(db_session)["ignored_warehouses"] == []


def test_create_material_issues_uses_workshop_binding_when_warehouse_not_pinned(db_session):
    """
    If the caller does not supply warehouse_ref1c, fall back to the
    workshop->warehouse binding from settings (plan: "привязка участок ->
    склад получатель"). If the caller does supply one, it wins.
    """
    from app.models import ProductionResource, WorkshopWarehouseBinding

    workshop = ProductionResource(resource_name="Цех сборки")
    db_session.add(workshop)
    db_session.flush()

    parent = Item(
        item_code="WH-PARENT",
        item_name="Parent",
        item_article="WH-P",
        unit="шт",
        stock_qty=0,
        status="active",
    )
    comp = Item(
        item_code="WH-COMP",
        item_name="Comp",
        item_article="WH-C",
        unit="м",
        stock_qty=100,
        status="active",
    )
    db_session.add_all([parent, comp])
    db_session.flush()
    spec = Specification(spec_name="WH spec", spec_ref1c="wh-spec")
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))

    order = ProductionOrder(
        order_number="WH-001",
        order_date=datetime(2026, 5, 20),
        is_posted=True,
        deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
    )
    db_session.add(product)
    db_session.flush()
    state = ProductionOrderLineState(
        product_id=product.product_id,
        status="shortage",
        issue_status="not_requested",
        workshop_id=workshop.resource_id,
    )
    db_session.add(state)
    db_session.add(
        WorkshopWarehouseBinding(
            workshop_id=workshop.resource_id,
            warehouse_ref1c="aaaa1111-aaaa-1111-aaaa-111111111111",
        )
    )
    db_session.commit()

    res_default = create_material_issues(
        db_session,
        [product.product_id],
        initiated_by="op",
    )
    assert len(res_default["created"]) == 1
    issue_id = res_default["created"][0]["issue_id"]
    from app.models import ProductionMaterialIssue
    issue = db_session.query(ProductionMaterialIssue).filter_by(issue_id=issue_id).one()
    assert issue.warehouse_ref1c == "aaaa1111-aaaa-1111-aaaa-111111111111"

    # Caller-supplied warehouse_ref1c wins over the binding. Create a second
    # product to avoid the active-issue idempotency lock.
    product2 = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=2,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
    )
    db_session.add(product2)
    db_session.flush()
    db_session.add(
        ProductionOrderLineState(
            product_id=product2.product_id,
            status="shortage",
            issue_status="not_requested",
            workshop_id=workshop.resource_id,
        )
    )
    db_session.commit()

    res_explicit = create_material_issues(
        db_session,
        [product2.product_id],
        initiated_by="op",
        warehouse_ref1c="bbbb2222-bbbb-2222-bbbb-222222222222",
    )
    assert len(res_explicit["created"]) == 1
    issue2 = (
        db_session.query(ProductionMaterialIssue)
        .filter_by(issue_id=res_explicit["created"][0]["issue_id"])
        .one()
    )
    assert issue2.warehouse_ref1c == "bbbb2222-bbbb-2222-bbbb-222222222222"


def test_create_material_issues_splits_components_by_source_warehouse(db_session):
    from app.models import (
        ItemWarehouseStock,
        ProductionMaterialIssue,
        ProductionResource,
        StockWarehouse,
        WorkshopWarehouseBinding,
    )

    workshop = ProductionResource(resource_name="Сварочный участок")
    db_session.add(workshop)
    db_session.flush()

    parent = Item(item_code="SRC-PARENT", item_name="Parent", item_article="SRC-P", unit="шт", stock_qty=0, status="active")
    comp_a = Item(item_code="SRC-A", item_name="Comp A", item_article="SRC-A", unit="шт", stock_qty=0, status="active")
    comp_b = Item(item_code="SRC-B", item_name="Comp B", item_article="SRC-B", unit="шт", stock_qty=0, status="active")
    db_session.add_all([parent, comp_a, comp_b])
    db_session.flush()
    spec = Specification(spec_name="SRC spec", spec_ref1c="src-spec")
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add_all([
        SpecComponent(spec_id=spec.spec_id, item_id=comp_a.item_id, quantity=1),
        SpecComponent(spec_id=spec.spec_id, item_id=comp_b.item_id, quantity=1),
    ])

    order = ProductionOrder(order_number="SRC-001", order_date=datetime(2026, 5, 20), is_posted=True, deletion_mark=False)
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(order_id=order.order_id, item_id=parent.item_id, line_number=1, quantity=1, produced_qty=0, remaining_qty=1)
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, workshop_id=workshop.resource_id))
    db_session.add(WorkshopWarehouseBinding(workshop_id=workshop.resource_id, warehouse_ref1c="WH-DEST"))
    db_session.add_all([
        StockWarehouse(warehouse_ref1c="WH-DEST", warehouse_code="DEST", warehouse_name="Участок сварочный", is_selected=True),
        StockWarehouse(warehouse_ref1c="WH-A", warehouse_code="A", warehouse_name="Склад А", is_selected=True),
        StockWarehouse(warehouse_ref1c="WH-B", warehouse_code="B", warehouse_name="Склад Б", is_selected=True),
    ])
    # No stock on WH-DEST: components lying on the destination workshop are
    # claimed in place (direction='in_place') instead of being transferred,
    # which is covered by test_section_stock_reservations.py.
    db_session.add_all([
        ItemWarehouseStock(item_id=comp_a.item_id, warehouse_ref1c="WH-A", qty=5),
        ItemWarehouseStock(item_id=comp_b.item_id, warehouse_ref1c="WH-B", qty=5),
    ])
    db_session.commit()

    result = create_material_issues(db_session, [product.product_id], initiated_by="op")
    assert result["selection_required"] == []
    assert len(result["created"]) == 2
    issues = (
        db_session.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.issue_id.in_([row["issue_id"] for row in result["created"]]))
        .order_by(ProductionMaterialIssue.source_warehouse_ref1c.asc())
        .all()
    )

    assert [issue.warehouse_ref1c for issue in issues] == ["WH-DEST", "WH-DEST"]
    assert [issue.source_warehouse_ref1c for issue in issues] == ["WH-A", "WH-B"]
    assert [len(issue.lines) for issue in issues] == [1, 1]
    assert {issues[0].lines[0].component_item_id, issues[1].lines[0].component_item_id} == {
        comp_a.item_id,
        comp_b.item_id,
    }


def test_create_material_issues_skips_component_already_on_destination_warehouse(db_session):
    workshop = ProductionResource(resource_name="Участок сборки")
    db_session.add(workshop)
    db_session.flush()

    parent = Item(item_code="DST-PARENT", item_name="Parent", item_article="DST-P", unit="шт", stock_qty=0, status="active")
    comp = Item(item_code="DST-C", item_name="Comp", item_article="DST-C", unit="шт", stock_qty=0, status="active")
    db_session.add_all([parent, comp])
    db_session.flush()
    spec = Specification(spec_name="DST spec", spec_ref1c="dst-spec")
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=2))

    order = ProductionOrder(order_number="DST-001", order_date=datetime(2026, 5, 20), is_posted=True, deletion_mark=False)
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(order_id=order.order_id, item_id=parent.item_id, line_number=1, quantity=1, produced_qty=0, remaining_qty=1)
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, workshop_id=workshop.resource_id))
    db_session.add(WorkshopWarehouseBinding(workshop_id=workshop.resource_id, warehouse_ref1c="WH-DEST"))
    db_session.add(StockWarehouse(warehouse_ref1c="WH-DEST", warehouse_code="DEST", warehouse_name="Участок", is_selected=True))
    db_session.add(ItemWarehouseStock(item_id=comp.item_id, warehouse_ref1c="WH-DEST", qty=2))
    db_session.commit()

    result = create_material_issues(db_session, [product.product_id], initiated_by="op")

    assert len(result["created"]) == 1
    assert result["created"][0]["direction"] == "in_place"
    assert result["reused"] == []
    assert result["selection_required"] == []
    assert result["errors"] == []
    assert result["already_on_destination"][0]["components"][0]["covered_qty"] == 2
    issue = db_session.query(ProductionMaterialIssue).one()
    assert issue.direction == "in_place"
    assert issue.status == "posted"
    assert issue.warehouse_ref1c == "WH-DEST"
    assert issue.lines[0].required_qty == 2
    assert issue.lines[0].issued_qty == 2


def test_create_material_issues_moves_only_missing_qty_when_partially_on_destination(db_session):
    workshop = ProductionResource(resource_name="Участок частичного покрытия")
    db_session.add(workshop)
    db_session.flush()

    parent = Item(item_code="PART-PARENT", item_name="Parent", item_article="PART-P", unit="шт", stock_qty=0, status="active")
    comp = Item(item_code="PART-C", item_name="Comp", item_article="PART-C", unit="шт", stock_qty=0, status="active")
    db_session.add_all([parent, comp])
    db_session.flush()
    spec = Specification(spec_name="PART spec", spec_ref1c="part-spec")
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=5))

    order = ProductionOrder(order_number="PART-001", order_date=datetime(2026, 5, 20), is_posted=True, deletion_mark=False)
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(order_id=order.order_id, item_id=parent.item_id, line_number=1, quantity=1, produced_qty=0, remaining_qty=1)
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, workshop_id=workshop.resource_id))
    db_session.add(WorkshopWarehouseBinding(workshop_id=workshop.resource_id, warehouse_ref1c="WH-DEST"))
    db_session.add_all([
        StockWarehouse(warehouse_ref1c="WH-DEST", warehouse_code="DEST", warehouse_name="Участок", is_selected=True),
        StockWarehouse(warehouse_ref1c="WH-SRC", warehouse_code="SRC", warehouse_name="Склад", is_selected=True),
    ])
    db_session.add_all([
        ItemWarehouseStock(item_id=comp.item_id, warehouse_ref1c="WH-DEST", qty=2),
        ItemWarehouseStock(item_id=comp.item_id, warehouse_ref1c="WH-SRC", qty=10),
    ])
    db_session.commit()

    result = create_material_issues(db_session, [product.product_id], initiated_by="op")

    assert len(result["created"]) == 2
    assert {row.get("direction", "issue") for row in result["created"]} == {"in_place", "issue"}
    assert result["already_on_destination"][0]["components"][0]["covered_qty"] == 2
    issues = db_session.query(ProductionMaterialIssue).order_by(ProductionMaterialIssue.issue_id).all()
    assert [issue.direction for issue in issues] == ["in_place", "issue"]
    assert issues[0].source_warehouse_ref1c == "WH-DEST"
    assert issues[0].lines[0].required_qty == 2
    assert issues[0].lines[0].issued_qty == 2
    assert issues[1].source_warehouse_ref1c == "WH-SRC"
    assert issues[1].lines[0].required_qty == 3


def test_create_material_issues_does_not_reuse_destination_stock_for_multiple_products(db_session):
    workshop = ProductionResource(resource_name="Участок общего остатка")
    db_session.add(workshop)
    db_session.flush()

    parent = Item(item_code="SHARED-PARENT", item_name="Parent", item_article="SHARED-P", unit="шт", stock_qty=0, status="active")
    comp = Item(item_code="SHARED-C", item_name="Comp", item_article="SHARED-C", unit="шт", stock_qty=0, status="active")
    db_session.add_all([parent, comp])
    db_session.flush()
    spec = Specification(spec_name="SHARED spec", spec_ref1c="shared-spec")
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=2))

    order = ProductionOrder(order_number="SHARED-001", order_date=datetime(2026, 5, 20), is_posted=True, deletion_mark=False)
    db_session.add(order)
    db_session.flush()
    products = []
    for line_number in (1, 2):
        product = ProductionProduct(order_id=order.order_id, item_id=parent.item_id, line_number=line_number, quantity=1, produced_qty=0, remaining_qty=1)
        db_session.add(product)
        db_session.flush()
        db_session.add(ProductionOrderLineState(product_id=product.product_id, workshop_id=workshop.resource_id))
        products.append(product)
    db_session.add(WorkshopWarehouseBinding(workshop_id=workshop.resource_id, warehouse_ref1c="WH-DEST"))
    db_session.add_all([
        StockWarehouse(warehouse_ref1c="WH-DEST", warehouse_code="DEST", warehouse_name="Участок", is_selected=True),
        StockWarehouse(warehouse_ref1c="WH-SRC", warehouse_code="SRC", warehouse_name="Склад", is_selected=True),
    ])
    db_session.add_all([
        ItemWarehouseStock(item_id=comp.item_id, warehouse_ref1c="WH-DEST", qty=2),
        ItemWarehouseStock(item_id=comp.item_id, warehouse_ref1c="WH-SRC", qty=10),
    ])
    db_session.commit()

    result = create_material_issues(db_session, [p.product_id for p in products], initiated_by="op")

    assert len(result["created"]) == 2
    assert {row.get("direction", "issue") for row in result["created"]} == {"in_place", "issue"}
    assert len(result["already_on_destination"]) == 1
    issues = db_session.query(ProductionMaterialIssue).order_by(ProductionMaterialIssue.issue_id).all()
    assert [issue.direction for issue in issues] == ["in_place", "issue"]
    assert issues[0].product_id == products[0].product_id
    assert issues[0].lines[0].required_qty == 2
    assert issues[1].product_id == products[1].product_id
    assert issues[1].lines[0].required_qty == 2


def test_create_material_issues_asks_when_component_has_multiple_source_warehouses(db_session):
    from app.models import (
        ItemWarehouseStock,
        ProductionMaterialIssue,
        ProductionResource,
        StockWarehouse,
        WorkshopWarehouseBinding,
    )

    workshop = ProductionResource(resource_name="Сборочный участок")
    db_session.add(workshop)
    db_session.flush()

    parent = Item(item_code="AMB-PARENT", item_name="Parent", item_article="AMB-P", unit="шт", stock_qty=0, status="active")
    comp = Item(item_code="AMB-C", item_name="Comp", item_article="AMB-C", unit="шт", stock_qty=0, status="active")
    db_session.add_all([parent, comp])
    db_session.flush()
    spec = Specification(spec_name="AMB spec", spec_ref1c="amb-spec")
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))

    order = ProductionOrder(order_number="AMB-001", order_date=datetime(2026, 5, 20), is_posted=True, deletion_mark=False)
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(order_id=order.order_id, item_id=parent.item_id, line_number=1, quantity=1, produced_qty=0, remaining_qty=1)
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductionOrderLineState(product_id=product.product_id, workshop_id=workshop.resource_id))
    db_session.add(WorkshopWarehouseBinding(workshop_id=workshop.resource_id, warehouse_ref1c="WH-DEST"))
    db_session.add_all([
        StockWarehouse(warehouse_ref1c="WH-A", warehouse_code="A", warehouse_name="Склад А", is_selected=True),
        StockWarehouse(warehouse_ref1c="WH-B", warehouse_code="B", warehouse_name="Склад Б", is_selected=True),
        StockWarehouse(warehouse_ref1c="WH-DEST", warehouse_code="DEST", warehouse_name="Участок", is_selected=True),
    ])
    # No stock on WH-DEST (it would be claimed in place); the ambiguity is
    # between the two source warehouses only.
    db_session.add_all([
        ItemWarehouseStock(item_id=comp.item_id, warehouse_ref1c="WH-A", qty=5),
        ItemWarehouseStock(item_id=comp.item_id, warehouse_ref1c="WH-B", qty=7),
    ])
    db_session.commit()

    first = create_material_issues(db_session, [product.product_id], initiated_by="op")
    assert first["created"] == []
    assert len(first["selection_required"]) == 1
    candidates = first["selection_required"][0]["components"][0]["warehouse_candidates"]
    assert {row["ref1c"] for row in candidates} == {"WH-A", "WH-B"}
    assert db_session.query(ProductionMaterialIssue).count() == 0

    second = create_material_issues(
        db_session,
        [product.product_id],
        initiated_by="op",
        source_warehouse_ref1c="WH-B",
    )
    assert len(second["created"]) == 1
    issue = db_session.query(ProductionMaterialIssue).one()
    assert issue.source_warehouse_ref1c == "WH-B"
    assert len(issue.lines) == 1


# ---------------------------------------------------------------------------
# Coverage with ignored_warehouses + per-warehouse stock breakdown
# ---------------------------------------------------------------------------


def test_preview_materials_excludes_ignored_warehouses_from_stock(db_session):
    """
    Plan rule: "Игнорируемые склады нужны, чтобы не задавать лишние вопросы
    по остаткам, например если компонент лежит в изоляторе брака."

    With item_warehouse_stock populated AND an ignored_warehouses entry, the
    coverage calculation must use the per-warehouse breakdown and exclude
    stock sitting in ignored warehouses, even if Item.stock_qty (aggregated)
    suggests there's enough.
    """
    from app.models import ItemWarehouseStock
    from app.services.production_control_settings import upsert_ignored_warehouse

    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="IgnoreCheckParent",
        child_specs=[("IGNCMP", "Ignored-stock comp", 0, 1)],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    # Aggregated says we have 10 — but it's all in the brak isolator.
    comp.stock_qty = 10
    db_session.add(
        ItemWarehouseStock(
            item_id=comp.item_id,
            warehouse_ref1c="brak-warehouse-guid",
            qty=10,
        )
    )
    db_session.commit()

    # Before adding to the ignore list: coverage should be 'ready' (10 >= 2).
    preview = preview_materials(db_session, product.product_id, refresh_state=True)
    assert preview["coverage"] == "ready"
    assert preview["components"][0]["available_qty"] == 10
    assert preview["components"][0]["coverage"] == "ok"

    # Bump state back into the coverage band so the next preview can refresh
    # it (sticky-status guarantee from PR #4).
    state = (
        db_session.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    state.status = "shortage"
    db_session.commit()

    # Mark brak warehouse as ignored — that stock should drop out.
    upsert_ignored_warehouse(
        db_session,
        "brak-warehouse-guid",
        warehouse_name="Изолятор брака",
        reason="Бракованные комплектующие",
    )

    preview_after = preview_materials(db_session, product.product_id, refresh_state=True)
    assert preview_after["coverage"] == "shortage"
    only_comp = preview_after["components"][0]
    assert only_comp["available_qty"] == 0
    assert only_comp["missing_qty"] == 2
    assert only_comp["coverage"] == "shortage"


def test_preview_materials_falls_back_to_aggregated_when_no_breakdown(db_session):
    """
    With ignored_warehouses configured but no item_warehouse_stock rows for
    the component (e.g. stock hasn't been re-synced yet after the migration),
    fall back to aggregated Item.stock_qty so coverage doesn't collapse to 0
    during the rollout.
    """
    from app.services.production_control_settings import upsert_ignored_warehouse

    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="FallbackParent",
        child_specs=[("FBCMP", "Aggregated-only comp", 0, 1)],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)
    comp.stock_qty = 5  # aggregated value, no per-warehouse rows
    db_session.commit()

    upsert_ignored_warehouse(db_session, "some-other-warehouse")

    preview = preview_materials(db_session, product.product_id)
    # With no breakdown rows, fallback returns 5 -> need 2 -> ok.
    assert preview["coverage"] == "ready"
    assert preview["components"][0]["available_qty"] == 5
    assert preview["components"][0]["coverage"] == "ok"


def test_preview_materials_mixes_breakdown_and_aggregated_fallback(db_session):
    """
    Component A has per-warehouse breakdown with everything in an ignored
    warehouse -> 0 available. Component B has no breakdown rows at all ->
    falls back to aggregated Item.stock_qty.

    Order-level coverage aggregates per the plan: any 'shortage' -> shortage.
    So this case is a 'shortage' (blocked on comp A) despite comp B being
    fully covered. The point of the test is the per-component values:
    breakdown path is authoritative when present, aggregated is the fallback.
    """
    from app.models import ItemWarehouseStock
    from app.services.production_control_settings import upsert_ignored_warehouse

    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="MixParent",
        child_specs=[
            ("MIXA", "Comp A all-ignored", 50, 1),  # need 1*2 = 2
            ("MIXB", "Comp B aggregated only", 50, 1),  # need 1*2 = 2
        ],
    )
    comp_a, _comp_b = comps
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    db_session.add(
        ItemWarehouseStock(
            item_id=comp_a.item_id,
            warehouse_ref1c="brak-mix-guid",
            qty=50,
        )
    )
    db_session.commit()
    upsert_ignored_warehouse(db_session, "brak-mix-guid")

    preview = preview_materials(db_session, product.product_id)
    assert preview["coverage"] == "shortage"  # blocked by comp A
    by_name = {c["item_name"]: c for c in preview["components"]}
    assert by_name["Comp A all-ignored"]["available_qty"] == 0
    assert by_name["Comp A all-ignored"]["coverage"] == "shortage"
    assert by_name["Comp B aggregated only"]["available_qty"] == 50
    assert by_name["Comp B aggregated only"]["coverage"] == "ok"

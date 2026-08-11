import datetime as _dt
import json
import math
from types import SimpleNamespace
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app.models import (
    DefaultSpecification,
    ProductionKind,
    ResourceProductionKind,
    Employee,
    Item,
    MrpRequirement,
    Operation,
    PlannedOrder,
    PlannedPurchase,
    PlanningRun,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ProductionStage,
    ProductionMaterialCustodyEvent,
    SpecComponent,
    SpecOperation,
    StockWarehouse,
    Specification,
    SupplierOrder,
    SupplierOrderItem,
    SyncLink,
    IgnoredWarehouse,
    WorkshopWarehouseBinding,
    LedgerBuildBatch,
    LedgerFutureSupply,
    LedgerGeneration,
    PhysicalImportBatch,
    StockBin,
)
from app.routers.production_control import (
    list_employees,
)
from app.services.production_control_journal import (
    cancel_local_order,
    list_journal,
    update_line_state,
)
from app.services.production_control_material_availability import (
    preview_materials,
)
from app.services import production_control_material_availability as material_availability
from app.services.production_material_custody_events import (
    append_material_issue_custody_event,
)
from app.services.production_material_custody_projection import (
    initialize_material_custody_baseline,
)
from app.services.production_control_material_issues import create_material_issues, delete_local_material_issue, list_material_issues
from app.services.production_control_printing import (
    build_route_sheet_snapshot_payloads,
    render_route_sheets_from_snapshots,
)
from app.services.one_c_production_order_export import PRODUCTION_ORDER_ENTITY
from app.services.planning_truth import PlanningTruthUnavailable, publish_generation
from app.services.mrp_mutation_guard import MrpMutationLineageError


def _render_route_sheets_snapshot_html(
    db_session,
    product_ids: list[int],
    *,
    auto_print: bool = False,
) -> str:
    payload_by_product = build_route_sheet_snapshot_payloads(
        db_session,
        product_ids,
        ledger_generation_id=int(
            db_session.info.get("production_journal_generation_id")
            or 0
        )
        or None,
    )
    unique_payloads: list[dict[str, object]] = []
    seen_anchors: set[int] = set()
    for product_id in product_ids:
        payload = payload_by_product.get(int(product_id))
        if not isinstance(payload, dict):
            continue
        anchor_product_id = int(payload.get("anchor_product_id") or payload.get("sheet", {}).get("product_id", 0))
        if anchor_product_id <= 0 or anchor_product_id in seen_anchors:
            continue
        seen_anchors.add(anchor_product_id)
        unique_payloads.append(payload)
    return render_route_sheets_from_snapshots(unique_payloads, auto_print=auto_print)



def _accepted_mrp_context(db_session, *, key: str):
    cutoff = datetime(2026, 5, 31, 23, 59, tzinfo=_dt.timezone.utc)
    batch = PhysicalImportBatch(
        batch_key=f"{key}-batch",
        status="completed",
        cutoff=cutoff,
        completed_at=cutoff,
        source_watermarks={"explicit_empty_prefix": True},
    )
    generation = LedgerGeneration(
        generation_key=f"{key}-generation",
        status="building",
        cutoff=cutoff,
        accepted_at=None,
        source_watermarks={"explicit_empty_prefix": True},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
            "future_supply": True,
        },
        physical_import_batch=batch,
        algorithm_version="test/1",
        replay_version="test/1",
    )
    db_session.add(generation)
    db_session.flush()
    initialize_material_custody_baseline(
        db_session,
        ledger_generation_id=int(generation.id),
        cells=[],
        observed_at=cutoff,
    )
    generation.status = "accepted"
    generation.accepted_at = cutoff
    publish_generation(db_session, generation)
    db_session.flush()
    db_session.expire_all()
    return generation, cutoff


def _accepted_stock_bin(db_session, item_id: int, warehouse_ref1c: str, qty: float) -> StockBin:
    """Seed the exact accepted Ledger fold only."""
    return StockBin(
        ledger_generation_id=int(db_session.info["production_journal_generation_id"]),
        item_id=int(item_id), characteristic_ref="", organization_ref="",
        warehouse_ref1c=warehouse_ref1c, on_hand=qty,
    )


def _ledger_future_supply(
    db_session,
    *,
    item_id: int,
    source_ref: str,
    source_line_ref: str = "1",
    kind: str = "supplier_order",
    generation_id: int | None = None,
    open_qty: float = 1.0,
    eta_date: _dt.date,
    planning_pool: str = "main",
    destination: str = "WH-STAGE-1",
) -> LedgerFutureSupply:
    generation_id = int(
        generation_id or db_session.info["production_journal_generation_id"]
    )
    generation = db_session.get(LedgerGeneration, generation_id)
    if generation is None:
        raise ValueError("generation not found")
    batch = LedgerBuildBatch(
        ledger_generation_id=generation_id,
        stage="snapshot_build",
        batch_key=f"test-future-supply-{generation_id}-{source_ref}",
        status="completed",
        algorithm_version="test/1",
        metrics={"source": "test"},
    )
    db_session.add(batch)
    db_session.flush()
    row = LedgerFutureSupply(
        ledger_generation_id=generation_id,
        capture_batch_id=int(batch.id),
        supply_kind=kind,
        item_id=int(item_id),
        planning_stock_pool=planning_pool,
        destination_warehouse_ref1c=destination,
        source_ref=source_ref,
        source_line_ref=source_line_ref,
        ordered_qty_at_cutoff=float(open_qty),
        realized_qty_at_cutoff=0,
        open_qty_at_cutoff=float(open_qty),
        eta_date=eta_date,
        source_state_key="test",
        capture_cutoff=generation.cutoff,
        source_content_hash="test-hash",
        evidence_status="exact",
    )
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture(autouse=True)
def accepted_production_journal_truth(db_session):
    """Journal reads have a published Ledger pointer in ordinary test cases."""
    generation, cutoff = _accepted_mrp_context(db_session, key="production-journal-default")
    db_session.info["production_journal_generation_id"] = generation.id
    return generation, cutoff


def _journal_mrp_run(db_session, *, period_from=None, period_to=None):
    period_from = period_from or _dt.date(2026, 6, 1)
    period_to = period_to or _dt.date(2026, 6, 30)
    plan = ProductionPlanHeader(
        name=f"Production journal {period_from.isoformat()} {period_to.isoformat()}",
        period_from=period_from, period_to=period_to, status="fixed",
    )
    db_session.add(plan)
    db_session.flush()
    run = PlanningRun(
        status="FIXED_SNAPSHOT", config_snapshot={}, source_plan_id=plan.id,
        period_from=period_from, period_to=period_to,
        ledger_generation_id=db_session.info["production_journal_generation_id"],
    )
    db_session.add(run)
    db_session.flush()
    return run


def test_list_employees_returns_active_synced_employees(db_session):
    db_session.add_all([
        Employee(
            employee_ref1c="11111111-1111-1111-1111-111111111111",
            employee_code="0001",
            employee_name="Иванов Иван",
            employee_type="employee",
            deletion_mark=False,
        ),
        Employee(
            employee_ref1c="22222222-2222-2222-2222-222222222222",
            employee_code="0002",
            employee_name="Петров Петр",
            employee_type="brigade",
            deletion_mark=True,
        ),
        Employee(
            employee_ref1c="33333333-3333-3333-3333-333333333333",
            employee_code="0003",
            employee_name="Яковлев Яков",
            employee_type="brigade",
            deletion_mark=False,
        ),
    ])
    db_session.commit()

    result = list_employees(db=db_session)

    assert result["total"] == 2
    assert [
        (row["employee_name"], row["employee_type"])
        for row in result["rows"]
    ] == [("Иванов Иван", "employee"), ("Яковлев Яков", "brigade")]
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

    html = _render_route_sheets_snapshot_html(db_session, [product.product_id])

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
        _accepted_stock_bin(db_session, component.item_id, "src-hl", 3),
        _accepted_stock_bin(db_session, component.item_id, "src-hl-2", 4),
        _accepted_stock_bin(db_session, component.item_id, "ignored-hl", 5),
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

    html = _render_route_sheets_snapshot_html(db_session, [product.product_id])

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
        html = _render_route_sheets_snapshot_html(db_session, product_ids)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", before_cursor_execute)

    assert "Листов: 3" in html
    assert html.count("МАРШРУТНЫЙ ЛИСТ №") == 3
    assert "MRP-BATCH-1" in html
    assert "MRP-BATCH-2" in html
    assert "MRP-BATCH-3" in html
    assert "Материал пакетной печати" in html
    assert "Собрать" in html
    # Число запросов остаётся константным независимо от размера пакета,
    # включая разрешение принятого поколения и чтение StockBin.
    assert len(statements) <= 13


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
                status="active",
    )
    component = Item(
        item_code="C-001",
        item_name="Комплектующее",
        item_article="ART-C",
        unit="м",
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
    assert journal["rows"][0]["status"] == "created"
    assert journal["rows"][0]["coverage_status"] == "unknown"
    # 1C-synced order: source defaults to '1c', order_ref1c populated.
    # Frontend uses these to hide the "Export to 1C" button on 1C rows.
    assert journal["rows"][0]["order_source"] == "1c"
    assert journal["rows"][0]["order_ref1c"] == "order-001"

    materials = preview_materials(db_session, product.product_id)
    assert materials["components"][0]["component_item_id"] == component.item_id
    assert materials["components"][0]["required_qty"] == 20

    product.produced_qty = 2
    product.remaining_qty = 999
    db_session.commit()
    corrupted_cache_preview = preview_materials(db_session, product.product_id)
    assert corrupted_cache_preview["qty"] == 6
    assert corrupted_cache_preview["components"][0]["required_qty"] == 15
    product.produced_qty = 0
    product.remaining_qty = 8
    db_session.commit()

    created = create_material_issues(db_session, [product.product_id], initiated_by="кладовщик")
    assert len(created["created"]) == 1
    assert created["created"][0]["lines_count"] == 1

    journal_after = list_journal(db_session)
    assert journal_after["rows"][0]["issue_status"] == "requested"
    # Creating a material-issue draft moves the line from 'shortage' to
    # 'to_move' ("документы созданы, ждём проведения") per plan.
    assert journal_after["rows"][0]["status"] == "to_move"
    assert journal_after["rows"][0]["issue_count"] == 1


def test_journal_visibility_and_completion_ignore_corrupted_remaining_cache(db_session):
    item = Item(
        item_code="P-REMAINING-CACHE",
        item_name="Corrupted remaining cache",
        unit="шт",
                status="active",
    )
    order = ProductionOrder(
        order_number="REMAINING-CACHE-001",
        order_date=datetime(2026, 5, 18),
        deletion_mark=False,
    )
    db_session.add_all([item, order])
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=8,
        produced_qty=2,
        # A non-factual writer falsely closed the line.
        remaining_qty=0,
    )
    db_session.add(product)
    db_session.commit()

    journal = list_journal(db_session)

    assert journal["total"] == 1
    assert journal["rows"][0]["produced_qty"] == 2
    assert journal["rows"][0]["remaining_qty"] == 6
    with pytest.raises(ValueError, match="невыпущенным остатком"):
        update_line_state(db_session, product.product_id, {"status": "completed"})

    # The inverse corruption must not keep a physically completed line visible
    # or block the completion transition.
    product.produced_qty = 8
    product.remaining_qty = 999
    db_session.commit()

    journal = list_journal(db_session)
    assert journal["total"] == 0
    assert update_line_state(
        db_session,
        product.product_id,
        {"status": "completed"},
    )["status"] == "ok"



def test_journal_exposes_historical_executor_number_after_ledger_advances(db_session):
    item = Item(
        item_code="P-ORDER-NUMBERS",
        item_name="Order number item",
        item_article="ART-ORDER-NUMBERS",
        unit="шт",
                status="active",
    )
    db_session.add(item)
    db_session.flush()

    run = _journal_mrp_run(db_session)
    order = ProductionOrder(
        order_number="PP001204945",
        order_date=datetime(2026, 6, 4),
        order_ref1c="order-ref-1c",
        is_posted=False,
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
            ledger_generation_id=None,
            status="success",
        )
    )
    db_session.commit()

    row = list_journal(db_session)["rows"][0]

    assert row["order_number"] == "PP001204945"
    assert row["order_one_c_number"] == "PP001204945"
    assert row["order_prodplan_number"] == f"MRP-RC-{run.run_id}-{item.item_id}-{order.order_id}"


def test_journal_row_exposes_close_action_for_mrp_order_with_1c_link(db_session):
    item = Item(
        item_code="P-CLOSE-ACTION",
        item_name="Close action item",
        item_article="ART-CLOSE-ACTION",
        unit="шт",
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    run = _journal_mrp_run(db_session)
    order = ProductionOrder(
        order_number="MRP-CLOSE-1",
        order_date=datetime(2026, 6, 10),
        order_ref1c="close-action-ref",
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
    )
    db_session.add(product)
    db_session.add(
        SyncLink(
            source_doctype="production_order",
            source_id=order.order_id,
            target_entity=PRODUCTION_ORDER_ENTITY,
            target_ref_key="close-action-ref",
            target_number="PO-CLOSE",
            ledger_generation_id=int(db_session.info["production_journal_generation_id"]),
            status="success",
        )
    )
    db_session.commit()

    row = list_journal(db_session)["rows"][0]
    assert row["order_source"] == "mrp"
    assert row["order_ref1c"] == "close-action-ref"
    assert row["available_actions"] == ["close_1c"]


def test_journal_row_hides_close_action_for_non_mrp_order(db_session):
    item = Item(
        item_code="P-CLOSE-ACTION-2",
        item_name="No close action item",
        item_article="ART-CLOSE-ACTION-2",
        unit="шт",
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    run = _journal_mrp_run(db_session)
    order = ProductionOrder(
        order_number="1C-NO-CLOSE",
        order_date=datetime(2026, 6, 11),
        order_ref1c="already-1c-ref",
        deletion_mark=False,
        source="1c",
        source_run_id=run.run_id,
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
    )
    db_session.add(product)
    db_session.add(
        SyncLink(
            source_doctype="production_order",
            source_id=order.order_id,
            target_entity=PRODUCTION_ORDER_ENTITY,
            target_ref_key="already-1c-ref",
            target_number="PO-1C-NO-CLOSE",
            ledger_generation_id=int(db_session.info["production_journal_generation_id"]),
            status="success",
        )
    )
    db_session.commit()

    row = list_journal(db_session)["rows"][0]
    assert row["order_source"] == "1c"
    assert row["available_actions"] == []


def test_journal_prodplan_numbers_are_unique_for_same_item_and_run(db_session):
    item = Item(
        item_code="P-DUP-PRODPLAN-NUMBERS",
        item_name="Duplicate display number item",
        unit="шт",
                status="active",
    )
    db_session.add(item)
    db_session.flush()

    run = _journal_mrp_run(db_session)
    orders = [
        ProductionOrder(
            order_number=f"PP00130000{idx}",
            order_date=datetime(2026, 6, 5),
            deletion_mark=False,
            source="mrp",
            source_run_id=run.run_id,
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
    item_a = Item(item_code="P-FLT-A", item_name="Деталь A", unit="шт", status="active")
    item_b = Item(item_code="P-FLT-B", item_name="Деталь B", unit="шт", status="active")
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


def test_journal_does_not_fabricate_work_item_coverage(db_session):
    item = Item(
        item_code="MRP-COV",
        item_name="MRP coverage item",
        unit="шт",
                optimal_batch=12,
        replenishment_method="Производство",
        status="active",
    )
    db_session.add(item)
    db_session.flush()

    run = _journal_mrp_run(db_session)
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=20,
        net_required_qty=20,
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
    assert row["mrp_req_net_qty"] is None
    assert row["mrp_req_covered_qty"] is None
    assert row["mrp_req_remaining_qty"] is None


def test_journal_splits_work_status_from_material_coverage(db_session):
    parent = Item(
        item_code="P-ASM",
        item_name="Собранная деталь",
        item_article="ASM",
        unit="шт",
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
    assert row["coverage_status"] == "unknown"
    assert row["coverage_label"] == "Недоступно"


def test_posted_assembled_line_uses_frozen_ready_coverage_when_available(db_session):
    parent, _spec, _comps = _make_basic_spec(
        db_session,
        parent_name="FrozenReadyPosted",
        child_specs=[("FRC1", "Component frozen ready", 10, 1)],
    )
    order, product = _make_internal_order_for(db_session, parent, qty=2)
    db_session.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="assembled",
            issue_status="posted",
        )
    )
    db_session.commit()

    frozen_coverage = {
        "coverage_status": "ready",
        "coverage_label": "Обеспечен",
    }
    row = list_journal(
        db_session,
        product_id=product.product_id,
        _material_coverage_by_product={product.product_id: frozen_coverage},
    )["rows"][0]

    assert row["coverage_status"] == "ready"
    assert row["coverage_label"] == "Обеспечен"


def test_journal_filters_by_workshop_and_coverage_before_paging(db_session):
    item = Item(item_code="P-FLT", item_name="Filter part", unit="шт", status="active")
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
        coverage_status="ready",
        limit=1,
        offset=0,
        _material_coverage_by_product={
            rows[1].product_id: {"coverage_status": "ready", "coverage_label": "Обеспечен"},
        },
    )

    assert journal["total"] == 1
    assert [row["order_number"] for row in journal["rows"]] == ["FLT-2"]


def test_journal_status_shortage_filter_is_exact(db_session):
    item = Item(item_code="P-STAT", item_name="Status part", unit="шт", status="active")
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


def test_journal_root_filter_uses_all_active_plan_snapshot_scopes(db_session):
    root = Item(item_code="ROOT-M", item_name="Root machine", unit="шт", status="active")
    component = Item(item_code="COMP-M", item_name="Shared component", unit="шт", status="active")
    db_session.add_all([root, component])
    db_session.flush()

    spec = Specification(spec_name="Root spec")
    db_session.add(spec)
    db_session.flush()
    db_session.add(DefaultSpecification(item_id=root.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=1))

    plan_a = ProductionPlanHeader(
        name="Current plan A",
        period_from=_dt.date(2026, 6, 1),
        period_to=_dt.date(2026, 6, 30),
        status="fixed",
    )
    plan_b = ProductionPlanHeader(
        name="Current plan B",
        period_from=_dt.date(2026, 7, 1),
        period_to=_dt.date(2026, 7, 31),
        status="fixed",
    )
    closed_plan = ProductionPlanHeader(
        name="Closed plan",
        period_from=_dt.date(2026, 5, 1),
        period_to=_dt.date(2026, 5, 31),
        status="closed",
    )
    db_session.add_all([plan_a, plan_b, closed_plan])
    db_session.flush()
    db_session.add_all([
        ProductionPlanLine(plan_id=plan_a.id, item_id=root.item_id, bucket_date=_dt.date(2026, 6, 1), qty=1),
        ProductionPlanLine(plan_id=plan_b.id, item_id=root.item_id, bucket_date=_dt.date(2026, 7, 1), qty=1),
        ProductionPlanLine(plan_id=closed_plan.id, item_id=root.item_id, bucket_date=_dt.date(2026, 5, 1), qty=1),
    ])
    current_generation_id = db_session.info["production_journal_generation_id"]
    # Older lineage is deliberately unbound; two active plan snapshots are
    # bound to the published generation.  The closed plan is excluded by
    # header status even when it carries that generation.
    old_run = PlanningRun(status="SUPERSEDED", config_snapshot={}, source_plan_id=plan_a.id)
    latest_run_a = PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={}, source_plan_id=plan_a.id, ledger_generation_id=current_generation_id)
    latest_run_b = PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={}, source_plan_id=plan_b.id, ledger_generation_id=current_generation_id)
    closed_run = PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={}, source_plan_id=closed_plan.id, ledger_generation_id=current_generation_id)
    db_session.add_all([old_run, latest_run_a, latest_run_b, closed_run])
    db_session.flush()

    for run, number in [
        (old_run, "OLD-RUN"),
        (latest_run_a, "LATEST-RUN-A"),
        (latest_run_b, "LATEST-RUN-B"),
        (closed_run, "CLOSED-RUN"),
    ]:
        order = ProductionOrder(
            order_number=number,
            order_date=datetime(2026, 5, 20),
            is_posted=True,
            deletion_mark=False,
            source="mrp",
            source_run_id=run.run_id,
        )
        db_session.add(order)
        db_session.flush()
        product = ProductionProduct(
            order_id=order.order_id,
            item_id=component.item_id,
            line_number=1,
            quantity=1,
            produced_qty=0,
            remaining_qty=1,
        )
        db_session.add(product)
        db_session.flush()
        db_session.add(ProductionOrderLineState(product_id=product.product_id, status="shortage"))
    db_session.commit()

    journal = list_journal(db_session, root_item_id=root.item_id)

    assert journal["total"] == 2
    assert {row["order_number"] for row in journal["rows"]} == {"LATEST-RUN-A", "LATEST-RUN-B"}


def test_journal_without_coverage_filter_does_not_reuse_last_row_status(db_session):
    item = Item(item_code="P-COV", item_name="Coverage part", unit="шт", status="active")
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

    material_coverage = {
        product.product_id: {
            "coverage_status": status,
            "coverage_label": "Обеспечен" if status == "ready" else "Частично обеспечен",
        }
        for product, status in zip(
            db_session.query(ProductionProduct).filter(ProductionProduct.item_id == item.item_id).order_by(ProductionProduct.product_id).all(),
            ["ready", "partial"],
        )
    }
    all_rows = list_journal(db_session, _material_coverage_by_product=material_coverage)["rows"]
    partial_rows = list_journal(
        db_session,
        coverage_status="partial",
        _material_coverage_by_product=material_coverage,
    )["rows"]

    assert {row["order_number"] for row in all_rows} == {"COV-1", "COV-2"}
    assert [row["order_number"] for row in partial_rows] == ["COV-2"]


def test_journal_sorts_by_planned_start_date(db_session):
    item = Item(item_code="P-SORT", item_name="Sort part", unit="шт", status="active")
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


def test_create_material_issues_retry_reuses_live_custody_without_refresh(db_session):
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
                status="active",
    )
    comp = Item(
        item_code="C-IDEM",
        item_name="Component",
        item_article="ART-C-IDEM",
        unit="м",
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
    assert second["created"] == []
    assert [row["issue_id"] for row in second["reused"]] == [first["created"][0]["issue_id"]]

    # And only one row physically exists.
    from app.models import ProductionMaterialIssue
    assert (
        db_session.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .count()
        == 1
    )


def test_exported_transfer_quantity_change_uses_live_custody(db_session):
    parent = Item(
        item_code="P-REEXP",
        item_name="Parent reexport",
        item_article="ART-P-REEXP",
        unit="шт",
                status="active",
    )
    comp = Item(
        item_code="C-REEXP",
        item_name="Component reexport",
        item_article="ART-C-REEXP",
        unit="м",
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
    assert (
        db_session.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .count()
        == 1
    )
    line = db_session.query(ProductionMaterialIssueLine).filter_by(issue_id=issue_id).one()
    assert float(line.required_qty) == 3.0


def test_posted_transfer_retry_reuses_live_custody(db_session):
    parent = Item(
        item_code="P-POSTED-REUSE",
        item_name="Parent posted reuse",
        item_article="ART-P-POSTED-REUSE",
        unit="шт",
                status="active",
    )
    comp = Item(
        item_code="C-POSTED-REUSE",
        item_name="Component posted reuse",
        item_article="ART-C-POSTED-REUSE",
        unit="шт",
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
    assert [row["issue_id"] for row in second["reused"]] == [issue_id]
    assert (
        db_session.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .count()
        == 1
    )


def test_existing_issue_source_change_reuses_original_live_issue(db_session):
    parent = Item(item_code="P-SRC-REUSE", item_name="Parent source reuse", unit="шт", status="active")
    comp = Item(item_code="C-SRC-REUSE", item_name="Component source reuse", unit="шт", status="active")
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
    assert [row["issue_id"] for row in second["reused"]] == [first["created"][0]["issue_id"]]
    assert (
        db_session.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id == product.product_id)
        .count()
        == 1
    )


@pytest.mark.parametrize(
    "bad_delta",
    [None, "", True, math.nan, math.inf, -math.inf],
)
def test_append_material_issue_custody_event_rejects_invalid_delta(db_session, bad_delta):
    item = Item(
        item_code="P-CUSTODY-INVALID",
        item_name="Parent custody invalid",
        unit="шт",
        status="active",
    )
    component = Item(
        item_code="C-CUSTODY-INVALID",
        item_name="Component custody invalid",
        unit="шт",
        status="active",
    )
    db_session.add_all([item, component])
    db_session.flush()
    order = ProductionOrder(
        order_number="CUSTODY-INVALID-1",
        order_date=datetime(2026, 6, 4),
        deletion_mark=False,
    )
    db_session.add(order)
    db_session.flush()
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
    issue = ProductionMaterialIssue(
        document_number="CUSTODY-DQ-1",
        product_id=product.product_id,
        order_id=order.order_id,
        status="requested",
        direction="issue",
        source_warehouse_ref1c="source-a",
        warehouse_ref1c="dest-a",
    )
    db_session.add(issue)
    db_session.flush()
    line = ProductionMaterialIssueLine(
        issue_id=issue.issue_id,
        component_item_id=component.item_id,
        required_qty=2,
        issued_qty=0,
        unit="шт",
        line_status="new",
    )
    db_session.add(line)
    db_session.flush()

    with pytest.raises(ValueError, match="numeric field"):
        append_material_issue_custody_event(
            db_session,
            issue=issue,
            line=line,
            delta_qty=bad_delta,
            source_kind="manual",
            location_kind="transit",
            warehouse_ref1c="source-a",
        )


def test_delete_local_material_issue_only_before_1c(db_session):
    parent = Item(item_code="P-DEL-ISSUE", item_name="Parent delete issue", unit="шт", status="active")
    comp = Item(item_code="C-DEL-ISSUE", item_name="Component delete issue", unit="шт", status="active")
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
    events = (
        db_session.query(ProductionMaterialCustodyEvent)
        .filter(ProductionMaterialCustodyEvent.product_id == product.product_id)
        .all()
    )
    assert len(events) == 2, [(e.source_kind, float(e.delta_qty)) for e in events]
    release_events = [
        e for e in events
        if e.source_kind == "terminal_release"
    ]
    assert len(release_events) == 1
    assert release_events[0].location_kind == "transit"
    assert release_events[0].delta_qty == pytest.approx(-5.0)


def test_cancel_local_order_without_1c_marks_deleted_and_removes_local_issues(db_session):
    item = Item(item_code="P-DEL-ORDER", item_name="Parent delete order", unit="шт", status="active")
    comp = Item(item_code="C-DEL-ORDER", item_name="Component delete order", unit="шт", status="active")
    db_session.add_all([item, comp])
    db_session.flush()
    run = PlanningRun(status="DONE", config_snapshot=json.dumps({}))
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        net_required_qty=5,
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
    assert not hasattr(req, "covered_qty")
    assert not hasattr(req, "remaining_qty")
    events = (
        db_session.query(ProductionMaterialCustodyEvent)
        .filter(ProductionMaterialCustodyEvent.product_id == product.product_id)
        .all()
    )
    assert len(events) >= 1
    release_events = [
        e for e in events if e.source_kind == "terminal_release"
    ]
    assert len(release_events) == 1
    assert release_events[0].location_kind == "transit"
    assert release_events[0].delta_qty < 0
    assert list_journal(db_session)["total"] == 0


def test_cancel_local_order_allows_local_delete_with_planned_1c_link(db_session):
    item = Item(item_code="P-DEL-ORDER-PLAN", item_name="Parent delete order planned", unit="шт", status="active")
    comp = Item(item_code="C-DEL-ORDER-PLAN", item_name="Component delete order planned", unit="шт", status="active")
    db_session.add_all([item, comp])
    db_session.flush()
    run = PlanningRun(status="DONE", config_snapshot=json.dumps({}))
    db_session.add(run)
    db_session.flush()
    req = MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        net_required_qty=4,
        period_from=_dt.date(2026, 6, 1),
        period_to=_dt.date(2026, 6, 30),
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    spec = Specification(spec_name="Delete order planned spec", spec_ref1c="spec-delete-order-planned")
    db_session.add(spec)
    db_session.flush()
    _route_spec_to_workshop(db_session, spec, "spec-delete-order-planned")
    db_session.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db_session.add(SpecComponent(spec_id=spec.spec_id, item_id=comp.item_id, quantity=1))
    order = ProductionOrder(order_number="MRP-RC-1-2", order_date=datetime(2026, 6, 4), deletion_mark=False, source="mrp", source_run_id=run.run_id)
    db_session.add(order)
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=4,
        produced_qty=0,
        remaining_qty=4,
        source_mrp_requirement_id=req.id,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        SyncLink(
            source_system="PRODPLAN",
            source_doctype="production_order",
            source_id=order.order_id,
            target_entity=PRODUCTION_ORDER_ENTITY,
            target_number="planned-1c-link",
            status="planned",
        )
    )
    db_session.commit()
    create_material_issues(db_session, [product.product_id], source_warehouse_ref1c="src-a")

    result = cancel_local_order(db_session, product.product_id)

    db_session.refresh(order)
    assert result["deleted_issues"] == 1
    assert order.deletion_mark is True
    assert list_journal(db_session)["total"] == 0


def test_material_issue_journal_shows_warehouse_names_and_filters_source(db_session):
    parent = Item(
        item_code="P-WH",
        item_name="Warehouse parent",
        unit="шт",
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
        produced_qty=2,
        remaining_qty=999,
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
    assert result["rows"][0]["remaining_qty"] == 3
    assert {row["warehouse_name"] for row in result["source_warehouses"]} == {
        "Склад отправитель A",
        "Склад отправитель B",
    }


def test_material_issue_journal_hides_completed_orders(db_session):
    item = Item(item_code="P-MI-DONE", item_name="Parent done issue", unit="шт", status="active")
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
    item = Item(item_code="P-MI-PRODUCED", item_name="Parent produced issue", unit="шт", status="active")
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


def _make_basic_spec(db, parent_name="Parent", child_specs=()):
    """Helper that wires Item + Specification + SpecComponents + DefaultSpecification."""
    parent = Item(
        item_code=f"P-{parent_name}",
        item_name=parent_name,
        item_article=f"ART-{parent_name}",
        unit="шт",
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
                        status="active",
        )
        db.add(comp)
        db.flush()
        if stock:
            db.add(
                _accepted_stock_bin(
                    db,
                    comp.item_id,
                    f"WH-{code}",
                    float(stock),
                )
            )
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

    preview = preview_materials(db_session, product.product_id)

    assert preview["coverage"] == "ready"
    for c in preview["components"]:
        assert c["coverage"] == "ok"
        assert c["missing_qty"] == 0
        assert c["eta_dates"] == []


@pytest.mark.parametrize(
    "bad_qty",
    [None, "", True, math.nan, math.inf, -math.inf],
)
def test_preview_materials_rejects_invalid_product_remaining_qty(
    db_session,
    monkeypatch,
    bad_qty,
):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="PreviewBadRemaining",
        child_specs=[("PBB1", "Component", 1, 1)],
    )
    _order, product = _make_internal_order_for(db_session, parent, qty=1)

    monkeypatch.setattr(
        material_availability,
        "accepted_product_output",
        lambda _product: SimpleNamespace(remaining_qty=bad_qty),
    )

    with pytest.raises(ValueError, match="numeric field"):
        preview_materials(db_session, product.product_id)


def test_preview_materials_rejects_when_future_supply_unavailable(db_session):
    generation = db_session.get(
        LedgerGeneration,
        int(db_session.info["production_journal_generation_id"]),
    )
    if generation is None:
        raise RuntimeError("production_journal_generation_id must be configured")
    generation.capabilities = {
        **dict(generation.capabilities or {}),
        "future_supply": False,
    }
    db_session.flush()

    parent, _spec, _components = _make_basic_spec(
        db_session,
        parent_name="PreviewFutureSupplyUnavailable",
        child_specs=[("PFSU1", "Component", 1, 1)],
    )
    _order, product = _make_internal_order_for(db_session, parent, qty=1)

    with pytest.raises(PlanningTruthUnavailable):
        preview_materials(db_session, product.product_id)


@pytest.mark.parametrize(
    "bad_on_hand",
    [None, "", True, math.nan, math.inf, -math.inf],
)
def test_preview_materials_rejects_invalid_item_ledger_position(db_session, monkeypatch, bad_on_hand):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="PreviewBadPosition",
        child_specs=[("PBB2", "Component", 1, 1)],
    )
    _order, product = _make_internal_order_for(db_session, parent, qty=1)
    import app.services.item_ledger as item_ledger_package

    def fake_item_ledger_position(_db, item_ids, **_kwargs):
        return {
            int(item_id): {
                "on_hand": bad_on_hand,
                "available": 0,
                "projected": 0,
                "uncovered": 0,
            }
            for item_id in item_ids
        }

    monkeypatch.setattr(item_ledger_package, "item_ledger_position", fake_item_ledger_position)

    with pytest.raises(ValueError, match="numeric field"):
        preview_materials(db_session, product.product_id, ledger_generation_id=int(db_session.info["production_journal_generation_id"]))


def test_live_posted_issue_does_not_mutate_accepted_custody_coverage(db_session):
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

    preview = preview_materials(db_session, product.product_id)
    only = preview["components"][0]

    assert only["required_qty"] == 4
    assert only["available_qty"] > 0
    assert only["reserved_at_workshop_qty"] == 0
    assert only["missing_qty"] == 0
    assert only["coverage"] == "ok"
    assert preview["coverage_status"] == "ready"

    row = list_journal(
        db_session,
        product_id=product.product_id,
        _material_coverage_by_product={product.product_id: preview},
    )["rows"][0]
    assert row["coverage_status"] == "ready"
    assert row["coverage_label"] == "Обеспечен"


def test_journal_uses_candidate_material_coverage_for_coverage_band_rows(db_session):
    parent, _spec, _comps = _make_basic_spec(
        db_session,
        parent_name="JournalReadyParent",
        child_specs=[("JRC1", "Journal component enough", 10, 1)],
    )
    _order, product = _make_internal_order_for(db_session, parent, qty=2)
    _order.source = "mrp"
    _order.source_run_id = _journal_mrp_run(db_session).run_id
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
    db_session.commit()

    material = preview_materials(db_session, product.product_id)
    row = list_journal(
        db_session,
        product_id=product.product_id,
        _material_coverage_by_product={product.product_id: material},
    )["rows"][0]

    assert row["coverage_status"] == "ready"
    assert row["coverage_label"] == "Обеспечен"
    assert row["status"] == "created"


def test_preview_materials_reads_ledger_future_supply_for_target_generation(db_session):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="ShortageParent",
        child_specs=[
            ("SC1", "No stock comp", 0, 1),  # need 1*2 = 2, have 0
        ],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)
    _ledger_future_supply(
        db_session,
        item_id=comp.item_id,
        source_ref="ЗАКП-COVER-001",
        eta_date=_dt.date(2026, 6, 1),
        open_qty=2.0,
    )
    db_session.commit()

    preview = preview_materials(
        db_session,
        product.product_id,
        ledger_generation_id=int(db_session.info["production_journal_generation_id"]),
    )
    assert preview["coverage"] == "shortage"
    only_comp = preview["components"][0]
    assert only_comp["coverage"] == "shortage"
    assert only_comp["missing_qty"] == 2
    # ETA from supplier_order LedgerFutureSupply row.
    eta = only_comp["eta_dates"]
    assert len(eta) >= 1
    sup_etas = [e for e in eta if e["source"] == "supplier_order"]
    assert sup_etas
    assert sup_etas[0]["ref"] == "ЗАКП-COVER-001"
    assert sup_etas[0]["date"] == "2026-06-01"


def test_preview_materials_includes_live_source_mutations_only_if_in_ledger_supply(db_session):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="MutationIgnoredParent",
        child_specs=[
            ("MIP1", "No stock comp", 0, 1),
        ],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    # A mutable legacy supplier order exists, but it must not be used by ETA
    # if LedgerFutureSupply has no exact rows for this generation.
    sup_order = SupplierOrder(
        order_number="ZAKP-LIVE-IGNORED",
        order_date=datetime(2026, 5, 10, 10),
        order_ref1c="sup-ref-ignore",
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
            remaining_qty=10,
            delivery_date=datetime(2026, 6, 1),
        )
    )
    db_session.commit()

    preview = preview_materials(
        db_session,
        product.product_id,
        ledger_generation_id=int(db_session.info["production_journal_generation_id"]),
    )
    only_comp = preview["components"][0]
    assert only_comp["eta_dates"] == []


def test_preview_materials_includes_ledger_wip_supply_eta(db_session):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="ProductionEtaParent",
        child_specs=[
            ("PEC1", "Production ETA component", 0, 3),  # need 3*2 = 6, have 0
        ],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    _ledger_future_supply(
        db_session,
        item_id=comp.item_id,
        source_ref="MRP-RC-13-PEC1",
        source_line_ref="1",
        kind="wip_order",
        eta_date=_dt.date(2026, 6, 30),
        open_qty=10,
    )
    preview = preview_materials(db_session, product.product_id)
    only_comp = preview["components"][0]
    prod_etas = [e for e in only_comp["expected_dates"] if e["source"] == "wip_order"]

    assert only_comp["coverage"] == "shortage"
    assert prod_etas
    assert prod_etas[0]["order_number"] == "MRP-RC-13-PEC1"
    assert prod_etas[0]["date"] == "2026-06-30"
    assert prod_etas[0]["qty"] == 10


def test_preview_materials_without_ledger_future_supply_rows_does_not_fabricate_eta(db_session):
    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="NoFutureRowsParent",
        child_specs=[
            ("NFS1", "No stock comp", 0, 1),
        ],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    preview = preview_materials(db_session, product.product_id)
    only_comp = preview["components"][0]
    assert only_comp["coverage"] == "shortage"
    assert only_comp["eta_dates"] == []
    assert only_comp["expected_dates"] == []


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

    preview = preview_materials(db_session, product.product_id)
    assert preview["coverage"] == "partial"
    by_name = {c["item_name"]: c for c in preview["components"]}
    assert by_name["Comp full"]["coverage"] == "ok"
    assert by_name["Comp partial"]["coverage"] == "partial"
    assert by_name["Comp partial"]["missing_qty"] == 1
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
                status="active",
    )
    comp = Item(
        item_code="WH-COMP",
        item_name="Comp",
        item_article="WH-C",
        unit="м",
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
    # The next command folds the first command's local custody tail immediately;
    # it does not wait for a new physical generation.
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

    second = create_material_issues(
        db_session,
        [product2.product_id],
        initiated_by="op",
        warehouse_ref1c="bbbb2222-bbbb-2222-bbbb-222222222222",
    )
    assert len(second["created"]) == 1


def test_create_material_issues_splits_components_by_source_warehouse(db_session):
    from app.models import (
        ProductionMaterialIssue,
        ProductionResource,
        StockWarehouse,
        WorkshopWarehouseBinding,
    )

    workshop = ProductionResource(resource_name="Сварочный участок")
    db_session.add(workshop)
    db_session.flush()

    parent = Item(item_code="SRC-PARENT", item_name="Parent", item_article="SRC-P", unit="шт", status="active")
    comp_a = Item(item_code="SRC-A", item_name="Comp A", item_article="SRC-A", unit="шт", status="active")
    comp_b = Item(item_code="SRC-B", item_name="Comp B", item_article="SRC-B", unit="шт", status="active")
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
    # No stock on WH-DEST: components already on the destination workshop are
    # claimed as zero-distance workshop issues, which is covered by
    # test_section_stock_reservations.py.
    db_session.add_all([
        _accepted_stock_bin(db_session, comp_a.item_id, "WH-A", 5),
        _accepted_stock_bin(db_session, comp_b.item_id, "WH-B", 5),
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

    parent = Item(item_code="DST-PARENT", item_name="Parent", item_article="DST-P", unit="шт", status="active")
    comp = Item(item_code="DST-C", item_name="Comp", item_article="DST-C", unit="шт", status="active")
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
    db_session.add(_accepted_stock_bin(db_session, comp.item_id, "WH-DEST", 2))
    db_session.commit()

    result = create_material_issues(db_session, [product.product_id], initiated_by="op")

    assert len(result["created"]) == 1
    assert result["created"][0]["direction"] == "issue"
    assert result["reused"] == []
    assert result["selection_required"] == []
    assert result["errors"] == []
    assert result["already_on_destination"][0]["components"][0]["covered_qty"] == 2
    issue = db_session.query(ProductionMaterialIssue).one()
    assert issue.direction == "issue"
    assert issue.status == "posted"
    assert issue.source_warehouse_ref1c == "WH-DEST"
    assert issue.warehouse_ref1c == "WH-DEST"
    assert issue.lines[0].required_qty == 2
    assert issue.lines[0].issued_qty == 2


def test_create_material_issues_moves_only_missing_qty_when_partially_on_destination(db_session):
    workshop = ProductionResource(resource_name="Участок частичного покрытия")
    db_session.add(workshop)
    db_session.flush()

    parent = Item(item_code="PART-PARENT", item_name="Parent", item_article="PART-P", unit="шт", status="active")
    comp = Item(item_code="PART-C", item_name="Comp", item_article="PART-C", unit="шт", status="active")
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
        _accepted_stock_bin(db_session, comp.item_id, "WH-DEST", 2),
        _accepted_stock_bin(db_session, comp.item_id, "WH-SRC", 10),
    ])
    db_session.commit()

    result = create_material_issues(db_session, [product.product_id], initiated_by="op")

    assert len(result["created"]) == 2
    assert {str(row.get("source_warehouse_ref1c") or "") for row in result["created"]} == {"WH-DEST", "WH-SRC"}
    assert result["already_on_destination"][0]["components"][0]["covered_qty"] == 2
    issues = db_session.query(ProductionMaterialIssue).order_by(ProductionMaterialIssue.issue_id).all()
    assert [issue.direction for issue in issues] == ["issue", "issue"]
    assert issues[0].source_warehouse_ref1c == "WH-DEST"
    assert issues[0].warehouse_ref1c == "WH-DEST"
    assert issues[0].lines[0].required_qty == 2
    assert issues[0].lines[0].issued_qty == 2
    assert issues[1].source_warehouse_ref1c == "WH-SRC"
    assert issues[1].lines[0].required_qty == 3


def test_create_material_issues_does_not_reuse_destination_stock_for_multiple_products(db_session):
    workshop = ProductionResource(resource_name="Участок общего остатка")
    db_session.add(workshop)
    db_session.flush()

    parent = Item(item_code="SHARED-PARENT", item_name="Parent", item_article="SHARED-P", unit="шт", status="active")
    comp = Item(item_code="SHARED-C", item_name="Comp", item_article="SHARED-C", unit="шт", status="active")
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
        _accepted_stock_bin(db_session, comp.item_id, "WH-DEST", 2),
        _accepted_stock_bin(db_session, comp.item_id, "WH-SRC", 10),
    ])
    db_session.commit()

    result = create_material_issues(db_session, [p.product_id for p in products], initiated_by="op")

    assert len(result["created"]) == 2
    assert {str(row.get("source_warehouse_ref1c") or "") for row in result["created"]} == {"WH-DEST", "WH-SRC"}
    assert len(result["already_on_destination"]) == 1
    issues = db_session.query(ProductionMaterialIssue).order_by(ProductionMaterialIssue.issue_id).all()
    assert [issue.direction for issue in issues] == ["issue", "issue"]
    assert issues[0].product_id == products[0].product_id
    assert issues[0].lines[0].required_qty == 2
    assert issues[1].product_id == products[1].product_id
    assert issues[1].lines[0].required_qty == 2


def test_create_material_issues_asks_when_component_has_multiple_source_warehouses(db_session):
    from app.models import (
        ProductionMaterialIssue,
        ProductionResource,
        StockWarehouse,
        WorkshopWarehouseBinding,
    )

    workshop = ProductionResource(resource_name="Сборочный участок")
    db_session.add(workshop)
    db_session.flush()

    parent = Item(item_code="AMB-PARENT", item_name="Parent", item_article="AMB-P", unit="шт", status="active")
    comp = Item(item_code="AMB-C", item_name="Comp", item_article="AMB-C", unit="шт", status="active")
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
        _accepted_stock_bin(db_session, comp.item_id, "WH-A", 5),
        _accepted_stock_bin(db_session, comp.item_id, "WH-B", 7),
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

    The accepted Ledger contour excludes stock sitting in ignored warehouses.
    """
    from app.services.production_control_settings import upsert_ignored_warehouse

    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="IgnoreCheckParent",
        child_specs=[("IGNCMP", "Ignored-stock comp", 0, 1)],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    db_session.add(
        _accepted_stock_bin(
            db_session, comp.item_id, "brak-warehouse-guid", 10
        )
    )
    db_session.commit()

    # Before adding to the ignore list: coverage should be 'ready' (10 >= 2).
    preview = preview_materials(db_session, product.product_id)
    assert preview["coverage"] == "ready"
    assert preview["components"][0]["available_qty"] == 10
    assert preview["components"][0]["coverage"] == "ok"

    # Mark brak warehouse as ignored — that stock should drop out.
    upsert_ignored_warehouse(
        db_session,
        "brak-warehouse-guid",
        warehouse_name="Изолятор брака",
        reason="Бракованные комплектующие",
    )

    preview_after = preview_materials(db_session, product.product_id)
    assert preview_after["coverage"] == "shortage"
    only_comp = preview_after["components"][0]
    assert only_comp["available_qty"] == 0
    assert only_comp["missing_qty"] == 2
    assert only_comp["coverage"] == "shortage"


def test_preview_materials_does_not_fall_back_to_aggregated_stock(db_session):
    from app.services.production_control_settings import upsert_ignored_warehouse

    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="FallbackParent",
        child_specs=[("FBCMP", "Aggregated-only comp", 0, 1)],
    )
    comp = comps[0]
    _order, product = _make_internal_order_for(db_session, parent, qty=2)
    db_session.commit()

    upsert_ignored_warehouse(db_session, "some-other-warehouse")

    preview = preview_materials(db_session, product.product_id)
    assert preview["coverage"] == "shortage"
    assert preview["components"][0]["available_qty"] == 0
    assert preview["components"][0]["coverage"] == "shortage"


def test_preview_materials_uses_only_nonignored_ledger_bins(db_session):
    from app.services.production_control_settings import upsert_ignored_warehouse

    parent, _spec, comps = _make_basic_spec(
        db_session,
        parent_name="MixParent",
        child_specs=[
            ("MIXA", "Comp A all-ignored", 0, 1),
            ("MIXB", "Comp B ledger stock", 0, 1),
        ],
    )
    comp_a, _comp_b = comps
    _order, product = _make_internal_order_for(db_session, parent, qty=2)

    db_session.add(
        _accepted_stock_bin(db_session, comp_a.item_id, "brak-mix-guid", 50)
    )
    db_session.add(
        _accepted_stock_bin(db_session, comps[1].item_id, "normal-mix-guid", 50)
    )
    db_session.commit()
    upsert_ignored_warehouse(db_session, "brak-mix-guid")

    preview = preview_materials(db_session, product.product_id)
    assert preview["coverage"] == "shortage"  # blocked by comp A
    by_name = {c["item_name"]: c for c in preview["components"]}
    assert by_name["Comp A all-ignored"]["available_qty"] == 0
    assert by_name["Comp A all-ignored"]["coverage"] == "shortage"
    assert by_name["Comp B ledger stock"]["available_qty"] == 50
    assert by_name["Comp B ledger stock"]["coverage"] == "ok"

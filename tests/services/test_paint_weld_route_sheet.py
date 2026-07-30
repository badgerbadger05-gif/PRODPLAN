"""Этап 3 связки «окраска↔сварка»: маршрутный лист двумя блоками операций.

Для цепочки paint_weld_chain_links печатается ОДИН лист:
- состав («Материалы и заготовки») — от сварной детали;
- операции двумя блоками: «Сварка — заказ 1С №…» и «Окраска — заказ 1С №…»;
- перемещения материалов обоих заказов;
- при выборе обеих строк цепочки дубль не печатается;
- печать с любой стороны цепочки даёт один и тот же комбинированный лист.
"""

from datetime import datetime

from app.models import (
    DefaultSpecification,
    Item,
    Operation,
    PaintWeldChainLink,
    PaintWeldPair,
    ProductionMaterialIssue,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ProductionStage,
    SpecComponent,
    SpecOperation,
    Specification,
    StockWarehouse,
    SyncLink,
)
from app.services.one_c_production_order_export import PRODUCTION_ORDER_ENTITY
from app.services.production_control_printing import (
    mark_route_sheets_printed,
    render_route_sheets_html,
)


def _setup_chain(db):
    """Пара окрашенная/сварная, спеки, пара заказов с цепочкой и 1С-номерами."""
    painted = Item(
        item_code="P-PAINTED",
        item_name="Кронштейн после покраски",
        item_article="ART-P",
        unit="шт",
        status="active",
    )
    welded = Item(
        item_code="P-WELDED",
        item_name="Кронштейн после сварки",
        item_article="ART-W",
        unit="шт",
        status="active",
    )
    metal = Item(
        item_code="M-STEEL",
        item_name="Труба стальная 40х40",
        item_article="ART-M",
        unit="м",
        status="active",
    )
    db.add_all([painted, welded, metal])
    db.flush()

    weld_spec = Specification(spec_name="Спека сварки", spec_ref1c="spec-weld")
    paint_spec = Specification(spec_name="Спека окраски", spec_ref1c="spec-paint")
    weld_stage = ProductionStage(stage_name="Сварочный участок", stage_order=1)
    paint_stage = ProductionStage(stage_name="Окрасочный участок", stage_order=2)
    weld_op = Operation(operation_ref1c="op-weld", operation_name="Сварка каркаса")
    paint_op = Operation(operation_ref1c="op-paint", operation_name="Покраска порошковая")
    db.add_all([weld_spec, paint_spec, weld_stage, paint_stage, weld_op, paint_op])
    db.flush()

    db.add_all(
        [
            DefaultSpecification(item_id=welded.item_id, spec_id=weld_spec.spec_id),
            DefaultSpecification(item_id=painted.item_id, spec_id=paint_spec.spec_id),
            SpecComponent(
                spec_id=weld_spec.spec_id,
                item_id=metal.item_id,
                quantity=2.5,
                component_type="Материал",
            ),
            SpecComponent(
                spec_id=paint_spec.spec_id,
                item_id=welded.item_id,
                quantity=1,
                component_type="Сборка",
            ),
            SpecOperation(
                spec_id=weld_spec.spec_id,
                stage_id=weld_stage.stage_id,
                operation_id=weld_op.operation_id,
                time_norm=0.5,
            ),
            SpecOperation(
                spec_id=paint_spec.spec_id,
                stage_id=paint_stage.stage_id,
                operation_id=paint_op.operation_id,
                time_norm=0.2,
            ),
        ]
    )

    pair = PaintWeldPair(
        painted_item_id=painted.item_id, welded_item_id=welded.item_id, source="auto"
    )
    db.add(pair)
    db.flush()

    paint_order = ProductionOrder(
        order_number="MRP-PAINT-1",
        order_date=datetime(2026, 7, 18),
        deletion_mark=False,
        source="mrp",
    )
    weld_order = ProductionOrder(
        order_number="MRP-WELD-1",
        order_date=datetime(2026, 7, 18),
        deletion_mark=False,
        source="mrp",
    )
    db.add_all([paint_order, weld_order])
    db.flush()

    paint_product = ProductionProduct(
        order_id=paint_order.order_id,
        item_id=painted.item_id,
        spec_id=paint_spec.spec_id,
        line_number=1,
        quantity=10,
        produced_qty=0,
        remaining_qty=10,
    )
    weld_product = ProductionProduct(
        order_id=weld_order.order_id,
        item_id=welded.item_id,
        spec_id=weld_spec.spec_id,
        line_number=1,
        quantity=6,
        produced_qty=0,
        remaining_qty=6,
    )
    db.add_all([paint_product, weld_product])
    db.flush()

    db.add_all(
        [
            SyncLink(
                source_system="PRODPLAN",
                source_doctype="production_order",
                source_id=paint_order.order_id,
                target_entity=PRODUCTION_ORDER_ENTITY,
                target_ref_key="ref-1c-paint",
                target_number="1C-PAINT-1",
                status="success",
            ),
            SyncLink(
                source_system="PRODPLAN",
                source_doctype="production_order",
                source_id=weld_order.order_id,
                target_entity=PRODUCTION_ORDER_ENTITY,
                target_ref_key="ref-1c-weld",
                target_number="1C-WELD-1",
                status="success",
            ),
            PaintWeldChainLink(
                painted_order_id=paint_order.order_id,
                welded_order_id=weld_order.order_id,
                pair_id=pair.id,
            ),
        ]
    )
    db.commit()
    return paint_product, weld_product, painted, welded, metal


def test_chain_route_sheet_is_single_with_two_operation_blocks(db_session):
    paint_product, weld_product, painted, welded, metal = _setup_chain(db_session)

    html = render_route_sheets_html(db_session, [paint_product.product_id])

    assert html.count('<section class="sheet">') == 1
    # два блока операций с 1С-номерами соответствующих заказов
    assert "Сварка — заказ 1С №1C-WELD-1" in html
    assert "Окраска — заказ 1С №1C-PAINT-1" in html
    assert "Сварка каркаса" in html
    assert "Покраска порошковая" in html
    # состав — от сварной детали (сырьё), а не сама сварная деталь
    assert "Труба стальная 40х40" in html
    # 2.5 м/ед × 6 шт сварки
    assert "15.000" in html
    # шапка листа — по окрасочному (родительскому) заказу
    assert "Кронштейн после покраски" in html


def test_route_sheet_quantities_ignore_corrupt_remaining_cache(db_session):
    paint_product, weld_product, *_ = _setup_chain(db_session)
    paint_product.produced_qty = 2
    paint_product.remaining_qty = 999
    weld_product.produced_qty = 2
    weld_product.remaining_qty = 999
    db_session.commit()

    html = render_route_sheets_html(db_session, [paint_product.product_id])

    # Weld remaining is 6 - 2 = 4, therefore steel is 4 * 2.5 = 10.
    assert "Сварка — заказ 1С №1C-WELD-1 · 4 шт" in html
    assert "10.000" in html
    # Paint quantity is 10 - 2 = 8.
    assert "Окраска — заказ 1С №1C-PAINT-1 · 8 шт" in html
    assert "999 шт" not in html


def test_chain_route_sheet_deduplicates_both_sides_selected(db_session):
    paint_product, weld_product, *_ = _setup_chain(db_session)

    html = render_route_sheets_html(
        db_session, [paint_product.product_id, weld_product.product_id]
    )

    assert html.count('<section class="sheet">') == 1
    assert "Листов: 1" in html


def test_chain_route_sheet_from_welded_side_renders_same_combined_sheet(db_session):
    paint_product, weld_product, *_ = _setup_chain(db_session)

    html = render_route_sheets_html(db_session, [weld_product.product_id])

    assert html.count('<section class="sheet">') == 1
    assert "Сварка — заказ 1С №1C-WELD-1" in html
    assert "Окраска — заказ 1С №1C-PAINT-1" in html
    assert "Кронштейн после покраски" in html


def test_chain_route_sheet_includes_transfers_of_both_orders(db_session):
    paint_product, weld_product, *_ = _setup_chain(db_session)
    db = db_session
    for ref, name in (("wh-metal", "Склад металла"), ("wh-weld", "Участок сварочный"), ("wh-paint", "Участок окраски")):
        db.add(StockWarehouse(warehouse_ref1c=ref, warehouse_code=ref, warehouse_name=name))
    db.add_all(
        [
            ProductionMaterialIssue(
                document_number="MT-WELD-1",
                product_id=weld_product.product_id,
                order_id=weld_product.order_id,
                status="requested",
                direction="issue",
                source_warehouse_ref1c="wh-metal",
                warehouse_ref1c="wh-weld",
            ),
            ProductionMaterialIssue(
                document_number="MT-PAINT-1",
                product_id=paint_product.product_id,
                order_id=paint_product.order_id,
                status="requested",
                direction="issue",
                source_warehouse_ref1c="wh-weld",
                warehouse_ref1c="wh-paint",
            ),
        ]
    )
    db.commit()

    html = render_route_sheets_html(db_session, [paint_product.product_id])

    assert "MT-WELD-1" in html
    assert "MT-PAINT-1" in html


def test_mark_printed_stamps_both_chain_sides(db_session):
    paint_product, weld_product, *_ = _setup_chain(db_session)

    marked = mark_route_sheets_printed(db_session, [paint_product.product_id])

    assert marked == 1
    states = {
        int(state.product_id): state
        for state in db_session.query(ProductionOrderLineState).all()
    }
    assert states[int(paint_product.product_id)].route_sheet_printed_at is not None
    assert states[int(weld_product.product_id)].route_sheet_printed_at is not None


def test_route_sheet_without_chain_unchanged(db_session):
    """Обычный (нецепочечный) продукт печатается по-старому — без блоков."""
    paint_product, weld_product, *_ = _setup_chain(db_session)
    db = db_session
    solo_item = Item(
        item_code="SOLO",
        item_name="Одиночная деталь",
        item_article="ART-S",
        unit="шт",
        status="active",
    )
    db.add(solo_item)
    db.flush()
    solo_order = ProductionOrder(
        order_number="MRP-SOLO-1",
        order_date=datetime(2026, 7, 18),
        deletion_mark=False,
        source="mrp",
    )
    db.add(solo_order)
    db.flush()
    solo_product = ProductionProduct(
        order_id=solo_order.order_id,
        item_id=solo_item.item_id,
        line_number=1,
        quantity=3,
        produced_qty=0,
        remaining_qty=3,
    )
    db.add(solo_product)
    db.commit()

    html = render_route_sheets_html(db_session, [solo_product.product_id])

    assert html.count('<section class="sheet">') == 1
    assert "Одиночная деталь" in html
    assert "Сварка — заказ 1С" not in html
    assert "Окраска — заказ 1С" not in html

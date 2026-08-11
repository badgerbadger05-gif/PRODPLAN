"""Материальный поток пары «окраска ↔ сварка» (этап 2, п.2 ТЗ).

Фиксирует текущее (корректное) поведение: у окрасочного заказа единственный
компонент спеки — сварная деталь, и подбор склада-источника идёт по фактическому
остатку сварной. Значит сварная деталь для окраски перемещается со складов
сварных остатков (напр. «Участок сварочный»), а не выдумывается на пустом складе.

Это интеграционный тест, закрепляющий поведение штатного
`create_material_issues` для пары — без правок общего кода.
"""
from __future__ import annotations

from datetime import datetime

from app.models import (
    PaintWeldChainLink,
    PaintWeldPair,
    DefaultSpecification,
    Item,
    ProductionMaterialIssue,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SpecComponent,
    Specification,
    StockWarehouse,
    StockBin,
    PhysicalImportBatch,
    LedgerGeneration,
    PlanningTruthState,
)
from app.services.production_control_material_issues import create_material_issues
from app.services.production_material_custody_projection import (
    initialize_material_custody_baseline,
)
from app.services.planning_truth import publish_generation

PAINT_WH = "wh-paint"      # склад-получатель окрасочного участка
WELD_WH = "wh-weld-stock"  # склад сварных остатков («Участок сварочный»)


def _item(db, *, code: str, name: str) -> Item:
    it = Item(
        item_code=code,
        item_name=name,
        item_article=code,
        item_ref1c=f"ref-{code}",
        unit="шт",
                replenishment_method="Производство",
        status="active",
    )
    db.add(it)
    db.flush()
    return it


def test_paint_order_pulls_welded_component_from_weld_stock_warehouse(db_session):
    db = db_session
    cutoff = datetime(2026, 8, 1)
    batch = PhysicalImportBatch(batch_key="paint-material-truth", status="completed", cutoff=cutoff, source_watermarks={})
    generation = LedgerGeneration(generation_key="paint-material-truth", status="building", cutoff=cutoff, accepted_at=None, physical_import_batch=batch, source_watermarks={}, capabilities={}, algorithm_version="test")
    db.add_all((batch, generation))
    db.flush()
    initialize_material_custody_baseline(
        db,
        ledger_generation_id=int(generation.id),
        cells=[],
        observed_at=cutoff,
    )
    generation.status = "accepted"
    generation.accepted_at = cutoff
    publish_generation(db, generation)
    db.flush()
    db.expire_all()
    for idx, ref in enumerate((PAINT_WH, WELD_WH), start=1):
        db.add(StockWarehouse(warehouse_ref1c=ref, warehouse_code=f"W{idx}", warehouse_name=ref, is_selected=True))
    db.flush()

    painted = _item(db, code="PNT", name="Кронштейн после покраски")
    welded = _item(db, code="WLD", name="Кронштейн после сварки")

    paint_spec = Specification(spec_name="Окраска", spec_ref1c="spec-paint")
    db.add(paint_spec)
    db.flush()
    db.add(DefaultSpecification(item_id=painted.item_id, spec_id=paint_spec.spec_id))
    db.add(SpecComponent(spec_id=paint_spec.spec_id, item_id=welded.item_id, quantity=1, component_type="Сборка"))

    # сварная деталь лежит только на складе сварных остатков
    db.add(StockBin(ledger_generation_id=generation.id, item_id=welded.item_id, characteristic_ref="", organization_ref="", warehouse_ref1c=WELD_WH, on_hand=50))

    order = ProductionOrder(
        order_number="PWC-P-paint",
        order_date=datetime(2026, 8, 1),
        is_posted=True,
        deletion_mark=False,
        order_ref1c="paint-ord-ref",
        source="mrp",
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=painted.item_id,
        line_number=1,
        quantity=10,
        produced_qty=0,
        remaining_qty=10,
        spec_id=paint_spec.spec_id,
    )
    db.add(product)
    db.flush()
    db.add(ProductionOrderLineState(product_id=product.product_id, status="ready", issue_status="not_requested"))
    db.commit()

    # получатель — окрасочный склад; источник должен подобраться по остатку сварной
    result = create_material_issues(db, [product.product_id], warehouse_ref1c=PAINT_WH)

    assert result["errors"] == []
    assert result["selection_required"] == []

    # единственное перемещение (сварная деталь) — со склада сварных остатков
    issued = (
        db.query(ProductionMaterialIssue)
        .filter_by(product_id=product.product_id, direction="issue")
        .one()
    )
    assert issued.warehouse_ref1c == PAINT_WH        # получатель — окраска
    assert issued.source_warehouse_ref1c == WELD_WH  # источник — сварные остатки
    assert issued.order_id == order.order_id         # перемещение под окрасочным заказом

    # и это подтверждено в сводке created
    assert any(row["source_warehouse_ref1c"] == WELD_WH for row in result["created"])


def test_chain_launch_does_not_transfer_the_welded_part_it_produces(db_session):
    """Сварную деталь на окраску не возят: её приносит производство.

    Регресс: при запуске цепочки система заводила на окрасочный заказ
    перемещение сварной детали, которой ещё не существует ни на одном складе,
    не находила источник, требовала выбрать склад вручную — и весь запуск
    обрывался до экспорта в 1С и до печати маршрутного листа.
    """
    db = db_session
    cutoff = datetime(2026, 8, 1)
    batch = PhysicalImportBatch(batch_key="chain-no-transfer", status="completed", cutoff=cutoff, source_watermarks={})
    generation = LedgerGeneration(generation_key="chain-no-transfer", status="building", cutoff=cutoff, accepted_at=None, physical_import_batch=batch, source_watermarks={}, capabilities={}, algorithm_version="test")
    db.add_all((batch, generation))
    db.flush()
    initialize_material_custody_baseline(
        db,
        ledger_generation_id=int(generation.id),
        cells=[],
        observed_at=cutoff,
    )
    generation.status = "accepted"
    generation.accepted_at = cutoff
    publish_generation(db, generation)
    db.flush()
    db.expire_all()
    for idx, ref in enumerate((PAINT_WH, WELD_WH), start=1):
        db.add(StockWarehouse(warehouse_ref1c=ref, warehouse_code=f"C{idx}", warehouse_name=ref, is_selected=True))
    db.flush()

    painted = _item(db, code="CHN-PNT", name="Рычаг после покраски")
    welded = _item(db, code="CHN-WLD", name="Рычаг после сварки")

    paint_spec = Specification(spec_name="Окраска цепочки", spec_ref1c="spec-chain-paint")
    db.add(paint_spec)
    db.flush()
    db.add(DefaultSpecification(item_id=painted.item_id, spec_id=paint_spec.spec_id))
    db.add(SpecComponent(spec_id=paint_spec.spec_id, item_id=welded.item_id, quantity=1, component_type="Сборка"))
    # Сварной детали НЕТ нигде: её только предстоит сварить этим же запуском.

    pair = PaintWeldPair(
        painted_item_id=painted.item_id,
        welded_item_id=welded.item_id,
        source="auto",
        is_active=True,
    )
    db.add(pair)
    db.flush()

    paint_order = ProductionOrder(
        order_number="PWC-P-chain", order_date=datetime(2026, 8, 1), is_posted=True,
        deletion_mark=False, order_ref1c="chain-paint-ref", source="mrp",
    )
    weld_order = ProductionOrder(
        order_number="PWC-W-chain", order_date=datetime(2026, 8, 1), is_posted=True,
        deletion_mark=False, order_ref1c="chain-weld-ref", source="mrp",
    )
    db.add_all((paint_order, weld_order))
    db.flush()
    paint_product = ProductionProduct(
        order_id=paint_order.order_id, item_id=painted.item_id, line_number=1,
        quantity=10, produced_qty=0, remaining_qty=10, spec_id=paint_spec.spec_id,
    )
    weld_product = ProductionProduct(
        order_id=weld_order.order_id, item_id=welded.item_id, line_number=1,
        quantity=10, produced_qty=0, remaining_qty=10,
    )
    db.add_all((paint_product, weld_product))
    db.flush()
    db.add(ProductionOrderLineState(product_id=paint_product.product_id, status="ready", issue_status="not_requested"))
    db.add(PaintWeldChainLink(
        painted_order_id=paint_order.order_id,
        welded_order_id=weld_order.order_id,
        pair_id=pair.id,
    ))
    db.commit()

    result = create_material_issues(db, [paint_product.product_id], warehouse_ref1c=PAINT_WH)

    # Главное: запуск не встаёт на выборе склада для несуществующей детали.
    assert result["selection_required"] == []
    assert result["errors"] == []
    # И перемещения сварной детали не создаётся вовсе.
    assert (
        db.query(ProductionMaterialIssue)
        .filter_by(product_id=paint_product.product_id, direction="issue")
        .count()
        == 0
    )

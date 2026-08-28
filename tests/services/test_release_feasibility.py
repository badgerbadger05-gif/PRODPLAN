"""Тесты проверки выпуска: блокирующие узлы и материалы под заданное количество."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

import pytest

from app.models import (
    DefaultSpecification,
    IgnoredWarehouse,
    Item,
    LedgerGeneration,
    PhysicalImportBatch,
    PlanningTruthState,
    ProductionMaterialCustodyProjectionManifest,
    SpecComponent,
    Specification,
    StockBin,
    StockWarehouse,
)
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C
from app.services.release_feasibility import analyze_release, find_items, resolve_item

CUTOFF = datetime(2026, 8, 21, tzinfo=timezone.utc)
MAIN_WAREHOUSE = "wh-main"


@pytest.fixture(autouse=True)
def accepted_generation(db_session):
    """Остаток проверки — принятое поколение Item Ledger, а не legacy-таблица."""
    batch = PhysicalImportBatch(
        batch_key="release-feasibility", status="completed", cutoff=CUTOFF
    )
    generation = LedgerGeneration(
        generation_key="release-feasibility",
        status="accepted",
        cutoff=CUTOFF,
        accepted_at=CUTOFF,
        physical_import_batch=batch,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        algorithm_version="tests/release-feasibility",
    )
    db_session.add_all([batch, generation])
    db_session.flush()
    db_session.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    db_session.add(
        ProductionMaterialCustodyProjectionManifest(
            ledger_generation_id=int(generation.id),
            cutoff=CUTOFF,
            status="complete",
            is_baseline=True,
            source_event_high_watermark_id=0,
            observed_at=CUTOFF,
            built_at=CUTOFF,
        )
    )
    db_session.add(
        StockWarehouse(
            warehouse_ref1c=MAIN_WAREHOUSE,
            warehouse_name="Основной склад",
            is_selected=True,
        )
    )
    db_session.flush()
    db_session.expire_all()
    return generation


def _stock_bin(db, item: Item, qty: float, warehouse: str = MAIN_WAREHOUSE) -> StockBin:
    generation_id = int(db.query(PlanningTruthState).one().current_generation_id)
    row = StockBin(
        ledger_generation_id=generation_id,
        item_id=int(item.item_id),
        characteristic_ref="",
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c=warehouse,
        on_hand=qty,
    )
    db.add(row)
    db.flush()
    return row


def _mk_item(db, code: str, *, stock: float = 0.0, article: Optional[str] = None) -> Item:
    item = Item(
        item_code=code,
        item_name=f"Изделие {code}",
        item_article=article if article is not None else f"ART-{code}",
        unit="шт",
        status="active",
    )
    db.add(item)
    db.flush()
    if abs(float(stock)) > 1e-9:
        _stock_bin(db, item, float(stock))
    return item


def _mk_spec(db, owner: Item, components: Dict[Item, float]) -> Specification:
    spec = Specification(spec_code=f"SP-{owner.item_code}", spec_name=f"Спека {owner.item_code}")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=int(owner.item_id), spec_id=int(spec.spec_id)))
    for comp_item, qty in components.items():
        db.add(
            SpecComponent(
                spec_id=int(spec.spec_id),
                item_id=int(comp_item.item_id),
                quantity=qty,
                component_type="Материал",
            )
        )
    db.flush()
    return spec


def _by_article(payload, article: str) -> dict:
    rows = [row for row in payload["blocking"] if row["item_article"] == article]
    assert rows, f"позиция {article} не попала в блокирующие: {[r['item_article'] for r in payload['blocking']]}"
    return rows[0]


# ---------------------------------------------------------------------------
# Базовые сценарии окраски
# ---------------------------------------------------------------------------


def test_enough_stock_everywhere_gives_clean_result(db_session):
    """Если всех компонентов хватает — блокирующих позиций нет."""
    product = _mk_item(db_session, "P1")
    material = _mk_item(db_session, "M1", stock=100.0)
    _mk_spec(db_session, product, {material: 2.0})

    payload = analyze_release(db_session, product, 10.0)

    assert payload["blocking"] == []
    assert payload["summary"]["status"] == "ok"
    assert payload["summary"]["producible_qty"] == 10.0
    assert payload["summary"]["fully_producible"] is True


def test_node_without_stock_but_with_components_is_yellow(db_session):
    """Узла на складе нет, но всех его компонентов хватает — жёлтый, не блокирующий."""
    product = _mk_item(db_session, "P1")
    node = _mk_item(db_session, "N1", stock=0.0)
    material = _mk_item(db_session, "M1", stock=1000.0)
    _mk_spec(db_session, product, {node: 1.0})
    _mk_spec(db_session, node, {material: 3.0})

    payload = analyze_release(db_session, product, 10.0)

    node_row = _by_article(payload, "ART-N1")
    assert node_row["status"] == "make"
    assert node_row["kind"] == "node"
    assert node_row["is_blocking"] is False
    assert node_row["shortage_qty"] == 10.0
    # Материала хватило — в проблемные он не попал.
    assert [row["item_article"] for row in payload["blocking"]] == ["ART-N1"]
    assert payload["summary"]["status"] == "make"
    assert payload["summary"]["make_count"] == 1
    assert payload["summary"]["shortage_count"] == 0
    assert payload["summary"]["producible_qty"] == 0.0
    assert payload["summary"]["fully_producible"] is False


def test_missing_component_is_red_and_blocks_the_node_above(db_session):
    """Не хватает компонента — он красный, а узел над ним уходит в blocked."""
    product = _mk_item(db_session, "P1")
    node = _mk_item(db_session, "N1", stock=0.0)
    material = _mk_item(db_session, "M1", stock=5.0)
    _mk_spec(db_session, product, {node: 1.0})
    _mk_spec(db_session, node, {material: 3.0})

    payload = analyze_release(db_session, product, 10.0)

    material_row = _by_article(payload, "ART-M1")
    assert material_row["status"] == "shortage"
    assert material_row["kind"] == "material"
    assert material_row["is_blocking"] is True
    assert material_row["required_qty"] == 30.0
    assert material_row["stock_on_hand"] == 5.0
    assert material_row["shortage_qty"] == 25.0

    node_row = _by_article(payload, "ART-N1")
    assert node_row["status"] == "blocked"
    assert node_row["is_blocking"] is True

    assert payload["summary"]["status"] == "blocked"
    assert payload["summary"]["shortage_count"] == 1
    assert payload["summary"]["blocked_count"] == 1
    # Материал позволяет изготовить часть узлов, но готовых узлов на складе нет.
    assert payload["summary"]["producible_qty"] == 0.0
    assert payload["summary"]["fully_producible"] is False


def test_node_stock_stops_the_explosion(db_session):
    """Узел лежит на складе — количества ниже по ветке уже не требуются."""
    product = _mk_item(db_session, "P1")
    node = _mk_item(db_session, "N1", stock=10.0)
    material = _mk_item(db_session, "M1", stock=0.0)
    _mk_spec(db_session, product, {node: 1.0})
    _mk_spec(db_session, node, {material: 3.0})

    payload = analyze_release(db_session, product, 10.0)

    # Выпуску ветка не мешает: узел берётся со склада, компонент не нужен.
    assert payload["blocking"] == []
    assert payload["summary"]["shortage_count"] == 0
    assert payload["summary"]["blocked_count"] == 0
    assert payload["summary"]["producible_qty"] == 10.0


def test_partial_node_stock_explodes_only_the_remainder(db_session):
    """Часть узлов есть на складе — вниз уходит только непокрытый остаток."""
    product = _mk_item(db_session, "P1")
    node = _mk_item(db_session, "N1", stock=4.0)
    material = _mk_item(db_session, "M1", stock=0.0)
    _mk_spec(db_session, product, {node: 1.0})
    _mk_spec(db_session, node, {material: 3.0})

    payload = analyze_release(db_session, product, 10.0)

    node_row = _by_article(payload, "ART-N1")
    assert node_row["required_qty"] == 10.0
    assert node_row["allocated_qty"] == 4.0
    assert node_row["shortage_qty"] == 6.0

    material_row = _by_article(payload, "ART-M1")
    # Изготовить надо 6 узлов, а не 10.
    assert material_row["required_qty"] == 18.0
    assert material_row["shortage_qty"] == 18.0
    assert payload["summary"]["producible_qty"] == 4.0


def test_immediate_release_is_limited_by_ready_direct_nodes(db_session):
    """Материалов может хватать на все 15, но «можно сейчас» ограничено готовыми узлами."""
    product = _mk_item(db_session, "P1")
    plentiful_node = _mk_item(db_session, "N1", stock=13.0)
    scarce_node = _mk_item(db_session, "N2", stock=1.0)
    material = _mk_item(db_session, "M1", stock=1000.0)
    _mk_spec(db_session, product, {plentiful_node: 1.0, scarce_node: 1.0})
    _mk_spec(db_session, plentiful_node, {material: 1.0})
    _mk_spec(db_session, scarce_node, {material: 1.0})

    payload = analyze_release(db_session, product, 15.0)

    assert payload["summary"]["shortage_count"] == 0
    assert payload["summary"]["make_count"] == 2
    assert payload["summary"]["producible_qty"] == 1.0
    assert payload["summary"]["fully_producible"] is False


# ---------------------------------------------------------------------------
# Общий склад на несколько веток
# ---------------------------------------------------------------------------


def test_shared_component_competes_for_one_stock_pool(db_session):
    """Один и тот же материал под двумя узлами тратит один и тот же остаток."""
    product = _mk_item(db_session, "P1")
    left = _mk_item(db_session, "N1")
    right = _mk_item(db_session, "N2")
    shared = _mk_item(db_session, "M1", stock=10.0)
    _mk_spec(db_session, product, {left: 1.0, right: 1.0})
    _mk_spec(db_session, left, {shared: 2.0})
    _mk_spec(db_session, right, {shared: 3.0})

    payload = analyze_release(db_session, product, 4.0)

    shared_row = _by_article(payload, "ART-M1")
    # 4 * 2 + 4 * 3 = 20 при остатке 10.
    assert shared_row["required_qty"] == 20.0
    assert shared_row["allocated_qty"] == 10.0
    assert shared_row["shortage_qty"] == 10.0
    assert shared_row["status"] == "shortage"
    assert sorted(parent["item_article"] for parent in shared_row["used_in"]) == ["ART-N1", "ART-N2"]


def test_component_on_two_levels_is_netted_after_all_demand_is_collected(db_session):
    """Материал стоит и в изделии, и в узле — гасится один раз общей потребностью."""
    product = _mk_item(db_session, "P1")
    node = _mk_item(db_session, "N1")
    material = _mk_item(db_session, "M1", stock=6.0)
    _mk_spec(db_session, product, {node: 1.0, material: 1.0})
    _mk_spec(db_session, node, {material: 1.0})

    payload = analyze_release(db_session, product, 5.0)

    material_row = _by_article(payload, "ART-M1")
    assert material_row["required_qty"] == 10.0
    assert material_row["allocated_qty"] == 6.0
    assert material_row["shortage_qty"] == 4.0


# ---------------------------------------------------------------------------
# Остатки складов
# ---------------------------------------------------------------------------


def test_ignored_warehouse_stock_does_not_cover_demand(db_session):
    """Остаток на игнорируемом складе виден, но в покрытие не идёт."""
    product = _mk_item(db_session, "P1")
    material = _mk_item(db_session, "M1")
    _mk_spec(db_session, product, {material: 1.0})

    db_session.add(StockWarehouse(warehouse_ref1c="wh-good", warehouse_name="Склад №2", is_selected=True))
    db_session.add(StockWarehouse(warehouse_ref1c="wh-bad", warehouse_name="Изолятор брака", is_selected=True))
    db_session.add(IgnoredWarehouse(warehouse_ref1c="wh-bad", warehouse_name="Изолятор брака"))
    _stock_bin(db_session, material, 4.0, warehouse="wh-good")
    _stock_bin(db_session, material, 96.0, warehouse="wh-bad")
    db_session.flush()

    payload = analyze_release(db_session, product, 10.0)

    material_row = _by_article(payload, "ART-M1")
    assert material_row["stock_on_hand"] == 4.0
    assert material_row["shortage_qty"] == 6.0
    warehouses = {row["warehouse_name"]: row for row in material_row["warehouses"]}
    assert warehouses["Склад №2"]["counted"] is True
    assert warehouses["Изолятор брака"]["counted"] is False
    assert warehouses["Изолятор брака"]["qty"] == 96.0


def test_material_custody_is_not_free_for_a_new_release(db_session, monkeypatch):
    """Детали, уже закреплённые за другим заказом, нельзя считать свободными."""
    from app.services.production_material_custody import MaterialCustodyState

    product = _mk_item(db_session, "P1")
    material = _mk_item(db_session, "M1", stock=30.0)
    _mk_spec(db_session, product, {material: 1.0})

    custody = MaterialCustodyState(
        by_warehouse_item={(MAIN_WAREHOUSE, int(material.item_id)): 20.0}
    )
    monkeypatch.setattr(
        "app.services.production_material_custody_projection.load_material_custody_projection",
        lambda _db, *, ledger_generation_id: custody,
    )

    payload = analyze_release(db_session, product, 25.0)

    material_row = _by_article(payload, "ART-M1")
    assert material_row["stock_on_hand"] == 10.0
    assert material_row["shortage_qty"] == 15.0
    assert material_row["status"] == "shortage"
    assert payload["summary"]["producible_qty"] == 10.0
    assert material_row["warehouses"] == [
        {
            "warehouse_name": "Основной склад",
            "qty": 10.0,
            "physical_qty": 30.0,
            "reserved_qty": 20.0,
            "counted": True,
        }
    ]


# ---------------------------------------------------------------------------
# Корень, дерево и защитные контуры
# ---------------------------------------------------------------------------


def test_root_stock_never_covers_the_release_task(db_session):
    """Готовые изделия на складе не отменяют задание на выпуск."""
    product = _mk_item(db_session, "P1", stock=1000.0)
    material = _mk_item(db_session, "M1", stock=2.0)
    _mk_spec(db_session, product, {material: 1.0})

    payload = analyze_release(db_session, product, 10.0)

    assert payload["root"]["stock_on_hand"] == 1000.0
    material_row = _by_article(payload, "ART-M1")
    assert material_row["shortage_qty"] == 8.0


def test_root_without_specification_is_reported(db_session):
    product = _mk_item(db_session, "P1")

    payload = analyze_release(db_session, product, 10.0)

    assert payload["root"]["has_spec"] is False
    assert "ROOT_NO_SPEC" in payload["summary"]["warnings"]
    assert payload["blocking"] == []


def test_tree_is_returned_only_on_demand_and_carries_branch_quantities(db_session):
    product = _mk_item(db_session, "P1")
    node = _mk_item(db_session, "N1", stock=4.0)
    material = _mk_item(db_session, "M1", stock=0.0)
    _mk_spec(db_session, product, {node: 2.0})
    _mk_spec(db_session, node, {material: 3.0})

    lean = analyze_release(db_session, product, 5.0)
    assert lean["tree"] is None

    full = analyze_release(db_session, product, 5.0, include_tree=True)
    tree = full["tree"]
    assert tree["item_article"] == "ART-P1"
    assert tree["branch_required_qty"] == 5.0

    node_node = tree["children"][0]
    assert node_node["item_article"] == "ART-N1"
    assert node_node["qty_per_parent"] == 2.0
    assert node_node["branch_required_qty"] == 10.0
    # 4 узла на складе, изготовить надо 6.
    assert node_node["branch_shortage_qty"] == 6.0

    material_node = node_node["children"][0]
    assert material_node["item_article"] == "ART-M1"
    assert material_node["branch_required_qty"] == 18.0
    assert material_node["status"] == "shortage"


def test_cycle_in_bom_is_cut_and_reported(db_session):
    product = _mk_item(db_session, "P1")
    node = _mk_item(db_session, "N1")
    _mk_spec(db_session, product, {node: 1.0})
    _mk_spec(db_session, node, {product: 1.0})

    payload = analyze_release(db_session, product, 3.0)

    assert "CYCLE_DETECTED" in payload["summary"]["warnings"]
    assert payload["summary"]["cycles"]


# ---------------------------------------------------------------------------
# Поиск изделия
# ---------------------------------------------------------------------------


def test_resolve_item_prefers_exact_article(db_session):
    exact = _mk_item(db_session, "P1", article="12-345")
    _mk_item(db_session, "P2", article="12-345-01")

    item, candidates = resolve_item(db_session, article="12-345")

    assert item is not None
    assert int(item.item_id) == int(exact.item_id)
    assert len(candidates) == 1


def test_resolve_item_returns_candidates_when_ambiguous(db_session):
    _mk_item(db_session, "P1", article="12-345-01")
    _mk_item(db_session, "P2", article="12-345-02")

    item, candidates = resolve_item(db_session, article="12-345")

    assert item is None
    assert len(candidates) == 2


def test_find_items_marks_specification_presence(db_session):
    with_spec = _mk_item(db_session, "P1", article="AA-1")
    material = _mk_item(db_session, "M1", article="AA-2")
    _mk_spec(db_session, with_spec, {material: 1.0})

    rows = {row["item_article"]: row for row in find_items(db_session, "AA-")}

    assert rows["AA-1"]["has_spec"] is True
    assert rows["AA-2"]["has_spec"] is False


# ---------------------------------------------------------------------------
# Полный состав: дефицит только в текущей чистой потребности
# ---------------------------------------------------------------------------


def _find_node(node: dict, article: str) -> dict:
    if node["item_article"] == article:
        return node
    for child in node["children"]:
        found = _find_node(child, article)
        if found is not None:
            return found
    return None


def test_covered_branch_does_not_create_component_need(db_session):
    """Родителя хватает — ни потребности, ни дефицита компонента нет."""
    product = _mk_item(db_session, "P1")
    node = _mk_item(db_session, "N1", stock=100.0)          # закрыт складом
    coating = _mk_item(db_session, "C1")                     # 0, спеки нет
    _mk_spec(db_session, product, {node: 1.0})
    _mk_spec(db_session, node, {coating: 1.0})

    payload = analyze_release(db_session, product, 10.0, include_tree=True)

    row = _find_node(payload["tree"], "ART-C1")
    assert row["branch_required_qty"] == 0.0
    assert row["branch_shortage_qty"] == 0.0
    assert row["status"] == "not_required"
    assert row["reason"] == "Не требуется: родитель закрыт остатком"
    assert row["stock_short"] is False
    assert payload["blocking"] == []
    assert payload["summary"]["producible_qty"] == 10.0


def test_covered_branch_does_not_mark_make_need(db_session):
    """Ненужную ветку нельзя красить как потребность в изготовлении."""
    product = _mk_item(db_session, "P1")
    node = _mk_item(db_session, "N1", stock=100.0)
    painted = _mk_item(db_session, "PT1")                    # 0 на складе
    blank = _mk_item(db_session, "BL1", stock=50.0)          # есть из чего делать
    _mk_spec(db_session, product, {node: 1.0})
    _mk_spec(db_session, node, {painted: 1.0})
    _mk_spec(db_session, painted, {blank: 1.0})

    payload = analyze_release(db_session, product, 10.0, include_tree=True)

    row = _find_node(payload["tree"], "ART-PT1")
    assert row["branch_required_qty"] == 0.0
    assert row["status"] == "not_required"
    assert row["reason"] == "Не требуется: родитель закрыт остатком"
    assert _find_node(payload["tree"], "ART-BL1")["status"] == "not_required"
    assert payload["blocking"] == []


def test_partial_stock_keeps_the_row_yellow_and_marks_the_stock_cell(db_session):
    """Остатка не хватает, но собрать можно: строка жёлтая, остаток — красный.

    Это две разные вещи: «позицию можно закрыть» и «имеющегося количества на
    потребность не хватает». Первое красит строку, второе — ячейку остатка.
    """
    product = _mk_item(db_session, "P1")
    node = _mk_item(db_session, "N1", stock=3.0)             # нужно 15, есть 3
    component = _mk_item(db_session, "M1", stock=1000.0)
    _mk_spec(db_session, product, {node: 1.0})
    _mk_spec(db_session, node, {component: 1.0})

    payload = analyze_release(db_session, product, 15.0, include_tree=True)

    row = _find_node(payload["tree"], "ART-N1")
    assert row["branch_required_qty"] == 15.0
    assert row["stock_on_hand"] == 3.0
    assert row["status"] == "make"
    assert row["stock_short"] is True
    # А там, где остатка хватает, ячейка не помечается.
    assert _find_node(payload["tree"], "ART-M1")["stock_short"] is False


def test_every_row_explains_its_colour_and_carries_the_lead_time(db_session):
    """Цвет без причины оператору ничего не даёт: рядом должно стоять «чем закрыть».

    Формулировки согласованы с барабаном ProdFlow, чтобы одна и та же ситуация
    в двух системах называлась одинаково.
    """
    product = _mk_item(db_session, "P1")
    covered = _mk_item(db_session, "M1", stock=1000.0)
    bought = _mk_item(db_session, "M2")               # закупное, нечем закрыть
    node = _mk_item(db_session, "N1")                 # изготавливаемое
    blank = _mk_item(db_session, "B1", stock=50.0)
    for item, method, rt in (
        (covered, "Закупка", 20),
        (bought, "Закупка", 30),
        (node, "Производство", 1),
        (blank, "Закупка", 45),
    ):
        item.replenishment_method = method
        item.replenishment_time = rt
    db_session.flush()
    _mk_spec(db_session, product, {covered: 1.0, bought: 1.0, node: 1.0})
    _mk_spec(db_session, node, {blank: 1.0})

    payload = analyze_release(db_session, product, 10.0, include_tree=True)

    by_article = {row["item_article"]: row for row in payload["blocking"]}
    assert by_article["ART-M2"]["reason"] == "Не хватает исходной детали"
    assert by_article["ART-M2"]["replenishment_time"] == 30
    assert by_article["ART-N1"]["reason"] == "Надо изготовить"
    assert by_article["ART-N1"]["replenishment_time"] == 1

    tree_rows = {}

    def walk(node_payload):
        tree_rows[node_payload["item_article"]] = node_payload
        for child in node_payload["children"]:
            walk(child)

    walk(payload["tree"])
    assert tree_rows["ART-M1"]["reason"] == "Есть на складе"
    assert tree_rows["ART-M1"]["replenishment_time"] == 20
    assert tree_rows["ART-B1"]["reason"] == "Есть на складе"


def test_rework_operation_is_not_a_blocker(db_session):
    """Операция на стороне складом не закрывается — и выпуск не блокирует.

    Травление, гибка на стороне, пошив: их не бывает на остатке по самой их
    природе. Считая их дефицитом, проверка красила в блокеры каждую деталь с
    покрытием и обнуляла возможный выпуск, хотя мешает не операция, а металл.
    """
    product = _mk_item(db_session, "P1")
    plated = _mk_item(db_session, "PL1")                    # деталь после покрытия
    blank = _mk_item(db_session, "BL1", stock=500.0)        # заготовка есть
    coating = _mk_item(db_session, "CT1")                   # операция, остатка нет
    coating.replenishment_method = "Переработка"
    db_session.flush()
    _mk_spec(db_session, product, {plated: 1.0})
    _mk_spec(db_session, plated, {blank: 1.0, coating: 0.2})

    payload = analyze_release(db_session, product, 10.0, include_tree=True)

    coating_row = _find_node(payload["tree"], "ART-CT1")
    assert coating_row["status"] == "non_stock"
    assert coating_row["reason"] == "Переработка: закрывается операцией"
    # Деталь под покрытием изготавливается, а не блокируется.
    assert _find_node(payload["tree"], "ART-PL1")["status"] == "make"
    assert [row["item_article"] for row in payload["blocking"]] == ["ART-PL1"]
    assert payload["summary"]["producible_qty"] == 0.0
    assert payload["summary"]["shortage_count"] == 0


def test_rework_without_the_part_still_shows_the_part_as_the_blocker(db_session):
    """Если нет заготовки — красной становится она, а не операция над ней."""
    product = _mk_item(db_session, "P1")
    plated = _mk_item(db_session, "PL1")
    blank = _mk_item(db_session, "BL1")                     # заготовки нет
    coating = _mk_item(db_session, "CT1")
    coating.replenishment_method = "Переработка"
    db_session.flush()
    _mk_spec(db_session, product, {plated: 1.0})
    _mk_spec(db_session, plated, {blank: 1.0, coating: 0.2})

    payload = analyze_release(db_session, product, 10.0)

    by_article = {row["item_article"]: row for row in payload["blocking"]}
    assert by_article["ART-BL1"]["status"] == "shortage"
    assert by_article["ART-PL1"]["status"] == "blocked"
    assert "ART-CT1" not in by_article
    assert payload["summary"]["producible_qty"] == 0.0


def test_service_from_1c_is_not_a_supply_position(db_session):
    """«Услуга» из 1С складом не закрывается — и блокером не является.

    Травление, гибка на стороне и пошив заведены в 1С как ТипНоменклатуры
    «Услуга» и остатка не имеют никогда. Пока проверка читала их как закупку,
    каждая деталь, проходящая через операцию, попадала в блокеры — а мешает
    выпуску не операция, а металл под ней. Признак берём из 1С, а не из
    названия позиции.
    """
    product = _mk_item(db_session, "P1")
    plated = _mk_item(db_session, "PL1")
    blank = _mk_item(db_session, "BL1", stock=500.0)
    service = _mk_item(db_session, "SV1")
    service.replenishment_method = "Закупка"      # в 1С у услуг стоит именно так
    service.item_type = "Услуга"
    db_session.flush()
    _mk_spec(db_session, product, {plated: 1.0})
    _mk_spec(db_session, plated, {blank: 1.0, service: 0.2})

    payload = analyze_release(db_session, product, 10.0, include_tree=True)

    row = _find_node(payload["tree"], "ART-SV1")
    assert row["status"] == "non_stock"
    assert row["reason"] == "Услуга: на складе не бывает"
    assert _find_node(payload["tree"], "ART-PL1")["status"] == "make"
    assert [r["item_article"] for r in payload["blocking"]] == ["ART-PL1"]
    assert payload["summary"]["producible_qty"] == 0.0


def test_stocked_material_stays_a_blocker_even_if_it_looks_like_an_operation(db_session):
    """Порошковая краска — запас, а не услуга: она обязана лежать на складе.

    Отсекаем строго по признаку 1С, а не по тому, как позиция называется.
    """
    product = _mk_item(db_session, "P1")
    powder = _mk_item(db_session, "PW1")
    powder.item_type = "Запас"
    powder.replenishment_method = "Закупка"
    db_session.flush()
    _mk_spec(db_session, product, {powder: 1.0})

    payload = analyze_release(db_session, product, 10.0)

    [row] = payload["blocking"]
    assert row["item_article"] == "ART-PW1"
    assert row["status"] == "shortage"
    assert payload["summary"]["producible_qty"] == 0.0


def test_technological_operation_is_not_a_supply_position(db_session):
    """«Операция» из 1С — тоже не запас: их на стенде 1542 против 484 услуг."""
    product = _mk_item(db_session, "P1")
    part = _mk_item(db_session, "PT1")
    blank = _mk_item(db_session, "BL1", stock=100.0)
    operation = _mk_item(db_session, "OP1")
    operation.item_type = "Операция"
    db_session.flush()
    _mk_spec(db_session, product, {part: 1.0})
    _mk_spec(db_session, part, {blank: 1.0, operation: 1.0})

    payload = analyze_release(db_session, product, 5.0, include_tree=True)

    row = _find_node(payload["tree"], "ART-OP1")
    assert row["status"] == "non_stock"
    assert row["reason"] == "Операция: на складе не бывает"
    assert payload["summary"]["producible_qty"] == 0.0

"""Tests for the DBR kit-boundary classifier (services/dbr/classify.py).

Pure classify_meta cases on fixture ItemMeta, plus one DB-backed integration
that assembles ItemMeta from the shared tables.
"""

import pytest

from app.models import (
    DefaultSpecification,
    Item,
    ItemCategory,
    ItemWarehouseStock,
    ProductionKind,
    ProductionResource,
    ResourceProductionKind,
    Specification,
    WorkshopWarehouseBinding,
)
from app.services.dbr import classify as classify_mod
from app.services.dbr.classify import ItemMeta, classify_meta
from app.services.dbr.core.drum import kit as kit_mod

W2, W3, W4 = "wh2", "wh3", "wh4"


# --------------------------------------------------------------------------
# Pure decision
# --------------------------------------------------------------------------


def test_fastener_excluded():
    d, wh, note = classify_meta(ItemMeta("BOLT", is_fastener=True), W2, W3, W4)
    assert d == kit_mod.FASTENER and wh is None and note is None


def test_phantom_recurses():
    d, wh, note = classify_meta(ItemMeta("PHANTOM", is_phantom=True), W2, W3, W4)
    assert d == kit_mod.RECURSE and wh is None


def test_w2_blank_priority_over_purchase_and_w3():
    meta = ItemMeta("FRAME", is_w2_blank=True, is_purchase=True, has_w3_shelf=True, has_spec=True)
    d, wh, note = classify_meta(meta, W2, W3, W4)
    assert d == kit_mod.W2 and wh == W2


def test_purchase_is_w4():
    d, wh, note = classify_meta(ItemMeta("BUY", is_purchase=True), W2, W3, W4)
    assert d == kit_mod.W4 and wh == W4


def test_manufactured_with_w3_shelf_is_w3():
    d, wh, note = classify_meta(ItemMeta("PAINTED", has_spec=True, has_w3_shelf=True), W2, W3, W4)
    assert d == kit_mod.W3 and wh == W3


def test_manufactured_without_shelf_is_w4():
    d, wh, note = classify_meta(ItemMeta("SUBASM", has_spec=True, has_w3_shelf=False), W2, W3, W4)
    assert d == kit_mod.W4 and wh == W4


def test_leaf_detail_under_schedule():
    d, wh, note = classify_meta(ItemMeta("DETAIL"), W2, W3, W4)
    assert d == kit_mod.UNDER_SCHEDULE and wh == W4 and note is None


def test_missing_w4_rejects_purchase_classification():
    with pytest.raises(ValueError, match=r"склад №4 \(W4\)"):
        classify_meta(ItemMeta("BUY", is_purchase=True), W2, W3, None)


def test_missing_w2_rejects_blank_classification():
    with pytest.raises(ValueError, match=r"склад №2 \(W2\)"):
        classify_meta(
            ItemMeta("FRAME", is_w2_blank=True, has_spec=True), None, W3, W4
        )


def test_missing_w3_rejects_configured_shelf_classification():
    with pytest.raises(ValueError, match=r"склад №3 \(W3\)"):
        classify_meta(
            ItemMeta("PAINTED", has_spec=True, has_w3_shelf=True), W2, None, W4
        )


def test_fastener_remains_intentionally_excluded_even_without_roles():
    decision, warehouse, note = classify_meta(
        ItemMeta("BOLT", is_fastener=True), None, None, None
    )
    assert (decision, warehouse, note) == (kit_mod.FASTENER, None, None)


# --------------------------------------------------------------------------
# DB-backed builder
# --------------------------------------------------------------------------


def test_build_classifier_from_db(db_session):
    db = db_session

    cat_fast = ItemCategory(category_name="Метизы", category_ref1c="C-FAST")
    cat_norm = ItemCategory(category_name="Детали", category_ref1c="C-NORM")
    db.add_all([cat_fast, cat_norm])
    db.flush()

    bolt = Item(item_code="BOLT", item_name="Болт", category_id=cat_fast.category_id)
    buy = Item(item_code="BUY", item_name="Покупное", replenishment_method="Закупка")
    frame = Item(item_code="FRAME", item_name="Рама-заготовка", category_id=cat_norm.category_id)
    painted = Item(item_code="PAINT", item_name="Крашеная деталь")
    db.add_all([bolt, buy, frame, painted])
    db.flush()

    # FRAME is manufactured by a workshop that delivers to W2.
    kind = ProductionKind(ref_1c="K1", name="Сварка")
    db.add(kind)
    db.flush()
    res = ProductionResource(resource_name="Сварочный участок", capacity=1)
    db.add(res)
    db.flush()
    db.add(ResourceProductionKind(resource_id=res.resource_id, production_kind_id=kind.id))
    db.add(WorkshopWarehouseBinding(workshop_id=res.resource_id, warehouse_ref1c="wip", production_warehouse_ref1c=W2))
    spec_frame = Specification(spec_name="Спека рамы", spec_ref1c="S-FRAME", production_kind_id=kind.id)
    spec_paint = Specification(spec_name="Спека крашеной", spec_ref1c="S-PAINT")
    db.add_all([spec_frame, spec_paint])
    db.flush()
    db.add(DefaultSpecification(item_id=frame.item_id, spec_id=spec_frame.spec_id))
    db.add(DefaultSpecification(item_id=painted.item_id, spec_id=spec_paint.spec_id))
    # PAINT has a shelf on W3
    db.add(ItemWarehouseStock(item_id=painted.item_id, warehouse_ref1c=W3, qty=0))
    db.commit()

    class _Settings:
        w2_warehouse_ref1c = W2
        w3_warehouse_ref1c = W3
        w4_warehouse_ref1c = W4
        fastener_categories = ["Метизы"]

    classify, notes = classify_mod.build_classifier(db, _Settings())
    assert classify("BOLT") == (kit_mod.FASTENER, None)
    assert classify("BUY") == (kit_mod.W4, W4)
    assert classify("FRAME") == (kit_mod.W2, W2)
    assert classify("PAINT") == (kit_mod.W3, W3)
    assert notes == []


def test_build_classifier_rejects_missing_required_warehouse_roles(db_session):
    class _Settings:
        w2_warehouse_ref1c = None
        w3_warehouse_ref1c = ""
        w4_warehouse_ref1c = None
        fastener_categories = ["Метизы"]

    try:
        classify_mod.build_classifier(db_session, _Settings())
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("missing warehouse roles must fail closed")

    assert "склад №2 (W2)" in message
    assert "склад №3 (W3)" in message
    assert "склад №4 (W4)" in message

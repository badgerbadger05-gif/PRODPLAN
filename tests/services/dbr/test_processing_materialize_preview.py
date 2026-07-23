from decimal import Decimal

import pytest

from app.models import (
    DbrFeederSignal,
    DbrSupermarketPosition,
    DefaultSpecification,
    Item,
    SpecComponent,
    Specification,
    SyncLink,
)
from app.services.dbr import processing_materialize_preview as preview


def _scenario(db):
    coated = Item(
        item_code="COATED-PREVIEW",
        item_name="Покрытая деталь",
        item_ref1c="coated-ref",
        supplier_ref1c="contractor-ref",
        replenishment_method="Переработка",
    )
    bare = Item(
        item_code="BARE-PREVIEW",
        item_name="Голая деталь",
        item_ref1c="bare-ref",
    )
    db.add_all([coated, bare])
    db.flush()
    spec = Specification(spec_name="Processing", spec_ref1c="processing-spec-ref")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=coated.item_id, spec_id=spec.spec_id))
    component = SpecComponent(
        spec_id=spec.spec_id,
        item_id=bare.item_id,
        quantity=Decimal("1.5"),
        component_type="Сборка",
    )
    db.add(component)
    position = DbrSupermarketPosition(
        item_id=coated.item_id,
        warehouse_ref1c="w4",
        supply_type="processing",
        mode="shelf",
        adu=1,
        commonality=1,
        rt_days=1,
        batch_days=1,
        q_batch=1,
        k_var=0,
        supply_risk_pct=0,
        red_qty=1,
        yellow_qty=1,
        green_qty=1,
        target_qty=3,
        rt_source="chain",
        data_quality=[],
        calculation_snapshot={},
    )
    db.add(position)
    db.flush()
    signal = DbrFeederSignal(
        dedup_key=f"processing-preview-{coated.item_id}",
        supermarket_position_id=position.id,
        item_id=coated.item_id,
        warehouse_ref1c="w4",
        status="Open",
        suggested_qty=Decimal("4"),
        priority=1,
        data_quality=[],
        reason_json={},
    )
    db.add(signal)
    db.flush()
    return signal, component, spec, bare


def test_preview_payload_shape_and_is_hard_gated(db_session):
    signal, _, _, _ = _scenario(db_session)

    result = preview.preview_processing_signal(db_session, signal.id)

    assert result["dry_run"] is True
    assert result["write_capable"] is False
    assert result["live_contract_confirmed"] is False
    assert result["gate"] == "blocked_until_1c_contract_confirmation"
    assert result["entity"] == "Document_ЗаказПоставщику"
    payload = result["payload"]
    assert payload["ВидОперации"] == "ЗаказНаПереработку"
    assert (
        payload["ХозяйственнаяОперация_Key"]
        == "8d96f6a2-9934-11eb-e39a-fa163e61326a"
    )
    assert payload["Контрагент_Key"] == "contractor-ref"
    assert payload["Запасы"][0]["Номенклатура_Key"] == "coated-ref"
    assert payload["Запасы"][0]["Количество"] == 4.0
    assert payload["Материалы"][0]["Номенклатура_Key"] == "bare-ref"
    assert payload["Материалы"][0]["Количество"] == 6.0


def test_preview_requires_exactly_one_assembly_component(db_session):
    signal, _, spec, bare = _scenario(db_session)
    db_session.add(
        SpecComponent(
            spec_id=spec.spec_id,
            item_id=bare.item_id,
            quantity=1,
            component_type="Сборка",
        )
    )
    db_session.flush()

    with pytest.raises(preview.ProcessingPreviewConflict, match="ровно один"):
        preview.preview_processing_signal(db_session, signal.id)


def test_preview_reports_missing_required_data(db_session):
    signal, _, _, _ = _scenario(db_session)
    signal.item.supplier_ref1c = None
    db_session.flush()

    with pytest.raises(preview.ProcessingPreviewConflict, match="подрядчик"):
        preview.preview_processing_signal(db_session, signal.id)


def test_preview_creates_no_links_or_1c_state(db_session):
    signal, _, _, _ = _scenario(db_session)
    before = db_session.query(SyncLink).count()

    preview.preview_processing_signal(db_session, signal.id)
    preview.preview_processing_signal(db_session, signal.id)

    assert db_session.query(SyncLink).count() == before
    assert signal.one_c_order_ref is None
    assert signal.status == "Open"

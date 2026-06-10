from __future__ import annotations

from types import SimpleNamespace

from app.models import Item, Unit
from app.schemas import ODataSyncRequest
from app.services import units_sync
from app.services.order_quantity_calculator import OrderQuantityCalculator


class _FakeUnitsClient:
    def __init__(self, *_args, **_kwargs):
        pass

    def get_count(self, *_args, **_kwargs):
        return 2

    def iter_pages(self, entity_name, **_kwargs):
        assert entity_name == units_sync.UNIT_CLASSIFIER_ENTITY
        yield [
            {
                "Ref_Key": "unit-pcs",
                "Code": "796",
                "Description": "шт",
                "НаименованиеПолное": "Штука",
                "МеждународноеСокращение": "PCE",
            },
            {
                "Ref_Key": "unit-meter",
                "Code": "006",
                "Description": "м",
                "НаименованиеПолное": "Метр",
                "МеждународноеСокращение": "MTR",
            },
        ]

    def _make_request(self, entity_name, params=None, **_kwargs):
        assert entity_name == units_sync.UNIT_CLASSIFIER_ENTITY
        assert params and "$select" not in params
        return {
            "value": [
                {
                    "Ref_Key": "unit-meter",
                    "Code": "006",
                    "Description": "м",
                    "НаименованиеПолное": "Метр",
                    "МеждународноеСокращение": "MTR",
                }
            ]
        }


def _req(entity_name: str = units_sync.UNIT_CLASSIFIER_ENTITY) -> ODataSyncRequest:
    return ODataSyncRequest(base_url="http://demo/odata", entity_name=entity_name)


def test_sync_units_from_classifier_uses_description_as_short_name(db_session, monkeypatch):
    monkeypatch.setattr(units_sync, "OData1CClient", _FakeUnitsClient)

    stats = units_sync.sync_units_from_odata(db_session, _req())

    assert stats["units_created"] == 2
    meter = db_session.query(Unit).filter_by(unit_ref1c="unit-meter").one()
    assert meter.unit_name == "м"
    assert meter.short_name == "м"
    assert meter.unit_full_name == "Метр"
    assert meter.iso_code == "MTR"


def test_backfill_units_from_items_uses_classifier_without_broad_select(db_session, monkeypatch):
    monkeypatch.setattr(units_sync, "OData1CClient", _FakeUnitsClient)
    db_session.add(
        Item(
            item_code="METER-ITEM",
            item_name="Meter item",
            unit="unit-meter",
            stock_qty=0,
            status="active",
        )
    )
    db_session.commit()

    stats = units_sync.backfill_units_from_items(
        db_session,
        _req(),
        catalogs=[units_sync.UNIT_CLASSIFIER_ENTITY],
    )

    assert stats["created"] == 1
    assert stats["missing_after"] == 0
    meter = db_session.query(Unit).filter_by(unit_ref1c="unit-meter").one()
    assert meter.short_name == "м"


def test_order_quantity_preserves_fractional_meter_qty_when_short_name_missing():
    item = SimpleNamespace(item_id=1, unit="unit-meter")
    unit = SimpleNamespace(unit_ref1c="unit-meter", unit_name="м", short_name=None, unit_code="006", precision=None)
    calc = OrderQuantityCalculator(
        snapshot={},
        default_spec_map={},
        spec_by_id={},
        components_loader=lambda _spec_id: [],
        item_by_id={1: item},
        units_by_ref={"unit-meter": unit},
    )

    assert calc.is_discrete_item(1) is False
    assert calc.normalize_qty_for_item(1, 2.75) == 2.75

import pytest

from app.models import Item, ProcessingContractorStock, ProcessingStockSyncState
from app.schemas import ODataSyncRequest
from app.services.processing_stock_sync import (
    processing_stock_totals,
    processing_stock_status,
    sync_processing_stock_from_odata,
)


def _request(*, dry_run=False):
    return ODataSyncRequest(
        base_url="http://1c.test/odata",
        entity_name="ignored",
        username="user",
        password="secret",
        dry_run=dry_run,
    )


def test_sync_aggregates_exact_axes_and_reports_unmatched(db_session, monkeypatch):
    db_session.add(Item(item_code="A", item_name="A", item_ref1c="item-a"))
    db_session.commit()
    seen = {}

    def fake_get_all(_self, **kwargs):
        seen.update(kwargs)
        return [
            {
                "Номенклатура_Key": "item-a",
                "Контрагент_Key": "supplier-1",
                "Заказ": {"Ref_Key": "order-1"},
                "Заказ_Type": "StandardODATA.Document_ЗаказПоставщику",
                "ТипПриемаПередачи": "Переработка",
                "КоличествоBalance": 2,
            },
            {
                "Номенклатура_Key": "item-a",
                "Контрагент_Key": "supplier-1",
                "Заказ": {"Ref_Key": "order-1"},
                "Заказ_Type": "StandardODATA.Document_ЗаказПоставщику",
                "ТипПриемаПередачи": "Переработка",
                "КоличествоBalance": 3,
            },
            {"Номенклатура_Key": "missing", "КоличествоBalance": 7},
        ]

    monkeypatch.setattr(
        "app.services.processing_stock_sync.OData1CClient.get_all",
        fake_get_all,
    )
    result = sync_processing_stock_from_odata(db_session, _request())

    assert "AccumulationRegister_ЗапасыПереданные/Balance(" in seen["entity_name"]
    assert "Dimensions='Номенклатура,Контрагент,Заказ,ТипПриемаПередачи'" in seen["entity_name"]
    assert seen["order_by"] is None
    row = db_session.query(ProcessingContractorStock).one()
    assert float(row.qty) == 5
    assert row.contractor_ref1c == "supplier-1"
    assert row.order_ref1c == "order-1"
    assert result["status"] == "ok"
    assert result["rows_seen"] == 3
    assert result["rows_stored"] == 1
    assert result["unmatched_items"] == 1
    assert result["total_qty"] == 5
    assert processing_stock_totals(db_session, {row.item_id}) == {row.item_id: 5}
    assert processing_stock_totals(db_session, set()) == {}


def test_query_failure_preserves_previous_snapshot(db_session, monkeypatch):
    item = Item(item_code="A", item_name="A", item_ref1c="item-a")
    db_session.add(item)
    db_session.flush()
    db_session.add(
        ProcessingContractorStock(
            item_id=item.item_id,
            contractor_ref1c="supplier-old",
            order_ref1c="",
            order_type="",
            transfer_type="",
            qty=11,
        )
    )
    db_session.add(
        ProcessingStockSyncState(
            id=1,
            status="ok",
            rows_seen=1,
            rows_stored=1,
            unmatched_items=0,
        )
    )
    db_session.commit()

    def fail(_self, **_kwargs):
        raise RuntimeError("1C unavailable")

    monkeypatch.setattr(
        "app.services.processing_stock_sync.OData1CClient.get_all",
        fail,
    )
    with pytest.raises(RuntimeError, match="1C unavailable"):
        sync_processing_stock_from_odata(db_session, _request())

    row = db_session.query(ProcessingContractorStock).one()
    assert float(row.qty) == 11
    status = processing_stock_status(db_session)
    assert status["status"] == "error"
    assert status["last_error"] == "1C unavailable"
    assert status["rows_stored"] == 1


def test_dry_run_does_not_replace_snapshot_or_health(db_session, monkeypatch):
    item = Item(item_code="A", item_name="A", item_ref1c="item-a")
    db_session.add(item)
    db_session.flush()
    db_session.add(
        ProcessingContractorStock(
            item_id=item.item_id,
            contractor_ref1c="old",
            order_ref1c="",
            order_type="",
            transfer_type="",
            qty=4,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        "app.services.processing_stock_sync.OData1CClient.get_all",
        lambda _self, **_kwargs: [
            {"Номенклатура_Key": "item-a", "КоличествоBalance": 9}
        ],
    )

    result = sync_processing_stock_from_odata(db_session, _request(dry_run=True))

    assert result == {
        "dry_run": True,
        "rows_seen": 1,
        "rows_stored": 1,
        "unmatched_items": 0,
        "total_qty": 9,
    }
    assert float(db_session.query(ProcessingContractorStock).one().qty) == 4
    assert processing_stock_status(db_session)["status"] == "never"

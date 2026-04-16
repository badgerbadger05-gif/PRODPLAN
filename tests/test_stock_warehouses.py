from app.models import Item, StockWarehouse
from app.schemas import ODataSyncRequest
from app.services import odata_stock_sync as stock_sync


def _mk_req() -> ODataSyncRequest:
    return ODataSyncRequest(
        base_url="http://example.local/odata",
        entity_name="AccumulationRegister_ЗапасыНаСкладах/Balance",
        username=None,
        password=None,
        token=None,
        filter_query=None,
        select_fields=None,
        dry_run=False,
        zero_missing=True,
    )


def test_sync_stock_uses_only_selected_warehouses(db_session, monkeypatch):
    db = db_session

    item = Item(
        item_code="ITEM-001",
        item_name="Item 1",
        item_article="ITEM-001",
        stock_qty=0.0,
        status="active",
    )
    db.add(item)
    db.flush()

    db.add_all(
        [
            StockWarehouse(
                warehouse_ref1c="W1",
                warehouse_code="01",
                warehouse_name="Main",
                is_selected=True,
            ),
            StockWarehouse(
                warehouse_ref1c="W2",
                warehouse_code="02",
                warehouse_name="Reserve",
                is_selected=False,
            ),
        ]
    )
    db.commit()

    def _fake_stock(**kwargs):
        return [
            {"code": "ITEM-001", "qty": 5.0, "ref": "", "warehouse_ref": "W1", "warehouse_code": "01", "warehouse_name": "Main"},
            {"code": "ITEM-001", "qty": 7.0, "ref": "", "warehouse_ref": "W2", "warehouse_code": "02", "warehouse_name": "Reserve"},
        ]

    monkeypatch.setattr(stock_sync, "get_stock_from_1c_odata", _fake_stock)

    stats = stock_sync.sync_stock_from_odata(db, _mk_req())

    db.refresh(item)
    assert float(item.stock_qty) == 5.0
    assert int(stats.get("warehouses_selected", 0)) == 1


def test_sync_stock_warehouses_upserts_and_selects_by_default(db_session, monkeypatch):
    db = db_session

    def _fake_stock(**kwargs):
        return [
            {"code": "ITEM-001", "qty": 1.0, "ref": "", "warehouse_ref": "W1", "warehouse_code": "01", "warehouse_name": "Main"},
            {"code": "ITEM-002", "qty": 2.0, "ref": "", "warehouse_ref": "W2", "warehouse_code": "02", "warehouse_name": "Reserve"},
        ]

    monkeypatch.setattr(stock_sync, "get_stock_from_1c_odata", _fake_stock)

    out = stock_sync.sync_stock_warehouses_from_odata(db, _mk_req())
    rows = db.query(StockWarehouse).order_by(StockWarehouse.warehouse_ref1c.asc()).all()

    assert int(out.get("warehouses_total", 0)) == 2
    assert len(rows) == 2
    assert all(bool(r.is_selected) for r in rows)

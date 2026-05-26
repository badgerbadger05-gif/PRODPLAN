from app.models import Item, ItemWarehouseStock, StockWarehouse
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

    monkeypatch.setattr(stock_sync, "_fetch_warehouse_catalog_rows", lambda req: ([], ""))
    monkeypatch.setattr(stock_sync, "get_stock_from_1c_odata", _fake_stock)

    out = stock_sync.sync_stock_warehouses_from_odata(db, _mk_req())
    rows = db.query(StockWarehouse).order_by(StockWarehouse.warehouse_ref1c.asc()).all()

    assert int(out.get("warehouses_total", 0)) == 2
    assert len(rows) == 2
    assert all(bool(r.is_selected) for r in rows)


def test_sync_stock_warehouses_prefers_catalog_lookup(db_session, monkeypatch):
    db = db_session

    monkeypatch.setattr(
        stock_sync,
        "_fetch_warehouse_catalog_rows",
        lambda req: (
            [
                {
                    "Ref_Key": "WH-CATALOG-1",
                    "Code": "НФ-000001",
                    "Description": "Основной склад",
                    "DeletionMark": False,
                },
                {
                    "Ref_Key": "WH-DELETED",
                    "Code": "НФ-000002",
                    "Description": "Удаленный склад",
                    "DeletionMark": True,
                },
            ],
            "Catalog_СтруктурныеЕдиницы",
        ),
    )
    monkeypatch.setattr(
        stock_sync,
        "get_stock_from_1c_odata",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("stock register must not be used")),
    )

    out = stock_sync.sync_stock_warehouses_from_odata(db, _mk_req())
    rows = db.query(StockWarehouse).order_by(StockWarehouse.warehouse_ref1c.asc()).all()

    assert out["odata_entity"] == "Catalog_СтруктурныеЕдиницы"
    assert int(out["warehouses_seen_in_odata"]) == 2
    assert [(r.warehouse_ref1c, r.warehouse_code, r.warehouse_name) for r in rows] == [
        ("WH-CATALOG-1", "НФ-000001", "Основной склад")
    ]


def test_sync_stock_populates_per_warehouse_breakdown(db_session, monkeypatch):
    """
    sync_stock_from_odata must populate item_warehouse_stock with one row per
    (item, warehouse) pair from the 1C response. Aggregated Item.stock_qty
    still gets the sum (existing behavior). Only selected warehouses leak
    through to the breakdown — the filter applied to aggregated stock is
    also applied to per-warehouse rows.
    """
    db = db_session

    item_a = Item(
        item_code="WH-A",
        item_name="Item A",
        item_article="WH-A",
        stock_qty=0.0,
        status="active",
    )
    item_b = Item(
        item_code="WH-B",
        item_name="Item B",
        item_article="WH-B",
        stock_qty=0.0,
        status="active",
    )
    db.add_all([item_a, item_b])
    db.flush()

    db.add_all(
        [
            StockWarehouse(
                warehouse_ref1c="WH-MAIN",
                warehouse_code="01",
                warehouse_name="Main",
                is_selected=True,
            ),
            StockWarehouse(
                warehouse_ref1c="WH-BRAK",
                warehouse_code="02",
                warehouse_name="Brak isolator",
                is_selected=True,
            ),
            StockWarehouse(
                warehouse_ref1c="WH-OFF",
                warehouse_code="03",
                warehouse_name="Hidden",
                is_selected=False,
            ),
        ]
    )
    db.commit()

    def _fake_stock(**kwargs):
        return [
            # item A: 7 on main, 3 on brak, 100 hidden -> aggregated keeps
            # 7+3=10 (only selected warehouses) and breakdown has two rows.
            {"code": "WH-A", "qty": 7.0, "ref": "", "warehouse_ref": "WH-MAIN", "warehouse_code": "01", "warehouse_name": "Main"},
            {"code": "WH-A", "qty": 3.0, "ref": "", "warehouse_ref": "WH-BRAK", "warehouse_code": "02", "warehouse_name": "Brak isolator"},
            {"code": "WH-A", "qty": 100.0, "ref": "", "warehouse_ref": "WH-OFF", "warehouse_code": "03", "warehouse_name": "Hidden"},
            # item B: only on main
            {"code": "WH-B", "qty": 5.0, "ref": "", "warehouse_ref": "WH-MAIN", "warehouse_code": "01", "warehouse_name": "Main"},
        ]

    monkeypatch.setattr(stock_sync, "get_stock_from_1c_odata", _fake_stock)

    stats = stock_sync.sync_stock_from_odata(db, _mk_req())

    # Aggregated stock matches existing behavior — sum over selected warehouses.
    db.refresh(item_a)
    db.refresh(item_b)
    assert float(item_a.stock_qty) == 10.0
    assert float(item_b.stock_qty) == 5.0

    # Per-warehouse breakdown is populated only with selected warehouses.
    rows_a = (
        db.query(ItemWarehouseStock)
        .filter(ItemWarehouseStock.item_id == item_a.item_id)
        .order_by(ItemWarehouseStock.warehouse_ref1c.asc())
        .all()
    )
    assert [(r.warehouse_ref1c, float(r.qty)) for r in rows_a] == [
        ("WH-BRAK", 3.0),
        ("WH-MAIN", 7.0),
    ]
    rows_b = (
        db.query(ItemWarehouseStock)
        .filter(ItemWarehouseStock.item_id == item_b.item_id)
        .all()
    )
    assert [(r.warehouse_ref1c, float(r.qty)) for r in rows_b] == [("WH-MAIN", 5.0)]

    # Stats expose the new counters.
    assert int(stats.get("warehouse_stock_rows_upserted", 0)) == 3
    assert int(stats.get("warehouse_stock_items_touched", 0)) == 2


def test_sync_stock_refreshes_breakdown_replacing_old_rows(db_session, monkeypatch):
    """
    On re-sync, old (item, warehouse) rows must be replaced with the current
    snapshot. Stale warehouses for an item should disappear once 1C stops
    reporting them.
    """
    db = db_session

    item = Item(
        item_code="WH-REFRESH",
        item_name="Refresh",
        item_article="WH-R",
        stock_qty=0.0,
        status="active",
    )
    db.add(item)
    db.flush()
    db.add(
        StockWarehouse(
            warehouse_ref1c="WH-MAIN",
            warehouse_code="01",
            warehouse_name="Main",
            is_selected=True,
        )
    )
    db.add(
        StockWarehouse(
            warehouse_ref1c="WH-OLD",
            warehouse_code="02",
            warehouse_name="Old",
            is_selected=True,
        )
    )
    # Seed legacy per-warehouse rows so we can verify replacement.
    db.add(ItemWarehouseStock(item_id=item.item_id, warehouse_ref1c="WH-OLD", qty=99.0))
    db.commit()

    def _fake_stock_round1(**kwargs):
        return [
            {"code": "WH-REFRESH", "qty": 4.0, "ref": "", "warehouse_ref": "WH-MAIN", "warehouse_code": "01", "warehouse_name": "Main"},
            {"code": "WH-REFRESH", "qty": 6.0, "ref": "", "warehouse_ref": "WH-OLD", "warehouse_code": "02", "warehouse_name": "Old"},
        ]

    monkeypatch.setattr(stock_sync, "get_stock_from_1c_odata", _fake_stock_round1)
    stock_sync.sync_stock_from_odata(db, _mk_req())

    rows = (
        db.query(ItemWarehouseStock)
        .filter(ItemWarehouseStock.item_id == item.item_id)
        .order_by(ItemWarehouseStock.warehouse_ref1c.asc())
        .all()
    )
    assert [(r.warehouse_ref1c, float(r.qty)) for r in rows] == [
        ("WH-MAIN", 4.0),
        ("WH-OLD", 6.0),
    ]

    # Round 2: WH-OLD disappears from 1C entirely. After the re-sync only
    # WH-MAIN should remain for this item.
    def _fake_stock_round2(**kwargs):
        return [
            {"code": "WH-REFRESH", "qty": 8.0, "ref": "", "warehouse_ref": "WH-MAIN", "warehouse_code": "01", "warehouse_name": "Main"},
        ]

    monkeypatch.setattr(stock_sync, "get_stock_from_1c_odata", _fake_stock_round2)
    stock_sync.sync_stock_from_odata(db, _mk_req())

    rows_after = (
        db.query(ItemWarehouseStock)
        .filter(ItemWarehouseStock.item_id == item.item_id)
        .all()
    )
    assert [(r.warehouse_ref1c, float(r.qty)) for r in rows_after] == [("WH-MAIN", 8.0)]

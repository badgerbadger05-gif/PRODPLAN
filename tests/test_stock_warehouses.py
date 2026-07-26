from app.models import Item, PhysicalImportBatch, StockWarehouse
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


def test_ordinary_stock_sync_mismatch_does_not_create_foreign_physical_batch(
    db_session, monkeypatch
):
    """A legacy stock sweep must not materialize Ledger truth on its own.

    The 1C Balance deliberately disagrees with the legacy quantity (10 -> 7),
    which used to enter reconcile and create an adjustment/import batch.  The
    BUILDING physical-refresh lifecycle is the sole owner of those writes.
    """
    item = Item(
        item_code="ITEM-MISMATCH",
        item_name="Mismatch",
        item_article="ITEM-MISMATCH",
        stock_qty=10.0,
        status="active",
    )
    db_session.add(item)
    db_session.add(
        StockWarehouse(
            warehouse_ref1c="W-MISMATCH",
            warehouse_code="M",
            warehouse_name="Main",
            is_selected=True,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        stock_sync,
        "get_stock_from_1c_odata",
        lambda **kwargs: [
            {
                "code": "ITEM-MISMATCH",
                "qty": 7.0,
                "ref": "",
                "warehouse_ref": "W-MISMATCH",
                "warehouse_code": "M",
                "warehouse_name": "Main",
            }
        ],
    )

    stock_sync.sync_stock_from_odata(db_session, _mk_req())

    db_session.refresh(item)
    assert float(item.stock_qty) == 7.0
    assert db_session.query(PhysicalImportBatch).count() == 0


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


def test_fetch_warehouse_catalog_rows_merges_all_catalog_candidates(monkeypatch):
    calls = []

    def _fake_get_all(self, entity_name, **kwargs):
        calls.append(entity_name)
        if entity_name == "Catalog_Склады":
            return [
                {
                    "Ref_Key": "WH-1",
                    "Code": "НФ-000001",
                    "Description": "Склад основной",
                    "DeletionMark": False,
                }
            ]
        if entity_name == "Catalog_СтруктурныеЕдиницы":
            return [
                {
                    "Ref_Key": "WH-2",
                    "Code": "НФ-000002",
                    "Description": "Склад участка",
                    "DeletionMark": False,
                }
            ]
        if entity_name == "Catalog_СтруктурныеЕдиницыПредприятия":
            return [
                {
                    "Ref_Key": "WH-1",
                    "Code": "НФ-000001",
                    "Description": "Склад основной дубль",
                    "DeletionMark": False,
                },
                {
                    "Ref_Key": "WH-3",
                    "Code": "НФ-000003",
                    "Description": "Кладовая",
                    "DeletionMark": False,
                },
            ]
        return []

    monkeypatch.setattr(stock_sync.OData1CClient, "get_all", _fake_get_all)

    rows, entity = stock_sync._fetch_warehouse_catalog_rows(_mk_req())

    assert calls == [
        "Catalog_Склады",
        "Catalog_СтруктурныеЕдиницы",
        "Catalog_СтруктурныеЕдиницыПредприятия",
        "Catalog_СкладыПредприятия",
    ]
    assert entity == (
        "Catalog_Склады, Catalog_СтруктурныеЕдиницы, "
        "Catalog_СтруктурныеЕдиницыПредприятия"
    )
    assert {row["Ref_Key"] for row in rows} == {"WH-1", "WH-2", "WH-3"}

from datetime import date

from app.models import Item, ItemCategory, PlannedPurchase, PlannedRework, PlanningRun, Unit
from app.schemas import ODataSyncRequest
from app.services.nomenclature_sync import sync_nomenclature_from_odata
from app.services.planning_service import (
    get_run_purchases_grouped_by_category,
    get_run_rework_grouped_by_category,
)


def _mk_run(db) -> PlanningRun:
    run = PlanningRun(
        status="SUCCESS",
        started_by="test",
        horizon_days=10,
        pinned=False,
        config_version_id=None,
        config_snapshot={},
        warnings=[],
        kpi={},
    )
    db.add(run)
    db.flush()
    return run


def test_sync_nomenclature_persists_item_category_link(db_session, monkeypatch):
    db = db_session

    class StubClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_count(self, entity_name, filter_query):
            return 1

        def iter_pages(self, entity_name, filter_query=None, select_fields=None, top=1000, max_pages=1000, order_by=None):
            yield [
                {
                    "Ref_Key": "item-ref-1",
                    "Code": "ITEM-001",
                    "Description": "Test Item",
                    "Артикул": "ART-001",
                    "ЕдиницаИзмерения_Key": "u1",
                    "КатегорияНоменклатуры_Key": "cat-ref-1",
                    "КатегорияНоменклатуры": {"Description": "Категория А"},
                    "СпособПополнения": "Покупка",
                    "СрокПополнения": 5,
                }
            ]

    monkeypatch.setattr("app.services.odata_client.OData1CClient", StubClient)

    result = sync_nomenclature_from_odata(
        db,
        ODataSyncRequest(base_url="http://example.test", entity_name="Catalog_Номенклатура"),
    )

    item = db.query(Item).filter(Item.item_code == "ITEM-001").one()
    category = db.query(ItemCategory).filter(ItemCategory.category_ref1c == "cat-ref-1").one()

    assert result["items_created"] == 1
    assert result["categories_created"] == 1
    assert item.category_id == category.category_id
    assert item.category.category_name == "Категория А"


def test_get_run_purchases_grouped_by_category_groups_items(db_session):
    db = db_session

    unit = Unit(unit_ref1c="u-pcat", unit_name="шт", short_name="шт", precision=0)
    group_a = ItemCategory(category_name="Группа А", category_ref1c="cat-a")
    group_b = ItemCategory(category_name="Группа Б", category_ref1c="cat-b")
    db.add_all([unit, group_a, group_b])
    db.flush()

    item_a = Item(item_code="BUY-A", item_name="Buy A", item_article="A", unit="u-pcat", category_id=group_a.category_id, status="active")
    item_b = Item(item_code="BUY-B", item_name="Buy B", item_article="B", unit="u-pcat", category_id=group_b.category_id, status="active")
    item_none = Item(item_code="BUY-N", item_name="Buy None", item_article="N", unit="u-pcat", status="active")
    db.add_all([item_a, item_b, item_none])
    db.flush()

    run = _mk_run(db)
    db.add_all(
        [
            PlannedPurchase(run_id=run.run_id, item_id=item_a.item_id, requested_qty=5, planned_qty=5, qty=5, need_date=date(2025, 1, 10), order_date=date(2025, 1, 5), lead_time_days=5, bucket_date=date(2025, 1, 10)),
            PlannedPurchase(run_id=run.run_id, item_id=item_b.item_id, requested_qty=3, planned_qty=3, qty=3, need_date=date(2025, 1, 11), order_date=date(2025, 1, 6), lead_time_days=5, bucket_date=date(2025, 1, 11)),
            PlannedPurchase(run_id=run.run_id, item_id=item_none.item_id, requested_qty=2, planned_qty=2, qty=2, need_date=date(2025, 1, 12), order_date=date(2025, 1, 7), lead_time_days=5, bucket_date=date(2025, 1, 12)),
        ]
    )
    db.commit()

    result = get_run_purchases_grouped_by_category(db=db, run_id=run.run_id)

    assert result["total_groups"] == 3
    names = [group["group_name"] for group in result["groups"]]
    assert "Группа А" in names
    assert "Группа Б" in names
    assert "Без товарной группы" in names


def test_get_run_rework_grouped_by_category_groups_and_counts_flags(db_session):
    db = db_session

    unit = Unit(unit_ref1c="u-rcat", unit_name="шт", short_name="шт", precision=0)
    group = ItemCategory(category_name="Переработка Группа", category_ref1c="cat-r")
    db.add_all([unit, group])
    db.flush()

    item_a = Item(item_code="RW-A", item_name="RW A", item_article="A", unit="u-rcat", category_id=group.category_id, status="active")
    item_b = Item(item_code="RW-B", item_name="RW B", item_article="B", unit="u-rcat", category_id=group.category_id, status="active")
    db.add_all([item_a, item_b])
    db.flush()

    run = _mk_run(db)
    db.add_all(
        [
            PlannedRework(run_id=run.run_id, item_id=item_a.item_id, spec_id=None, requested_qty=5, planned_qty=5, qty=5, need_date=date(2025, 1, 10), order_date=date(2025, 1, 9), lead_time_days=1, bucket_date=date(2025, 1, 10), component_limit=5, component_blocked=False, component_partial=False, shortage={"planned_qty": 5}),
            PlannedRework(run_id=run.run_id, item_id=item_b.item_id, spec_id=None, requested_qty=7, planned_qty=4, qty=4, need_date=date(2025, 1, 11), order_date=date(2025, 1, 10), lead_time_days=1, bucket_date=date(2025, 1, 11), component_limit=4, component_blocked=False, component_partial=True, shortage={"planned_qty": 4}),
        ]
    )
    db.commit()

    result = get_run_rework_grouped_by_category(db=db, run_id=run.run_id)

    assert result["total_groups"] == 1
    group_row = result["groups"][0]
    assert group_row["group_name"] == "Переработка Группа"
    assert group_row["sum_qty"] == 9.0
    assert group_row["sum_requested_qty"] == 12.0
    assert group_row["sum_planned_qty"] == 9.0
    assert group_row["blocked_orders"] == 0
    assert group_row["partial_orders"] == 1

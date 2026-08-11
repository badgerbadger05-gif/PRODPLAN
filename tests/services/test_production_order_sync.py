import datetime

import pytest

from app import models
from app.models import (
    Item,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ProductionStage,
    Specification,
    WorkshopWarehouseBinding,
)
from app.schemas import ODataSyncRequest
from app.services.production_order_sync import (
    PRODUCTION_ORDER_SYNC_FROM_1C,
    _resolve_product_destination,
    sync_production_facts,
    sync_production_orders_from_odata,
)


DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"
ASSEMBLY_RECORDER_TYPE = "Document_СборкаЗапасов"


def test_production_order_sync_includes_completed_orders(db_session, monkeypatch):
    db = db_session

    item = Item(
        item_code="PROD-SYNC-ITEM",
        item_name="Production Sync Item",
        item_article="PROD-SYNC-ITEM",
        item_ref1c="item-ref-1",
        status="active",
    )
    db.add(item)
    db.flush()

    seen_order_filter = {}

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
            if entity_name == "Document_ЗаказНаПроизводство":
                seen_order_filter["value"] = filter_query
                assert filter_query == f"Date ge datetime'{PRODUCTION_ORDER_SYNC_FROM_1C}' and Posted eq true"
                assert "DeletionMark" in select_fields
                assert "СостояниеЗаказа_Key" in select_fields
                assert "СтруктурнаяЕдиницаПродукции_Key" in select_fields
                return [
                    {
                        "Ref_Key": "order-ref-done",
                        "Number": "2047",
                        "Date": "2026-06-15T00:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "СостояниеЗаказа_Key": DONE_STATE_KEY,
                        "СтруктурнаяЕдиницаПродукции_Key": "warehouse-header",
                    }
                ]
            if entity_name == "Document_ЗаказНаПроизводство_Продукция":
                assert "СтруктурнаяЕдиница_Key" in select_fields
                return [
                    {
                        "Ref_Key": "order-ref-done",
                        "LineNumber": 1,
                        "Номенклатура_Key": "item-ref-1",
                        "Количество": 2.0,
                        "СтруктурнаяЕдиница_Key": "00000000-0000-0000-0000-000000000000",
                    }
                ]
            if entity_name == "Document_СборкаЗапасов":
                return []
            raise AssertionError(f"Unexpected OData entity: {entity_name}")

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    stats = sync_production_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    assert stats["orders_total"] == 1
    assert stats["orders_created"] == 1
    assert seen_order_filter["value"] == f"Date ge datetime'{PRODUCTION_ORDER_SYNC_FROM_1C}' and Posted eq true"

    order = db.query(ProductionOrder).one()
    assert order.order_ref1c == "order-ref-done"
    assert order.order_number == "2047"
    assert order.order_date == datetime.datetime(2026, 6, 15)
    assert order.order_state_key == DONE_STATE_KEY
    assert order.deletion_mark is False

    product = db.query(ProductionProduct).one()
    assert product.order_id == order.order_id
    assert product.item_id == item.item_id
    assert product.line_number == 1
    assert float(product.quantity) == 2.0
    assert product.destination_warehouse_ref1c == "warehouse-header"


def test_production_order_sync_missing_orders_remain_unchanged(db_session, monkeypatch):
    db = db_session

    old_order = ProductionOrder(
        order_number="APR-OLD",
        order_date=datetime.datetime(2026, 4, 20),
        order_ref1c="order-ref-april",
        is_posted=True,
        source="1c",
        deletion_mark=False,
    )
    may_order = ProductionOrder(
        order_number="MAY-MISSING",
        order_date=datetime.datetime(2026, 5, 10),
        order_ref1c="order-ref-may",
        is_posted=True,
        source="mrp",
        deletion_mark=False,
    )
    local_order = ProductionOrder(
        order_number="LOCAL-ONLY",
        order_date=datetime.datetime(2026, 5, 11),
        order_ref1c="order-ref-local",
        is_posted=True,
        deletion_mark=False,
    )
    db.add_all([old_order, may_order, local_order])
    db.commit()

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_all(self, entity_name, **_kwargs):
            if entity_name == "Document_ЗаказНаПроизводство":
                return [
                    {
                        "Ref_Key": "order-ref-loaded",
                        "Number": "MAY-LOADED",
                        "Date": "2026-05-11T00:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "СостояниеЗаказа_Key": DONE_STATE_KEY,
                    }
                ]
            if entity_name == "Document_ЗаказНаПроизводство_Продукция":
                return []
            if entity_name == "Document_СборкаЗапасов":
                return []
            raise AssertionError(f"Unexpected OData entity: {entity_name}")

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    sync_production_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    db.refresh(old_order)
    db.refresh(may_order)
    db.refresh(local_order)
    assert old_order.deletion_mark is False
    assert may_order.deletion_mark is False
    assert local_order.deletion_mark is False
    assert old_order.order_number == "APR-OLD"
    assert may_order.order_number == "MAY-MISSING"
    assert local_order.order_number == "LOCAL-ONLY"


def test_production_order_sync_respects_truncation_and_preserves_orders(db_session, monkeypatch):
    db = db_session

    mrp_order = ProductionOrder(
        order_number="MRP-KEEP",
        order_date=datetime.datetime(2026, 5, 12),
        order_ref1c="order-ref-mrp",
        is_posted=True,
        source="mrp",
        deletion_mark=False,
    )
    one_c_order = ProductionOrder(
        order_number="1C-OLD",
        order_date=datetime.datetime(2026, 5, 13),
        order_ref1c="order-ref-1c",
        is_posted=True,
        source="1c",
        deletion_mark=False,
    )
    db.add_all([mrp_order, one_c_order])
    db.flush()
    db.commit()

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            self.last_result_truncated = False

        def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
            if entity_name == "Document_ЗаказНаПроизводство":
                self.last_result_truncated = True
                return [
                    {
                        "Ref_Key": "order-ref-1c-loaded",
                        "Number": "NEW-1C",
                        "Date": "2026-05-14T00:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "СостояниеЗаказа_Key": DONE_STATE_KEY,
                    }
                ]
            if entity_name == "Document_ЗаказНаПроизводство_Продукция":
                return []
            raise AssertionError(f"Unexpected OData entity: {entity_name}")

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    with pytest.raises(Exception):
        sync_production_orders_from_odata(
            db,
            ODataSyncRequest(
                base_url="http://1c.example/odata",
                entity_name="Document_ЗаказНаПроизводство",
            ),
        )

    db.refresh(mrp_order)
    db.refresh(one_c_order)
    assert mrp_order.order_number == "MRP-KEEP"
    assert one_c_order.order_number == "1C-OLD"
    assert mrp_order.deletion_mark is False
    assert one_c_order.deletion_mark is False


def test_production_order_sync_fails_when_state_field_is_missing_from_payload(db_session, monkeypatch):
    db = db_session

    preserved = ProductionOrder(
        order_number="PRESERVE",
        order_date=datetime.datetime(2026, 5, 12),
        order_ref1c="order-ref-present",
        is_posted=True,
        deletion_mark=False,
    )
    db.add(preserved)
    db.commit()

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            self.last_result_truncated = False

        def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
            if entity_name == "Document_ЗаказНаПроизводство":
                return [
                    {
                        "Ref_Key": "order-ref-bad",
                        "Number": "BAD",
                        "Date": "2026-05-14T00:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                    }
                ]
            if entity_name == "Document_ЗаказНаПроизводство_Продукция":
                return []
            return []

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    with pytest.raises(Exception):
        sync_production_orders_from_odata(
            db,
            ODataSyncRequest(
                base_url="http://1c.example/odata",
                entity_name="Document_ЗаказНаПроизводство",
            ),
        )

    assert (
        db.query(ProductionOrder)
        .filter(ProductionOrder.order_ref1c == "order-ref-present")
        .one()
        .deletion_mark
        is False
    )
    assert (
        db.query(ProductionOrder)
        .filter(ProductionOrder.order_ref1c == "order-ref-present")
        .one()
        .order_number
        == "PRESERVE"
    )


def test_production_order_sync_fails_when_product_lines_read_is_truncated(db_session, monkeypatch):
    db = db_session

    preserved = ProductionOrder(
        order_number="PRESERVE-LINE",
        order_date=datetime.datetime(2026, 5, 12),
        order_ref1c="order-ref-preserve-lines",
        is_posted=True,
        deletion_mark=False,
    )
    db.add(preserved)
    db.commit()

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            self.last_result_truncated = False

        def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
            if entity_name == "Document_ЗаказНаПроизводство":
                self.last_result_truncated = False
                return [
                    {
                        "Ref_Key": "order-ref-preserve-lines",
                        "Number": "LINES",
                        "Date": "2026-05-12T00:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "СостояниеЗаказа_Key": DONE_STATE_KEY,
                    }
                ]
            if entity_name == "Document_ЗаказНаПроизводство_Продукция":
                self.last_result_truncated = True
                return []
            return []

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    with pytest.raises(Exception):
        sync_production_orders_from_odata(
            db,
            ODataSyncRequest(
                base_url="http://1c.example/odata",
                entity_name="Document_ЗаказНаПроизводство",
            ),
        )

    assert (
        db.query(ProductionOrder)
        .filter(ProductionOrder.order_ref1c == "order-ref-preserve-lines")
        .one()
        .deletion_mark
        is False
    )
    assert (
        db.query(ProductionOrder)
        .filter(ProductionOrder.order_ref1c == "order-ref-preserve-lines")
        .one()
        .order_number
        == "PRESERVE-LINE"
    )


def test_production_order_sync_allows_present_null_state_and_skips_record(db_session, monkeypatch):
    db = db_session

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            self.last_result_truncated = False

        def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
            if entity_name == "Document_ЗаказНаПроизводство":
                return [
                    {
                        "Ref_Key": "order-ref-null",
                        "Number": "NULL-STATE",
                        "Date": "2026-05-14T00:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "СостояниеЗаказа_Key": None,
                    }
                ]
            if entity_name == "Document_ЗаказНаПроизводство_Продукция":
                return []
            return []

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    stats = sync_production_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    assert stats["dry_run"] is True
    assert stats["orders_total"] == 0


def test_production_order_sync_skips_product_fields_for_mrp_source(db_session, monkeypatch):
    db = db_session

    item = Item(item_code="MRP-ITEM", item_name="MRP Item", item_ref1c="mrp-item")
    spec = Specification(
        spec_code="MRP-SPEC",
        spec_name="MRP Spec",
        spec_ref1c="mrp-spec",
    )
    stage = ProductionStage(stage_name="MRP Stage", stage_ref1c="mrp-stage")
    db.add_all([item, spec, stage])
    db.flush()

    order = ProductionOrder(
        order_number="MRP-ORIG",
        order_date=datetime.datetime(2026, 5, 12),
        order_ref1c="order-ref-mrp-sync",
        is_posted=True,
        source="mrp",
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=2.0,
        produced_qty=0.0,
        remaining_qty=2.0,
        spec_id=spec.spec_id,
        stage_id=stage.stage_id,
        characteristic_ref1c="old-char",
        destination_warehouse_ref1c="old-warehouse",
    )
    db.add(product)
    db.commit()

    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            self.last_result_truncated = False

        def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
            if entity_name == "Document_ЗаказНаПроизводство":
                return [
                    {
                        "Ref_Key": "order-ref-mrp-sync",
                        "Number": "MRP-SYNC",
                        "Date": "2026-05-12T00:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "СостояниеЗаказа_Key": DONE_STATE_KEY,
                    }
                ]
            if entity_name == "Document_ЗаказНаПроизводство_Продукция":
                return [
                    {
                        "Ref_Key": "order-ref-mrp-sync",
                        "LineNumber": 2,
                        "Номенклатура_Key": "mrp-item",
                        "Количество": 9.0,
                        "Спецификация_Key": "missing",
                        "Этап_Key": "missing-stage",
                        "Характеристика_Key": "new-char",
                        "СтруктурнаяЕдиница_Key": "new-warehouse",
                    }
                ]
            return []

    monkeypatch.setattr("app.services.odata_client.OData1CClient", FakeODataClient)

    sync_production_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    db.refresh(order)
    db.refresh(product)
    assert order.order_number == "MRP-SYNC"
    assert float(product.quantity) == 2.0
    assert product.spec_id == spec.spec_id
    assert product.stage_id == stage.stage_id
    assert product.line_number == 1
    assert product.characteristic_ref1c == "old-char"
    assert product.destination_warehouse_ref1c == "old-warehouse"


CUTOFF = datetime.datetime(2026, 7, 25)


def _accepted_generation(db, key="fact-cache"):
    """Принятое поколение с завершённой границей физического импорта."""
    batch = models.PhysicalImportBatch(
        batch_key=f"physical:{key}",
        status="completed",
        cutoff=CUTOFF,
        completed_at=CUTOFF,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=key,
        status="accepted",
        cutoff=CUTOFF,
        accepted_at=CUTOFF,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=batch,
        algorithm_version="tests/1",
    )
    db.add(generation)
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    db.flush()
    return generation, batch


def _later_import_batch(db, key="fact-cache-later"):
    batch = models.PhysicalImportBatch(
        batch_key=f"physical:{key}",
        status="completed",
        cutoff=CUTOFF,
        completed_at=CUTOFF,
        source_watermarks={},
    )
    db.add(batch)
    db.flush()
    return batch


def _order_with_line(db, *, item, order_ref1c, qty=10.0, number="ASM-1"):
    order = ProductionOrder(
        order_number=number,
        order_date=datetime.datetime(2026, 7, 20),
        order_ref1c=order_ref1c,
        is_posted=True,
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=qty,
        produced_qty=0,
        remaining_qty=qty,
    )
    db.add(product)
    db.flush()
    return order, product


def _assembly_fact(db, *, batch, item, recorder_ref, qty, line_no="1"):
    row = models.StockLedgerEntry(
        ingest_batch_id=batch.id,
        source_content_hash=f"hash:{recorder_ref}:{line_no}",
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="warehouse-1",
        qty=qty,
        qty_after=qty,
        posting_at=datetime.datetime(2026, 7, 22),
        record_type="Receipt",
        movement_kind="assembly_in",
        recorder_type=ASSEMBLY_RECORDER_TYPE,
        recorder_ref=recorder_ref,
        line_no=line_no,
        ingest_source="pull",
    )
    db.add(row)
    db.flush()
    return row


def _recorder_pull(db, *, recorder_ref, order_ref):
    db.add(models.StockRecorderPull(
        recorder_type=ASSEMBLY_RECORDER_TYPE,
        recorder_ref=recorder_ref,
        order_ref=order_ref,
        status="done",
        source="test",
    ))
    db.flush()


def _no_odata(monkeypatch):
    """Факт выпуска больше не имеет второго канала: 1С не читается вовсе."""
    class ForbiddenODataClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("Факт выпуска не должен читать 1С")

    monkeypatch.setattr("app.services.odata_client.OData1CClient", ForbiddenODataClient)


def _fact_item(db, code="ASM-ITEM"):
    item = Item(item_code=code, item_name=code, item_ref1c=f"ref-{code}")
    db.add(item)
    db.flush()
    return item


def test_production_fact_cache_follows_accepted_ledger(db_session, monkeypatch):
    """(а) Факт в принятом поколении обновляет кэш выпуска строки заказа."""
    db = db_session
    _no_odata(monkeypatch)
    _generation, batch = _accepted_generation(db)
    item = _fact_item(db)
    _order, product = _order_with_line(db, item=item, order_ref1c="asm-order")
    _assembly_fact(db, batch=batch, item=item, recorder_ref="asm-doc-1", qty=3)
    _recorder_pull(db, recorder_ref="asm-doc-1", order_ref="asm-order")
    db.commit()

    stats = sync_production_facts(db)

    assert stats["status"] == "ok"
    assert stats["facts"] == 1
    assert stats["matched_facts"] == 1
    assert stats["products_updated"] == 1
    db.refresh(product)
    assert float(product.produced_qty) == 3.0
    assert float(product.remaining_qty) == 7.0


def test_production_fact_cache_uses_exact_manufacture_link(db_session, monkeypatch):
    """Команда «Произвести» даёт точную идентичность факта до строки."""
    db = db_session
    _no_odata(monkeypatch)
    _generation, batch = _accepted_generation(db)
    item = _fact_item(db)
    order, product = _order_with_line(db, item=item, order_ref1c="asm-order")
    other_order, other_product = _order_with_line(
        db, item=item, order_ref1c="other-order", number="ASM-2"
    )
    db.add(models.ProductionManufacture(
        product_id=product.product_id,
        order_id=order.order_id,
        qty=4,
        status="exported",
        exported_ref1c="asm-doc-1",
    ))
    _assembly_fact(db, batch=batch, item=item, recorder_ref="asm-doc-1", qty=4)
    db.commit()

    stats = sync_production_facts(db)

    assert stats["exact_link_facts"] == 1
    db.refresh(product)
    db.refresh(other_product)
    assert float(product.produced_qty) == 4.0
    assert float(other_product.produced_qty) == 0.0
    assert other_order.order_id != order.order_id


def test_exact_manufacture_output_overflow_stays_surplus(db_session, monkeypatch):
    db = db_session
    _no_odata(monkeypatch)
    _generation, batch = _accepted_generation(db)
    item = _fact_item(db, code="ASM-EXACT-OVER")
    order, product = _order_with_line(
        db,
        item=item,
        order_ref1c="asm-exact-over-order",
        qty=10,
    )
    db.add(
        models.ProductionManufacture(
            product_id=product.product_id,
            order_id=order.order_id,
            qty=10,
            status="exported",
            exported_ref1c="asm-exact-over-doc",
        )
    )
    _assembly_fact(
        db,
        batch=batch,
        item=item,
        recorder_ref="asm-exact-over-doc",
        qty=12,
    )
    db.commit()

    stats = sync_production_facts(db)

    assert stats["exact_link_facts"] == 1
    assert stats["surplus_qty"] == 2.0
    db.refresh(product)
    assert float(product.produced_qty) == 10.0
    assert float(product.remaining_qty) == 0.0


@pytest.mark.parametrize(
    ("link_status", "expected_matched"),
    [("posted", 1), ("planned", 0), ("error", 0)],
)
def test_production_fact_cache_accepts_only_fact_eligible_material_issue_links(
    db_session,
    monkeypatch,
    link_status,
    expected_matched,
):
    """`posted` preserves provenance; non-terminal links cannot identify fact."""
    db = db_session
    _no_odata(monkeypatch)
    generation, batch = _accepted_generation(db, key=f"issue-{link_status}")
    item = _fact_item(db, code=f"ASM-ISSUE-{link_status}")
    order, product = _order_with_line(
        db,
        item=item,
        order_ref1c=f"order-{link_status}",
    )
    issue = models.ProductionMaterialIssue(
        document_number=f"MI-{link_status}",
        product_id=product.product_id,
        order_id=order.order_id,
        status=link_status,
        ledger_generation_id=generation.id,
    )
    db.add(issue)
    db.flush()
    recorder_ref = f"assembly-{link_status}"
    db.add(models.SyncLink(
        source_doctype="material_issue",
        source_id=issue.issue_id,
        target_entity="Document_ПеремещениеЗапасов",
        target_ref_key=recorder_ref,
        status=link_status,
    ))
    _assembly_fact(
        db,
        batch=batch,
        item=item,
        recorder_ref=recorder_ref,
        qty=4,
    )
    db.commit()

    stats = sync_production_facts(db)

    assert stats["matched_facts"] == expected_matched
    db.refresh(product)
    assert float(product.produced_qty) == (4.0 if expected_matched else 0.0)


def test_production_fact_cache_ignores_document_outside_accepted_generation(
    db_session, monkeypatch
):
    """(б) Ключевой тест канона: документ 1С есть, а в принятом Ledger его нет."""
    db = db_session
    _no_odata(monkeypatch)
    _generation, _batch = _accepted_generation(db)
    later = _later_import_batch(db)
    item = _fact_item(db)
    _order, product = _order_with_line(db, item=item, order_ref1c="asm-order")
    # Документ проведён в 1С и уже вытянут, но его ревизия попала в импорт-батч
    # ПОЗЖЕ границы принятого поколения — значит фактом он ещё не является.
    _assembly_fact(db, batch=later, item=item, recorder_ref="asm-doc-late", qty=5)
    _recorder_pull(db, recorder_ref="asm-doc-late", order_ref="asm-order")
    db.commit()

    stats = sync_production_facts(db)

    assert stats["status"] == "ok"
    assert stats["facts"] == 0
    db.refresh(product)
    assert float(product.produced_qty) == 0.0
    assert float(product.remaining_qty) == 10.0


def test_production_fact_cache_is_unavailable_without_accepted_generation(
    db_session, monkeypatch
):
    """(в) Без принятого поколения кэш не переписывается и не нулится."""
    db = db_session
    _no_odata(monkeypatch)
    item = _fact_item(db)
    _order, product = _order_with_line(db, item=item, order_ref1c="asm-order")
    product.produced_qty = 4
    product.remaining_qty = 6
    db.commit()

    stats = sync_production_facts(db)

    assert stats["status"] == "unavailable"
    assert stats["truth_status"] == "uninitialized"
    assert stats["products_updated"] == 0
    assert stats["reason"]
    db.refresh(product)
    assert float(product.produced_qty) == 4.0
    assert float(product.remaining_qty) == 6.0


def test_production_fact_cache_keeps_cancellation_separate_from_physical_remaining(
    db_session, monkeypatch
):
    """Статус cancelled не имеет права подменять физический остаток нулём."""
    db = db_session
    _no_odata(monkeypatch)
    _generation, batch = _accepted_generation(db)
    item = _fact_item(db)
    _order, product = _order_with_line(db, item=item, order_ref1c="asm-order")
    product.remaining_qty = 0
    db.add(ProductionOrderLineState(
        product_id=product.product_id, status="cancelled"
    ))
    _assembly_fact(db, batch=batch, item=item, recorder_ref="asm-doc-1", qty=2)
    _recorder_pull(db, recorder_ref="asm-doc-1", order_ref="asm-order")
    db.commit()

    sync_production_facts(db)

    db.refresh(product)
    assert float(product.produced_qty) == 2.0
    assert float(product.remaining_qty) == 8.0


def test_production_fact_cache_repairs_corrupted_remaining_when_produced_is_unchanged(
    db_session, monkeypatch
):
    db = db_session
    _no_odata(monkeypatch)
    _accepted_generation(db)
    item = _fact_item(db)
    _order, product = _order_with_line(db, item=item, order_ref1c="asm-order")
    product.produced_qty = 0
    product.remaining_qty = 0
    db.commit()

    stats = sync_production_facts(db)

    db.refresh(product)
    assert stats["products_updated"] == 1
    assert float(product.produced_qty) == 0.0
    assert float(product.remaining_qty) == 10.0


def test_production_fact_cache_leaves_ambiguous_fact_unassigned(db_session, monkeypatch):
    """Факт с несколькими заказами-кандидатами не приписывается произвольно."""
    db = db_session
    _no_odata(monkeypatch)
    _generation, batch = _accepted_generation(db)
    item = _fact_item(db)
    _first_order, first = _order_with_line(db, item=item, order_ref1c="asm-order")
    _second_order, second = _order_with_line(
        db, item=item, order_ref1c="asm-order-2", number="ASM-2"
    )
    # Шапка документа называет один заказ, а сам recorder совпадает с
    # order_ref1c другого: два кандидата, значит связь недоказана.
    _assembly_fact(db, batch=batch, item=item, recorder_ref="asm-order-2", qty=6)
    _recorder_pull(db, recorder_ref="asm-order-2", order_ref="asm-order")
    db.commit()

    stats = sync_production_facts(db)

    assert stats["ambiguous_facts"] == 1
    assert stats["matched_facts"] == 0
    db.refresh(first)
    db.refresh(second)
    assert float(first.produced_qty) == 0.0
    assert float(second.produced_qty) == 0.0


def test_production_fact_cache_spreads_order_fact_across_lines_oldest_first(
    db_session, monkeypatch
):
    """Без точной связи выпуск ложится на строки заказа FIFO, ничего не теряя."""
    db = db_session
    _no_odata(monkeypatch)
    _generation, batch = _accepted_generation(db)
    item = _fact_item(db)
    order, first = _order_with_line(db, item=item, order_ref1c="asm-order", qty=4.0)
    second = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=2,
        quantity=5.0,
        produced_qty=0,
        remaining_qty=5.0,
    )
    db.add(second)
    db.flush()
    _assembly_fact(db, batch=batch, item=item, recorder_ref="asm-doc-1", qty=6)
    _recorder_pull(db, recorder_ref="asm-doc-1", order_ref="asm-order")
    db.commit()

    stats = sync_production_facts(db)

    assert stats["order_scope_facts"] == 1
    assert stats["surplus_qty"] == 0.0
    db.refresh(first)
    db.refresh(second)
    assert float(first.produced_qty) == 4.0
    assert float(first.remaining_qty) == 0.0
    assert float(second.produced_qty) == 2.0
    assert float(second.remaining_qty) == 3.0


def test_production_fact_cache_keeps_order_overflow_as_surplus(db_session, monkeypatch):
    """Order output never exceeds its obligations; physical excess stays explicit."""
    db = db_session
    _no_odata(monkeypatch)
    _generation, batch = _accepted_generation(db)
    item = _fact_item(db)
    order, first = _order_with_line(db, item=item, order_ref1c="asm-order", qty=4.0)
    second = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=2,
        quantity=5.0,
        produced_qty=0,
        remaining_qty=5.0,
    )
    db.add(second)
    db.flush()
    _assembly_fact(db, batch=batch, item=item, recorder_ref="asm-doc-over", qty=12)
    _recorder_pull(db, recorder_ref="asm-doc-over", order_ref="asm-order")
    db.commit()

    stats = sync_production_facts(db)

    assert stats["order_scope_facts"] == 1
    assert stats["surplus_qty"] == 3.0
    db.refresh(first)
    db.refresh(second)
    assert float(first.produced_qty) == 4.0
    assert float(second.produced_qty) == 5.0
    assert float(first.produced_qty) <= float(first.quantity)
    assert float(second.produced_qty) <= float(second.quantity)


def test_production_fact_cache_dry_run_does_not_persist(db_session, monkeypatch):
    db = db_session
    _no_odata(monkeypatch)
    _generation, batch = _accepted_generation(db)
    item = _fact_item(db)
    _order, product = _order_with_line(db, item=item, order_ref1c="asm-order")
    _assembly_fact(db, batch=batch, item=item, recorder_ref="asm-doc-1", qty=3)
    _recorder_pull(db, recorder_ref="asm-doc-1", order_ref="asm-order")
    db.commit()

    stats = sync_production_facts(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_СборкаЗапасов",
            dry_run=True,
        ),
    )

    assert stats["dry_run"] is True
    db.refresh(product)
    assert float(product.produced_qty) == 0.0


def test_production_destination_falls_back_to_existing_workshop_binding(db_session):
    item = Item(item_code="BIND", item_name="Binding item")
    order = ProductionOrder(
        order_number="BIND-1",
        order_date=datetime.datetime(2026, 8, 1),
        order_ref1c="bind-order",
    )
    workshop = ProductionResource(resource_name="Binding workshop")
    db_session.add_all([item, order, workshop])
    db_session.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        quantity=1,
        produced_qty=0,
        remaining_qty=1,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add_all(
        [
            ProductionOrderLineState(
                product_id=product.product_id, workshop_id=workshop.resource_id
            ),
            WorkshopWarehouseBinding(
                workshop_id=workshop.resource_id,
                warehouse_ref1c="reserve-ref",
                production_warehouse_ref1c="production-ref",
            ),
        ]
    )
    db_session.flush()

    destination, source = _resolve_product_destination(
        db_session,
        {"СтруктурнаяЕдиница_Key": "00000000-0000-0000-0000-000000000000"},
        {
            "СтруктурнаяЕдиницаПродукции_Key": "00000000-0000-0000-0000-000000000000"
        },
        product,
    )

    assert (destination, source) == ("production-ref", "binding")


def _spec_stage_fixture(db):
    """Item + Specification + ProductionStage all carrying 1C refs."""
    item = Item(
        item_code="SPEC-SYNC-ITEM",
        item_name="Spec Sync Item",
        item_article="SPEC-SYNC-ITEM",
        item_ref1c="item-ref-spec",
                status="active",
    )
    spec = Specification(
        spec_code="SP-1",
        spec_name="Spec 1",
        spec_ref1c="spec-ref-1",
    )
    stage = ProductionStage(stage_name="Сварка", stage_ref1c="stage-ref-1")
    db.add_all([item, spec, stage])
    db.flush()
    return item, spec, stage


def _spec_stage_client(product_row, *, reject_extended=False, calls=None):
    class FakeODataClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_all(self, entity_name, filter_query=None, select_fields=None, **_kwargs):
            if entity_name == "Document_ЗаказНаПроизводство":
                return [
                    {
                        "Ref_Key": "order-ref-spec",
                        "Number": "3001",
                        "Date": "2026-06-15T00:00:00",
                        "Posted": True,
                        "DeletionMark": False,
                        "СостояниеЗаказа_Key": DONE_STATE_KEY,
                    }
                ]
            if entity_name == "Document_ЗаказНаПроизводство_Продукция":
                if calls is not None:
                    calls.append(list(select_fields or []))
                if reject_extended and "Спецификация_Key" in (select_fields or []):
                    raise RuntimeError("Bad request: path segment is not found")
                return [dict(product_row)]
            if entity_name == "Document_СборкаЗапасов":
                return []
            raise AssertionError(f"Unexpected OData entity: {entity_name}")

    return FakeODataClient


def test_production_order_sync_resolves_spec_and_stage_refs(db_session, monkeypatch):
    db = db_session
    _item, spec, stage = _spec_stage_fixture(db)
    db.commit()

    calls = []
    monkeypatch.setattr(
        "app.services.odata_client.OData1CClient",
        _spec_stage_client(
            {
                "Ref_Key": "order-ref-spec",
                "LineNumber": 1,
                "Номенклатура_Key": "item-ref-spec",
                "Количество": 3.0,
                "Спецификация_Key": "spec-ref-1",
                "Этап_Key": "stage-ref-1",
            },
            calls=calls,
        ),
    )

    sync_production_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    assert "Спецификация_Key" in calls[0]
    assert "Этап_Key" in calls[0]
    product = db.query(ProductionProduct).one()
    assert product.spec_id == spec.spec_id
    assert product.stage_id == stage.stage_id


def test_production_order_sync_keeps_spec_and_stage_when_1c_omits_them(
    db_session, monkeypatch
):
    """Regression: a sync without Спецификация_Key/Этап_Key used to NULL them."""
    db = db_session
    item, spec, stage = _spec_stage_fixture(db)
    order = ProductionOrder(
        order_number="3001",
        order_date=datetime.datetime(2026, 6, 15),
        order_ref1c="order-ref-spec",
        is_posted=True,
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    db.add(
        ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=3,
            produced_qty=0,
            remaining_qty=3,
            spec_id=spec.spec_id,
            stage_id=stage.stage_id,
        )
    )
    db.commit()

    monkeypatch.setattr(
        "app.services.odata_client.OData1CClient",
        _spec_stage_client(
            {
                "Ref_Key": "order-ref-spec",
                "LineNumber": 1,
                "Номенклатура_Key": "item-ref-spec",
                "Количество": 4.0,
            }
        ),
    )

    sync_production_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    product = db.query(ProductionProduct).one()
    assert float(product.quantity) == 4.0
    assert product.spec_id == spec.spec_id
    assert product.stage_id == stage.stage_id


def test_production_order_sync_falls_back_when_1c_rejects_spec_select(
    db_session, monkeypatch
):
    db = db_session
    item, spec, stage = _spec_stage_fixture(db)
    order = ProductionOrder(
        order_number="3001",
        order_date=datetime.datetime(2026, 6, 15),
        order_ref1c="order-ref-spec",
        is_posted=True,
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    db.add(
        ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=3,
            produced_qty=0,
            remaining_qty=3,
            spec_id=spec.spec_id,
            stage_id=stage.stage_id,
        )
    )
    db.commit()

    calls = []
    monkeypatch.setattr(
        "app.services.odata_client.OData1CClient",
        _spec_stage_client(
            {
                "Ref_Key": "order-ref-spec",
                "LineNumber": 1,
                "Номенклатура_Key": "item-ref-spec",
                "Количество": 3.0,
            },
            reject_extended=True,
            calls=calls,
        ),
    )

    stats = sync_production_orders_from_odata(
        db,
        ODataSyncRequest(
            base_url="http://1c.example/odata",
            entity_name="Document_ЗаказНаПроизводство",
        ),
    )

    # Первая попытка с расширенным $select, вторая — на минимальном наборе.
    assert "Спецификация_Key" in calls[0]
    assert "Спецификация_Key" not in calls[1]
    assert stats["products_failed"] == 0
    product = db.query(ProductionProduct).one()
    assert product.spec_id == spec.spec_id
    assert product.stage_id == stage.stage_id


def test_production_destination_line_precedes_header(db_session):
    destination, source = _resolve_product_destination(
        db_session,
        {"СтруктурнаяЕдиница_Key": "line-ref"},
        {"СтруктурнаяЕдиницаПродукции_Key": "header-ref"},
    )

    assert (destination, source) == ("line-ref", "line")

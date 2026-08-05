"""Specification reconcile preserves operation rows referenced by manufactures."""
from __future__ import annotations

from datetime import datetime

import app.services.odata_client as odata_client_module
from app.models import (
    Item,
    Operation,
    ProductionManufacture,
    ProductionManufactureOperation,
    ProductionOrder,
    ProductionProduct,
    SpecOperation,
    Specification,
)
from app.schemas import ODataSyncRequest
from app.services import specification_sync


def _patch_client(monkeypatch, records):
    class _Fake:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_all(self, _entity_name, filter_query=None, select_fields=None):
            return records

    monkeypatch.setattr(odata_client_module, "OData1CClient", _Fake)


def _request():
    return ODataSyncRequest(
        base_url="http://demo/odata",
        entity_name="Catalog_Спецификации",
    )


def _spec_record(operations):
    return {
        "Ref_Key": "spec-1",
        "Code": "S-1",
        "Description": "Спека",
        "ВидПроизводства_Key": "",
        "Состав": [],
        "Операции": operations,
    }


def _operation_row(operation_ref):
    return {
        "Операция_Key": operation_ref,
        "НормаВремени": 1.0,
        "Этап_Key": "",
    }


def _seed_spec_with_operations(db):
    spec = Specification(spec_code="S-1", spec_name="Спека", spec_ref1c="spec-1")
    db.add(spec)
    db.flush()
    operations = {}
    for ref in ("op-kept", "op-referenced", "op-orphan"):
        operation = Operation(operation_ref1c=ref, time_norm=1.0)
        db.add(operation)
        db.flush()
        spec_operation = SpecOperation(
            spec_id=spec.spec_id,
            operation_id=operation.operation_id,
            time_norm=1.0,
        )
        db.add(spec_operation)
        db.flush()
        operations[ref] = (operation, spec_operation)
    db.commit()
    return spec, operations


def _seed_manufacture_reference(db, spec_operation, operation):
    item = Item(item_code="PROD-1", item_name="Изделие", item_ref1c="item-prod-1")
    db.add(item)
    db.flush()
    order = ProductionOrder(
        order_number="0001",
        order_date=datetime(2026, 7, 1),
        order_ref1c="order-1",
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        quantity=1,
        produced_qty=0,
        remaining_qty=1,
    )
    db.add(product)
    db.flush()
    manufacture = ProductionManufacture(
        product_id=product.product_id,
        order_id=order.order_id,
        qty=1,
    )
    db.add(manufacture)
    db.flush()
    db.add(ProductionManufactureOperation(
        manufacture_id=manufacture.manufacture_id,
        spec_operation_id=spec_operation.spec_operation_id,
        operation_id=operation.operation_id,
        line_number=1,
        employee_ref1c="emp-1",
        employee_name="Исполнитель",
    ))
    db.commit()


def test_reconcile_keeps_spec_operations_referenced_by_manufactures(db_session, monkeypatch):
    spec, operations = _seed_spec_with_operations(db_session)
    referenced_operation, referenced_spec_operation = operations["op-referenced"]
    _seed_manufacture_reference(db_session, referenced_spec_operation, referenced_operation)
    _patch_client(monkeypatch, [_spec_record([_operation_row("op-kept")])])

    result = specification_sync.sync_specifications_from_odata(db_session, _request())

    remaining = {
        row.operation_id
        for row in db_session.query(SpecOperation).filter(SpecOperation.spec_id == spec.spec_id)
    }
    assert remaining == {
        operations["op-kept"][0].operation_id,
        referenced_operation.operation_id,
    }
    assert result["spec_operations_deleted"] == 1


def test_reconcile_deletes_only_unreferenced_when_1c_has_no_operations(db_session, monkeypatch):
    spec, operations = _seed_spec_with_operations(db_session)
    referenced_operation, referenced_spec_operation = operations["op-referenced"]
    _seed_manufacture_reference(db_session, referenced_spec_operation, referenced_operation)
    _patch_client(monkeypatch, [_spec_record([])])

    result = specification_sync.sync_specifications_from_odata(db_session, _request())

    remaining = {
        row.operation_id
        for row in db_session.query(SpecOperation).filter(SpecOperation.spec_id == spec.spec_id)
    }
    assert remaining == {referenced_operation.operation_id}
    assert result["spec_operations_deleted"] == 2

"""Reconcile операций спецификации не должен удалять строки spec_operations,
на которые ссылаются операции изготовлений (production_manufacture_operations):
изготовление сохраняет производственную основу, а попытка удаления валит FK и
абортирует всю транзакцию синка (все последующие спецификации падали каскадом
PendingRollbackError — так синк спецификаций сломался на проде).
"""
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
        def __init__(self, *_a, **_k):
            pass

        def get_all(self, _entity_name, filter_query=None, select_fields=None):
            return records

    monkeypatch.setattr(odata_client_module, "OData1CClient", _Fake)


def _req():
    return ODataSyncRequest(base_url="http://demo/odata", entity_name="Catalog_Спецификации")


def _spec_record(operations):
    return {
        "Ref_Key": "spec-1",
        "Code": "S-1",
        "Description": "Спека",
        "ВидПроизводства_Key": "",
        "Состав": [],
        "Операции": operations,
    }


def _op_row(op_key, time_norm=1.0):
    return {"Операция_Key": op_key, "НормаВремени": time_norm, "Этап_Key": ""}


def _seed_spec_with_ops(db):
    spec = Specification(spec_code="S-1", spec_name="Спека", spec_ref1c="spec-1")
    db.add(spec)
    db.flush()

    ops = {}
    for ref in ("op-kept", "op-referenced", "op-orphan"):
        op = Operation(operation_ref1c=ref, time_norm=1.0)
        db.add(op)
        db.flush()
        spec_op = SpecOperation(spec_id=spec.spec_id, operation_id=op.operation_id, time_norm=1.0)
        db.add(spec_op)
        db.flush()
        ops[ref] = (op, spec_op)
    db.commit()
    return spec, ops


def _seed_manufacture_referencing(db, spec_op: SpecOperation, operation: Operation):
    item = Item(item_code="PROD-1", item_name="Изделие", item_ref1c="item-prod-1")
    db.add(item)
    db.flush()
    order = ProductionOrder(order_number="0001", order_date=datetime(2026, 7, 1), order_ref1c="order-1")
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
    manufacture = ProductionManufacture(product_id=product.product_id, order_id=order.order_id, qty=1)
    db.add(manufacture)
    db.flush()
    db.add(
        ProductionManufactureOperation(
            manufacture_id=manufacture.manufacture_id,
            spec_operation_id=spec_op.spec_operation_id,
            operation_id=operation.operation_id,
            line_number=1,
            employee_ref1c="emp-1",
            employee_name="Исполнитель",
        )
    )
    db.commit()


def test_reconcile_keeps_spec_operations_referenced_by_manufactures(db_session, monkeypatch):
    spec, ops = _seed_spec_with_ops(db_session)
    _seed_manufacture_referencing(db_session, ops["op-referenced"][1], ops["op-referenced"][0])

    # В 1С осталась только op-kept: op-referenced и op-orphan из спеки исчезли.
    _patch_client(monkeypatch, [_spec_record([_op_row("op-kept")])])

    result = specification_sync.sync_specifications_from_odata(db_session, _req())

    remaining = {
        row.operation_id
        for row in db_session.query(SpecOperation).filter(SpecOperation.spec_id == spec.spec_id)
    }
    kept_op = ops["op-kept"][0].operation_id
    referenced_op = ops["op-referenced"][0].operation_id
    # Осиротевшая строка удалена, строка с изготовлением — защищена.
    assert remaining == {kept_op, referenced_op}
    assert result["spec_operations_deleted"] == 1


def test_reconcile_deletes_all_unreferenced_when_1c_has_no_operations(db_session, monkeypatch):
    spec, ops = _seed_spec_with_ops(db_session)
    _seed_manufacture_referencing(db_session, ops["op-referenced"][1], ops["op-referenced"][0])

    _patch_client(monkeypatch, [_spec_record([])])

    result = specification_sync.sync_specifications_from_odata(db_session, _req())

    remaining = {
        row.operation_id
        for row in db_session.query(SpecOperation).filter(SpecOperation.spec_id == spec.spec_id)
    }
    assert remaining == {ops["op-referenced"][0].operation_id}
    assert result["spec_operations_deleted"] == 2

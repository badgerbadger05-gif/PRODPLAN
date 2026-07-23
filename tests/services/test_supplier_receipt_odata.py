from decimal import Decimal

from app import models
from app.services.item_ledger.supplier_receipt_allocation import (
    CORRECTION_OPERATION,
    RECEIPT_OPERATION,
    SUPPLIER_RETURN_OPERATION,
    TRANSFER_OPERATION,
    _validate_operations,
)
from app.services.item_ledger.supplier_receipt_odata import (
    extract_supplier_document_evidence,
)


ORDER_TYPE = "StandardODATA.Document_ЗаказПоставщику"


class _Client:
    def __init__(self, documents):
        self.documents = documents
        self.calls = []

    def _make_request(self, endpoint, params=None):
        self.calls.append((endpoint, params))
        return self.documents[endpoint]

    def get_all(self, entity_name, **kwargs):
        self.calls.append((entity_name, kwargs))
        document_type = entity_name.removesuffix("_Запасы")
        ref = kwargs["filter_query"].split("guid'", 1)[1].split("'", 1)[0]
        return self.documents[f"{document_type}(guid'{ref}')"]["Запасы"]


def _item(db, ref="item-ref"):
    row = models.Item(item_code=ref, item_name=ref, item_ref1c=ref)
    db.add(row)
    db.flush()
    return row


def _sle(
    item,
    *,
    row_id=1,
    doc_type="Document_ПриходнаяНакладная",
    ref="receipt-ref",
    line="1",
    qty="2",
    warehouse="wh-in",
):
    row = models.StockLedgerEntry(
        item_id=item.item_id,
        recorder_type=doc_type,
        recorder_ref=ref,
        line_no=line,
        qty=Decimal(qty),
        characteristic_ref="",
        warehouse_ref1c=warehouse,
    )
    row.id = row_id
    return row


def _doc(
    ref,
    operation,
    operation_name,
    *,
    qty="2",
    warehouse="wh-in",
    line="1",
):
    return {
        "Ref_Key": ref,
        "ХозяйственнаяОперация_Key": operation + "-9934-11eb-e39a-fa163e61326a",
        "ВидОперации": operation_name,
        "Заказ": "order-ref",
        "Заказ_Type": ORDER_TYPE,
        "СтруктурнаяЕдиница_Key": warehouse,
        "Запасы": [{
            "LineNumber": line,
            "Номенклатура_Key": "item-ref",
            "Характеристика_Key": "00000000-0000-0000-0000-000000000000",
            "Количество": qty,
        }],
    }


def test_exact_receipt_fetch_is_deduplicated_and_preserves_basis_line_zero(db_session):
    item = _item(db_session)
    entries = [
        _sle(item, row_id=2),
        _sle(item, row_id=1),
    ]
    doc = _doc(
        "receipt-ref", RECEIPT_OPERATION, "Приобретение у поставщика",
        qty="4",
    )
    client = _Client({
        "Document_ПриходнаяНакладная(guid'receipt-ref')": doc
    })

    result = extract_supplier_document_evidence(db_session, client, entries)

    assert len(client.calls) == 2
    assert client.calls == [
        ("Document_ПриходнаяНакладная(guid'receipt-ref')", None),
        (
            "Document_ПриходнаяНакладная_Запасы",
            {
                "filter_query": "Ref_Key eq guid'receipt-ref'",
                "top": 1000,
                "max_pages": 100,
                "order_by": "LineNumber",
            },
        ),
    ]
    assert result.fetched_document_count == 1
    assert [row.supplier_order_line_no for row in result.evidence] == ["0"]
    assert result.evidence[0].signed_qty == Decimal("4")
    assert result.diagnostics == ()


def test_correction_receipt_minus_one_requires_typed_original_receipt(db_session):
    item = _item(db_session)
    entry = _sle(
        item,
        doc_type="Document_КорректировкаПоступления",
        ref="correction-ref",
        qty="-1",
    )
    doc = _doc(
        "correction-ref",
        CORRECTION_OPERATION,
        "Корректировка поступления",
        qty="-1",
    )
    doc["Запасы"][0].update({
        "Количество": "29",
        "КоличествоДоИзменения": "30",
        "КоличествоПослеИзменения": "29",
    })
    doc.update({
        "ИсправляемыйДокументПоступления": "original-receipt",
        "ИсправляемыйДокументПоступления_Type":
            "StandardODATA.Document_ПриходнаяНакладная",
    })

    result = extract_supplier_document_evidence(
        db_session,
        _Client({
            "Document_КорректировкаПоступления(guid'correction-ref')": doc
        }),
        [entry],
    )

    assert result.evidence[0].signed_qty == Decimal("-1")
    assert result.evidence[0].correction_receipt_ref == "original-receipt"


def test_supplier_return_expense_normalizes_positive_document_qty_negative(db_session):
    item = _item(db_session)
    entry = _sle(
        item,
        doc_type="Document_РасходнаяНакладная",
        ref="return-ref",
        qty="-2",
    )
    doc = _doc(
        "return-ref", SUPPLIER_RETURN_OPERATION, "Возврат поставщику"
    )

    result = extract_supplier_document_evidence(
        db_session,
        _Client({"Document_РасходнаяНакладная(guid'return-ref')": doc}),
        [entry],
    )

    assert result.evidence[0].signed_qty == Decimal("-2")


def test_transfer_builds_balanced_pair_from_one_document_line(db_session):
    item = _item(db_session)
    entries = [
        _sle(
            item, row_id=1, doc_type="Document_ПеремещениеЗапасов",
            ref="transfer-ref", qty="-2", warehouse="wh-from",
        ),
        _sle(
            item, row_id=2, doc_type="Document_ПеремещениеЗапасов",
            ref="transfer-ref", qty="2", warehouse="wh-to",
        ),
    ]
    doc = _doc(
        "transfer-ref", TRANSFER_OPERATION, "Перемещение запасов"
    )
    doc.pop("СтруктурнаяЕдиница_Key")
    doc["СкладОтправитель_Key"] = "wh-from"
    doc["СкладПолучатель_Key"] = "wh-to"
    doc.pop("Заказ")
    doc.pop("Заказ_Type")

    result = extract_supplier_document_evidence(
        db_session,
        _Client({"Document_ПеремещениеЗапасов(guid'transfer-ref')": doc}),
        entries,
    )

    assert [row.signed_qty for row in result.evidence] == [
        Decimal("-2"), Decimal("2")
    ]
    assert all(not row.supplier_order_ref for row in result.evidence)
    _validate_operations(result.evidence)
    assert result.diagnostics == ()


def test_live_basis_line_field_precedes_legacy_aliases(db_session):
    item = _item(db_session)
    doc = _doc(
        "receipt-ref", RECEIPT_OPERATION, "Приобретение у поставщика"
    )
    doc["Запасы"][0].update({
        "НомерСтрокиДокументаОснования": 7,
        "СтрокаЗаказа": 99,
    })
    result = extract_supplier_document_evidence(
        db_session,
        _Client({
            "Document_ПриходнаяНакладная(guid'receipt-ref')": doc
        }),
        [_sle(item)],
    )
    assert result.evidence[0].supplier_order_line_no == "7"


def test_correction_without_verifiable_delta_fails_closed(db_session):
    item = _item(db_session)
    doc = _doc(
        "correction-ref",
        CORRECTION_OPERATION,
        "Корректировка поступления",
        qty="-1",
    )
    doc.update({
        "ДокументПоступления_Key": "original-receipt",
        "ДокументПоступления_Type":
            "StandardODATA.Document_ПриходнаяНакладная",
    })
    result = extract_supplier_document_evidence(
        db_session,
        _Client({
            "Document_КорректировкаПоступления(guid'correction-ref')": doc
        }),
        [_sle(
            item,
            doc_type="Document_КорректировкаПоступления",
            ref="correction-ref",
            qty="-1",
        )],
    )
    assert result.evidence == ()
    assert result.diagnostics[0].code == "correction_delta_unverified"


def test_missing_mismatch_and_duplicate_lines_fail_closed(db_session):
    item = _item(db_session)
    base = _doc(
        "receipt-ref", RECEIPT_OPERATION, "Приобретение у поставщика"
    )
    missing = {**base, "Запасы": []}
    mismatch = {**base, "Запасы": [{**base["Запасы"][0], "Количество": "3"}]}
    duplicate = {**base, "Запасы": base["Запасы"] * 2}
    entry = _sle(item)

    codes = []
    for document in (missing, mismatch, duplicate):
        result = extract_supplier_document_evidence(
            db_session,
            _Client({
                "Document_ПриходнаяНакладная(guid'receipt-ref')": document
            }),
            [entry],
        )
        assert result.evidence == ()
        codes.append(result.diagnostics[0].code)

    assert codes == [
        "missing_document_line", "quantity_mismatch", "duplicate_document_line"
    ]


def test_unknown_type_wrong_operation_block_but_missing_order_is_unplanned_evidence(
    db_session,
):
    item = _item(db_session)
    unknown = extract_supplier_document_evidence(
        db_session, _Client({}),
        [_sle(item, doc_type="Document_Неизвестный")],
    )
    assert unknown.diagnostics[0].code == "unsupported_document_type"

    wrong_doc = _doc(
        "receipt-ref", TRANSFER_OPERATION, "Перемещение запасов"
    )
    wrong = extract_supplier_document_evidence(
        db_session,
        _Client({
            "Document_ПриходнаяНакладная(guid'receipt-ref')": wrong_doc
        }),
        [_sle(item)],
    )
    assert wrong.diagnostics[0].code == "wrong_operation"

    no_order_doc = _doc(
        "receipt-ref", RECEIPT_OPERATION, "Приобретение у поставщика"
    )
    no_order_doc.pop("Заказ")
    no_order = extract_supplier_document_evidence(
        db_session,
        _Client({
            "Document_ПриходнаяНакладная(guid'receipt-ref')": no_order_doc
        }),
        [_sle(item)],
    )
    assert no_order.diagnostics == ()
    assert len(no_order.evidence) == 1
    assert no_order.evidence[0].supplier_order_ref == ""
    assert no_order.evidence[0].supplier_order_type == ""
    assert no_order.evidence[0].supplier_order_line_no == "0"

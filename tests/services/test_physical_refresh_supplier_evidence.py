from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger import physical_refresh_supplier_evidence as adapter
from app.services.item_ledger.supplier_receipt_allocation import (
    CORRECTION_OPERATION,
    RECEIPT_OPERATION,
    SUPPLIER_RETURN_OPERATION,
    SupplierDocumentEvidence,
)
from app.services.item_ledger.supplier_receipt_odata import (
    SupplierEvidenceDiagnostic,
    SupplierEvidenceExtractionResult,
)


def _world(db_session):
    parent_cutoff = datetime(2026, 9, 10, tzinfo=timezone.utc)
    target_cutoff = datetime(2026, 9, 11, tzinfo=timezone.utc)
    parent_batch = models.PhysicalImportBatch(
        batch_key="supplier-evidence-parent",
        status="completed",
        cutoff=parent_cutoff,
        completed_at=parent_cutoff,
        source_watermarks={},
        source_complete=True,
    )
    target_batch = models.PhysicalImportBatch(
        batch_key="supplier-evidence-target",
        status="completed",
        cutoff=target_cutoff,
        completed_at=target_cutoff,
        source_watermarks={},
        source_complete=True,
    )
    db_session.add_all([parent_batch, target_batch])
    db_session.flush()
    parent = models.LedgerGeneration(
        generation_key="supplier-evidence-parent",
        status="accepted",
        cutoff=parent_cutoff,
        accepted_at=parent_cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=parent_batch,
        algorithm_version="supplier-evidence-tests",
    )
    target = models.LedgerGeneration(
        generation_key="supplier-evidence-target",
        status="building",
        cutoff=target_cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=target_batch,
        algorithm_version="supplier-evidence-tests",
    )
    item = models.Item(
        item_code="SUPPLIER-EVIDENCE-1",
        item_name="supplier evidence",
        item_ref1c="item-ref-1",
    )
    db_session.add_all([parent, target, item])
    db_session.flush()
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    db_session.commit()
    return parent, target, parent_batch, target_batch, item


def _sle(
    db_session,
    batch,
    item,
    *,
    ref="receipt-1",
    qty="4",
    posting=None,
    recorder_type=None,
    content_hash=None,
    business_identity=None,
):
    posting = posting or batch.cutoff
    row = models.StockLedgerEntry(
        ingest_batch_id=batch.id,
        source_content_hash=(content_hash or f"supplier-{ref}").ljust(64, "0"),
        business_identity=business_identity or f"supplier:{ref}",
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="wh-ref-1",
        qty=Decimal(qty),
        qty_after=Decimal(qty),
        posting_at=posting,
        known_at=batch.cutoff,
        record_type="Receipt" if Decimal(qty) >= 0 else "Expense",
        movement_kind="supplier_receipt",
        recorder_type=recorder_type or "Document_ПриходнаяНакладная",
        recorder_ref=ref,
        line_no="1",
        ingest_source="pull",
        active=True,
    )
    db_session.add(row)
    db_session.flush()
    return row


class _ReceiptClient:
    def __init__(self, *, ref="receipt-1", operation_key=RECEIPT_OPERATION, operation_name="Приобретение у поставщика"):
        self.ref = ref
        self.operation_key = operation_key
        self.operation_name = operation_name
        self.calls = []

    def _make_request(self, endpoint):
        self.calls.append(("document", endpoint))
        return {
            "Ref_Key": self.ref,
            "ВидОперации_Key": self.operation_key,
            "ВидОперации": self.operation_name,
        }

    def get_all(self, entity_name, *, filter_query=None, **_kwargs):
        self.calls.append((entity_name, filter_query))
        return [{
            "LineNumber": "1",
            "Номенклатура_Key": "item-ref-1",
            "СтруктурнаяЕдиница_Key": "wh-ref-1",
            "Характеристика_Key": "",
            "Количество": "4",
            "Заказ_Key": "",
            "Заказ_Type": "",
        }]


def _fake_evidence(
    row,
    *,
    operation_key=RECEIPT_OPERATION,
    qty=None,
    order_ref="",
    order_type="",
):
    return SupplierDocumentEvidence(
        receipt_doc_type=row.recorder_type,
        receipt_doc_ref=row.recorder_ref,
        receipt_doc_line_no=row.line_no,
        operation_key=operation_key,
        operation_name={
            RECEIPT_OPERATION: "Приобретение у поставщика",
            CORRECTION_OPERATION: "Корректировка поступления",
            SUPPLIER_RETURN_OPERATION: "Возврат поставщику",
        }[operation_key],
        supplier_order_type=order_type,
        supplier_order_ref=order_ref,
        supplier_order_line_no="0",
        item_id=row.item_id,
        characteristic_ref="",
        warehouse_ref1c=row.warehouse_ref1c,
        signed_qty=Decimal(qty if qty is not None else row.qty),
    )


def _fake_result(evidence=(), diagnostics=()):
    return SupplierEvidenceExtractionResult(
        evidence=tuple(evidence),
        diagnostics=tuple(diagnostics),
        fetched_document_count=len(tuple(evidence)),
    )


def _supplier_order(db_session, item, *, line_numbers=(1,)):
    order = models.SupplierOrder(
        order_number="order-1",
        order_date=datetime(2026, 9, 1),
        order_ref1c="order-1",
    )
    db_session.add(order)
    db_session.flush()
    db_session.add_all([
        models.SupplierOrderItem(
            order_id=order.order_id,
            item_id_ref=item.item_id,
            line_number=line_number,
            quantity=Decimal("4"),
            received_qty=Decimal("0"),
            remaining_qty=Decimal("4"),
        )
        for line_number in line_numbers
    ])
    db_session.flush()


def _build(db_session, parent, target, item, batch, *, monkeypatch, row, evidence=None, result=None):
    if result is None:
        result = _fake_result((evidence or _fake_evidence(row),))
    seen = []

    def extract(db, client, rows):
        seen.extend(rows)
        return result

    monkeypatch.setattr(adapter, "extract_supplier_document_evidence", extract)
    manifest = adapter.build_bounded_supplier_receipt_manifest(
        db_session,
        parent_generation_id=parent.id,
        target_generation_id=target.id,
        target_cutoff=target.cutoff,
        odata_client=object(),
        changed_sle_ids=(row.id,),
        affected_scopes=((item.item_id, "", "", "default", "buy"),),
    )
    return manifest, seen


def test_forward_manifest_uses_only_explicit_changed_sles_and_canonical_odata_matcher(db_session):
    parent, target, parent_batch, target_batch, item = _world(db_session)
    historical = _sle(db_session, parent_batch, item, ref="historical")
    changed = _sle(db_session, target_batch, item)
    client = _ReceiptClient()
    manifest = adapter.build_bounded_supplier_receipt_manifest(
        db_session,
        parent_generation_id=parent.id,
        target_generation_id=target.id,
        target_cutoff=target.cutoff,
        odata_client=client,
        changed_sle_ids=(changed.id,),
        affected_scopes=((item.item_id, "", "", "default", "buy"),),
    )
    assert manifest.new_sle_ids == (changed.id,)
    assert [fact.sle_id for fact in manifest.receipt_facts] == [changed.id]
    assert manifest.receipt_facts[0].signed_qty == Decimal("4")
    assert historical.id not in manifest.new_sle_ids
    assert [call[1] for call in client.calls if call[0] == "document"] == [
        "Document_ПриходнаяНакладная(guid'receipt-1')"
    ]
    assert db_session.query(models.StockLedgerSupplierReceiptProvenance).count() == 0


def test_empty_delta_is_a_read_only_noop_without_odata_client(db_session):
    parent, target, _parent_batch, _target_batch, item = _world(db_session)
    manifest = adapter.build_bounded_supplier_receipt_manifest(
        db_session,
        parent_generation_id=parent.id,
        target_generation_id=target.id,
        target_cutoff=target.cutoff,
        odata_client=None,
        changed_sle_ids=(),
        affected_scopes=((item.item_id, "", "", "default", "buy"),),
    )
    assert manifest == adapter.BoundedBuyReceiptDeltaManifest()


def test_direct_unmatched_supplier_receipt_is_valid(db_session, monkeypatch):
    parent, target, _parent_batch, target_batch, item = _world(db_session)
    row = _sle(db_session, target_batch, item)
    manifest, _seen = _build(
        db_session, parent, target, item, target_batch, monkeypatch=monkeypatch,
        row=row, evidence=_fake_evidence(row, order_ref=""),
    )
    assert manifest.receipt_facts[0].supplier_order_ref == ""
    assert manifest.receipt_facts[0].supplier_order_line_no == "0"


def test_order_line_zero_uses_unique_canonical_supplier_order_line(db_session, monkeypatch):
    parent, target, _parent_batch, target_batch, item = _world(db_session)
    _supplier_order(db_session, item, line_numbers=(1,))
    row = _sle(db_session, target_batch, item)
    manifest, _seen = _build(
        db_session, parent, target, item, target_batch, monkeypatch=monkeypatch,
        row=row, evidence=_fake_evidence(
            row,
            order_ref="order-1",
            order_type="Document_ЗаказПоставщику",
        ),
    )
    assert manifest.receipt_facts[0].supplier_order_ref == "order-1"
    assert manifest.receipt_facts[0].supplier_order_line_no == "1"


def test_order_line_zero_with_multiple_canonical_lines_is_ambiguous(db_session, monkeypatch):
    parent, target, _parent_batch, target_batch, item = _world(db_session)
    _supplier_order(db_session, item, line_numbers=(1, 2))
    row = _sle(db_session, target_batch, item)
    with pytest.raises(adapter.BoundedSupplierEvidenceError, match="ambiguous"):
        _build(
            db_session, parent, target, item, target_batch, monkeypatch=monkeypatch,
            row=row, evidence=_fake_evidence(
                row,
                order_ref="order-1",
                order_type="Document_ЗаказПоставщику",
            ),
        )


def test_one_document_evidence_maps_to_two_aggregate_delta_sles(db_session, monkeypatch):
    parent, target, _parent_batch, target_batch, item = _world(db_session)
    first = _sle(db_session, target_batch, item, qty="2")
    second = _sle(
        db_session,
        target_batch,
        item,
        qty="2",
        content_hash="aggregate-2",
        business_identity="supplier:receipt-1:aggregate-2",
    )
    result = _fake_result((_fake_evidence(first, qty="4"),))

    def extract(_db, _client, rows):
        assert {int(row.id) for row in rows} == {int(first.id), int(second.id)}
        return result

    monkeypatch.setattr(adapter, "extract_supplier_document_evidence", extract)
    manifest = adapter.build_bounded_supplier_receipt_manifest(
        db_session,
        parent_generation_id=parent.id,
        target_generation_id=target.id,
        target_cutoff=target.cutoff,
        odata_client=object(),
        changed_sle_ids=(first.id, second.id),
        affected_scopes=((item.item_id, "", "", "default", "buy"),),
    )
    assert manifest.new_sle_ids == tuple(sorted((first.id, second.id)))
    assert sum((fact.signed_qty for fact in manifest.receipt_facts), Decimal("0")) == Decimal("4")


def test_diagnostics_are_fail_closed_and_do_not_write_provenance(db_session, monkeypatch):
    parent, target, _parent_batch, target_batch, item = _world(db_session)
    row = _sle(db_session, target_batch, item)
    result = _fake_result(diagnostics=(SupplierEvidenceDiagnostic(
        recorder_type=row.recorder_type,
        recorder_ref=row.recorder_ref,
        line_no=row.line_no,
        code="item_mismatch",
        detail="document item differs",
    ),))
    with pytest.raises(adapter.BoundedSupplierEvidenceError, match="item_mismatch"):
        _build(
            db_session, parent, target, item, target_batch, monkeypatch=monkeypatch,
            row=row, result=result,
        )
    assert db_session.query(models.StockLedgerSupplierReceiptProvenance).count() == 0


@pytest.mark.parametrize(
    ("operation", "qty"),
    [(CORRECTION_OPERATION, "4"), (SUPPLIER_RETURN_OPERATION, "-2")],
)
def test_correction_and_return_require_complete_affected_scope_evidence(
    db_session, monkeypatch, operation, qty,
):
    parent, target, _parent_batch, target_batch, item = _world(db_session)
    row = _sle(db_session, target_batch, item, qty=qty)
    with pytest.raises(adapter.BoundedSupplierEvidenceError, match="complete affected-scope evidence required"):
        _build(
            db_session, parent, target, item, target_batch, monkeypatch=monkeypatch,
            row=row, evidence=_fake_evidence(row, operation_key=operation, qty=qty),
        )


def test_backdate_and_stale_parent_fail_closed(db_session, monkeypatch):
    parent, target, parent_batch, target_batch, item = _world(db_session)
    backdated = _sle(
        db_session, target_batch, item,
        posting=parent_batch.cutoff - timedelta(hours=1),
    )
    with pytest.raises(adapter.BoundedSupplierEvidenceError, match="complete affected-scope evidence required"):
        _build(
            db_session, parent, target, item, target_batch, monkeypatch=monkeypatch,
            row=backdated,
        )
    pointer = db_session.get(models.PlanningTruthState, 1)
    pointer.current_generation_id = None
    db_session.flush()
    forward = _sle(db_session, target_batch, item, ref="forward-2")
    with pytest.raises(adapter.BoundedSupplierEvidenceError, match="not current truth"):
        _build(
            db_session, parent, target, item, target_batch, monkeypatch=monkeypatch,
            row=forward,
        )


def test_validator_rejects_non_forward_manifest_and_never_scans_visible_history(
    db_session, monkeypatch,
):
    parent, target, _parent_batch, target_batch, item = _world(db_session)
    row = _sle(db_session, target_batch, item)
    fact = _fake_evidence(row)
    from app.services.item_ledger import physical_visibility

    monkeypatch.setattr(
        physical_visibility,
        "visible_sles_for_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("supplier evidence adapter must not scan visible history")
        ),
    )
    manifest, _seen = _build(
        db_session, parent, target, item, target_batch, monkeypatch=monkeypatch,
        row=row, evidence=_fake_evidence(row),
    )
    validated = adapter.validate_bounded_supplier_receipt_manifest(
        db_session,
        parent_generation_id=parent.id,
        target_generation_id=target.id,
        target_cutoff=target.cutoff,
        affected_scopes=((item.item_id, "", "", "default", "buy"),),
        manifest=manifest,
    )
    assert validated == manifest

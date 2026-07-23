import datetime
from decimal import Decimal

import pytest

from app.services.item_ledger import supplier_receipt_allocation as receipt_service
from app.services.item_ledger.supplier_receipt_allocation import (
    CORRECTION_OPERATION,
    RECEIPT_OPERATION,
    SUPPLIER_RETURN_OPERATION,
    TRANSFER_OPERATION,
    CoveragePin,
    ReceiptFact,
    SupplierDocumentEvidence,
    SupplierReceiptEvidenceError,
    _validate_operations,
    allocate_supplier_receipts,
    rebuild_supplier_receipt_coverage,
)
from app import models


def _fact(
    sle_id,
    qty,
    *,
    ref=None,
    line="1",
    at=None,
    correction=None,
    order="order-1",
):
    return ReceiptFact(
        sle_id=sle_id,
        posting_at=at or datetime.datetime(2026, 7, 1, 12) + datetime.timedelta(minutes=sle_id),
        signed_qty=Decimal(str(qty)),
        supplier_order_ref=order,
        supplier_order_line_no=line,
        receipt_ref=ref or f"receipt-{sle_id}",
        receipt_line_no="1",
        correction_receipt_ref=correction,
    )


def _pin(pin_id, req_id, qty, *, due=1):
    return CoveragePin(
        freeze_allocation_id=pin_id,
        requirement_id=req_id,
        bucket_id=None,
        qty=Decimal(str(qty)),
        due_at=datetime.datetime(2026, 7, due),
    )


def _evidence(operation, qty, *, ref="doc", correction=None):
    operation_names = {
        RECEIPT_OPERATION: "Приобретение у поставщика",
        CORRECTION_OPERATION: "Корректировка приобретения",
        SUPPLIER_RETURN_OPERATION: "Возврат поставщику",
        TRANSFER_OPERATION: "Перемещение товаров",
    }
    return SupplierDocumentEvidence(
        receipt_doc_type="Document_Receipt",
        receipt_doc_ref=ref,
        receipt_doc_line_no="1",
        operation_key=operation,
        operation_name=operation_names.get(operation[:8], "test"),
        supplier_order_type="StandardODATA.Document_ЗаказПоставщику",
        supplier_order_ref="order-1",
        supplier_order_line_no="1",
        item_id=1,
        characteristic_ref="",
        warehouse_ref1c="wh",
        signed_qty=Decimal(str(qty)),
        correction_receipt_ref=correction,
    )


def test_partial_receipt_fills_two_frozen_pins_fifo_and_caps_fact():
    allocations, unplanned = allocate_supplier_receipts(
        [_fact(1, 7)],
        {("order-1", "1"): [_pin(1, 10, 5), _pin(2, 20, 5, due=2)]},
    )
    assert [(row.pin.requirement_id, row.qty) for row in allocations] == [
        (10, Decimal("5")),
        (20, Decimal("2")),
    ]
    assert unplanned == 0


def test_overreceipt_is_unplanned_and_does_not_overfill_pin():
    allocations, unplanned = allocate_supplier_receipts(
        [_fact(1, 9)], {("order-1", "1"): [_pin(1, 10, 4)]}
    )
    assert [row.qty for row in allocations] == [Decimal("4")]
    assert unplanned == Decimal("5")


def test_correction_unwinds_only_named_receipt_not_other_order_lot():
    allocations, unplanned = allocate_supplier_receipts(
        [
            _fact(1, 4, ref="original"),
            _fact(2, 3, ref="other"),
            _fact(3, -3, ref="correction", correction="original"),
        ],
        {("order-1", "1"): [_pin(1, 10, 7)]},
    )
    assert [row.qty for row in allocations] == [
        Decimal("4"), Decimal("3"), Decimal("-3")
    ]
    assert allocations[-1].pin.requirement_id == 10
    assert unplanned == 0


def test_supplier_return_unwinds_latest_receipts_only_in_same_order_lineage():
    allocations, unplanned = allocate_supplier_receipts(
        [
            _fact(1, 3),
            _fact(2, 2, order="order-2"),
            _fact(3, -2),
        ],
        {
            ("order-1", "1"): [_pin(1, 10, 3)],
            ("order-2", "1"): [_pin(2, 20, 2)],
        },
    )
    assert [(row.pin.requirement_id, row.qty) for row in allocations] == [
        (10, Decimal("3")),
        (20, Decimal("2")),
        (10, Decimal("-2")),
    ]
    assert unplanned == 0


def test_repeated_allocation_is_pure_and_idempotent():
    args = (
        [_fact(1, 2)],
        {("order-1", "1"): [_pin(1, 10, 2)]},
    )
    assert allocate_supplier_receipts(*args) == allocate_supplier_receipts(*args)


def test_unknown_operation_fails_closed_and_correction_requires_origin():
    with pytest.raises(SupplierReceiptEvidenceError, match="unsupported"):
        _validate_operations((_evidence("unknown", 1),))
    with pytest.raises(SupplierReceiptEvidenceError, match="original"):
        _validate_operations((_evidence(CORRECTION_OPERATION, -1),))


def test_transfer_is_excluded_only_as_balanced_pair():
    with pytest.raises(SupplierReceiptEvidenceError, match="balanced pair"):
        _validate_operations((_evidence(TRANSFER_OPERATION, 1),))
    first = _evidence(TRANSFER_OPERATION, 1)
    second = _evidence(TRANSFER_OPERATION, -1)
    second = SupplierDocumentEvidence(
        **{**second.__dict__, "warehouse_ref1c": "wh-2"}
    )
    _validate_operations((first, second))


def test_supported_signed_operations_are_accepted():
    _validate_operations((
        _evidence(RECEIPT_OPERATION, 1),
        _evidence(CORRECTION_OPERATION, -1, correction="receipt"),
        _evidence(SUPPLIER_RETURN_OPERATION, -1),
    ))


def test_full_uuid_operation_key_matches_only_with_exact_normalized_name():
    _validate_operations((
        _evidence(f"{RECEIPT_OPERATION}-aaaa-bbbb-cccc-dddddddddddd", 1),
    ))
    bad = _evidence(f"{RECEIPT_OPERATION}-aaaa-bbbb-cccc-dddddddddddd", 1)
    bad = SupplierDocumentEvidence(**{**bad.__dict__, "operation_name": "Другое"})
    with pytest.raises(SupplierReceiptEvidenceError, match="unsupported"):
        _validate_operations((bad,))


@pytest.mark.parametrize(
    ("operation", "operation_name", "correction"),
    [
        (RECEIPT_OPERATION, "ПоступлениеОтПоставщика", None),
        (
            CORRECTION_OPERATION,
            "Корректировка по согласованию сторон",
            "receipt",
        ),
        (SUPPLIER_RETURN_OPERATION, "ВозвратПоставщику", None),
    ],
)
def test_live_and_predefined_compact_operation_names(
    operation, operation_name, correction
):
    row = _evidence(operation, -1 if operation != RECEIPT_OPERATION else 1,
                    correction=correction)
    row = SupplierDocumentEvidence(
        **{**row.__dict__, "operation_name": operation_name}
    )
    _validate_operations((row,))


def _persistence_fixture(db, *, duplicate=False, legacy_received=999):
    item = models.Item(item_code="SUP-CORE", item_name="Supplier core")
    batch = models.PhysicalImportBatch(
        batch_key="supplier-core-batch", status="completed", source_watermarks={}
    )
    generation = models.LedgerGeneration(
        generation_key="supplier-core-generation",
        status="building",
        source_watermarks={},
        capabilities={},
        physical_import_batch=batch,
        algorithm_version="test/1",
    )
    other_generation = models.LedgerGeneration(
        generation_key="supplier-core-other",
        status="building",
        source_watermarks={},
        capabilities={},
        physical_import_batch=batch,
        algorithm_version="test/1",
    )
    db.add_all([item, generation, other_generation])
    db.flush()
    order = models.SupplierOrder(
        order_number="1",
        order_date=datetime.datetime(2026, 6, 1),
        order_ref1c="order-1",
    )
    db.add(order)
    db.flush()
    db.add(models.SupplierOrderItem(
        order_id=order.order_id,
        item_id_ref=item.item_id,
        line_number=1,
        characteristic_ref1c=None,
        quantity=Decimal("5"),
        received_qty=Decimal(str(legacy_received)),
        remaining_qty=Decimal("-994"),
    ))
    if duplicate:
        db.add(models.SupplierOrderItem(
            order_id=order.order_id,
            item_id_ref=item.item_id,
            line_number=1,
            characteristic_ref1c=None,
            quantity=Decimal("5"),
            received_qty=0,
            remaining_qty=5,
        ))
    run = models.PlanningRun(
        config_snapshot={},
        active_freeze_version=1,
        ledger_generation_id=generation.id,
    )
    other_run = models.PlanningRun(
        config_snapshot={},
        active_freeze_version=1,
        ledger_generation_id=other_generation.id,
    )
    db.add_all([run, other_run])
    db.flush()
    req = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        period_from=datetime.date(2026, 7, 1),
        period_to=datetime.date(2026, 7, 31),
    )
    other_req = models.MrpRequirement(
        run_id=other_run.run_id,
        item_id=item.item_id,
        period_from=datetime.date(2026, 8, 1),
        period_to=datetime.date(2026, 8, 31),
    )
    db.add_all([req, other_req])
    db.flush()
    db.add_all([
        models.MrpFreezeAllocation(
            run_id=run.run_id,
            freeze_version=1,
            requirement_id=req.id,
            item_id=item.item_id,
            source_type="supplier_order",
            source_ref="order-1",
            source_line_ref="1",
            alloc_qty=Decimal("5"),
        ),
        models.MrpFreezeAllocation(
            run_id=other_run.run_id,
            freeze_version=1,
            requirement_id=other_req.id,
            item_id=item.item_id,
            source_type="supplier_order",
            source_ref="order-1",
            source_line_ref="1",
            alloc_qty=Decimal("100"),
        ),
    ])
    sle = models.StockLedgerEntry(
        ingest_batch_id=batch.id,
        source_content_hash="d" * 64,
        item_id=item.item_id,
        characteristic_ref="",
        warehouse_ref1c="wh",
        qty=Decimal("3"),
        posting_at=datetime.datetime(2026, 7, 2),
        record_type="Receipt",
        movement_kind="receipt",
        recorder_type="Document_Receipt",
        recorder_ref="doc",
        line_no="1",
    )
    db.add(sle)
    db.commit()
    return generation, req


def test_persistence_ignores_legacy_received_qty_and_cross_generation_pin(db_session):
    generation, req = _persistence_fixture(db_session, legacy_received=999)
    result = rebuild_supplier_receipt_coverage(
        db_session,
        ledger_generation_id=generation.id,
        evidence=[_evidence(RECEIPT_OPERATION, 3)],
        cycle_id="test",
    )
    db_session.commit()
    assert result.exact_fact_count == 1
    rows = db_session.query(models.MrpExecutionAllocation).all()
    assert [(row.requirement_id, row.allocated_qty) for row in rows] == [
        (req.id, Decimal("3.000"))
    ]
    provenance = db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).one()
    assert provenance.operation_kind == "supplier_receipt"
    assert provenance.operation_key == RECEIPT_OPERATION
    assert provenance.operation_name == "Приобретение у поставщика"
    assert provenance.correction_receipt_ref is None
    assert len(provenance.evidence_hash) == 64
    assert provenance.evidence_payload["signed_qty"] == "3"


def test_live_basis_line_zero_resolves_unique_canonical_order_line(db_session):
    generation, req = _persistence_fixture(db_session)
    evidence = _evidence(RECEIPT_OPERATION, 3)
    evidence = SupplierDocumentEvidence(
        **{**evidence.__dict__, "supplier_order_line_no": "0"}
    )
    rebuild_supplier_receipt_coverage(
        db_session,
        ledger_generation_id=generation.id,
        evidence=[evidence],
        cycle_id="test",
    )
    db_session.commit()
    provenance = db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).one()
    assert provenance.match_status == "exact"
    assert provenance.supplier_order_line_no == "1"
    assert db_session.query(models.MrpExecutionAllocation).one().requirement_id == req.id


def test_duplicate_order_lines_persist_ambiguity_without_allocation(db_session):
    generation, _req = _persistence_fixture(db_session, duplicate=True)
    result = rebuild_supplier_receipt_coverage(
        db_session,
        ledger_generation_id=generation.id,
        evidence=[_evidence(RECEIPT_OPERATION, 3)],
        cycle_id="test",
    )
    db_session.commit()
    assert result.exact_fact_count == 0
    provenance = db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).one()
    assert provenance.match_status == "ambiguous"
    assert provenance.ambiguity_count == 2
    assert db_session.query(models.MrpExecutionAllocation).count() == 0


def test_persistence_rerun_is_idempotent(db_session):
    generation, _req = _persistence_fixture(db_session)
    kwargs = dict(
        ledger_generation_id=generation.id,
        evidence=[_evidence(RECEIPT_OPERATION, 3)],
        cycle_id="test",
    )
    rebuild_supplier_receipt_coverage(db_session, **kwargs)
    db_session.commit()
    rebuild_supplier_receipt_coverage(db_session, **kwargs)
    db_session.commit()
    assert db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).count() == 1
    assert db_session.query(models.MrpExecutionAllocation).count() == 1


def test_accepted_generation_rejects_mutation(db_session):
    generation, _req = _persistence_fixture(db_session)
    generation.status = "accepted"
    db_session.commit()
    with pytest.raises(SupplierReceiptEvidenceError, match="immutable"):
        rebuild_supplier_receipt_coverage(
            db_session,
            ledger_generation_id=generation.id,
            evidence=[_evidence(RECEIPT_OPERATION, 3)],
            cycle_id="test",
        )


def test_correction_of_multi_sle_receipt_aggregates_negative_pin_row(db_session):
    generation, _req = _persistence_fixture(db_session)
    first = db_session.query(models.StockLedgerEntry).filter_by(
        recorder_ref="doc"
    ).one()
    db_session.add_all([
        models.StockLedgerEntry(
            ingest_batch_id=first.ingest_batch_id,
            source_content_hash="e" * 64,
            item_id=first.item_id,
            characteristic_ref="",
            warehouse_ref1c="wh",
            qty=Decimal("2"),
            posting_at=datetime.datetime(2026, 7, 2, 0, 1),
            record_type="Receipt",
            movement_kind="receipt",
            recorder_type="Document_Receipt",
            recorder_ref="doc",
            line_no="1",
        ),
        models.StockLedgerEntry(
            ingest_batch_id=first.ingest_batch_id,
            source_content_hash="f" * 64,
            item_id=first.item_id,
            characteristic_ref="",
            warehouse_ref1c="wh",
            qty=Decimal("-4"),
            posting_at=datetime.datetime(2026, 7, 3),
            record_type="Expense",
            movement_kind="expense",
            recorder_type="Document_Receipt",
            recorder_ref="correction",
            line_no="1",
        ),
    ])
    db_session.commit()
    positive = _evidence(RECEIPT_OPERATION, 5)
    correction = _evidence(
        CORRECTION_OPERATION, -4, ref="correction", correction="doc"
    )
    result = rebuild_supplier_receipt_coverage(
        db_session,
        ledger_generation_id=generation.id,
        evidence=[positive, correction],
        cycle_id="test",
    )
    db_session.commit()
    rows = db_session.query(models.MrpExecutionAllocation).order_by(
        models.MrpExecutionAllocation.allocated_qty
    ).all()
    assert result.allocation_count == 3
    assert [row.allocated_qty for row in rows] == [
        Decimal("-4.000"), Decimal("2.000"), Decimal("3.000")
    ]
    correction_rows = db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).filter_by(operation_kind="correction").all()
    assert correction_rows
    assert {row.correction_receipt_ref for row in correction_rows} == {"doc"}
    assert all(
        row.evidence_payload["correction_receipt_ref"] == "doc"
        for row in correction_rows
    )


def test_rebuild_preserves_other_generation_rows(db_session):
    generation, _req = _persistence_fixture(db_session)
    other_run = db_session.query(models.PlanningRun).filter(
        models.PlanningRun.ledger_generation_id != generation.id
    ).one()
    other_req = db_session.query(models.MrpRequirement).filter_by(
        run_id=other_run.run_id
    ).one()
    other_generation_id = int(other_run.ledger_generation_id)
    db_session.add(models.MrpExecutionAllocation(
        ledger_generation_id=other_generation_id,
        cycle_id="other",
        requirement_id=other_req.id,
        fact_type="supplier_receipt",
        allocation_kind="coverage_realization",
        fact_ref="other",
        fact_line_ref="1",
        allocated_qty=Decimal("1"),
    ))
    db_session.commit()
    rebuild_supplier_receipt_coverage(
        db_session,
        ledger_generation_id=generation.id,
        evidence=[_evidence(RECEIPT_OPERATION, 3)],
        cycle_id="test",
    )
    db_session.commit()
    assert db_session.query(models.MrpExecutionAllocation).filter_by(
        ledger_generation_id=other_generation_id
    ).count() == 1


def test_failure_after_delete_rolls_back_savepoint_and_preserves_projection(
    db_session, monkeypatch
):
    generation, _req = _persistence_fixture(db_session)
    kwargs = dict(
        ledger_generation_id=generation.id,
        evidence=[_evidence(RECEIPT_OPERATION, 3)],
        cycle_id="first",
    )
    rebuild_supplier_receipt_coverage(db_session, **kwargs)
    db_session.commit()
    before_provenance = db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).count()
    before_allocations = db_session.query(models.MrpExecutionAllocation).count()

    def injected_failure(*_args, **_kwargs):
        raise RuntimeError("injected after destructive deletes")

    monkeypatch.setattr(
        receipt_service, "allocate_supplier_receipts", injected_failure
    )
    with pytest.raises(RuntimeError, match="injected"):
        rebuild_supplier_receipt_coverage(db_session, **kwargs)
    # This is the dangerous caller pattern the savepoint must tolerate.
    db_session.commit()

    assert db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).count() == before_provenance
    assert db_session.query(
        models.MrpExecutionAllocation
    ).count() == before_allocations

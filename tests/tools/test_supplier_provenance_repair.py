"""Contract of ``--phase supplier-provenance-repair``.

A lightweight physical refresh used to fork without carrying supplier
provenance, and the retention prune deleted the parent's copy at the next
publication.  Databases already in that state have the facts but not the
typing: the typing sits at an older accepted generation, or nowhere.  This
phase re-owns what still exists and refuses to invent the rest.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import models
from tools.current_execution_migration import (
    PostflightBlocked,
    PreflightBlocked,
    apply_supplier_provenance_repair,
)

CUTOFF = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
POSTING_AT = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
SUPPLIER_RECORDER_TYPE = "Document_\u041f\u0440\u0438\u0445\u043e\u0434\u043d\u0430\u044f\u041d\u0430\u043a\u043b\u0430\u0434\u043d\u0430\u044f"


def _engine():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    return engine


def _generation(session, key, *, batch_id):
    generation = models.LedgerGeneration(
        generation_key=key,
        status="accepted",
        cutoff=CUTOFF,
        accepted_at=CUTOFF,
        source_watermarks={},
        capabilities={},
        physical_import_batch_id=int(batch_id),
        algorithm_version="repair-tests",
    )
    session.add(generation)
    session.flush()
    return generation


def _provenance(session, generation, sle):
    session.add(models.StockLedgerSupplierReceiptProvenance(
        ledger_generation_id=int(generation.id),
        stock_ledger_entry_id=int(sle.id),
        receipt_doc_type=SUPPLIER_RECORDER_TYPE,
        receipt_doc_ref=f"receipt-{int(sle.id)}",
        receipt_doc_line_no="1",
        supplier_order_ref="ORDER-1",
        supplier_order_line_no="1",
        operation_kind="supplier_receipt",
        operation_key="test",
        operation_name="test supplier receipt",
        evidence_hash=f"hash:{int(sle.id)}".ljust(64, "0"),
        evidence_payload={"signed_qty": str(sle.qty), "item_id": int(sle.item_id)},
        match_rule="bounded-typed",
        match_status="exact",
        ambiguity_count=0,
    ))
    session.flush()


def _world(engine, *, type_at_old=True, receipts=2):
    """Typed evidence at the older generation, pointer at the newer one."""
    with Session(engine) as session:
        batch = models.PhysicalImportBatch(
            batch_key="repair-boundary",
            status="completed",
            cutoff=CUTOFF,
            source_watermarks={"origin": "test"},
            source_complete=True,
            completed_at=CUTOFF,
        )
        session.add(batch)
        session.flush()
        old = _generation(session, "repair-old", batch_id=batch.id)
        pointer = _generation(session, "repair-pointer", batch_id=batch.id)
        session.add(models.PlanningTruthState(id=1, current_generation_id=pointer.id))
        for index in range(1, int(receipts) + 1):
            item = models.Item(item_code=f"REPAIR-{index}", item_name="Repair item")
            session.add(item)
            session.flush()
            sle = models.StockLedgerEntry(
                ingest_batch_id=int(batch.id),
                source_content_hash=f"{index:064d}",
                item_id=int(item.item_id),
                characteristic_ref="",
                organization_ref="ORG",
                warehouse_ref1c="WH",
                qty=Decimal("5"),
                posting_at=POSTING_AT,
                record_type="Receipt",
                recorder_type=SUPPLIER_RECORDER_TYPE,
                recorder_ref=f"doc-{index}",
                line_no="1",
                ingest_source="document_pull",
                active=True,
            )
            session.add(sle)
            session.flush()
            if type_at_old:
                _provenance(session, old, sle)
        session.commit()
        return int(old.id), int(pointer.id)


def test_repair_requires_explicit_writers_stopped():
    engine = _engine()
    _world(engine)
    with pytest.raises(PreflightBlocked, match="writers-stopped"):
        apply_supplier_provenance_repair(engine, writers_stopped=False)


def test_repair_reowns_the_pointer_evidence_from_the_older_generation():
    engine = _engine()
    old_id, pointer_id = _world(engine, receipts=3)

    report = apply_supplier_provenance_repair(engine, writers_stopped=True)

    assert report["status"] == "ready"
    assert report["generation_id"] == pointer_id
    assert report["source_generation_ids"] == [old_id]
    assert report["provenance_rows_before"] == 0
    assert report["provenance_rows_after"] == 3
    assert report["reowned_rows"] == 3
    assert report["lost_before"] == 3
    assert report["lost_after"] == 0
    assert report["idempotent"] is False
    assert report["untyped_anywhere"] == 0
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM stock_ledger_supplier_receipt_provenance "
            "WHERE ledger_generation_id = :g"
        ), {"g": pointer_id}).scalar_one() == 3
        # The older generation keeps its own copy; this is a re-own, not a move.
        assert connection.execute(text(
            "SELECT count(*) FROM stock_ledger_supplier_receipt_provenance "
            "WHERE ledger_generation_id = :g"
        ), {"g": old_id}).scalar_one() == 3


def test_repair_is_idempotent():
    engine = _engine()
    _world(engine, receipts=2)

    first = apply_supplier_provenance_repair(engine, writers_stopped=True)
    second = apply_supplier_provenance_repair(engine, writers_stopped=True)

    assert first["reowned_rows"] == 2
    assert first["idempotent"] is False
    assert second["reowned_rows"] == 0
    assert second["idempotent"] is True
    assert second["provenance_rows_after"] == first["provenance_rows_after"]
    assert second["lost_before"] == 0


def test_repair_fails_closed_when_no_generation_holds_the_evidence():
    """Nothing to re-own: the typing is gone, so the rebuild runbook applies."""
    engine = _engine()
    _world(engine, type_at_old=False, receipts=2)

    with pytest.raises(PreflightBlocked, match="ledger rebuild runbook"):
        apply_supplier_provenance_repair(engine, writers_stopped=True)


def test_repair_reports_facts_nobody_ever_typed_and_does_not_invent_them():
    """A supplier document outside the planning contour is not damage.

    It is typed by nobody by design, so the repair reports it and leaves it
    alone rather than manufacturing an order link the database never had.
    """
    engine = _engine()
    old_id, pointer_id = _world(engine, receipts=3)
    with Session(engine) as session:
        orphan = session.execute(text(
            "SELECT max(stock_ledger_entry_id) "
            "FROM stock_ledger_supplier_receipt_provenance"
        )).scalar_one()
        session.execute(text(
            "DELETE FROM stock_ledger_supplier_receipt_provenance "
            "WHERE stock_ledger_entry_id = :sle"
        ), {"sle": int(orphan)})
        session.commit()

    report = apply_supplier_provenance_repair(engine, writers_stopped=True)

    assert report["status"] == "ready"
    assert report["reowned_rows"] == 2
    assert report["lost_after"] == 0
    assert report["untyped_anywhere"] == 1
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM stock_ledger_supplier_receipt_provenance "
            "WHERE ledger_generation_id = :g"
        ), {"g": pointer_id}).scalar_one() == 2
        assert connection.execute(text(
            "SELECT count(*) FROM stock_ledger_supplier_receipt_provenance "
            "WHERE ledger_generation_id = :g"
        ), {"g": old_id}).scalar_one() == 2


def test_pre_deploy_backlog_is_its_own_read_only_phase():
    """Reported, never blocking, and out of the publication transaction."""
    from tools.current_execution_migration import pre_deploy_backlog

    engine = _engine()
    _world(engine, receipts=2)

    report = pre_deploy_backlog(engine)

    assert report["phase"] == "pre-deploy-backlog"
    assert report["status"] == "ready"
    assert set(report) >= {
        "untyped_buy_owed_receipts",
        "unallocated_make_owed_outputs",
        "evaluated",
    }
    if report["evaluated"]:
        # No BUY owners in this fixture, so nothing is owed to one.
        assert report["untyped_buy_owed_receipts"] == 0
        assert report["unallocated_make_owed_outputs"] == 0
        assert report["ambiguous_distribution_pool_count"] == 0
    else:
        assert report["reason"]


def test_repair_normalises_pre_contract_rows_in_place():
    """A stand is repaired without waiting for a delta to touch the rows.

    The pointer owns rows exactly as the bounded writer stored them before
    the one-row contract: writer markers in the identity and operation
    columns, no payload keys.  The phase re-derives their evidence from the
    SLE each already references and writes it through the one row builder;
    a second run finds nothing to do.
    """
    from app.services.item_ledger.supplier_receipt_allocation import (
        SUPPLIER_ORDER_TYPE,
        SUPPLIER_PROVENANCE_PAYLOAD_KEYS,
        provenance_is_pre_contract,
    )

    engine = _engine()
    _old_id, pointer_id = _world(engine, type_at_old=False, receipts=2)
    with Session(engine) as session:
        for sle in session.query(models.StockLedgerEntry).all():
            session.add(models.StockLedgerSupplierReceiptProvenance(
                ledger_generation_id=int(pointer_id),
                stock_ledger_entry_id=int(sle.id),
                receipt_doc_type="bounded_physical_refresh",
                receipt_doc_ref=f"sle:{int(sle.id)}",
                receipt_doc_line_no="1",
                supplier_order_ref="ORDER-1",
                supplier_order_line_no="1",
                operation_kind="supplier_receipt",
                operation_key="bounded_physical_refresh",
                operation_name="bounded typed supplier evidence",
                evidence_hash="legacy".ljust(64, "0"),
                evidence_payload={},
                match_rule="bounded-typed",
                match_status="exact",
                ambiguity_count=0,
            ))
        session.commit()

    first = apply_supplier_provenance_repair(engine, writers_stopped=True)
    second = apply_supplier_provenance_repair(engine, writers_stopped=True)

    assert first["status"] == "ready"
    assert first["pre_contract_rows_before"] == 2
    assert first["pre_contract_rows_normalised"] == 2
    assert first["pre_contract_rows_after"] == 0
    assert first["idempotent"] is False
    assert second["pre_contract_rows_before"] == 0
    assert second["pre_contract_rows_normalised"] == 0
    assert second["idempotent"] is True
    with Session(engine) as session:
        rows = session.query(models.StockLedgerSupplierReceiptProvenance).filter_by(
            ledger_generation_id=pointer_id
        ).all()
        assert len(rows) == 2
        for row in rows:
            sle = session.get(models.StockLedgerEntry, int(row.stock_ledger_entry_id))
            assert not provenance_is_pre_contract(row)
            assert row.receipt_doc_type == sle.recorder_type
            assert row.receipt_doc_ref == sle.recorder_ref
            assert row.receipt_doc_line_no == sle.line_no
            assert set(row.evidence_payload) == set(SUPPLIER_PROVENANCE_PAYLOAD_KEYS)
            assert row.evidence_payload["supplier_order_type"] == SUPPLIER_ORDER_TYPE
            assert Decimal(row.evidence_payload["signed_qty"]) == Decimal("5")
            # Typing is untouched: same order line, status and rule.
            assert (row.supplier_order_ref, row.match_status, row.match_rule) == (
                "ORDER-1", "exact", "bounded-typed",
            )

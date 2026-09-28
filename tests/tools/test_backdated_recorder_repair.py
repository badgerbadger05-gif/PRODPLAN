"""Contract of ``--phase backdated-recorder-repair``.

1C carries the *document* date in the register's ``Period``, so a receipt
typed into 1C after the accepted cutoff but dated behind it lands inside an
already-closed forward window.  Routine sync never revisits that window and
nothing queues a document a person wrote by hand, so the balance diverged and
the cutoff snap closed the divergence with a synthetic
``cutoff_balance_adjustment`` — a row no BUY attribution reads.  The goods were
on the shelf, the plan was never credited, and the purchase journal kept
asking for them.

This phase is the by-hand pointer at such a document: it ingests the real
recorder, retires the snap that stood in for it without moving the balance,
types the receipt with the canonical supplier evidence seam, and hands the
result to the existing replenishment rebase writers.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models
from tools.current_execution_migration import (
    PostflightBlocked,
    PreflightBlocked,
    apply_backdated_recorder_repair,
    _parse_recorder_identity,
)


CUTOFF = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
FREEZE_AT = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
POSTED_AT = datetime(2026, 9, 9, 18, 0, 29)
DOC_TYPE = "Document_ПриходнаяНакладная"
DOC_REF = "3a4e70d8-b283-11f1-9910-9ee51454587f"
ITEM_REF = "71d503ec-8a1b-11ef-83b6-9ee51454587f"
WAREHOUSE = "15377c4e-bf96-11f0-95ca-9ee51454587f"
QTY = Decimal("29600")


class StubClient:
    """Serves one receipt document and its register record set."""

    def __init__(self, *, qty: Decimal = QTY):
        self.qty = qty

    def get_all(self, entity, filter_query=None, order_by=None,
                select_fields=None, top=None, max_pages=None):
        if entity == "AccumulationRegister_ЗапасыНаСкладах":
            return [{
                "Recorder": DOC_REF,
                "Recorder_Type": f"StandardODATA.{DOC_TYPE}",
                "RecordSet": [{
                    "Period": POSTED_AT.isoformat(),
                    "LineNumber": "1",
                    "Active": True,
                    "RecordType": "Receipt",
                    "Организация_Key": "",
                    "Номенклатура_Key": ITEM_REF,
                    "Характеристика_Key": "00000000-0000-0000-0000-000000000000",
                    "СтруктурнаяЕдиница_Key": WAREHOUSE,
                    "Количество": str(self.qty),
                }],
            }]
        if entity == f"{DOC_TYPE}_Запасы":
            return [{
                "Ref_Key": DOC_REF,
                "LineNumber": "1",
                "Номенклатура_Key": ITEM_REF,
                "Характеристика_Key": "00000000-0000-0000-0000-000000000000",
                "Склад_Key": WAREHOUSE,
                "Количество": str(self.qty),
            }]
        return []

    def _make_request(self, endpoint, params=None, **kwargs):
        if DOC_REF in endpoint:
            return {
                "Ref_Key": DOC_REF,
                "Number": "ЗСНФ-002015",
                "Date": POSTED_AT.isoformat(),
                "Posted": True,
                "ХозяйственнаяОперация_Key": "8d97069c-0000-0000-0000-000000000000",
                "ВидОперации": "ПоступлениеОтПоставщика",
                "Склад_Key": WAREHOUSE,
            }
        raise AssertionError(f"unexpected request {endpoint}")


def _engine():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    return engine


def _batch(session, key, cutoff):
    batch = models.PhysicalImportBatch(
        batch_key=key, status="completed", cutoff=cutoff,
        source_watermarks={}, source_complete=True, completed_at=cutoff,
    )
    session.add(batch)
    session.flush()
    return batch


def _world(engine, *, snap_qty: Decimal = QTY):
    """A BUY owner whose receipt exists only as a cutoff snap."""
    with Session(engine) as session:
        freeze_batch = _batch(session, "repair-freeze", FREEZE_AT)
        current_batch = _batch(session, "repair-current", CUTOFF)
        generation = models.LedgerGeneration(
            generation_key="repair-pointer", status="accepted", cutoff=CUTOFF,
            accepted_at=CUTOFF, source_watermarks={}, capabilities={},
            physical_import_batch_id=int(current_batch.id),
            algorithm_version="repair-tests",
        )
        item = models.Item(
            item_code="00-00002417", item_name="Болт М6x25",
            item_article="VP-001110", item_ref1c=ITEM_REF,
        )
        session.add_all([generation, item])
        session.add(models.StockWarehouse(
            warehouse_ref1c=WAREHOUSE, warehouse_name="Склад №4",
            is_selected=True,
        ))
        session.flush()
        session.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
        session.flush()

        snap = models.StockLedgerEntry(
            ingest_batch_id=int(current_batch.id),
            source_content_hash="snap-hash",
            business_identity="cutoff:snap:1",
            item_id=item.item_id, characteristic_ref="", organization_ref="",
            warehouse_ref1c=WAREHOUSE, qty=snap_qty, posting_at=CUTOFF,
            record_type="Receipt", movement_kind="cutoff_balance_adjustment",
            recorder_type="cutoff_balance_adjustment", recorder_ref="snap-1",
            line_no="0", ingest_source="cutoff_balance_adjustment",
        )
        session.add(snap)

        run = models.PlanningRun(
            status="FIXED_SNAPSHOT", ledger_generation_id=generation.id,
            config_snapshot={}, active_freeze_version=1, ledger_cutoff=CUTOFF,
        )
        session.add(run)
        session.flush()
        requirement = models.MrpRequirement(
            run_id=run.run_id, item_id=item.item_id,
            total_required_qty=QTY, net_required_qty=QTY,
            period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
            bom_level=0, planning_stock_pool="default", characteristic_ref="",
            organization_ref="", freeze_version=1,
        )
        session.add(requirement)
        session.flush()
        session.add(models.MrpFreezeBaseline(
            run_id=run.run_id, freeze_version=1, item_id=item.item_id,
            characteristic_ref="", organization_ref="",
            planning_stock_pool="default", stock_qty=Decimal("0"),
            physical_import_batch_id=int(freeze_batch.id), baseline_at=FREEZE_AT,
        ))
        owner = models.ReservationEntry(
            ledger_generation_id=generation.id, item_id=item.item_id,
            run_id=run.run_id, freeze_version=1, requirement_id=requirement.id,
            priority_period_from=date(2026, 9, 1),
            priority_period_to=date(2026, 9, 30),
            realization_mode="buy", reserved_qty=QTY,
            replenishment_required_qty=QTY,
            covered_from_stock_at_freeze_qty=Decimal("0"),
            lifecycle_status="active", owner_kind="current", is_current=True,
            current_identity=f"reservation:req:{int(requirement.id)}:mode:buy",
        )
        session.add(owner)
        session.commit()
        return {
            "generation_id": int(generation.id),
            "item_id": int(item.item_id),
            "owner_id": int(owner.id),
            "snap_id": int(snap.id),
        }


def _active_total(engine, item_id):
    with Session(engine) as session:
        return sum(
            (Decimal(str(row.qty)) for row in session.query(models.StockLedgerEntry)
             .filter(models.StockLedgerEntry.item_id == int(item_id),
                     models.StockLedgerEntry.active.is_(True))),
            Decimal("0"),
        )


def test_recorder_identity_requires_type_and_ref():
    assert _parse_recorder_identity(f"{DOC_TYPE}:{DOC_REF}") == (DOC_TYPE, DOC_REF)
    with pytest.raises(PreflightBlocked, match="Recorder_Type"):
        _parse_recorder_identity(DOC_REF)


def test_phase_requires_writers_stopped():
    engine = _engine()
    _world(engine)
    with pytest.raises(PreflightBlocked, match="writers-stopped"):
        apply_backdated_recorder_repair(
            engine, writers_stopped=False,
            recorders=((DOC_TYPE, DOC_REF),), client=StubClient(),
        )


def test_phase_requires_at_least_one_recorder():
    engine = _engine()
    _world(engine)
    with pytest.raises(PreflightBlocked, match="--recorder is required"):
        apply_backdated_recorder_repair(
            engine, writers_stopped=True, recorders=(), client=StubClient(),
        )


def test_receipt_replaces_its_snap_and_credits_the_owner():
    engine = _engine()
    world = _world(engine)
    before = _active_total(engine, world["item_id"])

    report = apply_backdated_recorder_repair(
        engine, writers_stopped=True,
        recorders=((DOC_TYPE, DOC_REF),), client=StubClient(), republish=False,
    )

    assert report["status"] == "ready"
    assert report["imported_rows"] == 1
    assert report["typed_supplier_provenance"] == 1
    assert report["retired_cutoff_snaps"]["retired_rows"] == 1
    assert report["retired_cutoff_snaps"]["reissued_rows"] == 0
    # The snap stood for exactly this document, so the cell keeps its balance.
    assert _active_total(engine, world["item_id"]) == before

    with Session(engine) as session:
        snap = session.get(models.StockLedgerEntry, world["snap_id"])
        assert snap.active is False
        assert session.query(models.StockLedgerFactSupersession).filter_by(
            old_sle_id=world["snap_id"]
        ).one().new_sle_id is None
        owner = session.get(models.ReservationEntry, world["owner_id"])
        # Canon: the receipt is the fact that quenches the need.
        assert Decimal(str(owner.replenishment_received_qty)) == QTY
        real = session.query(models.StockLedgerEntry).filter_by(
            recorder_ref=DOC_REF
        ).one()
        assert real.recorder_type == DOC_TYPE
        assert real.active is True
        assert session.query(
            models.StockLedgerSupplierReceiptProvenance
        ).filter_by(stock_ledger_entry_id=int(real.id)).count() == 1


def test_import_above_the_snap_is_refused_rather_than_doubled():
    """Only a snap that really absorbed the document may be retired."""
    engine = _engine()
    _world(engine, snap_qty=Decimal("100"))
    with pytest.raises(PostflightBlocked, match="exceed the cutoff snaps"):
        apply_backdated_recorder_repair(
            engine, writers_stopped=True,
            recorders=((DOC_TYPE, DOC_REF),), client=StubClient(), republish=False,
        )

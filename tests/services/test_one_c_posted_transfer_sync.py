"""Tests for one_c_posted_transfer_sync."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.models import (
    Item,
    PhysicalImportBatch,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionMaterialCustodyEvent,
    StockLedgerEntry,
    StockRecorderPull,
    SyncLink,
)
from app.services import one_c_posted_transfer_sync as posted_sync
from app.services.one_c_stock_transfer_export import STOCK_TRANSFER_ENTITY
from app.services.production_material_custody_events import (
    append_material_issue_custody_event,
    project_transfer_custody_events_for_recorder,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_issue_with_link(
    db,
    *,
    line_status: str = "to_move",
    issue_status: str = "exported",
    target_ref_key: str = "transfer-ref-key",
    link_status: str = "success",
    direction: str = "issue",
    source_warehouse_ref1c: str | None = None,
    warehouse_ref1c: str | None = None,
) -> tuple[ProductionMaterialIssue, SyncLink]:
    """Build an item + order + product + state + material_issue + sync_link
    chain matching what a successful export leaves behind."""
    item = Item(
        item_code=f"IT-{target_ref_key}",
        item_name=f"Item {target_ref_key}",
        item_article=f"ART-{target_ref_key}",
        item_ref1c=f"item-ref-{target_ref_key}",
        unit="шт",
                status="active",
    )
    db.add(item)
    db.flush()

    order = ProductionOrder(
        order_number=f"O-{target_ref_key}",
        order_date=datetime(2026, 5, 20),
        is_posted=True,
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
    )
    db.add(product)
    db.flush()
    state = ProductionOrderLineState(
        product_id=product.product_id,
        status=line_status,
        issue_status=issue_status,
    )
    db.add(state)

    issue = ProductionMaterialIssue(
        document_number=f"MI-{target_ref_key}",
        product_id=product.product_id,
        order_id=order.order_id,
        status=issue_status,
        direction=direction,
        exported_ref1c=target_ref_key,
    )
    if source_warehouse_ref1c is not None:
        issue.source_warehouse_ref1c = source_warehouse_ref1c
    if warehouse_ref1c is not None:
        issue.warehouse_ref1c = warehouse_ref1c
    db.add(issue)
    db.flush()
    db.add(
        ProductionMaterialIssueLine(
            issue_id=int(issue.issue_id),
            component_item_id=int(item.item_id),
            required_qty=1.0,
            issued_qty=0.0,
            line_status="planned",
        )
    )

    link = SyncLink(
        source_system="PRODPLAN",
        source_doctype="material_issue",
        source_id=issue.issue_id,
        target_system="1C",
        target_entity=STOCK_TRANSFER_ENTITY,
        target_ref_key=target_ref_key,
        target_number=issue.document_number,
        status=link_status,
    )
    db.add(link)
    db.commit()
    return issue, link


def _mk_transfer_batch(db, *, batch_key: str) -> PhysicalImportBatch:
    batch = PhysicalImportBatch(
        batch_key=batch_key,
        status="completed",
        source_watermarks={},
        completed_at=datetime(2026, 5, 20),
    )
    db.add(batch)
    db.flush()
    return batch


def _add_transfer_sle(
    db,
    *,
    batch: PhysicalImportBatch,
    transfer_ref: str,
    item_id: int,
    movement_kind: str,
    warehouse_ref1c: str,
    qty: float,
    posting_at: datetime,
    line_no: int,
) -> StockLedgerEntry:
    row = StockLedgerEntry(
        ingest_batch_id=int(batch.id),
        source_content_hash="a" * 64,
        item_id=int(item_id),
        warehouse_ref1c=warehouse_ref1c,
        qty=float(qty),
        posting_at=posting_at,
        movement_kind=movement_kind,
        recorder_type=STOCK_TRANSFER_ENTITY,
        recorder_ref=transfer_ref,
        line_no=str(line_no),
    )
    db.add(row)
    db.flush()
    return row


def test_manual_posted_transfer_uses_exact_order_basis_for_custody(db_session):
    db = db_session
    product_item = Item(
        item_code="manual-product",
        item_name="Manual product",
        item_article="manual-product",
        item_ref1c="manual-product-ref",
        unit="шт",
        status="active",
    )
    component = Item(
        item_code="manual-component",
        item_name="Manual component",
        item_article="manual-component",
        item_ref1c="manual-component-ref",
        unit="шт",
        status="active",
    )
    db.add_all([product_item, component])
    db.flush()
    order = ProductionOrder(
        order_number="manual-order",
        order_date=datetime(2026, 5, 20),
        order_ref1c="manual-order-ref",
        is_posted=True,
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=product_item.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
    )
    db.add(product)
    db.add(
        StockRecorderPull(
            recorder_type=STOCK_TRANSFER_ENTITY,
            recorder_ref="manual-transfer-ref",
            order_ref=order.order_ref1c,
            status="done",
            source="test",
        )
    )
    batch = _mk_transfer_batch(db, batch_key="manual-order-transfer")
    outbound = _add_transfer_sle(
        db,
        batch=batch,
        transfer_ref="manual-transfer-ref",
        item_id=component.item_id,
        movement_kind="transfer_out",
        warehouse_ref1c="source-warehouse",
        qty=-3,
        posting_at=datetime(2026, 5, 20, 12, 0),
        line_no=1,
    )
    inbound = _add_transfer_sle(
        db,
        batch=batch,
        transfer_ref="manual-transfer-ref",
        item_id=component.item_id,
        movement_kind="transfer_in",
        warehouse_ref1c="workshop-warehouse",
        qty=3,
        posting_at=datetime(2026, 5, 20, 12, 0),
        line_no=2,
    )

    assert project_transfer_custody_events_for_recorder(
        db,
        recorder_type=STOCK_TRANSFER_ENTITY,
        recorder_ref="manual-transfer-ref",
        stock_ledger_entries=[outbound, inbound],
    ) == 1
    db.flush()
    event = db.query(ProductionMaterialCustodyEvent).one()
    assert event.issue_id is None
    assert event.product_id == product.product_id
    assert event.component_item_id == component.item_id
    assert event.location_kind == "workshop"
    assert event.delta_qty == pytest.approx(3)
    assert event.source_sle_id == inbound.id

    assert project_transfer_custody_events_for_recorder(
        db,
        recorder_type=STOCK_TRANSFER_ENTITY,
        recorder_ref="manual-transfer-ref",
        stock_ledger_entries=[outbound, inbound],
    ) == 0


def test_manual_transfer_order_basis_fails_closed_for_multiple_products(db_session):
    db = db_session
    first = Item(
        item_code="ambiguous-first",
        item_name="Ambiguous first",
        item_article="ambiguous-first",
        item_ref1c="ambiguous-first-ref",
        unit="шт",
        status="active",
    )
    second = Item(
        item_code="ambiguous-second",
        item_name="Ambiguous second",
        item_article="ambiguous-second",
        item_ref1c="ambiguous-second-ref",
        unit="шт",
        status="active",
    )
    db.add_all([first, second])
    db.flush()
    order = ProductionOrder(
        order_number="ambiguous-order",
        order_date=datetime(2026, 5, 20),
        order_ref1c="ambiguous-order-ref",
        is_posted=True,
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    db.add_all(
        [
            ProductionProduct(
                order_id=order.order_id,
                item_id=first.item_id,
                line_number=1,
                quantity=1,
                produced_qty=0,
                remaining_qty=1,
            ),
            ProductionProduct(
                order_id=order.order_id,
                item_id=second.item_id,
                line_number=2,
                quantity=1,
                produced_qty=0,
                remaining_qty=1,
            ),
            StockRecorderPull(
                recorder_type=STOCK_TRANSFER_ENTITY,
                recorder_ref="ambiguous-transfer-ref",
                order_ref=order.order_ref1c,
                status="done",
                source="test",
            ),
        ]
    )
    batch = _mk_transfer_batch(db, batch_key="ambiguous-order-transfer")
    inbound = _add_transfer_sle(
        db,
        batch=batch,
        transfer_ref="ambiguous-transfer-ref",
        item_id=first.item_id,
        movement_kind="transfer_in",
        warehouse_ref1c="workshop-warehouse",
        qty=1,
        posting_at=datetime(2026, 5, 20, 12, 0),
        line_no=1,
    )

    assert project_transfer_custody_events_for_recorder(
        db,
        recorder_type=STOCK_TRANSFER_ENTITY,
        recorder_ref="ambiguous-transfer-ref",
        stock_ledger_entries=[inbound],
    ) == 0
    assert db.query(ProductionMaterialCustodyEvent).count() == 0


class _FakeOData:
    """In-memory stub for OData1CClient. Configurable per-ref Posted flag."""

    def __init__(self, posted_refs: set[str]) -> None:
        self.posted_refs = posted_refs
        self.deleted_refs: set[str] = set()
        self.calls: list = []

    def get_all(self, entity, filter_query=None, select_fields=None, **_kwargs):
        self.calls.append((entity, filter_query))
        # Parse refs out of "Ref_Key eq guid'...'" — naive but enough for the
        # test stub. Then emit a row only for the ones in posted_refs.
        rows = []
        if filter_query:
            for chunk in str(filter_query).split("guid'"):
                if "'" in chunk:
                    ref = chunk.split("'", 1)[0]
                    if ref in self.posted_refs:
                        rows.append(
                            {
                                "Ref_Key": ref,
                                "Posted": True,
                                "DeletionMark": False,
                                "Запасы": [
                                    {
                                        "Номенклатура_Key": f"item-ref-{ref}",
                                        "Количество": 1.0,
                                    }
                                ],
                            }
                        )
                    elif ref in self.deleted_refs:
                        rows.append(
                            {
                                "Ref_Key": ref,
                                "Posted": False,
                                "DeletionMark": True,
                                "Запасы": [],
                            }
                        )
        return rows


def _stub_config(monkeypatch):
    monkeypatch.setattr(
        posted_sync,
        "_load_odata_config",
        lambda: {"base_url": "http://demo/odata/unf", "username": "u", "password": "p"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_advances_to_assembled_when_1c_says_posted(db_session, monkeypatch):
    db = db_session
    issue, link = _mk_issue_with_link(
        db, line_status="to_move", issue_status="exported", target_ref_key="ref-aaa"
    )

    _stub_config(monkeypatch)
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: _FakeOData({"ref-aaa"})
    )

    result = posted_sync.sync_posted_transfers(db)

    assert result["status"] == "ok"
    assert result["candidates"] == 1
    assert result["posted_found"] == 1
    assert result["advanced"] == 1
    assert result["errors"] == []
    assert result["details"][0]["issue_id"] == issue.issue_id

    db.refresh(link)
    db.refresh(issue)
    assert link.status == "posted"
    assert link.last_synced_at is not None
    assert issue.status == "posted"

    state = (
        db.query(ProductionOrderLineState)
        .filter_by(product_id=issue.product_id)
        .one()
    )
    assert state.status == "assembled"
    assert state.issue_status == "posted"


def test_ledger_projector_posts_transfer_custody_events(db_session, monkeypatch):
    db = db_session
    issue, link = _mk_issue_with_link(
        db,
        line_status="issued",
        issue_status="exported",
        target_ref_key="ref-ledger",
        direction="issue",
        source_warehouse_ref1c="WH-SRC",
        warehouse_ref1c="WH-DEST",
    )
    batch = _mk_transfer_batch(db, batch_key="batch-ledger-ref-ledger")
    posting_at = datetime(2026, 7, 30, 12, 0, 0)
    _add_transfer_sle(
        db,
        batch=batch,
        transfer_ref="ref-ledger",
        item_id=int(issue.lines[0].component_item_id),
        movement_kind="transfer_out",
        warehouse_ref1c="WH-SRC",
        qty=-1.0,
        posting_at=posting_at,
        line_no=1,
    )
    _add_transfer_sle(
        db,
        batch=batch,
        transfer_ref="ref-ledger",
        item_id=int(issue.lines[0].component_item_id),
        movement_kind="transfer_in",
        warehouse_ref1c="WH-DEST",
        qty=1.0,
        posting_at=posting_at,
        line_no=2,
    )
    assert (
        db.query(StockLedgerEntry)
        .filter(StockLedgerEntry.recorder_ref == "ref-ledger")
        .count()
        == 2
    )
    db.flush()
    db.commit()

    _stub_config(monkeypatch)
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: _FakeOData({"ref-ledger"})
    )

    rows = db.query(StockLedgerEntry).filter_by(recorder_ref="ref-ledger").all()
    assert project_transfer_custody_events_for_recorder(
        db,
        recorder_type=STOCK_TRANSFER_ENTITY,
        recorder_ref="ref-ledger",
        stock_ledger_entries=rows,
    ) == 3
    first = posted_sync.sync_posted_transfers(db)
    assert first["advanced"] == 1
    assert first["posted_found"] == 1
    db.refresh(issue)
    assert issue.status == "posted"

    events = (
        db.query(ProductionMaterialCustodyEvent)
        .filter(ProductionMaterialCustodyEvent.issue_id == int(issue.issue_id))
        .order_by(ProductionMaterialCustodyEvent.location_kind.asc())
        .all()
    )
    assert len(events) == 3
    opening = next(
        e
        for e in events
        if e.source_kind == "issue_created" and e.location_kind == "transit"
    )
    outbound = next(e for e in events if e.source_kind == "transfer_posted" and e.location_kind == "transit")
    inbound = next(e for e in events if e.location_kind == "workshop")
    assert opening.source_sle_id is None
    assert opening.delta_qty == pytest.approx(1.0)
    assert opening.effective_at == posting_at
    assert opening.warehouse_ref1c == "WH-SRC"
    assert outbound.source_kind == "transfer_posted"
    assert inbound.source_kind == "transfer_posted"
    assert outbound.source_sle_id is not None
    assert inbound.source_sle_id is not None
    assert outbound.delta_qty == pytest.approx(-1.0)
    assert inbound.delta_qty == pytest.approx(1.0)
    assert outbound.effective_at == posting_at
    assert inbound.effective_at == posting_at
    assert outbound.warehouse_ref1c == "WH-SRC"
    assert inbound.warehouse_ref1c == "WH-DEST"

    assert project_transfer_custody_events_for_recorder(
        db,
        recorder_type=STOCK_TRANSFER_ENTITY,
        recorder_ref="ref-ledger",
        stock_ledger_entries=rows,
    ) == 0
    second = posted_sync.sync_posted_transfers(db)
    assert second["advanced"] == 0
    events_after = (
        db.query(ProductionMaterialCustodyEvent)
        .filter(ProductionMaterialCustodyEvent.issue_id == int(issue.issue_id))
        .all()
    )
    assert len(events_after) == 3


def test_ledger_projector_collapses_equivalent_duplicate_component_rows(db_session):
    db = db_session
    issue, _link = _mk_issue_with_link(
        db,
        line_status="issued",
        issue_status="exported",
        target_ref_key="ref-duplicate-equivalent",
        direction="issue",
        source_warehouse_ref1c="WH-SRC",
        warehouse_ref1c="WH-DEST",
    )
    first = issue.lines[0]
    first.required_qty = 11.0
    first.issued_qty = 11.0
    first.unit = "шт"
    first.line_status = "issued"
    db.add(
        ProductionMaterialIssueLine(
            issue_id=int(issue.issue_id),
            component_item_id=int(first.component_item_id),
            required_qty=11.0,
            issued_qty=11.0,
            unit="шт",
            line_status="issued",
        )
    )
    batch = _mk_transfer_batch(db, batch_key="batch-duplicate-equivalent")
    posting_at = datetime(2026, 6, 23, 12, 0, 0)
    _add_transfer_sle(
        db, batch=batch, transfer_ref="ref-duplicate-equivalent",
        item_id=int(first.component_item_id), movement_kind="transfer_out",
        warehouse_ref1c="WH-SRC", qty=-22.0, posting_at=posting_at, line_no=1,
    )
    _add_transfer_sle(
        db, batch=batch, transfer_ref="ref-duplicate-equivalent",
        item_id=int(first.component_item_id), movement_kind="transfer_in",
        warehouse_ref1c="WH-DEST", qty=22.0, posting_at=posting_at, line_no=2,
    )
    db.commit()

    rows = db.query(StockLedgerEntry).filter_by(
        recorder_ref="ref-duplicate-equivalent"
    ).all()
    assert project_transfer_custody_events_for_recorder(
        db,
        recorder_type=STOCK_TRANSFER_ENTITY,
        recorder_ref="ref-duplicate-equivalent",
        stock_ledger_entries=rows,
    ) == 3
    db.flush()
    events = db.query(ProductionMaterialCustodyEvent).filter_by(
        issue_id=int(issue.issue_id)
    ).all()
    assert len(events) == 3
    assert {event.document_line_no for event in events} == {str(first.line_id)}


def test_ledger_projector_rejects_conflicting_duplicate_component_rows(db_session):
    db = db_session
    issue, _link = _mk_issue_with_link(
        db,
        target_ref_key="ref-duplicate-conflict",
        direction="issue",
        source_warehouse_ref1c="WH-SRC",
        warehouse_ref1c="WH-DEST",
    )
    first = issue.lines[0]
    db.add(
        ProductionMaterialIssueLine(
            issue_id=int(issue.issue_id),
            component_item_id=int(first.component_item_id),
            required_qty=1.0,
            issued_qty=0.0,
            unit="кг",
            line_status="planned",
        )
    )
    db.commit()

    with pytest.raises(RuntimeError, match="conflicting duplicate component rows"):
        project_transfer_custody_events_for_recorder(
            db,
            recorder_type=STOCK_TRANSFER_ENTITY,
            recorder_ref="ref-duplicate-conflict",
            stock_ledger_entries=[],
        )


def test_ledger_projector_holds_only_what_was_reserved(db_session):
    """Shipping more than was reserved is ordinary, and the surplus is not ours.

    On the shadow stand four issue lines were posted in 1C above their
    reservation (40.644 reserved, 49 moved).  Custody closes the reservation and
    stops there: the surplus does reach the workshop, but as free stock for the
    next issue, not as this product's hold.  Folding the whole movement drove
    the transit cell negative and failed the entire Ledger build behind it.
    """
    db = db_session
    issue, _link = _mk_issue_with_link(
        db,
        line_status="issued",
        issue_status="exported",
        target_ref_key="ref-surplus",
        direction="issue",
        source_warehouse_ref1c="WH-SRC",
        warehouse_ref1c="WH-DEST",
    )
    line = issue.lines[0]
    # The operator reserved 0.6 …
    append_material_issue_custody_event(
        db,
        issue=issue,
        line=line,
        delta_qty=0.6,
        source_kind="issue_created",
        location_kind="transit",
        warehouse_ref1c="WH-SRC",
        source_ref1c="WH-SRC",
    )
    db.flush()
    batch = _mk_transfer_batch(db, batch_key="batch-surplus")
    posting_at = datetime(2026, 7, 30, 12, 0, 0)
    # … the storekeeper shipped 1.0.
    _add_transfer_sle(
        db, batch=batch, transfer_ref="ref-surplus",
        item_id=int(line.component_item_id), movement_kind="transfer_out",
        warehouse_ref1c="WH-SRC", qty=-1.0, posting_at=posting_at, line_no=1,
    )
    _add_transfer_sle(
        db, batch=batch, transfer_ref="ref-surplus",
        item_id=int(line.component_item_id), movement_kind="transfer_in",
        warehouse_ref1c="WH-DEST", qty=1.0, posting_at=posting_at, line_no=2,
    )
    db.commit()
    rows = db.query(StockLedgerEntry).filter_by(recorder_ref="ref-surplus").all()

    appended = project_transfer_custody_events_for_recorder(
        db,
        recorder_type=STOCK_TRANSFER_ENTITY,
        recorder_ref="ref-surplus",
        stock_ledger_entries=rows,
    )
    db.commit()

    assert appended == 2          # outbound and inbound, both clamped
    events = (
        db.query(ProductionMaterialCustodyEvent)
        .filter(ProductionMaterialCustodyEvent.issue_id == int(issue.issue_id))
        .all()
    )
    transit = [e for e in events if e.location_kind == "transit"]
    workshop = [e for e in events if e.location_kind == "workshop"]
    # The reservation is closed exactly, never overdrawn …
    assert sum(float(e.delta_qty) for e in transit) == pytest.approx(0.0)
    # … and only the reserved part is held for this product at the workshop.
    assert sum(float(e.delta_qty) for e in workshop) == pytest.approx(0.6)

    # A replay changes nothing.
    assert project_transfer_custody_events_for_recorder(
        db,
        recorder_type=STOCK_TRANSFER_ENTITY,
        recorder_ref="ref-surplus",
        stock_ledger_entries=rows,
    ) == 0


def test_ledger_projector_never_rewrites_a_transfer_it_already_recorded(db_session):
    """Rows written by the older rule stay as they are — repair is not automatic.

    A transfer recorded at its full quantity before custody was clamped leaves
    the cell negative.  The projector must not quietly invent compensating
    history for it: that is a one-off data correction someone decides on, with
    the failure visible instead of silently absorbed.
    """
    db = db_session
    issue, _link = _mk_issue_with_link(
        db,
        line_status="issued",
        issue_status="exported",
        target_ref_key="ref-legacy",
        direction="issue",
        source_warehouse_ref1c="WH-SRC",
        warehouse_ref1c="WH-DEST",
    )
    line = issue.lines[0]
    append_material_issue_custody_event(
        db, issue=issue, line=line, delta_qty=0.6,
        source_kind="issue_created", location_kind="transit",
        warehouse_ref1c="WH-SRC", source_ref1c="WH-SRC",
    )
    batch = _mk_transfer_batch(db, batch_key="batch-legacy")
    posting_at = datetime(2026, 7, 30, 12, 0, 0)
    outbound = _add_transfer_sle(
        db, batch=batch, transfer_ref="ref-legacy",
        item_id=int(line.component_item_id), movement_kind="transfer_out",
        warehouse_ref1c="WH-SRC", qty=-1.0, posting_at=posting_at, line_no=1,
    )
    inbound = _add_transfer_sle(
        db, batch=batch, transfer_ref="ref-legacy",
        item_id=int(line.component_item_id), movement_kind="transfer_in",
        warehouse_ref1c="WH-DEST", qty=1.0, posting_at=posting_at, line_no=2,
    )
    append_material_issue_custody_event(
        db, issue=issue, line=line, delta_qty=-1.0,
        source_kind="transfer_posted", location_kind="transit",
        warehouse_ref1c="WH-SRC", source_ref1c="WH-SRC",
        source_sle_id=int(outbound.id), effective_at=posting_at,
    )
    append_material_issue_custody_event(
        db, issue=issue, line=line, delta_qty=1.0,
        source_kind="transfer_posted", location_kind="workshop",
        warehouse_ref1c="WH-DEST", source_ref1c="WH-SRC",
        source_sle_id=int(inbound.id), effective_at=posting_at,
    )
    db.commit()
    rows = db.query(StockLedgerEntry).filter_by(recorder_ref="ref-legacy").all()

    assert project_transfer_custody_events_for_recorder(
        db,
        recorder_type=STOCK_TRANSFER_ENTITY,
        recorder_ref="ref-legacy",
        stock_ledger_entries=rows,
    ) == 0
    transit = [
        event
        for event in db.query(ProductionMaterialCustodyEvent)
        .filter(ProductionMaterialCustodyEvent.issue_id == int(issue.issue_id))
        .all()
        if event.location_kind == "transit"
    ]
    assert sum(float(event.delta_qty) for event in transit) == pytest.approx(-0.4)


def test_ledger_projector_dedupes_same_physical_transfer_reimport(db_session):
    db = db_session
    issue, _link = _mk_issue_with_link(
        db,
        line_status="issued",
        issue_status="exported",
        target_ref_key="ref-ledger-reimport",
        direction="issue",
        source_warehouse_ref1c="WH-SRC",
        warehouse_ref1c="WH-DEST",
    )
    posting_at = datetime(2026, 7, 30, 12, 0, 0)
    old_batch = _mk_transfer_batch(db, batch_key="batch-ledger-reimport-old")
    old_outbound = _add_transfer_sle(
        db, batch=old_batch, transfer_ref="ref-ledger-reimport",
        item_id=int(issue.lines[0].component_item_id), movement_kind="transfer_out",
        warehouse_ref1c="WH-SRC", qty=-1.0, posting_at=posting_at, line_no=1,
    )
    old_inbound = _add_transfer_sle(
        db, batch=old_batch, transfer_ref="ref-ledger-reimport",
        item_id=int(issue.lines[0].component_item_id), movement_kind="transfer_in",
        warehouse_ref1c="WH-DEST", qty=1.0, posting_at=posting_at, line_no=2,
    )
    assert project_transfer_custody_events_for_recorder(
        db, recorder_type=STOCK_TRANSFER_ENTITY, recorder_ref="ref-ledger-reimport",
        stock_ledger_entries=[old_outbound, old_inbound],
    ) == 3
    old_outbound.active = False
    old_inbound.active = False
    new_batch = _mk_transfer_batch(db, batch_key="batch-ledger-reimport-new")
    new_outbound = _add_transfer_sle(
        db, batch=new_batch, transfer_ref="ref-ledger-reimport",
        item_id=int(issue.lines[0].component_item_id), movement_kind="transfer_out",
        warehouse_ref1c="WH-SRC", qty=-1.0, posting_at=posting_at, line_no=1,
    )
    new_inbound = _add_transfer_sle(
        db, batch=new_batch, transfer_ref="ref-ledger-reimport",
        item_id=int(issue.lines[0].component_item_id), movement_kind="transfer_in",
        warehouse_ref1c="WH-DEST", qty=1.0, posting_at=posting_at, line_no=2,
    )

    assert project_transfer_custody_events_for_recorder(
        db, recorder_type=STOCK_TRANSFER_ENTITY, recorder_ref="ref-ledger-reimport",
        stock_ledger_entries=[new_outbound, new_inbound],
    ) == 0
    assert (
        db.query(ProductionMaterialCustodyEvent)
        .filter_by(issue_id=issue.issue_id)
        .count()
        == 3
    )


def test_ledger_projector_posts_return_transfer_custody_event(db_session, monkeypatch):
    db = db_session
    issue, _link = _mk_issue_with_link(
        db,
        line_status="issued",
        issue_status="exported",
        target_ref_key="ref-return-ledger",
        direction="return",
        source_warehouse_ref1c="WH-WORKSHOP",
        warehouse_ref1c="WH-RAW",
    )
    batch = _mk_transfer_batch(db, batch_key="batch-ledger-ref-return")
    posting_at = datetime(2026, 7, 30, 12, 30, 0)
    _add_transfer_sle(
        db,
        batch=batch,
        transfer_ref="ref-return-ledger",
        item_id=int(issue.lines[0].component_item_id),
        movement_kind="transfer_out",
        warehouse_ref1c="WH-WORKSHOP",
        qty=-1.0,
        posting_at=posting_at,
        line_no=1,
    )
    db.commit()

    _stub_config(monkeypatch)
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: _FakeOData({"ref-return-ledger"})
    )

    rows = db.query(StockLedgerEntry).filter_by(recorder_ref="ref-return-ledger").all()
    assert project_transfer_custody_events_for_recorder(
        db,
        recorder_type=STOCK_TRANSFER_ENTITY,
        recorder_ref="ref-return-ledger",
        stock_ledger_entries=rows,
    ) == 1
    result = posted_sync.sync_posted_transfers(db)
    assert result["advanced"] == 1

    events = (
        db.query(ProductionMaterialCustodyEvent)
        .filter(ProductionMaterialCustodyEvent.issue_id == int(issue.issue_id))
        .all()
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.source_kind == "transfer_returned"
    assert ev.location_kind == "workshop"
    assert ev.delta_qty == pytest.approx(-1.0)
    assert ev.source_sle_id is not None
    assert ev.warehouse_ref1c == "WH-WORKSHOP"
    assert ev.effective_at == posting_at


def test_keeps_line_to_move_until_all_delivery_issues_are_posted(db_session, monkeypatch):
    db = db_session
    issue_posted, link_posted = _mk_issue_with_link(
        db, line_status="to_move", issue_status="exported", target_ref_key="ref-one"
    )
    product_id = int(issue_posted.product_id)
    order_id = int(issue_posted.order_id)

    item = Item(
        item_code="IT-ref-two",
        item_name="Item ref-two",
        item_article="ART-ref-two",
        item_ref1c="item-ref-two",
        unit="шт",
                status="active",
    )
    db.add(item)
    db.flush()
    issue_pending = ProductionMaterialIssue(
        document_number="MI-ref-two",
        product_id=product_id,
        order_id=order_id,
        status="exported",
        exported_ref1c="ref-two",
    )
    db.add(issue_pending)
    db.flush()
    db.add(
        ProductionMaterialIssueLine(
            issue_id=int(issue_pending.issue_id),
            component_item_id=int(item.item_id),
            required_qty=1.0,
            issued_qty=0.0,
            line_status="planned",
        )
    )
    db.add(
        SyncLink(
            source_system="PRODPLAN",
            source_doctype="material_issue",
            source_id=issue_pending.issue_id,
            target_system="1C",
            target_entity=STOCK_TRANSFER_ENTITY,
            target_ref_key="ref-two",
            target_number=issue_pending.document_number,
            status="success",
        )
    )
    db.commit()

    _stub_config(monkeypatch)
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: _FakeOData({"ref-one"})
    )

    result = posted_sync.sync_posted_transfers(db)

    assert result["status"] == "ok"
    assert result["posted_found"] == 1
    assert result["advanced"] == 1

    db.refresh(link_posted)
    db.refresh(issue_posted)
    db.refresh(issue_pending)
    assert link_posted.status == "posted"
    assert issue_posted.status == "posted"
    assert issue_pending.status == "exported"

    state = (
        db.query(ProductionOrderLineState)
        .filter_by(product_id=product_id)
        .one()
    )
    assert state.status == "to_move"
    assert state.issue_status == "requested"


def test_idempotent_repeat_run(db_session, monkeypatch):
    """Second pass still checks posted docs, but does not flag unchanged rows."""
    db = db_session
    issue, link = _mk_issue_with_link(db, target_ref_key="ref-bbb")
    _stub_config(monkeypatch)
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: _FakeOData({"ref-bbb"})
    )

    first = posted_sync.sync_posted_transfers(db)
    assert first["advanced"] == 1

    second = posted_sync.sync_posted_transfers(db)
    assert second["candidates"] == 1
    assert second["posted_found"] == 1
    assert second["advanced"] == 0


def test_posted_sync_does_not_project_existing_ledger_rows(db_session, monkeypatch):
    db = db_session
    issue, link = _mk_issue_with_link(
        db,
        line_status="issued",
        issue_status="posted",
        target_ref_key="ref-posted-no-changes",
        link_status="posted",
        direction="issue",
        source_warehouse_ref1c="WH-SRC",
        warehouse_ref1c="WH-DEST",
    )
    db.refresh(issue)
    issue.lines[0].required_qty = 1.0
    issue.lines[0].issued_qty = 1.0
    db.flush()

    batch = _mk_transfer_batch(db, batch_key="batch-posted-no-changes")
    posting_at = datetime(2026, 7, 30, 13, 0, 0)
    _add_transfer_sle(
        db,
        batch=batch,
        transfer_ref="ref-posted-no-changes",
        item_id=int(issue.lines[0].component_item_id),
        movement_kind="transfer_out",
        warehouse_ref1c="WH-SRC",
        qty=-1.0,
        posting_at=posting_at,
        line_no=1,
    )
    _add_transfer_sle(
        db,
        batch=batch,
        transfer_ref="ref-posted-no-changes",
        item_id=int(issue.lines[0].component_item_id),
        movement_kind="transfer_in",
        warehouse_ref1c="WH-DEST",
        qty=1.0,
        posting_at=posting_at,
        line_no=2,
    )
    db.commit()

    _stub_config(monkeypatch)
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: _FakeOData({"ref-posted-no-changes"})
    )

    first = posted_sync.sync_posted_transfers(db)
    # The fake posted document also normalizes the line quantity, so the
    # administrative sync may advance its own state. It must not write custody.
    assert first["advanced"] == 1
    assert first["posted_found"] == 1

    events = (
        db.query(ProductionMaterialCustodyEvent)
        .filter(ProductionMaterialCustodyEvent.issue_id == int(issue.issue_id))
        .all()
    )
    assert events == []

    second = posted_sync.sync_posted_transfers(db)
    assert second["advanced"] == 0


def test_repeat_run_updates_quantity_changed_in_1c(db_session, monkeypatch):
    db = db_session
    issue, link = _mk_issue_with_link(
        db,
        issue_status="posted",
        target_ref_key="ref-qty",
        link_status="posted",
    )
    line = issue.lines[0]
    line.required_qty = 0.656
    line.issued_qty = 0.656
    db.commit()

    class _QtyChangedOData(_FakeOData):
        def get_all(self, entity, filter_query=None, select_fields=None, **kwargs):
            rows = super().get_all(entity, filter_query, select_fields, **kwargs)
            for row in rows:
                row["Запасы"][0]["Количество"] = 0.688
            return rows

    _stub_config(monkeypatch)
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: _QtyChangedOData({"ref-qty"})
    )

    result = posted_sync.sync_posted_transfers(db)

    assert result["status"] == "ok"
    assert result["candidates"] == 1
    assert result["posted_found"] == 1
    assert result["advanced"] == 1
    db.refresh(line)
    assert float(line.required_qty) == pytest.approx(0.688)
    assert float(line.issued_qty) == pytest.approx(0.688)


def test_repeat_run_adds_component_line_added_in_1c(db_session, monkeypatch):
    db = db_session
    issue, _link = _mk_issue_with_link(
        db,
        issue_status="posted",
        target_ref_key="ref-added",
        link_status="posted",
    )
    added_item = Item(
        item_code="IT-added",
        item_name="Added in 1C",
        item_article="ART-added",
        item_ref1c="item-ref-added-extra",
        unit="шт",
                status="active",
    )
    db.add(added_item)
    db.commit()

    class _AddedLineOData(_FakeOData):
        def get_all(self, entity, filter_query=None, select_fields=None, **kwargs):
            rows = super().get_all(entity, filter_query, select_fields, **kwargs)
            for row in rows:
                row["Запасы"].append(
                    {
                        "Номенклатура_Key": "item-ref-added-extra",
                        "Количество": 7.0,
                    }
                )
            return rows

    _stub_config(monkeypatch)
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: _AddedLineOData({"ref-added"})
    )

    result = posted_sync.sync_posted_transfers(db)

    assert result["advanced"] == 1
    lines = (
        db.query(ProductionMaterialIssueLine)
        .filter_by(issue_id=issue.issue_id)
        .order_by(ProductionMaterialIssueLine.component_item_id)
        .all()
    )
    added = [line for line in lines if line.component_item_id == added_item.item_id]
    assert len(added) == 1
    assert float(added[0].required_qty) == pytest.approx(7.0)
    assert float(added[0].issued_qty) == pytest.approx(7.0)
    assert added[0].line_status == "issued"


def test_only_lines_present_in_posted_doc_are_marked_issued(db_session, monkeypatch):
    """Regression: lines missing from the posted 1C document stayed unissued."""
    db = db_session
    issue, _link = _mk_issue_with_link(db, target_ref_key="ref-partial")
    confirmed_item_id = int(issue.lines[0].component_item_id)

    extra_item = Item(
        item_code="IT-not-in-1c",
        item_name="Not in the posted document",
        item_article="ART-not-in-1c",
        item_ref1c="item-ref-not-in-1c",
        unit="шт",
                status="active",
    )
    db.add(extra_item)
    db.flush()
    db.add(
        ProductionMaterialIssueLine(
            issue_id=int(issue.issue_id),
            component_item_id=int(extra_item.item_id),
            required_qty=5.0,
            issued_qty=0.0,
            line_status="planned",
        )
    )
    db.commit()

    _stub_config(monkeypatch)
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: _FakeOData({"ref-partial"})
    )

    result = posted_sync.sync_posted_transfers(db)
    assert result["advanced"] == 1

    lines = {
        int(line.component_item_id): line
        for line in db.query(ProductionMaterialIssueLine)
        .filter_by(issue_id=issue.issue_id)
        .all()
    }
    assert lines[confirmed_item_id].line_status == "issued"
    assert float(lines[confirmed_item_id].issued_qty) == pytest.approx(1.0)

    untouched = lines[int(extra_item.item_id)]
    assert untouched.line_status == "planned"
    assert float(untouched.issued_qty) == pytest.approx(0.0)


def test_falls_back_to_required_qty_when_1c_omits_document_rows(db_session, monkeypatch):
    """No table part in the 1C answer = composition unknown, legacy behaviour."""
    db = db_session
    issue, _link = _mk_issue_with_link(db, target_ref_key="ref-no-rows")

    class _NoRowsOData(_FakeOData):
        def get_all(self, entity, filter_query=None, select_fields=None, **kwargs):
            rows = super().get_all(entity, filter_query, select_fields, **kwargs)
            for row in rows:
                row.pop("Запасы", None)
            return rows

    _stub_config(monkeypatch)
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: _NoRowsOData({"ref-no-rows"})
    )

    result = posted_sync.sync_posted_transfers(db)
    assert result["advanced"] == 1

    line = db.query(ProductionMaterialIssueLine).filter_by(issue_id=issue.issue_id).one()
    assert line.line_status == "issued"
    assert float(line.issued_qty) == pytest.approx(float(line.required_qty))


def test_does_not_regress_state_past_assembled(db_session, monkeypatch):
    """If the line is already 'produced' / 'produced_partial', the posted
    transfer must not roll it back to 'assembled'."""
    db = db_session
    issue, link = _mk_issue_with_link(
        db, line_status="produced", issue_status="exported", target_ref_key="ref-ccc"
    )
    _stub_config(monkeypatch)
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: _FakeOData({"ref-ccc"})
    )

    result = posted_sync.sync_posted_transfers(db)
    assert result["posted_found"] == 1

    state = (
        db.query(ProductionOrderLineState).filter_by(product_id=issue.product_id).one()
    )
    # Stayed at 'produced', NOT downgraded to 'assembled'.
    assert state.status == "produced"
    # issue_status is still bumped to 'posted' since it's the issue-pipeline
    # signal independent of the line coverage state.
    assert state.issue_status == "posted"

    db.refresh(issue)
    assert issue.status == "posted"


def test_skips_links_not_posted_in_1c(db_session, monkeypatch):
    """Links whose 1C document still has Posted=false stay at status='success'."""
    db = db_session
    issue_posted, link_posted = _mk_issue_with_link(db, target_ref_key="ref-yes")
    issue_pending, link_pending = _mk_issue_with_link(db, target_ref_key="ref-no")

    _stub_config(monkeypatch)
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: _FakeOData({"ref-yes"})
    )

    result = posted_sync.sync_posted_transfers(db)
    assert result["candidates"] == 2
    assert result["posted_found"] == 1
    assert result["advanced"] == 1

    db.refresh(link_posted)
    db.refresh(link_pending)
    assert link_posted.status == "posted"
    assert link_pending.status == "success"  # untouched

    db.refresh(issue_posted)
    db.refresh(issue_pending)
    assert issue_posted.status == "posted"
    assert issue_pending.status == "exported"  # untouched


def test_cancels_deleted_transfer_in_1c(db_session, monkeypatch):
    db = db_session
    issue, link = _mk_issue_with_link(db, target_ref_key="ref-deleted")

    class _DeletedOData(_FakeOData):
        def __init__(self):
            super().__init__(set())
            self.deleted_refs = {"ref-deleted"}

    _stub_config(monkeypatch)
    monkeypatch.setattr(posted_sync, "OData1CClient", lambda **_: _DeletedOData())

    result = posted_sync.sync_posted_transfers(db)

    assert result["advanced"] == 1
    assert result["details"][0]["action"] == "cancelled"
    db.refresh(link)
    db.refresh(issue)
    assert link.status == "cancelled"
    assert issue.status == "cancelled"
    assert issue.lines[0].line_status == "cancelled"


def test_dry_run_does_not_persist(db_session, monkeypatch):
    db = db_session
    issue, link = _mk_issue_with_link(db, target_ref_key="ref-dry")
    _stub_config(monkeypatch)
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: _FakeOData({"ref-dry"})
    )

    result = posted_sync.sync_posted_transfers(db, dry_run=True)
    assert result["dry_run"] is True
    assert result["posted_found"] == 1
    # `advanced` still counts in-memory changes, but rollback means none of
    # them hit the DB.
    db.refresh(link)
    db.refresh(issue)
    assert link.status == "success"
    assert issue.status == "exported"


def test_empty_when_no_pending_links(db_session, monkeypatch):
    """When there are no successful material-issue links, no 1C call is made
    and the function returns cleanly with candidates=0."""
    db = db_session
    # No fixtures. Stub explicitly fails if instantiated.
    monkeypatch.setattr(
        posted_sync,
        "_load_odata_config",
        lambda: {"base_url": "http://x"},
    )
    monkeypatch.setattr(
        posted_sync, "OData1CClient", lambda **_: pytest.fail("must not be called"),
    )

    result = posted_sync.sync_posted_transfers(db)
    assert result["status"] == "ok"
    assert result["candidates"] == 0
    assert result["advanced"] == 0

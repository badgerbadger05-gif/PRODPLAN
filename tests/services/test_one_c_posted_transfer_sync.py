"""Tests for one_c_posted_transfer_sync."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.models import (
    Item,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SyncLink,
)
from app.services import one_c_posted_transfer_sync as posted_sync
from app.services.one_c_stock_transfer_export import STOCK_TRANSFER_ENTITY


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
) -> tuple[ProductionMaterialIssue, SyncLink]:
    """Build an item + order + product + state + material_issue + sync_link
    chain matching what a successful export leaves behind."""
    item = Item(
        item_code=f"IT-{target_ref_key}",
        item_name=f"Item {target_ref_key}",
        item_article=f"ART-{target_ref_key}",
        item_ref1c=f"item-ref-{target_ref_key}",
        unit="шт",
        stock_qty=0,
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
        direction="issue",
        exported_ref1c=target_ref_key,
    )
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
        stock_qty=0,
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
        stock_qty=0,
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
        stock_qty=0,
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

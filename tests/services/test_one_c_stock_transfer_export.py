"""Tests for one_c_stock_transfer_export."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.models import (
    DefaultSpecification,
    Item,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SpecComponent,
    Specification,
    SyncLink,
)
from app.services import one_c_stock_transfer_export as exporter


# -----------------------------
# Helpers
# -----------------------------


def _mk_item(db, *, code: str, ref1c: str) -> Item:
    it = Item(
        item_code=code,
        item_name=f"Item {code}",
        item_article=code,
        item_ref1c=ref1c,
        unit="шт",
        stock_qty=100,
        status="active",
    )
    db.add(it)
    db.flush()
    return it


def _mk_issue(
    db,
    *,
    parent: Item,
    component: Item,
    source_wh: str | None = None,
    dest_wh: str | None = None,
    status: str = "draft",
) -> ProductionMaterialIssue:
    spec = Specification(spec_name=f"Spec {parent.item_code}", spec_ref1c=f"spec-{parent.item_code}")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=1))

    order = ProductionOrder(
        order_number=f"TR-{parent.item_id}",
        order_date=datetime(2026, 5, 20),
        is_posted=True,
        deletion_mark=False,
        order_ref1c=f"order-ref-{parent.item_id}",
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=1,
        quantity=5,
        produced_qty=0,
        remaining_qty=5,
    )
    db.add(product)
    db.flush()
    issue = ProductionMaterialIssue(
        document_number=f"MI-{parent.item_id}",
        product_id=product.product_id,
        order_id=order.order_id,
        status=status,
        warehouse_ref1c=dest_wh,
        source_warehouse_ref1c=source_wh,
    )
    db.add(issue)
    db.flush()
    db.add(
        ProductionMaterialIssueLine(
            issue_id=issue.issue_id,
            component_item_id=component.item_id,
            required_qty=5,
            issued_qty=0,
            line_status="planned",
        )
    )
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="shortage",
            issue_status="requested",
        )
    )
    db.commit()
    return issue


class _FakeClient:
    def __init__(self, *, ref_key: str = "transfer-ref-key", fail: bool = False) -> None:
        self.ref_key = ref_key
        self.fail = fail
        self.posts: list = []

    def post(self, entity, payload, **_kwargs):
        self.posts.append((entity, payload))
        if self.fail:
            raise RuntimeError("simulated 1C failure")
        return {"Ref_Key": self.ref_key}


def _stub_config(monkeypatch, *, base_url: str) -> None:
    monkeypatch.setattr(
        exporter,
        "_load_odata_config",
        lambda: {"base_url": base_url, "username": "u", "password": "p"},
    )


# -----------------------------
# Tests
# -----------------------------


def test_dry_run_returns_payload_with_both_warehouses(db_session, monkeypatch):
    db = db_session
    parent = _mk_item(db, code="TRP1", ref1c="parent-ref-1")
    comp = _mk_item(db, code="TRC1", ref1c="comp-ref-1")
    issue = _mk_issue(
        db,
        parent=parent,
        component=comp,
        source_wh="src-warehouse-guid",
        dest_wh="dst-warehouse-guid",
    )

    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("Network client must not be instantiated in dry-run"),
    )

    result = exporter.export_material_issues_to_1c(db, [issue.issue_id], dry_run=True)
    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["issues_eligible"] == 1
    [pl] = result["payloads"]
    payload = pl["payload"]
    assert payload["Posted"] is False
    assert payload["СкладОтправитель_Key"] == "src-warehouse-guid"
    assert payload["СкладПолучатель_Key"] == "dst-warehouse-guid"
    assert payload["ДокументОснование"] == f"order-ref-{parent.item_id}"
    assert payload["ДокументОснование_Type"] == "StandardODATA.Document_ЗаказНаПроизводство"
    [stock_line] = payload["Запасы"]
    assert stock_line["Номенклатура_Key"] == "comp-ref-1"
    assert float(stock_line["Количество"]) == 5.0
    assert "PRODPLAN source=material_issue/" in payload["Комментарий"]

    # No sync_link writes during dry-run.
    assert db.query(SyncLink).filter_by(source_doctype="material_issue").count() == 0


def test_skips_issue_without_parent_order_ref1c(db_session):
    """Per contract: child document cannot be exported without a basis.
    If the parent ProductionOrder isn't in 1C yet (no order_ref1c), the
    transfer must be skipped, not exported orphaned."""
    db = db_session
    parent = _mk_item(db, code="TR-NOBASE", ref1c="parent-ref-nobase")
    comp = _mk_item(db, code="TR-NOBASE-C", ref1c="comp-ref-nobase")
    issue = _mk_issue(db, parent=parent, component=comp)
    # Clear the parent's order_ref1c — simulating an order not yet exported to 1C.
    issue.order.order_ref1c = None
    db.commit()

    result = exporter.export_material_issues_to_1c(db, [issue.issue_id], dry_run=True)

    assert result["issues_eligible"] == 0
    assert len(result["skipped_rows"]) == 1
    assert "order_ref1c" in result["skipped_rows"][0]["reason"]


def test_payload_omits_warehouse_keys_when_unset(db_session, monkeypatch):
    """If source/destination warehouse aren't known, omit those keys
    entirely so 1C accepts the draft and the user can fill them in."""
    db = db_session
    parent = _mk_item(db, code="TRP2", ref1c="parent-ref-2")
    comp = _mk_item(db, code="TRC2", ref1c="comp-ref-2")
    issue = _mk_issue(db, parent=parent, component=comp, source_wh=None, dest_wh=None)

    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: pytest.fail("no network"))
    result = exporter.export_material_issues_to_1c(db, [issue.issue_id], dry_run=True)
    payload = result["payloads"][0]["payload"]
    assert "СкладОтправитель_Key" not in payload
    assert "СкладПолучатель_Key" not in payload


def test_demo_guard_refuses_non_demo_without_override(db_session, monkeypatch):
    db = db_session
    parent = _mk_item(db, code="TRP3", ref1c="parent-ref-3")
    comp = _mk_item(db, code="TRC3", ref1c="comp-ref-3")
    issue = _mk_issue(db, parent=parent, component=comp, dest_wh="dst")

    _stub_config(monkeypatch, base_url="http://erp.example/odata/unf")
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    with pytest.raises(PermissionError):
        exporter.export_material_issues_to_1c(db, [issue.issue_id], dry_run=False)
    assert fake.posts == []

    # allow_production overrides.
    result = exporter.export_material_issues_to_1c(
        db, [issue.issue_id], dry_run=False, allow_production=True
    )
    assert result["issues_created"] == 1


def test_successful_export_stamps_sync_link_and_issue_status(db_session, monkeypatch):
    db = db_session
    parent = _mk_item(db, code="TRP4", ref1c="parent-ref-4")
    comp = _mk_item(db, code="TRC4", ref1c="comp-ref-4")
    issue = _mk_issue(
        db,
        parent=parent,
        component=comp,
        source_wh="src-4",
        dest_wh="dst-4",
    )

    _stub_config(monkeypatch, base_url="http://1c-demo/odata/unf_demo")
    monkeypatch.setattr(
        exporter, "OData1CClient", lambda **_: _FakeClient(ref_key="c8dbfcc4-trf-ref")
    )

    result = exporter.export_material_issues_to_1c(db, [issue.issue_id], dry_run=False)
    assert result["status"] == "ok"
    assert result["issues_created"] == 1

    db.refresh(issue)
    assert issue.status == "exported"
    assert issue.exported_ref1c == "c8dbfcc4-trf-ref"
    assert issue.exported_at is not None

    link = (
        db.query(SyncLink)
        .filter_by(
            source_doctype="material_issue",
            source_id=issue.issue_id,
            target_entity=exporter.STOCK_TRANSFER_ENTITY,
        )
        .one()
    )
    assert link.status == "success"
    assert link.target_ref_key == "c8dbfcc4-trf-ref"
    assert link.target_number == issue.document_number

    # ProductionOrderLineState.issue_status moves to 'exported'.
    state = (
        db.query(ProductionOrderLineState)
        .filter_by(product_id=issue.product_id)
        .one()
    )
    assert state.issue_status == "exported"


def test_second_export_is_noop(db_session, monkeypatch):
    db = db_session
    parent = _mk_item(db, code="TRP5", ref1c="parent-ref-5")
    comp = _mk_item(db, code="TRC5", ref1c="comp-ref-5")
    issue = _mk_issue(db, parent=parent, component=comp, dest_wh="dst-5")

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(ref_key="reuse-ref-key")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    exporter.export_material_issues_to_1c(db, [issue.issue_id], dry_run=False)
    assert len(fake.posts) == 1

    result = exporter.export_material_issues_to_1c(db, [issue.issue_id], dry_run=False)
    assert result["issues_created"] == 0
    assert result["issues_already_linked"] == 1
    assert len(fake.posts) == 1


def test_skipped_invalid_inputs(db_session, monkeypatch):
    db = db_session
    parent = _mk_item(db, code="TRP6", ref1c="parent-ref-6")
    comp_no_ref = Item(
        item_code="TRC-NOREF",
        item_name="No ref comp",
        item_article="NOREF",
        item_ref1c=None,
        unit="шт",
        stock_qty=10,
        status="active",
    )
    db.add(comp_no_ref)
    db.flush()
    # Issue with a component lacking item_ref1c -> skipped.
    no_ref_issue = _mk_issue(db, parent=parent, component=comp_no_ref, dest_wh="dst")
    # Cancelled issue -> skipped.
    parent2 = _mk_item(db, code="TRP7", ref1c="parent-ref-7")
    comp2 = _mk_item(db, code="TRC7", ref1c="comp-ref-7")
    cancelled_issue = _mk_issue(
        db, parent=parent2, component=comp2, dest_wh="dst", status="cancelled"
    )

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_material_issues_to_1c(
        db, [no_ref_issue.issue_id, cancelled_issue.issue_id, 999_999], dry_run=False
    )
    reasons = [r["reason"] for r in result["skipped_rows"]]
    assert any("item_ref1c" in r for r in reasons)
    assert any("cancelled" in r for r in reasons)
    assert any("не найден" in r for r in reasons)
    assert result["issues_eligible"] == 0
    assert fake.posts == []


def test_partial_failure_keeps_other_issues_committed(db_session, monkeypatch):
    db = db_session
    parent_ok = _mk_item(db, code="TROK", ref1c="parent-ok")
    parent_bad = _mk_item(db, code="TRBAD", ref1c="parent-bad")
    comp_ok = _mk_item(db, code="TRCOK", ref1c="comp-ok")
    comp_bad = _mk_item(db, code="TRCBAD", ref1c="comp-bad")
    issue_ok = _mk_issue(db, parent=parent_ok, component=comp_ok, dest_wh="dst")
    issue_bad = _mk_issue(db, parent=parent_bad, component=comp_bad, dest_wh="dst")

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")

    call_count = {"n": 0}

    class _SometimesFail:
        def post(self, entity, payload, **_):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise RuntimeError("simulated transfer failure")
            return {"Ref_Key": "ok-transfer-ref"}

    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: _SometimesFail())

    result = exporter.export_material_issues_to_1c(
        db, [issue_ok.issue_id, issue_bad.issue_id], dry_run=False
    )
    assert result["status"] == "partial_error"
    assert result["issues_created"] == 1
    assert result["issues_error"] == 1

    db.refresh(issue_ok)
    db.refresh(issue_bad)
    assert issue_ok.status == "exported"
    assert issue_ok.exported_ref1c == "ok-transfer-ref"
    assert issue_bad.status == "error"
    assert "simulated transfer failure" in (issue_bad.export_error or "")

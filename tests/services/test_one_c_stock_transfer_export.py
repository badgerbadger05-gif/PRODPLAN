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
    Unit,
)
from app.services import one_c_stock_transfer_export as exporter
from app.services.one_c_document_numbers import material_issue_number


# -----------------------------
# Helpers
# -----------------------------


def _mk_item(db, *, code: str, ref1c: str) -> Item:
    it = Item(
        item_code=code,
        item_name=f"Item {code}",
        item_article=code,
        item_ref1c=ref1c,
        unit=f"unit-ref-{code}",
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
    source_wh: str | None = "src-wh",
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
        self.patches: list = []
        self.balance_rows: list = []

    def post(self, entity, payload, **_kwargs):
        self.posts.append((entity, payload))
        if self.fail:
            raise RuntimeError("simulated 1C failure")
        return {"Ref_Key": self.ref_key}

    def patch(self, entity_ref, payload, **_kwargs):
        self.patches.append((entity_ref, payload))
        if self.fail:
            raise RuntimeError("simulated 1C failure")
        return {}

    def get_all(self, entity_name, **_kwargs):
        if str(entity_name).startswith("AccumulationRegister_ЗапасыНаСкладах/Balance"):
            return list(self.balance_rows)
        return []


def _stub_config(monkeypatch, *, base_url: str) -> None:
    monkeypatch.setattr(
        exporter,
        "_load_odata_config",
        lambda: {"base_url": base_url, "username": "u", "password": "p"},
    )


# -----------------------------
# Tests
# -----------------------------


def test_dry_run_returns_payload_with_both_structural_units(db_session, monkeypatch):
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
    assert payload["Number"] == material_issue_number(db, issue)
    assert payload["СтруктурнаяЕдиница_Key"] == "src-warehouse-guid"
    assert payload["СтруктурнаяЕдиницаПолучатель_Key"] == "dst-warehouse-guid"
    assert payload["ДокументОснование"] == f"order-ref-{parent.item_id}"
    assert payload["ДокументОснование_Type"] == "StandardODATA.Document_ЗаказНаПроизводство"
    [stock_line] = payload["Запасы"]
    assert stock_line["Номенклатура_Key"] == "comp-ref-1"
    assert "ЕдиницаИзмерения" not in stock_line
    assert stock_line["СтавкаНДС_Key"] == exporter.DEFAULT_STOCK_TRANSFER_VAT_RATE_REF1C
    assert stock_line["КлючСвязи"] == 1
    assert float(stock_line["Количество"]) == 5.0
    assert "PRODPLAN source=material_issue/" in payload["Комментарий"]

    # No sync_link writes during dry-run.
    assert db.query(SyncLink).filter_by(source_doctype="material_issue").count() == 0


def test_text_unit_is_resolved_to_classifier_guid(db_session, monkeypatch):
    db = db_session
    unit_ref = "aae0017c-991b-11eb-e39a-fa163e61326a"
    db.add(Unit(unit_ref1c=unit_ref, unit_name="шт", short_name="шт", unit_code="796"))
    db.commit()
    parent = _mk_item(db, code="TRP-TXT-UNIT", ref1c="parent-text-unit-ref")
    comp = _mk_item(db, code="TRC-TXT-UNIT", ref1c="comp-text-unit-ref")
    comp.unit = "шт"
    issue = _mk_issue(
        db,
        parent=parent,
        component=comp,
        source_wh="src-text-unit-wh",
        dest_wh="dst-text-unit-wh",
    )

    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("Network client must not be instantiated in dry-run"),
    )

    result = exporter.export_material_issues_to_1c(db, [issue.issue_id], dry_run=True)

    [stock_line] = result["payloads"][0]["payload"]["Запасы"]
    assert stock_line["ЕдиницаИзмерения"] == unit_ref
    assert stock_line["ЕдиницаИзмерения_Type"] == "StandardODATA.Catalog_КлассификаторЕдиницИзмерения"


def test_apply_payload_fills_source_storage_cell_from_live_1c_balance(db_session, monkeypatch):
    db = db_session
    parent = _mk_item(db, code="TRP-CELL", ref1c="parent-cell-ref")
    comp = _mk_item(db, code="TRC-CELL", ref1c="comp-cell-ref")
    issue = _mk_issue(
        db,
        parent=parent,
        component=comp,
        source_wh="src-cell-wh",
        dest_wh="dst-cell-wh",
    )

    _stub_config(monkeypatch, base_url="http://1c-demo/odata/unf_demo")
    fake = _FakeClient(ref_key="transfer-cell-ref")
    fake.balance_rows = [
        {
            "Номенклатура_Key": "comp-cell-ref",
            "СтруктурнаяЕдиница_Key": "src-cell-wh",
            "Ячейка_Key": "cell-ref-1",
            "КоличествоBalance": 12,
        }
    ]
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_material_issues_to_1c(
        db, [issue.issue_id], dry_run=False, allow_production=False
    )

    assert result["issues_created"] == 1
    [(_, payload)] = fake.posts
    [stock_line] = payload["Запасы"]
    assert stock_line["Ячейка_Key"] == "cell-ref-1"
    assert payload["ПоложениеЯчейкиОтправителя"] == "ВТабличнойЧасти"


def test_transfer_numbers_share_order_key_and_use_suffixes(db_session):
    db = db_session
    parent = _mk_item(db, code="TRP-SFX", ref1c="parent-sfx")
    comp = _mk_item(db, code="TRC-SFX", ref1c="comp-sfx")
    first = _mk_issue(db, parent=parent, component=comp)
    order = first.order
    product2 = ProductionProduct(
        order_id=order.order_id,
        item_id=parent.item_id,
        line_number=2,
        quantity=3,
        produced_qty=0,
        remaining_qty=3,
    )
    db.add(product2)
    db.flush()
    second = ProductionMaterialIssue(
        document_number="tmp-second-suffix",
        product_id=product2.product_id,
        order_id=order.order_id,
        status="draft",
        warehouse_ref1c="dst-2",
        source_warehouse_ref1c="src-2",
    )
    db.add(second)
    db.flush()
    db.commit()

    first_number = material_issue_number(db, first)
    second_number = material_issue_number(db, second)
    assert first_number.startswith("MT")
    assert second_number.startswith("MT")
    assert len(first_number) == 11
    assert len(second_number) == 11
    assert first_number != second_number


def test_chain_auto_exports_parent_order_in_dry_run(db_session):
    """Per contract: a transfer is created ONLY on the basis of a production
    order. When the parent isn't in 1C yet, the transfer export should
    chain-call the parent order export first. In dry_run, both payloads
    appear in the result; the transfer itself still skips because dry_run
    doesn't actually stamp order_ref1c."""
    db = db_session
    parent = _mk_item(db, code="TR-CHAIN", ref1c="parent-ref-chain")
    comp = _mk_item(db, code="TR-CHAIN-C", ref1c="comp-ref-chain")
    issue = _mk_issue(db, parent=parent, component=comp)
    # Clear parent's order_ref1c and mark it MRP-source so it's eligible.
    issue.order.order_ref1c = None
    issue.order.source = "mrp"
    db.commit()

    result = exporter.export_material_issues_to_1c(db, [issue.issue_id], dry_run=True)

    # The chain step ran and produced a parent-order payload.
    assert result["parent_orders_export"] is not None
    assert result["parent_orders_export"]["entity"] == "Document_ЗаказНаПроизводство"
    assert result["parent_orders_export"]["orders_eligible"] == 1

    # In dry_run the parent isn't actually stamped, so the child still
    # cannot find a basis and skips with a diagnostic.
    assert result["issues_eligible"] == 0


def test_dbr_parent_chain_threads_allow_production(db_session, monkeypatch):
    db = db_session
    parent = _mk_item(db, code="TR-DBR-GUARD", ref1c="parent-dbr-guard")
    comp = _mk_item(db, code="TR-DBR-GUARD-C", ref1c="comp-dbr-guard")
    issue = _mk_issue(db, parent=parent, component=comp)
    issue.order.order_ref1c = None
    issue.order.source = "dbr"
    issue.product.source_dbr_signal_id = 987
    db.flush()
    calls = []

    from app.services.dbr import materialize_service

    def fake_launch(_db, signal_id, *, dry_run, allow_production):
        calls.append((signal_id, dry_run, allow_production))
        return {"created": False, "already_launched": False}

    monkeypatch.setattr(materialize_service, "launch_signal", fake_launch)

    exporter._chain_export_parent_orders(
        db,
        [issue.issue_id],
        dry_run=False,
        allow_production=False,
    )

    assert calls == [(987, False, False)]


def test_chain_full_apply_exports_order_then_transfer(db_session, monkeypatch):
    """In apply mode the chain actually exports the parent order first,
    stamps order_ref1c, then exports the transfer with the correct
    ДокументОснование."""
    db = db_session
    parent = _mk_item(db, code="TR-FULL", ref1c="parent-ref-full")
    comp = _mk_item(db, code="TR-FULL-C", ref1c="comp-ref-full")
    issue = _mk_issue(db, parent=parent, component=comp, dest_wh="dst-wh")
    issue.order.order_ref1c = None
    issue.order.source = "mrp"
    db.commit()

    fake = _FakeClient(ref_key="stub-key")
    # Both parent-order and transfer exports go through the same fake client.
    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)
    # The chained production-order exporter uses its own _create_odata_client.
    from app.services import one_c_production_order_export as poe
    monkeypatch.setattr(poe, "_load_odata_config", lambda: {"base_url": "http://demo/odata/unf_demo", "username": "u", "password": "p"})
    monkeypatch.setattr(poe, "OData1CClient", lambda **_: fake)
    # First POST returns the order's ref; second POST returns the transfer's ref.
    fake.ref_key = "order-created-ref"
    original_post = fake.post

    def staged_post(entity, payload, **kw):
        # Cycle ref_key: order first, then transfer
        if entity == "Document_ЗаказНаПроизводство":
            fake.ref_key = "transfer-created-ref"
            return {"Ref_Key": "order-created-ref"}
        return original_post(entity, payload, **kw)
    monkeypatch.setattr(fake, "post", staged_post)

    result = exporter.export_material_issues_to_1c(
        db, [issue.issue_id], dry_run=False, allow_production=False
    )

    # Parent chain happened first.
    assert result["parent_orders_export"]["orders_created"] == 1
    # Then the transfer was exported and stamped the parent's ref as basis.
    assert result["issues_created"] == 1
    # Verify the actual basis in the posted transfer payload.
    transfer_posts = [p for p in fake.posts if p[0] == "Document_ПеремещениеЗапасов"]
    assert len(transfer_posts) == 1
    assert transfer_posts[0][1]["ДокументОснование"] == "order-created-ref"
    assert transfer_posts[0][1]["ДокументОснование_Type"] == "StandardODATA.Document_ЗаказНаПроизводство"


def test_export_skips_issue_when_source_warehouse_unset(db_session, monkeypatch):
    """Transfers without a source warehouse must not become 1C documents."""
    db = db_session
    parent = _mk_item(db, code="TRP2", ref1c="parent-ref-2")
    comp = _mk_item(db, code="TRC2", ref1c="comp-ref-2")
    issue = _mk_issue(db, parent=parent, component=comp, source_wh=None, dest_wh=None)

    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: pytest.fail("no network"))
    result = exporter.export_material_issues_to_1c(db, [issue.issue_id], dry_run=True)
    assert result["payloads"] == []
    assert result["issues_eligible"] == 0
    assert result["skipped_rows"][0]["reason"] == "склад отправитель пуст — перемещение в 1С не сформировано"


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
    assert link.target_number == material_issue_number(db, issue)

    # ProductionOrderLineState.issue_status moves to 'exported'.
    state = (
        db.query(ProductionOrderLineState)
        .filter_by(product_id=issue.product_id)
        .one()
    )
    assert state.issue_status == "exported"


def test_second_export_patches_existing_transfer(db_session, monkeypatch):
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
    assert result["issues_created"] == 1
    assert result["issues_already_linked"] == 0
    assert len(fake.posts) == 1
    assert len(fake.patches) == 1
    assert fake.patches[0][0] == "Document_ПеремещениеЗапасов(guid'reuse-ref-key')"


def test_posted_transfer_is_not_patched_on_second_export(db_session, monkeypatch):
    db = db_session
    parent = _mk_item(db, code="TRP5P", ref1c="parent-ref-5p")
    comp = _mk_item(db, code="TRC5P", ref1c="comp-ref-5p")
    issue = _mk_issue(db, parent=parent, component=comp, dest_wh="dst-5p")

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(ref_key="posted-ref-key")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    exporter.export_material_issues_to_1c(db, [issue.issue_id], dry_run=False)
    issue.status = "posted"
    db.commit()

    result = exporter.export_material_issues_to_1c(db, [issue.issue_id], dry_run=False)

    assert result["issues_created"] == 0
    assert result["issues_already_linked"] == 1
    assert len(fake.posts) == 1
    assert fake.patches == []


def test_existing_error_link_with_ref_patches_not_posts_duplicate(db_session, monkeypatch):
    db = db_session
    parent = _mk_item(db, code="TRP5R", ref1c="parent-ref-5r")
    comp = _mk_item(db, code="TRC5R", ref1c="comp-ref-5r")
    issue = _mk_issue(db, parent=parent, component=comp, dest_wh="dst-5r")

    db.add(SyncLink(
        source_doctype="material_issue",
        source_id=issue.issue_id,
        target_entity=exporter.STOCK_TRANSFER_ENTITY,
        target_number=material_issue_number(db, issue),
        payload_hash="old-hash",
        target_ref_key="existing-transfer-ref",
        status="error",
        last_error="post failed after create",
    ))
    db.commit()

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(ref_key="new-ref-should-not-be-used")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_material_issues_to_1c(db, [issue.issue_id], dry_run=False)

    assert result["issues_created"] == 1
    assert fake.posts == []
    assert len(fake.patches) == 1
    assert fake.patches[0][0] == "Document_ПеремещениеЗапасов(guid'existing-transfer-ref')"
    db.refresh(issue)
    assert issue.status == "exported"
    assert issue.exported_ref1c == "existing-transfer-ref"


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

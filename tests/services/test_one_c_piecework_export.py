"""Tests for one_c_piecework_export."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.models import (
    Item,
    ProductionManufacture,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SyncLink,
)
from app.services import one_c_piecework_export as exporter
from app.services.one_c_document_numbers import piecework_number


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_item(db, *, code: str, ref1c: str | None = None) -> Item:
    it = Item(
        item_code=code,
        item_name=f"Item {code}",
        item_article=code,
        item_ref1c=ref1c,
        unit="шт",
        stock_qty=0,
        status="active",
    )
    db.add(it)
    db.flush()
    return it


def _mk_manufacture(
    db,
    item: Item,
    *,
    qty: float = 5.0,
    exported_ref1c: str | None = "manuf-basis-ref",
    status: str = "exported",
) -> ProductionManufacture:
    order = ProductionOrder(
        order_number=f"PO-{item.item_id}",
        order_date=datetime(2026, 5, 20),
        is_posted=True,
        deletion_mark=False,
        order_ref1c=f"order-ref-{item.item_id}",
    )
    db.add(order)
    db.flush()
    product = ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=qty,
        produced_qty=qty,
        remaining_qty=0,
    )
    db.add(product)
    db.flush()
    db.add(ProductionOrderLineState(
        product_id=product.product_id,
        status="produced",
        issue_status="not_requested",
    ))
    manufacture = ProductionManufacture(
        product_id=product.product_id,
        order_id=order.order_id,
        qty=qty,
        executor="Иванов",
        status=status,
        exported_ref1c=exported_ref1c,
    )
    db.add(manufacture)
    db.commit()
    return manufacture


class _FakeClient:
    def __init__(self, *, ref_key: str = "pw-ref-key", fail: bool = False) -> None:
        self.ref_key = ref_key
        self.fail = fail
        self.posts: list = []
        self.patches: list = []
        self.operations: list = []

    def post(self, entity, payload, **_):
        self.posts.append((entity, payload))
        if self.fail:
            raise RuntimeError("simulated 1C failure")
        return {"Ref_Key": self.ref_key}

    def patch(self, entity_ref, payload, **_):
        self.patches.append((entity_ref, payload))
        if self.fail:
            raise RuntimeError("simulated 1C failure")
        return {}

    def post_operation(self, operation_path):
        self.operations.append(operation_path)


def _stub_config(monkeypatch, *, base_url: str) -> None:
    monkeypatch.setattr(
        exporter,
        "_load_odata_config",
        lambda: {"base_url": base_url, "username": "u", "password": "p"},
    )


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------


def test_dry_run_returns_payload(db_session):
    db = db_session
    item = _mk_item(db, code="PW-ITEM", ref1c="item-ref-pw")
    m = _mk_manufacture(db, item, qty=3.0, exported_ref1c="basis-ref-abc")

    result = exporter.export_piecework_to_1c(
        db, [m.manufacture_id],
        operation_ref="op-ref-123",
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["entity"] == "Document_СдельныйНаряд"
    assert len(result["payloads"]) == 1
    payload = result["payloads"][0]["payload"]
    assert payload["Posted"] is False
    assert payload["Закрыт"] is True
    assert payload["ДатаЗакрытия"] == payload["Date"]
    assert payload["ДокументОснование"] == "basis-ref-abc"
    assert payload["ДокументОснование_Type"] == "StandardODATA.Document_СборкаЗапасов"
    assert payload["ЗаказНаПроизводство_Key"] == f"order-ref-{item.item_id}"
    assert payload["Операции"][0]["Операция_Key"] == "op-ref-123"
    assert payload["Операции"][0]["КоличествоПлан"] == 3.0
    assert payload["Операции"][0]["КоличествоФакт"] == 3.0
    assert payload["Операции"][0]["ЗаказНаПроизводство_Key"] == f"order-ref-{item.item_id}"


def test_basis_type_is_document_sborka(db_session):
    db = db_session
    item = _mk_item(db, code="PW-BASIS", ref1c="item-ref-basis")
    m = _mk_manufacture(db, item, exported_ref1c="sborka-ref-999")

    result = exporter.export_piecework_to_1c(
        db, [m.manufacture_id],
        operation_ref="op-ref",
        dry_run=True,
    )

    payload = result["payloads"][0]["payload"]
    assert payload["ДокументОснование"] == "sborka-ref-999"
    assert payload["ДокументОснование_Type"] == "StandardODATA.Document_СборкаЗапасов"


def test_norm_and_price_computed(db_session):
    db = db_session
    item = _mk_item(db, code="PW-NORM", ref1c="item-ref-norm")
    m = _mk_manufacture(db, item, qty=4.0, exported_ref1c="ref-norm")

    result = exporter.export_piecework_to_1c(
        db, [m.manufacture_id],
        operation_ref="op-ref",
        time_norm=0.5,
        price=10.0,
        dry_run=True,
    )

    op = result["payloads"][0]["payload"]["Операции"][0]
    assert op["НормаВремени"] == 0.5
    assert op["Расценка"] == 10.0
    assert op["Нормочасы"] == pytest.approx(2.0)
    assert op["Стоимость"] == pytest.approx(40.0)


def test_optional_org_and_unit_in_payload(db_session):
    db = db_session
    item = _mk_item(db, code="PW-ORG", ref1c="item-ref-org")
    m = _mk_manufacture(db, item, exported_ref1c="ref-org")

    result = exporter.export_piecework_to_1c(
        db, [m.manufacture_id],
        operation_ref="op-ref",
        organization_ref="org-ref-abc",
        structural_unit_ref="unit-ref-abc",
        dry_run=True,
    )

    payload = result["payloads"][0]["payload"]
    assert payload["Организация_Key"] == "org-ref-abc"
    assert payload["СтруктурнаяЕдиница_Key"] == "unit-ref-abc"
    assert payload["Операции"][0]["СтруктурнаяЕдиница_Key"] == "unit-ref-abc"


# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------


def test_skips_manufacture_without_exported_ref1c(db_session):
    db = db_session
    item = _mk_item(db, code="PW-SKIP", ref1c="item-ref-skip")
    m = _mk_manufacture(db, item, exported_ref1c=None, status="draft")

    result = exporter.export_piecework_to_1c(
        db, [m.manufacture_id],
        operation_ref="op-ref",
        dry_run=True,
    )

    assert result["manufactures_eligible"] == 0
    assert len(result["skipped_rows"]) == 1
    assert "exported_ref1c" in result["skipped_rows"][0]["reason"]


def test_skips_cancelled_manufacture(db_session):
    db = db_session
    item = _mk_item(db, code="PW-CANCEL", ref1c="item-ref-cancel")
    m = _mk_manufacture(db, item, status="cancelled", exported_ref1c="ref-cancel")

    result = exporter.export_piecework_to_1c(
        db, [m.manufacture_id],
        operation_ref="op-ref",
        dry_run=True,
    )

    assert result["manufactures_eligible"] == 0
    assert len(result["skipped_rows"]) == 1


def test_skips_missing_item_ref1c(db_session):
    db = db_session
    item = _mk_item(db, code="PW-NOREF", ref1c=None)
    m = _mk_manufacture(db, item, exported_ref1c="ref-noref")

    result = exporter.export_piecework_to_1c(
        db, [m.manufacture_id],
        operation_ref="op-ref",
        dry_run=True,
    )

    assert result["manufactures_eligible"] == 0
    assert len(result["skipped_rows"]) == 1


# ---------------------------------------------------------------------------
# Idempotency — already linked
# ---------------------------------------------------------------------------


def test_already_linked_not_re_sent(db_session):
    db = db_session
    item = _mk_item(db, code="PW-IDEM", ref1c="item-ref-idem")
    m = _mk_manufacture(db, item, exported_ref1c="ref-idem")

    db.add(SyncLink(
        source_doctype="piecework",
        source_id=m.manufacture_id,
        target_entity="Document_СдельныйНаряд",
        target_number=piecework_number(db, m),
        payload_hash="hash",
        target_ref_key="existing-pw-ref",
        status="success",
    ))
    db.commit()

    result = exporter.export_piecework_to_1c(
        db, [m.manufacture_id],
        operation_ref="op-ref",
        dry_run=True,
    )

    assert result["manufactures_eligible"] == 0
    assert result["manufactures_already_linked"] == 1


# ---------------------------------------------------------------------------
# Live POST (monkeypatched client)
# ---------------------------------------------------------------------------


def test_live_post_creates_sync_link(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="PW-LIVE", ref1c="item-ref-live")
    m = _mk_manufacture(db, item, exported_ref1c="ref-live")

    fake = _FakeClient(ref_key="pw-created-ref")
    _stub_config(monkeypatch, base_url="http://mtzw7/unf_demo/odata/standard.odata")
    monkeypatch.setattr(exporter, "_create_odata_client", lambda *a, **kw: fake)

    result = exporter.export_piecework_to_1c(
        db, [m.manufacture_id],
        operation_ref="op-ref-live",
        dry_run=False,
    )

    assert result["manufactures_created"] == 1
    assert result["manufactures_error"] == 0
    assert len(fake.posts) == 1
    assert fake.posts[0][0] == "Document_СдельныйНаряд"
    assert fake.operations == [
        "Document_СдельныйНаряд(guid'pw-created-ref')/Post?PostingModeOperational=true"
    ]
    assert len(fake.patches) == 1
    assert fake.patches[0][0] == "Document_СдельныйНаряд(guid'pw-created-ref')"
    assert fake.patches[0][1]["Date"] == fake.posts[0][1]["Date"]
    assert fake.patches[0][1]["ДатаЗакрытия"] == fake.posts[0][1]["Date"]

    link = db.query(SyncLink).filter_by(
        source_doctype="piecework",
        source_id=m.manufacture_id,
        target_entity="Document_СдельныйНаряд",
    ).one()
    assert link.status == "success"
    assert link.target_ref_key == "pw-created-ref"


def test_existing_error_link_with_ref_patches_not_posts_duplicate(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="PW-RETRY", ref1c="item-ref-retry")
    m = _mk_manufacture(db, item, exported_ref1c="ref-retry")

    db.add(SyncLink(
        source_doctype="piecework",
        source_id=m.manufacture_id,
        target_entity="Document_СдельныйНаряд",
        target_number=piecework_number(db, m),
        payload_hash="old-hash",
        target_ref_key="existing-ref",
        status="error",
        last_error="post failed after create",
    ))
    db.commit()

    fake = _FakeClient(ref_key="new-ref-should-not-be-used")
    _stub_config(monkeypatch, base_url="http://mtzw7/unf_demo/odata/standard.odata")
    monkeypatch.setattr(exporter, "_create_odata_client", lambda *a, **kw: fake)

    result = exporter.export_piecework_to_1c(
        db, [m.manufacture_id],
        operation_ref="op-ref-retry",
        dry_run=False,
    )

    assert result["manufactures_created"] == 1
    assert fake.posts == []
    assert len(fake.patches) == 2
    assert fake.patches[0][0] == "Document_СдельныйНаряд(guid'existing-ref')"
    assert fake.operations == [
        "Document_СдельныйНаряд(guid'existing-ref')/Post?PostingModeOperational=true"
    ]
    assert fake.patches[1][0] == "Document_СдельныйНаряд(guid'existing-ref')"
    link = db.query(SyncLink).filter_by(
        source_doctype="piecework",
        source_id=m.manufacture_id,
        target_entity="Document_СдельныйНаряд",
    ).one()
    assert link.status == "success"
    assert link.target_ref_key == "existing-ref"


def test_live_post_error_recorded_in_sync_link(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="PW-ERR", ref1c="item-ref-err")
    m = _mk_manufacture(db, item, exported_ref1c="ref-err")

    fake = _FakeClient(fail=True)
    _stub_config(monkeypatch, base_url="http://mtzw7/unf_demo/odata/standard.odata")
    monkeypatch.setattr(exporter, "_create_odata_client", lambda *a, **kw: fake)

    result = exporter.export_piecework_to_1c(
        db, [m.manufacture_id],
        operation_ref="op-ref-err",
        dry_run=False,
    )

    assert result["manufactures_error"] == 1
    assert result["status"] == "partial_error"

    link = db.query(SyncLink).filter_by(
        source_doctype="piecework",
        source_id=m.manufacture_id,
        target_entity="Document_СдельныйНаряд",
    ).one()
    assert link.status == "error"

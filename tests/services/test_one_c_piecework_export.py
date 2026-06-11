"""Tests for one_c_piecework_export."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.models import (
    DefaultSpecification,
    Employee,
    Item,
    Operation,
    ProductionManufacture,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    Specification,
    SpecOperation,
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

    def _make_request(self, endpoint, params=None, **_):
        if "Catalog_Бригады" in endpoint:
            return {
                "Состав": [
                    {"LineNumber": "1", "Сотрудник_Key": "member-ref-1"},
                    {"LineNumber": "2", "Сотрудник_Key": "member-ref-2"},
                ]
            }
        return {}


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


def test_default_spec_operation_used_when_product_spec_missing(db_session):
    db = db_session
    item = _mk_item(db, code="PW-DEF-SPEC", ref1c="item-ref-def-spec")
    spec = Specification(spec_name="Default piecework spec", spec_ref1c="spec-ref-def")
    operation = Operation(operation_ref1c="op-ref-def", operation_name="Сборка", time_norm=0.25, operation_price=30)
    db.add_all([spec, operation])
    db.flush()
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db.add(SpecOperation(spec_id=spec.spec_id, operation_id=operation.operation_id, time_norm=0.75))
    db.commit()
    m = _mk_manufacture(db, item, qty=2.0, exported_ref1c="ref-def-spec")

    result = exporter.export_piecework_to_1c(db, [m.manufacture_id], dry_run=True)

    op = result["payloads"][0]["payload"]["Операции"][0]
    assert op["Операция_Key"] == "op-ref-def"
    assert op["Спецификация_Key"] == "spec-ref-def"
    assert op["НормаВремени"] == 0.75
    assert op["Расценка"] == 30.0
    assert op["Нормочасы"] == pytest.approx(1.5)
    assert op["Стоимость"] == pytest.approx(60.0)


def test_all_spec_operations_are_exported_to_piecework(db_session):
    db = db_session
    item = _mk_item(db, code="PW-MULTI-OPS", ref1c="item-ref-multi-ops")
    spec = Specification(spec_name="Multi operation spec", spec_ref1c="spec-ref-multi")
    drill = Operation(operation_ref1c="op-ref-drill", operation_name="Сверловка", time_norm=0.01)
    weld = Operation(operation_ref1c="op-ref-weld", operation_name="Сварка", time_norm=0.10)
    db.add_all([spec, drill, weld])
    db.flush()
    db.add_all([
        SpecOperation(spec_id=spec.spec_id, operation_id=drill.operation_id, time_norm=0.01),
        SpecOperation(spec_id=spec.spec_id, operation_id=weld.operation_id, time_norm=0.10),
    ])
    db.commit()
    m = _mk_manufacture(db, item, qty=8.0, exported_ref1c="ref-multi-ops")
    m.product.spec_id = spec.spec_id
    db.commit()

    result = exporter.export_piecework_to_1c(db, [m.manufacture_id], dry_run=True)

    operations = result["payloads"][0]["payload"]["Операции"]
    assert [op["LineNumber"] for op in operations] == [1, 2]
    assert [op["Операция_Key"] for op in operations] == ["op-ref-drill", "op-ref-weld"]
    assert [op["КоличествоПлан"] for op in operations] == [8.0, 8.0]
    assert [op["КоличествоФакт"] for op in operations] == [8.0, 8.0]
    assert [op["НормаВремени"] for op in operations] == [0.01, 0.10]
    assert [op["Нормочасы"] for op in operations] == [pytest.approx(0.08), pytest.approx(0.8)]


def test_zero_price_is_not_sent_as_piecework_rate(db_session):
    db = db_session
    item = _mk_item(db, code="PW-NO-PRICE", ref1c="item-ref-no-price")
    m = _mk_manufacture(db, item, qty=4.0, exported_ref1c="ref-no-price")

    result = exporter.export_piecework_to_1c(
        db, [m.manufacture_id],
        operation_ref="op-ref",
        time_norm=0.5,
        price=0.0,
        dry_run=True,
    )

    op = result["payloads"][0]["payload"]["Операции"][0]
    assert "Расценка" not in op
    assert "Стоимость" not in op
    assert op["Нормочасы"] == pytest.approx(2.0)


def test_brigade_executor_uses_brigade_type_and_composition(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="PW-BRIGADE", ref1c="item-ref-brigade")
    m = _mk_manufacture(db, item, qty=4.0, exported_ref1c="basis-ref-brigade")
    db.add(
        Employee(
            employee_ref1c="brigade-ref",
            employee_type="brigade",
            employee_code="000000022",
            employee_name="Иванов",
            deletion_mark=False,
        )
    )
    db.commit()

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(ref_key="pw-brigade-ref")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_piecework_to_1c(
        db,
        [m.manufacture_id],
        operation_ref="op-ref",
        dry_run=False,
        allow_production=True,
    )

    assert result["manufactures_created"] == 1
    payload = fake.posts[0][1]
    assert payload["Исполнитель"] == exporter.EMPTY_REF1C
    assert payload["ПоложениеИсполнителя"] == "ВТабличнойЧасти"
    assert payload["Операции"][0]["Исполнитель"] == "brigade-ref"
    assert payload["Операции"][0]["Исполнитель_Type"] == "StandardODATA.Catalog_Бригады"


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


def test_single_executor_is_written_to_operation_rows(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="PW-HEADER-EXEC", ref1c="item-ref-header-exec")
    m = _mk_manufacture(db, item, exported_ref1c="ref-header-exec")
    db.add(
        Employee(
            employee_ref1c="employee-ref",
            employee_type="employee",
            employee_code="000000023",
            employee_name="Иванов",
            deletion_mark=False,
        )
    )
    db.commit()

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(ref_key="pw-header-exec-ref")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_piecework_to_1c(
        db,
        [m.manufacture_id],
        operation_ref="op-ref",
        dry_run=False,
        allow_production=True,
    )

    assert result["manufactures_created"] == 1
    payload = fake.posts[0][1]
    assert payload["Исполнитель"] == exporter.EMPTY_REF1C
    assert payload["ПоложениеИсполнителя"] == "ВТабличнойЧасти"
    assert payload["Операции"][0]["Исполнитель"] == "employee-ref"
    assert payload["Операции"][0]["Исполнитель_Type"] == "StandardODATA.Catalog_Сотрудники"


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
    assert len(fake.patches) == 2
    assert fake.patches[0][0] == "Document_СдельныйНаряд(guid'pw-created-ref')"
    assert fake.patches[0][1]["Date"] == fake.posts[0][1]["Date"]
    assert fake.patches[0][1]["ДатаЗакрытия"] == fake.posts[0][1]["Date"]
    assert fake.patches[1][0] == f"Document_ЗаказНаПроизводство(guid'order-ref-{item.item_id}')"
    assert fake.patches[1][1]["СостояниеЗаказа_Key"] == exporter.DONE_STATE_KEY

    db.refresh(m.order)
    assert m.order.order_state_key == exporter.DONE_STATE_KEY
    assert m.order.order_state_name == "Завершен"

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
    assert len(fake.patches) == 3
    assert fake.patches[0][0] == "Document_СдельныйНаряд(guid'existing-ref')"
    assert fake.operations == [
        "Document_СдельныйНаряд(guid'existing-ref')/Post?PostingModeOperational=true"
    ]
    assert fake.patches[1][0] == "Document_СдельныйНаряд(guid'existing-ref')"
    assert fake.patches[2][0] == f"Document_ЗаказНаПроизводство(guid'order-ref-{item.item_id}')"
    assert fake.patches[2][1]["СостояниеЗаказа_Key"] == exporter.DONE_STATE_KEY
    link = db.query(SyncLink).filter_by(
        source_doctype="piecework",
        source_id=m.manufacture_id,
        target_entity="Document_СдельныйНаряд",
    ).one()
    assert link.status == "success"
    assert link.target_ref_key == "existing-ref"
    db.refresh(m.order)
    assert m.order.order_state_key == exporter.DONE_STATE_KEY


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

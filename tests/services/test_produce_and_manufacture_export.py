"""Tests for produce_line + one_c_manufacture_export."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import models
from app.models import (
    DefaultSpecification,
    Employee,
    Item,
    Operation,
    ProductionManufacture,
    ProductionManufactureOperation,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionStage,
    PhysicalImportBatch,
    LedgerGeneration,
    PlanningRun,
    PlanningTruthState,
    MrpRequirement,
    SpecComponent,
    SpecOperation,
    Specification,
    SyncLink,
    WorkshopWarehouseBinding,
)
from app.routers.production_control import ExportPieceworkPayload
from app.services import one_c_manufacture_export as exporter
from app.services import one_c_piecework_export as piecework_exporter
from app.services.item_ledger.assembly_output_persistence import (
    materialize_assembly_output_allocations,
)
from app.services.item_ledger.ingest import pull_recorder_movements
from app.services import production_control_production_flow as flow
from app.services.production_control_production_flow import produce_line, rollback_local_manufacture


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_export_piecework_payload_does_not_require_operation_ref():
    payload = ExportPieceworkPayload(manufacture_ids=[1], dry_run=False)

    assert payload.operation_ref is None
    assert payload.manufacture_ids == [1]


def _mk_item(db, *, code: str, ref1c: str | None = None) -> Item:
    it = Item(
        item_code=code,
        item_name=f"Item {code}",
        item_article=code,
        item_ref1c=ref1c,
        unit=f"unit-ref-{code}",
        stock_qty=0,
        status="active",
    )
    db.add(it)
    db.flush()
    return it


def _mk_product(db, item: Item, *, qty: float = 10.0) -> ProductionProduct:
    order = ProductionOrder(
        order_number=f"O-{item.item_id}",
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
        produced_qty=0,
        remaining_qty=qty,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="ready",
            issue_status="posted",
        )
    )
    db.add(
        ProductionMaterialIssue(
            document_number=f"MI-{product.product_id}",
            product_id=product.product_id,
            order_id=order.order_id,
            status="posted",
            direction="issue",
            warehouse_ref1c="workshop-ref",
            source_warehouse_ref1c="source-ref",
        )
    )
    db.commit()
    return product


def _attach_current_mrp_lineage(db, product: ProductionProduct) -> None:
    cutoff = datetime(2026, 5, 20, tzinfo=timezone.utc)
    physical = PhysicalImportBatch(
        batch_key=f"manufacture-lineage-{product.product_id}",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    generation = LedgerGeneration(
        generation_key=f"manufacture-lineage-{product.product_id}",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
        physical_import_batch=physical,
        algorithm_version="tests/current-lineage",
    )
    db.add(generation)
    db.flush()
    db.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        ledger_generation_id=generation.id,
        ledger_cutoff=cutoff,
        active_freeze_version=1,
        period_from=date(2026, 5, 1),
        period_to=date(2026, 5, 31),
        config_snapshot={},
        pinned=True,
        fixed_at=cutoff,
        finished_at=cutoff,
    )
    db.add(run)
    db.flush()
    requirement = MrpRequirement(
        run_id=run.run_id,
        item_id=product.item_id,
        freeze_version=1,
        planning_stock_pool="default",
        total_required_qty=product.quantity,
        net_required_qty=product.quantity,
        period_from=date(2026, 5, 1),
        period_to=date(2026, 5, 31),
    )
    db.add(requirement)
    db.flush()
    product.order.source_run_id = run.run_id
    product.ledger_generation_id = generation.id
    product.source_mrp_requirement_id = requirement.id
    db.commit()


def _stock_kit_on_workshop(db, product: ProductionProduct, component: Item, qty: float) -> None:
    """Add a delivered line to the product's posted issue so the produce
    guard sees the kit reserved on the workshop."""
    issue = (
        db.query(ProductionMaterialIssue)
        .filter_by(product_id=product.product_id)
        .order_by(ProductionMaterialIssue.issue_id.desc())
        .first()
    )
    db.add(
        ProductionMaterialIssueLine(
            issue_id=issue.issue_id,
            component_item_id=component.item_id,
            required_qty=qty,
            issued_qty=qty,
            line_status="issued",
        )
    )
    db.commit()


class _FakeClient:
    def __init__(
        self,
        *,
        ref_key: str = "manuf-ref-key",
        fail: bool = False,
        parent_order_doc: dict | None = None,
    ) -> None:
        self.ref_key = ref_key
        self.fail = fail
        self.parent_order_doc = parent_order_doc or {}
        self.posts: list = []
        self.patches: list = []
        self.gets: list = []
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

    def _make_request(self, endpoint, params=None, **_):
        self.gets.append((endpoint, params or {}))
        return dict(self.parent_order_doc)

    def post_operation(self, operation_path):
        self.operations.append(operation_path)


class _PostFailsAfterCreateClient(_FakeClient):
    def post_operation(self, *_args, **_kwargs):
        raise RuntimeError("posting failed after create")


def _stub_config(monkeypatch, *, base_url: str) -> None:
    monkeypatch.setattr(
        exporter,
        "_load_odata_config",
        lambda: {"base_url": base_url, "username": "u", "password": "p"},
    )


# ---------------------------------------------------------------------------
# produce_line
# ---------------------------------------------------------------------------


def test_produce_full_creates_command_without_recording_fact(db_session):
    db = db_session
    item = _mk_item(db, code="PRD-FULL", ref1c="ref-prd-full")
    product = _mk_product(db, item, qty=5.0)

    result = produce_line(db, product.product_id, qty=5, executor="иван")
    assert result["status"] == "pending_1c_fact"
    assert result["qty"] == 5.0
    assert result["produced_qty_total"] == 0.0
    assert result["remaining_qty"] == 5.0
    assert result["commanded_qty_total"] == 5.0
    assert result["command_remaining_qty"] == 0.0
    assert result["fact_pending"] is True
    assert result["line_status"] == "ready"

    db.refresh(product)
    assert float(product.produced_qty) == 0.0
    assert float(product.remaining_qty) == 5.0

    state = (
        db.query(ProductionOrderLineState)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert state.status == "ready"

    manufacture = (
        db.query(ProductionManufacture)
        .filter_by(product_id=product.product_id)
        .one()
    )
    assert float(manufacture.qty) == 5.0
    assert manufacture.executor == "иван"
    assert manufacture.status == "draft"


def test_produce_gate_and_response_ignore_corrupt_remaining_cache(db_session):
    db = db_session
    item = _mk_item(db, code="PRD-CORRUPT", ref1c="ref-prd-corrupt")
    product = _mk_product(db, item, qty=5.0)
    product.produced_qty = 2
    product.remaining_qty = 999
    db.commit()

    with pytest.raises(ValueError, match="остаток"):
        produce_line(db, product.product_id, qty=4)

    result = produce_line(db, product.product_id, qty=1)
    assert result["produced_qty_total"] == 2.0
    assert result["remaining_qty"] == 3.0
    assert result["command_remaining_qty"] == 2.0


def test_produce_line_saves_operation_executors(db_session):
    db = db_session
    item = _mk_item(db, code="PRD-OP-EXEC", ref1c="ref-prd-op-exec")
    product = _mk_product(db, item, qty=5.0)
    employee = Employee(
        employee_ref1c="employee-op-ref",
        employee_type="employee",
        employee_code="000000031",
        employee_name="Оператор операции",
        deletion_mark=False,
    )
    operation = Operation(operation_ref1c="op-exec-ref", operation_name="Сборка", time_norm=0.25)
    spec = Specification(spec_name="Operation executor spec")
    db.add_all([employee, operation, spec])
    db.flush()
    product.spec_id = spec.spec_id
    spec_operation = SpecOperation(spec_id=spec.spec_id, operation_id=operation.operation_id, time_norm=0.25)
    db.add(spec_operation)
    db.commit()

    result = produce_line(
        db,
        product.product_id,
        qty=5,
        operation_executors=[{
            "line_number": 1,
            "spec_operation_id": spec_operation.spec_operation_id,
            "operation_id": operation.operation_id,
            "employee_ref1c": employee.employee_ref1c,
        }],
    )

    row = (
        db.query(ProductionManufactureOperation)
        .filter_by(manufacture_id=result["manufacture_id"])
        .one()
    )
    assert row.spec_operation_id == spec_operation.spec_operation_id
    assert row.operation_id == operation.operation_id
    assert row.employee_ref1c == "employee-op-ref"
    assert row.employee_name == "Оператор операции"


def test_partial_commands_fill_executable_quantity_without_recording_fact(db_session):
    db = db_session
    item = _mk_item(db, code="PRD-PART", ref1c="ref-prd-part")
    product = _mk_product(db, item, qty=7.0)

    r1 = produce_line(db, product.product_id, qty=3, executor="op1")
    assert r1["line_status"] == "ready"
    assert r1["command_remaining_qty"] == 4.0

    r2 = produce_line(db, product.product_id, qty=4, executor="op2")
    assert r2["line_status"] == "ready"
    assert r2["command_remaining_qty"] == 0.0
    db.refresh(product)
    assert float(product.produced_qty) == 0.0
    assert float(product.remaining_qty) == 7.0

    # Two manufactures, total 7.
    mans = (
        db.query(ProductionManufacture)
        .filter_by(product_id=product.product_id)
        .order_by(ProductionManufacture.manufacture_id.asc())
        .all()
    )
    assert [float(m.qty) for m in mans] == [3.0, 4.0]


def test_produce_more_than_uncommanded_quantity_is_rejected(db_session):
    db = db_session
    item = _mk_item(db, code="PRD-OVER", ref1c="ref-prd-over")
    product = _mk_product(db, item, qty=2.0)

    with pytest.raises(ValueError, match="исполнительные документы"):
        produce_line(db, product.product_id, qty=3)

    db.refresh(product)
    assert float(product.quantity) == 2.0
    assert float(product.produced_qty) == 0.0
    assert float(product.remaining_qty) == 2.0
    assert (
        db.query(ProductionManufacture).filter_by(product_id=product.product_id).count()
        == 0
    )


def test_produce_zero_or_negative_raises(db_session):
    db = db_session
    item = _mk_item(db, code="PRD-ZERO", ref1c="ref-prd-zero")
    product = _mk_product(db, item, qty=4.0)
    for bad in [0, -1, -0.5]:
        with pytest.raises(ValueError):
            produce_line(db, product.product_id, qty=bad)


def test_produce_unknown_product_raises(db_session):
    with pytest.raises(ValueError, match="не найдена"):
        produce_line(db_session, 999_999, qty=1)


def test_produce_requires_posted_material_issue(db_session):
    db = db_session
    item = _mk_item(db, code="PRD-NOMOVE", ref1c="ref-prd-nomove")
    product = _mk_product(db, item, qty=4.0)
    db.query(ProductionMaterialIssue).filter_by(product_id=product.product_id).delete()
    state = db.query(ProductionOrderLineState).filter_by(product_id=product.product_id).one()
    state.issue_status = "not_requested"
    db.commit()

    with pytest.raises(ValueError, match="перемещения материалов"):
        produce_line(db, product.product_id, qty=1)

    db.refresh(product)
    assert float(product.produced_qty) == 0
    assert float(product.remaining_qty) == 4
    assert db.query(ProductionManufacture).filter_by(product_id=product.product_id).count() == 0


def test_produce_refreshes_1c_spec_before_reservation_guard(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="PRD-SPEC-REFRESH", ref1c="ref-prd-spec-refresh")
    component = _mk_item(db, code="COMP-SPEC-REFRESH", ref1c="ref-comp-spec-refresh")
    product = _mk_product(db, item, qty=16.0)

    spec = Specification(
        spec_code="SPEC-REFRESH",
        spec_name="Spec refresh",
        spec_ref1c="spec-refresh-ref",
    )
    db.add(spec)
    db.flush()
    product.spec_id = spec.spec_id
    db.add(
        SpecComponent(
            spec_id=spec.spec_id,
            item_id=component.item_id,
            quantity=0.043,
            component_type="Материал",
        )
    )
    _stock_kit_on_workshop(db, product, component, qty=0.656)

    def fake_refresh(db_arg, product_arg):
        assert int(product_arg.product_id) == int(product.product_id)
        row = (
            db_arg.query(SpecComponent)
            .filter_by(spec_id=spec.spec_id, item_id=component.item_id)
            .one()
        )
        row.quantity = 0.041
        db_arg.flush()
        return True

    monkeypatch.setattr(flow, "_refresh_product_spec_from_1c", fake_refresh)

    result = produce_line(db, product.product_id, qty=16.0)

    assert result["status"] == "pending_1c_fact"
    assert result["line_status"] == "ready"
    db.refresh(product)
    assert float(product.produced_qty) == 0.0
    assert float(product.remaining_qty) == 16.0


# ---------------------------------------------------------------------------
# one_c_manufacture_export
# ---------------------------------------------------------------------------


def test_dry_run_returns_payload_with_order_ref(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="EXP-ITEM", ref1c="item-ref-exp")
    product = _mk_product(db, item, qty=4)
    r = produce_line(db, product.product_id, qty=4)
    mid = r["manufacture_id"]

    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("Network must not be touched in dry-run"),
    )

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=True)
    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["manufactures_eligible"] == 1
    [pl] = result["payloads"]
    payload = pl["payload"]
    assert payload["Posted"] is False
    assert payload["Number"].startswith("MF")
    # Manufacture is linked to the parent production order through the UNF
    # dedicated field. Its generic basis type does not accept production orders.
    assert payload["ЗаказНаПроизводство_Key"] == "order-ref-{}".format(item.item_id)
    assert "ДокументОснование" not in payload
    assert "ДокументОснование_Type" not in payload
    [prod_row] = payload["Продукция"]
    assert prod_row["Номенклатура_Key"] == "item-ref-exp"
    assert prod_row["ЕдиницаИзмерения"] == item.unit
    assert prod_row["ЕдиницаИзмерения_Type"] == "StandardODATA.Catalog_КлассификаторЕдиницИзмерения"
    assert float(prod_row["Количество"]) == 4.0
    assert (
        db.query(SyncLink).filter_by(source_doctype="manufacture").count() == 0
    )


def test_manufacture_payload_header_uses_product_structural_unit():
    entry = exporter.ManufactureExportEntry(
        manufacture_id=1,
        product_id=2,
        order_id=3,
        order_ref1c="order-ref",
        item_ref1c="item-ref",
        item_name="Item",
        item_article="ART",
        unit_ref1c="unit-ref",
        qty=4,
        material_structural_unit_ref1c="materials-ref",
        product_structural_unit_ref1c="products-ref",
        number="MF000000001",
    )

    payload = exporter._build_header_payload(entry, {})

    assert payload["СтруктурнаяЕдиница_Key"] == exporter.DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C
    assert payload["СтруктурнаяЕдиницаПродукции_Key"] == "products-ref"
    assert payload["СтруктурнаяЕдиницаЗапасов_Key"] == "materials-ref"
    assert payload["Продукция"][0]["СтруктурнаяЕдиница_Key"] == "products-ref"
    assert (
        payload["Продукция"][0]["ПодразделениеЗавершающегоЭтапа_Key"]
        == exporter.DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C
    )


def test_export_inherits_warehouses_from_parent_1c_order_when_local_binding_missing(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="EXP-INHERIT", ref1c="item-ref-inherit")
    component = _mk_item(db, code="EXP-INHERIT-C", ref1c="component-ref-inherit")
    spec = Specification(spec_name="Spec inherit", spec_ref1c="spec-ref-inherit")
    stage = ProductionStage(stage_name="Stage inherit", stage_ref1c="stage-ref")
    completion_stage = ProductionStage(
        stage_name="Завершение производства",
        stage_ref1c="completion-stage-ref",
    )
    db.add(spec)
    db.add(stage)
    db.add(completion_stage)
    db.flush()
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db.add(
        SpecComponent(
            spec_id=spec.spec_id,
            item_id=component.item_id,
            quantity=2,
            stage_id=stage.stage_id,
        )
    )
    product = _mk_product(db, item, qty=5)
    # Mirror a 1C-synced line without spec/workshop: local binding cannot be
    # resolved, but the already-linked parent order has authoritative units.
    product.spec_id = None
    state = db.query(ProductionOrderLineState).filter_by(product_id=product.product_id).one()
    state.workshop_id = None
    _stock_kit_on_workshop(db, product, component, 10)
    mid = produce_line(db, product.product_id, qty=5, executor="operator")["manufacture_id"]
    db.commit()

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(
        ref_key="inherit-manuf-ref",
        parent_order_doc={
            "СтруктурнаяЕдиницаРезерв_Key": "parent-reserve-ref",
            "СтруктурнаяЕдиницаПродукции_Key": "parent-product-ref",
            "Продукция": [],
            "Запасы": [],
        },
    )
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)

    assert result["manufactures_created"] == 1
    assert fake.gets
    payload = fake.posts[0][1]
    assert payload["СтруктурнаяЕдиница_Key"] == exporter.DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C
    assert payload["СтруктурнаяЕдиницаПродукции_Key"] == "parent-product-ref"
    assert payload["СтруктурнаяЕдиницаЗапасов_Key"] == "parent-reserve-ref"
    assert payload["Продукция"][0]["СтруктурнаяЕдиница_Key"] == "parent-product-ref"
    assert (
        payload["Продукция"][0]["ПодразделениеЗавершающегоЭтапа_Key"]
        == exporter.DEFAULT_PRODUCTION_STRUCTURAL_UNIT_REF1C
    )
    assert payload["Продукция"][0]["Спецификация_Key"] == "spec-ref-inherit"
    assert payload["Запасы"][0]["Номенклатура_Key"] == "component-ref-inherit"
    assert payload["Запасы"][0]["Количество"] == 10.0
    assert payload["Запасы"][0]["Спецификация_Key"] == "spec-ref-inherit"
    assert payload["Запасы"][0]["СтруктурнаяЕдиница_Key"] == "parent-reserve-ref"
    assert "Этап_Key" not in payload["Запасы"][0]
    assert payload["ВыполненныеЭтапы"] == [
        {"LineNumber": 1, "КлючСвязи": 1, "Этап_Key": "stage-ref"},
        {"LineNumber": 2, "КлючСвязи": 1, "Этап_Key": "completion-stage-ref"},
    ]


def test_export_uses_parent_order_product_warehouse_over_local_binding(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="EXP-PARENT-WH", ref1c="item-ref-parent-wh")
    component = _mk_item(db, code="EXP-PARENT-WH-C", ref1c="component-ref-parent-wh")
    spec = Specification(spec_name="Spec parent warehouse", spec_ref1c="spec-ref-parent-wh")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=1))
    product = _mk_product(db, item, qty=3)
    state = db.query(ProductionOrderLineState).filter_by(product_id=product.product_id).one()
    state.workshop_id = 77
    db.add(
        WorkshopWarehouseBinding(
            workshop_id=77,
            warehouse_ref1c="local-material-ref",
            production_warehouse_ref1c="local-product-ref",
        )
    )
    _stock_kit_on_workshop(db, product, component, 3)
    mid = produce_line(db, product.product_id, qty=3, executor="operator")["manufacture_id"]
    db.commit()

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(
        ref_key="parent-wh-manuf-ref",
        parent_order_doc={
            "СтруктурнаяЕдиницаРезерв_Key": "parent-reserve-ref",
            "СтруктурнаяЕдиницаПродукции_Key": "parent-product-ref",
            "Продукция": [],
            "Запасы": [],
        },
    )
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)

    assert result["manufactures_created"] == 1
    payload = fake.posts[0][1]
    assert payload["СтруктурнаяЕдиницаПродукции_Key"] == "parent-product-ref"
    assert payload["Продукция"][0]["СтруктурнаяЕдиница_Key"] == "parent-product-ref"
    assert payload["СтруктурнаяЕдиницаЗапасов_Key"] == "local-material-ref"
    assert payload["Запасы"][0]["СтруктурнаяЕдиница_Key"] == "local-material-ref"


def test_export_uses_completion_stage_even_without_local_spec_stages(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="EXP-STAGE", ref1c="item-ref-stage")
    component = _mk_item(db, code="EXP-STAGE-C", ref1c="component-ref-stage")
    spec = Specification(spec_name="Spec stage", spec_ref1c="spec-ref-stage")
    completion_stage = ProductionStage(
        stage_name="Завершение производства",
        stage_ref1c="completion-stage-ref",
    )
    db.add(spec)
    db.add(completion_stage)
    db.flush()
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db.add(
        SpecComponent(
            spec_id=spec.spec_id,
            item_id=component.item_id,
            quantity=1,
        )
    )
    product = _mk_product(db, item, qty=4)
    product.spec_id = None
    _stock_kit_on_workshop(db, product, component, 4)
    mid = produce_line(db, product.product_id, qty=4, executor="operator")["manufacture_id"]
    db.commit()

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(
        ref_key="stage-manuf-ref",
        parent_order_doc={
            "СтруктурнаяЕдиницаРезерв_Key": "parent-reserve-ref",
            "СтруктурнаяЕдиницаПродукции_Key": "parent-product-ref",
            "Продукция": [],
            "Запасы": [{"Этап_Key": "parent-stage-ref"}],
        },
    )
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)

    assert result["manufactures_created"] == 1
    payload = fake.posts[0][1]
    assert "Этап_Key" not in payload["Запасы"][0]
    assert payload["ВыполненныеЭтапы"] == [
        {"LineNumber": 1, "КлючСвязи": 1, "Этап_Key": "completion-stage-ref"},
    ]


def test_rollback_local_manufacture_restores_line(db_session):
    db = db_session
    item = _mk_item(db, code="ROLLBACK", ref1c="item-ref-rollback")
    product = _mk_product(db, item, qty=5)
    result = produce_line(db, product.product_id, qty=5, executor="operator")

    rolled_back = rollback_local_manufacture(db, result["manufacture_id"])

    assert rolled_back["status"] == "rolled_back"
    product = db.query(ProductionProduct).filter_by(product_id=product.product_id).one()
    assert float(product.produced_qty) == 0
    assert float(product.remaining_qty) == 5
    assert db.query(ProductionManufacture).filter_by(manufacture_id=result["manufacture_id"]).one_or_none() is None


def test_export_writes_into_configured_production_base(db_session, monkeypatch):
    """Go-live: демо-гард удалён — пишем в базу из настроек подключения.

    Предпросмотр остаётся только за dry_run: он не постит в 1С.
    """
    db = db_session
    item = _mk_item(db, code="EXP-GUARD", ref1c="item-ref-guard")
    product = _mk_product(db, item, qty=2)
    mid = produce_line(db, product.product_id, qty=2)["manufacture_id"]

    _stub_config(monkeypatch, base_url="http://erp-prod/odata/unf")
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    preview = exporter.export_manufactures_to_1c(db, [mid], dry_run=True)
    assert preview["payloads"]
    assert fake.posts == []

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)
    assert result["manufactures_created"] == 1


def test_successful_export_stamps_link_and_manufacture(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="EXP-OK", ref1c="item-ref-ok")
    product = _mk_product(db, item, qty=3)
    mid = produce_line(db, product.product_id, qty=3, executor="petrov")["manufacture_id"]

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    monkeypatch.setattr(
        exporter, "OData1CClient", lambda **_: _FakeClient(ref_key="be5ab6fe-manu-ok")
    )

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)
    assert result["status"] == "ok"
    assert result["manufactures_created"] == 1

    m = db.query(ProductionManufacture).filter_by(manufacture_id=mid).one()
    assert m.status == "exported"
    assert m.exported_ref1c == "be5ab6fe-manu-ok"
    assert m.exported_at is not None

    link = (
        db.query(SyncLink)
        .filter_by(
            source_doctype="manufacture",
            source_id=mid,
            target_entity=exporter.MANUFACTURE_ENTITY,
        )
        .one()
    )
    assert link.status == "success"
    assert link.target_ref_key == "be5ab6fe-manu-ok"


def test_failed_posting_keeps_created_ref_on_manufacture(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="EXP-POST-FAIL", ref1c="item-ref-post-fail")
    product = _mk_product(db, item, qty=3)
    mid = produce_line(db, product.product_id, qty=3, executor="petrov")["manufacture_id"]

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: _PostFailsAfterCreateClient(ref_key="created-but-not-posted"),
    )

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)

    assert result["status"] == "partial_error"
    assert result["manufactures_error"] == 1
    m = db.query(ProductionManufacture).filter_by(manufacture_id=mid).one()
    assert m.status == "error"
    assert m.exported_ref1c == "created-but-not-posted"
    assert m.exported_at is not None
    assert "posting failed after create" in m.export_error

    link = (
        db.query(SyncLink)
        .filter_by(
            source_doctype="manufacture",
            source_id=mid,
            target_entity=exporter.MANUFACTURE_ENTITY,
        )
        .one()
    )
    assert link.status == "error"
    assert link.target_ref_key == "created-but-not-posted"

    retry_client = _FakeClient(ref_key="should-not-create")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: retry_client)

    retry = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)

    assert retry["status"] == "ok"
    assert retry["manufactures_created"] == 1
    assert retry["manufactures_already_linked"] == 0
    assert retry_client.posts == []
    assert len(retry_client.patches) == 1
    assert retry_client.patches[0][0] == "Document_СборкаЗапасов(guid'created-but-not-posted')"
    m = db.query(ProductionManufacture).filter_by(manufacture_id=mid).one()
    assert m.status == "exported"
    assert m.exported_ref1c == "created-but-not-posted"
    assert m.export_error is None


def test_second_export_does_not_touch_successfully_exported_document(db_session, monkeypatch):
    """Contract «Что не делать» п.5: a successful sync_link means the document
    exists and is posted — a repeat must not Unpost/PATCH/Post it again."""
    db = db_session
    item = _mk_item(db, code="EXP-DUP", ref1c="item-ref-dup")
    product = _mk_product(db, item, qty=2)
    mid = produce_line(db, product.product_id, qty=2)["manufacture_id"]

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(ref_key="reuse-ref")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    exporter.export_manufactures_to_1c(db, [mid], dry_run=False)
    assert len(fake.posts) == 1
    operations_after_first = list(fake.operations)

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)
    assert result["manufactures_created"] == 0
    assert result["manufactures_error"] == 0
    assert result["manufactures_eligible"] == 0
    assert result["manufactures_already_linked"] == 1
    assert len(fake.posts) == 1
    assert fake.patches == []
    assert fake.operations == operations_after_first

    entry = result["entries"][0]
    assert entry["status"] == "existing"
    assert entry["target_ref_key"] == "reuse-ref"

    m = db.query(ProductionManufacture).filter_by(manufacture_id=mid).one()
    assert m.status == "exported"
    assert m.exported_ref1c == "reuse-ref"


def test_manufacture_payload_without_order_ref_raises_value_error():
    """Business invariant, not an assert: СборкаЗапасов needs its order basis."""
    entry = exporter.ManufactureExportEntry(
        manufacture_id=1,
        product_id=2,
        order_id=3,
        order_ref1c=None,
        item_ref1c="item-ref",
        item_name="Item",
        item_article="ART",
        unit_ref1c="unit-ref",
        qty=1,
        number="MF000000001",
    )

    with pytest.raises(ValueError, match="order_ref1c"):
        exporter._build_header_payload(entry, {})


# ---------------------------------------------------------------------------
# Resumable «Произвести»
# ---------------------------------------------------------------------------


def test_commanded_qty_counts_errored_manufacture_with_1c_document(db_session):
    db = db_session
    item = _mk_item(db, code="CMD-ERR", ref1c="item-ref-cmd-err")
    product = _mk_product(db, item, qty=5)
    mid = produce_line(db, product.product_id, qty=5)["manufacture_id"]
    m = db.query(ProductionManufacture).filter_by(manufacture_id=mid).one()
    m.status = "error"
    m.exported_ref1c = "assembly-created-ref"
    db.commit()

    totals = exporter.commanded_qty_by_product(db, [product.product_id])

    assert totals[product.product_id] == 5.0

    # ... while a local-only failure (no 1C document) frees the quantity again.
    m.exported_ref1c = None
    db.commit()
    assert exporter.commanded_qty_by_product(db, [product.product_id])[product.product_id] == 0.0


def test_repeat_produce_resumes_failed_assembly_instead_of_duplicating(db_session, monkeypatch):
    """A СборкаЗапасов created but not posted must be retried, never doubled."""
    db = db_session
    item = _mk_item(db, code="RESUME-ERR", ref1c="item-ref-resume-err")
    product = _mk_product(db, item, qty=5)
    mid = produce_line(db, product.product_id, qty=5)["manufacture_id"]

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: _PostFailsAfterCreateClient(ref_key="assembly-created-not-posted"),
    )
    failed = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)
    assert failed["manufactures_error"] == 1

    again = produce_line(db, product.product_id, qty=5)

    assert again["status"] == "resumed_pending_1c_fact"
    assert again["resumed"] is True
    assert again["manufacture_id"] == mid
    assert "СборкаЗапасов уже создана" in again["resume_reason"]
    assert (
        db.query(ProductionManufacture).filter_by(product_id=product.product_id).count() == 1
    )

    # And the resumed export repairs the existing document instead of creating one.
    retry_client = _FakeClient(ref_key="should-not-create")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: retry_client)
    retry = exporter.export_manufactures_to_1c(db, [again["manufacture_id"]], dry_run=False)
    assert retry["manufactures_created"] == 1
    assert retry_client.posts == []


def test_repeat_produce_resumes_when_piecework_order_is_missing(db_session, monkeypatch):
    """Assembly posted, наряд failed: the next press rolls the chain forward."""
    db = db_session
    item = _mk_item(db, code="RESUME-PW", ref1c="item-ref-resume-pw")
    product = _mk_product(db, item, qty=6)
    mid = produce_line(db, product.product_id, qty=6)["manufacture_id"]

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: _FakeClient(ref_key="assembly-posted-ref"))
    assert exporter.export_manufactures_to_1c(db, [mid], dry_run=False)["manufactures_created"] == 1

    again = produce_line(db, product.product_id, qty=6)

    assert again["resumed"] is True
    assert again["manufacture_id"] == mid
    assert "СдельныйНаряд не создан" in again["resume_reason"]
    assert (
        db.query(ProductionManufacture).filter_by(product_id=product.product_id).count() == 1
    )

    # Once the наряд exists, the line is fully commanded again — no more resume.
    db.add(SyncLink(
        source_doctype="piecework",
        source_id=mid,
        target_entity=piecework_exporter.PIECEWORK_ENTITY,
        target_number="PW000000001",
        payload_hash="hash",
        target_ref_key="piecework-ref",
        status="success",
    ))
    db.commit()

    with pytest.raises(ValueError, match="весь объём"):
        produce_line(db, product.product_id, qty=6)


def test_export_failure_detail_surfaces_skipped_rows():
    from app.routers.production_control import _export_failure_detail

    export = {
        "entries": [],
        "skipped_rows": [
            {"manufacture_id": 7, "reason": "item_ref1c пустой, нельзя сопоставить"},
        ],
    }

    detail = _export_failure_detail(export, {}, "1С не создала и не провела СборкаЗапасов")

    assert "item_ref1c пустой" in detail
    assert detail.startswith("1С не создала")
    # An entry-level error still wins over the skip list.
    assert _export_failure_detail(export, {"error": "boom"}, "default") == "boom"


def test_chain_auto_exports_parent_order_in_dry_run(db_session):
    """Per contract: Document_СборкаЗапасов is created ONLY on the basis of a
    production order. When the parent isn't in 1C yet, the manufacture export
    chains the parent order export first. In dry_run both payloads appear in
    the result."""
    item = _mk_item(db_session, code="MF-CHAIN", ref1c="item-ref-chain")
    product = _mk_product(db_session, item, qty=3)
    mid = produce_line(db_session, product.product_id, qty=3)["manufacture_id"]
    m = db_session.query(ProductionManufacture).filter_by(manufacture_id=mid).one()
    m.order.order_ref1c = None
    m.order.source = "mrp"
    _attach_current_mrp_lineage(db_session, product)

    result = exporter.export_manufactures_to_1c(db_session, [mid], dry_run=True)

    assert result["parent_orders_export"] is not None
    assert result["parent_orders_export"]["entity"] == "Document_ЗаказНаПроизводство"
    assert result["parent_orders_export"]["orders_eligible"] == 1
    # Child skips in dry_run because parent isn't actually stamped.
    assert result["manufactures_eligible"] == 0


def test_skipped_for_invalid_inputs(db_session, monkeypatch):
    db = db_session
    # 1) item with empty ref1c -> skipped
    item_no_ref = _mk_item(db, code="EXP-NOREF", ref1c=None)
    product_no_ref = _mk_product(db, item_no_ref, qty=2)
    mid_no_ref = produce_line(db, product_no_ref.product_id, qty=2)["manufacture_id"]
    # 2) cancelled manufacture -> skipped
    item_can = _mk_item(db, code="EXP-CAN", ref1c="ref-can")
    product_can = _mk_product(db, item_can, qty=1)
    mid_can = produce_line(db, product_can.product_id, qty=1)["manufacture_id"]
    m_can = db.query(ProductionManufacture).filter_by(manufacture_id=mid_can).one()
    m_can.status = "cancelled"
    db.commit()

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_manufactures_to_1c(
        db, [mid_no_ref, mid_can, 999_999], dry_run=False
    )
    reasons = [r["reason"] for r in result["skipped_rows"]]
    assert any("item_ref1c" in r for r in reasons)
    assert any("cancelled" in r for r in reasons)
    assert any("не найден" in r for r in reasons)
    assert fake.posts == []


# ---------------------------------------------------------------------------
# Pre-flight 1C balance guard (Document_СборкаЗапасов write-off coverage)
# ---------------------------------------------------------------------------


class _BalanceClient(_FakeClient):
    """FakeClient with the get_all used by the live-balance guard."""

    def __init__(self, *, balance_rows=None, balance_fail: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.balance_rows = list(balance_rows or [])
        self.balance_fail = balance_fail
        self.balance_queries: list = []

    def get_all(self, entity, **kwargs):
        self.balance_queries.append((entity, kwargs.get("filter_query")))
        if self.balance_fail:
            raise RuntimeError("balance query failed")
        return list(self.balance_rows)


def _guard_scenario(db, *, code: str, produce_qty: float = 3.0):
    """Item + 1-component spec + workshop binding; returns (mid, component_ref)."""
    item = _mk_item(db, code=f"GRD-{code}", ref1c=f"item-ref-guard-{code}")
    component = _mk_item(db, code=f"GRD-{code}-C", ref1c=f"component-ref-guard-{code}")
    spec = Specification(spec_name=f"Spec guard {code}", spec_ref1c=f"spec-ref-guard-{code}")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=1))
    product = _mk_product(db, item, qty=produce_qty)
    state = db.query(ProductionOrderLineState).filter_by(product_id=product.product_id).one()
    state.workshop_id = 177
    db.add(
        WorkshopWarehouseBinding(
            workshop_id=177,
            warehouse_ref1c="guard-material-ref",
            production_warehouse_ref1c="guard-product-ref",
        )
    )
    _stock_kit_on_workshop(db, product, component, produce_qty)
    mid = produce_line(db, product.product_id, qty=produce_qty, executor="op")["manufacture_id"]
    db.commit()
    return mid, component.item_ref1c


def test_export_blocked_when_1c_unit_balance_insufficient(db_session, monkeypatch):
    db = db_session
    mid, comp_ref = _guard_scenario(db, code="SHORT", produce_qty=3.0)

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _BalanceClient(
        balance_rows=[{"Номенклатура_Key": comp_ref, "КоличествоBalance": 2.0}]
    )
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)

    assert result["manufactures_blocked"] == 1
    assert result["manufactures_error"] == 1
    assert result["manufactures_created"] == 0
    assert result["status"] == "partial_error"
    # No 1C document was created or posted.
    assert fake.posts == []
    assert fake.operations == []
    # The balance was queried for the bound material unit.
    assert any("guard-material-ref" in str(q[1]) for q in fake.balance_queries)

    m = db.query(ProductionManufacture).filter_by(manufacture_id=mid).one()
    assert m.status == "draft"
    assert m.exported_ref1c is None
    assert "нужно 3" in m.export_error and "в 1С 2" in m.export_error

    entry = result["entries"][0]
    assert entry["error"] and "Недостаточно остатков в 1С" in entry["error"]


def test_export_blocked_when_component_absent_on_unit(db_session, monkeypatch):
    db = db_session
    mid, _comp_ref = _guard_scenario(db, code="ZERO", produce_qty=2.0)

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _BalanceClient(balance_rows=[])
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)

    assert result["manufactures_blocked"] == 1
    assert fake.posts == []
    m = db.query(ProductionManufacture).filter_by(manufacture_id=mid).one()
    assert "в 1С 0" in m.export_error


def test_export_proceeds_when_1c_unit_balance_sufficient(db_session, monkeypatch):
    db = db_session
    mid, comp_ref = _guard_scenario(db, code="OK", produce_qty=3.0)

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _BalanceClient(
        ref_key="guard-ok-ref",
        balance_rows=[{"Номенклатура_Key": comp_ref, "КоличествоBalance": 5.0}],
    )
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)

    assert result["manufactures_blocked"] == 0
    assert result["manufactures_created"] == 1
    assert result["status"] == "ok"
    m = db.query(ProductionManufacture).filter_by(manufacture_id=mid).one()
    assert m.status == "exported"
    assert m.export_error is None


def test_export_fails_open_when_balance_query_errors(db_session, monkeypatch):
    """1C connectivity hiccups must not block exports — 1C itself validates
    stock at posting time."""
    db = db_session
    mid, _comp_ref = _guard_scenario(db, code="FOPEN", produce_qty=3.0)

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _BalanceClient(ref_key="guard-fopen-ref", balance_fail=True)
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)

    assert result["manufactures_blocked"] == 0
    assert result["manufactures_created"] == 1


def test_guard_skips_repair_of_existing_1c_document(db_session, monkeypatch):
    """A manufacture whose 1C document already exists (retry/repair path) must
    not be balance-checked: if that document was posted, its write-off already
    left the register and the live balance would double-count it."""
    db = db_session
    mid, _comp_ref = _guard_scenario(db, code="RETRY", produce_qty=3.0)
    m = db.query(ProductionManufacture).filter_by(manufacture_id=mid).one()
    m.exported_ref1c = "existing-manuf-ref"
    m.status = "error"
    db.commit()

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    # Live balance is empty — would block a fresh export, must not block repair.
    fake = _BalanceClient(balance_rows=[])
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_manufactures_to_1c(db, [mid], dry_run=False)

    assert result["manufactures_blocked"] == 0
    assert result["manufactures_created"] == 1
    # Repair goes through patch of the existing document, not a new post.
    assert fake.patches
    assert fake.balance_queries == []
    db.refresh(m)
    assert m.status == "exported"


def test_guard_checks_bulk_batch_against_shared_balance(db_session, monkeypatch):
    """Two manufactures that fit the unit balance individually but not
    together: the first passes, the second is blocked against the remainder."""
    db = db_session
    item = _mk_item(db, code="GRD-BULK", ref1c="item-ref-guard-bulk")
    component = _mk_item(db, code="GRD-BULK-C", ref1c="component-ref-guard-bulk")
    spec = Specification(spec_name="Spec guard bulk", spec_ref1c="spec-ref-guard-bulk")
    db.add(spec)
    db.flush()
    db.add(DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=2))
    product = _mk_product(db, item, qty=6.0)
    state = db.query(ProductionOrderLineState).filter_by(product_id=product.product_id).one()
    state.workshop_id = 178
    db.add(
        WorkshopWarehouseBinding(
            workshop_id=178,
            warehouse_ref1c="guard-bulk-material-ref",
            production_warehouse_ref1c="guard-bulk-product-ref",
        )
    )
    _stock_kit_on_workshop(db, product, component, 12.0)
    mid1 = produce_line(db, product.product_id, qty=3, executor="op")["manufacture_id"]
    mid2 = produce_line(db, product.product_id, qty=3, executor="op")["manufacture_id"]
    db.commit()

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    # Each manufacture writes off 6; 1C unit holds 10 — only one fits.
    fake = _BalanceClient(
        balance_rows=[
            {"Номенклатура_Key": "component-ref-guard-bulk", "КоличествоBalance": 10.0}
        ]
    )
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_manufactures_to_1c(
        db, [mid1, mid2], dry_run=False
    )

    assert result["manufactures_created"] == 1
    assert result["manufactures_blocked"] == 1
    m1 = db.query(ProductionManufacture).filter_by(manufacture_id=mid1).one()
    m2 = db.query(ProductionManufacture).filter_by(manufacture_id=mid2).one()
    assert m1.status == "exported"
    assert m2.status == "draft"
    # The second entry is judged against the remainder: 10 - 6 = 4.
    assert "нужно 6" in m2.export_error and "в 1С 4" in m2.export_error
    # One shared balance read per unit for the whole batch, not per entry.
    assert len(fake.balance_queries) == 1


def test_produce_exports_both_documents_then_readback_closes_plans_fifo(
    db_session, monkeypatch
):
    """Document creation is a command; only its Ledger read-back is FIFO fact."""
    db = db_session
    item = _mk_item(db, code="E2E-PRODUCE", ref1c="e2e-item-ref")
    product = _mk_product(db, item, qty=7)
    _attach_current_mrp_lineage(db, product)

    command = produce_line(db, product.product_id, qty=7, executor="operator")
    manufacture_id = int(command["manufacture_id"])
    db.refresh(product)
    assert float(product.produced_qty) == 0
    assert float(product.remaining_qty) == 7

    _stub_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    manufacture_client = _FakeClient(ref_key="assembly-e2e-ref")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: manufacture_client)
    manufacture = exporter.export_manufactures_to_1c(
        db, [manufacture_id], dry_run=False
    )
    assert manufacture["manufactures_created"] == 1
    assert len(manufacture_client.posts) == 1

    monkeypatch.setattr(
        piecework_exporter,
        "_load_odata_config",
        lambda: {
            "base_url": "http://demo/odata/unf_demo",
            "username": "u",
            "password": "p",
        },
    )
    piecework_client = _FakeClient(ref_key="piecework-e2e-ref")
    monkeypatch.setattr(
        piecework_exporter, "OData1CClient", lambda **_: piecework_client
    )
    piecework = piecework_exporter.export_piecework_to_1c(
        db,
        [manufacture_id],
        operation_ref="operation-e2e-ref",
        dry_run=False,
    )
    assert piecework["manufactures_created"] == 1
    assert len(piecework_client.posts) == 1

    cutoff = datetime(2026, 7, 31, tzinfo=timezone.utc)
    physical_batch = models.PhysicalImportBatch(
        batch_key="produce-e2e-physical",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    generation = models.LedgerGeneration(
        generation_key="produce-e2e-generation",
        status="building",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=physical_batch,
        algorithm_version="tests/produce-e2e",
    )
    db.add_all([
        generation,
        models.StockWarehouse(
            warehouse_ref1c="e2e-warehouse",
            warehouse_name="E2E warehouse",
        ),
    ])
    db.flush()

    lines = []
    for idx, (start, qty) in enumerate(
        ((date(2026, 7, 1), Decimal("2")), (date(2026, 8, 1), Decimal("8"))),
        start=1,
    ):
        plan = models.ProductionPlanHeader(
            name=f"produce-e2e-plan-{idx}",
            period_from=start,
            period_to=date(2026, 12, 31),
            status="fixed",
            fixed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        db.add(plan)
        db.flush()
        db.add(models.PlanningRun(
            status="FIXED_SNAPSHOT",
            config_snapshot={},
            ledger_generation_id=int(generation.id),
            ledger_cutoff=cutoff,
            active_freeze_version=1,
            source_plan_id=int(plan.id),
            period_from=start,
            period_to=date(2026, 12, 31),
            fixed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ))
        line = models.ProductionPlanLine(
            plan_id=int(plan.id),
            item_id=int(item.item_id),
            bucket_date=start,
            qty=qty,
        )
        db.add(line)
        db.flush()
        lines.append(line)

    class ReadBackClient:
        def get_all(self, entity_name, **_kwargs):
            if entity_name == "Document_СборкаЗапасов":
                return [{
                    "Ref_Key": "assembly-e2e-ref",
                    "ЗаказНаПроизводство_Key": product.order.order_ref1c,
                }]
            return [{
                "Recorder": "assembly-e2e-ref",
                "Recorder_Type": "AccumulationRecordType",
                "RecordSet": [{
                    "Period": "2026-07-10T10:00:00",
                    "LineNumber": "1",
                    "Active": True,
                    "RecordType": "Receipt",
                    "Организация_Key": "00000000-0000-0000-0000-000000000000",
                    "Номенклатура_Key": "e2e-item-ref",
                    "Характеристика_Key": "00000000-0000-0000-0000-000000000000",
                    "Партия_Key": "00000000-0000-0000-0000-000000000000",
                    "СтруктурнаяЕдиница_Key": "e2e-warehouse",
                    "Ячейка_Key": "00000000-0000-0000-0000-000000000000",
                    "Количество": 7,
                    "КоличествоИнт": 0,
                    "ХозяйственнаяОперация_Key": "00000000-0000-0000-0000-000000000000",
                }],
            }]

    pulled = pull_recorder_movements(
        db,
        "Document_СборкаЗапасов",
        "assembly-e2e-ref",
        client=ReadBackClient(),
        ledger_generation_id=int(generation.id),
    )
    assert pulled.status == "done"
    generation.physical_import_batch_id = int(pulled.physical_import_batch_id)
    # This part of the test exercises the documented FIFO fallback for a
    # migrated/legacy product whose immutable plan-line identity is absent.
    product.source_mrp_requirement_id = None
    db.flush()
    result = materialize_assembly_output_allocations(db, int(generation.id))
    allocations = (
        db.query(models.AssemblyOutputAllocation)
        .filter_by(ledger_generation_id=int(generation.id))
        .order_by(models.AssemblyOutputAllocation.allocation_ordinal.asc())
        .all()
    )
    assert result["allocations"] == 2
    assert [int(row.plan_line_id) for row in allocations] == [
        int(lines[0].id),
        int(lines[1].id),
    ]
    assert [Decimal(row.allocated_qty) for row in allocations] == [
        Decimal("2"),
        Decimal("5"),
    ]

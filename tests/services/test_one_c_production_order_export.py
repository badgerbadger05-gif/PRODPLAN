"""Tests for one_c_production_order_export.

Covers:
- dry-run preview returns payloads without touching the network
- go-live: запись идёт в базу из настроек, демо-гарда больше нет
- successful write stamps sync_link + production_orders.order_ref1c
- second call is a no-op (sync_link idempotency)
- ineligible orders (1C-synced, deletion_mark, missing item ref1c, missing
  order) are reported in skipped_rows or marked existing
"""
from __future__ import annotations

import datetime as _dt
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app import models
from app.models import (
    DefaultSpecification,
    Item,
    Operation,
    PaintWeldChainLink,
    PaintWeldPair,
    MrpRequirement,
    PlannedOrder,
    PlanningRun,
    ProductionOrderLineState,
    ProductionOrder,
    ProductionPlanHeader,
    ProductionMaterialIssue,
    ProductionProduct,
    ProductionResource,
    ProductionStage,
    ResourceStage,
    ReplenishmentWorkItem,
    ReservationEntry,
    SpecComponent,
    Specification,
    SpecOperation,
    WorkshopWarehouseBinding,
    SyncLink,
)
from app.services import one_c_production_order_export as exporter
from app.services.planning_truth import publish_generation


# -----------------------------
# Helpers
# -----------------------------


def _mk_run(db) -> PlanningRun:
    cutoff = _dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc)
    generation = models.LedgerGeneration(
        generation_key=f"production-export-{id(db)}",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
        algorithm_version="test/1",
        replay_version="test/1",
        physical_import_batch=models.PhysicalImportBatch(
            batch_key=f"production-export-batch-{id(db)}",
            status="completed",
            cutoff=cutoff,
            source_watermarks={},
        ),
    )
    publish_generation(db, generation)
    run = PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot=json.dumps({}),
        active_freeze_version=1,
        ledger_generation_id=generation.id,
        ledger_cutoff=cutoff,
    )
    db.add(run)
    db.flush()
    return run


def _mk_item(db, *, code: str, ref1c: str) -> Item:
    it = Item(
        item_code=code,
        item_name=f"Item {code}",
        item_article=code,
        item_ref1c=ref1c,
        unit=f"unit-ref-{code}",
                status="active",
    )
    db.add(it)
    db.flush()
    return it


def _mk_mrp_order(db, item, *, run_id: int, qty=5, deletion=False) -> ProductionOrder:
    run = db.get(PlanningRun, run_id)
    proposal = PlannedOrder(
        run_id=run_id,
        item_id=item.item_id,
        requested_qty=qty,
        planned_qty=qty,
        qty=qty,
        need_date=_dt.date(2026, 5, 25),
        bucket_date=_dt.date(2026, 5, 25),
        ledger_generation_id=run.ledger_generation_id,
    )
    db.add(proposal)
    db.flush()
    order = ProductionOrder(
        order_number=f"MRP-{run_id}-{item.item_id}",
        order_date=datetime(2026, 5, 20),
        order_ref1c=None,
        is_posted=False,
        deletion_mark=deletion,
        source="mrp",
        source_run_id=run_id,
    )
    db.add(order)
    db.flush()
    db.add(
        ProductionProduct(
            order_id=order.order_id,
            item_id=item.item_id,
            line_number=1,
            quantity=qty,
            produced_qty=0,
            remaining_qty=qty,
            source_planned_order_id=proposal.order_id,
            ledger_generation_id=run.ledger_generation_id,
        )
    )
    db.commit()
    return order


class _FakeClient:
    """Minimal stand-in for OData1CClient.post."""

    def __init__(
        self,
        *,
        ref_key: str = "fake-1c-ref-key",
        fail: bool = False,
        existing_docs: list | None = None,
    ) -> None:
        self.ref_key = ref_key
        self.fail = fail
        self.posts: list = []
        self.patches: list = []
        self.operations: list = []
        self.existing_docs = existing_docs or []
        self.get_calls: list = []

    def get_all(self, entity, **kwargs):
        self.get_calls.append((entity, kwargs))
        return list(self.existing_docs)

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

    def post_operation(self, operation_path):
        self.operations.append(operation_path)


def _stub_odata_config(monkeypatch, *, base_url: str) -> None:
    monkeypatch.setattr(
        exporter,
        "_load_odata_config",
        lambda: {"base_url": base_url, "username": "u", "password": "p"},
    )


def _stub_close_config(monkeypatch, *, base_url: str, done_state_ref: str = "done-state-ref", done_variant_ref: str = "done-variant-ref") -> None:
    monkeypatch.setattr(
        exporter,
        "_load_odata_config",
        lambda: {
            "base_url": base_url,
            "username": "u",
            "password": "p",
            "default_production_order_done_state_ref1c": done_state_ref,
            "default_production_order_done_variant_ref1c": done_variant_ref,
        },
    )


def _mk_sync_link_for_order(
    db,
    order,
    *,
    target_ref_key: str = "00000000-0000-0000-0000-000000000123",
    status: str = "success",
) -> SyncLink:
    run = db.get(models.PlanningRun, order.source_run_id) if order.source_run_id else None
    link = SyncLink(
        source_doctype="production_order",
        source_id=int(order.order_id),
        target_entity=exporter.PRODUCTION_ORDER_ENTITY,
        target_system="1C",
        target_ref_key=target_ref_key,
        target_number="MRP-1",
        status=status,
        payload_hash="close-payload-hash",
        ledger_generation_id=int(run.ledger_generation_id) if run and run.ledger_generation_id is not None else None,
    )
    db.add(link)
    db.flush()
    return link


# -----------------------------
# Tests
# -----------------------------


def test_dry_run_returns_payload_without_touching_network(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="P1", ref1c="11111111-1111-1111-1111-111111111111")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id, qty=7)

    # Even if config exists, dry-run must not instantiate a client. Stub it
    # to a sentinel that would error on use.
    _stub_odata_config(monkeypatch, base_url="http://1c-demo.local/odata/unf_demo")
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("Network client must not be instantiated in dry-run"),
    )

    result = exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=True)

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["orders_eligible"] == 1
    assert result["orders_already_linked"] == 0
    assert result["orders_created"] == 0
    assert result["skipped_rows"] == []

    [pl] = result["payloads"]
    assert pl["order_id"] == order.order_id
    assert pl["payload"]["Posted"] is False
    assert pl["payload"]["Number"].startswith("PP")
    [prod_row] = pl["payload"]["Продукция"]
    assert prod_row["Номенклатура_Key"] == item.item_ref1c
    assert prod_row["ЕдиницаИзмерения"] == item.unit
    assert prod_row["ЕдиницаИзмерения_Type"] == "StandardODATA.Catalog_КлассификаторЕдиницИзмерения"
    assert float(prod_row["Количество"]) == 7.0
    assert "PRODPLAN source=production_order/" in pl["payload"]["Комментарий"]

    # No sync_link writes on dry-run.
    assert db.query(SyncLink).count() == 0


def test_dry_run_exports_closed_run_order_netted_by_current_mrp(
    db_session, monkeypatch
):
    """A recalculated plan keeps old order identity but may still execute it."""
    db = db_session
    item = _mk_item(
        db,
        code="P-HIST",
        ref1c="11111111-1111-1111-1111-111111111199",
    )
    plan = ProductionPlanHeader(
        name="Historical production export",
        period_from=_dt.date(2026, 10, 1),
        period_to=_dt.date(2026, 10, 31),
        status="fixed",
    )
    db.add(plan)
    db.flush()
    old_run = _mk_run(db)
    old_run.source_plan_id = int(plan.id)
    old_run.period_from = plan.period_from
    old_run.period_to = plan.period_to
    order = _mk_mrp_order(db, item, run_id=old_run.run_id, qty=6)
    old_run.status = "CLOSED"
    current_run = PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        active_freeze_version=1,
        ledger_generation_id=int(old_run.ledger_generation_id),
        ledger_cutoff=old_run.ledger_cutoff,
        source_plan_id=int(plan.id),
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db.add(current_run)
    db.flush()
    requirement = MrpRequirement(
        run_id=int(current_run.run_id),
        item_id=int(item.item_id),
        total_required_qty=Decimal("100"),
        net_required_qty=Decimal("100"),
        period_from=plan.period_from,
        period_to=plan.period_to,
        freeze_version=1,
    )
    db.add(requirement)
    db.flush()
    reservation = ReservationEntry(
        ledger_generation_id=int(old_run.ledger_generation_id),
        item_id=int(item.item_id),
        run_id=int(current_run.run_id),
        freeze_version=1,
        requirement_id=int(requirement.id),
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
        realization_mode="make",
        reserved_qty=Decimal("100"),
        replenishment_required_qty=Decimal("100"),
        lifecycle_status="active",
    )
    db.add(reservation)
    db.flush()
    db.add(ReplenishmentWorkItem(
        ledger_generation_id=int(old_run.ledger_generation_id),
        reservation_id=int(reservation.id),
        plan_id=int(plan.id),
        run_id=int(current_run.run_id),
        requirement_id=int(requirement.id),
        item_id=int(item.item_id),
        replenishment_method="make",
        replenishment_required_qty=Decimal("100"),
        replenishment_fulfilled_qty=Decimal("0"),
        replenishment_remaining_qty=Decimal("100"),
    ))
    db.commit()

    _stub_odata_config(monkeypatch, base_url="http://1c-demo.local/odata/unf_demo")
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("Network client must not be instantiated in dry-run"),
    )

    result = exporter.export_production_orders_to_1c(
        db, [order.order_id], dry_run=True
    )

    assert result["status"] == "ok"
    assert result["orders_eligible"] == 1
    assert result["payloads"][0]["order_id"] == int(order.order_id)


def test_dry_run_payload_includes_materials_operations_and_reserve_warehouse(db_session, monkeypatch):
    db = db_session
    parent = _mk_item(db, code="P-BOM", ref1c="parent-ref")
    component = _mk_item(db, code="C-BOM", ref1c="component-ref")
    spec = Specification(spec_name="Spec BOM", spec_ref1c="spec-ref")
    op = Operation(operation_ref1c="operation-ref", operation_name="Cut", time_norm=0.25)
    stage = ProductionStage(stage_name="Stage BOM", stage_ref1c="stage-ref")
    resource = ProductionResource(resource_name="Workshop BOM")
    db.add_all([spec, op, stage, resource])
    db.flush()
    db.add(DefaultSpecification(item_id=parent.item_id, spec_id=spec.spec_id))
    db.add(SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=2, stage_id=stage.stage_id))
    db.add(SpecOperation(spec_id=spec.spec_id, operation_id=op.operation_id, stage_id=stage.stage_id, time_norm=0.5))
    db.add(ResourceStage(resource_id=resource.resource_id, stage_id=stage.stage_id))
    db.add(
        WorkshopWarehouseBinding(
            workshop_id=resource.resource_id,
            warehouse_ref1c="workshop-warehouse-ref",
            production_warehouse_ref1c="production-warehouse-ref",
        )
    )
    db.flush()
    run = _mk_run(db)
    order = _mk_mrp_order(db, parent, run_id=run.run_id, qty=3)
    product = db.query(ProductionProduct).filter_by(order_id=order.order_id).one()
    product.spec_id = spec.spec_id
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="ready",
            issue_status="not_requested",
            workshop_id=resource.resource_id,
            planned_start_date=_dt.date(2026, 6, 12),
            planned_finish_date=_dt.date(2026, 6, 13),
        )
    )
    db.add(
        ProductionMaterialIssue(
            document_number="MI-BOM",
            product_id=product.product_id,
            order_id=order.order_id,
            status="draft",
            warehouse_ref1c="workshop-warehouse-ref",
            source_warehouse_ref1c="source-warehouse-ref",
        )
    )
    db.commit()

    _stub_odata_config(monkeypatch, base_url="http://1c-demo.local/odata/unf_demo")
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("Network client must not be instantiated in dry-run"),
    )
    monkeypatch.setattr(exporter, "_current_1c_datetime", lambda: "2026-05-27T09:58:40")

    result = exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=True)
    payload = result["payloads"][0]["payload"]

    # The document date is durable order data, so a retry produces the same
    # canonical full payload rather than a new hash every second.
    assert payload["Date"] == "2026-05-20T00:00:00"
    assert payload["Старт"] == "2026-06-12T00:00:00"
    assert payload["Финиш"] == "2026-06-13T00:00:00"
    assert payload["СтруктурнаяЕдиницаРезерв_Key"] == "workshop-warehouse-ref"
    assert payload["СтруктурнаяЕдиницаПродукции_Key"] == "production-warehouse-ref"
    [prod_row] = payload["Продукция"]
    assert prod_row["LineNumber"] == 1
    assert prod_row["СтруктурнаяЕдиница_Key"] == "production-warehouse-ref"
    assert prod_row["Спецификация_Key"] == "spec-ref"
    assert isinstance(prod_row["КлючСвязи"], int)
    assert 0 < prod_row["КлючСвязи"] < 2**63
    [stock_row] = payload["Запасы"]
    assert stock_row["Номенклатура_Key"] == component.item_ref1c
    assert stock_row["Количество"] == 6.0
    assert stock_row["ЕдиницаИзмерения"] == component.unit
    assert stock_row["Спецификация_Key"] == "spec-ref"
    assert stock_row["СтруктурнаяЕдиница_Key"] == "workshop-warehouse-ref"
    [operation_row] = payload["Операции"]
    assert operation_row["Операция_Key"] == "operation-ref"
    assert operation_row["КоличествоПлан"] == 3.0
    assert operation_row["НормаВремени"] == 0.5
    assert operation_row["Нормочасы"] == 1.5
    assert operation_row["СтруктурнаяЕдиница_Key"] == "production-warehouse-ref"
    assert operation_row["КлючСвязиПродукция"] == prod_row["КлючСвязи"]
    assert payload["ЗапланированыОперации"] is True


def test_welded_chain_order_delivers_output_to_painted_workshop(
    db_session, monkeypatch
):
    db = db_session
    painted_item = _mk_item(db, code="P-PAINT", ref1c="painted-item-ref")
    welded_item = _mk_item(db, code="P-WELD", ref1c="welded-item-ref")
    painted_resource = ProductionResource(resource_name="Powder coating")
    welded_resource = ProductionResource(resource_name="Welding")
    db.add_all([painted_resource, welded_resource])
    db.flush()
    db.add_all(
        [
            WorkshopWarehouseBinding(
                workshop_id=painted_resource.resource_id,
                warehouse_ref1c="paint-workshop-ref",
                production_warehouse_ref1c="painted-output-ref",
            ),
            WorkshopWarehouseBinding(
                workshop_id=welded_resource.resource_id,
                warehouse_ref1c="weld-workshop-ref",
                production_warehouse_ref1c="assembly-output-ref",
            ),
        ]
    )
    run = _mk_run(db)
    painted_order = _mk_mrp_order(db, painted_item, run_id=run.run_id, qty=3)
    welded_order = _mk_mrp_order(db, welded_item, run_id=run.run_id, qty=3)
    painted_product = (
        db.query(ProductionProduct)
        .filter_by(order_id=painted_order.order_id)
        .one()
    )
    welded_product = (
        db.query(ProductionProduct)
        .filter_by(order_id=welded_order.order_id)
        .one()
    )
    db.add_all(
        [
            ProductionOrderLineState(
                product_id=painted_product.product_id,
                status="ready",
                issue_status="not_requested",
                workshop_id=painted_resource.resource_id,
            ),
            ProductionOrderLineState(
                product_id=welded_product.product_id,
                status="ready",
                issue_status="not_requested",
                workshop_id=welded_resource.resource_id,
            ),
        ]
    )
    pair = PaintWeldPair(
        painted_item_id=painted_item.item_id,
        welded_item_id=welded_item.item_id,
        source="manual",
        is_active=True,
    )
    db.add(pair)
    db.flush()
    db.add(
        PaintWeldChainLink(
            painted_order_id=painted_order.order_id,
            welded_order_id=welded_order.order_id,
            pair_id=pair.id,
        )
    )
    db.commit()

    _stub_odata_config(monkeypatch, base_url="http://1c-demo.local/odata/unf_demo")
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("Network client must not be instantiated in dry-run"),
    )

    result = exporter.export_production_orders_to_1c(
        db, [welded_order.order_id], dry_run=True
    )
    payload = result["payloads"][0]["payload"]

    assert payload["СтруктурнаяЕдиницаРезерв_Key"] == "weld-workshop-ref"
    assert payload["СтруктурнаяЕдиницаПродукции_Key"] == "paint-workshop-ref"
    assert payload["Продукция"][0]["СтруктурнаяЕдиница_Key"] == "paint-workshop-ref"


def test_export_writes_into_configured_production_base(db_session, monkeypatch):
    """Go-live: демо-гард удалён — пишем в базу из настроек подключения.

    Единственный предпросмотр — dry_run: он не создаёт клиента и не постит.
    """
    db = db_session
    item = _mk_item(db, code="P2", ref1c="22222222-2222-2222-2222-222222222222")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id)

    _stub_odata_config(monkeypatch, base_url="http://erp-prod.example/odata/unf")  # NOT unf_demo
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    preview = exporter.export_production_orders_to_1c(
        db, [order.order_id], dry_run=True
    )
    assert preview["payloads"]
    assert fake.posts == []

    result = exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=False)
    assert result["orders_created"] == 1
    assert len(fake.posts) == 1


def test_successful_export_stamps_sync_link_and_order_ref1c(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="P3", ref1c="33333333-3333-3333-3333-333333333333")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id, qty=4)

    _stub_odata_config(monkeypatch, base_url="http://mtzw7/unf_demo/odata")
    fake = _FakeClient(ref_key="1e1f5690-5345-11f1-9dae-9ee51454587f")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=False)

    assert result["status"] == "ok"
    assert result["orders_created"] == 1
    assert result["orders_error"] == 0

    db.refresh(order)
    assert order.order_ref1c == "1e1f5690-5345-11f1-9dae-9ee51454587f"

    link = (
        db.query(SyncLink)
        .filter_by(
            source_system="PRODPLAN",
            source_doctype="production_order",
            source_id=order.order_id,
            target_entity=exporter.PRODUCTION_ORDER_ENTITY,
        )
        .one()
    )
    assert link.status == "success"
    assert link.target_ref_key == "1e1f5690-5345-11f1-9dae-9ee51454587f"
    assert link.target_number is not None
    assert link.payload_hash is not None
    assert link.last_synced_at is not None


def test_successful_export_records_where_the_output_lands(db_session, monkeypatch):
    """Future supply needs the destination the export itself chose.

    The Ledger accepts an open production order as expected arrival only with an
    exact destination warehouse, and the 1C sync never writes PRODPLAN's own
    order lines back.  Leaving the column empty rejected every launched order as
    evidence, so the requirement it was launched for stayed uncovered and the
    same work was offered for launch again.
    """
    db = db_session
    item = _mk_item(db, code="P-DEST", ref1c="55555555-5555-5555-5555-555555555555")
    resource = ProductionResource(resource_name="Workshop DEST")
    db.add(resource)
    db.flush()
    db.add(
        WorkshopWarehouseBinding(
            workshop_id=resource.resource_id,
            warehouse_ref1c="workshop-warehouse-ref",
            production_warehouse_ref1c="production-warehouse-ref",
        )
    )
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id, qty=4)
    product = db.query(ProductionProduct).filter_by(order_id=order.order_id).one()
    assert not (product.destination_warehouse_ref1c or "")
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="ready",
            issue_status="not_requested",
            workshop_id=resource.resource_id,
        )
    )
    db.commit()

    _stub_odata_config(monkeypatch, base_url="http://mtzw7/unf_demo/odata")
    fake = _FakeClient(ref_key="2e1f5690-5345-11f1-9dae-9ee51454587f")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=False)

    assert result["orders_created"] == 1
    db.refresh(product)
    assert product.destination_warehouse_ref1c == "production-warehouse-ref"


def test_recalculated_existing_order_is_reused_patched_and_reposted(
    db_session, monkeypatch
):
    db = db_session
    item = _mk_item(db, code="P-RECALC", ref1c="56565656-5656-5656-5656-565656565656")
    resource = ProductionResource(resource_name="Workshop recalculated")
    db.add(resource)
    db.flush()
    binding = WorkshopWarehouseBinding(
        workshop_id=resource.resource_id,
        warehouse_ref1c="workshop-recalc-ref",
        production_warehouse_ref1c="old-output-ref",
    )
    db.add(binding)
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id, qty=4)
    product = db.query(ProductionProduct).filter_by(order_id=order.order_id).one()
    db.add(
        ProductionOrderLineState(
            product_id=product.product_id,
            status="ready",
            issue_status="not_requested",
            workshop_id=resource.resource_id,
        )
    )
    db.commit()

    _stub_odata_config(monkeypatch, base_url="http://mtzw7/unf_demo/odata")
    fake = _FakeClient(ref_key="recalculated-existing-ref")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    first = exporter.export_production_orders_to_1c(
        db, [order.order_id], dry_run=False
    )
    assert first["orders_created"] == 1
    db.refresh(product)
    assert product.destination_warehouse_ref1c == "old-output-ref"

    accepted_generation = db.get(models.LedgerGeneration, run.ledger_generation_id)
    previous_generation = models.LedgerGeneration(
        generation_key=f"previous-production-export-{id(db)}",
        status="stale",
        cutoff=accepted_generation.cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch_id=accepted_generation.physical_import_batch_id,
        algorithm_version="test/previous",
        replay_version="test/previous",
    )
    db.add(previous_generation)
    db.flush()
    link = db.query(SyncLink).filter_by(
        source_doctype="production_order", source_id=order.order_id
    ).one()
    link.ledger_generation_id = previous_generation.id
    binding.production_warehouse_ref1c = "new-output-ref"
    db.commit()
    fake.existing_docs = [
        {
            "Ref_Key": "recalculated-existing-ref",
            "Комментарий": "same prodplan-origin marker",
            "Posted": True,
        }
    ]
    fake.get_calls.clear()

    second = exporter.export_production_orders_to_1c(
        db, [order.order_id], dry_run=False
    )

    assert second["orders_created"] == 1
    assert second["orders_already_linked"] == 0
    assert len(fake.posts) == 1
    assert len(fake.patches) == 1
    assert fake.get_calls == []
    assert fake.patches[0][0].endswith("(guid'recalculated-existing-ref')")
    assert (
        fake.patches[0][1]["СтруктурнаяЕдиницаПродукции_Key"]
        == "new-output-ref"
    )
    assert fake.operations[-2:] == [
        "Document_ЗаказНаПроизводство(guid'recalculated-existing-ref')/Unpost",
        "Document_ЗаказНаПроизводство(guid'recalculated-existing-ref')/Post?PostingModeOperational=true",
    ]
    db.refresh(product)
    db.refresh(link)
    assert product.destination_warehouse_ref1c == "new-output-ref"
    assert link.ledger_generation_id == run.ledger_generation_id
    assert link.status == "success"


def test_second_export_is_noop_due_to_existing_link(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="P4", ref1c="44444444-4444-4444-4444-444444444444")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id)

    _stub_odata_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(ref_key="aaaa-existing-ref-key")
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=False)
    assert len(fake.posts) == 1

    # Re-call. Same order, success link already there -> entries[].status='existing',
    # no new POST and orders_created=0.
    result = exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=False)
    assert result["orders_created"] == 0
    assert result["orders_already_linked"] == 1
    assert len(fake.posts) == 1  # no additional POST


def test_empty_local_link_recovers_document_from_1c_origin_marker(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="PX", ref1c="99999999-9999-9999-9999-999999999999")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id, qty=7)
    preview = exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=True)
    comment = preview["payloads"][0]["payload"]["Комментарий"]
    assert "prodplan-origin=" in comment

    _stub_odata_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient(
        existing_docs=[
            {
                "Ref_Key": "cross-instance-ref",
                "Number": "OTHER",
                "Комментарий": comment,
                "Posted": True,
            }
        ]
    )
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.export_production_orders_to_1c(
        db, [order.order_id], dry_run=False
    )

    assert result["orders_created"] == 0
    assert result["orders_recovered"] == 1
    assert fake.posts == []
    db.refresh(order)
    assert order.order_ref1c == "cross-instance-ref"
    assert db.query(SyncLink).filter_by(
        source_doctype="production_order", source_id=order.order_id
    ).one().target_ref_key == "cross-instance-ref"


def test_legacy_error_link_with_ref_fails_closed_before_network(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="P4R", ref1c="44444444-4444-4444-4444-44444444444a")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id)

    db.add(SyncLink(
        source_system="PRODPLAN",
        source_doctype="production_order",
        source_id=order.order_id,
        target_system="1C",
        target_entity=exporter.PRODUCTION_ORDER_ENTITY,
        target_number="PP-RETRY",
        payload_hash="old-hash",
        target_ref_key="existing-order-ref",
        status="error",
        last_error="post failed after create",
    ))
    db.commit()

    _stub_odata_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("legacy retry must fail before creating a network client"),
    )

    with pytest.raises(exporter.MrpMutationLineageError, match="accepted Ledger generation"):
        exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=False)

    db.refresh(order)
    assert order.order_ref1c is None
    assert db.query(SyncLink).filter_by(source_id=order.order_id).one().payload_hash == "old-hash"


def test_mismatched_retry_payload_fails_closed_before_network(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="P4M", ref1c="44444444-4444-4444-4444-44444444444b")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id)

    db.add(SyncLink(
        source_system="PRODPLAN",
        source_doctype="production_order",
        source_id=order.order_id,
        target_system="1C",
        target_entity=exporter.PRODUCTION_ORDER_ENTITY,
        target_number="PP-MISMATCH",
        payload_hash="not-the-canonical-payload",
        target_ref_key="existing-order-ref",
        status="success",
        ledger_generation_id=run.ledger_generation_id,
    ))
    order.order_ref1c = "existing-order-ref"
    db.commit()

    _stub_odata_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("mismatched retry must fail before creating a network client"),
    )

    with pytest.raises(exporter.MrpMutationLineageError, match="canonical export payload"):
        exporter.export_production_orders_to_1c(db, [order.order_id], dry_run=False)

    db.refresh(order)
    assert order.order_ref1c == "existing-order-ref"


def test_successful_order_link_survives_later_accepted_generation():
    entry = exporter.ProductionOrderExportEntry(
        order_id=10,
        number="PP000000010",
        source_run_id=2,
        ledger_generation_id=160,
        freeze_version=1,
        document_date=datetime(2026, 8, 10),
    )
    link = SyncLink(
        source_system="PRODPLAN",
        source_doctype="production_order",
        source_id=10,
        target_system="1C",
        target_entity=exporter.PRODUCTION_ORDER_ENTITY,
        target_number="PP000000010",
        target_ref_key="existing-order-ref",
        payload_hash="payload-from-generation-137",
        status="success",
        ledger_generation_id=137,
    )

    verified = exporter._validate_existing_retry_link(
        entry=entry,
        link=link,
        expected_payload_hash="payload-materialized-in-generation-160",
        order_ref1c="existing-order-ref",
    )

    assert verified == "existing-order-ref"


def test_successful_order_link_survives_canonical_rebuild_lineage_clear():
    entry = exporter.ProductionOrderExportEntry(
        order_id=10,
        number="PP000000010",
        source_run_id=2,
        ledger_generation_id=160,
        freeze_version=1,
        document_date=datetime(2026, 8, 10),
    )
    link = SyncLink(
        source_system="PRODPLAN",
        source_doctype="production_order",
        source_id=10,
        target_system="1C",
        target_entity=exporter.PRODUCTION_ORDER_ENTITY,
        target_number="PP000000010",
        target_ref_key="existing-order-ref",
        payload_hash="historical-payload",
        status="success",
        ledger_generation_id=None,
    )

    assert exporter._validate_existing_retry_link(
        entry=entry,
        link=link,
        expected_payload_hash="current-payload",
        order_ref1c="existing-order-ref",
    ) == "existing-order-ref"


def test_failed_order_link_from_another_generation_still_fails_closed():
    entry = exporter.ProductionOrderExportEntry(
        order_id=10,
        number="PP000000010",
        source_run_id=2,
        ledger_generation_id=160,
        freeze_version=1,
        document_date=datetime(2026, 8, 10),
    )
    link = SyncLink(
        source_system="PRODPLAN",
        source_doctype="production_order",
        source_id=10,
        target_system="1C",
        target_entity=exporter.PRODUCTION_ORDER_ENTITY,
        target_number="PP000000010",
        target_ref_key=None,
        payload_hash="old-attempt",
        status="error",
        ledger_generation_id=137,
    )

    with pytest.raises(exporter.MrpMutationLineageError, match="another Ledger generation"):
        exporter._validate_existing_retry_link(
            entry=entry,
            link=link,
            expected_payload_hash="current-attempt",
            order_ref1c="",
        )


def test_missing_durable_order_date_fails_before_payload_hash():
    entry = exporter.ProductionOrderExportEntry(
        order_id=1,
        number="PP-MISSING-DATE",
        source_run_id=1,
        ledger_generation_id=1,
        freeze_version=1,
        document_date=None,
    )

    with pytest.raises(exporter.MrpMutationLineageError, match="durable document date"):
        exporter._build_header_payload(entry)


def test_skipped_rows_for_invalid_orders(db_session, monkeypatch):
    db = db_session
    run = _mk_run(db)

    # (1) 1C-synced order — wrong source, must be skipped
    item_a = _mk_item(db, code="PA", ref1c="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    order_1c = ProductionOrder(
        order_number="1C-ORIGIN",
        order_date=datetime(2026, 5, 20),
        order_ref1c="some-existing-1c-ref",
        deletion_mark=False,
        source="1c",
    )
    db.add(order_1c)
    db.flush()
    db.add(
        ProductionProduct(
            order_id=order_1c.order_id,
            item_id=item_a.item_id,
            line_number=1,
            quantity=1,
            produced_qty=0,
            remaining_qty=1,
        )
    )
    # (2) deletion_mark=True — must be skipped
    item_b = _mk_item(db, code="PB", ref1c="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    order_del = _mk_mrp_order(db, item_b, run_id=run.run_id, deletion=True)
    # (3) Item with empty ref1c — must be skipped
    item_noref = Item(
        item_code="P-NOREF",
        item_name="No ref",
        item_article="P-NOREF",
        item_ref1c=None,
        unit="шт",
                status="active",
    )
    db.add(item_noref)
    db.flush()
    order_noref = _mk_mrp_order(db, item_noref, run_id=run.run_id)

    db.commit()

    _stub_odata_config(monkeypatch, base_url="http://demo/odata/unf_demo")
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    with pytest.raises(ValueError, match="do not exist"):
        exporter.export_production_orders_to_1c(
            db,
            [order_1c.order_id, order_del.order_id, order_noref.order_id, 999_999],
            dry_run=False,
        )
    assert fake.posts == []


def test_partial_failure_keeps_other_orders_committed(db_session, monkeypatch):
    db = db_session
    item_ok = _mk_item(db, code="POK", ref1c="okokokok-okok-okok-okok-okokokokokok")
    item_bad = _mk_item(db, code="PBAD", ref1c="bdbdbdbd-bdbd-bdbd-bdbd-bdbdbdbdbdbd")
    run = _mk_run(db)
    order_ok = _mk_mrp_order(db, item_ok, run_id=run.run_id)
    order_bad = _mk_mrp_order(db, item_bad, run_id=run.run_id)

    _stub_odata_config(monkeypatch, base_url="http://demo/odata/unf_demo")

    call_count = {"n": 0}

    class _SometimesFail:
        def post(self, entity, payload, **_):
            call_count["n"] += 1
            # Second POST fails — order_bad gets recorded as error.
            if call_count["n"] >= 2:
                raise RuntimeError("simulated failure")
            return {"Ref_Key": "ok-ref-key"}

    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: _SometimesFail())

    result = exporter.export_production_orders_to_1c(
        db, [order_ok.order_id, order_bad.order_id], dry_run=False
    )

    assert result["status"] == "partial_error"
    assert result["orders_created"] == 1
    assert result["orders_error"] == 1

    db.refresh(order_ok)
    db.refresh(order_bad)
    assert order_ok.order_ref1c == "ok-ref-key"
    assert order_bad.order_ref1c is None  # failed -> stays unstamped

    link_ok = (
        db.query(SyncLink)
        .filter_by(source_id=order_ok.order_id, target_entity=exporter.PRODUCTION_ORDER_ENTITY)
        .one()
    )
    assert link_ok.status == "success"
    link_bad = (
        db.query(SyncLink)
        .filter_by(source_id=order_bad.order_id, target_entity=exporter.PRODUCTION_ORDER_ENTITY)
        .one()
    )
    assert link_bad.status == "error"
    assert "simulated failure" in (link_bad.last_error or "")


def test_close_dry_run_without_network(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="PC", ref1c="cccccccc-cccc-cccc-cccc-cccccccccccc")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id, qty=1)
    order.order_ref1c = "e2d7f3f0-0000-0000-0000-000000000123"
    _mk_sync_link_for_order(db, order, target_ref_key=order.order_ref1c)
    db.commit()

    _stub_close_config(monkeypatch, base_url="http://1c-demo.local/odata/unf_demo")
    fake = _FakeClient()
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("Network client must not be used in dry-run close"),
    )

    result = exporter.close_production_orders_to_1c(db, [order.order_id], dry_run=True)

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["orders_eligible"] == 1
    assert result["orders_closed"] == 0
    assert result["orders_error"] == 0
    assert len(result["payloads"]) == 1
    payload = result["payloads"][0]["payload"]
    assert payload["СостояниеЗаказа_Key"] == "done-state-ref"
    assert payload["ВариантЗавершения"] == "done-variant-ref"
    assert "Date" not in payload
    assert "ДатаЗакрытия" not in payload
    assert "Комментарий" not in payload


def test_close_live_writes_patch_to_1c_without_mutating_local_order(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="PXC", ref1c="dddddddd-dddd-dddd-dddd-dddddddddddd")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id, qty=2)
    order.order_ref1c = "22222222-2222-2222-2222-222222222222"
    _mk_sync_link_for_order(db, order, target_ref_key=order.order_ref1c)
    db.commit()

    _stub_close_config(monkeypatch, base_url="http://erp-prod/odata/unf")
    fake = _FakeClient()
    monkeypatch.setattr(exporter, "OData1CClient", lambda **_: fake)

    result = exporter.close_production_orders_to_1c(
        db,
        [order.order_id],
        dry_run=False,
    )

    assert result["status"] == "ok"
    assert result["orders_closed"] == 1
    assert result["orders_error"] == 0
    assert len(fake.patches) == 1
    assert fake.patches[0][0] == "Document_ЗаказНаПроизводство(guid'22222222-2222-2222-2222-222222222222')"
    assert fake.patches[0][1]["СостояниеЗаказа_Key"] == "done-state-ref"
    assert fake.patches[0][1]["ВариантЗавершения"] == "done-variant-ref"
    assert "Date" not in fake.patches[0][1]
    assert "ДатаЗакрытия" not in fake.patches[0][1]
    assert "Комментарий" not in fake.patches[0][1]
    db.expire_all()
    unchanged = db.get(ProductionOrder, order.order_id)
    assert unchanged is not None
    assert unchanged.deletion_mark is False
    assert unchanged.order_state_key == order.order_state_key


def test_close_fails_closed_when_link_missing_or_ineligible(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="PNL", ref1c="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id, qty=3)
    order.order_ref1c = "33333333-3333-3333-3333-333333333333"
    db.commit()

    _stub_close_config(monkeypatch, base_url="http://1c-demo.local/odata/unf_demo")
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("close should be skipped when SyncLink/lineage is missing"),
    )

    result = exporter.close_production_orders_to_1c(
        db, [order.order_id], dry_run=False
    )

    assert result["status"] == "ok"
    assert result["orders_eligible"] == 0
    assert len(result["payloads"]) == 0
    assert len(result["skipped_rows"]) == 1
    assert "SyncLink не найден" in result["skipped_rows"][0]["reason"]


def test_close_requires_done_variant_in_config(db_session, monkeypatch):
    db = db_session
    item = _mk_item(db, code="PXA", ref1c="ffffffff-ffff-ffff-ffff-ffffffffffff")
    run = _mk_run(db)
    order = _mk_mrp_order(db, item, run_id=run.run_id, qty=1)
    order.order_ref1c = "44444444-4444-4444-4444-444444444444"
    _mk_sync_link_for_order(db, order, target_ref_key=order.order_ref1c)
    db.commit()

    _stub_close_config(monkeypatch, base_url="http://1c-demo.local/odata/unf_demo", done_variant_ref="")
    monkeypatch.setattr(
        exporter,
        "OData1CClient",
        lambda **_: pytest.fail("close should fail before network when variant is not configured"),
    )

    with pytest.raises(exporter.MrpMutationLineageError, match="done_variant_ref1c"):
        exporter.close_production_orders_to_1c(db, [order.order_id], dry_run=False)

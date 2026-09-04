from datetime import date, datetime
from decimal import Decimal

from app import models
from app.services.item_ledger.assembly_readiness_persistence import (
    _curve_inputs,
    _physical_supplies,
)


def _queue_scope(db):
    batch = models.PhysicalImportBatch(
        batch_key="readiness-frozen-route",
        status="completed",
        cutoff=datetime(2026, 9, 4),
        source_watermarks={},
        completed_at=datetime(2026, 9, 4),
    )
    generation = models.LedgerGeneration(
        generation_key="readiness-frozen-route",
        status="building",
        cutoff=datetime(2026, 9, 4),
        source_watermarks={},
        capabilities={},
        algorithm_version="test",
        physical_import_batch=batch,
    )
    root = models.Item(
        item_code="READY-ROOT",
        item_name="Ready root",
        unit="шт",
        status="active",
        replenishment_method="Производство",
    )
    child = models.Item(
        item_code="READY-CHILD",
        item_name="Ready child",
        unit="шт",
        status="active",
        replenishment_method="Покупка",
    )
    plan = models.ProductionPlanHeader(
        name="Readiness frozen route",
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        status="fixed",
        created_by="test",
        fixed_at=datetime(2026, 9, 1),
    )
    db.add_all([batch, generation, root, child, plan])
    db.flush()
    line = models.ProductionPlanLine(
        plan_id=int(plan.id),
        item_id=int(root.item_id),
        bucket_date=date(2026, 9, 4),
        qty=Decimal("3"),
        accepted_output_qty=Decimal("0"),
        remaining_output_qty=Decimal("3"),
    )
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=int(plan.id),
        period_from=plan.period_from,
        period_to=plan.period_to,
        ledger_generation_id=int(generation.id),
        active_freeze_version=2,
    )
    db.add_all([line, run])
    db.flush()
    queue = models.AssemblyQueueLine(
        ledger_generation_id=int(generation.id),
        planning_run_id=int(run.run_id),
        plan_id=int(plan.id),
        plan_line_id=int(line.id),
        item_id=int(root.item_id),
        bucket_date=line.bucket_date,
        period_from=plan.period_from,
        period_to=plan.period_to,
        planned_output_qty=Decimal("3"),
        accepted_plan_output_qty=Decimal("0"),
        assembly_remaining_qty=Decimal("3"),
        original_priority=[],
        sort_key="001",
        line_status="open",
    )
    db.add(queue)
    db.flush()
    return queue, run, root, child


def test_curve_inputs_use_only_frozen_branch_and_distinct_warehouse_roles(db_session):
    queue, run, root, child = _queue_scope(db_session)
    db_session.add_all(
        [
            models.MrpFreezeComponent(
                run_id=int(run.run_id),
                freeze_version=2,
                root_item_id=int(root.item_id),
                parent_item_id=int(root.item_id),
                component_item_id=int(child.item_id),
                spec_ref="ROOT-SPEC-FROZEN",
                spec_version="hash-frozen",
                child_spec_ref="",
                norm_qty_per_unit=Decimal("2"),
                unit_coef=Decimal("1"),
            ),
            models.MrpFreezeBomNode(
                run_id=int(run.run_id),
                freeze_version=2,
                root_item_id=int(root.item_id),
                item_id=int(root.item_id),
                spec_ref="ROOT-SPEC-FROZEN",
                spec_version="hash-frozen",
                replenishment_mode="make",
                replenishment_time_days=7,
                resource_id=None,
                material_warehouse_ref1c="WH-MATERIALS",
                output_warehouse_ref1c="WH-OUTPUT",
                is_kitting=True,
                route_reason="",
            ),
            models.MrpFreezeBomNode(
                run_id=int(run.run_id),
                freeze_version=2,
                root_item_id=int(root.item_id),
                item_id=int(child.item_id),
                spec_ref="",
                replenishment_mode="buy",
                replenishment_time_days=11,
                material_warehouse_ref1c="",
                output_warehouse_ref1c="",
                is_kitting=False,
                route_reason="",
            ),
        ]
    )
    db_session.flush()

    lines, edges, policies = _curve_inputs(db_session, [queue])

    assert lines[0].root_spec_ref == "ROOT-SPEC-FROZEN"
    assert lines[0].target_warehouse_ref1c == "WH-MATERIALS"
    assert lines[0].unavailable_reasons == ()
    assert edges[0].parent_spec_ref == "ROOT-SPEC-FROZEN"
    assert edges[0].child_spec_ref == ""
    root_policy = next(row for row in policies if row.item_id == root.item_id)
    assert root_policy.lead_days == 7
    assert root_policy.route_kind == "kitting"
    assert root_policy.material_warehouse_ref1c == "WH-MATERIALS"
    assert root_policy.output_warehouse_ref1c == "WH-OUTPUT"


def test_curve_inputs_fail_closed_for_legacy_unscoped_freeze(db_session):
    queue, run, root, child = _queue_scope(db_session)
    db_session.add(
        models.MrpFreezeComponent(
            run_id=int(run.run_id),
            freeze_version=2,
            root_item_id=None,
            parent_item_id=int(root.item_id),
            component_item_id=int(child.item_id),
            spec_ref="LEGACY",
            norm_qty_per_unit=Decimal("1"),
            unit_coef=Decimal("1"),
        )
    )
    db_session.flush()

    lines, edges, policies = _curve_inputs(db_session, [queue])

    assert edges == ()
    assert policies == ()
    assert "FROZEN_BOM_SCHEMA_OUTDATED" in lines[0].unavailable_reasons


def test_curve_inputs_preserve_frozen_non_stock_reason(db_session):
    queue, run, root, child = _queue_scope(db_session)
    db_session.add_all(
        [
            models.MrpFreezeComponent(
                run_id=int(run.run_id),
                freeze_version=2,
                root_item_id=int(root.item_id),
                parent_item_id=int(root.item_id),
                component_item_id=int(child.item_id),
                spec_ref="ROOT-SPEC",
                child_spec_ref="",
                norm_qty_per_unit=Decimal("1"),
            ),
            models.MrpFreezeBomNode(
                run_id=int(run.run_id),
                freeze_version=2,
                root_item_id=int(root.item_id),
                item_id=int(root.item_id),
                spec_ref="ROOT-SPEC",
                replenishment_mode="make",
                replenishment_time_days=0,
                material_warehouse_ref1c="ASSEMBLY",
                output_warehouse_ref1c="ASSEMBLY",
            ),
            models.MrpFreezeBomNode(
                run_id=int(run.run_id),
                freeze_version=2,
                root_item_id=int(root.item_id),
                item_id=int(child.item_id),
                spec_ref="",
                replenishment_mode="buy",
                replenishment_time_days=1,
                is_stock_item=False,
            ),
        ]
    )
    db_session.flush()

    _lines, _edges, policies = _curve_inputs(db_session, [queue])

    child_policy = next(row for row in policies if row.item_id == child.item_id)
    assert child_policy.unavailable_reason == "NON_STOCK_ITEM"


def test_nested_node_custody_is_scoped_by_frozen_root_and_owner(db_session):
    queue, run, root, child = _queue_scope(db_session)
    material = models.Item(
        item_code="READY-MATERIAL",
        item_name="Ready material",
        unit="шт",
        status="active",
    )
    db_session.add(material)
    db_session.flush()
    db_session.add(
        models.MrpFreezeBomNode(
            run_id=int(run.run_id),
            freeze_version=2,
            root_item_id=int(root.item_id),
            item_id=int(child.item_id),
            spec_ref="CHILD-SPEC",
            replenishment_mode="make",
            replenishment_time_days=1,
            material_warehouse_ref1c="CHILD-WIP",
            output_warehouse_ref1c="ASSEMBLY",
        )
    )
    requirement = models.MrpRequirement(
        run_id=int(run.run_id),
        item_id=int(child.item_id),
        total_required_qty=Decimal("1"),
        net_required_qty=Decimal("1"),
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
    )
    order = models.ProductionOrder(
        order_number="READY-CUSTODY",
        order_date=datetime(2026, 9, 3),
        order_ref1c="ready-custody-order",
    )
    db_session.add_all([requirement, order])
    db_session.flush()
    product = models.ProductionProduct(
        order_id=int(order.order_id),
        item_id=int(child.item_id),
        line_number=1,
        quantity=Decimal("1"),
        produced_qty=Decimal("0"),
        remaining_qty=Decimal("1"),
        source_mrp_requirement_id=int(requirement.id),
    )
    db_session.add(product)
    db_session.flush()
    generation = db_session.get(models.LedgerGeneration, int(queue.ledger_generation_id))
    db_session.add_all(
        [
            models.ProductionMaterialCustodyProjectionManifest(
                ledger_generation_id=int(generation.id),
                cutoff=generation.cutoff,
                status="complete",
                is_baseline=True,
                source_event_high_watermark_id=0,
            ),
            models.ProductionMaterialCustodyProjection(
                ledger_generation_id=int(generation.id),
                product_id=int(product.product_id),
                component_item_id=int(material.item_id),
                location_kind="workshop",
                warehouse_ref1c="CHILD-WIP",
                reserved_qty=Decimal("2"),
                source_event_high_watermark_id=0,
            ),
            models.ProductionMaterialCustodyProjection(
                ledger_generation_id=int(generation.id),
                product_id=int(product.product_id),
                component_item_id=int(material.item_id),
                location_kind="transit",
                warehouse_ref1c="STORE-3",
                reserved_qty=Decimal("1"),
                source_event_high_watermark_id=0,
            ),
        ]
    )
    db_session.flush()

    supplies = _physical_supplies(db_session, int(generation.id), [queue])
    custody = next(
        row
        for row in supplies
        if row.source_key.startswith("custody:") and row.layer == "now"
    )
    transit = next(
        row
        for row in supplies
        if row.source_key.startswith("custody:") and row.layer == "transfer"
    )

    assert custody.item_id == int(material.item_id)
    assert custody.qty == Decimal("2")
    assert custody.bom_key == int(run.run_id)
    assert custody.queue_line_id == int(queue.id)
    assert custody.root_item_ids == (int(root.item_id),)
    assert custody.custody_owner_item_id == int(child.item_id)
    assert custody.source_ref == "READY-CUSTODY"
    assert transit.item_id == int(material.item_id)
    assert transit.qty == Decimal("1")
    assert transit.warehouse_ref1c == "STORE-3"
    assert transit.transfer_destination_warehouse_ref1c == "CHILD-WIP"
    assert transit.root_item_ids == (int(root.item_id),)
    assert transit.custody_owner_item_id == int(child.item_id)

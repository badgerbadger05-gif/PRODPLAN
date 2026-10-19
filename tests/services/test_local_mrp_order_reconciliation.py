from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.local_mrp_order_reconciliation import (
    reconcile_local_mrp_orders,
    transition_local_mrp_order_spec,
)


def _scenario(
    db,
    *,
    order_qty: Decimal,
    target_qty: Decimal,
    order_ref1c: str | None = None,
):
    cutoff = datetime(2026, 9, 4, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key=f"reconcile-physical-{order_qty}-{target_qty}-{order_ref1c}",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    db.add(physical)
    db.flush()
    generation = models.LedgerGeneration(
        generation_key=f"reconcile-{order_qty}-{target_qty}-{order_ref1c}",
        status="building",
        cutoff=cutoff,
        capabilities={},
        source_watermarks={"generation_kind": "obligation_refresh"},
        physical_import_batch_id=int(physical.id),
        algorithm_version="test",
    )
    plan = models.ProductionPlanHeader(
        name=f"Reconcile {order_qty} to {target_qty}",
        period_from=date(2026, 10, 1),
        period_to=date(2026, 10, 31),
        status="fixed",
    )
    item = models.Item(
        item_code=f"reconcile-{order_qty}-{target_qty}-{order_ref1c}",
        item_name="Reconciliation item",
    )
    db.add_all([generation, plan, item])
    db.flush()
    historical_run = models.PlanningRun(
        status="CLOSED",
        config_snapshot={},
        ledger_generation_id=int(generation.id),
        ledger_cutoff=cutoff,
        active_freeze_version=1,
        source_plan_id=int(plan.id),
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    live_run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=int(generation.id),
        ledger_cutoff=cutoff,
        active_freeze_version=1,
        source_plan_id=int(plan.id),
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db.add_all([historical_run, live_run])
    db.flush()
    historical_requirement = models.MrpRequirement(
        run_id=int(historical_run.run_id),
        item_id=int(item.item_id),
        total_required_qty=order_qty,
        net_required_qty=order_qty,
        period_from=plan.period_from,
        period_to=plan.period_to,
        freeze_version=1,
    )
    live_requirement = models.MrpRequirement(
        run_id=int(live_run.run_id),
        item_id=int(item.item_id),
        total_required_qty=target_qty,
        net_required_qty=target_qty,
        period_from=plan.period_from,
        period_to=plan.period_to,
        freeze_version=1,
    )
    db.add_all([historical_requirement, live_requirement])
    db.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=int(generation.id),
        item_id=int(item.item_id),
        run_id=int(live_run.run_id),
        freeze_version=1,
        requirement_id=int(live_requirement.id),
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
        realization_mode="make",
        reserved_qty=target_qty,
        replenishment_required_qty=target_qty,
        lifecycle_status="active",
    )
    db.add(reservation)
    db.flush()
    if target_qty > 0:
        db.add(
            models.ReplenishmentWorkItem(
                ledger_generation_id=int(generation.id),
                reservation_id=int(reservation.id),
                plan_id=int(plan.id),
                run_id=int(live_run.run_id),
                requirement_id=int(live_requirement.id),
                item_id=int(item.item_id),
                replenishment_method="make",
                replenishment_required_qty=target_qty,
                replenishment_fulfilled_qty=Decimal("0"),
                replenishment_remaining_qty=target_qty,
            )
        )
    order = models.ProductionOrder(
        order_number=f"MRP-RECONCILE-{order_qty}",
        order_date=cutoff,
        order_ref1c=order_ref1c,
        source="mrp",
        source_run_id=int(historical_run.run_id),
        deletion_mark=False,
    )
    db.add(order)
    db.flush()
    product = models.ProductionProduct(
        order_id=int(order.order_id),
        item_id=int(item.item_id),
        line_number=1,
        quantity=order_qty,
        produced_qty=Decimal("0"),
        remaining_qty=order_qty,
        source_mrp_requirement_id=int(historical_requirement.id),
        ledger_generation_id=int(generation.id),
    )
    db.add(product)
    db.flush()
    return generation, live_run, order, product


def _explicit_transition_scenario(
    db,
    *,
    second_legitimate_spec=False,
    ambiguous_target_revision=False,
    include_target_edge=True,
    ambiguous_route=False,
    unavailable_route=False,
    manual_route=False,
):
    generation, live_run, order, product = _scenario(
        db, order_qty=Decimal("296"), target_qty=Decimal("296"),
    )
    source_run = db.get(models.PlanningRun, int(order.source_run_id))
    suffix = f"{live_run.run_id}-{product.product_id}"
    old_spec = models.Specification(
        spec_code=f"OLD-{suffix}", spec_name=f"Old {suffix}",
        spec_ref1c=f"old-spec-{suffix}", content_hash=f"old-hash-{suffix}",
    )
    new_kind = models.ProductionKind(
        ref_1c=f"new-kind-{suffix}", name=f"New kind {suffix}",
    )
    db.add_all([old_spec, new_kind])
    db.flush()
    new_spec = models.Specification(
        spec_code=f"NEW-{suffix}", spec_name=f"New {suffix}",
        spec_ref1c=f"new-spec-{suffix}", content_hash=f"new-hash-{suffix}",
        production_kind_id=int(new_kind.id),
    )
    component = models.Item(
        item_code=f"component-{suffix}", item_name=f"Component {suffix}",
    )
    new_resource = models.ProductionResource(resource_name=f"New resource {suffix}")
    old_resource = models.ProductionResource(resource_name=f"Old resource {suffix}")
    db.add_all([new_spec, component, new_resource, old_resource])
    db.flush()
    db.add(models.DefaultSpecification(
        item_id=int(product.item_id), spec_id=int(new_spec.spec_id),
        characteristic_id="00000000-0000-0000-0000-000000000000",
    ))
    if not unavailable_route:
        db.add(models.ResourceProductionKind(
            resource_id=int(new_resource.resource_id),
            production_kind_id=int(new_kind.id),
        ))
    if ambiguous_route:
        second_resource = models.ProductionResource(resource_name=f"Second resource {suffix}")
        db.add(second_resource)
        db.flush()
        db.add(models.ResourceProductionKind(
            resource_id=int(second_resource.resource_id),
            production_kind_id=int(new_kind.id),
        ))
    db.add_all([
        models.WorkshopWarehouseBinding(
            workshop_id=int(new_resource.resource_id),
            warehouse_ref1c=f"material-{suffix}",
            production_warehouse_ref1c=f"output-{suffix}",
        ),
        models.WorkshopWarehouseBinding(
            workshop_id=int(old_resource.resource_id),
            warehouse_ref1c=f"old-material-{suffix}",
            production_warehouse_ref1c=f"old-output-{suffix}",
        ),
    ])
    live_run.active_freeze_version = 1
    db.add(models.PlanningLivePointer(
        plan_id=int(live_run.source_plan_id), run_id=int(live_run.run_id),
        status="active",
    ))
    db.add(models.PlanningRunSuccessor(
        plan_id=int(live_run.source_plan_id),
        predecessor_run_id=int(source_run.run_id),
        successor_run_id=int(live_run.run_id),
        reason="specification_rebase",
    ))
    if include_target_edge:
        db.add(models.MrpFreezeComponent(
            run_id=int(live_run.run_id), freeze_version=1,
            root_item_id=int(product.item_id), parent_item_id=int(product.item_id),
            component_item_id=int(component.item_id),
            spec_ref=str(new_spec.spec_ref1c), spec_version=str(new_spec.content_hash),
            child_spec_ref="", norm_qty_per_unit=Decimal("1"), unit_coef=Decimal("1"),
        ))
    db.add(models.MrpFreezeBomNode(
        run_id=int(live_run.run_id), freeze_version=1,
        root_item_id=int(product.item_id), item_id=int(product.item_id),
        spec_ref=str(new_spec.spec_ref1c), spec_version=str(new_spec.content_hash),
        replenishment_mode="make", replenishment_time_days=1,
        resource_id=(None if unavailable_route else int(new_resource.resource_id)),
        material_warehouse_ref1c=("" if unavailable_route else f"material-{suffix}"),
        output_warehouse_ref1c=("" if unavailable_route else f"output-{suffix}"),
        is_stock_item=True, is_kitting=False,
        route_reason=("KIND_NOT_BOUND" if unavailable_route else ""),
    ))
    if second_legitimate_spec:
        other_spec = models.Specification(
            spec_code=f"OTHER-{suffix}", spec_name=f"Other {suffix}",
            spec_ref1c=f"other-spec-{suffix}", content_hash=f"other-hash-{suffix}",
            production_kind_id=int(new_kind.id),
        )
        other_component = models.Item(
            item_code=f"other-component-{suffix}", item_name=f"Other component {suffix}",
        )
        db.add_all([other_spec, other_component])
        db.flush()
        db.add(models.MrpFreezeComponent(
            run_id=int(live_run.run_id), freeze_version=1,
            root_item_id=int(other_component.item_id),
            parent_item_id=int(product.item_id),
            component_item_id=int(other_component.item_id),
            spec_ref=str(other_spec.spec_ref1c),
            spec_version=str(other_spec.content_hash), child_spec_ref="",
            norm_qty_per_unit=Decimal("1"), unit_coef=Decimal("1"),
        ))
    if ambiguous_target_revision:
        conflicting_component = models.Item(
            item_code=f"conflicting-component-{suffix}",
            item_name=f"Conflicting component {suffix}",
        )
        db.add(conflicting_component)
        db.flush()
        db.add(models.MrpFreezeComponent(
            run_id=int(live_run.run_id), freeze_version=1,
            root_item_id=int(conflicting_component.item_id),
            parent_item_id=int(product.item_id),
            component_item_id=int(conflicting_component.item_id),
            spec_ref=str(new_spec.spec_ref1c),
            spec_version=f"conflicting-hash-{suffix}", child_spec_ref="",
            norm_qty_per_unit=Decimal("1"), unit_coef=Decimal("1"),
        ))

    product.spec_id = int(old_spec.spec_id)
    product.spec_revision_hash = str(old_spec.content_hash)
    state = models.ProductionOrderLineState(
        product_id=int(product.product_id), status="shortage",
        issue_status="not_requested", workshop_id=int(old_resource.resource_id),
        workshop_id_source=("manual_route" if manual_route else "legacy"),
    )
    if manual_route:
        product.destination_warehouse_ref1c = f"manual-output-{suffix}"
    db.add(state)
    db.flush()
    return generation, live_run, order, product, old_spec, new_spec, state, new_resource


def _transition(db, product, old_spec, new_spec):
    return transition_local_mrp_order_spec(
        db,
        product_id=int(product.product_id),
        new_spec_id=int(new_spec.spec_id),
        expected_old_spec_id=int(old_spec.spec_id),
        expected_old_spec_revision_hash=str(old_spec.content_hash),
        expected_new_spec_revision_hash=str(new_spec.content_hash),
    )


@pytest.mark.parametrize(
    ("order_qty", "target_qty"),
    [
        (Decimal("40"), Decimal("30")),
        (Decimal("30"), Decimal("50")),
    ],
)
def test_reconciles_untouched_local_order_both_directions(
    db_session, order_qty, target_qty
):
    generation, live_run, order, product = _scenario(
        db_session,
        order_qty=order_qty,
        target_qty=target_qty,
    )

    summary = reconcile_local_mrp_orders(
        db_session,
        ledger_generation_id=int(generation.id),
        live_run_ids=[int(live_run.run_id)],
    )

    assert Decimal(product.quantity) == target_qty
    assert Decimal(product.remaining_qty) == target_qty
    assert order.deletion_mark is False
    assert summary["resized"] == 1
    assert summary["cancelled"] == 0
    assert Decimal(summary["entries"][0]["previous_quantity"]) == order_qty
    assert Decimal(summary["entries"][0]["quantity"]) == target_qty


def test_cancels_untouched_local_order_when_make_target_disappears(db_session):
    generation, live_run, order, product = _scenario(
        db_session,
        order_qty=Decimal("40"),
        target_qty=Decimal("0"),
    )

    summary = reconcile_local_mrp_orders(
        db_session,
        ledger_generation_id=int(generation.id),
        live_run_ids=[int(live_run.run_id)],
    )

    assert order.deletion_mark is True
    assert product.control_state.status == "cancelled"
    assert summary["resized"] == 0
    assert summary["cancelled"] == 1


def test_preserves_order_already_linked_to_1c(db_session):
    generation, live_run, order, product = _scenario(
        db_session,
        order_qty=Decimal("40"),
        target_qty=Decimal("30"),
        order_ref1c="11111111-1111-1111-1111-111111111111",
    )

    summary = reconcile_local_mrp_orders(
        db_session,
        ledger_generation_id=int(generation.id),
        live_run_ids=[int(live_run.run_id)],
    )

    assert Decimal(product.quantity) == Decimal("40")
    assert order.deletion_mark is False
    assert summary["resized"] == 0
    assert summary["cancelled"] == 0
    assert summary["locked"] == 1
    assert summary["entries"] == []


def test_preserves_order_after_even_a_failed_1c_export_attempt(db_session):
    generation, live_run, order, product = _scenario(
        db_session,
        order_qty=Decimal("40"),
        target_qty=Decimal("30"),
    )
    db_session.add(
        models.SyncLink(
            source_system="PRODPLAN",
            source_doctype="production_order",
            source_id=int(order.order_id),
            target_system="1C",
            target_entity="Document_ЗаказНаПроизводство",
            target_number=str(order.order_number),
            payload_hash="failed-attempt",
            status="error",
            last_error="timeout",
        )
    )
    db_session.flush()

    summary = reconcile_local_mrp_orders(
        db_session,
        ledger_generation_id=int(generation.id),
        live_run_ids=[int(live_run.run_id)],
    )

    assert Decimal(product.quantity) == Decimal("40")
    assert summary["resized"] == 0
    assert summary["locked"] == 1


def test_local_draft_material_issue_does_not_lock_quantity(db_session):
    generation, live_run, order, product = _scenario(
        db_session,
        order_qty=Decimal("40"),
        target_qty=Decimal("30"),
    )
    db_session.add(
        models.ProductionMaterialIssue(
            document_number="LOCAL-DRAFT-ISSUE",
            product_id=int(product.product_id),
            order_id=int(order.order_id),
            status="draft",
            direction="issue",
        )
    )
    db_session.flush()

    summary = reconcile_local_mrp_orders(
        db_session,
        ledger_generation_id=int(generation.id),
        live_run_ids=[int(live_run.run_id)],
    )

    assert Decimal(product.quantity) == Decimal("30")
    assert summary["resized"] == 1
    assert summary["locked"] == 0


def test_exported_material_issue_locks_quantity(db_session):
    generation, live_run, order, product = _scenario(
        db_session,
        order_qty=Decimal("40"),
        target_qty=Decimal("30"),
    )
    db_session.add(
        models.ProductionMaterialIssue(
            document_number="EXPORTED-ISSUE",
            product_id=int(product.product_id),
            order_id=int(order.order_id),
            status="requested",
            direction="issue",
            exported_ref1c="22222222-2222-2222-2222-222222222222",
        )
    )
    db_session.flush()

    summary = reconcile_local_mrp_orders(
        db_session,
        ledger_generation_id=int(generation.id),
        live_run_ids=[int(live_run.run_id)],
    )

    assert Decimal(product.quantity) == Decimal("40")
    assert summary["resized"] == 0
    assert summary["locked"] == 1


def test_explicit_transition_uses_exact_frozen_target_with_other_legitimate_refs(
    db_session,
):
    (
        _generation, _live_run, order, product, old_spec, new_spec, state,
        new_resource,
    ) = _explicit_transition_scenario(
        db_session, second_legitimate_spec=True,
    )
    identity_before = {
        "order_id": order.order_id,
        "order_number": order.order_number,
        "source_run_id": order.source_run_id,
        "product_id": product.product_id,
        "source_requirement": product.source_mrp_requirement_id,
        "ledger_generation_id": product.ledger_generation_id,
        "quantity": product.quantity,
        "remaining_qty": product.remaining_qty,
    }

    result = _transition(db_session, product, old_spec, new_spec)

    assert result["action"] == "updated"
    assert result["frozen_target_edge_count"] == 1
    assert product.spec_id == new_spec.spec_id
    assert product.spec_revision_hash == new_spec.content_hash
    assert state.workshop_id == new_resource.resource_id
    assert state.workshop_id_source == "auto"
    assert {
        "order_id": order.order_id,
        "order_number": order.order_number,
        "source_run_id": order.source_run_id,
        "product_id": product.product_id,
        "source_requirement": product.source_mrp_requirement_id,
        "ledger_generation_id": product.ledger_generation_id,
        "quantity": product.quantity,
        "remaining_qty": product.remaining_qty,
    } == identity_before


def test_explicit_transition_is_idempotent_after_exact_update(db_session):
    (
        _generation, _live_run, _order, product, old_spec, new_spec, _state,
        _resource,
    ) = _explicit_transition_scenario(db_session)
    first = _transition(db_session, product, old_spec, new_spec)
    second = _transition(db_session, product, old_spec, new_spec)

    assert first["action"] == "updated"
    assert second["action"] == "unchanged"
    assert second["skip_reason"] == "already_exact_explicit_target"
    assert product.spec_id == new_spec.spec_id
    assert product.spec_revision_hash == new_spec.content_hash


def test_explicit_transition_blocks_target_absent_from_current_frozen_graph(
    db_session,
):
    (
        _generation, _live_run, _order, product, old_spec, new_spec, _state,
        _resource,
    ) = _explicit_transition_scenario(db_session, include_target_edge=False)

    result = _transition(db_session, product, old_spec, new_spec)

    assert result["action"] == "blocked"
    assert "explicit_target_absent_from_current_frozen_graph" in result["blocked_reasons"]
    assert product.spec_id == old_spec.spec_id
    assert product.spec_revision_hash == old_spec.content_hash


def test_explicit_transition_blocks_two_revisions_of_same_frozen_target(db_session):
    (
        _generation, _live_run, _order, product, old_spec, new_spec, _state,
        _resource,
    ) = _explicit_transition_scenario(
        db_session, ambiguous_target_revision=True,
    )

    result = _transition(db_session, product, old_spec, new_spec)

    assert result["action"] == "blocked"
    assert "explicit_target_frozen_revision_ambiguous" in result["blocked_reasons"]
    assert len(result["frozen_target_revisions"]) == 2
    assert product.spec_id == old_spec.spec_id


def test_explicit_transition_blocks_ambiguous_automatic_route(db_session):
    (
        _generation, _live_run, _order, product, old_spec, new_spec, _state,
        _resource,
    ) = _explicit_transition_scenario(db_session, ambiguous_route=True)

    result = _transition(db_session, product, old_spec, new_spec)

    assert result["action"] == "blocked"
    assert "current_target_kind_route_ambiguous" in result["blocked_reasons"]
    assert product.spec_id == old_spec.spec_id


def test_explicit_transition_requires_target_to_remain_current_default(db_session):
    (
        _generation, _live_run, _order, product, old_spec, new_spec, _state,
        _resource,
    ) = _explicit_transition_scenario(db_session)
    default = db_session.query(models.DefaultSpecification).filter_by(
        item_id=int(product.item_id)
    ).one()
    default.spec_id = int(old_spec.spec_id)
    db_session.flush()

    result = _transition(db_session, product, old_spec, new_spec)

    assert result["action"] == "blocked"
    assert "explicit_target_is_not_current_default" in result["blocked_reasons"]
    assert product.spec_id == old_spec.spec_id


def test_explicit_transition_checks_old_spec_and_revision_guards(db_session):
    (
        _generation, _live_run, _order, product, old_spec, new_spec, _state,
        _resource,
    ) = _explicit_transition_scenario(db_session)

    result = transition_local_mrp_order_spec(
        db_session,
        product_id=int(product.product_id),
        new_spec_id=int(new_spec.spec_id),
        expected_old_spec_id=int(old_spec.spec_id) + 1000,
        expected_old_spec_revision_hash="foreign-old-hash",
        expected_new_spec_revision_hash=str(new_spec.content_hash),
    )

    assert result["action"] == "blocked"
    assert "old_specification_guard_mismatch" in result["blocked_reasons"]
    assert "old_specification_revision_guard_mismatch" in result["blocked_reasons"]
    assert product.spec_id == old_spec.spec_id


def test_explicit_transition_preserves_approved_manual_route(db_session):
    (
        _generation, _live_run, _order, product, old_spec, new_spec, state,
        _resource,
    ) = _explicit_transition_scenario(
        db_session, second_legitimate_spec=True,
        unavailable_route=True, manual_route=True,
    )
    previous_workshop = state.workshop_id
    previous_destination = product.destination_warehouse_ref1c

    result = _transition(db_session, product, old_spec, new_spec)

    assert result["action"] == "updated"
    assert result["route_action"] == "manual_override_preserved"
    assert state.workshop_id == previous_workshop
    assert state.workshop_id_source == "manual_route"
    assert product.destination_warehouse_ref1c == previous_destination
    assert product.spec_id == new_spec.spec_id


@pytest.mark.parametrize(
    ("guard", "expected_reason"),
    [
        ("sync_attempt", "order_has_sync_attempt"),
        ("order_ref", "order_has_1c_ref"),
        ("posted", "order_is_posted"),
        ("material_issue", "material_issue_exists"),
        ("custody_event", "custody_event_exists"),
        ("manufacture", "manufacture_exists"),
        ("chain", "paint_weld_chain_exists"),
        ("accepted_output", "accepted_output_exists"),
        ("terminal", "line_status:completed"),
        ("in_progress", "line_status:in_progress"),
        ("printed", "route_sheet_printed"),
        ("multi_line", "order_not_single_line"),
    ],
)
def test_explicit_transition_protects_every_execution_guard(
    db_session, guard, expected_reason,
):
    (
        _generation, _live_run, order, product, old_spec, new_spec, state,
        _resource,
    ) = _explicit_transition_scenario(db_session)
    if guard == "sync_attempt":
        db_session.add(models.SyncLink(
            source_system="PRODPLAN", source_doctype="production_order",
            source_id=int(order.order_id), target_system="1C",
            target_entity="Document_ЗаказНаПроизводство", status="error",
        ))
    elif guard == "order_ref":
        order.order_ref1c = "11111111-1111-1111-1111-111111111111"
    elif guard == "posted":
        order.is_posted = True
    elif guard == "material_issue":
        db_session.add(models.ProductionMaterialIssue(
            document_number=f"GUARD-ISSUE-{product.product_id}",
            product_id=int(product.product_id), order_id=int(order.order_id),
            status="draft", direction="issue",
        ))
    elif guard == "custody_event":
        db_session.add(models.ProductionMaterialCustodyEvent(
            product_id=int(product.product_id),
            component_item_id=int(product.item_id), source_kind="baseline",
            effective_at=datetime(2026, 9, 1), location_kind="workshop",
            warehouse_ref1c="guard-workshop", delta_qty=Decimal("1"),
            idempotency_key=f"guard-custody-{product.product_id}",
        ))
    elif guard == "manufacture":
        db_session.add(models.ProductionManufacture(
            product_id=int(product.product_id), order_id=int(order.order_id),
            qty=Decimal("1"), status="cancelled",
            request_key=f"guard-manufacture-{product.product_id}",
        ))
    elif guard == "chain":
        counterpart_item = models.Item(
            item_code=f"counterpart-{product.product_id}", item_name="Counterpart",
        )
        counterpart_order = models.ProductionOrder(
            order_number=f"COUNTERPART-{product.product_id}",
            order_date=datetime(2026, 9, 1), source="mrp",
        )
        db_session.add_all([counterpart_item, counterpart_order])
        db_session.flush()
        pair = models.PaintWeldPair(
            painted_item_id=int(product.item_id),
            welded_item_id=int(counterpart_item.item_id), source="manual",
        )
        db_session.add(pair)
        db_session.flush()
        db_session.add(models.PaintWeldChainLink(
            painted_order_id=int(order.order_id),
            welded_order_id=int(counterpart_order.order_id), pair_id=int(pair.id),
        ))
    elif guard == "accepted_output":
        product.produced_qty = Decimal("1")
        product.remaining_qty = Decimal("295")
    elif guard == "terminal":
        state.status = "completed"
    elif guard == "in_progress":
        state.status = "in_progress"
    elif guard == "multi_line":
        db_session.add(models.ProductionProduct(
            order_id=int(order.order_id), item_id=int(product.item_id),
            line_number=2, quantity=Decimal("1"), produced_qty=Decimal("0"),
            remaining_qty=Decimal("1"),
        ))
    else:
        state.route_sheet_printed_at = datetime(2026, 10, 19, tzinfo=timezone.utc)
    db_session.flush()

    result = _transition(db_session, product, old_spec, new_spec)

    assert result["action"] == "blocked"
    assert expected_reason in result["blocked_reasons"]
    assert product.spec_id == old_spec.spec_id
    assert product.spec_revision_hash == old_spec.content_hash

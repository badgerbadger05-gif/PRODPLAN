from datetime import datetime, timezone

from app import models
from app.services.accepted_command_mutation import (
    accepted_mutation_epoch,
    accepted_mutation_tail,
    consume_accepted_mutations,
    record_accepted_production_mutation,
)
from app.services.planning_truth import PlanningTruthPublishConflict, publish_generation
from app.services.item_ledger.current_execution import (
    invalidate_current_execution_scope,
    publish_current_execution_scope,
    publish_current_production_control_from_payload,
)
from app.services.production_control_journal_projection import (
    rebase_production_payload_for_accepted_mutations,
)
import pytest


def _truth(db):
    cutoff = datetime(2026, 10, 30, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key="accepted-command-epoch", status="completed", cutoff=cutoff,
        completed_at=cutoff, source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="accepted-command-epoch", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, physical_import_batch=batch, source_watermarks={},
        capabilities={"physical_ledger": True}, algorithm_version="test",
    )
    db.add_all((batch, generation))
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    db.add(models.AcceptedProductionMutationState(id=1))
    db.commit()
    return generation


def test_epoch_tail_is_monotonic_and_hwm_survives_atomic_compaction(db_session):
    generation = _truth(db_session)
    first = record_accepted_production_mutation(
        db_session, mutation_kind="local_make", product_ids=(9, 9), item_ids=(4,),
        business_identities=("mrp:4",),
    )
    second = record_accepted_production_mutation(
        db_session, mutation_kind="custody_event", product_ids=(10,), item_ids=(4, 5),
    )
    assert (first, second, accepted_mutation_epoch(db_session)) == (1, 2, 2)
    tail = accepted_mutation_tail(db_session, after_epoch=0)
    assert tail.product_ids == (9, 10)
    assert tail.item_ids == (4, 5)
    assert tail.business_identities == ("mrp:4",)
    assert all(
        int(row.accepted_generation_id) == int(generation.id)
        for row in db_session.query(models.AcceptedProductionMutation).all()
    )
    consume_accepted_mutations(db_session, through_epoch=2)
    assert db_session.query(models.AcceptedProductionMutation).count() == 0
    assert accepted_mutation_epoch(db_session) == 2
    third = record_accepted_production_mutation(
        db_session, mutation_kind="executor", product_ids=(11,),
    )
    assert third == 3


def test_full_scope_mutation_is_explicit(db_session):
    _truth(db_session)
    record_accepted_production_mutation(
        db_session, mutation_kind="production_invalidation", full_scope=True,
    )
    assert accepted_mutation_tail(db_session, after_epoch=0).full_scope is True


def test_generation_publication_cas_rejects_unrebased_command_epoch(db_session):
    parent = _truth(db_session)
    child = models.LedgerGeneration(
        generation_key="accepted-command-child", status="accepted",
        cutoff=parent.cutoff, accepted_at=parent.accepted_at,
        physical_import_batch_id=int(parent.physical_import_batch_id),
        source_watermarks={
            "parent_generation_id": int(parent.id),
            "generation_kind": "obligation_refresh",
            "accepted_production_mutation_epoch": 0,
        },
        capabilities={}, algorithm_version="test",
    )
    db_session.add(child)
    db_session.flush()
    record_accepted_production_mutation(
        db_session, mutation_kind="executor", product_ids=(9,),
    )
    with pytest.raises(PlanningTruthPublishConflict, match="changed during"):
        publish_generation(db_session, child, expected_parent_id=int(parent.id))
    db_session.rollback()


def test_generation_publication_consumes_rebased_epoch_atomically(db_session):
    parent = _truth(db_session)
    epoch = record_accepted_production_mutation(
        db_session, mutation_kind="executor", product_ids=(9,),
    )
    child = models.LedgerGeneration(
        generation_key="accepted-command-rebased", status="accepted",
        cutoff=parent.cutoff, accepted_at=parent.accepted_at,
        physical_import_batch_id=int(parent.physical_import_batch_id),
        source_watermarks={
            "parent_generation_id": int(parent.id),
            "generation_kind": "obligation_refresh",
            "accepted_production_mutation_epoch": epoch,
        },
        capabilities={}, algorithm_version="test",
    )
    publish_generation(db_session, child, expected_parent_id=int(parent.id))
    assert db_session.query(models.AcceptedProductionMutation).count() == 0
    state = db_session.get(models.AcceptedProductionMutationState, 1)
    assert state.last_issued_epoch == state.last_consumed_epoch == epoch


def test_distinct_dependency_changes_stamp_while_scope_already_unready(db_session):
    parent = _truth(db_session)
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g1",
        source_generation_id=int(parent.id),
        scope_key="production:all-live-orders",
        entity_kinds=("production_control_journal",),
        rows=[],
    )
    assert invalidate_current_execution_scope(
        db_session, entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
        source_revision="settings:v2", reason="settings_changed",
    )
    assert invalidate_current_execution_scope(
        db_session, entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
        source_revision="settings:v3", reason="settings_changed",
    )
    assert accepted_mutation_epoch(db_session) == 2
    assert not invalidate_current_execution_scope(
        db_session, entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
        source_revision="settings:v3", reason="settings_changed",
    )
    assert accepted_mutation_epoch(db_session) == 2


def test_invalidation_a_b_a_stamps_current_transitions_not_historical_values(db_session):
    parent = _truth(db_session)
    publish_current_execution_scope(
        db_session, source_revision="accepted:g1",
        source_generation_id=int(parent.id),
        scope_key="production:all-live-orders",
        entity_kinds=("production_control_journal",), rows=[],
    )
    for revision in ("settings:A", "settings:B", "settings:A"):
        assert invalidate_current_execution_scope(
            db_session, entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
            source_revision=revision, reason="settings_changed",
        )
    assert accepted_mutation_epoch(db_session) == 3
    assert not invalidate_current_execution_scope(
        db_session, entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
        source_revision="settings:A", reason="settings_changed",
    )
    assert accepted_mutation_epoch(db_session) == 3


def test_metadata_rebase_preserves_target_physical_output_and_coverage(db_session):
    parent = _truth(db_session)
    item = models.Item(item_code="REBASE", item_name="Rebase")
    order = models.ProductionOrder(
        order_number="REBASE-ORDER", order_date=parent.cutoff,
        order_ref1c="new-order-ref",
    )
    db_session.add_all((item, order))
    db_session.flush()
    product = models.ProductionProduct(
        order_id=int(order.order_id), item_id=int(item.item_id),
        quantity=10, produced_qty=0, remaining_qty=10,
    )
    db_session.add(product)
    db_session.flush()
    identity = f"production:product:{int(product.product_id)}"
    candidate = {
        "current_identity": identity,
        "product_id": int(product.product_id), "order_id": int(order.order_id),
        "item_id": int(item.item_id), "quantity": 10,
        "produced_qty": 5, "remaining_qty": 5,
        "spec_id": None, "spec_revision_hash": None,
        "issue_status": "not_requested", "order_ref1c": None,
        "coverage_status": "ready", "coverage_label": "Ready",
        "material_coverage_snapshot": {"coverage_status": "ready"},
        "material_coverage_status": "ready",
        "available_actions": ["produce"], "root_item_ids": [],
    }
    rebuilt = {
        **candidate,
        "order_ref1c": "new-order-ref", "issue_status": "exported",
        "available_actions": ["produce", "print"],
        "produced_qty": 0, "remaining_qty": 10,
        "coverage_status": "shortage", "coverage_label": "Shortage",
    }
    publish_current_production_control_from_payload(
        db_session, int(parent.id), {"meta": {}, "rows": [rebuilt]},
    )
    result = rebase_production_payload_for_accepted_mutations(
        db_session, {"rows": [candidate], "summary": {"total_rows": 1}},
        parent_generation_id=int(parent.id),
        product_ids=(int(product.product_id),),
        target_payload={"rows": [candidate], "summary": {"total_rows": 1}},
    )
    row = result["rows"][0]
    assert row["order_ref1c"] == "new-order-ref"
    assert row["issue_status"] == "exported"
    assert row["available_actions"] == ["produce", "print"]
    assert (row["produced_qty"], row["remaining_qty"]) == (5, 5)
    assert row["material_coverage_snapshot"] == {"coverage_status": "ready"}


def test_preview_cache_overlay_never_replaces_target_material_truth(db_session):
    parent = _truth(db_session)
    identity = "mrp-reservation:reservation:req:77:mode:make"
    current = {
        "current_identity": identity, "product_id": None, "item_id": 7,
        "source_mrp_requirement_id": 77, "source_run_id": 9,
        "reservation_id": 11, "quantity": 10, "launchable_qty": 10,
        "materialized_order_qty": 0, "spec_id": 3, "spec_revision_hash": "spec",
        "coverage_status": "ready", "coverage_label": "Обеспечен",
        "material_coverage_snapshot": {"coverage_status": "ready", "stock_qty": 10},
        "_material_quantity_snapshots": {"3": {"payload": {"qty": 3}}},
    }
    publish_current_production_control_from_payload(
        db_session, int(parent.id), {"meta": {}, "rows": [current]},
    )
    target = {
        **current,
        "coverage_status": "shortage", "coverage_label": "Дефицит",
        "material_coverage_snapshot": {"coverage_status": "shortage", "stock_qty": 0},
    }
    target.pop("_material_quantity_snapshots")
    result = rebase_production_payload_for_accepted_mutations(
        db_session, {"rows": [target]},
        parent_generation_id=int(parent.id), product_ids=(),
        business_identities=(identity,), target_payload={"rows": [target]},
    )
    row = result["rows"][0]
    assert row["coverage_status"] == "shortage"
    assert row["material_coverage_snapshot"] == {
        "coverage_status": "shortage", "stock_qty": 0,
    }
    assert "_material_quantity_snapshots" not in row

    unchanged_target = dict(current)
    unchanged_target.pop("_material_quantity_snapshots")
    unchanged = rebase_production_payload_for_accepted_mutations(
        db_session, {"rows": [unchanged_target]},
        parent_generation_id=int(parent.id), product_ids=(),
        business_identities=(identity,),
        target_payload={"rows": [unchanged_target]},
    )["rows"][0]
    assert unchanged["_material_quantity_snapshots"] == {
        "3": {"payload": {"qty": 3}},
    }

    changed_basis = {**target, "launchable_qty": 7, "materialized_order_qty": 5}
    changed = rebase_production_payload_for_accepted_mutations(
        db_session, {"rows": [changed_basis]},
        parent_generation_id=int(parent.id), product_ids=(),
        business_identities=(identity,),
        target_payload={"rows": [changed_basis]},
    )["rows"][0]
    assert "_material_quantity_snapshots" not in changed
    assert (changed["launchable_qty"], changed["materialized_order_qty"]) == (7, 5)


def test_materialize_merge_uses_canonical_target_executor_and_residual(db_session):
    parent = _truth(db_session)
    item = models.Item(item_code="TARGET-MAKE", item_name="Target make")
    order = models.ProductionOrder(order_number="TARGET-ORDER", order_date=parent.cutoff)
    db_session.add_all((item, order))
    db_session.flush()
    product = models.ProductionProduct(
        order_id=order.order_id, item_id=item.item_id, quantity=5,
        produced_qty=0, remaining_qty=5,
    )
    db_session.add(product)
    db_session.flush()
    proposal_identity = "mrp-reservation:reservation:req:88:mode:make"
    product_identity = f"production:product:{product.product_id}"
    current_proposal = {
        "current_identity": proposal_identity, "product_id": None,
        "item_id": item.item_id, "source_mrp_requirement_id": 88,
        "source_run_id": 10, "reservation_id": 12,
        "quantity": 5, "launchable_qty": 5, "materialized_order_qty": 5,
        "spec_id": None, "spec_revision_hash": None,
    }
    current_product = {
        "current_identity": product_identity, "product_id": product.product_id,
        "order_id": order.order_id, "item_id": item.item_id, "quantity": 5,
        "spec_id": None, "spec_revision_hash": None,
        "order_ref1c": "latest-command-ref", "coverage_status": "ready",
        "material_coverage_snapshot": {"coverage_status": "ready", "stock_qty": 10},
    }
    publish_current_production_control_from_payload(
        db_session, int(parent.id),
        {"meta": {}, "rows": [current_proposal, current_product]},
    )
    target_proposal = {
        **current_proposal, "quantity": 2, "launchable_qty": 2,
        "materialized_order_qty": 5,
    }
    target_product = {
        **current_product, "order_ref1c": None, "coverage_status": "shortage",
        "material_coverage_snapshot": {"coverage_status": "shortage", "stock_qty": 0},
    }
    result = rebase_production_payload_for_accepted_mutations(
        db_session, {"rows": []}, parent_generation_id=int(parent.id),
        product_ids=(product.product_id,),
        business_identities=(proposal_identity, product_identity),
        target_payload={"rows": [target_proposal, target_product]},
    )
    rows = {row["current_identity"]: row for row in result["rows"]}
    assert (rows[proposal_identity]["quantity"], rows[proposal_identity]["launchable_qty"]) == (2, 2)
    assert rows[product_identity]["order_ref1c"] == "latest-command-ref"
    assert rows[product_identity]["coverage_status"] == "shortage"
    assert rows[product_identity]["material_coverage_snapshot"] == {
        "coverage_status": "shortage", "stock_qty": 0,
    }

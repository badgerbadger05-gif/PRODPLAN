"""R11 regression tests for generation-neutral current projection identity."""

from datetime import date
from decimal import Decimal
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app import models
from app.services.item_ledger.current_execution import (
    _semantic_view,
    load_current_execution_rows,
    publish_current_execution_scope,
)
from app.services.item_ledger.current_replenishment import (
    apply_current_replenishment,
    _input_checksum,
)
from app.services.item_ledger.historical_replay_core import Fact, Reserve
from app.services.purchase_control_projection import (
    _build_supplier_card_rows,
    _supplier_current_row_key,
)


def test_current_execution_semantic_view_drops_only_technical_fields():
    production_a = {
        "material_coverage_calculated_at": "2026-09-15T10:00:00",
        "material_coverage_snapshot": {
            "work_item_id": 10, "required": "5", "covered": "3",
            "nested": {"work_item_id": "business-id"},
        },
        "remaining_qty": "2",
    }
    production_b = {
        **production_a,
        "material_coverage_calculated_at": "2026-09-15T11:00:00",
    }
    production_b["material_coverage_snapshot"] = {
        **production_a["material_coverage_snapshot"], "work_item_id": 11,
    }
    assert _semantic_view(production_a, "production_control_journal") == _semantic_view(
        production_b, "production_control_journal"
    )
    assert _semantic_view(production_a, "production_control_journal")["material_coverage_snapshot"]["nested"]["work_item_id"] == "business-id"
    changed = {**production_b, "material_coverage_snapshot": {"required": "5", "covered": "4"}}
    assert _semantic_view(production_a, "production_control_journal") != _semantic_view(
        changed, "production_control_journal"
    )

    period_a = {"queue_links": [{"current_identity": "plan-line:1", "source_revision": "accepted:g1"}]}
    period_b = {"queue_links": [{"current_identity": "plan-line:1", "source_revision": "accepted:g2"}]}
    assert _semantic_view(period_a, "period_plan_execution") == _semantic_view(
        period_b, "period_plan_execution"
    )
    assert _semantic_view(
        {"queue_links": [{"current_identity": "plan-line:2", "source_revision": "accepted:g2"}]},
        "period_plan_execution",
    ) != _semantic_view(period_b, "period_plan_execution")

    purchase_a = {
        "horizon_buckets": [{"work_item_id": 10, "to_order_qty": "5"}],
        "slices": [{"work_item_id": 10, "to_order_qty": "5"}],
        "materialization_input": {"slices": [{"work_item_id": 10, "to_order_qty": "5"}]},
    }
    purchase_b = {
        "horizon_buckets": [{"work_item_id": 11, "to_order_qty": "5"}],
        "slices": [{"work_item_id": 11, "to_order_qty": "5"}],
        "materialization_input": {"slices": [{"work_item_id": 11, "to_order_qty": "5"}]},
    }
    assert _semantic_view(purchase_a, "purchase_control_journal") == _semantic_view(
        purchase_b, "purchase_control_journal"
    )
    assert _semantic_view(
        {**purchase_b, "slices": [{"work_item_id": 11, "to_order_qty": "6"}]},
        "purchase_control_journal",
    ) != _semantic_view(purchase_a, "purchase_control_journal")


def test_current_execution_publish_skips_technical_only_generation_refresh(db_session):
    cutoff = datetime(2026, 9, 15, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key=f"r11-publish-churn-{uuid4().hex[:10]}", status="completed",
        cutoff=cutoff, source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=f"r11-publish-generation-{uuid4().hex[:10]}", status="accepted",
        cutoff=cutoff, accepted_at=cutoff, source_watermarks={}, capabilities={},
        algorithm_version="r11-test", physical_import_batch=batch,
    )
    db_session.add(generation)
    db_session.flush()
    cases = (
        (
            "production_control_journal", "production:churn", "production:1",
            {"material_coverage_calculated_at": "10:00", "material_coverage_snapshot": {"covered": 3},
             "nested": {"material_coverage_calculated_at": "business-value"}},
            {"material_coverage_calculated_at": "11:00", "material_coverage_snapshot": {"covered": 3},
             "nested": {"material_coverage_calculated_at": "business-value"}},
            {"material_coverage_calculated_at": "11:00", "material_coverage_snapshot": {"covered": 4},
             "nested": {"material_coverage_calculated_at": "business-value"}},
        ),
        (
            "period_plan_execution", "period:churn", "period:1",
            {"queue_links": [{"current_identity": "plan-line:1", "href": "#/queue/1", "source_revision": "accepted:g1"}]},
            {"queue_links": [{"current_identity": "plan-line:1", "href": "#/queue/1", "source_revision": "accepted:g2"}]},
            {"queue_links": [{"current_identity": "plan-line:1", "href": "#/queue/2", "source_revision": "accepted:g2"}]},
        ),
        (
            "purchase_control_journal", "purchase:churn", "purchase:1",
            {"horizon_buckets": [{"work_item_id": 1, "to_order_qty": 5}], "slices": [{"work_item_id": 1, "to_order_qty": 5}],
             "materialization_input": {"slices": [{"work_item_id": 1, "to_order_qty": 5}]}},
            {"horizon_buckets": [{"work_item_id": 2, "to_order_qty": 5}], "slices": [{"work_item_id": 2, "to_order_qty": 5}],
             "materialization_input": {"slices": [{"work_item_id": 2, "to_order_qty": 5}]}},
            {"horizon_buckets": [{"work_item_id": 2, "to_order_qty": 6}], "slices": [{"work_item_id": 2, "to_order_qty": 6}],
             "materialization_input": {"slices": [{"work_item_id": 2, "to_order_qty": 6}]}},
        ),
    )
    total_changes = 0
    for entity_kind, scope, identity, first_payload, technical_payload, business_payload in cases:
        first = publish_current_execution_scope(
            db_session, source_revision="accepted:g1", source_generation_id=generation.id,
            scope_key=scope, rows=[{"entity_kind": entity_kind, "business_identity": identity, "payload": first_payload}],
            entity_kinds=(entity_kind,),
        )
        db_session.flush()
        total_changes += 1
        second = publish_current_execution_scope(
            db_session, source_revision="accepted:g2", source_generation_id=generation.id,
            scope_key=scope, rows=[{"entity_kind": entity_kind, "business_identity": identity, "payload": technical_payload}],
            entity_kinds=(entity_kind,),
        )
        db_session.flush()
        assert first.changed_rows == 1
        assert second.changed_rows == 0
        assert db_session.query(models.CurrentExecutionChange).count() == total_changes
        if entity_kind == "production_control_journal":
            stored = load_current_execution_rows(
                db_session, entity_kind=entity_kind, scope_key=scope
            )[0].payload
            # The semantic no-op keeps the prior technical timestamp, while a
            # same-named nested business value remains visible and comparable.
            assert stored["material_coverage_calculated_at"] == "10:00"
            assert stored["nested"]["material_coverage_calculated_at"] == "business-value"
        third = publish_current_execution_scope(
            db_session, source_revision="accepted:g3", source_generation_id=generation.id,
            scope_key=scope, rows=[{"entity_kind": entity_kind, "business_identity": identity, "payload": business_payload}],
            entity_kinds=(entity_kind,),
        )
        db_session.flush()
        total_changes += 1
        assert third.changed_rows == 1
        assert db_session.query(models.CurrentExecutionChange).count() == total_changes


def test_production_semantic_view_handles_snapshot_work_item_and_legacy_hash(db_session):
    cutoff = datetime(2026, 9, 15, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key=f"r11-production-hash-{uuid4().hex[:10]}", status="completed",
        cutoff=cutoff, source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=f"r11-production-hash-generation-{uuid4().hex[:10]}", status="accepted",
        cutoff=cutoff, accepted_at=cutoff, source_watermarks={}, capabilities={},
        algorithm_version="r11-test", physical_import_batch=batch,
    )
    db_session.add(generation)
    db_session.flush()
    base = {
        "current_identity": "production-order-line:r11-hash",
        "material_coverage_snapshot": {"work_item_id": 10, "stock_qty": 5, "available_qty": 3},
    }
    first = publish_current_execution_scope(
        db_session, source_revision="accepted:g1", source_generation_id=generation.id,
        scope_key="production:r11-hash", entity_kinds=("production_control_journal",),
        rows=[{"entity_kind": "production_control_journal", "business_identity": base["current_identity"], "payload": base}],
    )
    db_session.flush()
    row = db_session.query(models.CurrentExecutionRow).one()
    old_hash = row.content_hash
    old_payload = dict(row.payload)
    row.content_hash = "legacy-raw-hash"
    db_session.flush()
    changes = db_session.query(models.CurrentExecutionChange).count()

    technical = {
        **base,
        "material_coverage_snapshot": {"work_item_id": 11, "stock_qty": 5, "available_qty": 3},
    }
    second = publish_current_execution_scope(
        db_session, source_revision="accepted:g2", source_generation_id=generation.id,
        scope_key="production:r11-hash", entity_kinds=("production_control_journal",),
        rows=[{"entity_kind": "production_control_journal", "business_identity": base["current_identity"], "payload": technical}],
    )
    db_session.flush()
    assert first.changed_rows == 1
    assert second.changed_rows == 0
    assert db_session.query(models.CurrentExecutionChange).count() == changes
    assert row.content_hash == "legacy-raw-hash"
    assert dict(row.payload) == old_payload

    business = {
        **technical,
        "material_coverage_snapshot": {"work_item_id": 11, "stock_qty": 4, "available_qty": 3},
    }
    third = publish_current_execution_scope(
        db_session, source_revision="accepted:g3", source_generation_id=generation.id,
        scope_key="production:r11-hash", entity_kinds=("production_control_journal",),
        rows=[{"entity_kind": "production_control_journal", "business_identity": base["current_identity"], "payload": business}],
    )
    db_session.flush()
    assert third.changed_rows == 1
    assert db_session.query(models.CurrentExecutionChange).count() == changes + 1
    assert row.content_hash != "legacy-raw-hash"


def test_publish_manual_input_change_is_not_hidden_by_semantic_hash_fallback(db_session):
    cutoff = datetime(2026, 9, 15, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key=f"r11-manual-hash-{uuid4().hex[:10]}", status="completed",
        cutoff=cutoff, source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=f"r11-manual-generation-{uuid4().hex[:10]}", status="accepted",
        cutoff=cutoff, accepted_at=cutoff, source_watermarks={}, capabilities={},
        algorithm_version="r11-test", physical_import_batch=batch,
    )
    db_session.add(generation)
    db_session.flush()
    row = {"current_identity": "purchase:r11-manual", "quantity": 5}
    first = publish_current_execution_scope(
        db_session, source_revision="accepted:g1", source_generation_id=generation.id,
        scope_key="purchase:r11-manual", entity_kinds=("purchase_control_journal",),
        rows=[{"entity_kind": "purchase_control_journal", "business_identity": row["current_identity"],
               "payload": row, "manual_input": {"note": "first"}}],
    )
    db_session.flush()
    changes = db_session.query(models.CurrentExecutionChange).count()
    second = publish_current_execution_scope(
        db_session, source_revision="accepted:g2", source_generation_id=generation.id,
        scope_key="purchase:r11-manual", entity_kinds=("purchase_control_journal",),
        rows=[{"entity_kind": "purchase_control_journal", "business_identity": row["current_identity"],
               "payload": row, "manual_input": {"note": "second"}}],
    )
    db_session.flush()
    assert first.changed_rows == 1
    assert second.changed_rows == 1
    assert db_session.query(models.CurrentExecutionChange).count() == changes + 1
    stored = db_session.query(models.CurrentExecutionRow).one()
    assert stored.manual_input == {"note": "second"}


def test_supplier_row_key_uses_stable_identity_and_is_bounded():
    assert _supplier_current_row_key("supplier_order:SO-1:1:") == "ledger-supply:supplier_order:SO-1:1:"
    long_identity = "x" * 256
    key = _supplier_current_row_key(long_identity)
    assert key.startswith("ledger-supply:sha256:")
    assert len(key) <= 256
    assert _supplier_current_row_key(long_identity) == key


def test_supplier_builder_keeps_row_key_across_physical_generations(db_session):
    token = uuid4().hex[:10]
    item = models.Item(item_code=f"R11-SUPPLY-{token}", item_name="R11")
    db_session.add(item)
    generations = []
    for ordinal in (1, 2):
        cutoff = datetime(2026, 9, 15, ordinal, tzinfo=timezone.utc)
        batch = models.PhysicalImportBatch(
            batch_key=f"r11-supply-physical-{token}-{ordinal}", status="completed",
            cutoff=cutoff, source_watermarks={},
        )
        generation = models.LedgerGeneration(
            generation_key=f"r11-supply-generation-{token}-{ordinal}", status="building",
            cutoff=cutoff, source_watermarks={}, capabilities={},
            algorithm_version="r11-test", physical_import_batch=batch,
        )
        db_session.add(generation)
        db_session.flush()
        capture = models.LedgerBuildBatch(
            ledger_generation_id=generation.id, stage="future_supply_capture", status="completed",
            batch_key=f"r11-supply-capture-{token}-{ordinal}",
            algorithm_version="r11-test", metrics={},
        )
        db_session.add(capture)
        db_session.flush()
        db_session.add(models.LedgerFutureSupply(
            ledger_generation_id=generation.id, supply_kind="supplier_order",
            item_id=item.item_id, planning_stock_pool="default", source_ref=f"SO-{ordinal}",
            source_line_ref="1", source_local_id="line-1", ordered_qty_at_cutoff=Decimal("5"),
            realized_qty_at_cutoff=Decimal("1"), open_qty_at_cutoff=Decimal("4"),
            source_state_key="open", capture_cutoff=cutoff, source_content_hash=(str(ordinal) * 64),
            capture_batch_id=capture.id, current_identity="supplier-order:stable:1:",
            evidence_status="exact",
        ))
        generations.append(generation)
    db_session.flush()
    first_rows, _ = _build_supplier_card_rows(db_session, generations[0])
    second_rows, _ = _build_supplier_card_rows(db_session, generations[1])
    assert first_rows[0]["row_key"] == second_rows[0]["row_key"]
    assert first_rows[0]["row_key"] == "ledger-supply:supplier-order:stable:1:"


def test_replenishment_input_checksum_ignores_physical_reservation_id():
    common = dict(
        item_id=7,
        mode="buy",
        reserved_qty=Decimal("5"),
        due_date=date(2026, 9, 30),
        plan_period_from=date(2026, 9, 1),
        plan_period_to=date(2026, 9, 30),
        run_id=2,
        requirement_id=10,
    )
    first = Reserve(reserve_id="101", **common)
    second = Reserve(reserve_id="202", **common)
    assert _input_checksum((), (first,), "[7,\"\",\"\",\"selected\",\"buy\"]") == _input_checksum(
        (), (second,), "[7,\"\",\"\",\"selected\",\"buy\"]"
    )


def test_replenishment_reuses_assignment_when_building_id_changes(db_session):
    token = uuid4().hex[:10]
    cutoff = datetime(2026, 9, 15, tzinfo=timezone.utc)
    batch1 = models.PhysicalImportBatch(
        batch_key=f"r11-churn-batch-1-{token}", status="completed", cutoff=cutoff,
        source_watermarks={},
    )
    batch2 = models.PhysicalImportBatch(
        batch_key=f"r11-churn-batch-2-{token}", status="completed", cutoff=cutoff,
        source_watermarks={},
    )
    gen1 = models.LedgerGeneration(
        generation_key=f"r11-churn-gen-1-{token}", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, source_watermarks={}, capabilities={},
        algorithm_version="r11-test", physical_import_batch=batch1,
    )
    gen2 = models.LedgerGeneration(
        generation_key=f"r11-churn-gen-2-{token}", status="building", cutoff=cutoff,
        source_watermarks={}, capabilities={},
        algorithm_version="r11-test", physical_import_batch=batch2,
    )
    item = models.Item(item_code=f"R11-CHURN-{token}", item_name="R11")
    db_session.add_all([gen1, gen2, item])
    db_session.flush()
    run = models.PlanningRun(config_snapshot={})
    db_session.add(run)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id, item_id=item.item_id,
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
        total_required_qty=Decimal("5"), net_required_qty=Decimal("5"),
    )
    db_session.add(requirement)
    db_session.flush()
    identity = f"reservation:req:{requirement.id}:mode:buy"
    owner = models.ReservationEntry(
        ledger_generation_id=gen1.id, item_id=item.item_id, run_id=run.run_id,
        requirement_id=requirement.id, realization_mode="buy", reserved_qty=Decimal("5"),
        replenishment_required_qty=Decimal("5"), current_identity=identity,
        owner_kind="current", is_current=True, lifecycle_status="active",
        priority_period_from=date(2026, 9, 1), priority_period_to=date(2026, 9, 30),
    )
    stage = models.ReservationEntry(
        ledger_generation_id=gen2.id, item_id=item.item_id, run_id=run.run_id,
        requirement_id=requirement.id, realization_mode="buy", reserved_qty=Decimal("5"),
        replenishment_required_qty=Decimal("5"), current_identity=identity,
        owner_kind="building", is_current=False, lifecycle_status="active",
        priority_period_from=date(2026, 9, 1), priority_period_to=date(2026, 9, 30),
    )
    db_session.add_all([owner, stage])
    db_session.flush()
    fact = Fact(
        fact_id="9001", item_id=item.item_id, mode="buy", qty=Decimal("2"),
        posting_at=cutoff,
    )
    first = apply_current_replenishment(
        db_session, generation_id=gen1.id, source_key="physical:r11-churn",
        source_revision=1, facts=(fact,), reserves=(Reserve(
            reserve_id=str(owner.id), item_id=item.item_id, mode="buy", reserved_qty=Decimal("5"),
            due_date=date(2026, 9, 30), plan_period_from=date(2026, 9, 1),
            plan_period_to=date(2026, 9, 30), run_id=run.run_id, requirement_id=requirement.id,
        ),), complete_scope=True,
    )
    db_session.flush()
    second = apply_current_replenishment(
        db_session, generation_id=gen2.id, source_key="physical:r11-churn",
        source_revision=2, facts=(fact,), reserves=(Reserve(
            reserve_id=str(stage.id), item_id=item.item_id, mode="buy", reserved_qty=Decimal("5"),
            due_date=date(2026, 9, 30), plan_period_from=date(2026, 9, 1),
            plan_period_to=date(2026, 9, 30), run_id=run.run_id, requirement_id=requirement.id,
        ),), complete_scope=True, allow_building=True,
    )
    assert first.changed_pairs == 1
    assert second.changed_pairs == 0
    assert second.audit_events == 0
    allocation = db_session.query(models.ReservationConsumptionAllocation).one()
    assert allocation.reservation_id == owner.id


def test_replenishment_rejects_mismatched_reservation_identity(db_session):
    from tests.services.test_current_replenishment_transaction import _reserves, _world
    from app.services.item_ledger.current_replenishment import CurrentReplenishmentError

    generation_id, _item_id, reservations, facts = _world(db_session, prefix="identity-mismatch")
    reservations[0].current_identity = "reservation:req:wrong:mode:buy"
    with pytest.raises(CurrentReplenishmentError, match="mismatched current identity"):
        apply_current_replenishment(
            db_session,
            generation_id=generation_id,
            source_key="physical:identity-mismatch",
            source_revision=1,
            facts=facts,
            reserves=_reserves(reservations),
            complete_scope=True,
        )

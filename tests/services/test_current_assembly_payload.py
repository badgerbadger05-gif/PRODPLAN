from datetime import date, datetime
from decimal import Decimal

import pytest

from app import models
from app.services.mrp_stock_helpers import DEFAULT_ORGANIZATION_REF1C
from app.services.item_ledger.current_execution import (
    build_compact_current_assembly_payload,
    CurrentExecutionUnavailable,
    resolve_compact_queue_owner_ids,
)
from app.services.item_ledger.drum_schedule_persistence import (
    build_compact_current_drum_payload,
)
from app.services.item_ledger.shelf_projection_persistence import (
    build_compact_current_shelf_payload,
)


def _world(db):
    parent_batch = models.PhysicalImportBatch(
        batch_key="compact-assembly-parent-batch",
        status="completed",
        cutoff=datetime(2026, 9, 10),
        source_watermarks={},
    )
    target_batch = models.PhysicalImportBatch(
        batch_key="compact-assembly-target-batch",
        status="completed",
        cutoff=datetime(2026, 9, 11),
        source_watermarks={},
    )
    parent = models.LedgerGeneration(
        generation_key="compact-assembly-parent-generation",
        status="accepted",
        cutoff=parent_batch.cutoff,
        accepted_at=parent_batch.cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=parent_batch,
        algorithm_version="compact-assembly-test",
    )
    target = models.LedgerGeneration(
        generation_key="compact-assembly-target-generation",
        status="building",
        cutoff=target_batch.cutoff,
        source_watermarks={"parent_generation_id": parent.id},
        capabilities={"physical_ledger": True},
        physical_import_batch=target_batch,
        algorithm_version="compact-assembly-test",
    )
    root = models.Item(item_code="COMPACT-ROOT", item_name="compact root")
    child = models.Item(item_code="COMPACT-CHILD", item_name="compact child")
    plan = models.ProductionPlanHeader(
        name="Compact assembly plan",
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        status="fixed",
        fixed_at=datetime(2026, 9, 1),
    )
    db.add_all([parent_batch, target_batch, parent, target, root, child, plan])
    db.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        ledger_generation_id=parent.id,
        ledger_cutoff=parent.cutoff,
        active_freeze_version=1,
    )
    line = models.ProductionPlanLine(
        plan_id=plan.id,
        item_id=root.item_id,
        bucket_date=date(2026, 9, 10),
        qty=Decimal("3"),
        accepted_output_qty=Decimal("0"),
        remaining_output_qty=Decimal("3"),
    )
    db.add_all([run, line])
    db.flush()
    db.add_all([
        models.MrpFreezeComponent(
            run_id=run.run_id,
            freeze_version=1,
            root_item_id=root.item_id,
            parent_item_id=root.item_id,
            component_item_id=child.item_id,
            spec_ref="ROOT",
            child_spec_ref="",
            norm_qty_per_unit=Decimal("1"),
            unit_coef=Decimal("1"),
        ),
        models.MrpFreezeBomNode(
            run_id=run.run_id,
            freeze_version=1,
            root_item_id=root.item_id,
            item_id=root.item_id,
            spec_ref="ROOT",
            replenishment_mode="make",
            material_warehouse_ref1c="WH-MATERIAL",
            output_warehouse_ref1c="WH-FINISHED",
        ),
        models.MrpFreezeBomNode(
            run_id=run.run_id,
            freeze_version=1,
            root_item_id=root.item_id,
            item_id=child.item_id,
            spec_ref="",
            replenishment_mode="buy",
            material_warehouse_ref1c="",
            output_warehouse_ref1c="",
        ),
        models.StockWarehouse(
            warehouse_ref1c="WH-SOURCE",
            warehouse_name="source",
            is_selected=True,
            is_finished_goods=False,
        ),
        models.StockWarehouse(
            warehouse_ref1c="WH-MATERIAL",
            warehouse_name="material",
            is_selected=True,
            is_finished_goods=False,
        ),
        models.StockBin(
            ledger_generation_id=parent.id,
            item_id=child.item_id,
            organization_ref=DEFAULT_ORGANIZATION_REF1C,
            warehouse_ref1c="WH-SOURCE",
            on_hand=Decimal("3"),
            is_current=True,
        ),
        models.ProductionMaterialCustodyProjectionManifest(
            ledger_generation_id=parent.id,
            cutoff=parent.cutoff,
            status="complete",
            is_baseline=True,
            source_event_high_watermark_id=0,
        ),
        models.PlanningTruthState(id=1, current_generation_id=parent.id),
    ])
    db.commit()
    return parent, target, root, child, line


def _build(db, parent, target, root):
    return build_compact_current_assembly_payload(
        db,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        affected_physical_keys=((root.item_id, "", DEFAULT_ORGANIZATION_REF1C, "WH-SOURCE"),),
    )


def test_compact_assembly_payload_matches_scope_shape_without_staging_or_sle_scan(
    db_session, monkeypatch
):
    parent, target, root, _child, line = _world(db_session)
    from app.services.item_ledger import physical_visibility

    monkeypatch.setattr(
        physical_visibility,
        "visible_sles_for_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("compact readiness must not scan visible SLE history")
        ),
    )
    first = _build(db_session, parent, target, root)
    second = _build(db_session, parent, target, root)

    assert first.queue_rows == second.queue_rows
    assert first.readiness_rows == second.readiness_rows
    assert first.queue_rows[0]["business_identity"] == f"plan-line:{line.id}"
    assert first.readiness_rows[0]["business_identity"] == f"plan-line:{line.id}"
    assert db_session.query(models.AssemblyQueueLine).filter_by(
        ledger_generation_id=target.id
    ).count() == 0
    assert db_session.query(models.AssemblyReadiness).filter_by(
        ledger_generation_id=target.id
    ).count() == 0
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id


def test_compact_assembly_readiness_uses_current_stock_without_target_staging(db_session):
    parent, target, root, child, _line = _world(db_session)
    stock = db_session.query(models.StockBin).filter_by(item_id=child.item_id).one()
    stock.on_hand = Decimal("0")
    db_session.flush()
    blocked = _build(db_session, parent, target, root)
    stock.on_hand = Decimal("3")
    db_session.flush()
    ready = _build(db_session, parent, target, root)

    blocked_status = blocked.readiness_rows[0]["payload"]["status"]
    ready_status = ready.readiness_rows[0]["payload"]["status"]
    assert blocked_status != ready_status or (
        blocked.readiness_rows[0]["payload"]["ready_qty"]
        != ready.readiness_rows[0]["payload"]["ready_qty"]
    )
    assert ready.readiness_rows[0]["payload"]["queue_line_id"] == blocked.readiness_rows[0]["payload"]["queue_line_id"]


def test_compact_drum_payload_is_deterministic_and_has_no_staging_ids(db_session):
    parent, target, root, _child, _line = _world(db_session)
    assembly = _build(db_session, parent, target, root)
    first = build_compact_current_drum_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload=assembly,
    )
    second = build_compact_current_drum_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload=assembly,
    )
    assert first.rows == second.rows
    assert all(
        "queue_line_id" not in (row.get("payload") or {})
        for row in first.rows
        if row["entity_kind"] != "drum_schedule"
    )
    assert db_session.query(models.DrumSchedule).filter_by(
        ledger_generation_id=target.id
    ).count() == 0
    assert db_session.query(models.DrumSlot).count() == 0
    assert db_session.query(models.DrumCapacityGap).count() == 0


_READINESS_COPY_KEYS = {
    "readiness_curve",
    "action_manifest",
    "blocking_manifest",
    "unavailable_reasons",
    "readiness_date",
    "readiness_status",
}


def _with_takt(db, root):
    """Give the root a takt so the drum produces a real slot, not only exclusions."""
    resource = models.ProductionResource(
        resource_name="Compact drum resource", planning_range=3, capacity=Decimal("5")
    )
    root.optimal_batch = Decimal("1")
    db.add(resource)
    db.flush()
    db.add(models.AssemblyRate(
        resource_id=resource.resource_id,
        item_id=root.item_id,
        qty_per_capacity=Decimal("1"),
    ))
    db.flush()
    return resource


def _restate_readiness_explanations(assembly):
    """Return the same assembly payload with only its *explanations* rewritten.

    Quantities, statuses and curve horizons are untouched, so the drum plan is
    the same plan; only the human-facing blockers and actions differ.
    """
    from copy import deepcopy

    rows = []
    for raw in assembly.readiness_rows:
        row = deepcopy(dict(raw))
        payload = dict(row["payload"])
        payload["blocking_manifest"] = [{"reason": "RESTATED_BLOCKER"}]
        payload["action_manifest"] = [{"action_kind": "make", "item_id": 1, "qty": "1"}]
        payload["unavailable_reasons"] = ["RESTATED_REASON"]
        payload["readiness_curve"] = [
            {**dict(point), "blockers": [{"reason": "RESTATED_BLOCKER"}],
             "actions": [{"action_kind": "make", "item_id": 1, "qty": "1"}],
             "required_actions": [{"action_kind": "make", "item_id": 1, "qty": "1"}]}
            for point in list(payload.get("readiness_curve") or [])
        ]
        row["payload"] = payload
        rows.append(row)
    return type(assembly)(
        **{
            **{
                field: getattr(assembly, field)
                for field in assembly.__dataclass_fields__
            },
            "readiness_rows": tuple(rows),
        }
    )


def test_compact_drum_rows_link_to_readiness_instead_of_copying_it(db_session):
    parent, target, root, _child, _line = _world(db_session)
    _with_takt(db_session, root)
    assembly = _build(db_session, parent, target, root)
    drum = build_compact_current_drum_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload=assembly,
    )
    business_rows = [row for row in drum.rows if row["entity_kind"] != "drum_schedule"]
    assert business_rows, "the fixture must produce at least one drum row"
    for row in business_rows:
        payload = row["payload"]
        assert not (_READINESS_COPY_KEYS & set(payload)), (
            f"{row['entity_kind']} still copies {_READINESS_COPY_KEYS & set(payload)}"
        )
        assert payload["readiness_ref"] == f"plan-line:{int(payload['plan_line_id'])}"


def test_readiness_only_change_does_not_move_the_drum_rows(db_session):
    parent, target, root, _child, _line = _world(db_session)
    _with_takt(db_session, root)
    assembly = _build(db_session, parent, target, root)

    def drum_for(payload):
        return build_compact_current_drum_payload(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            assembly_payload=payload,
        )

    before = drum_for(assembly)
    after = drum_for(_restate_readiness_explanations(assembly))
    assert before.rows == after.rows


def test_readiness_only_change_writes_no_drum_change_rows(db_session):
    from app.services.item_ledger.current_execution import publish_current_execution_scope

    parent, target, root, _child, _line = _world(db_session)
    _with_takt(db_session, root)
    assembly = _build(db_session, parent, target, root)
    drum_kinds = ("drum_schedule", "drum_slot", "drum_gap", "drum_excluded")

    def publish(payload, revision):
        drum = build_compact_current_drum_payload(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            assembly_payload=payload,
        )
        publish_current_execution_scope(
            db_session,
            source_revision=revision,
            source_generation_id=int(parent.id),
            scope_key="assembly:all-live-plans",
            entity_kinds=("assembly_readiness",),
            rows=[dict(row, scope_key="assembly:all-live-plans")
                  for row in payload.readiness_rows],
        )
        publish_current_execution_scope(
            db_session,
            source_revision=revision,
            source_generation_id=int(parent.id),
            scope_key="drum:all-live-plans",
            entity_kinds=drum_kinds,
            rows=list(drum.rows),
        )
        db_session.flush()

    publish(assembly, "physical:g1")
    baseline = db_session.query(models.CurrentExecutionChange).count()
    publish(_restate_readiness_explanations(assembly), "physical:g2")

    new_changes = db_session.query(models.CurrentExecutionChange).order_by(
        models.CurrentExecutionChange.id.asc(),
    ).all()[baseline:]
    assert new_changes, "the readiness change itself must still be audited"
    assert {row.entity_kind for row in new_changes} == {"assembly_readiness"}
    assert not [row for row in new_changes if row.entity_kind in drum_kinds]


def test_compact_queue_owner_resolution_passes_schedule_and_resolves_dependents(db_session):
    db_session.add(models.CurrentExecutionRow(
        entity_kind="assembly_queue",
        business_identity="plan-line:42",
        scope_key="assembly:all-live-plans",
        source_revision="test",
        result_status="accepted",
        result_ready=True,
        content_hash="a" * 64,
        payload={"plan_line_id": 42},
        manual_input={},
    ))
    db_session.flush()
    schedule = {
        "entity_kind": "drum_schedule",
        "business_identity": "drum:all-live-plans",
        "payload": {"metrics": {"slots": 1}},
    }
    slot = {
        "entity_kind": "drum_slot",
        "business_identity": "slot:plan-line:42:ordinal:1",
        "payload": {"plan_line_id": 42, "slot_ordinal": 1},
    }
    readiness = {
        "entity_kind": "assembly_readiness",
        "business_identity": "plan-line:42",
        "payload": {"plan_line_id": 42, "status": "ready"},
    }
    resolved = resolve_compact_queue_owner_ids(db_session, (schedule, readiness, slot))
    assert resolved[0] == schedule
    owner_id = db_session.query(
        models.CurrentExecutionRow
    ).one().id
    assert resolved[1]["payload"]["queue_line_id"] == owner_id
    assert resolved[2]["payload"]["queue_line_id"] == owner_id

    with pytest.raises(CurrentExecutionUnavailable, match="unknown compact queue"):
        resolve_compact_queue_owner_ids(
            db_session,
            ({"entity_kind": "drum_unknown", "payload": {}},),
        )


def test_compact_shelf_payload_uses_current_owners_without_projection_rows(db_session):
    parent, target, root, child, _line = _world(db_session)
    run = db_session.query(models.PlanningRun).filter_by(
        ledger_generation_id=parent.id
    ).one()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=child.item_id,
        total_required_qty=Decimal("3"),
        net_required_qty=Decimal("3"),
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        bom_level=1,
    )
    resource = models.ProductionResource(
        resource_name="Compact shelf resource", planning_range=2, capacity=Decimal("5")
    )
    root.optimal_batch = Decimal("1")
    db_session.add_all([requirement, resource])
    db_session.flush()
    db_session.add_all([
        models.AssemblyRate(resource_id=resource.resource_id, item_id=root.item_id, qty_per_capacity=Decimal("1")),
        models.MrpFreezeComponentCumulative(
            run_id=run.run_id,
            freeze_version=1,
            root_item_id=root.item_id,
            component_item_id=child.item_id,
            cumulative_norm_qty_per_root_unit=Decimal("1"),
        ),
        models.ReservationEntry(
            ledger_generation_id=parent.id,
            item_id=child.item_id,
            run_id=run.run_id,
            requirement_id=requirement.id,
            priority_period_from=date(2026, 9, 1),
            priority_period_to=date(2026, 9, 30),
            realization_mode="make",
            reserved_qty=Decimal("3"),
            replenishment_required_qty=Decimal("3"),
            current_identity="reservation:compact-shelf",
            owner_kind="current",
            is_current=True,
        ),
        models.ShelfPolicy(
            item_id=child.item_id,
            warehouse_ref1c="WH-SOURCE",
            replenishment_time_days=1,
            review_cycle_days=0,
            safety_days=0,
            batch_multiple=Decimal("1"),
        ),
    ])
    db_session.flush()
    assembly = _build(db_session, parent, target, root)
    drum = build_compact_current_drum_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload=assembly,
    )
    shelf = build_compact_current_shelf_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        drum_payload=drum,
    )
    assert len(shelf.rows) == 1
    assert shelf.rows[0]["payload"]["item_id"] == child.item_id
    assert db_session.query(models.ShelfProjection).filter_by(
        ledger_generation_id=target.id
    ).count() == 0

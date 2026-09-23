from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app import models
from app.routers.production_control import get_orders_journal
from app.services import production_control_material_availability as material_availability
from app.services.planning_truth import publish_generation
from app.services.production_control_material_availability import get_materials_snapshot
from app.services.production_material_custody_projection import (
    initialize_material_custody_baseline,
)
from types import SimpleNamespace

from app.services.production_control_journal_projection import (
    ProductionControlJournalPromotionError,
    ProductionControlJournalUnavailable,
    RouteSheetSnapshotUnavailable,
    list_root_product_options,
    _public_journal_row,
    _compact_business_payload,
    _drum_readiness_pull_by_run_item,
    _affected_production_product_ids,
    build_candidate_payload,
    build_compact_current_production_control_payload,
    read_route_sheet_snapshot_rows,
    validate_candidate_payload,
    read_current_projection,
)
from app.services.production_control_journal import list_make_proposals
from app.services.item_ledger.future_supply_capture import (
    FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION,
    replace_future_supply_capture,
)
from app.services.production_control_common import DONE_STATE_KEY
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    publish_current_production_control_from_payload,
)


CAPABILITIES = {
    "physical_ledger": True,
    "reservation_replay": True,
    "execution_allocations": True,
    "planning_snapshots": True,
    "production_control_journal": True,
    "future_supply": True,
}


def test_public_journal_row_strips_internal_material_snapshot():
    source = {
        "product_id": 7,
        "reservation_id": 11838054,
        "material_coverage_snapshot": {"components": [{"item_id": 9}]},
        "_route_sheet_snapshot": {"version": 1},
    }

    assert _public_journal_row(source) == {"product_id": 7}
    assert source["reservation_id"] == 11838054
    assert "material_coverage_snapshot" in source
    assert "_route_sheet_snapshot" in source


def test_affected_product_scope_accepts_nullable_production_spec_id(db_session):
    """1C order lines may omit spec_id; affected-scope lookup must stay safe."""
    _item, _order, product = _journal_line(db_session)
    db_session.flush()
    assert product.spec_id is None

    impacted = _affected_production_product_ids(
        db_session,
        product_ids=[product.product_id],
        affected_item_ids=[product.item_id],
    )

    assert impacted == {product.product_id}


def test_compact_business_payload_has_no_legacy_snapshot_locator():
    source = {
        "generation_id": 12,
        "snapshot_id": 34,
        "business": {"item_id": 7, "qty": "2"},
    }

    compact = _compact_business_payload(source)

    assert compact == {"business": {"item_id": 7, "qty": "2"}}
    assert "planning_read_snapshot_id" not in compact


def test_current_journal_sort_keeps_nulls_last_and_tie_breakers_ascending(db_session):
    generation = _building_generation(db_session, "journal-current-sort-contract")
    generation.status = "accepted"
    generation.accepted_at = generation.cutoff
    db_session.flush()
    payload_rows = [
        {
            "current_identity": "production-order-line:1:101",
            "order_id": 1,
            "product_id": 101,
            "item_id": 101,
            "line_number": 2,
            "order_number": "SORT-B",
            "order_date": "2026-09-01",
            "planned_start_date": "2026-09-10",
            "root_item_ids": [],
        },
        {
            "current_identity": "production-order-line:2:102",
            "order_id": 2,
            "product_id": 102,
            "item_id": 102,
            "line_number": 1,
            "order_number": "SORT-A",
            "order_date": "2026-09-01",
            "planned_start_date": "2026-09-10",
            "root_item_ids": [],
        },
        {
            "current_identity": "production-order-line:3:103",
            "order_id": 3,
            "product_id": 103,
            "item_id": 103,
            "line_number": 1,
            "order_number": "SORT-DATE",
            "order_date": "2026-09-01",
            "planned_start_date": "2026-09-11",
            "root_item_ids": [],
        },
        {
            "current_identity": "production-order-line:4:104",
            "order_id": 4,
            "product_id": 104,
            "item_id": 104,
            "line_number": 1,
            "order_number": "SORT-NULL",
            "order_date": "2026-09-01",
            "planned_start_date": None,
            "root_item_ids": [],
        },
    ]
    publish_current_production_control_from_payload(
        db_session,
        generation.id,
        {
            "meta": {
                "ledger_generation_id": generation.id,
                "truth_status": "accepted",
                "read_only": True,
                "row_count": len(payload_rows),
            },
            "rows": payload_rows,
        },
    )
    db_session.flush()

    asc = read_current_projection(
        db_session,
        sort_by="planned_start_date",
        sort_dir="asc",
    )
    desc = read_current_projection(
        db_session,
        sort_by="planned_start_date",
        sort_dir="desc",
    )

    assert [row["order_number"] for row in asc["rows"]] == [
        "SORT-A", "SORT-B", "SORT-DATE", "SORT-NULL",
    ]
    assert [row["order_number"] for row in desc["rows"]] == [
        "SORT-DATE", "SORT-A", "SORT-B", "SORT-NULL",
    ]


@pytest.mark.parametrize("changed_field, changed_value", [
    ("norm_qty_per_unit", 3),
    ("unit_coef", 2),
    ("spec_version", "other-revision"),
])
def test_material_bom_does_not_hide_conflicting_root_copies(changed_field, changed_value):
    values = dict(run_id=1, freeze_version=1, parent_item_id=10,
                  component_item_id=20, spec_ref="weld", spec_version="v1",
                  norm_qty_per_unit=2, unit_coef=1)
    first = models.MrpFreezeComponent(root_item_id=100, **values)
    second = models.MrpFreezeComponent(root_item_id=200, **{**values, changed_field: changed_value})
    with pytest.raises(ValueError, match="ambiguous across plan roots"):
        material_availability._unique_frozen_components([first, second])


@pytest.mark.parametrize("action_kind", ["make", "rework", "kitting"])
def test_drum_make_manifest_becomes_run_scoped_mechshop_pull(db_session, action_kind):
    generation = _building_generation(db_session, "journal-readiness-pull")
    line = models.AssemblyQueueLine(
        ledger_generation_id=generation.id,
        planning_run_id=77,
        plan_id=8,
        plan_line_id=9,
        item_id=100,
        bucket_date=date(2026, 9, 8),
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
        planned_output_qty=2,
        accepted_plan_output_qty=0,
        assembly_remaining_qty=2,
        original_priority=["2026-09-01", 9],
        sort_key="2026-09-01|0000000009",
        line_status="open",
    )
    db_session.add(line)
    db_session.flush()
    db_session.add(models.AssemblyReadiness(
        ledger_generation_id=generation.id,
        assembly_queue_line_id=line.id,
        status="recoverable",
        open_qty=2,
        ready_qty=0,
        launchable_qty=2,
        action_manifest=[{
            "action_kind": action_kind, "item_id": 501, "qty": "4",
            "available_date": "2026-09-06",
        }],
        evidence_signature="e" * 64,
    ))
    schedule = models.DrumSchedule(
        ledger_generation_id=generation.id,
        status="completed", algorithm_version="test",
        schedule_from=date(2026, 9, 3), schedule_to=date(2026, 9, 30),
        queue_signature="q", slot_signature="s", gap_signature="g",
        slot_row_count=1, gap_row_count=0,
        total_open_qty=2, total_slot_qty=2, total_gap_qty=0,
    )
    db_session.add(schedule)
    db_session.flush()
    slot = models.DrumSlot(
        drum_schedule_id=schedule.id,
        assembly_queue_line_id=line.id,
        plan_id=8, plan_line_id=9, item_id=100, resource_id=3,
        slot_date=date(2026, 9, 8), slot_qty=2, planned_output_qty=2,
        slot_ordinal=0, original_priority=["2026-09-01", 9],
        readiness_phase="launch",
        action_manifest=[{
            "action_kind": action_kind, "item_id": 501, "qty": "4",
            "available_date": "2026-09-06",
        }],
    )
    db_session.add(slot)
    db_session.flush()

    pull = _drum_readiness_pull_by_run_item(db_session, generation.id)

    assert pull[(77, 501)]["readiness_required_qty"] == 4.0
    assert pull[(77, 501)]["readiness_need_date"] == "2026-09-08"
    assert pull[(77, 501)]["protected_drum_slots"] == [{
        "drum_slot_id": slot.id,
        "root_item_id": 100,
        "slot_date": "2026-09-08",
        "slot_qty": "2",
        "readiness_phase": "launch",
    }]


def _building_generation(db, key: str):
    cutoff = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key=f"{key}:physical",
        status="completed",
        cutoff=cutoff,
        completed_at=cutoff,
        source_watermarks={"explicit_empty_prefix": True},
    )
    generation = models.LedgerGeneration(
        generation_key=key,
        status="building",
        cutoff=cutoff,
        source_watermarks={"explicit_empty_prefix": True},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="test",
        replay_version="test",
    )
    db.add(generation)
    db.flush()
    _seed_future_supply_capture(db, generation)
    initialize_material_custody_baseline(
        db,
        ledger_generation_id=int(generation.id),
        cells=[],
        observed_at=generation.cutoff,
    )
    db.expire_all()
    return generation


def _seed_future_supply_capture(db, generation):
    batch = models.LedgerBuildBatch(
        ledger_generation_id=int(generation.id),
        stage="future_supply_capture",
        batch_key=f"{generation.id}:future_supply_capture",
        status="building",
        algorithm_version=FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION,
        metrics={},
    )
    db.add(batch)
    db.flush()
    replace_future_supply_capture(
        db,
        int(generation.id),
        int(batch.id),
        [],
    )
    batch.status = "completed"
    batch.completed_at = generation.cutoff
    db.flush()




def _journal_line(db):
    item = models.Item(
        item_code="SNAP-PROD-1",
        item_name="Snapshot production line",
        item_article="SNAP-ARTICLE",
        unit="шт",
        status="active",
    )
    order = models.ProductionOrder(
        order_number="SNAP-ORDER-1",
        order_date=datetime(2026, 7, 20),
        source="1c",
        deletion_mark=False,
    )
    db.add_all([item, order])
    db.flush()
    product = models.ProductionProduct(
        order_id=order.order_id,
        item_id=item.item_id,
        line_number=1,
        quantity=10,
        produced_qty=3,
        remaining_qty=999,
    )
    db.add(product)
    db.flush()
    return item, order, product


def _make_proposal(db, generation, tag=""):
    suffix = str(tag)
    item = models.Item(
        item_code=f"SNAP-MAKE-PROPOSAL{suffix}",
        item_name="Snapshot MAKE proposal",
        item_article="SNAP-MAKE",
        unit="шт",
        replenishment_method="Производство",
        status="active",
    )
    component = models.Item(
        item_code=f"SNAP-MAKE-COMPONENT{suffix}",
        item_name="Snapshot MAKE component",
        item_article="SNAP-COMP",
        unit="шт",
        replenishment_method="Закупка",
        status="active",
    )
    spec = models.Specification(
        spec_code=f"SNAP-MAKE-SPEC{suffix}",
        spec_name=f"Snapshot MAKE specification{suffix}",
        spec_ref1c=f"snap-make-spec{suffix}",
    )
    plan = models.ProductionPlanHeader(
        name=f"Snapshot proposal plan{suffix}",
        period_from=generation.cutoff.date(),
        period_to=generation.cutoff.date(),
        status="fixed",
    )
    db.add_all([item, component, spec, plan])
    db.flush()
    db.add_all([
        models.DefaultSpecification(item_id=item.item_id, spec_id=spec.spec_id),
        models.SpecComponent(spec_id=spec.spec_id, item_id=component.item_id, quantity=1),
    ])
    db.flush()
    plan_line = models.ProductionPlanLine(
        plan_id=plan.id,
        item_id=item.item_id,
        bucket_date=generation.cutoff.date(),
        qty=12,
    )
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db.add_all([plan_line, run])
    db.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=12,
        net_required_qty=12,
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=0,
        freeze_version=1,
    )
    db.add(requirement)
    db.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        run_id=run.run_id,
        freeze_version=1,
        requirement_id=requirement.id,
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
        realization_mode="make",
        reserved_qty=12,
        covered_from_stock_at_freeze_qty=0,
        replenishment_required_qty=12,
        replenishment_received_qty=2,
        realized_qty=2,
        lifecycle_status="active",
    )
    db.add(reservation)
    db.flush()
    work = models.ReplenishmentWorkItem(
        ledger_generation_id=generation.id,
        reservation_id=reservation.id,
        plan_id=plan.id,
        run_id=run.run_id,
        requirement_id=requirement.id,
        item_id=item.item_id,
        replenishment_method="make",
        replenishment_required_qty=12,
        replenishment_fulfilled_qty=2,
        replenishment_remaining_qty=10,
    )
    db.add(work)
    db.flush()
    return run, work


class _CandidatePayload:
    """Test boundary for an unpersisted candidate payload."""

    def __init__(self, generation, payload):
        self.id = int(generation.id)
        self.cutoff = generation.cutoff
        self.truth_status = str(payload["meta"].get("truth_status"))
        self.payload = payload
        self.rows = [
            SimpleNamespace(
                row_key=str(row.get("journal_row_key") or row.get("current_identity")),
                row_kind=(
                    "production_proposal"
                    if row.get("product_id") is None
                    else "production_order"
                ),
                payload=row,
            )
            for row in payload.get("rows", [])
        ]


def _build_candidate(db, generation_id, *, accepted_run_ids):
    generation = db.get(models.LedgerGeneration, int(generation_id))
    payload = build_candidate_payload(
        db,
        int(generation_id),
        accepted_run_ids=accepted_run_ids,
    )
    return _CandidatePayload(generation, payload)


def _candidate_rows(candidate):
    return {str(row.row_key): row.payload for row in candidate.rows}


def _accept(db, generation, candidate):
    accepted_at = datetime(2026, 7, 29, 13, tzinfo=timezone.utc)
    generation.status = "accepted"
    generation.accepted_at = accepted_at
    generation.capabilities = dict(CAPABILITIES)
    publish_generation(db, generation)
    db.flush()


def _publish_current(db, generation, candidate):
    publish_current_production_control_from_payload(
        db,
        int(generation.id),
        candidate.payload,
    )
    db.flush()


def _setup_chain_journal_rows(db):
    painted = models.Item(
        item_code="SNAP-PAINTED",
        item_name="Snap painted",
        item_article="SNAP-P",
        unit="шт",
        status="active",
    )
    welded = models.Item(
        item_code="SNAP-WELDED",
        item_name="Snap welded",
        item_article="SNAP-W",
        unit="шт",
        status="active",
    )
    db.add_all([painted, welded])
    db.flush()

    paint_order = models.ProductionOrder(
        order_number="SNAP-PAINT-ORDER",
        order_date=datetime(2026, 7, 20),
        deletion_mark=False,
        source="1c",
    )
    weld_order = models.ProductionOrder(
        order_number="SNAP-WELD-ORDER",
        order_date=datetime(2026, 7, 20),
        deletion_mark=False,
        source="1c",
    )
    db.add_all([paint_order, weld_order])
    db.flush()

    paint_product = models.ProductionProduct(
        order_id=paint_order.order_id,
        item_id=painted.item_id,
        line_number=1,
        quantity=8,
        produced_qty=0,
        remaining_qty=8,
    )
    weld_product = models.ProductionProduct(
        order_id=weld_order.order_id,
        item_id=welded.item_id,
        line_number=1,
        quantity=8,
        produced_qty=0,
        remaining_qty=6,
    )
    db.add_all([paint_product, weld_product])
    db.flush()

    pair = models.PaintWeldPair(
        painted_item_id=painted.item_id,
        welded_item_id=welded.item_id,
        source="auto",
    )
    db.add(pair)
    db.flush()
    db.add(
        models.PaintWeldChainLink(
            painted_order_id=paint_order.order_id,
            welded_order_id=weld_order.order_id,
            pair_id=pair.id,
        )
    )
    db.flush()
    return paint_product, weld_product


def test_candidate_payload_row_contains_route_sheet_payload(db_session):
    generation = _building_generation(db_session, "production-journal-row-route")
    item, order, product = _journal_line(db_session)
    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    db_session.flush()

    row = _candidate_rows(snapshot)[f"product:{product.product_id}"]
    route_payload = row["_route_sheet_snapshot"]
    assert int(route_payload["version"]) == 1
    assert int(route_payload["sheet"]["product_id"]) == product.product_id
    assert int(route_payload["sheet"]["remaining_qty"]) == 7
    assert "_route_sheet_snapshot" in row


def test_compact_current_production_control_payload_uses_current_sources_and_publishes(
    db_session,
):
    parent = _building_generation(db_session, "production-journal-compact-parent")
    parent.status = "accepted"
    parent.accepted_at = parent.cutoff
    parent.capabilities = dict(CAPABILITIES)
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    target = _building_generation(db_session, "production-journal-compact-target")
    item, order, product = _journal_line(db_session)
    db_session.flush()

    payload = build_compact_current_production_control_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload={"queue_rows": []},
        drum_payload={"rows": []},
        shelf_payload={"rows": []},
        accepted_run_ids=[999],
    )

    assert payload["meta"]["ledger_generation_id"] == target.id
    row = next(row for row in payload["rows"] if row["product_id"] == product.product_id)
    assert row["current_identity"] == f"production-order-line:{order.order_id}:1"
    # Item 28a: the order row keeps the coverage snapshot the current material
    # readers serve, exactly as the staged path publishes it.
    assert isinstance(row.get("material_coverage_snapshot"), dict)
    assert not any(
        key in {"generation_id", "ledger_generation_id", "source_generation_id", "snapshot_id"}
        for key in row
    )

    # The builder's output is the same DTO boundary consumed after the target
    # has been accepted; no generation-scoped staging owner is needed.
    target.status = "accepted"
    target.accepted_at = target.cutoff
    db_session.flush()
    result = publish_current_production_control_from_payload(
        db_session,
        target.id,
        payload,
    )
    assert result.changed_rows == len(payload["rows"])
    assert db_session.query(models.ReplenishmentWorkItem).count() == 0
    assert db_session.query(models.AssemblyQueueLine).count() == 0


def test_candidate_payload_contains_unmaterialized_make_proposal(db_session):
    generation = _building_generation(db_session, "production-journal-make-proposal")
    run, work = _make_proposal(db_session, generation)

    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    row = _candidate_rows(snapshot)[f"work-item:{work.id}"]

    assert row["journal_row_key"] == f"work-item:{work.id}"
    assert row["work_item_id"] == work.id
    assert row["product_id"] is None
    assert row["order_id"] is None
    assert row["remaining_qty"] == 10
    assert row["status"] == "not_created"
    assert row["coverage_status"] == "shortage"
    assert row["coverage_label"] == "Дефицит"
    assert row["material_coverage_status"] == "shortage"
    assert row["available_actions"] == ["materialize"]
    assert "_route_sheet_snapshot" not in row
    assert db_session.query(models.ProductionOrder).count() == 0


def test_compact_current_production_control_payload_uses_stable_reservation_not_work_item(
    db_session,
):
    parent = _building_generation(db_session, "production-journal-compact-mrp-parent")
    parent.status = "accepted"
    parent.accepted_at = parent.cutoff
    parent.capabilities = dict(CAPABILITIES)
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    run, work = _make_proposal(db_session, parent)
    reservation = db_session.get(models.ReservationEntry, int(work.reservation_id))
    reservation.owner_kind = "current"
    reservation.is_current = True
    reservation.current_identity = f"reservation:req:{reservation.requirement_id}:mode:make"
    # The compact builder must not need the generation-local work item.
    db_session.delete(work)
    target = _building_generation(db_session, "production-journal-compact-mrp-target")
    db_session.flush()

    payload = build_compact_current_production_control_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload={
            "queue_rows": [{
                "entity_kind": "assembly_queue",
                "business_identity": f"plan-line:{run.source_plan_id}",
                "payload": {
                    "run_id": run.run_id,
                    "item_id": reservation.item_id,
                },
            }],
        },
        drum_payload={"rows": []},
        shelf_payload={"rows": [{
            "entity_kind": "shelf_projection",
            "payload": {
                "item_id": reservation.item_id,
                "materialized_qty": 4,
                "pull_qty": 4,
                "warehouse_ref1c": "WH",
            },
        }]},
        accepted_run_ids=[run.run_id],
    )

    row = next(row for row in payload["rows"] if row.get("product_id") is None)
    assert row["reservation_id"] == reservation.id
    assert "work_item_id" not in row
    assert row["launchable_qty"] == 4
    assert row["root_item_ids"] == [reservation.item_id]
    assert db_session.query(models.ReplenishmentWorkItem).count() == 0


def test_compact_make_proposal_keeps_only_its_row_root_membership(db_session):
    parent = _building_generation(db_session, "production-journal-compact-row-roots-parent")
    parent.status = "accepted"
    parent.accepted_at = parent.cutoff
    parent.capabilities = dict(CAPABILITIES)
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    run, work = _make_proposal(db_session, parent, tag="-row-roots")
    reservation = db_session.get(models.ReservationEntry, int(work.reservation_id))
    reservation.owner_kind = "current"
    reservation.is_current = True
    reservation.current_identity = f"reservation:req:{reservation.requirement_id}:mode:make"
    db_session.delete(work)
    second_root = models.Item(
        item_code="SNAP-MAKE-SECOND-ROOT",
        item_name="Second root",
        item_article="SNAP-SECOND-ROOT",
        unit="шт",
        status="active",
    )
    db_session.add(second_root)
    db_session.flush()
    db_session.add(models.ProductionPlanLine(
        plan_id=int(run.source_plan_id),
        item_id=int(second_root.item_id),
        bucket_date=parent.cutoff.date(),
        qty=3,
    ))
    db_session.flush()
    target = _building_generation(db_session, "production-journal-compact-row-roots-target")
    payload = build_compact_current_production_control_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload={
            "queue_rows": [
                {
                    "entity_kind": "assembly_queue",
                    "business_identity": "plan-line:first-root",
                    "payload": {"run_id": run.run_id, "item_id": reservation.item_id},
                },
                {
                    "entity_kind": "assembly_queue",
                    "business_identity": "plan-line:second-root",
                    "payload": {"run_id": run.run_id, "item_id": second_root.item_id},
                },
            ],
        },
        drum_payload={"rows": []},
        shelf_payload={"rows": [{
            "entity_kind": "shelf_projection",
            "payload": {
                "item_id": reservation.item_id,
                "materialized_qty": 4,
                "pull_qty": 4,
                "warehouse_ref1c": "WH",
            },
        }]},
        accepted_run_ids=[run.run_id],
    )
    row = next(row for row in payload["rows"] if row.get("product_id") is None)
    assert row["root_item_ids"] == [reservation.item_id]


def test_compact_make_proposal_carries_and_reuses_its_material_snapshot(
    db_session, monkeypatch
):
    """Item 28d: the bounded path publishes the proposal snapshot the
    work-item materials reader serves, previewing only what it cannot reuse.

    Coverage scalars still come from one bulk fold; the per-proposal preview
    runs for a proposal without a reusable parent snapshot, and an untouched
    proposal of the same quantity reuses its parent row with zero change rows.
    """
    from app.services.item_ledger.current_execution import (
        publish_current_production_control_from_payload,
    )

    parent = _building_generation(db_session, "production-journal-compact-bulk-coverage-parent")
    parent.status = "accepted"
    parent.accepted_at = parent.cutoff
    parent.capabilities = dict(CAPABILITIES)
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    run, work = _make_proposal(db_session, parent)
    reservation = db_session.get(models.ReservationEntry, int(work.reservation_id))
    reservation.owner_kind = "current"
    reservation.is_current = True
    reservation.current_identity = f"reservation:req:{reservation.requirement_id}:mode:make"
    db_session.delete(work)
    target = _building_generation(db_session, "production-journal-compact-bulk-coverage-target")
    db_session.flush()

    previews = []

    def preview(db, *, work_item_id, item_id, quantity, spec_id, **kwargs):
        previews.append(int(item_id))
        # The real preview goes through ``public_materials_payload``, which
        # drops ``line_quantity``: reuse must not depend on it.
        return {
            "work_item_id": int(work_item_id),
            "coverage_status": "shortage",
            "components": [{
                "component_item_id": 555001, "required_qty": str(quantity),
            }],
        }

    monkeypatch.setattr(
        "app.services.production_control_material_availability.preview_make_work_item_materials",
        preview,
    )

    def build(affected_item_ids=None):
        return build_compact_current_production_control_payload(
            db_session,
            target_generation_id=target.id,
            parent_generation_id=parent.id,
            assembly_payload={
                "queue_rows": [{
                    "entity_kind": "assembly_queue",
                    "business_identity": "plan-line:bulk-coverage",
                    "payload": {"run_id": run.run_id, "item_id": reservation.item_id},
                }],
            },
            drum_payload={"rows": []},
            shelf_payload={"rows": []},
            accepted_run_ids=[run.run_id],
            affected_item_ids=affected_item_ids,
        )

    first = build()
    row = next(row for row in first["rows"] if row.get("product_id") is None)
    assert row["coverage_status"] == "shortage"
    snapshot = row["material_coverage_snapshot"]
    assert isinstance(snapshot, dict) and snapshot["components"]
    assert "work_item_id" not in snapshot  # no synthetic locator leaks
    assert previews == [int(reservation.item_id)]

    publish_current_production_control_from_payload(db_session, int(parent.id), first)
    changes = db_session.query(models.CurrentExecutionChange).count()
    previews.clear()

    # A delta that touches neither the proposal item nor its components.
    second = build(affected_item_ids=[999999])
    reused = next(row for row in second["rows"] if row.get("product_id") is None)
    assert previews == []
    assert reused["material_coverage_snapshot"] == snapshot
    result = publish_current_production_control_from_payload(db_session, int(parent.id), second)
    assert result.idempotent is True
    assert db_session.query(models.CurrentExecutionChange).count() == changes

    # A delta naming one of its components previews it again.
    build(affected_item_ids=[555001])
    assert previews == [int(reservation.item_id)]


def test_compact_production_reuses_unchanged_parent_material_snapshot(
    db_session, monkeypatch
):
    parent = _building_generation(db_session, "production-journal-reuse-parent")
    parent.status = "accepted"
    parent.accepted_at = parent.cutoff
    parent.capabilities = dict(CAPABILITIES)
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    target = _building_generation(db_session, "production-journal-reuse-target")
    _item, order, product = _journal_line(db_session)
    snapshot = {
        "ledger_generation_id": int(parent.id),
        "product_id": int(product.product_id),
        "coverage_status": "ready",
        "coverage_label": "Обеспечен",
        "components": [{"component_item_id": 987654, "coverage": "ok"}],
    }
    db_session.add(
        models.CurrentExecutionScope(
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
            source_revision="accepted:parent",
            source_generation_id=parent.id,
            result_ready=True,
            content_hash="a" * 64,
            summary={"total_rows": 1},
        )
    )
    db_session.add(
        models.CurrentExecutionRow(
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
            business_identity=f"production-order-line:{order.order_id}:1",
            source_revision="accepted:parent",
            source_generation_id=parent.id,
            result_status="accepted",
            result_ready=True,
            content_hash="b" * 64,
            payload={
                "product_id": int(product.product_id),
                "item_id": int(product.item_id),
                "material_coverage_snapshot": snapshot,
            },
        )
    )
    db_session.flush()
    calls = []

    def fail_preview(*args, **kwargs):
        calls.append(int(args[1]))
        raise AssertionError("unchanged production rows must reuse parent coverage")

    monkeypatch.setattr(
        "app.services.production_control_material_availability.preview_materials",
        fail_preview,
    )
    payload = build_compact_current_production_control_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload={"queue_rows": []},
        drum_payload={"rows": []},
        shelf_payload={"rows": []},
        accepted_run_ids=[999],
        affected_item_ids=[123456],
    )
    assert len(payload["rows"]) == payload["meta"]["row_count"] == 1
    assert calls == []
    row = payload["rows"][0]
    assert row["product_id"] == product.product_id
    assert row["coverage_status"] == "ready"

    calls.clear()

    def count_preview(db, product_id, *, ledger_generation_id=None, **kwargs):
        calls.append(int(product_id))
        return dict(snapshot)

    monkeypatch.setattr(
        "app.services.production_control_material_availability.preview_materials",
        count_preview,
    )
    changed_payload = build_compact_current_production_control_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload={"queue_rows": []},
        drum_payload={"rows": []},
        shelf_payload={"rows": []},
        accepted_run_ids=[999],
        affected_item_ids=[987654],
    )
    assert len(changed_payload["rows"]) == changed_payload["meta"]["row_count"] == 1
    assert calls == [product.product_id]


def test_compact_make_row_parity_with_legacy_builder(db_session):
    parent = _building_generation(db_session, "production-journal-compact-parity-parent")
    parent.status = "accepted"
    parent.accepted_at = parent.cutoff
    parent.capabilities = dict(CAPABILITIES)
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    run, work = _make_proposal(db_session, parent)
    reservation = db_session.get(models.ReservationEntry, int(work.reservation_id))
    reservation.owner_kind = "current"
    reservation.is_current = True
    reservation.current_identity = f"reservation:req:{reservation.requirement_id}:mode:make"
    target = _building_generation(db_session, "production-journal-compact-parity-target")
    db_session.flush()

    from app.services.production_control_journal_projection import _build_rows

    legacy_rows, _ = _build_rows(db_session, parent, [run.run_id])
    legacy = next(row for row in legacy_rows if row.get("product_id") is None)
    legacy["root_item_ids"] = [int(reservation.item_id)]
    compact = build_compact_current_production_control_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload={
            "queue_rows": [{
                "entity_kind": "assembly_queue",
                "business_identity": "plan-line:parity",
                "payload": {"run_id": run.run_id, "item_id": reservation.item_id},
            }],
        },
        drum_payload={"rows": []},
        shelf_payload={"rows": []},
        accepted_run_ids=[run.run_id],
    )
    compact_row = next(row for row in compact["rows"] if row.get("product_id") is None)

    ignored = {"work_item_id", "journal_row_key", "current_identity"}
    # material_coverage_snapshot is evidence consumed only during candidate
    # publication and is intentionally removed from the compact current owner.
    ignored.add("material_coverage_snapshot")
    legacy_normalized = {
        key: value for key, value in legacy.items() if key not in ignored
    }
    compact_normalized = {
        key: value for key, value in compact_row.items() if key not in ignored
    }
    assert legacy_normalized == compact_normalized


def test_compact_purchase_payload_next_target_keeps_stable_business_rows_and_no_audit(
    db_session,
):
    parent = _building_generation(db_session, "production-journal-compact-next-parent")
    parent.status = "accepted"
    parent.accepted_at = parent.cutoff
    parent.capabilities = dict(CAPABILITIES)
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    run, work = _make_proposal(db_session, parent)
    reservation = db_session.get(models.ReservationEntry, int(work.reservation_id))
    reservation.owner_kind = "current"
    reservation.is_current = True
    reservation.current_identity = (
        f"reservation:req:{reservation.requirement_id}:mode:make"
    )
    db_session.delete(work)
    target1 = _building_generation(db_session, "production-journal-compact-next-1")
    target2 = _building_generation(db_session, "production-journal-compact-next-2")
    queue = [{
        "entity_kind": "assembly_queue",
        "business_identity": "plan-line:next-target",
        "payload": {"run_id": run.run_id, "item_id": reservation.item_id},
    }]
    shelf = [{
        "entity_kind": "shelf_projection",
        "payload": {
            "item_id": reservation.item_id,
            "materialized_qty": 4,
            "pull_qty": 4,
            "warehouse_ref1c": "WH",
        },
    }]
    audit_before = db_session.query(models.CurrentExecutionChange).count()
    first = build_compact_current_production_control_payload(
        db_session,
        target_generation_id=target1.id,
        parent_generation_id=parent.id,
        assembly_payload={"queue_rows": queue},
        drum_payload={"rows": []},
        shelf_payload={"rows": shelf},
        accepted_run_ids=[run.run_id],
    )
    second = build_compact_current_production_control_payload(
        db_session,
        target_generation_id=target2.id,
        parent_generation_id=parent.id,
        assembly_payload={"queue_rows": queue},
        drum_payload={"rows": []},
        shelf_payload={"rows": shelf},
        accepted_run_ids=[run.run_id],
    )

    def business_rows(payload):
        return [
            {
                key: value
                for key, value in row.items()
                if key not in {"material_coverage_calculated_at"}
            }
            for row in payload["rows"]
        ]

    assert business_rows(first) == business_rows(second)
    assert first["rows"][0]["current_identity"] == (
        f"mrp-reservation:{reservation.current_identity}"
    )
    assert db_session.query(models.CurrentExecutionChange).count() == audit_before
    assert db_session.query(models.ReservationCurrentChange).count() == 0
    assert db_session.query(models.ReplenishmentWorkItem).count() == 0


def test_compact_purchase_payload_affected_scope_changes_only_that_make_row(db_session):
    parent = _building_generation(db_session, "production-journal-compact-scope-parent")
    parent.status = "accepted"
    parent.accepted_at = parent.cutoff
    parent.capabilities = dict(CAPABILITIES)
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    run1, work1 = _make_proposal(db_session, parent, "-one")
    run2, work2 = _make_proposal(db_session, parent, "-two")
    reservations = []
    for work in (work1, work2):
        reservation = db_session.get(models.ReservationEntry, int(work.reservation_id))
        reservation.owner_kind = "current"
        reservation.is_current = True
        reservation.current_identity = (
            f"reservation:req:{reservation.requirement_id}:mode:make"
        )
        reservations.append(reservation)
        db_session.delete(work)
    target = _building_generation(db_session, "production-journal-compact-scope-target")
    db_session.flush()
    queue = [
        {
            "entity_kind": "assembly_queue",
            "business_identity": f"plan-line:{run1.run_id}",
            "payload": {"run_id": run1.run_id, "item_id": reservations[0].item_id},
        },
        {
            "entity_kind": "assembly_queue",
            "business_identity": f"plan-line:{run2.run_id}",
            "payload": {"run_id": run2.run_id, "item_id": reservations[1].item_id},
        },
    ]

    def shelf_rows(first_qty):
        return [{
            "entity_kind": "shelf_projection",
            "payload": {
                "item_id": reservation.item_id,
                "materialized_qty": first_qty if index == 0 else 4,
                "pull_qty": first_qty if index == 0 else 4,
                "warehouse_ref1c": "WH",
            },
        } for index, reservation in enumerate(reservations)]

    base = build_compact_current_production_control_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload={"queue_rows": queue},
        drum_payload={"rows": []},
        shelf_payload={"rows": shelf_rows(4)},
        accepted_run_ids=[run1.run_id, run2.run_id],
    )
    changed = build_compact_current_production_control_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload={"queue_rows": queue},
        drum_payload={"rows": []},
        shelf_payload={"rows": shelf_rows(0)},
        accepted_run_ids=[run1.run_id, run2.run_id],
    )
    base_by_identity = {row["current_identity"]: row for row in base["rows"]}
    changed_by_identity = {row["current_identity"]: row for row in changed["rows"]}
    second_identity = f"mrp-reservation:{reservations[1].current_identity}"
    assert changed_by_identity[second_identity] == base_by_identity[second_identity]
    first_identity = f"mrp-reservation:{reservations[0].current_identity}"
    assert first_identity not in changed_by_identity


def test_compact_purchase_payload_fails_closed_on_stale_parent_and_ambiguous_owner(
    db_session,
):
    parent = _building_generation(db_session, "production-journal-compact-stale-parent")
    parent.status = "accepted"
    parent.accepted_at = parent.cutoff
    parent.capabilities = dict(CAPABILITIES)
    pointer = models.PlanningTruthState(id=1, current_generation_id=parent.id)
    db_session.add(pointer)
    run, work = _make_proposal(db_session, parent)
    reservation = db_session.get(models.ReservationEntry, int(work.reservation_id))
    reservation.owner_kind = "current"
    reservation.is_current = True
    reservation.current_identity = "wrong-stable-owner"
    db_session.delete(work)
    target = _building_generation(db_session, "production-journal-compact-stale-target")
    kwargs = dict(
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload={"queue_rows": []},
        drum_payload={"rows": []},
        shelf_payload={"rows": []},
        accepted_run_ids=[run.run_id],
    )
    pointer.current_generation_id = target.id
    with pytest.raises(ProductionControlJournalPromotionError, match="current truth"):
        build_compact_current_production_control_payload(db_session, **kwargs)
    pointer.current_generation_id = parent.id
    with pytest.raises(ProductionControlJournalPromotionError, match="ambiguous stable"):
        build_compact_current_production_control_payload(db_session, **kwargs)


@pytest.mark.parametrize("root_copies", [1, 2, 3])
@pytest.mark.parametrize("ambiguous", [False, True])
def test_paint_weld_proposals_use_welded_frozen_bom_and_block_welded_row(db_session, root_copies, ambiguous):
    generation = _building_generation(db_session, "production-journal-paint-weld")
    run, painted_work = _make_proposal(db_session, generation)
    painted_item = db_session.get(models.Item, int(painted_work.item_id))
    welded_item = models.Item(
        item_code="SNAP-WELDED-PROPOSAL",
        item_name="Сварная деталь",
        item_article="SNAP-WELDED",
        unit="шт",
        replenishment_method="Производство",
        status="active",
    )
    raw_item = models.Item(
        item_code="SNAP-WELD-RAW",
        item_name="Сырьё сварки",
        item_article="SNAP-RAW",
        unit="шт",
        replenishment_method="Закупка",
        status="active",
    )
    db_session.add_all([welded_item, raw_item])
    db_session.flush()
    db_session.add(
        models.PaintWeldPair(
            painted_item_id=int(painted_item.item_id),
            welded_item_id=int(welded_item.item_id),
            source="manual",
            is_active=True,
        )
    )
    db_session.add(
        models.MrpFreezeComponent(
            run_id=int(run.run_id),
            freeze_version=1,
            parent_item_id=int(welded_item.item_id),
            component_item_id=int(raw_item.item_id),
            spec_ref="frozen-weld-spec",
            spec_version="v1",
            norm_qty_per_unit=2,
            unit_coef=1,
        )
    )
    # The same welded assembly belongs to several root products. Each root
    # stores its own copy of the per-unit BOM, not an additional raw material.
    for root_item in [painted_item, welded_item][:root_copies - 1]:
        db_session.add(models.MrpFreezeComponent(
            run_id=int(run.run_id), freeze_version=1,
            root_item_id=int(root_item.item_id),
            parent_item_id=int(welded_item.item_id),
            component_item_id=int(raw_item.item_id),
            spec_ref="frozen-weld-spec", spec_version="v1",
            norm_qty_per_unit=2, unit_coef=1,
        ))
    if ambiguous:
        db_session.add(models.MrpFreezeComponent(
            run_id=int(run.run_id), freeze_version=1,
            root_item_id=int(raw_item.item_id),
            parent_item_id=int(welded_item.item_id),
            component_item_id=int(raw_item.item_id),
            spec_ref="other-weld-spec", spec_version="v2",
            norm_qty_per_unit=3, unit_coef=1,
        ))
    warehouse = models.StockWarehouse(
        warehouse_ref1c="paint-weld-stock",
        warehouse_code="PWS",
        warehouse_name="Paint weld stock",
        is_selected=True,
    )
    db_session.add(warehouse)
    db_session.flush()
    db_session.add(
        models.StockBin(
            ledger_generation_id=int(generation.id),
            item_id=int(raw_item.item_id),
            characteristic_ref="",
            organization_ref="",
            warehouse_ref1c=warehouse.warehouse_ref1c,
            on_hand=20,
        )
    )

    welded_requirement = models.MrpRequirement(
        run_id=int(run.run_id),
        item_id=int(welded_item.item_id),
        total_required_qty=10,
        net_required_qty=10,
        period_from=run.period_from,
        period_to=run.period_to,
        bom_level=1,
        freeze_version=1,
    )
    db_session.add(welded_requirement)
    db_session.flush()
    welded_reservation = models.ReservationEntry(
        ledger_generation_id=int(generation.id),
        item_id=int(welded_item.item_id),
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        run_id=int(run.run_id),
        freeze_version=1,
        requirement_id=int(welded_requirement.id),
        priority_period_from=run.period_from,
        priority_period_to=run.period_to,
        realization_mode="make",
        reserved_qty=10,
        covered_from_stock_at_freeze_qty=0,
        replenishment_required_qty=10,
        replenishment_received_qty=0,
        realized_qty=0,
        lifecycle_status="active",
    )
    db_session.add(welded_reservation)
    db_session.flush()
    welded_work = models.ReplenishmentWorkItem(
        ledger_generation_id=int(generation.id),
        reservation_id=int(welded_reservation.id),
        plan_id=int(painted_work.plan_id),
        run_id=int(run.run_id),
        requirement_id=int(welded_requirement.id),
        item_id=int(welded_item.item_id),
        replenishment_method="make",
        replenishment_required_qty=10,
        replenishment_fulfilled_qty=0,
        replenishment_remaining_qty=10,
    )
    db_session.add(welded_work)
    db_session.flush()

    materials = material_availability.preview_make_work_item_materials(
        db_session,
        work_item_id=int(painted_work.id),
        item_id=int(painted_work.item_id),
        quantity=10,
        spec_id=None,
        ledger_generation_id=int(generation.id),
        order_number=f"MRP-R-{int(painted_work.requirement_id)}",
        run_id=int(run.run_id),
    )
    assert materials["coverage_basis"] == "welded_bom"
    assert materials["coverage_basis_item_id"] == welded_item.item_id
    if ambiguous:
        assert materials["components"] == []
        assert materials["coverage_status"] == "unavailable"
        assert materials["coverage_label"] == "Неоднозначная спецификация"
    else:
        assert [row["component_item_id"] for row in materials["components"]] == [raw_item.item_id]
        assert materials["components"][0]["required_qty"] == 20

    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    rows = _candidate_rows(snapshot)
    painted_row = rows[f"work-item:{painted_work.id}"]
    welded_row = rows[f"work-item:{welded_work.id}"]
    assert painted_row["coverage_status"] == ("unavailable" if ambiguous else "ready")
    assert painted_row["paint_weld_pair"]["role"] == "painted"
    assert painted_row["paint_weld_pair"]["counterpart_item_id"] == welded_item.item_id
    assert welded_row["paint_weld_pair"]["role"] == "welded"
    assert welded_row["available_actions"] == []
    assert "запуск выполняется из окрашенной строки" in welded_row["selection_disabled_reason"]

    # Promotion must accept the disabled welded proposal shape.  The promoter
    # previously required available_actions == ["materialize"] for every
    # proposal and rejected the welded row (available_actions == []) as a
    # "malformed proposal row", which blocked promotion of the whole
    # production-control journal candidate — and therefore every generation
    # acceptance that contained a weld→paint chain.
    _accept(db_session, generation, snapshot)
    promoted_welded = _candidate_rows(snapshot)[f"work-item:{welded_work.id}"]
    assert promoted_welded["available_actions"] == []
    assert generation.status == "accepted"


def test_route_sheet_builder_uses_candidate_generation_for_stock_bins(
    db_session,
    monkeypatch,
):
    accepted_generation = _building_generation(db_session, "route-bin-accepted")
    building_generation = _building_generation(db_session, "route-bin-building")

    accepted_generation.status = "accepted"
    accepted_generation.accepted_at = datetime(2026, 7, 29, 13, tzinfo=timezone.utc)
    accepted_generation.capabilities = dict(CAPABILITIES)
    publish_generation(db_session, accepted_generation)
    db_session.commit()

    main_item = models.Item(
        item_code="ROUTE-MAIN",
        item_name="Route Main",
        item_article="R-MAIN",
        unit="шт",
        status="active",
    )
    comp_item = models.Item(
        item_code="ROUTE-COMP",
        item_name="Route Component",
        item_article="R-COMP",
        unit="шт",
        status="active",
    )
    db_session.add_all([main_item, comp_item])
    db_session.flush()

    spec = models.Specification(
        spec_name="Route Snapshot Spec",
        spec_ref1c="ROUTE-SPEC",
    )
    db_session.add(spec)
    db_session.flush()

    db_session.add_all(
        [
            models.DefaultSpecification(
                item_id=main_item.item_id,
                spec_id=spec.spec_id,
            ),
            models.SpecComponent(
                spec_id=spec.spec_id,
                item_id=comp_item.item_id,
                quantity=1,
                component_type="Материал",
            ),
        ]
    )

    order = models.ProductionOrder(
        order_number="ROUTE-ORDER",
        order_date=datetime(2026, 7, 20),
        deletion_mark=False,
        source="1c",
    )
    db_session.add(order)
    db_session.flush()

    product = models.ProductionProduct(
        order_id=order.order_id,
        item_id=main_item.item_id,
        line_number=1,
        quantity=10,
        produced_qty=0,
        remaining_qty=10,
    )
    db_session.add(product)
    db_session.flush()

    db_session.add_all(
        [
            models.StockWarehouse(
                warehouse_ref1c="accepted-wh-a",
                warehouse_code="accepted-a",
                warehouse_name="Accepted A",
                is_selected=True,
            ),
            models.StockWarehouse(
                warehouse_ref1c="accepted-wh-b",
                warehouse_code="accepted-b",
                warehouse_name="Accepted B",
                is_selected=True,
            ),
            models.StockWarehouse(
                warehouse_ref1c="building-wh-a",
                warehouse_code="building-a",
                warehouse_name="Building A",
                is_selected=True,
            ),
        ]
    )
    db_session.flush()

    db_session.add_all(
        [
            models.StockBin(
                ledger_generation_id=accepted_generation.id,
                item_id=comp_item.item_id,
                characteristic_ref="",
                organization_ref="",
                warehouse_ref1c="accepted-wh-a",
                on_hand=5,
            ),
            models.StockBin(
                ledger_generation_id=accepted_generation.id,
                item_id=comp_item.item_id,
                characteristic_ref="",
                organization_ref="",
                warehouse_ref1c="accepted-wh-b",
                on_hand=7,
            ),
            models.StockBin(
                ledger_generation_id=building_generation.id,
                item_id=comp_item.item_id,
                characteristic_ref="",
                organization_ref="",
                warehouse_ref1c="building-wh-a",
                on_hand=9,
            ),
        ]
    )
    snapshot = _build_candidate(
        db_session,
        building_generation.id,
        accepted_run_ids=[],
    )

    route_payload = _candidate_rows(snapshot)[f"product:{product.product_id}"]["_route_sheet_snapshot"]

    components = route_payload["sheet"]["components"]
    assert len(components) == 1
    assert components[0]["multi_stock_warning"] is False


def test_route_sheet_snapshot_rows_use_anchor_dedup_and_are_immutable(db_session):
    generation = _building_generation(db_session, "production-journal-route-chain")
    painted_product, welded_product = _setup_chain_journal_rows(db_session)
    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)

    first = read_route_sheet_snapshot_rows(
        db_session,
        [painted_product.product_id, welded_product.product_id],
    )
    assert len(first) == 1
    first_sheet = first[0]["sheet"]
    assert first[0]["anchor_product_id"] == painted_product.product_id
    assert int(first_sheet["chain"]["weld_product_id"]) == welded_product.product_id
    original_qty = first_sheet["remaining_qty"]

    first[0]["sheet"]["remaining_qty"] = 9999
    second = read_route_sheet_snapshot_rows(
        db_session,
        [painted_product.product_id, welded_product.product_id],
    )
    assert second[0]["sheet"]["remaining_qty"] == original_qty


def test_validate_candidate_payload_rejects_missing_route_sheet_payload(db_session):
    generation = _building_generation(db_session, "production-journal-route-invalid")
    _, _, product = _journal_line(db_session)
    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    payload = dict(_candidate_rows(snapshot)[f"product:{product.product_id}"])
    payload.pop("_route_sheet_snapshot", None)
    snapshot.payload["rows"] = [
        payload if row.get("product_id") == product.product_id else row
        for row in snapshot.payload["rows"]
    ]

    with pytest.raises(ProductionControlJournalPromotionError):
        validate_candidate_payload(snapshot.payload, generation)


def test_route_sheet_snapshot_rows_fail_closed_without_snapshot(db_session):
    generation = _building_generation(db_session, "production-journal-route-missing")
    generation.status = "accepted"
    generation.accepted_at = datetime(2026, 7, 29, 13, tzinfo=timezone.utc)
    generation.capabilities = dict(CAPABILITIES)
    publish_generation(db_session, generation)
    db_session.commit()

    with pytest.raises(RouteSheetSnapshotUnavailable) as caught:
        read_route_sheet_snapshot_rows(db_session, [1, 2, 3])
    assert caught.value.as_dict()["code"] == "route_sheet_snapshot_unavailable"


def test_public_current_read_is_paged_and_stable_after_live_mutation(db_session):
    generation = _building_generation(db_session, "production-journal-snapshot")
    item, order, product = _journal_line(db_session)

    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    assert _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[],
    ).payload == snapshot.payload
    assert snapshot.truth_status == "building"
    assert snapshot.payload["meta"]["row_count"] == 1
    assert len(snapshot.rows) == 1
    assert snapshot.rows[0].payload["remaining_qty"] == 7
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)

    first = read_current_projection(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert first["total"] == 1
    assert first["rows"][0]["product_id"] == product.product_id
    assert first["rows"][0]["remaining_qty"] == 7
    material_first = get_materials_snapshot(db_session, product.product_id)
    assert material_first["truth_status"] == "accepted"
    assert material_first["cutoff"] == snapshot.cutoff.isoformat()
    assert material_first["ledger_generation_id"] == generation.id
    assert material_first["product_id"] == product.product_id

    # Public reads are byte-stable for the generation and do not rebuild from
    # live rows, even when an operational writer changes those rows later.
    item.item_name = "MUTATED LIVE NAME"
    order.deletion_mark = True
    product.produced_qty = 10
    product.remaining_qty = 0
    db_session.commit()

    second = read_current_projection(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert second == first
    assert get_materials_snapshot(db_session, product.product_id) == material_first


def test_operator_quantity_is_live_while_accepted_output_stays_frozen(db_session):
    """Количество заказа — команда оператора, принятый выпуск — истина Ledger.

    Канон относит состояние исполнительных заказов к подвижному: потребность мы
    фиксируем, а заказ — уже исполнение. Поэтому изменённое оператором
    количество видно сразу, а `produced_qty` продолжает читаться из снимка.
    """
    generation = _building_generation(db_session, "production-journal-live-qty")
    item, order, product = _journal_line(db_session)
    snapshot = _build_candidate(
        db_session, generation.id, accepted_run_ids=[],
    )
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)

    before = read_current_projection(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert before["rows"][0]["quantity"] == 10
    assert before["rows"][0]["produced_qty"] == 3
    assert before["rows"][0]["remaining_qty"] == 7

    # Оператор уменьшил количество к запуску у ещё не выгруженного заказа.
    product.quantity = 4
    # Ledger своим чередом принял ещё выпуск — эта величина снимочная и в
    # журнал отсюда попасть не должна.
    product.produced_qty = 9
    db_session.commit()

    after = read_current_projection(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert after["rows"][0]["quantity"] == 4, "команда оператора обязана быть видна сразу"
    assert after["rows"][0]["produced_qty"] == 3, "принятый выпуск остаётся снимочным"
    assert after["rows"][0]["remaining_qty"] == 1, "остаток считается от снимочного выпуска"


def test_completed_1c_order_is_hidden_immediately_from_current_read(db_session):
    generation = _building_generation(db_session, "production-journal-live-completion")
    _item, order, _product = _journal_line(db_session)
    snapshot = _build_candidate(
        db_session, generation.id, accepted_run_ids=[],
    )
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)

    before = read_current_projection(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert before["total"] == 1

    # The order was completed in 1C and its state was read back. Completion is
    # mutable execution state, so the immutable planning row is hidden rather
    # than rewritten while waiting for the next accepted generation.
    order.order_state_key = DONE_STATE_KEY
    db_session.commit()

    after = read_current_projection(db_session, search="SNAP-ARTICLE", limit=20, offset=0)
    assert after["rows"] == []
    assert after["total"] == 0
    assert after["offset"] == 0


def test_missing_current_publication_fails_closed_and_router_maps_it_to_503(db_session):
    generation = _building_generation(db_session, "production-journal-missing")
    generation.status = "accepted"
    generation.accepted_at = datetime(2026, 7, 29, 13, tzinfo=timezone.utc)
    generation.capabilities = dict(CAPABILITIES)
    publish_generation(db_session, generation)
    db_session.commit()

    with pytest.raises(ProductionControlJournalUnavailable) as caught:
        read_current_projection(db_session)
    assert caught.value.as_dict()["status"] == "unavailable"
    assert "missing" in caught.value.as_dict()["reason"]

    with pytest.raises(HTTPException) as router_error:
        get_orders_journal(db=db_session)
    assert router_error.value.status_code == 503
    assert (
        router_error.value.detail["code"]
        == "production_control_current_unavailable"
    )


def test_stale_truth_fails_before_current_lookup(db_session):
    generation = _building_generation(db_session, "production-journal-stale")
    _journal_line(db_session)
    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    _accept(db_session, generation, snapshot)
    generation.status = "stale"
    generation.reason = "refresh overdue"
    db_session.commit()

    with pytest.raises(ProductionControlJournalUnavailable) as caught:
        read_current_projection(db_session)
    detail = caught.value.as_dict()
    assert detail["truth_status"] == "stale"
    assert detail["status"] == "unavailable"


def test_list_root_product_options_reads_only_frozen_current_labels(db_session):
    generation = _building_generation(db_session, "journal-root-options")
    _journal_line(db_session)
    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[],
    )
    root_a = models.Item(
        item_code="ROOT-A",
        item_name="Root A",
        item_article="RA-1",
        unit="шт",
        status="active",
    )
    root_b = models.Item(
        item_code="ROOT-B",
        item_name="Root B",
        item_article="RB-2",
        unit="шт",
        status="active",
    )
    db_session.add_all([root_a, root_b])
    db_session.flush()
    payload = dict(snapshot.payload)
    payload["meta"] = {
        **dict(payload["meta"]),
        "root_product_options": [
            {"item_id": root_a.item_id, "item_name": "Root A", "item_article": "RA-1", "item_code": "ROOT-A"},
            {"item_id": root_b.item_id, "item_name": "Root B", "item_article": "RB-2", "item_code": "ROOT-B"},
        ],
    }
    snapshot.payload = payload
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)

    root_a.item_name = "Renamed after acceptance"
    root_b.item_article = "ZZ-LIVE"
    db_session.flush()

    options = list_root_product_options(db_session)
    assert [item["item_id"] for item in options] == [root_a.item_id, root_b.item_id]
    assert options[0]["item_name"] == "Root A"
    assert options[1]["item_name"] == "Root B"


def _launch_after_cutoff(db, generation, work, *, created_at, quantity=10):
    """Открыть исполнительный заказ уже ПОСЛЕ cutoff принятого поколения."""
    order = models.ProductionOrder(
        order_number="SNAP-LAUNCH-1",
        order_date=datetime(2026, 7, 30),
        source="mrp",
        source_run_id=int(work.run_id),
        order_ref1c="snap-launch-ref",
        deletion_mark=False,
        created_at=created_at,
    )
    db.add(order)
    db.flush()
    product = models.ProductionProduct(
        order_id=order.order_id,
        item_id=int(work.item_id),
        line_number=1,
        quantity=quantity,
        produced_qty=0,
        remaining_qty=quantity,
        source_mrp_requirement_id=int(work.requirement_id),
        ledger_generation_id=int(generation.id),
    )
    db.add(product)
    db.flush()
    db.commit()
    return order, product


def test_journal_shows_order_opened_after_cutoff_without_new_generation(db_session):
    """Запуск после cutoff виден сразу: «Не создан» по существующему документу
    1С — это потеря заказа для оператора, а не корректная заморозка плана."""
    generation = _building_generation(db_session, "production-journal-live-launch")
    run, work = _make_proposal(db_session, generation)
    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)

    before = read_current_projection(db_session, limit=100)
    proposal = before["rows"][0]
    assert proposal["status"] == "not_created"
    assert proposal["product_id"] is None

    order, product = _launch_after_cutoff(
        db_session,
        generation,
        work,
        created_at=generation.cutoff.replace(tzinfo=None) + timedelta(minutes=44),
    )

    after = read_current_projection(db_session, limit=100)
    row = next(
        item for item in after["rows"]
        if item.get("source_mrp_requirement_id") == int(work.requirement_id)
    )
    assert row["status"] == "created"
    assert row["product_id"] == product.product_id
    assert row["order_id"] == order.order_id
    assert row["order_number"] == "SNAP-LAUNCH-1"
    assert row["order_ref1c"] == "snap-launch-ref"
    assert row["available_actions"] == ["close_1c"]
    # Плановые величины остаются снимочными: наложение касается только
    # исполнительной части строки.
    assert row["remaining_qty"] == 10
    assert row["coverage_status"] == "shortage"


def test_journal_overlays_print_and_transfer_state_after_cutoff(db_session):
    generation = _building_generation(db_session, "production-journal-live-state")
    run, work = _make_proposal(db_session, generation)
    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)
    _order, product = _launch_after_cutoff(
        db_session,
        generation,
        work,
        created_at=generation.cutoff.replace(tzinfo=None) + timedelta(minutes=44),
    )
    printed_at = generation.cutoff.replace(tzinfo=None) + timedelta(minutes=45)
    db_session.add(
        models.ProductionOrderLineState(
            product_id=product.product_id,
            status="to_move",
            issue_status="exported",
            route_sheet_printed_at=printed_at,
        )
    )
    db_session.commit()

    row = next(
        item
        for item in read_current_projection(db_session, limit=100)["rows"]
        if item.get("source_mrp_requirement_id") == int(work.requirement_id)
    )

    assert row["status"] == "to_move"
    assert row["issue_status"] == "exported"
    assert row["route_sheet_printed_at"] == printed_at.isoformat()


def test_materials_are_available_for_order_opened_after_cutoff(db_session):
    generation = _building_generation(db_session, "production-journal-live-materials")
    run, work = _make_proposal(db_session, generation)
    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)
    _order, product = _launch_after_cutoff(
        db_session,
        generation,
        work,
        created_at=generation.cutoff.replace(tzinfo=None) + timedelta(minutes=44),
    )

    with pytest.raises(CurrentExecutionUnavailable, match="current production material row"):
        get_materials_snapshot(db_session, product.product_id)


def test_materials_endpoint_answers_through_its_strict_response_model(db_session):
    """Комплектация обязана проходить контракт ответа, а не только сборщик.

    `ProductionMaterialsResponse` запрещает лишние поля. Служебные ключи снимка
    наружу не выходят, и проверять это надо через саму ручку: тест, зовущий
    сборщик напрямую, проходит мимо модели ответа и такой отказ не ловит.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.routers.production_control import router as production_router

    generation = _building_generation(db_session, "production-journal-materials-api")
    _item, _order, product = _journal_line(db_session)
    snapshot = _build_candidate(
        db_session, generation.id, accepted_run_ids=[],
    )
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)

    app = FastAPI()
    app.include_router(production_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/production-control/orders/{product.product_id}/materials"
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["product_id"] == product.product_id
    assert "line_quantity" not in payload


def test_work_item_materials_read_the_persisted_proposal_coverage(db_session):
    """Item 29d, obligation path: the stored snapshot carries the quantity it
    was previewed for, so the reader answers 200 without replaying anything.

    It used to answer 400: the preview stripped ``line_quantity`` from the
    stored snapshot, and the reader had no quantity basis.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.routers.production_control import router as production_router

    generation = _building_generation(db_session, "production-journal-wi-materials-api")
    run, work = _make_proposal(db_session, generation)
    snapshot = _build_candidate(
        db_session, generation.id, accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)

    from app.services.item_ledger.current_execution import (
        get_current_execution_scope,
        load_current_execution_rows,
    )
    manifest = get_current_execution_scope(
        db_session,
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    )
    current_row = next(
        row for row in load_current_execution_rows(
            db_session,
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
        )
        if int((row.payload or {}).get("source_mrp_requirement_id") or 0) == int(work.requirement_id)
        and int((row.payload or {}).get("item_id") or 0) == int(work.item_id)
    )

    app = FastAPI()
    app.include_router(production_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/production-control/work-items/{work.id}/materials",
            params={
                "current_identity": current_row.business_identity,
                "expected_source_revision": manifest.source_revision,
            },
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["components"]
    assert body["work_item_id"] == work.id
    assert "line_quantity" not in body  # response shaping, not storage
    stored = current_row.payload["material_coverage_snapshot"]
    assert stored["line_quantity"] == float(current_row.payload["launchable_qty"])


def test_work_item_materials_remain_readable_from_the_published_row_generation(db_session):
    """Смена истины между списком и карточкой не обнуляет комплектующие."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.routers.production_control import router as production_router

    row_generation = _building_generation(db_session, "production-journal-wi-pinned")
    run, work = _make_proposal(db_session, row_generation)
    row_snapshot = _build_candidate(
        db_session, row_generation.id, accepted_run_ids=[run.run_id],
    )
    _accept(db_session, row_generation, row_snapshot)
    _publish_current(db_session, row_generation, row_snapshot)

    current_generation = _building_generation(db_session, "production-journal-wi-current")
    current_snapshot = _build_candidate(
        db_session, current_generation.id, accepted_run_ids=[],
    )
    _accept(db_session, current_generation, current_snapshot)
    _publish_current(db_session, current_generation, current_snapshot)

    app = FastAPI()
    app.include_router(production_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        stale_unpinned = client.get(
            f"/api/v1/production-control/work-items/{work.id}/materials"
        )
        pinned = client.get(
            f"/api/v1/production-control/work-items/{work.id}/materials",
            params={"ledger_generation_id": row_generation.id},
        )
        wrong_generation = client.get(
            f"/api/v1/production-control/work-items/{work.id}/materials",
            params={"ledger_generation_id": current_generation.id},
        )
    app.dependency_overrides.clear()

    assert stale_unpinned.status_code == 409
    assert wrong_generation.status_code == 409
    # A historical generation selector is not a runtime fallback. Once the
    # accepted current publication moved, the old locator is unavailable.
    assert pinned.status_code == 409, pinned.text


def test_materials_survive_a_generation_flip_right_after_launch(db_session):
    """Заказ, выписанный за минуты до смены поколения, не должен пропадать.

    В снимок он не попал — создан после cutoff. Клеймо поколения на строке при
    этом осталось прежним: это отметка о том, при каком поколении заказ выписан,
    а не разрешение считать по нему покрытие. Раньше живой путь сверял клеймо с
    принятым поколением, заказ проваливался между двумя путями, и оператор
    получал material_coverage_snapshot_unavailable вместо комплектующих.
    """
    generation = _building_generation(db_session, "production-journal-gen-flip")
    run, work = _make_proposal(db_session, generation)
    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)
    _order, product = _launch_after_cutoff(
        db_session,
        generation,
        work,
        created_at=generation.cutoff.replace(tzinfo=None) + timedelta(minutes=13),
    )
    # Поколение сменилось уже после того, как заказ был выписан.
    product.ledger_generation_id = int(generation.id) - 1
    db_session.commit()

    with pytest.raises(CurrentExecutionUnavailable, match="current production material row"):
        get_materials_snapshot(db_session, product.product_id)


def test_route_sheet_prints_for_order_opened_after_cutoff(db_session):
    """Маршрутный лист — документ по физическому заказу, а не плановая
    гипотеза: он обязан печататься сразу после запуска."""
    generation = _building_generation(db_session, "production-journal-live-route")
    run, work = _make_proposal(db_session, generation)
    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)

    _, product = _launch_after_cutoff(
        db_session,
        generation,
        work,
        created_at=generation.cutoff.replace(tzinfo=None) + timedelta(minutes=44),
    )

    rows = read_route_sheet_snapshot_rows(db_session, [product.product_id])
    assert len(rows) == 1
    assert int(rows[0]["anchor_product_id"]) == product.product_id


def test_route_sheet_still_fails_closed_for_unknown_product(db_session):
    """Наложение не превращается в дыру: изделия, которого нет ни в снимке, ни
    среди созданных после cutoff, по-прежнему нет."""
    generation = _building_generation(db_session, "production-journal-live-unknown")
    run, work = _make_proposal(db_session, generation)
    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)

    with pytest.raises(RouteSheetSnapshotUnavailable) as caught:
        read_route_sheet_snapshot_rows(db_session, [987654])
    assert "987654" in caught.value.as_dict()["reason"]


def test_order_deleted_in_1c_does_not_resurrect_journal_row(db_session):
    """Снятый пометкой удаления заказ не должен подменять строку-предложение."""
    generation = _building_generation(db_session, "production-journal-live-deleted")
    run, work = _make_proposal(db_session, generation)
    snapshot = _build_candidate(
        db_session,
        generation.id,
        accepted_run_ids=[run.run_id],
    )
    _accept(db_session, generation, snapshot)
    _publish_current(db_session, generation, snapshot)

    order, _product = _launch_after_cutoff(
        db_session,
        generation,
        work,
        created_at=generation.cutoff.replace(tzinfo=None) + timedelta(minutes=44),
    )
    order.deletion_mark = True
    db_session.commit()

    rows = read_current_projection(db_session, limit=100)["rows"]
    row = rows[0]
    assert row["status"] == "not_created"
    assert row["product_id"] is None


def test_a_custody_event_on_a_component_names_it_as_touched(db_session):
    """Item 29c: custody folded by this refresh invalidates snapshot reuse.

    The delta item, the items whose StockBin was restamped and the components
    of the custody events linked to the delta are all "touched"; a snapshot
    naming any of them is previewed again (see the proposal reuse test).
    """
    from app.services.item_ledger.physical_refresh_current_publish import (
        production_affected_item_ids,
    )

    _item, _order, product = _journal_line(db_session)
    delta_item = models.Item(item_code="CUSTODY-DELTA", item_name="delta")
    component = models.Item(item_code="CUSTODY-COMPONENT", item_name="component")
    restamped = models.Item(item_code="CUSTODY-RESTAMPED", item_name="restamped")
    db_session.add_all([delta_item, component, restamped])
    db_session.flush()
    generation = _building_generation(db_session, "custody-touched")
    sle = models.StockLedgerEntry(
        ingest_batch_id=int(generation.physical_import_batch_id),
        source_content_hash="custody-touched-sle", business_identity="custody-touched-sle",
        item_id=int(delta_item.item_id), characteristic_ref="", organization_ref="org",
        warehouse_ref1c="WH", qty=1, posting_at=datetime(2026, 7, 23),
        record_type="Receipt", movement_kind="transfer_in", recorder_type="Doc",
        recorder_ref="custody-touched", line_no="1", ingest_source="test",
    )
    db_session.add(sle)
    db_session.flush()
    db_session.add(models.ProductionMaterialCustodyEvent(
        product_id=int(product.product_id), component_item_id=int(component.item_id),
        source_kind="transfer_posted", source_sle_id=int(sle.id),
        effective_at=datetime(2026, 7, 23), location_kind="workshop",
        warehouse_ref1c="WH", delta_qty=1, idempotency_key="custody-touched",
    ))
    db_session.flush()

    touched = production_affected_item_ids(
        db_session,
        rows=[sle],
        stock_result=SimpleNamespace(
            changed_keys=(SimpleNamespace(item_id=int(restamped.item_id)),)
        ),
        custody_source_sle_ids=[int(sle.id)],
    )

    assert set(touched) == {
        int(delta_item.item_id), int(component.item_id), int(restamped.item_id),
    }


def test_work_item_materials_read_a_proposal_published_by_the_bounded_path(db_session):
    """Item 29d, bounded path: same stored contract, same 200.

    A bounded refresh republishes the proposal without a new work item; the
    obligation-generation work item remains the locator.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.routers.production_control import router as production_router
    from app.services.item_ledger.current_execution import (
        get_current_execution_scope,
        load_current_execution_rows,
        publish_current_production_control_from_payload,
    )

    # The work item belongs to the generation that froze the obligation; the
    # current rows are published by a later one.
    frozen = _building_generation(db_session, "production-journal-wi-bounded-frozen")
    run, work = _make_proposal(db_session, frozen)
    parent = _building_generation(db_session, "production-journal-wi-bounded-parent")
    parent.status = "accepted"
    parent.accepted_at = parent.cutoff
    parent.capabilities = dict(CAPABILITIES)
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    reservation = db_session.get(models.ReservationEntry, int(work.reservation_id))
    reservation.owner_kind = "current"
    reservation.is_current = True
    reservation.current_identity = f"reservation:req:{reservation.requirement_id}:mode:make"
    target = _building_generation(db_session, "production-journal-wi-bounded-target")
    db_session.flush()

    payload = build_compact_current_production_control_payload(
        db_session,
        target_generation_id=target.id,
        parent_generation_id=parent.id,
        assembly_payload={
            "queue_rows": [{
                "entity_kind": "assembly_queue",
                "business_identity": "plan-line:bounded-reader",
                "payload": {"run_id": run.run_id, "item_id": reservation.item_id},
            }],
        },
        drum_payload={"rows": []},
        shelf_payload={"rows": []},
        accepted_run_ids=[run.run_id],
    )
    publish_current_production_control_from_payload(db_session, int(parent.id), payload)
    db_session.flush()
    manifest = get_current_execution_scope(
        db_session, entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    )
    current_row = next(
        row for row in load_current_execution_rows(
            db_session, entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
        )
        if row.payload.get("product_id") is None
    )
    assert current_row.business_identity.startswith("mrp-reservation:")

    app = FastAPI()
    app.include_router(production_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/production-control/work-items/{work.id}/materials",
            params={
                "current_identity": current_row.business_identity,
                "expected_source_revision": manifest.source_revision,
            },
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["components"]
    assert "line_quantity" not in body

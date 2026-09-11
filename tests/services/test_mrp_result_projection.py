from datetime import datetime, timezone
from decimal import Decimal
import asyncio
from hashlib import sha256
import json
import inspect

import pytest

from app import models
from app.routers import plan as plan_router
from app.services import mrp_result_projection
from app.services.mrp_result_projection import (
    build_mrp_result_current_payload,
    read_mrp_result_manifest,
    read_mrp_result_rows,
)
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    _mrp_current_identity,
    publish_current_obligation_views_from_generation,
    publish_current_mrp_results_from_payloads,
)


def _accepted_generation(db):
    physical = models.PhysicalImportBatch(
        batch_key="mrp-result-snapshot-physical",
        status="completed",
        cutoff=datetime(2026, 7, 23, tzinfo=timezone.utc),
        source_watermarks={},
        completed_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    db.add(physical)
    db.flush()
    generation = models.LedgerGeneration(
        generation_key="mrp-result-snapshot-test",
        status="accepted",
        cutoff=datetime(2026, 7, 23, tzinfo=timezone.utc),
        accepted_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
        capabilities={
            "execution_allocations": True,
            "planning_snapshots": True,
        },
        source_watermarks={},
        physical_import_batch_id=physical.id,
        algorithm_version="test",
    )
    db.add(generation)
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    db.flush()
    return generation


def _building_generation(db, *, cutoff):
    physical = models.PhysicalImportBatch(
        batch_key="mrp-result-candidate-physical",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    db.add(physical)
    db.flush()
    generation = models.LedgerGeneration(
        generation_key="mrp-result-candidate-test",
        status="building",
        cutoff=cutoff,
        capabilities={},
        source_watermarks={"generation_kind": "obligation_refresh"},
        physical_import_batch_id=physical.id,
        algorithm_version="test",
    )
    db.add(generation)
    db.flush()
    return generation


def _candidate_purchase_run(db, generation):
    item = models.Item(item_code="CANDIDATE-PURCHASE", item_name="Candidate purchase")
    db.add(item)
    db.flush()
    plan = models.ProductionPlanHeader(
        name="Candidate snapshot plan",
        period_from=datetime(2026, 8, 1).date(),
        period_to=datetime(2026, 8, 31).date(),
        status="fixed",
    )
    db.add(plan)
    db.flush()
    run = models.PlanningRun(
        status="BUILDING_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db.add(run)
    db.flush()
    db.add(models.PlannedPurchase(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=Decimal("5"), planned_qty=Decimal("5"), qty=Decimal("5"),
        need_date=datetime(2026, 8, 1).date(),
        order_date=datetime(2026, 7, 1).date(), lead_time_days=1,
        bucket_date=datetime(2026, 8, 1).date(),
        ledger_generation_id=generation.id,
    ))
    db.flush()
    return run, item


def _seal_candidate_manifest(generation, run):
    payload = {
        "version": 1,
        "entries": [{
            "action": "add", "plan_id": run.source_plan_id,
            "parent_run_id": None, "candidate_run_id": run.run_id,
        }],
        "add_request": {
            "plan_ids": [run.source_plan_id], "horizon_days": None,
            "config_version_id": None, "config_snapshot": {},
        },
    }
    generation.source_watermarks = {
        **generation.source_watermarks,
        "obligation_refresh_manifest": payload,
        "obligation_refresh_manifest_hash": sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


class _CurrentPayload:
    def __init__(self, run, payload):
        self.id = int(run.run_id)
        self.cutoff = run.ledger_cutoff
        self.ledger_generation_id = int(run.ledger_generation_id)
        self.truth_status = "building" if str(run.status) == "BUILDING_SNAPSHOT" else "accepted"
        self.payload = payload
        self.rows = [
            type("PayloadRow", (), {"payload": row["payload"]})()
            for row in payload.get("rows", [])
        ]


def _build_current_payload(db_session, run):
    if not hasattr(run, "run_id"):
        run = db_session.get(models.PlanningRun, int(run))
    return _CurrentPayload(run, build_mrp_result_current_payload(db_session, run.run_id))


def _publish_current_mrp(db_session, run, candidate=None):
    candidate = candidate or _build_current_payload(db_session, run)
    empty = {"rows": [], "meta": {"row_count": 0}}
    truth = db_session.get(models.PlanningTruthState, 1)
    generation_id = int(truth.current_generation_id) if truth else int(run.ledger_generation_id)
    return publish_current_obligation_views_from_generation(
        db_session, generation_id, purchase_payload=empty,
        production_payload=empty,
        mrp_payloads={str(run.run_id): candidate.payload}, period_payloads={},
        period_run_ids=[],
    )


def _manual_mrp_payload(run, rows):
    """Build a compact direct payload for reader/export tests."""
    normalized = []
    counts = {kind: 0 for kind in ("production", "purchase", "rework", "capacity")}
    totals = {kind: 0.0 for kind in counts}
    for index, source in enumerate(rows):
        payload = dict(source)
        kind = str(payload.get("row_kind") or "").strip().lower()
        assert kind in counts
        payload["run_id"] = int(run.run_id)
        payload["row_kind"] = kind
        identity = str(payload.pop("current_identity", f"{kind}:manual:{index}"))
        normalized.append({"current_identity": identity, "payload": payload})
        counts[kind] += 1
        totals[kind] += float(payload.get("qty") or 0)
    return {
        "run_id": int(run.run_id),
        "summary": {},
        "row_counts": counts,
        "total_qty": totals,
        "rows": normalized,
    }


def test_missing_snapshot_fails_closed_without_reading_planning_rows(db_session):
    generation = _accepted_generation(db_session)
    item = models.Item(item_code="MISSING-SNAPSHOT", item_name="Legacy-looking row")
    db_session.add(item)
    db_session.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        models.PlannedPurchase(
            run_id=run.run_id,
            item_id=item.item_id,
            requested_qty=Decimal("99"),
            planned_qty=Decimal("99"),
            qty=Decimal("99"),
            need_date=datetime(2026, 8, 1).date(),
            order_date=datetime(2026, 7, 1).date(),
            lead_time_days=1,
            bucket_date=datetime(2026, 8, 1).date(),
        )
    )

    with pytest.raises(CurrentExecutionUnavailable, match="manifest"):
        read_mrp_result_rows(
            db_session, run.run_id, row_kind="purchase"
        )
    assert db_session.query(models.CurrentExecutionScope).count() == 0


def test_candidate_payload_stays_unpublished_until_current_acceptance(
    db_session, monkeypatch
):
    accepted = _accepted_generation(db_session)
    candidate_generation = _building_generation(db_session, cutoff=accepted.cutoff)
    run, item = _candidate_purchase_run(db_session, candidate_generation)
    _seal_candidate_manifest(candidate_generation, run)

    snapshot = _build_current_payload(db_session, run.run_id)

    assert snapshot.truth_status == "building"
    assert snapshot.ledger_generation_id == candidate_generation.id
    assert len(snapshot.payload["rows"]) == 1
    assert snapshot.payload["rows"][0]["payload"]["item_id"] == item.item_id
    # A normal GET follows the accepted pointer and must neither see this
    # unpublished generation nor calculate a replacement.
    monkeypatch.setattr(
        mrp_result_projection.planning_service,
        "get_run_purchases",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("GET calculated")),
    )
    with pytest.raises(CurrentExecutionUnavailable, match="manifest"):
        read_mrp_result_rows(db_session, run.run_id, row_kind="purchase")
    assert db_session.query(models.CurrentExecutionScope).count() == 0


def test_candidate_builder_accepts_successor_run_lineage(db_session):
    accepted = _accepted_generation(db_session)
    candidate_generation = _building_generation(db_session, cutoff=accepted.cutoff)
    predecessor = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=accepted.id,
        ledger_cutoff=accepted.cutoff,
        active_freeze_version=1,
    )
    db_session.add(predecessor)
    db_session.flush()
    run, _item = _candidate_purchase_run(db_session, candidate_generation)
    plan = db_session.get(models.ProductionPlanHeader, int(run.source_plan_id))
    plan.predecessor_run_id = int(predecessor.run_id)
    run.prior_run_id = int(predecessor.run_id)
    _seal_candidate_manifest(candidate_generation, run)

    snapshot = _build_current_payload(db_session, run.run_id)

    assert snapshot.truth_status == "building"
    assert run.prior_run_id == plan.predecessor_run_id == predecessor.run_id


def test_candidate_payload_is_deterministic_without_persisted_rows(db_session):
    accepted = _accepted_generation(db_session)
    candidate_generation = _building_generation(db_session, cutoff=accepted.cutoff)
    run, _ = _candidate_purchase_run(db_session, candidate_generation)
    _seal_candidate_manifest(candidate_generation, run)

    first = _build_current_payload(db_session, run.run_id)
    second = _build_current_payload(db_session, run.run_id)

    assert second.id == first.id == run.run_id
    assert second.payload == first.payload
    assert db_session.query(models.CurrentExecutionScope).count() == 0


def test_candidate_builder_rejects_retired_refresh_manifest_action(db_session):
    accepted = _accepted_generation(db_session)
    candidate_generation = _building_generation(db_session, cutoff=accepted.cutoff)
    candidate_generation.source_watermarks = {
        **candidate_generation.source_watermarks,
        "parent_generation_id": accepted.id,
    }
    run, item = _candidate_purchase_run(db_session, candidate_generation)
    parent = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=None,
        ledger_cutoff=accepted.cutoff,
        source_plan_id=run.source_plan_id,
        period_from=run.period_from,
        period_to=run.period_to,
    )
    db_session.add(parent)
    db_session.flush()
    run.prior_run_id = parent.run_id
    requirement = models.MrpRequirement(
        run_id=parent.run_id,
        item_id=item.item_id,
        total_required_qty=0,
        net_required_qty=0,
        period_from=parent.period_from,
        period_to=parent.period_to,
        bom_level=0,
    )
    db_session.add(requirement)
    db_session.flush()
    db_session.add(
        models.ReservationEntry(
            ledger_generation_id=accepted.id,
            item_id=item.item_id,
            run_id=parent.run_id,
            freeze_version=0,
            requirement_id=requirement.id,
            priority_period_from=parent.period_from,
            priority_period_to=parent.period_to,
        )
    )
    payload = {
        "version": 1,
        "entries": [{
            "action": "refresh", "plan_id": run.source_plan_id,
            "parent_run_id": parent.run_id, "candidate_run_id": run.run_id,
        }],
    }
    candidate_generation.source_watermarks = {
        **candidate_generation.source_watermarks,
        "obligation_refresh_manifest": payload,
        "obligation_refresh_manifest_hash": sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }

    with pytest.raises(ValueError, match="invalid entries"):
        _build_current_payload(db_session, run.run_id)


def test_candidate_payload_contains_root_membership_without_persistence(db_session):
    accepted = _accepted_generation(db_session)
    candidate_generation = _building_generation(db_session, cutoff=accepted.cutoff)
    run, item = _candidate_purchase_run(db_session, candidate_generation)
    _seal_candidate_manifest(candidate_generation, run)
    root = models.Item(item_code="CANDIDATE-ROOT", item_name="Candidate root")
    db_session.add(root)
    db_session.flush()
    db_session.add(models.MrpFreezeComponent(
        run_id=run.run_id,
        freeze_version=1,
        parent_item_id=root.item_id,
        component_item_id=item.item_id,
        spec_ref="candidate-spec",
        norm_qty_per_unit=Decimal("1"),
        unit_coef=Decimal("1"),
    ))
    db_session.flush()

    payload = _build_current_payload(db_session, run.run_id)

    assert db_session.query(models.CurrentExecutionScope).count() == 0
    assert payload.payload["rows"][0]["payload"]["root_item_ids"] == [root.item_id]


def test_candidate_builder_rejects_missing_or_tampered_sealed_manifest(db_session):
    accepted = _accepted_generation(db_session)
    candidate_generation = _building_generation(db_session, cutoff=accepted.cutoff)
    run, _ = _candidate_purchase_run(db_session, candidate_generation)

    with pytest.raises(ValueError, match="lacks a sealed"):
        _build_current_payload(db_session, run.run_id)

    _seal_candidate_manifest(candidate_generation, run)
    candidate_generation.source_watermarks = {
        **candidate_generation.source_watermarks,
        "obligation_refresh_manifest": {"version": 999, "entries": []},
    }
    with pytest.raises(ValueError, match="hash conflicts"):
        _build_current_payload(db_session, run.run_id)


def test_candidate_builder_rejects_rogue_unsealed_building_run(db_session):
    accepted = _accepted_generation(db_session)
    candidate_generation = _building_generation(db_session, cutoff=accepted.cutoff)
    run, _ = _candidate_purchase_run(db_session, candidate_generation)
    _seal_candidate_manifest(candidate_generation, run)
    rogue_plan = models.ProductionPlanHeader(
        name="Rogue candidate plan",
        period_from=datetime(2026, 9, 1).date(),
        period_to=datetime(2026, 9, 30).date(), status="fixed",
    )
    db_session.add(rogue_plan)
    db_session.flush()
    db_session.add(models.PlanningRun(
        status="BUILDING_SNAPSHOT", config_snapshot={},
        ledger_generation_id=candidate_generation.id,
        ledger_cutoff=candidate_generation.cutoff,
        source_plan_id=rogue_plan.id,
        period_from=rogue_plan.period_from, period_to=rogue_plan.period_to,
    ))
    db_session.flush()

    with pytest.raises(ValueError, match="missing or extra candidates"):
        _build_current_payload(db_session, run.run_id)


def test_builder_publishes_rows_and_frozen_root_membership(
    db_session, monkeypatch
):
    generation = _accepted_generation(db_session)
    root = models.Item(item_code="ROOT-S", item_name="Root")
    component = models.Item(item_code="COMP-S", item_name="Component")
    db_session.add_all([root, component])
    db_session.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=4,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        models.MrpFreezeComponent(
            run_id=run.run_id,
            freeze_version=4,
            parent_item_id=root.item_id,
            component_item_id=component.item_id,
            spec_ref="frozen-spec",
            norm_qty_per_unit=Decimal("2"),
            unit_coef=Decimal("1"),
        )
    )
    db_session.add(
        models.PlannedPurchase(
            run_id=run.run_id,
            item_id=component.item_id,
            requested_qty=Decimal("5"),
            planned_qty=Decimal("5"),
            qty=Decimal("5"),
            need_date=datetime(2026, 8, 1).date(),
            order_date=datetime(2026, 7, 1).date(),
            lead_time_days=1,
            bucket_date=datetime(2026, 8, 1).date(),
            ledger_generation_id=generation.id,
        )
    )
    area = models.ProductionResource(resource_name="Snapshot area")
    db_session.add(area)
    db_session.flush()
    db_session.add(
        models.CapacityLoad(
            run_id=run.run_id,
            area_id=area.resource_id,
            bucket_date=datetime(2026, 8, 2).date(),
            hours_planned=Decimal("10"),
            hours_available=Decimal("8"),
            overload_hours=Decimal("2"),
        )
    )
    db_session.flush()

    snapshot = _build_current_payload(db_session, run.run_id)
    _publish_current_mrp(db_session, run)
    result = read_mrp_result_rows(
        db_session,
        run.run_id,
        row_kind="purchase",
        root_item_id=root.item_id,
    )
    manifest = read_mrp_result_manifest(
        db_session, run.run_id,
    )
    capacity = read_mrp_result_rows(
        db_session,
        run.run_id,
        row_kind="capacity",
        area_id=area.resource_id,
        date_from="2026-08-02",
        date_to="2026-08-02",
    )
    monkeypatch.setattr(
        plan_router,
        "get_run_capacity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("legacy capacity getter was called")
        ),
    )
    capacity_endpoint = asyncio.run(
        plan_router.get_planning_result_capacity(
            run_id=run.run_id,
            area_id=area.resource_id,
            bucket_type=None,
            date_from="2026-08-02",
            date_to="2026-08-02",
            limit=200,
            offset=0,
            db=db_session,
        )
    )

    assert result["current_identity"] == f"mrp-run:{run.run_id}"
    assert result["source_revision"].startswith("accepted:g")
    assert result["ledger_generation"] == generation.id
    assert result["total"] == 1
    assert result["rows"][0]["item_id"] == component.item_id
    assert manifest["snapshot_counts"]["purchase"] == 1
    assert manifest["snapshot_counts"]["capacity"] == 1
    assert capacity["current_identity"] == f"mrp-run:{run.run_id}"
    assert capacity["source_revision"].startswith("accepted:g")
    assert capacity_endpoint["current_identity"] == f"mrp-run:{run.run_id}"
    assert capacity["total"] == 1
    assert capacity["rows"][0]["overload_hours"] == 2.0
    assert result["rows"][0]["root_item_ids"] == [root.item_id]


def test_current_scope_id_cannot_cross_run_or_generation(db_session):
    generation = _accepted_generation(db_session)
    run_a = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    run_b = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db_session.add_all([run_a, run_b])
    db_session.flush()
    _publish_current_mrp(db_session, run_a)
    scope = db_session.query(models.CurrentExecutionScope).filter_by(
        entity_kind="mrp_result", scope_key="mrp:all-live-plans"
    ).one()
    with pytest.raises(CurrentExecutionUnavailable, match="current MRP run is missing"):
        read_mrp_result_rows(
            db_session,
            run_b.run_id,
            row_kind="production",
            current_scope_id=scope.id,
        )


def test_builder_rejects_legacy_obligation_without_generation(db_session):
    generation = _accepted_generation(db_session)
    item = models.Item(item_code="LEGACY-PURCHASE", item_name="Legacy purchase")
    db_session.add(item)
    db_session.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        models.PlannedPurchase(
            run_id=run.run_id,
            item_id=item.item_id,
            requested_qty=Decimal("7"),
            planned_qty=Decimal("7"),
            qty=Decimal("7"),
            need_date=datetime(2026, 8, 1).date(),
            order_date=datetime(2026, 7, 1).date(),
            lead_time_days=1,
            bucket_date=datetime(2026, 8, 1).date(),
            ledger_generation_id=None,
        )
    )
    db_session.flush()

    with pytest.raises(ValueError, match="NULL or foreign"):
        _build_current_payload(db_session, run.run_id)

    assert db_session.query(models.CurrentExecutionScope).count() == 0


def _physical_refresh_fork(db, parent, *, key, cutoff):
    """Publish a fact-only fork and advance the pointer, sealing the lineage."""
    physical = models.PhysicalImportBatch(
        batch_key=f"{key}-physical",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    db.add(physical)
    db.flush()
    child = models.LedgerGeneration(
        generation_key=key,
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        capabilities=dict(parent.capabilities or {}),
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": int(parent.id),
        },
        physical_import_batch_id=physical.id,
        algorithm_version="test",
    )
    db.add(child)
    db.flush()
    pointer = db.get(models.PlanningTruthState, 1)
    pointer.current_generation_id = int(child.id)
    db.flush()
    return child


def _fixed_purchase_run(db, generation, *, code):
    """One fixed run whose obligation is frozen against ``generation``."""
    item = models.Item(item_code=code, item_name=f"Item {code}")
    db.add(item)
    db.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db.add(run)
    db.flush()
    db.add(
        models.PlannedPurchase(
            run_id=run.run_id,
            item_id=item.item_id,
            requested_qty=Decimal("9"),
            planned_qty=Decimal("9"),
            qty=Decimal("9"),
            need_date=datetime(2026, 8, 1).date(),
            order_date=datetime(2026, 7, 1).date(),
            lead_time_days=1,
            bucket_date=datetime(2026, 8, 1).date(),
            ledger_generation_id=generation.id,
        )
    )
    db.flush()
    return run


def test_builder_accepts_a_run_inherited_through_a_physical_refresh(db_session):
    """A fact-only fork does not re-anchor an obligation, so the run stays live.

    Requiring the run to carry the accepted generation id is what killed this
    page on the live contour: ten runs stayed anchored to their freeze
    generation while the hourly refresh advanced the pointer far past it.
    """
    anchor = _accepted_generation(db_session)
    run = _fixed_purchase_run(db_session, anchor, code="INHERIT-PURCHASE")
    child = _physical_refresh_fork(
        db_session, anchor, key="inherit-child", cutoff=datetime(2026, 7, 24, tzinfo=timezone.utc)
    )

    snapshot = _build_current_payload(db_session, run.run_id)

    # A physical fact fork does not retarget the frozen obligation. The run
    # remains anchored to its accepted generation while the fork advances only
    # the physical pointer.
    assert int(snapshot.ledger_generation_id) == int(anchor.id)
    _publish_current_mrp(db_session, run)
    manifest = read_mrp_result_manifest(db_session, run.run_id)
    assert manifest["current_identity"] == f"mrp-run:{run.run_id}"
    assert manifest["source_revision"].startswith("accepted:g")
    assert manifest["ledger_generation"] == int(child.id)
    assert manifest["snapshot_counts"]["purchase"] == 1


def test_inherited_obligation_snapshot_is_reused_by_the_next_refresh(db_session):
    """The payload projects a frozen obligation, so a fact fork must not rebuild it."""
    anchor = _accepted_generation(db_session)
    run = _fixed_purchase_run(db_session, anchor, code="REUSE-PURCHASE")
    first_child = _physical_refresh_fork(
        db_session, anchor, key="reuse-child", cutoff=datetime(2026, 7, 24, tzinfo=timezone.utc)
    )
    first = _build_current_payload(db_session, run.run_id)
    _physical_refresh_fork(
        db_session,
        first_child,
        key="reuse-grandchild",
        cutoff=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    repeated = _build_current_payload(db_session, run.run_id)

    assert repeated.id == first.id
    assert repeated.payload == first.payload
    assert db_session.query(models.CurrentExecutionScope).count() == 0
    # The current publication remains tied to the obligation's accepted anchor;
    # it is not inferred from the later physical pointer.
    assert first.ledger_generation_id == int(anchor.id)


def test_builder_rejects_a_run_anchored_outside_the_sealed_lineage(db_session):
    anchor = _accepted_generation(db_session)
    run = _fixed_purchase_run(db_session, anchor, code="FOREIGN-PURCHASE")
    foreign = _physical_refresh_fork(
        db_session, anchor, key="foreign-branch", cutoff=datetime(2026, 7, 24, tzinfo=timezone.utc)
    )
    run.ledger_generation_id = int(foreign.id)
    run.ledger_cutoff = foreign.cutoff
    db_session.flush()
    # A sibling fork of the same parent: accepted truth never descends from it.
    _physical_refresh_fork(
        db_session, anchor, key="accepted-branch", cutoff=datetime(2026, 7, 25, tzinfo=timezone.utc)
    )

    with pytest.raises(ValueError, match="outside the sealed lineage"):
        _build_current_payload(db_session, run.run_id)

    assert db_session.query(models.CurrentExecutionScope).count() == 0


def test_builder_rejects_a_run_whose_cutoff_left_its_anchor(db_session):
    """Inheritance widens the generation check, never the frozen cutoff."""
    anchor = _accepted_generation(db_session)
    run = _fixed_purchase_run(db_session, anchor, code="CUTOFF-PURCHASE")
    child = _physical_refresh_fork(
        db_session, anchor, key="cutoff-child", cutoff=datetime(2026, 7, 24, tzinfo=timezone.utc)
    )
    run.ledger_cutoff = child.cutoff
    db_session.flush()

    with pytest.raises(ValueError, match="cutoff differs from the accepted Ledger cutoff"):
        _build_current_payload(db_session, run.run_id)

    assert db_session.query(models.CurrentExecutionScope).count() == 0


def test_builder_failure_rolls_back_manifest_rows_and_memberships(
    db_session, monkeypatch
):
    generation = _accepted_generation(db_session)
    item = models.Item(item_code="ATOMIC-PURCHASE", item_name="Atomic purchase")
    db_session.add(item)
    db_session.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        models.PlannedPurchase(
            run_id=run.run_id,
            item_id=item.item_id,
            requested_qty=Decimal("3"),
            planned_qty=Decimal("3"),
            qty=Decimal("3"),
            need_date=datetime(2026, 8, 1).date(),
            order_date=datetime(2026, 7, 1).date(),
            lead_time_days=1,
            bucket_date=datetime(2026, 8, 1).date(),
            ledger_generation_id=generation.id,
        )
    )
    db_session.flush()

    def fail_after_manifest_and_rows(*args, **kwargs):
        raise RuntimeError("injected root membership failure")

    monkeypatch.setattr(
        mrp_result_projection, "_frozen_root_membership", fail_after_manifest_and_rows
    )
    with pytest.raises(RuntimeError, match="injected"):
        _build_current_payload(db_session, run.run_id)

    assert db_session.query(models.CurrentExecutionScope).count() == 0


def test_purchase_export_reads_shared_snapshot_not_legacy_getter(
    db_session, monkeypatch
):
    generation = _accepted_generation(db_session)
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()
    _publish_current_mrp(
        db_session,
        run,
        candidate=_CurrentPayload(
            run,
            _manual_mrp_payload(run, [{
                "current_identity": "purchase:one",
                "row_kind": "purchase",
                "item_id": 1,
                "item_name": "Snapshot item",
                "item_article": "S-1",
                "qty": 4.0,
                "unit": "шт.",
                "bucket_date": "2026-08-01",
                "sort_key": "2026-08-01|000000000001|000000000000",
            }]),
        ),
    )

    def legacy_service_must_not_run(*args, **kwargs):
        raise AssertionError("legacy purchase getter was called")

    monkeypatch.setattr(
        plan_router, "get_run_purchases", legacy_service_must_not_run
    )
    result = asyncio.run(
        plan_router.export_planning_result_purchases(
            run_id=run.run_id,
            format="csv",
            root_item_id=None,
            bucket_type=None,
            date_from=None,
            date_to=None,
            sort_by=None,
            sort_dir=None,
            current_scope_id=None,
            db=db_session,
        )
    )

    assert result["current_identity"] == f"mrp-run:{run.run_id}"
    assert result["source_revision"].startswith("accepted:g")
    assert result["total_rows"] == 1
    assert "Snapshot item" in result["data"]


def test_grouped_endpoints_read_snapshot_rows_not_legacy_group_getters(
    db_session, monkeypatch
):
    generation = _accepted_generation(db_session)
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()
    _publish_current_mrp(
        db_session,
        run,
        candidate=_CurrentPayload(
            run,
            _manual_mrp_payload(run, [
                {
                    "current_identity": "purchase:one",
                    "row_kind": "purchase",
                    "item_id": 1,
                    "item_name": "Snapshot purchase",
                    "item_article": "P-1",
                    "qty": 4.0,
                    "unit": "шт.",
                    "bucket_date": "2026-08-01",
                    "sort_key": "2026-08-01|000000000001|000000000000",
                },
                {
                    "current_identity": "rework:one",
                    "row_kind": "rework",
                    "item_id": 2,
                    "item_name": "Snapshot rework",
                    "item_article": "R-1",
                    "qty": 3.0,
                    "unit": "шт.",
                    "bucket_date": "2026-08-01",
                    "sort_key": "2026-08-01|000000000002|000000000001",
                },
            ]),
        ),
    )

    calls = {"snapshot_reads": 0}

    def _counting_snapshot_reader(*args, **kwargs):
        calls["snapshot_reads"] += 1
        return mrp_result_projection.read_mrp_result_rows(*args, **kwargs)

    def _legacy_group_getter_called(*_args, **_kwargs):
        raise AssertionError("legacy grouped getter was called")

    monkeypatch.setattr(plan_router, "read_mrp_result_rows", _counting_snapshot_reader)
    monkeypatch.setattr(
        plan_router,
        "get_run_purchases_grouped_by_category",
        _legacy_group_getter_called,
        raising=False,
    )
    monkeypatch.setattr(
        plan_router,
        "get_run_rework_grouped_by_category",
        _legacy_group_getter_called,
        raising=False,
    )

    # Static guard: grouped result endpoints use the current reader and avoid
    # dead legacy grouped live getters.
    assert "get_run_purchases_grouped_by_category" not in inspect.getsource(
        plan_router.get_planning_result_purchases_grouped_by_category
    )
    assert "get_run_rework_grouped_by_category" not in inspect.getsource(
        plan_router.get_planning_result_rework_grouped_by_category
    )

    purchase_payload = asyncio.run(
        plan_router.get_planning_result_purchases_grouped_by_category(
            run_id=run.run_id,
            item_id=None,
            date_from=None,
            date_to=None,
            limit=100,
            offset=0,
            sort_by=None,
            sort_dir="asc",
            db=db_session,
        )
    )
    rework_payload = asyncio.run(
        plan_router.get_planning_result_rework_grouped_by_category(
            run_id=run.run_id,
            item_id=None,
            date_from=None,
            date_to=None,
            limit=100,
            offset=0,
            sort_by=None,
            sort_dir="asc",
            db=db_session,
        )
    )

    assert purchase_payload["current_identity"] == f"mrp-run:{run.run_id}"
    assert purchase_payload["source_revision"].startswith("accepted:g")
    assert purchase_payload["ledger_generation"] == generation.id
    assert purchase_payload["total_groups"] == 1
    assert purchase_payload["total_orders"] == 1
    assert rework_payload["current_identity"] == f"mrp-run:{run.run_id}"
    assert rework_payload["source_revision"].startswith("accepted:g")
    assert rework_payload["ledger_generation"] == generation.id
    assert rework_payload["total_groups"] == 1
    assert rework_payload["total_orders"] == 1
    assert calls["snapshot_reads"] >= 2


def test_read_mrp_result_rows_supports_supplier_filter_and_missing_supplier_filter(
    db_session,
):
    generation = _accepted_generation(db_session)
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()

    item_a = models.Item(item_code="SUP-A", item_name="A")
    item_b = models.Item(item_code="SUP-B", item_name="B")
    item_c = models.Item(item_code="SUP-C", item_name="C")
    db_session.add_all([item_a, item_b, item_c])
    db_session.flush()

    _publish_current_mrp(
        db_session,
        run,
        candidate=_CurrentPayload(
            run,
            _manual_mrp_payload(run, [
                {
                    "current_identity": "purchase-a",
                    "row_kind": "purchase",
                    "item_id": item_a.item_id,
                    "item_name": "A",
                    "supplier_ref1c": "supp-a",
                    "supplier_name": "Поставщик A",
                    "qty": 4,
                    "sort_key": "2026-01-01|000000000001|000000000000",
                },
                {
                    "current_identity": "purchase-b",
                    "row_kind": "purchase",
                    "item_id": item_b.item_id,
                    "item_name": "B",
                    "supplier_ref1c": "supp-b",
                    "supplier_name": "   ",
                    "qty": 3,
                    "sort_key": "2026-01-01|000000000002|000000000000",
                },
                {
                    "current_identity": "purchase-c",
                    "row_kind": "purchase",
                    "item_id": item_c.item_id,
                    "item_name": "C",
                    "supplier_ref1c": "supp-c",
                    "supplier_name": "",
                    "qty": 5,
                    "sort_key": "2026-01-02|000000000003|000000000000",
                },
            ]),
        ),
    )

    supplier_rows = read_mrp_result_rows(
        db_session,
        run.run_id,
        row_kind="purchase",
        supplier_ref1c="supp-b",
    )
    missing_rows = read_mrp_result_rows(
        db_session,
        run.run_id,
        row_kind="purchase",
        supplier_ref1c="__missing_supplier_name",
    )

    assert supplier_rows["total"] == 1
    assert supplier_rows["rows"][0]["item_name"] == "B"
    assert supplier_rows["rows"][0]["supplier_name"] == "   "
    assert missing_rows["total"] == 2
    assert {row["item_name"] for row in missing_rows["rows"]} == {"B", "C"}


def test_read_mrp_result_rows_supports_category_filters_and_missing_category(
    db_session,
):
    generation = _accepted_generation(db_session)
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()

    _publish_current_mrp(
        db_session,
        run,
        candidate=_CurrentPayload(
            run,
            _manual_mrp_payload(run, [
                {
                    "current_identity": "purchase-cat-a",
                    "row_kind": "purchase",
                    "item_id": 1,
                    "item_name": "Category A",
                    "category_id": 11,
                    "category_ref1c": "cat-a",
                    "qty": 2,
                    "sort_key": "2026-01-01|000000000001|000000000000",
                },
                {
                    "current_identity": "purchase-cat-b",
                    "row_kind": "purchase",
                    "item_id": 2,
                    "item_name": "Category B",
                    "category_id": 12,
                    "category_ref1c": "cat-b",
                    "qty": 4,
                    "sort_key": "2026-01-01|000000000002|000000000000",
                },
                {
                    "current_identity": "purchase-missing-cat",
                    "row_kind": "purchase",
                    "item_id": 3,
                    "item_name": "Missing category",
                    "category_id": None,
                    "category_ref1c": None,
                    "qty": 6,
                    "sort_key": "2026-01-01|000000000003|000000000000",
                },
            ]),
        ),
    )

    category_rows = read_mrp_result_rows(
        db_session,
        run.run_id,
        row_kind="purchase",
        category_id=11,
    )
    ref_rows = read_mrp_result_rows(
        db_session,
        run.run_id,
        row_kind="purchase",
        category_ref1c="cat-b",
    )
    missing_category_rows = read_mrp_result_rows(
        db_session,
        run.run_id,
        row_kind="purchase",
        category_ref1c="__missing_category",
    )

    assert category_rows["total"] == 1
    assert category_rows["rows"][0]["item_name"] == "Category A"
    assert ref_rows["total"] == 1
    assert ref_rows["rows"][0]["item_name"] == "Category B"
    assert missing_category_rows["total"] == 1
    assert missing_category_rows["rows"][0]["item_name"] == "Missing category"

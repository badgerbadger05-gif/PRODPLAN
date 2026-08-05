from datetime import date, datetime, timezone
from decimal import Decimal

from app import models
from app.services.specification_rebase_worker import (
    run_one_pending_specification_rebase,
)
from app.services.specification_revision import record_specification_revisions
from app.services.mrp_mutation_guard import MrpMutationLineageError
from app.services.production_control_journal import (
    _frozen_spec_identity_for_requirement,
)
import pytest


def test_revision_history_enqueues_only_known_content_change(db_session):
    item = models.Item(item_code="C", item_name="Component", status="active")
    spec = models.Specification(
        spec_code="S",
        spec_name="Specification",
        spec_ref1c="spec-ref",
    )
    db_session.add_all([item, spec])
    db_session.flush()
    component = models.SpecComponent(
        spec_id=int(spec.spec_id),
        item_id=int(item.item_id),
        quantity=Decimal("1"),
        component_type="Материал",
    )
    db_session.add(component)
    db_session.flush()

    baseline = record_specification_revisions(
        db_session,
        [int(spec.spec_id)],
        previous_hash_by_id={int(spec.spec_id): None},
    )
    first_hash = str(spec.content_hash)
    assert baseline["revisions_created"] == 1
    assert baseline["rebase_requests_queued"] == 0

    component.quantity = Decimal("2")
    db_session.flush()
    changed = record_specification_revisions(
        db_session,
        [int(spec.spec_id)],
        previous_hash_by_id={int(spec.spec_id): first_hash},
    )

    assert changed["revisions_created"] == 1
    assert changed["rebase_requests_queued"] == 1
    assert str(spec.content_hash) != first_hash
    assert db_session.query(models.SpecificationRevision).count() == 2
    queued = db_session.query(models.SpecificationRebaseQueue).one()
    assert queued.old_content_hash == first_hash
    assert queued.new_content_hash == spec.content_hash


def test_worker_drains_one_stale_run_and_completes_request(db_session, monkeypatch):
    spec = models.Specification(
        spec_code="S",
        spec_name="Specification",
        spec_ref1c="spec-ref",
        content_hash="new-hash",
    )
    item = models.Item(item_code="R", item_name="Root", status="active")
    db_session.add_all([spec, item])
    db_session.flush()
    revision = models.SpecificationRevision(
        spec_id=int(spec.spec_id),
        content_hash="new-hash",
        payload={"version": 1},
        source="test",
    )
    db_session.add(revision)
    db_session.flush()
    request = models.SpecificationRebaseQueue(
        spec_id=int(spec.spec_id),
        revision_id=int(revision.id),
        old_content_hash="old-hash",
        new_content_hash="new-hash",
        status="pending",
    )
    plan = models.ProductionPlanHeader(
        name="Old",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="fixed",
        fixed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    db_session.add_all([request, plan])
    db_session.flush()
    run = models.PlanningRun(
        source_plan_id=int(plan.id),
        status="FIXED_SNAPSHOT",
        period_from=plan.period_from,
        period_to=plan.period_to,
        fixed_at=plan.fixed_at,
        active_freeze_version=1,
        pinned=True,
        config_snapshot={},
    )
    db_session.add(run)
    db_session.flush()
    db_session.add(
        models.MrpFreezeComponent(
            run_id=int(run.run_id),
            freeze_version=1,
            parent_item_id=int(item.item_id),
            parent_characteristic_ref="",
            parent_organization_ref="",
            parent_planning_stock_pool="default",
            component_item_id=int(item.item_id),
            component_characteristic_ref="",
            component_organization_ref="",
            component_planning_stock_pool="default",
            spec_ref="spec-ref",
            spec_version="old-hash",
            norm_qty_per_unit=Decimal("1"),
            unit_coef=Decimal("1"),
        )
    )
    db_session.commit()

    from app.services import specification_rebase_worker as worker

    def fake_rebase(db, run_id, **kwargs):
        old = db.get(models.PlanningRun, int(run_id))
        old.status = "CLOSED"
        plan = db.get(models.ProductionPlanHeader, int(old.source_plan_id))
        successor_run = models.PlanningRun(
            source_plan_id=int(plan.id),
            prior_run_id=int(old.run_id),
            status="FIXED_SNAPSHOT",
            period_from=plan.period_from,
            period_to=plan.period_to,
            fixed_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
            active_freeze_version=1,
            pinned=True,
            config_snapshot={},
        )
        db.add(successor_run)
        db.flush()
        db.add(
            models.MrpFreezeComponent(
                run_id=int(successor_run.run_id),
                freeze_version=1,
                parent_item_id=int(item.item_id),
                parent_characteristic_ref="",
                parent_organization_ref="",
                parent_planning_stock_pool="default",
                component_item_id=int(item.item_id),
                component_characteristic_ref="",
                component_organization_ref="",
                component_planning_stock_pool="default",
                spec_ref="spec-ref",
                spec_version="new-hash",
                norm_qty_per_unit=Decimal("2"),
                unit_coef=Decimal("1"),
            )
        )
        db.commit()
        return {"status": "rebased", "predecessor_run_id": int(run_id)}

    monkeypatch.setattr(worker, "rebase_fixed_plan_remaining_roots", fake_rebase)

    result = run_one_pending_specification_rebase(db_session)

    assert result["status"] == "rebased"
    assert result["affected_runs_before"] == 1
    assert result["affected_runs_after"] == 0
    db_session.expire_all()
    queued = db_session.get(models.SpecificationRebaseQueue, int(request.id))
    assert queued.status == "completed"
    assert queued.attempt_count == 1


def test_new_order_is_blocked_when_mrp_has_older_spec_revision(db_session):
    root = models.Item(item_code="ROOT", item_name="Root", status="active")
    component = models.Item(item_code="COMP", item_name="Component", status="active")
    spec = models.Specification(
        spec_code="S",
        spec_name="Specification",
        spec_ref1c="spec-ref",
        content_hash="new-hash",
    )
    db_session.add_all([root, component, spec])
    db_session.flush()
    db_session.add(
        models.DefaultSpecification(item_id=int(root.item_id), spec_id=int(spec.spec_id))
    )
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        active_freeze_version=1,
        config_snapshot={},
    )
    db_session.add(run)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=int(run.run_id),
        item_id=int(root.item_id),
        total_required_qty=Decimal("1"),
        net_required_qty=Decimal("1"),
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        bom_level=0,
        freeze_version=1,
    )
    db_session.add(requirement)
    db_session.flush()
    db_session.add(
        models.MrpFreezeComponent(
            run_id=int(run.run_id),
            freeze_version=1,
            parent_item_id=int(root.item_id),
            parent_characteristic_ref="",
            parent_organization_ref="",
            parent_planning_stock_pool="default",
            component_item_id=int(component.item_id),
            component_characteristic_ref="",
            component_organization_ref="",
            component_planning_stock_pool="default",
            spec_ref="spec-ref",
            spec_version="old-hash",
            norm_qty_per_unit=Decimal("1"),
            unit_coef=Decimal("1"),
        )
    )
    db_session.flush()

    with pytest.raises(MrpMutationLineageError, match="successor-MRP"):
        _frozen_spec_identity_for_requirement(db_session, requirement)

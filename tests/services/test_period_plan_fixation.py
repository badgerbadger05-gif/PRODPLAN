"""Atomic «Зафиксировать» — period_plan_target.md §Фиксация.

Fixation is ONE action: validate a non-empty plan, publish the single-generation
MRP snapshot, mark the plan fixed — all in one transaction. A plan must never
end up ``fixed`` (immutable, no way back to draft) without its snapshot.
"""

import datetime
from datetime import date

import pytest

from app.models import (
    AssemblyRate,
    Item,
    LedgerGeneration,
    MrpRequirement,
    PhysicalImportBatch,
    PlanningRun,
    PlanningTruthState,
    ProductionPlanHeader,
    ProductionPlanLine,
    ProductionResource,
)
from app.services import period_plan_service
from app.services.period_plan_service import (
    _fix_refresh_generation_key,
    create_mrp_snapshot_for_plan,
    fix_period_plan,
)


@pytest.fixture(autouse=True)
def _accepted_planning_truth(db_session):
    """One explicit accepted Ledger generation every fixation descends from."""
    cutoff = datetime.datetime(2026, 7, 23)
    batch = PhysicalImportBatch(
        batch_key="fixation-ledger",
        status="completed",
        cutoff=cutoff,
        source_watermarks={"opening_at": "2025-01-01T00:00:00+00:00"},
        completed_at=cutoff,
    )
    generation = LedgerGeneration(
        generation_key="fixation-ledger",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        source_watermarks={"replay_from": "2026-06-01T00:00:00"},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
            "planning_snapshots": True,
        },
        physical_import_batch=batch,
        algorithm_version="test",
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(PlanningTruthState(id=1, current_generation_id=generation.id))
    resource = ProductionResource(
        resource_name="Fixation assembly",
        planning_range=30,
        capacity=100,
    )
    db_session.add(resource)
    db_session.flush()
    db_session.info["fixation_assembly_resource_id"] = int(resource.resource_id)
    db_session.commit()
    db_session.info["fixation_generation_id"] = int(generation.id)
    return generation


def _purchased_item(db, code: str) -> Item:
    item = Item(
        item_code=code,
        item_name=f"Деталь {code}",
        item_article=code,
        unit="шт",
        stock_qty=0.0,
        replenishment_method="Покупка",
        replenishment_time=3,
        status="active",
    )
    db.add(item)
    db.flush()
    db.add(AssemblyRate(
        resource_id=int(db.info["fixation_assembly_resource_id"]),
        item_id=int(item.item_id),
        qty_per_capacity=1,
    ))
    db.flush()
    return item


def _draft_plan(db, item: Item | None, qty: float = 5.0) -> ProductionPlanHeader:
    bucket = date(2026, 8, 7)
    plan = ProductionPlanHeader(
        name="Август",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="draft",
        created_by="test",
    )
    db.add(plan)
    db.flush()
    if item is not None:
        db.add(ProductionPlanLine(
            plan_id=plan.id, item_id=item.item_id, bucket_date=bucket, qty=qty,
        ))
    db.commit()
    return plan


# ---------------------------------------------------------------------------
# (а) the flagship button is one atomic scenario
# ---------------------------------------------------------------------------

def test_fix_publishes_the_mrp_snapshot_and_fixes_the_plan_in_one_action(db_session):
    item = _purchased_item(db_session, "FIX-ATOMIC")
    plan = _draft_plan(db_session, item, qty=12.0)

    result = fix_period_plan(db_session, plan.id, fixed_by="tester")

    assert result["status"] == "fixed"
    assert result["fixed_by"] == "tester"
    assert result["fixed_at"] is not None

    snapshot = result["mrp"]
    assert snapshot["status"] == "ok"
    assert snapshot["published"] is True
    # The key is server-owned, derived from the plan and the parent generation.
    assert snapshot["generation_key"] == _fix_refresh_generation_key(
        plan_id=int(plan.id),
        parent_generation_id=int(db_session.info["fixation_generation_id"]),
    )

    run = db_session.query(PlanningRun).filter_by(run_id=snapshot["run_id"]).one()
    assert run.source_plan_id == int(plan.id)
    assert run.status == "FIXED_SNAPSHOT"
    assert run.ledger_generation_id == int(snapshot["ledger_generation_id"])
    # The BOM was exploded once and its requirement persisted by the same action.
    requirement = db_session.query(MrpRequirement).filter_by(
        run_id=int(run.run_id), item_id=item.item_id,
    ).one()
    assert float(requirement.total_required_qty) == pytest.approx(12.0)

    db_session.expire_all()
    assert db_session.get(ProductionPlanHeader, plan.id).status == "fixed"


def test_fix_is_idempotent_for_a_plan_that_already_owns_a_snapshot(db_session):
    item = _purchased_item(db_session, "FIX-IDEMPOTENT")
    plan = _draft_plan(db_session, item, qty=4.0)

    first = fix_period_plan(db_session, plan.id, fixed_by="tester")
    second = fix_period_plan(db_session, plan.id, fixed_by="tester")

    assert second["mrp"]["run_id"] == first["mrp"]["run_id"]
    assert second["mrp"].get("immutable") is True
    assert second["mrp"]["published"] is False
    assert db_session.query(PlanningRun).filter_by(
        source_plan_id=int(plan.id), status="FIXED_SNAPSHOT",
    ).count() == 1


# ---------------------------------------------------------------------------
# (б) fail closed — a failed snapshot must not leave the plan fixed
# ---------------------------------------------------------------------------

def test_failed_snapshot_leaves_the_plan_in_draft(db_session, monkeypatch):
    item = _purchased_item(db_session, "FIX-FAILCLOSED")
    plan = _draft_plan(db_session, item, qty=9.0)

    def boom(db, **kwargs):
        raise RuntimeError("ledger refresh exploded")

    monkeypatch.setattr(
        "app.services.obligation_refresh_orchestrator.run_obligation_refresh", boom,
    )

    with pytest.raises(RuntimeError, match="ledger refresh exploded"):
        fix_period_plan(db_session, plan.id, fixed_by="tester")

    db_session.expire_all()
    stored = db_session.get(ProductionPlanHeader, plan.id)
    assert stored.status == "draft"
    assert stored.fixed_at is None
    assert stored.fixed_by is None
    assert db_session.query(PlanningRun).filter_by(source_plan_id=int(plan.id)).count() == 0
    # The plan is still editable — the whole point of failing closed.
    period_plan_service._assert_plan_editable(stored)


# ---------------------------------------------------------------------------
# (в) an empty plan is not a fixable obligation
# ---------------------------------------------------------------------------

def test_fix_rejects_an_empty_plan(db_session):
    plan = _draft_plan(db_session, None)

    with pytest.raises(ValueError, match="пустой план"):
        fix_period_plan(db_session, plan.id)

    db_session.expire_all()
    assert db_session.get(ProductionPlanHeader, plan.id).status == "draft"


def test_fix_rejects_a_plan_whose_only_line_is_zero(db_session):
    item = _purchased_item(db_session, "FIX-ZERO")
    plan = _draft_plan(db_session, item, qty=0.0)

    with pytest.raises(ValueError, match="пустой план"):
        fix_period_plan(db_session, plan.id)

    db_session.expire_all()
    assert db_session.get(ProductionPlanHeader, plan.id).status == "draft"


def test_fix_rejects_a_closed_plan(db_session):
    item = _purchased_item(db_session, "FIX-CLOSED")
    plan = _draft_plan(db_session, item, qty=3.0)
    plan.status = "closed"
    db_session.commit()

    with pytest.raises(ValueError, match="Закрытый план"):
        fix_period_plan(db_session, plan.id)


# ---------------------------------------------------------------------------
# server-owned generation key
# ---------------------------------------------------------------------------

def test_snapshot_helper_resolves_the_generation_key_without_a_caller(db_session):
    item = _purchased_item(db_session, "FIX-KEYLESS")
    plan = _draft_plan(db_session, item, qty=6.0)
    plan.status = "fixed"
    db_session.commit()

    result = create_mrp_snapshot_for_plan(db_session, plan.id, started_by="test")
    db_session.commit()

    assert result["generation_key"] == _fix_refresh_generation_key(
        plan_id=int(plan.id),
        parent_generation_id=int(db_session.info["fixation_generation_id"]),
    )
    assert result["run_id"] > 0


def test_snapshot_helper_fails_closed_without_accepted_truth(db_session):
    item = _purchased_item(db_session, "FIX-NOTRUTH")
    plan = _draft_plan(db_session, item, qty=6.0)
    plan.status = "fixed"
    db_session.query(PlanningTruthState).delete()
    db_session.commit()

    with pytest.raises(ValueError, match="Ledger truth is unavailable"):
        create_mrp_snapshot_for_plan(db_session, plan.id, started_by="test")

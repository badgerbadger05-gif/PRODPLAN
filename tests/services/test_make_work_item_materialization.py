from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import models
from app.services.production_control_journal import cancel_local_order, materialize_make_work_items


def _scope(db):
    cutoff = datetime(2026, 7, 26, 8, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="make-work-item-physical", status="completed", cutoff=cutoff
    )
    generation = models.LedgerGeneration(
        generation_key="make-work-item-generation",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        physical_import_batch=physical,
        algorithm_version="test",
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
    )
    item = models.Item(
        item_code="MAKE-WORK",
        item_name="Make work item",
        replenishment_method="Производство",
        optimal_batch=Decimal("4"),
    )
    plan = models.ProductionPlanHeader(
        name="Make work plan",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="fixed",
    )
    db.add_all([physical, generation, item, plan])
    db.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        source_plan_id=plan.id,
        period_from=plan.period_from,
        period_to=plan.period_to,
        ledger_generation_id=generation.id,
        ledger_cutoff=cutoff,
        active_freeze_version=1,
    )
    db.add(run)
    db.flush()
    db.add(models.PlanningLivePointer(plan_id=plan.id, run_id=run.run_id))
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=10,
        net_required_qty=10,
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
        reserved_qty=10,
        covered_from_stock_at_freeze_qty=0,
        replenishment_required_qty=10,
        replenishment_received_qty=2,
        realized_qty=2,
        lifecycle_status="active",
        is_current=True,
        owner_kind="current",
        current_identity=f"reservation:req:{requirement.id}:mode:make",
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
        replenishment_required_qty=10,
        replenishment_fulfilled_qty=2,
        replenishment_remaining_qty=8,
    )
    db.add(work)
    db.flush()
    db.add(
        models.PlanningTruthState(
            id=1,
            current_generation_id=generation.id,
        )
    )
    db.commit()
    return work, requirement, reservation


def _advance_physical_truth(db):
    pointer = db.get(models.PlanningTruthState, 1)
    parent = db.get(models.LedgerGeneration, pointer.current_generation_id)
    cutoff = parent.cutoff + timedelta(days=1)
    batch = models.PhysicalImportBatch(
        batch_key=f"make-fork-batch-{parent.id}", status="completed", cutoff=cutoff,
    )
    child = models.LedgerGeneration(
        generation_key=f"make-fork-{parent.id}", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, physical_import_batch=batch, algorithm_version="test",
        source_watermarks={"parent_generation_id": parent.id, "generation_kind": "physical_refresh"},
        capabilities=dict(parent.capabilities),
    )
    db.add(child)
    db.flush()
    pointer.current_generation_id = child.id
    db.commit()
    return child


def test_launch_inherited_make_obligation_and_export_after_two_physical_forks(db_session):
    from app.services.mrp_mutation_guard import require_materialized_orders

    work, requirement, reservation = _scope(db_session)
    anchor = work.ledger_generation_id
    child = _advance_physical_truth(db_session)
    request = {work.id: {"launch_qty": 3, "expected_materialized_qty": 0}}
    result = materialize_make_work_items(db_session, [work.id], launch_requests=request)
    assert [row["qty"] for row in result["created"]] == [3.0]
    order = db_session.get(models.ProductionOrder, result["created"][0]["order_id"])
    assert require_materialized_orders(db_session, [order], consumer="test.launch") == child.id
    second_child = _advance_physical_truth(db_session)
    retry = materialize_make_work_items(db_session, [work.id], launch_requests=request)
    assert retry["created"] == []
    assert retry["reused"][0]["product_id"] == result["created"][0]["product_id"]
    assert require_materialized_orders(db_session, [order], consumer="test.launch") == second_child.id
    assert work.ledger_generation_id == reservation.ledger_generation_id == anchor
    assert work.replenishment_remaining_qty == Decimal("8")
    assert requirement.net_required_qty == reservation.replenishment_required_qty == Decimal("10")


def test_launch_after_fork_uses_current_realization_without_rewriting_old_work_item(db_session):
    work, _, reservation = _scope(db_session)
    _advance_physical_truth(db_session)
    reservation.replenishment_received_qty = Decimal("5")
    reservation.realized_qty = Decimal("5")
    db_session.commit()
    too_much = materialize_make_work_items(db_session, [work.id], launch_requests={
        work.id: {"launch_qty": 8, "expected_materialized_qty": 0},
    })
    assert too_much["created"] == []
    assert "доступно к запуску 5" in too_much["errors"][0]
    result = materialize_make_work_items(db_session, [work.id])
    assert [row["qty"] for row in result["created"]] == [4.0, 1.0]
    assert work.replenishment_remaining_qty == Decimal("8")


@pytest.mark.parametrize("mutation", ["not_current", "closed", "wrong_item", "wrong_freeze", "retired_pointer", "foreign_generation"])
def test_launch_inherited_make_obligation_rejects_invalid_owner_before_creating_order(db_session, mutation):
    from app.services.mrp_mutation_guard import MrpMutationLineageError

    work, requirement, reservation = _scope(db_session)
    _advance_physical_truth(db_session)
    if mutation == "not_current":
        reservation.is_current = False
    elif mutation == "closed":
        reservation.lifecycle_status = "closed"
    elif mutation == "wrong_item":
        requirement.item_id += 999
    elif mutation == "wrong_freeze":
        requirement.freeze_version += 1
    elif mutation == "retired_pointer":
        db_session.get(models.PlanningLivePointer, work.plan_id).status = "retired"
    else:
        foreign = models.LedgerGeneration(
            generation_key="foreign-make", status="accepted", cutoff=datetime(2026, 7, 28),
            physical_import_batch_id=db_session.get(models.LedgerGeneration, work.ledger_generation_id).physical_import_batch_id,
            algorithm_version="test", source_watermarks={}, capabilities={},
        )
        db_session.add(foreign)
        db_session.flush()
        work.ledger_generation_id = foreign.id
    db_session.commit()
    with pytest.raises(MrpMutationLineageError):
        materialize_make_work_items(db_session, [work.id])
    assert db_session.query(models.ProductionOrder).count() == 0


def test_materialize_make_work_item_is_idempotent_and_does_not_mutate_truth(db_session):
    work, requirement, reservation = _scope(db_session)

    first = materialize_make_work_items(db_session, [work.id])
    second = materialize_make_work_items(db_session, [work.id])

    assert [row["qty"] for row in first["created"]] == [4.0, 4.0]
    assert second["created"] == []
    assert len(second["reused"]) == 2
    db_session.refresh(work)
    db_session.refresh(requirement)
    db_session.refresh(reservation)
    assert Decimal(work.replenishment_remaining_qty) == Decimal("8")
    assert Decimal(requirement.net_required_qty) == Decimal("10")
    assert Decimal(reservation.replenishment_required_qty) == Decimal("10")


def test_materialize_make_work_items_can_rematerialize_after_full_cancel(db_session):
    work, requirement, reservation = _scope(db_session)

    first = materialize_make_work_items(db_session, [work.id])
    assert [row["qty"] for row in first["created"]] == [4.0, 4.0]

    for row in first["created"]:
        cancel_local_order(db_session, int(row["product_id"]))

    second = materialize_make_work_items(db_session, [work.id])
    assert second["created"][0]["order_number"] == f"MRP-R-{requirement.id}-3"
    assert second["created"][1]["order_number"] == f"MRP-R-{requirement.id}-4"
    assert second["created"][0]["work_item_id"] == work.id
    assert second["created"][1]["work_item_id"] == work.id
    assert second["reused"] == []
    assert [row["qty"] for row in second["created"]] == [4.0, 4.0]
    assert len(second["created"]) == 2

    for row in first["created"]:
        product = db_session.get(models.ProductionProduct, int(row["product_id"]))
        assert product is not None
        order = db_session.get(models.ProductionOrder, int(product.order_id))
        assert order is not None
        assert order.deletion_mark is True

    db_session.refresh(work)
    db_session.refresh(requirement)
    db_session.refresh(reservation)
    assert Decimal(work.replenishment_remaining_qty) == Decimal("8")
    assert Decimal(requirement.net_required_qty) == Decimal("10")
    assert Decimal(reservation.replenishment_required_qty) == Decimal("10")


def test_materialize_make_work_item_uses_operator_launch_qty_and_retries_safely(db_session):
    work, requirement, reservation = _scope(db_session)
    request = {
        work.id: {
            "launch_qty": 3,
            "expected_materialized_qty": 0,
        }
    }

    first = materialize_make_work_items(
        db_session, [work.id], launch_requests=request
    )
    retry = materialize_make_work_items(
        db_session, [work.id], launch_requests=request
    )

    assert [row["qty"] for row in first["created"]] == [3.0]
    assert retry["created"] == []
    assert [row["qty"] for row in retry["reused"]] == [3.0]
    db_session.refresh(work)
    db_session.refresh(requirement)
    db_session.refresh(reservation)
    assert Decimal(work.replenishment_remaining_qty) == Decimal("8")
    assert Decimal(requirement.net_required_qty) == Decimal("10")
    assert Decimal(reservation.replenishment_required_qty) == Decimal("10")


def test_materialize_make_work_item_rejects_qty_above_unlaunched_remainder(db_session):
    work, _requirement, _reservation = _scope(db_session)

    result = materialize_make_work_items(
        db_session,
        [work.id],
        launch_requests={
            work.id: {"launch_qty": 9, "expected_materialized_qty": 0}
        },
    )

    assert result["created"] == []
    assert "доступно к запуску 8" in result["errors"][0]


def test_materialize_reuses_exact_requirement_after_physical_generation_advances(db_session):
    work, requirement, _reservation = _scope(db_session)
    first = materialize_make_work_items(db_session, [work.id])
    first_product_ids = [row["product_id"] for row in first["created"]]

    _advance_physical_truth(db_session)
    second = materialize_make_work_items(db_session, [work.id])

    assert second["created"] == []
    assert [row["product_id"] for row in second["reused"]] == first_product_ids
    assert {row["work_item_id"] for row in second["reused"]} == {work.id}


def test_materialize_counts_previous_mrp_run_of_same_plan_and_retries(db_session):
    from app.services.production_control_journal import _active_open_qty_by_requirement

    work, requirement, _reservation = _scope(db_session)
    first = materialize_make_work_items(db_session, [work.id], launch_requests={
        work.id: {"launch_qty": 3, "expected_materialized_qty": 0},
    })
    current_run = db_session.get(models.PlanningRun, requirement.run_id)
    older_run = models.PlanningRun(status="SUPERSEDED", config_snapshot={},
                                   source_plan_id=current_run.source_plan_id)
    db_session.add(older_run)
    db_session.flush()
    older_requirement = models.MrpRequirement(
        run_id=older_run.run_id, item_id=requirement.item_id,
        total_required_qty=10, net_required_qty=10, bom_level=0, freeze_version=1,
        period_from=requirement.period_from, period_to=requirement.period_to,
    )
    db_session.add(older_requirement)
    db_session.flush()
    old_product = db_session.get(models.ProductionProduct, first["created"][0]["product_id"])
    old_product.source_mrp_requirement_id = older_requirement.id
    db_session.commit()
    scope = {(current_run.source_plan_id, requirement.item_id): requirement.id}
    assert _active_open_qty_by_requirement(db_session, scope)[requirement.id] == 3
    request = {work.id: {"launch_qty": 5, "expected_materialized_qty": 3}}
    result = materialize_make_work_items(db_session, [work.id], launch_requests=request)
    assert result["errors"] == []
    assert sum(row["qty"] for row in result["created"]) == 5
    retry = materialize_make_work_items(db_session, [work.id], launch_requests=request)
    assert retry["errors"] == []
    assert retry["created"] == []
    assert _active_open_qty_by_requirement(db_session, scope)[requirement.id] == 8
    assert old_product.source_mrp_requirement_id == older_requirement.id


@pytest.mark.parametrize("state", ["fully_produced", "done_order", "cancelled_line"])
def test_materialize_ignores_finished_retained_executor_without_changing_its_lifecycle(db_session, state):
    from app.services.production_control_common import DONE_STATE_KEY
    from app.services.production_control_journal import _active_open_qty_by_requirement

    work, requirement, reservation = _scope(db_session)
    first = materialize_make_work_items(db_session, [work.id], launch_requests={
        work.id: {"launch_qty": 3, "expected_materialized_qty": 0},
    })
    product = db_session.get(models.ProductionProduct, first["created"][0]["product_id"])
    order = product.order
    order.order_ref1c = "already-exported-retained-order"
    current_run = db_session.get(models.PlanningRun, requirement.run_id)
    older_run = models.PlanningRun(status="SUPERSEDED", config_snapshot={}, source_plan_id=current_run.source_plan_id)
    db_session.add(older_run)
    db_session.flush()
    older_req = models.MrpRequirement(run_id=older_run.run_id, item_id=requirement.item_id,
        total_required_qty=3, net_required_qty=3, bom_level=0, freeze_version=1,
        period_from=requirement.period_from, period_to=requirement.period_to)
    db_session.add(older_req)
    db_session.flush()
    product.source_mrp_requirement_id = older_req.id
    order.source_run_id = older_run.run_id
    if state == "fully_produced":
        product.produced_qty = 3
        product.remaining_qty = 999  # The compatibility remainder is not truth.
    elif state == "done_order":
        order.order_state_key = DONE_STATE_KEY
    else:
        product.control_state.status = "cancelled"
    db_session.commit()
    frozen = (reservation.reserved_qty, reservation.covered_from_stock_at_freeze_qty,
              reservation.replenishment_required_qty, reservation.replenishment_received_qty)
    old_state = (order.order_state_key, product.produced_qty, product.remaining_qty)
    scope = {(current_run.source_plan_id, requirement.item_id): requirement.id}
    assert _active_open_qty_by_requirement(db_session, scope).get(requirement.id, 0) == 0
    result = materialize_make_work_items(db_session, [work.id])
    assert result["reused"] == []
    assert sum(row["qty"] for row in result["created"]) == 8
    assert old_state == (order.order_state_key, product.produced_qty, product.remaining_qty)
    assert frozen == (reservation.reserved_qty, reservation.covered_from_stock_at_freeze_qty,
                      reservation.replenishment_required_qty, reservation.replenishment_received_qty)


def test_materialize_does_not_count_orders_of_another_plan(db_session):
    work, requirement, _reservation = _scope(db_session)
    first = materialize_make_work_items(db_session, [work.id], launch_requests={
        work.id: {"launch_qty": 3, "expected_materialized_qty": 0},
    })
    plan = models.ProductionPlanHeader(name="Other plan", status="fixed",
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30))
    db_session.add(plan)
    db_session.flush()
    other_run = models.PlanningRun(status="FIXED_SNAPSHOT", config_snapshot={}, source_plan_id=plan.id)
    db_session.add(other_run)
    db_session.flush()
    other_req = models.MrpRequirement(run_id=other_run.run_id, item_id=requirement.item_id,
        total_required_qty=10, net_required_qty=10, bom_level=0, freeze_version=1,
        period_from=plan.period_from, period_to=plan.period_to)
    db_session.add(other_req)
    db_session.flush()
    product = db_session.get(models.ProductionProduct, first["created"][0]["product_id"])
    product.source_mrp_requirement_id = other_req.id
    db_session.commit()
    result = materialize_make_work_items(db_session, [work.id], launch_requests={
        work.id: {"launch_qty": 8, "expected_materialized_qty": 0},
    })
    assert result["errors"] == []
    assert sum(row["qty"] for row in result["created"]) == 8

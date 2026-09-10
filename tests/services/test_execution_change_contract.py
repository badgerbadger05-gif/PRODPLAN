"""Hand-calculated acceptance examples, committed before implementation.

These test the change plan only. Database atomicity/identity and source
acceptance require separate integration tests before a production switch.
"""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from app.services.item_ledger import historical_replay_core as core


def reserve(name, requirement, quantity, *, item=1):
    return core.Reserve(
        reserve_id=name, item_id=item, mode="buy", reserved_qty=D(quantity),
        due_date=date(2026, 9, 30), plan_period_from=date(2026, 9, 1),
        plan_period_to=date(2026, 9, 30), run_id=requirement,
        requirement_id=requirement,
    )


def fact(name, quantity, *, day=1, requirement=None, item=1):
    return core.Fact(
        fact_id=name, item_id=item, mode="buy", qty=D(quantity),
        posting_at=datetime(2026, 9, day, tzinfo=timezone.utc),
        requirement_id=requirement,
    )


def allocation(fact_id, reserve_id, quantity, rule="fifo"):
    return core.Allocation(fact_id, reserve_id, D(quantity), rule, rule == "pegged")


def change_plan(facts, reserves, previous=()):
    planner = getattr(core, "plan_allocation_changes", None)
    assert callable(planner), "current execution change planner is not implemented"
    return planner(facts, reserves, previous_allocations=previous)


def test_addressed_younger_reserve_precedes_older_fifo_and_excess_is_preserved():
    # A needs 8, younger B needs 7; receipt of 18 is explicitly for B.
    plan = change_plan([fact("M1", "18", requirement=2)],
                       [reserve("A", 1, "8"), reserve("B", 2, "7")])
    assert plan.result.allocations == (
        allocation("M1", "B", "7", "pegged"), allocation("M1", "A", "8"),
    )
    assert plan.result.surplus_qty == D("3")
    assert plan.result.fact_qty == D("18")
    assert set(plan.insertions) == set(plan.result.allocations)
    assert plan.updates == plan.deletions == ()


def test_repeated_import_proposes_no_writes_for_one_hundred_retries():
    previous = (allocation("M1", "A", "8", "pegged"), allocation("M1", "B", "2"))
    for _ in range(100):
        plan = change_plan([fact("M1", "10", requirement=1)],
                           [reserve("A", 1, "8"), reserve("B", 2, "7")], previous)
        assert plan.insertions == plan.updates == plan.deletions == ()
        assert plan.result.allocated_qty == D("10")


def test_corrected_earlier_receipt_reassigns_later_receipt_without_touching_other_item():
    previous = (
        allocation("M1-v1", "A", "8", "pegged"), allocation("M1-v1", "B", "2"),
        allocation("M2", "B", "5"), allocation("X", "C", "3"),
    )
    plan = change_plan(
        [fact("M1-v2", "6", requirement=1), fact("M2", "5", day=2), fact("X", "3", item=2)],
        [reserve("A", 1, "8"), reserve("B", 2, "7"), reserve("C", 3, "3", item=2)], previous,
    )
    assert set(plan.result.allocations) == {
        allocation("M1-v2", "A", "6", "pegged"), allocation("M2", "A", "2"),
        allocation("M2", "B", "3"), allocation("X", "C", "3"),
    }
    assert set(plan.insertions) == {allocation("M1-v2", "A", "6", "pegged"), allocation("M2", "A", "2")}
    assert set(plan.deletions) == {allocation("M1-v1", "A", "8", "pegged"), allocation("M1-v1", "B", "2")}
    assert [(u.before, u.after) for u in plan.updates] == [(allocation("M2", "B", "5"), allocation("M2", "B", "3"))]


def test_cancelled_receipt_removes_only_its_assignments_and_recalculates_following_fifo():
    plan = change_plan([fact("M2", "5", day=2)], [reserve("A", 1, "8"), reserve("B", 2, "7")],
                       [allocation("M1", "A", "8"), allocation("M1", "B", "2"), allocation("M2", "B", "5")])
    assert plan.result.allocations == (allocation("M2", "A", "5"),)
    assert [(r.reserve_id, r.realized_qty) for r in plan.result.realizations] == [("A", D("5")), ("B", D("0"))]
    assert len(plan.deletions) == 3
    assert plan.insertions == (allocation("M2", "A", "5"),)


def test_changed_evidence_updates_existing_pair_even_if_quantity_unchanged():
    plan = change_plan([fact("M1", "3", requirement=1)], [reserve("A", 1, "8")], [allocation("M1", "A", "3")])
    assert plan.insertions == plan.deletions == ()
    assert [(u.before, u.after) for u in plan.updates] == [(allocation("M1", "A", "3"), allocation("M1", "A", "3", "pegged"))]


def test_unknown_address_uses_fifo_and_frozen_inputs_are_not_changed():
    reserves = (reserve("A", 1, "8"), reserve("B", 2, "7"))
    before = tuple(replace(r) for r in reserves)
    plan = change_plan([fact("M1", "10", requirement=999)], reserves)
    assert plan.result.allocations == (allocation("M1", "A", "8"), allocation("M1", "B", "2"))
    assert reserves == before


def test_empty_complete_scope_removes_old_assignments():
    previous = (allocation("M1", "A", "4"),)
    plan = change_plan([], [], previous)
    assert plan.deletions == previous
    assert plan.insertions == plan.updates == ()


def test_fractional_quantities_are_exact():
    plan = change_plan([fact("M1", "0.3")], [reserve("A", 1, "0.1"), reserve("B", 2, "0.2")])
    assert plan.result.allocations == (allocation("M1", "A", "0.1"), allocation("M1", "B", "0.2"))
    assert plan.result.surplus_qty == D("0")


def test_order_of_input_rows_does_not_change_change_plan():
    facts = [fact("M2", "5", day=2), fact("M1", "6")]
    reserves = [reserve("B", 2, "7"), reserve("A", 1, "8")]
    previous = [allocation("M2", "B", "5"), allocation("M1", "A", "8")]
    assert change_plan(facts, reserves, previous) == change_plan(reversed(facts), reversed(reserves), reversed(previous))


def test_duplicate_previous_pair_is_rejected_instead_of_silently_losing_quantity():
    with pytest.raises(ValueError, match="duplicate"):
        change_plan([], [], [allocation("M1", "A", "3"), allocation("M1", "A", "4")])


@pytest.mark.parametrize("quantity", ["0", "-1"])
def test_invalid_previous_quantity_is_rejected(quantity):
    with pytest.raises(ValueError, match="positive"):
        change_plan([], [], [allocation("M1", "A", quantity)])


def test_signed_return_requires_canonical_normalization_before_planning():
    with pytest.raises(ValueError, match="normalized"):
        change_plan([fact("return", "-2")], [reserve("A", 1, "8")])

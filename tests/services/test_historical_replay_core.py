from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.services.item_ledger.historical_replay_core import (
    Fact,
    Reserve,
    allocate_historical_facts,
)


def reserve(
    reserve_id: str,
    requirement_id: int,
    *,
    mode: str = "make",
    qty: str = "10",
    due: date = date(2026, 6, 30),
    run_id: int = 1,
    plan_period_from: date | None = None,
    plan_period_to: date | None = None,
    bucket_id: int = 1,
    order_refs: tuple[str, ...] = (),
) -> Reserve:
    _plan_period_from = plan_period_from or due.replace(day=1)
    _plan_period_to = plan_period_to or due
    if _plan_period_to < _plan_period_from:
        raise ValueError("plan_period_to must be >= plan_period_from")
    return Reserve(
        reserve_id=reserve_id,
        item_id=7,
        mode=mode,  # type: ignore[arg-type]
        reserved_qty=Decimal(qty),
        due_date=due,
        plan_period_from=_plan_period_from,
        plan_period_to=_plan_period_to,
        run_id=run_id,
        requirement_id=requirement_id,
        bucket_date=due,
        bucket_id=bucket_id,
        order_refs=order_refs,
    )


def fact(
    fact_id: str,
    qty: str,
    *,
    mode: str = "make",
    requirement_id: int | None = None,
    order_ref: str | None = None,
    posting_at: datetime = datetime(2026, 7, 1, tzinfo=timezone.utc),
) -> Fact:
    return Fact(
        fact_id=fact_id,
        item_id=7,
        mode=mode,  # type: ignore[arg-type]
        qty=Decimal(qty),
        posting_at=posting_at,
        requirement_id=requirement_id,
        order_ref=order_ref,
    )


def test_exact_requirement_wins_before_fifo():
    old = reserve("old", 10, due=date(2026, 5, 31), mode="consume")
    linked = reserve("linked", 20, mode="consume")

    result = allocate_historical_facts(
        [fact("f", "6", requirement_id=20, mode="consume")],
        [old, linked],
    )

    assert [(a.reserve_id, a.qty, a.match_rule) for a in result.allocations] == [
        ("linked", Decimal("6"), "requirement")
    ]


def test_later_consumption_exact_wins_before_earlier_unaddressed_consume_is_unplanned():
    rows = [
        reserve("owned", 20, qty="5", mode="consume"),
        reserve("pool", 10, due=date(2026, 5, 31), qty="5", mode="consume"),
    ]
    early = Fact(
        **{
            **fact("early", "7", mode="consume").__dict__,
            "posting_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        }
    )
    late = fact("late", "5", requirement_id=20, mode="consume")

    result = allocate_historical_facts([early, late], rows)

    assert [(a.fact_id, a.reserve_id, a.match_rule) for a in result.allocations] == [
        ("late", "owned", "requirement")
    ]
    assert result.unplanned[0].reason == "consume_fact_requires_address"
    assert result.unplanned_qty == Decimal("7")


def test_order_link_wins_then_consume_surplus_remains_unplanned():
    old = reserve("old", 10, due=date(2026, 5, 31), qty="4", mode="consume")
    linked = reserve("linked", 20, qty="3", order_refs=("PO-1",), mode="consume")

    result = allocate_historical_facts(
        [fact("f", "6", order_ref="PO-1", mode="consume")],
        [linked, old],
    )

    assert [(a.reserve_id, a.qty, a.match_rule) for a in result.allocations] == [
        ("linked", Decimal("3"), "order"),
    ]
    assert result.unplanned[0].reason == "no_eligible_reserve_capacity"
    assert result.unplanned_qty == Decimal("3")


def test_unaddressed_make_fifo_uses_due_plan_run_requirement_bucket_order():
    rows = [
        reserve("bucket-2", 1, qty="2", bucket_id=2),
        reserve("bucket-1", 1, qty="2", bucket_id=1),
        reserve("later-run", 2, qty="2", run_id=2),
    ]
    result = allocate_historical_facts([fact("f", "5")], reversed(rows))

    assert [(a.reserve_id, a.qty) for a in result.allocations] == [
        ("bucket-1", Decimal("2")),
        ("bucket-2", Decimal("2")),
        ("later-run", Decimal("1")),
    ]


def test_unaddressed_output_next_year_closes_oldest_reserve():
    old_reserve = reserve("old", 1, qty="2", due=date(2026, 12, 31))
    newer_reserve = reserve("new", 2, qty="4", due=date(2027, 1, 31))

    result = allocate_historical_facts(
        [fact("next-year-output", "5", posting_at=datetime(2027, 7, 1, tzinfo=timezone.utc))],
        [newer_reserve, old_reserve],
    )

    assert [(a.reserve_id, a.qty, a.match_rule) for a in result.allocations] == [
        ("old", Decimal("2"), "fifo"),
        ("new", Decimal("3"), "fifo"),
    ]


def test_exact_august_identity_may_apply_only_to_consume():
    august = reserve(
        "august",
        8,
        qty="4",
        due=date(2026, 8, 31),
        order_refs=("PO-AUG",),
        mode="consume",
    )

    by_requirement = allocate_historical_facts(
        [fact("july-owned", "4", requirement_id=8, mode="consume")],
        [august],
    )
    by_order = allocate_historical_facts(
        [fact("july-order", "4", order_ref="PO-AUG", mode="consume")],
        [august],
    )

    assert by_requirement.allocations[0].match_rule == "requirement"
    assert by_order.allocations[0].match_rule == "order"
    assert by_requirement.unplanned_qty == by_order.unplanned_qty == Decimal("0")


def test_make_with_identity_never_pegs_fifo_before_older_underproduction():
    result = allocate_historical_facts(
        [
            fact(
                "june-output",
                "3",
                posting_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
                mode="make",
            ),
            fact(
                "july-output",
                "2",
                posting_at=datetime(2027, 7, 9, tzinfo=timezone.utc),
                requirement_id=13,
                mode="make",
            ),
        ],
        [
            reserve("run14", 14, qty="5", due=date(2026, 7, 31), run_id=14),
            reserve("run13", 13, qty="3", due=date(2026, 6, 30), run_id=13),
        ],
    )

    assert [(a.fact_id, a.reserve_id, a.match_rule) for a in result.allocations] == [
        ("june-output", "run13", "fifo"),
        ("july-output", "run14", "fifo"),
    ]
    assert result.unplanned_qty == Decimal("0")


def test_unaddressed_output_still_uses_oldest_prior_period_reserve():
    may = reserve("may", 5, qty="2", due=date(2026, 5, 31))
    june = reserve("june", 6, qty="2", due=date(2026, 6, 30))
    august = reserve("august", 8, qty="2", due=date(2026, 8, 31))

    result = allocate_historical_facts(
        [fact("july-output", "3")],
        [august, june, may],
    )

    assert [(row.reserve_id, row.qty) for row in result.allocations] == [
        ("may", Decimal("2")),
        ("june", Decimal("1")),
    ]


def test_fifo_ordering_respects_overlapping_plan_periods():
    older_window = reserve(
        "older-window",
        1,
        qty="2",
        due=date(2026, 7, 31),
        plan_period_from=date(2026, 5, 1),
        plan_period_to=date(2026, 6, 30),
        bucket_id=2,
    )
    younger_window = reserve(
        "younger-window",
        1,
        qty="3",
        due=date(2026, 6, 30),
        plan_period_from=date(2026, 6, 1),
        plan_period_to=date(2026, 7, 31),
        bucket_id=1,
    )

    result = allocate_historical_facts(
        [fact("output", "4")],
        [younger_window, older_window],
    )

    assert [(row.reserve_id, row.qty) for row in result.allocations] == [
        ("older-window", Decimal("2")),
        ("younger-window", Decimal("2")),
    ]


def test_unaddressed_consume_is_unplanned_and_never_global_fifo():
    result = allocate_historical_facts(
        [fact("expense", "4", mode="consume")],
        [reserve("consume-r", 1, mode="consume")],
    )

    assert result.allocations == ()
    assert result.unplanned[0].reason == "consume_fact_requires_address"
    assert result.unplanned_qty == Decimal("4")


def test_addressed_consume_can_realize_only_its_reserve():
    result = allocate_historical_facts(
        [fact("expense", "12", mode="consume", requirement_id=2)],
        [
            reserve("other", 1, mode="consume", qty="10"),
            reserve("target", 2, mode="consume", qty="5"),
        ],
    )

    assert [(a.reserve_id, a.qty) for a in result.allocations] == [
        ("target", Decimal("5"))
    ]
    assert result.unplanned_qty == Decimal("7")


def test_conservation_capacity_and_deterministic_idempotence():
    facts = [fact("b", "9"), fact("a", "8")]
    reserves = [reserve("r2", 2, qty="10"), reserve("r1", 1, qty="3")]

    first = allocate_historical_facts(facts, reserves)
    second = allocate_historical_facts(reversed(facts), reversed(reserves))

    assert first == second
    assert first.fact_qty == first.allocated_qty + first.unplanned_qty
    assert all(row.realized_qty <= row.reserved_qty for row in first.realizations)


@pytest.mark.parametrize(
    "bad_fact",
    [
        fact("negative", "-1"),
        fact("zero", "0"),
        Fact(
            fact_id="reversal",
            item_id=7,
            mode="make",
            qty=Decimal("1"),
            posting_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            is_reversal=True,
        ),
    ],
)
def test_signed_or_reversal_facts_require_explicit_normalization(bad_fact):
    with pytest.raises(ValueError, match="normalized explicitly"):
        allocate_historical_facts([bad_fact], [reserve("r", 1)])


def test_duplicate_fact_identity_is_rejected_and_inputs_are_immutable():
    row = fact("same", "1")
    with pytest.raises(ValueError, match="fact_id must be unique"):
        allocate_historical_facts([row, row], [reserve("r", 1)])
    with pytest.raises(FrozenInstanceError):
        row.qty = Decimal("2")  # type: ignore[misc]

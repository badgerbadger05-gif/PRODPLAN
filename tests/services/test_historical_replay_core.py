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
    bucket_id: int = 1,
    order_refs: tuple[str, ...] = (),
) -> Reserve:
    return Reserve(
        reserve_id=reserve_id,
        item_id=7,
        mode=mode,  # type: ignore[arg-type]
        reserved_qty=Decimal(qty),
        due_date=due,
        plan_period_from=due.replace(day=1),
        plan_period_to=due,
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
) -> Fact:
    return Fact(
        fact_id=fact_id,
        item_id=7,
        mode=mode,  # type: ignore[arg-type]
        qty=Decimal(qty),
        posting_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        requirement_id=requirement_id,
        order_ref=order_ref,
    )


def test_exact_requirement_wins_before_fifo():
    old = reserve("old", 10, due=date(2026, 5, 31))
    linked = reserve("linked", 20)

    result = allocate_historical_facts([fact("f", "6", requirement_id=20)], [old, linked])

    assert [(a.reserve_id, a.qty, a.match_rule) for a in result.allocations] == [
        ("linked", Decimal("6"), "requirement")
    ]


def test_later_exact_fact_wins_globally_before_earlier_unaddressed_fifo():
    rows = [
        reserve("owned", 20, qty="5"),
        reserve("pool", 10, due=date(2026, 5, 31), qty="5"),
    ]
    early = Fact(
        **{
            **fact("early", "7").__dict__,
            "posting_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        }
    )
    late = fact("late", "5", requirement_id=20)

    result = allocate_historical_facts([early, late], rows)

    assert [(a.fact_id, a.reserve_id, a.match_rule) for a in result.allocations] == [
        ("late", "owned", "requirement"),
        ("early", "pool", "fifo"),
    ]
    assert result.unplanned_qty == Decimal("2")


def test_order_link_wins_then_make_surplus_uses_canonical_fifo():
    old = reserve("old", 10, due=date(2026, 5, 31), qty="4")
    linked = reserve("linked", 20, qty="3", order_refs=("PO-1",))

    result = allocate_historical_facts([fact("f", "6", order_ref="PO-1")], [linked, old])

    assert [(a.reserve_id, a.qty, a.match_rule) for a in result.allocations] == [
        ("linked", Decimal("3"), "order"),
        ("old", Decimal("3"), "fifo"),
    ]


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

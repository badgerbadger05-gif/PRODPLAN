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
    qty: str,
    *,
    flow: str = "make",
    period_from: date = date(2026, 6, 1),
    run_id: int = 1,
    order_refs: tuple[str, ...] = (),
) -> Reserve:
    return Reserve(
        reserve_id=reserve_id,
        item_id=7,
        mode=flow,  # type: ignore[arg-type]
        reserved_qty=Decimal(qty),
        due_date=date(2026, 6, 30),
        plan_period_from=period_from,
        plan_period_to=date(period_from.year, period_from.month, 28),
        run_id=run_id,
        requirement_id=requirement_id,
        order_refs=order_refs,
        organization_ref="ORG",
        planning_stock_pool="selected",
    )


def fact(
    fact_id: str,
    qty: str,
    *,
    flow: str = "make",
    requirement_id: int | None = None,
    order_ref: str | None = None,
) -> Fact:
    return Fact(
        fact_id=fact_id,
        item_id=7,
        mode=flow,  # type: ignore[arg-type]
        qty=Decimal(qty),
        posting_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        requirement_id=requirement_id,
        order_ref=order_ref,
        organization_ref="ORG",
        planning_stock_pool="selected",
    )


def test_make_replenishment_fills_oldest_reserve_first() -> None:
    result = allocate_historical_facts(
        [fact("receipt", "7")],
        [
            reserve("july", 20, "5", period_from=date(2026, 7, 1), run_id=2),
            reserve("june", 10, "5"),
        ],
    )

    assert [(row.reserve_id, row.qty, row.match_rule) for row in result.allocations] == [
        ("june", Decimal("5"), "fifo"),
        ("july", Decimal("2"), "fifo"),
    ]


def test_exact_address_match_is_pegged_and_excess_continues_fifo() -> None:
    result = allocate_historical_facts(
        [
            fact(
                "receipt",
                "7",
                flow="buy",
                requirement_id=20,
                order_ref="PO-20",
            ),
        ],
        [
            reserve("exact-old", 20, "2", flow="buy", period_from=date(2026, 6, 1), run_id=1),
            reserve("exact-mid", 20, "2", flow="buy", period_from=date(2026, 6, 2), run_id=1),
            reserve(
                "fifo-non-addressed",
                10,
                "4",
                flow="buy",
                period_from=date(2026, 7, 1),
                run_id=3,
            ),
        ],
    )

    assert [(row.reserve_id, row.qty, row.match_rule, row.is_addressed) for row in result.allocations] == [
        ("exact-old", Decimal("2"), "pegged", True),
        ("exact-mid", Decimal("2"), "pegged", True),
        ("fifo-non-addressed", Decimal("3"), "fifo", False),
    ]


def test_unknown_or_ambiguous_identity_uses_global_fifo() -> None:
    result = allocate_historical_facts(
        [fact("receipt", "6", flow="buy", requirement_id=99)],
        [
            reserve("older", 10, "3", flow="buy", period_from=date(2026, 6, 1), run_id=1),
            reserve("newer", 20, "3", flow="buy", period_from=date(2026, 6, 2), run_id=1),
        ],
    )

    assert [(row.reserve_id, row.qty, row.match_rule, row.is_addressed) for row in result.allocations] == [
        ("older", Decimal("3"), "fifo", False),
        ("newer", Decimal("3"), "fifo", False),
    ]


def test_order_reference_matching_multiple_reserves_is_fifo_only() -> None:
    result = allocate_historical_facts(
        [fact("receipt", "4", flow="buy", order_ref="PO-SHARED")],
        [
            reserve("older", 10, "2", flow="buy", order_refs=("PO-SHARED",)),
            reserve(
                "newer",
                20,
                "2",
                flow="buy",
                period_from=date(2026, 7, 1),
                run_id=2,
                order_refs=("PO-SHARED",),
            ),
        ],
    )

    assert [(row.reserve_id, row.match_rule, row.is_addressed) for row in result.allocations] == [
        ("older", "fifo", False),
        ("newer", "fifo", False),
    ]


def test_exact_identity_does_not_disable_fifo_for_excess() -> None:
    result = allocate_historical_facts(
        [fact("receipt", "12", flow="buy", requirement_id=20)],
        [
            reserve("exact", 20, "5", flow="buy", period_from=date(2026, 6, 1), run_id=1),
            reserve("fifo", 10, "10", flow="buy", period_from=date(2026, 6, 2), run_id=2),
        ],
    )

    assert [(row.reserve_id, row.qty, row.match_rule, row.is_addressed) for row in result.allocations] == [
        ("exact", Decimal("5"), "pegged", True),
        ("fifo", Decimal("7"), "fifo", False),
    ]


def test_surplus_after_all_live_demands_is_free_quantity() -> None:
    result = allocate_historical_facts(
        [fact("receipt", "9")],
        [reserve("only", 10, "4")],
    )

    assert result.allocated_qty == Decimal("4")
    assert result.surplus_qty == Decimal("5")
    assert result.surplus[0].reason == "no_live_replenishment_demand"


def test_allocator_is_deterministic_and_does_not_mutate_inputs() -> None:
    facts = [fact("receipt", "3")]
    reserves = [reserve("only", 10, "5")]

    assert allocate_historical_facts(facts, reserves) == allocate_historical_facts(
        facts, reserves
    )
    assert reserves[0].reserved_qty == Decimal("5")


def test_invalid_consume_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported mode"):
        allocate_historical_facts(
            [fact("legacy", "1", flow="consume")],
            [reserve("only", 10, "1")],
        )

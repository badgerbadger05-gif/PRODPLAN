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
        [fact("receipt", "7", requirement_id=20)],
        [
            reserve("july", 20, "5", period_from=date(2026, 7, 1), run_id=2),
            reserve("june", 10, "5"),
        ],
    )

    assert [(row.reserve_id, row.qty, row.match_rule) for row in result.allocations] == [
        ("june", Decimal("5"), "fifo"),
        ("july", Decimal("2"), "fifo"),
    ]


def test_buy_uses_the_same_fifo_and_order_identity_is_only_provenance() -> None:
    result = allocate_historical_facts(
        [fact("receipt", "6", flow="buy", requirement_id=20, order_ref="PO-20")],
        [
            reserve("older", 10, "4", flow="buy"),
            reserve(
                "linked",
                20,
                "4",
                flow="buy",
                period_from=date(2026, 7, 1),
                run_id=2,
            ),
        ],
    )

    assert [(row.reserve_id, row.qty) for row in result.allocations] == [
        ("older", Decimal("4")),
        ("linked", Decimal("2")),
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

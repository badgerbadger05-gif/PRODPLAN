from datetime import date, datetime
from decimal import Decimal

import pytest

from app.services.item_ledger.reservation_consumption_core import (
    Allocation,
    Fact,
    Reserve,
    allocate_consumption_facts,
)


def _reserve(
    reserve_id: str,
    *,
    requirement_id: int,
    run_id: int,
    qty: str,
    baseline_at: str,
    pool: str = "pool/main",
    characteristic_ref: str = "",
    organization_ref: str = "",
    period_from: date = date(2026, 8, 1),
) -> Reserve:
    return Reserve(
        reserve_id=reserve_id,
        requirement_id=requirement_id,
        run_id=run_id,
        reserved_qty=Decimal(qty),
        baseline_at=datetime.fromisoformat(baseline_at),
        plan_period_from=period_from,
        plan_period_to=date(period_from.year, period_from.month, 28),
        item_id=7,
        pool=pool,
        characteristic_ref=characteristic_ref,
        organization_ref=organization_ref,
    )


def _fact(
    fact_id: str,
    qty: str,
    *,
    posting_at: str,
    pool: str = "pool/main",
    requirement_id: int | None = None,
    run_id: int | None = None,
    reservation_id: str | None = None,
    characteristic_ref: str = "",
    organization_ref: str = "",
) -> Fact:
    return Fact(
        fact_id=fact_id,
        item_id=7,
        qty=Decimal(qty),
        posting_at=datetime.fromisoformat(posting_at),
        pool=pool,
        characteristic_ref=characteristic_ref,
        organization_ref=organization_ref,
        requirement_id=requirement_id,
        run_id=run_id,
        reservation_id=reservation_id,
    )


def test_addressed_single_reservation_is_pegged_first_and_fifo_rest_follows():
    result = allocate_consumption_facts(
        [
            _fact(
                "consume-1",
                "8",
                posting_at="2026-08-07T00:00:00+00:00",
                reservation_id="r2",
            )
        ],
        [
            _reserve(
                "r1",
                requirement_id=10,
                run_id=1,
                qty="10",
                baseline_at="2026-08-01T00:00:00+00:00",
            ),
            _reserve(
                "r2",
                requirement_id=20,
                run_id=2,
                qty="2",
                baseline_at="2026-08-01T00:00:00+00:00",
                period_from=date(2026, 8, 2),
            ),
            _reserve(
                "r3",
                requirement_id=30,
                run_id=3,
                qty="10",
                baseline_at="2026-08-01T00:00:00+00:00",
                period_from=date(2026, 8, 3),
            ),
        ],
    )

    assert [Allocation(a.fact_id, a.reserve_id, a.qty, a.match_rule, a.is_addressed) for a in result.allocations] == [
        Allocation("consume-1", "r2", Decimal("2"), "pegged", True),
        Allocation("consume-1", "r1", Decimal("6"), "fifo", False),
    ]
    assert result.surplus_qty == Decimal("0")
    assert result.allocated_qty == Decimal("8")
    assert result.surplus == ()


def test_fact_consumption_can_share_reserve_capacity_across_multiple_facts():
    result = allocate_consumption_facts(
        [
            _fact("first", "6", posting_at="2026-08-04T00:00:00+00:00", run_id=5),
            _fact("second", "6", posting_at="2026-08-05T00:00:00+00:00", run_id=5),
        ],
        [
            _reserve(
                "shared",
                requirement_id=10,
                run_id=5,
                qty="10",
                baseline_at="2026-08-01T00:00:00+00:00",
            ),
        ],
    )

    assert result.realizations[0].realized_qty == Decimal("10")
    assert result.allocated_qty == Decimal("10")
    assert result.surplus_qty == Decimal("2")


def test_ambiguous_address_falls_back_to_fifo_for_entire_fact():
    result = allocate_consumption_facts(
        [
            _fact(
                "consume-ambiguous",
                "6",
                posting_at="2026-08-06T00:00:00+00:00",
                run_id=8,
            )
        ],
        [
            _reserve(
                "r1",
                requirement_id=11,
                run_id=8,
                qty="3",
                baseline_at="2026-08-01T00:00:00+00:00",
            ),
            _reserve(
                "r2",
                requirement_id=12,
                run_id=8,
                qty="2",
                baseline_at="2026-08-01T00:00:00+00:00",
            ),
        ],
    )

    assert [row.match_rule for row in result.allocations] == ["fifo", "fifo"]
    assert all(not row.is_addressed for row in result.allocations)
    assert result.surplus[0].reason == "no_live_replenishment_demand"
    assert result.surplus[0].qty == Decimal("1")


def test_exact_addressed_by_run_and_requirement_id_uses_pegged_even_when_fifo_has_earlier_pool_order():
    result = allocate_consumption_facts(
        [
            _fact(
                "consume-precise",
                "3",
                posting_at="2026-08-06T00:00:00+00:00",
                run_id=9,
                requirement_id=11,
            )
        ],
        [
            _reserve(
                "fifo-a",
                requirement_id=12,
                run_id=9,
                qty="3",
                baseline_at="2026-08-01T00:00:00+00:00",
                period_from=date(2026, 7, 1),
            ),
            _reserve(
                "exact",
                requirement_id=11,
                run_id=9,
                qty="2",
                baseline_at="2026-08-01T00:00:00+00:00",
                period_from=date(2026, 8, 1),
            ),
            _reserve(
                "fifo-b",
                requirement_id=13,
                run_id=10,
                qty="2",
                baseline_at="2026-08-01T00:00:00+00:00",
                period_from=date(2026, 8, 2),
            ),
        ],
    )

    assert result.allocations[0] == Allocation(
        "consume-precise",
        "exact",
        Decimal("2"),
        "pegged",
        True,
    )
    assert result.allocations[1] == Allocation(
        "consume-precise",
        "fifo-a",
        Decimal("1"),
        "fifo",
        False,
    )


def test_posting_before_reserve_baseline_goes_to_surplus_only():
    result = allocate_consumption_facts(
        [
            _fact(
                "consume-old",
                "4",
                posting_at="2026-08-01T00:00:00+00:00",
            )
        ],
        [
            _reserve(
                "r1",
                requirement_id=10,
                run_id=1,
                qty="10",
                baseline_at="2026-08-01T10:00:00+00:00",
            )
        ],
    )

    assert result.allocated_qty == Decimal("0")
    assert result.surplus_qty == Decimal("4")
    assert len(result.allocations) == 0


def test_pool_isolation_and_idempotent_conservation():
    facts = (
        _fact("consume-1", "3", posting_at="2026-08-07T00:00:00+00:00", run_id=1),
        _fact("consume-2", "2", posting_at="2026-08-07T01:00:00+00:00", pool="pool/other"),
    )
    reserves = (
        _reserve("r1", requirement_id=10, run_id=1, qty="10", baseline_at="2026-08-01T00:00:00+00:00"),
        _reserve(
            "r2",
            requirement_id=11,
            run_id=2,
            qty="10",
            baseline_at="2026-08-01T00:00:00+00:00",
            pool="pool/other",
        ),
    )

    first = allocate_consumption_facts(facts, reserves)
    second = allocate_consumption_facts(facts, reserves)

    assert first == second
    assert first.allocated_qty == Decimal("5")
    assert first.surplus_qty == Decimal("0")
    assert first.realizations[0].realized_qty == Decimal("3")
    assert first.realizations[1].realized_qty == Decimal("2")


def test_full_pool_qualifier_prevents_cross_characteristic_or_organization_take():
    result = allocate_consumption_facts(
        [
            _fact(
                "consume", "9", posting_at="2026-08-07T00:00:00+00:00",
                characteristic_ref="char-a", organization_ref="org-a",
            )
        ],
        [
            _reserve("same-char", requirement_id=1, run_id=1, qty="4", baseline_at="2026-08-01T00:00:00+00:00", characteristic_ref="char-a", organization_ref="org-a"),
            _reserve("other-char", requirement_id=2, run_id=2, qty="10", baseline_at="2026-08-01T00:00:00+00:00", characteristic_ref="char-b", organization_ref="org-a"),
            _reserve("other-org", requirement_id=3, run_id=3, qty="10", baseline_at="2026-08-01T00:00:00+00:00", characteristic_ref="char-a", organization_ref="org-b"),
        ],
    )

    assert [(row.reserve_id, row.qty) for row in result.allocations] == [("same-char", Decimal("4"))]
    assert result.surplus_qty == Decimal("5")


def test_dynamic_hold_uses_full_reservation_not_static_replenishment_remainder():
    # The persistence adapter supplies ``ReservationEntry.reserved_qty`` here,
    # never the historical ``replenishment_required_qty`` (e.g. 40 after 60
    # was covered at freeze).  A 100-unit senior hold therefore consumes all
    # 100 before a younger reserve may receive the fact.
    result = allocate_consumption_facts(
        [_fact("consume", "100", posting_at="2026-08-07T00:00:00+00:00")],
        [
            _reserve("senior", requirement_id=1, run_id=1, qty="100", baseline_at="2026-08-01T00:00:00+00:00"),
            _reserve("junior", requirement_id=2, run_id=2, qty="100", baseline_at="2026-08-01T00:00:00+00:00", period_from=date(2026, 8, 2)),
        ],
    )
    assert [(row.reserve_id, row.qty) for row in result.allocations] == [("senior", Decimal("100"))]


@pytest.mark.parametrize(
    "fact_qty,reserve_qty,expected_error",
    [
        ("0", "1", "qty must be positive"),
        ("1", "0", "reserved_qty must be positive"),
        ("-1", "1", "qty must be positive"),
    ],
)
def test_allocator_rejects_non_positive_inputs(
    fact_qty: str,
    reserve_qty: str,
    expected_error: str,
) -> None:
    with pytest.raises(ValueError, match=expected_error):
        allocate_consumption_facts(
            [_fact("bad-fact", fact_qty, posting_at="2026-08-07T00:00:00+00:00")],
            [_reserve("bad", requirement_id=1, run_id=1, qty=reserve_qty, baseline_at="2026-08-01T00:00:00+00:00")],
        )


def test_allocator_rejects_duplicate_fact_ids():
    with pytest.raises(ValueError, match="fact_id must be unique"):
        allocate_consumption_facts(
            [
                _fact("same", "1", posting_at="2026-08-07T00:00:00+00:00"),
                _fact("same", "2", posting_at="2026-08-07T00:00:00+00:00"),
            ],
            [
                _reserve("r1", requirement_id=1, run_id=1, qty="10", baseline_at="2026-08-01T00:00:00+00:00"),
            ],
        )


def test_allocator_rejects_duplicate_reserve_ids():
    with pytest.raises(ValueError, match="reserve_id must be unique"):
        allocate_consumption_facts(
            [_fact("consume", "1", posting_at="2026-08-07T00:00:00+00:00")],
            [
                _reserve("same", requirement_id=1, run_id=1, qty="2", baseline_at="2026-08-01T00:00:00+00:00"),
                _reserve("same", requirement_id=2, run_id=2, qty="2", baseline_at="2026-08-01T00:00:00+00:00"),
            ],
        )

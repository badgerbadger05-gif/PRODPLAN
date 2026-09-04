from datetime import date
from decimal import Decimal

import pytest

from app.services.item_ledger.drum_scheduler import (
    AssemblyRateProfile,
    QueueLine,
    build_drum_plan,
)


def test_readiness_gate_allows_ready_younger_line_to_pass_blocked_old_line():
    older = QueueLine(
        queue_line_id=1,
        plan_id=1,
        plan_line_id=1,
        item_id=10,
        sort_key="001",
        planned_output_qty=Decimal("1"),
        accepted_plan_output_qty=Decimal("0"),
        original_priority=("old",),
        ready_qty=Decimal("0"),
        readiness_status="blocked",
    )
    younger = QueueLine(
        queue_line_id=2,
        plan_id=2,
        plan_line_id=2,
        item_id=20,
        sort_key="002",
        planned_output_qty=Decimal("1"),
        accepted_plan_output_qty=Decimal("0"),
        original_priority=("young",),
        ready_qty=Decimal("1"),
        readiness_status="ready",
    )

    plan = build_drum_plan(
        (older, younger),
        {
            10: (AssemblyRateProfile(resource_id=1, qty_per_capacity=Decimal("1")),),
            20: (AssemblyRateProfile(resource_id=1, qty_per_capacity=Decimal("1")),),
        },
        {},
        schedule_from=date(2026, 9, 3),
        schedule_to=date(2026, 9, 4),
        resource_capacity_by_id={1: Decimal("1")},
    )

    assert [(slot.queue_line_id, slot.slot_date) for slot in plan.slots] == [
        (2, date(2026, 9, 3)),
        (1, date(2026, 9, 4)),
    ]
    assert [slot.readiness_phase for slot in plan.slots] == ["now", "blocked"]
    assert plan.metrics["total_open_qty"] == "2"
    assert plan.metrics["total_slot_qty"] == "2"


def test_readiness_curve_delays_each_increment_until_its_available_date():
    line = QueueLine(
        queue_line_id=1,
        plan_id=1,
        plan_line_id=1,
        item_id=10,
        sort_key="001",
        planned_output_qty=Decimal("3"),
        accepted_plan_output_qty=Decimal("0"),
        original_priority=("old",),
        readiness_status="recoverable",
        readiness_curve=(
            ("now", Decimal("1"), date(2026, 9, 3)),
            ("transfer", Decimal("2"), date(2026, 9, 4)),
            ("kitting", Decimal("2"), date(2026, 9, 4)),
            ("committed", Decimal("2"), date(2026, 9, 4)),
            ("launch", Decimal("3"), date(2026, 9, 7)),
        ),
    )

    plan = build_drum_plan(
        (line,),
        {10: (AssemblyRateProfile(1, Decimal("1")),)},
        {},
        schedule_from=date(2026, 9, 3),
        schedule_to=date(2026, 9, 8),
        resource_capacity_by_id={1: Decimal("1")},
    )

    assert [(slot.readiness_phase, slot.slot_date, slot.slot_qty) for slot in plan.slots] == [
        ("now", date(2026, 9, 3), Decimal("1.000")),
        ("transfer", date(2026, 9, 4), Decimal("1.000")),
        ("launch", date(2026, 9, 7), Decimal("1.000")),
    ]


def _line(line_id: int, qty: str, *, sort_key: str, item_id: int = 1) -> QueueLine:
    return QueueLine(
        queue_line_id=line_id,
        plan_id=1,
        plan_line_id=line_id,
        item_id=item_id,
        sort_key=sort_key,
        planned_output_qty=Decimal(qty),
        accepted_plan_output_qty=Decimal("0"),
        original_priority=(sort_key,),
    )


def test_drum_splits_fifo_and_exposes_horizon_gap() -> None:
    result = build_drum_plan(
        (_line(1, "7", sort_key="a"), _line(2, "6", sort_key="b")),
        {1: (AssemblyRateProfile(10, Decimal("1")),)},
        {date(2026, 7, 27): True, date(2026, 7, 28): True},
        schedule_from=date(2026, 7, 27),
        schedule_to=date(2026, 7, 28),
        resource_capacity_by_id={10: Decimal("5")},
    )

    assert [(row.queue_line_id, row.slot_date, row.slot_qty) for row in result.slots] == [
        (1, date(2026, 7, 27), Decimal("5")),
        (1, date(2026, 7, 28), Decimal("2")),
        (2, date(2026, 7, 28), Decimal("3")),
    ]
    assert [(row.queue_line_id, row.gap_qty) for row in result.gaps] == [
        (2, Decimal("3"))
    ]
    assert result.gaps[0].readiness_phase == "unavailable"
    assert Decimal(result.metrics["total_open_qty"]) == Decimal("13")
    assert Decimal(result.metrics["total_slot_qty"]) == Decimal("10")
    assert Decimal(result.metrics["total_gap_qty"]) == Decimal("3")


def test_drum_does_not_persist_sub_quantum_decimal_capacity_residue() -> None:
    """Repeated division must not turn exhausted capacity into a 0.000 slot."""
    only_day = date(2026, 7, 27)
    lines = tuple(
        _line(index, qty, sort_key=f"{index:02d}")
        for index, qty in enumerate(("1", "2", "3", "5", "7", "1"), start=1)
    )

    result = build_drum_plan(
        lines,
        {1: (AssemblyRateProfile(10, Decimal("2.250")),)},
        {only_day: True},
        schedule_from=only_day,
        schedule_to=only_day,
        resource_capacity_by_id={10: Decimal("8.000")},
    )

    assert all(slot.slot_qty == slot.slot_qty.to_integral_value() for slot in result.slots)
    assert [(gap.queue_line_id, gap.gap_qty) for gap in result.gaps] == [
        (6, Decimal("1.000"))
    ]
    assert Decimal(result.metrics["total_open_qty"]) == Decimal("19")
    assert Decimal(result.metrics["total_slot_qty"]) == Decimal("18")
    assert Decimal(result.metrics["total_gap_qty"]) == Decimal("1")


def test_drum_is_deterministic_and_respects_non_workday() -> None:
    kwargs = dict(
        queue_lines=(_line(2, "4", sort_key="b"), _line(1, "4", sort_key="a")),
        rates_by_item={1: (AssemblyRateProfile(10, Decimal("2")),)},
        calendar_by_date={
            date(2026, 7, 27): False,
            date(2026, 7, 28): True,
        },
        schedule_from=date(2026, 7, 27),
        schedule_to=date(2026, 7, 28),
        resource_capacity_by_id={10: Decimal("4")},
    )
    first = build_drum_plan(**kwargs)
    second = build_drum_plan(**kwargs)

    assert first == second
    assert [row.queue_line_id for row in first.slots] == [1, 2]
    assert {row.slot_date for row in first.slots} == {date(2026, 7, 28)}


def test_shared_resource_books_capacity_units_not_sku_units() -> None:
    """A fast SKU must not eat the slow SKU's capacity by unit count."""
    day = date(2026, 7, 27)
    result = build_drum_plan(
        (
            _line(1, "4", sort_key="a", item_id=1),
            _line(2, "100", sort_key="b", item_id=2),
        ),
        {
            1: (AssemblyRateProfile(10, Decimal("1")),),
            2: (AssemblyRateProfile(10, Decimal("5")),),
        },
        {day: True},
        schedule_from=day,
        schedule_to=day,
        resource_capacity_by_id={10: Decimal("10")},
    )

    by_line = {row.queue_line_id: row.slot_qty for row in result.slots}
    # 4 units of the takt-1 SKU consume 4 capacity units, so 6 capacity units
    # remain and yield 6 * 5 = 30 units of the takt-5 SKU — not 50 - 4 = 46.
    assert by_line == {1: Decimal("4"), 2: Decimal("30")}
    assert [(row.queue_line_id, row.gap_qty) for row in result.gaps] == [
        (2, Decimal("70"))
    ]

    rates = {1: Decimal("1"), 2: Decimal("5")}
    consumed = sum(
        (slot.slot_qty / rates[slot.item_id] for slot in result.slots),
        Decimal("0"),
    )
    assert consumed == Decimal("10")


def test_shared_resource_is_not_starved_by_foreign_sku_units() -> None:
    """A high-takt SKU must not block the rest of the day for a slow SKU."""
    day = date(2026, 7, 27)
    result = build_drum_plan(
        (
            _line(1, "30", sort_key="a", item_id=1),
            _line(2, "20", sort_key="b", item_id=2),
        ),
        {
            1: (AssemblyRateProfile(10, Decimal("10")),),
            2: (AssemblyRateProfile(10, Decimal("1")),),
        },
        {day: True},
        schedule_from=day,
        schedule_to=day,
        resource_capacity_by_id={10: Decimal("10")},
    )

    by_line = {row.queue_line_id: row.slot_qty for row in result.slots}
    # 30 units at takt 10 cost 3 capacity units; 7 capacity units are left, so
    # the takt-1 SKU still gets 7 units instead of being locked out by "30".
    assert by_line == {1: Decimal("30"), 2: Decimal("7")}
    assert [(row.queue_line_id, row.gap_qty) for row in result.gaps] == [
        (2, Decimal("13"))
    ]

    rates = {1: Decimal("10"), 2: Decimal("1")}
    consumed = sum(
        (slot.slot_qty / rates[slot.item_id] for slot in result.slots),
        Decimal("0"),
    )
    assert consumed == Decimal("10")


def test_per_resource_horizon_stops_short_of_the_global_window() -> None:
    first = date(2026, 7, 27)
    second = date(2026, 7, 28)
    result = build_drum_plan(
        (
            _line(1, "8", sort_key="a", item_id=1),
            _line(2, "8", sort_key="b", item_id=2),
        ),
        {
            1: (AssemblyRateProfile(10, Decimal("1")),),
            2: (AssemblyRateProfile(11, Decimal("1")),),
        },
        {first: True, second: True},
        schedule_from=first,
        schedule_to=second,
        resource_capacity_by_id={10: Decimal("5"), 11: Decimal("5")},
        resource_horizon_end_by_id={10: first},
    )

    slots = {(row.queue_line_id, row.slot_date): row.slot_qty for row in result.slots}
    # Resource 10 closes after day one; resource 11 keeps the full window.
    assert slots == {
        (1, first): Decimal("5"),
        (2, first): Decimal("5"),
        (2, second): Decimal("3"),
    }
    assert [(row.queue_line_id, row.gap_date, row.gap_qty) for row in result.gaps] == [
        (1, first, Decimal("3"))
    ]


@pytest.mark.parametrize(
    ("rates", "message"),
    [
        ({}, "missing assembly rate"),
        (
            {
                1: (
                    AssemblyRateProfile(10, Decimal("1")),
                    AssemblyRateProfile(11, Decimal("1")),
                )
            },
            "ambiguous assembly rates",
        ),
    ],
)
def test_drum_fails_closed_for_rate_ambiguity(rates, message) -> None:
    with pytest.raises(ValueError, match=message):
        build_drum_plan(
            (_line(1, "1", sort_key="a"),),
            rates,
            {date(2026, 7, 27): True},
            schedule_from=date(2026, 7, 27),
            schedule_to=date(2026, 7, 27),
            resource_capacity_by_id={10: Decimal("1"), 11: Decimal("1")},
        )


def test_drum_rejects_fractional_finished_assembly_quantity() -> None:
    with pytest.raises(ValueError, match="fractional root quantity"):
        build_drum_plan(
            (_line(1, "1.5", sort_key="a"),),
            {1: (AssemblyRateProfile(10, Decimal("1")),)},
            {date(2026, 7, 27): True},
            schedule_from=date(2026, 7, 27),
            schedule_to=date(2026, 7, 27),
            resource_capacity_by_id={10: Decimal("1")},
        )

from datetime import date
from decimal import Decimal

import pytest

from app.services.item_ledger.drum_scheduler import (
    AssemblyRateProfile,
    QueueLine,
    build_drum_plan,
)


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
    assert Decimal(result.metrics["total_open_qty"]) == Decimal("13")
    assert Decimal(result.metrics["total_slot_qty"]) == Decimal("10")
    assert Decimal(result.metrics["total_gap_qty"]) == Decimal("3")


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

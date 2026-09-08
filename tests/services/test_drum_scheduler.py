from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from dataclasses import replace

import pytest

from app.services.item_ledger.drum_scheduler import (
    AssemblyRateProfile,
    QueueLine,
    build_drum_plan,
)
from app.services.item_ledger.drum_schedule_persistence import (
    _slot_readiness_payload,
)
from app.services.item_ledger.drum_saved_calendar import (
    DrumSavedCalendarError,
    saved_working_days,
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
        assembly_remaining_qty=Decimal("1"),
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
        assembly_remaining_qty=Decimal("1"),
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
    assert plan.gaps == ()
    assert plan.metrics["total_open_qty"] == "2"
    assert plan.metrics["total_slot_qty"] == "2"
    assert plan.metrics["total_gap_qty"] == "0"


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
        assembly_remaining_qty=Decimal("3"),
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


def test_readiness_source_does_not_override_oldest_first_on_same_date():
    day = date(2026, 9, 3)
    older = QueueLine(
        queue_line_id=1,
        plan_id=1,
        plan_line_id=1,
        item_id=10,
        sort_key="001",
        planned_output_qty=Decimal("1"),
        accepted_plan_output_qty=Decimal("0"),
        original_priority=("old",),
        assembly_remaining_qty=Decimal("1"),
        readiness_status="recoverable",
        readiness_curve=(("transfer", Decimal("1"), day),),
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
        assembly_remaining_qty=Decimal("1"),
        readiness_status="ready",
        readiness_curve=(("now", Decimal("1"), day),),
    )

    plan = build_drum_plan(
        (younger, older),
        {
            10: (AssemblyRateProfile(1, Decimal("1")),),
            20: (AssemblyRateProfile(1, Decimal("1")),),
        },
        {day: True},
        schedule_from=day,
        schedule_to=day,
        resource_capacity_by_id={1: Decimal("1")},
    )

    assert [(slot.queue_line_id, slot.readiness_phase) for slot in plan.slots] == [
        (1, "transfer")
    ]
    assert [(gap.queue_line_id, gap.gap_qty) for gap in plan.gaps] == [
        (2, Decimal("1"))
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
        assembly_remaining_qty=Decimal(qty),
        ready_qty=Decimal(qty),
        readiness_status="ready",
    )


def test_fifo_reclaims_later_days_while_younger_work_fills_earlier_days():
    older = replace(_line(1, "2", sort_key="001"),
                    readiness_curve=(("launch", Decimal("2"), date(2026, 9, 8)),))
    younger = _line(2, "3", sort_key="002")
    plan = build_drum_plan(
        (younger, older), {1: (AssemblyRateProfile(10, Decimal("1")),)}, {},
        schedule_from=date(2026, 9, 7), schedule_to=date(2026, 9, 9),
        resource_capacity_by_id={10: Decimal("1")},
    )
    assert sorted((s.slot_date, s.queue_line_id) for s in plan.slots) == [
        (date(2026, 9, 7), 2), (date(2026, 9, 8), 1), (date(2026, 9, 9), 1),
    ]
    assert [(g.queue_line_id, g.gap_qty) for g in plan.gaps] == [(2, Decimal("2"))]


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
    assert result.gaps[0].readiness_phase == "now"
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
    assert first.working_days == (date(2026, 7, 28),)
    assert first.resource_horizon_ends == ()
    assert first.resource_daily_capacities == ((10, Decimal("4")),)
    assert [row.queue_line_id for row in first.slots] == [1, 2]
    assert {row.slot_date for row in first.slots} == {date(2026, 7, 28)}


def test_weekend_cannot_be_overridden_to_working_day() -> None:
    saturday = date(2026, 9, 5)
    monday = date(2026, 9, 7)
    result = build_drum_plan(
        (_line(1, "1", sort_key="a"),),
        {1: (AssemblyRateProfile(10, Decimal("1")),)},
        {saturday: True, monday: True},
        schedule_from=saturday,
        schedule_to=monday,
        resource_capacity_by_id={10: Decimal("1")},
    )

    assert result.working_days == (monday,)
    assert result.slots[0].slot_date == monday


def test_saved_drum_calendar_rejects_weekend_as_working_day() -> None:
    schedule = SimpleNamespace(
        schedule_from=date(2026, 9, 4),
        schedule_to=date(2026, 9, 7),
        working_days=["2026-09-05"],
    )

    with pytest.raises(DrumSavedCalendarError, match="Saturday or Sunday"):
        saved_working_days(schedule)


def test_drum_rejects_resource_horizon_without_a_working_day() -> None:
    saturday = date(2026, 9, 5)
    sunday = date(2026, 9, 6)

    with pytest.raises(ValueError, match="contains no working day"):
        build_drum_plan(
            (_line(1, "1", sort_key="a"),),
            {1: (AssemblyRateProfile(10, Decimal("1")),)},
            {saturday: True, sunday: True},
            schedule_from=saturday,
            schedule_to=sunday,
            resource_capacity_by_id={10: Decimal("1")},
        )


@pytest.mark.parametrize("remaining", ["-1", "NaN"])
def test_drum_rejects_invalid_saved_remaining(remaining: str) -> None:
    line = _line(1, "1", sort_key="a")
    line = QueueLine(
        **{
            **line.__dict__,
            "assembly_remaining_qty": Decimal(remaining),
        }
    )
    with pytest.raises(ValueError, match="invalid saved remaining quantity"):
        build_drum_plan(
            (line,),
            {1: (AssemblyRateProfile(10, Decimal("1")),)},
            {date(2026, 9, 7): True},
            schedule_from=date(2026, 9, 7),
            schedule_to=date(2026, 9, 7),
            resource_capacity_by_id={10: Decimal("1")},
        )


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

    consumed = sum(
        (slot.capacity_load for slot in result.slots),
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

    consumed = sum(
        (slot.capacity_load for slot in result.slots),
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
    assert result.resource_horizon_ends == ((10, first),)
    assert result.resource_daily_capacities == (
        (10, Decimal("5")),
        (11, Decimal("5")),
    )
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


def test_drum_reads_saved_remaining_instead_of_recomputing_plan_delta() -> None:
    line = _line(1, "10", sort_key="a")
    line = QueueLine(
        **{
            **line.__dict__,
            "accepted_plan_output_qty": Decimal("2"),
            "assembly_remaining_qty": Decimal("3"),
            "ready_qty": Decimal("3"),
        }
    )
    result = build_drum_plan(
        (line,),
        {1: (AssemblyRateProfile(10, Decimal("1")),)},
        {date(2026, 7, 27): True},
        schedule_from=date(2026, 7, 27),
        schedule_to=date(2026, 7, 27),
        resource_capacity_by_id={10: Decimal("10")},
    )

    assert result.metrics["total_open_qty"] == "3"
    assert result.slots[0].slot_qty == Decimal("3")


def test_drum_rejects_a_decreasing_readiness_curve_instead_of_clamping_it() -> None:
    line = _line(1, "2", sort_key="a")
    line = QueueLine(
        **{
            **line.__dict__,
            "readiness_curve": (
                ("now", Decimal("2"), date(2026, 7, 27)),
                ("transfer", Decimal("1"), date(2026, 7, 28)),
            ),
        }
    )
    with pytest.raises(ValueError, match="readiness curve decreases"):
        build_drum_plan(
            (line,),
            {1: (AssemblyRateProfile(10, Decimal("1")),)},
            {},
            schedule_from=date(2026, 7, 27),
            schedule_to=date(2026, 7, 28),
            resource_capacity_by_id={10: Decimal("10")},
        )


@pytest.mark.parametrize("cumulative", ["0.5", "NaN", "-1"])
def test_drum_rejects_non_whole_saved_readiness_curve(cumulative: str) -> None:
    line = _line(1, "2", sort_key="a")
    line = QueueLine(
        **{
            **line.__dict__,
            "readiness_curve": (
                ("now", Decimal(cumulative), date(2026, 7, 27)),
            ),
        }
    )

    with pytest.raises(ValueError, match="saved readiness cumulative quantity"):
        build_drum_plan(
            (line,),
            {1: (AssemblyRateProfile(10, Decimal("1")),)},
            {},
            schedule_from=date(2026, 7, 27),
            schedule_to=date(2026, 7, 27),
            resource_capacity_by_id={10: Decimal("10")},
        )


@pytest.mark.parametrize("ready_qty", ["0.5", "NaN", "-1"])
def test_drum_rejects_non_whole_saved_ready_quantity(ready_qty: str) -> None:
    line = _line(1, "2", sort_key="a")
    line = QueueLine(
        **{
            **line.__dict__,
            "ready_qty": Decimal(ready_qty),
            "readiness_curve": (),
        }
    )

    with pytest.raises(ValueError, match="saved ready quantity"):
        build_drum_plan(
            (line,),
            {1: (AssemblyRateProfile(10, Decimal("1")),)},
            {},
            schedule_from=date(2026, 7, 27),
            schedule_to=date(2026, 7, 27),
            resource_capacity_by_id={10: Decimal("10")},
        )


def test_blocked_work_uses_capacity_and_keeps_overflow_on_last_workday() -> None:
    line = _line(1, "2", sort_key="a")
    line = QueueLine(
        **{
            **line.__dict__,
            "ready_qty": Decimal("0"),
            "readiness_status": "blocked",
        }
    )
    result = build_drum_plan(
        (line,),
        {1: (AssemblyRateProfile(10, Decimal("1")),)},
        {},
        schedule_from=date(2026, 9, 4),  # Friday
        schedule_to=date(2026, 9, 6),  # Sunday
        resource_capacity_by_id={10: Decimal("1")},
    )

    assert len(result.slots) == 1
    assert result.slots[0].slot_qty == Decimal("1")
    assert result.slots[0].readiness_phase == "blocked"
    assert result.gaps[0].gap_date == date(2026, 9, 4)
    assert result.gaps[0].readiness_phase == "blocked"


def test_tile_curve_keeps_saved_explanations_for_every_horizon() -> None:
    readiness = SimpleNamespace(
        readiness_curve=[
            {
                "horizon": "now",
                "cumulative_qty": "0",
                "available_date": None,
                "actions": [],
                "required_actions": [],
                "blockers": [{"reason": "HORIZON_DOES_NOT_ALLOW_REPLENISHMENT"}],
            },
            {
                "horizon": "launch",
                "cumulative_qty": "1",
                "available_date": "2026-09-08",
                "actions": [{"action_kind": "make", "item_id": 20, "qty": "1"}],
                "required_actions": [{"action_kind": "buy", "item_id": 30, "qty": "2"}],
                "blockers": [{"reason": "LEAD_TIME_MISSING"}],
            },
        ]
    )

    ready_date, curve, _actions = _slot_readiness_payload(
        readiness,
        "launch",
        Decimal("1"),
    )

    assert ready_date == date(2026, 9, 8)
    assert curve[0]["blockers"][0]["reason"] == "HORIZON_DOES_NOT_ALLOW_REPLENISHMENT"
    assert curve[1]["required_actions"][0]["action_kind"] == "buy"


@pytest.mark.parametrize("batch, expected", [(None, None), (Decimal("3"), Decimal("3"))])
def test_item_optimal_batch_owns_drum_rate_not_legacy_value(db_session, batch, expected):
    from app import models
    from app.services.item_ledger.drum_schedule_persistence import _rates_and_capacity
    item = models.Item(item_code="BATCH-OWNER", item_name="Batch owner", optimal_batch=batch)
    resource = models.ProductionResource(resource_name="Batch capacity", capacity=2)
    db_session.add_all([item, resource])
    db_session.flush()
    db_session.add(models.AssemblyRate(item_id=item.item_id, resource_id=resource.resource_id,
                                      qty_per_capacity=99))
    db_session.flush()
    rates, capacities, _, _ = _rates_and_capacity(db_session, [SimpleNamespace(item_id=item.item_id)])
    if expected is None:
        assert item.item_id not in rates
    else:
        assert rates[item.item_id][0].qty_per_capacity == expected
        assert rates[item.item_id][0].qty_per_capacity * capacities[resource.resource_id] == 6


@pytest.mark.parametrize("created, expected", [
    ("2026-09-08T07:00:00+00:00", date(2026, 9, 8)),
    ("2026-09-07T21:30:00+00:00", date(2026, 9, 8)),
    ("2026-09-08T00:30:00", date(2026, 9, 8)),
])
def test_calendar_uses_new_build_day_in_moscow_with_old_ledger_cutoff(created, expected):
    from datetime import datetime
    from app.services.item_ledger.drum_schedule_persistence import _planning_start
    generation = SimpleNamespace(cutoff=datetime.fromisoformat("2026-09-07T10:29:01+00:00"),
                                 created_at=datetime.fromisoformat(created))
    assert _planning_start(generation) == expected

from datetime import datetime, timezone
from decimal import Decimal

from app.services.item_ledger.assembly_output_core import (
    OutputFact,
    QueueCandidate,
    allocate_output_fact,
)


def _line(line_id: int, qty: str, *, item_id: int = 1):
    return QueueCandidate(10 + line_id, line_id, item_id, Decimal(qty))


def test_exact_never_spills_into_another_plan():
    result = allocate_output_fact(
        OutputFact(100, 1, Decimal("9"), (2,), "exact_plan_line"),
        [_line(1, "3"), _line(2, "4"), _line(3, "5")],
    )
    assert [(row.plan_line_id, row.qty, row.match_rule) for row in result.allocations] == [
        (2, Decimal("4"), "exact"),
    ]
    assert result.surplus_qty == Decimal("5")


def test_missing_exact_uses_caller_fifo_and_keeps_surplus():
    result = allocate_output_fact(
        OutputFact(101, 1, Decimal("8")),
        [_line(1, "2"), _line(2, "3"), _line(3, "0")],
    )
    assert [(row.plan_line_id, row.qty) for row in result.allocations] == [
        (1, Decimal("2")),
        (2, Decimal("3")),
    ]
    assert result.surplus_qty == Decimal("3")


def test_ambiguous_exact_never_allocates():
    result = allocate_output_fact(
        OutputFact(102, 1, Decimal("5"), (1, 2), "planned_order"),
        [_line(1, "5"), _line(2, "5")],
    )
    assert result.decision_status == "ambiguous"
    assert result.allocations == ()
    assert result.surplus_qty == Decimal("5")


def test_other_items_and_exhausted_lines_are_ignored_deterministically():
    fact = OutputFact(103, 1, Decimal("4"))
    rows = [_line(1, "0"), _line(2, "7", item_id=2), _line(3, "4")]
    first = allocate_output_fact(fact, rows)
    second = allocate_output_fact(fact, rows)
    assert first == second
    assert [(row.plan_line_id, row.qty) for row in first.allocations] == [
        (3, Decimal("4")),
    ]


def test_fifo_skips_plan_fixed_after_the_fact():
    fact = OutputFact(
        104,
        1,
        Decimal("5"),
        posting_at=datetime(2026, 7, 5, tzinfo=timezone.utc),
    )
    rows = [
        QueueCandidate(
            11,
            1,
            1,
            Decimal("5"),
            eligible_from=datetime(2026, 7, 6, tzinfo=timezone.utc),
        ),
        QueueCandidate(
            12,
            2,
            1,
            Decimal("5"),
            eligible_from=datetime(2026, 7, 4, tzinfo=timezone.utc),
        ),
    ]

    result = allocate_output_fact(fact, rows)

    assert [(row.plan_line_id, row.qty) for row in result.allocations] == [
        (2, Decimal("5")),
    ]


def test_ambiguous_or_invalid_provenance_never_falls_back_to_fifo():
    rows = [_line(1, "5"), _line(2, "5")]
    ambiguous = allocate_output_fact(
        OutputFact(
            105,
            1,
            Decimal("5"),
            link_kind="exact_plan_line",
            provenance_status="ambiguous",
            provenance_reason="two products",
        ),
        rows,
    )
    invalid = allocate_output_fact(
        OutputFact(
            106,
            1,
            Decimal("5"),
            link_kind="exact_plan_line",
            provenance_status="invalid",
            provenance_reason="stale lineage",
        ),
        rows,
    )

    assert ambiguous.allocations == ()
    assert ambiguous.surplus_qty == Decimal("5")
    assert invalid.allocations == ()
    assert invalid.surplus_qty == Decimal("5")

"""R5 correction/return semantics before current-writer integration.

The expected rows are the final positive basis assignments.  A signed
correction or supplier return is an event that unwinds an existing positive
basis; it is never persisted as a negative allocation row.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.supplier_receipt_allocation import (
    ReceiptFact,
    replay_supplier_receipt_basis,
)


def _reservation(
    reservation_id: int,
    requirement_id: int,
    qty: str,
    *,
    due: int = 1,
    lifecycle_status: str = "active",
    order_ref: str = "order-1",
):
    return models.ReservationEntry(
        id=reservation_id,
        ledger_generation_id=1,
        item_id=10,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        run_id=reservation_id,
        freeze_version=1,
        requirement_id=requirement_id,
        priority_period_from=date(2026, 9, due),
        priority_period_to=date(2026, 9, due),
        realization_mode="buy",
        reserved_qty=Decimal(qty),
        replenishment_required_qty=Decimal(qty),
        replenishment_received_qty=Decimal("0"),
        realized_qty=Decimal("0"),
        lifecycle_status=lifecycle_status,
    )


def _fact(
    sle_id: int,
    qty: str,
    *,
    at: datetime,
    ref: str,
    order_ref: str = "order-1",
    order_line: str = "1",
    correction_ref: str | None = None,
):
    return ReceiptFact(
        sle_id=sle_id,
        posting_at=at,
        signed_qty=Decimal(qty),
        item_id=10,
        supplier_order_ref=order_ref,
        supplier_order_line_no=order_line,
        receipt_ref=ref,
        receipt_line_no="1",
        correction_receipt_ref=correction_ref,
    )


def _basis(result):
    return [
        (int(row.fact.sle_id), int(row.reservation.id), row.qty, row.match_rule)
        for row in result.allocations
    ]


def test_decreasing_m1_replays_shared_pool_and_changes_m2_assignment():
    reservations = {
        10: [
            _reservation(1, 101, "5", due=1),
            _reservation(2, 102, "5", due=2),
        ]
    }
    facts = [
        _fact(1, "5", at=datetime(2026, 9, 1), ref="m1"),
        _fact(2, "5", at=datetime(2026, 9, 2), ref="m2"),
        _fact(
            3,
            "-3",
            at=datetime(2026, 9, 3),
            ref="m1-correction",
            correction_ref="m1",
        ),
    ]

    result = replay_supplier_receipt_basis(facts, reservations)

    assert _basis(result) == [
        (1, 1, Decimal("2"), "fifo"),
        (2, 1, Decimal("3"), "fifo"),
        (2, 2, Decimal("2"), "fifo"),
    ]
    assert result.convergence_boundary == 3


def test_full_cancel_and_repeated_cancel_are_idempotent_and_do_not_leave_negative_rows():
    reservations = {10: [_reservation(1, 101, "5")]}
    facts = [
        _fact(1, "5", at=datetime(2026, 9, 1), ref="m1"),
        _fact(
            2,
            "-5",
            at=datetime(2026, 9, 2),
            ref="cancel-m1",
            correction_ref="m1",
        ),
    ]

    first = replay_supplier_receipt_basis(facts, reservations)
    second = replay_supplier_receipt_basis(facts, reservations)

    assert first.allocations == ()
    assert first.surplus_qty == Decimal("0")
    assert first == second


def test_return_with_exact_original_ref_and_order_line_unwinds_newest_then_global():
    reservations = {
        10: [
            _reservation(1, 101, "2", due=1),
            _reservation(2, 102, "2", due=2),
            _reservation(3, 103, "6", due=3),
        ]
    }
    facts = [
        _fact(1, "2", at=datetime(2026, 9, 1), ref="old", order_ref="order-1"),
        _fact(2, "2", at=datetime(2026, 9, 2), ref="new", order_ref="order-1"),
        _fact(3, "5", at=datetime(2026, 9, 3), ref="other", order_ref="order-2", order_line="2"),
        _fact(4, "-6", at=datetime(2026, 9, 4), ref="return", order_ref="order-1"),
    ]

    result = replay_supplier_receipt_basis(facts, reservations)

    assert _basis(result) == [
        (1, 1, Decimal("2"), "fifo"),
        (3, 3, Decimal("3"), "fifo"),
    ]


def test_closed_reservation_is_not_reopened_by_later_fact_and_decimal_is_exact():
    reservations = {
        10: [
            _reservation(1, 101, "1.125", lifecycle_status="closed"),
            _reservation(2, 102, "2.250", due=2),
        ]
    }
    facts = [
        _fact(1, "1.125", at=datetime(2026, 9, 1), ref="late"),
    ]

    result = replay_supplier_receipt_basis(facts, reservations)

    assert _basis(result) == [(1, 2, Decimal("1.125"), "fifo")]
    assert all(row.qty >= 0 for row in result.allocations)


def test_equal_posting_times_and_delivery_order_have_deterministic_equivalent_result():
    reservations = {
        10: [_reservation(1, 101, "2"), _reservation(2, 102, "2", due=2)]
    }
    first = [
        _fact(2, "2", at=datetime(2026, 9, 1), ref="b"),
        _fact(1, "2", at=datetime(2026, 9, 1), ref="a"),
    ]
    second = list(reversed(first))

    assert replay_supplier_receipt_basis(first, reservations) == replay_supplier_receipt_basis(
        second, reservations
    )


def test_equivalent_correction_sequences_converge_without_arbitrary_row_limit():
    reservations = {10: [_reservation(1, 101, "10")]}
    base = _fact(1, "10", at=datetime(2026, 9, 1), ref="base")
    sequence_a = [
        base,
        _fact(2, "-3", at=datetime(2026, 9, 2), ref="c1", correction_ref="base"),
        _fact(3, "-2", at=datetime(2026, 9, 3), ref="c2", correction_ref="base"),
    ]
    sequence_b = [
        base,
        _fact(4, "-5", at=datetime(2026, 9, 4), ref="c3", correction_ref="base"),
    ]

    assert replay_supplier_receipt_basis(sequence_a, reservations) == replay_supplier_receipt_basis(
        sequence_b, reservations
    )

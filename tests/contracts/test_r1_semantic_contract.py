"""R1 hand-written semantic fixtures.

These cases deliberately exercise the pure allocators, not persistence.  The
last test is the R1 documentation gate: the inventory/contract artifact must
name the same business cases without turning generation or snapshot IDs into
semantic assertions.
"""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app import models
from app.services.item_ledger.historical_replay_core import (
    Allocation,
    Fact,
    Reserve,
    plan_allocation_changes,
)
from app.services.item_ledger.reservation_consumption_core import (
    Fact as ConsumptionFact,
    Reserve as ConsumptionReserve,
    allocate_consumption_facts,
)
from app.services.item_ledger.supplier_receipt_allocation import (
    ReceiptFact,
    allocate_supplier_receipts,
)


def _consumption_reserve(reserve_id: str, requirement_id: int, qty: str, day: int):
    return ConsumptionReserve(
        reserve_id=reserve_id,
        requirement_id=requirement_id,
        run_id=requirement_id,
        reserved_qty=Decimal(qty),
        baseline_at=datetime(2026, 9, 1),
        plan_period_from=date(2026, 9, day),
        plan_period_to=date(2026, 9, day),
        item_id=10,
        pool="main",
    )


def test_r1_address_then_fifo_fixture_is_conservative_and_deterministic():
    result = allocate_consumption_facts(
        [
            ConsumptionFact(
                fact_id="receipt-1",
                item_id=10,
                qty=Decimal("8.125"),
                posting_at=datetime(2026, 9, 3),
                pool="main",
                reservation_id="senior",
            )
        ],
        [
            _consumption_reserve("senior", 20, "2.125", 2),
            _consumption_reserve("older", 10, "10", 1),
        ],
    )

    assert [(row.reserve_id, row.qty, row.match_rule) for row in result.allocations] == [
        ("senior", Decimal("2.125"), "pegged"),
        ("older", Decimal("6.000"), "fifo"),
    ]
    assert result.fact_qty == result.allocated_qty + result.surplus_qty


def test_r1_correction_changes_only_the_named_fact_reserve_pair():
    reserves = [
        Reserve(
            reserve_id="r1",
            item_id=10,
            mode="buy",
            reserved_qty=Decimal("10"),
            due_date=date(2026, 9, 1),
            plan_period_from=date(2026, 9, 1),
            plan_period_to=date(2026, 9, 1),
            run_id=1,
            requirement_id=1,
        ),
        Reserve(
            reserve_id="r2",
            item_id=10,
            mode="buy",
            reserved_qty=Decimal("10"),
            due_date=date(2026, 9, 2),
            plan_period_from=date(2026, 9, 2),
            plan_period_to=date(2026, 9, 2),
            run_id=2,
            requirement_id=2,
        ),
    ]
    original = [
        Fact(
            fact_id="receipt-a",
            item_id=10,
            mode="buy",
            qty=Decimal("4"),
            posting_at=datetime(2026, 9, 1),
            requirement_id=1,
        ),
        Fact(
            fact_id="receipt-b",
            item_id=10,
            mode="buy",
            qty=Decimal("3"),
            posting_at=datetime(2026, 9, 2),
        ),
    ]
    before = plan_allocation_changes(
        original,
        reserves,
        previous_allocations=(),
    )
    corrected = [
        Fact(**{**original[0].__dict__, "qty": Decimal("1")}),
        original[1],
    ]
    change = plan_allocation_changes(
        corrected,
        reserves,
        previous_allocations=before.result.allocations,
    )

    assert change.deletions == ()
    assert [(row.before.fact_id, row.after.qty) for row in change.updates] == [
        ("receipt-a", Decimal("1"))
    ]
    assert [(row.fact_id, row.reserve_id) for row in change.insertions] == []


def test_r1_supplier_return_unwinds_newest_assignment_without_overrelease():
    def reserve(reservation_id: int, requirement_id: int, qty: str):
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
            priority_period_from=date(2026, 9, 1),
            priority_period_to=date(2026, 9, 1),
            realization_mode="buy",
            reserved_qty=Decimal(qty),
            replenishment_required_qty=Decimal(qty),
            replenishment_received_qty=Decimal("0"),
            realized_qty=Decimal("0"),
        )

    def fact(sle_id: int, qty: str):
        return ReceiptFact(
            sle_id=sle_id,
            posting_at=datetime(2026, 9, sle_id),
            signed_qty=Decimal(qty),
            item_id=10,
            supplier_order_ref="order-1",
            supplier_order_line_no="1",
            receipt_ref=f"receipt-{sle_id}",
            receipt_line_no="1",
        )

    allocations, unplanned = allocate_supplier_receipts(
        [fact(1, "3"), fact(2, "2"), fact(3, "-2")],
        {10: [reserve(1, 100, "3"), reserve(2, 200, "2")]},
    )

    assert [(row.reservation.id, row.qty) for row in allocations] == [
        (1, Decimal("3")),
        (2, Decimal("2")),
        (2, Decimal("-2")),
    ]
    assert unplanned == Decimal("0")


def test_r1_artifact_names_semantics_and_separates_technical_lineage():
    artifact = Path(__file__).parents[2] / "docs" / "r1-data-contract-inventory.md"
    text = artifact.read_text(encoding="utf-8")
    for marker in (
        "generation_id",
        "parent_generation_id",
        "snapshot_id",
        "Факт",
        "Frozen",
        "Адресный",
        "Исправление",
        "Возврат",
        "Смена MRP",
        "не является бизнес-идентичностью",
    ):
        assert marker in text

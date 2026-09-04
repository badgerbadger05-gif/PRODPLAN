from datetime import date
from decimal import Decimal

import pytest

from app.services.item_ledger.assembly_readiness_core import (
    FrozenBomEdge,
    ReadinessCurveLine,
    ReadinessSupply,
    ReplenishmentPolicy,
    ReadinessLine,
    allocate_assembly_readiness,
    allocate_readiness_curves,
)


def _line(line_id: int, sort_key: str, qty: str, *norms: tuple[int, str]):
    return ReadinessLine(
        queue_line_id=line_id,
        sort_key=sort_key,
        open_qty=Decimal(qty),
        component_norms=tuple((item_id, Decimal(norm)) for item_id, norm in norms),
    )


def test_blocked_old_line_does_not_hoard_stock_needed_by_ready_younger_line():
    rows = allocate_assembly_readiness(
        (
            _line(1, "001", "1", (10, "1"), (20, "1")),
            _line(2, "002", "1", (10, "1")),
        ),
        {10: Decimal("1"), 20: Decimal("0")},
    )

    assert [(row.queue_line_id, row.status, row.ready_qty) for row in rows] == [
        (1, "blocked", Decimal("0.000")),
        (2, "ready", Decimal("1.000")),
    ]
    assert [row.component_item_id for row in rows[0].blockers] == [20]


def test_partial_line_allocates_only_its_releasable_root_quantity():
    [row] = allocate_assembly_readiness(
        (_line(1, "001", "3", (10, "2")),),
        {10: Decimal("5")},
    )

    assert row.status == "partial"
    assert row.ready_qty == Decimal("2")
    assert row.blockers[0].required_qty == Decimal("2")
    assert row.blockers[0].shortage_qty == Decimal("1.0000")


def test_missing_frozen_direct_components_fails_closed():
    [row] = allocate_assembly_readiness(
        (_line(1, "001", "4"),),
        {},
    )

    assert row.status == "unavailable"
    assert row.ready_qty == 0
    assert row.blockers == ()


def test_curve_distinguishes_point_of_use_from_transferable_stock():
    [row] = allocate_readiness_curves(
        (ReadinessCurveLine(1, "001", 1, 100, Decimal("2"), "assembly"),),
        (FrozenBomEdge(1, 100, 10, Decimal("1")),),
        (
            ReadinessSupply("at-line", 10, Decimal("1"), "now", "assembly"),
            ReadinessSupply("at-store", 10, Decimal("1"), "transfer", "store-3"),
        ),
        (),
        as_of=date(2026, 9, 3),
    )

    assert [point.cumulative_qty for point in row.points] == [
        Decimal("1.000"), Decimal("2.000"), Decimal("2.000"),
        Decimal("2.000"), Decimal("2.000"),
    ]
    assert row.points[1].actions[0].action_kind == "transfer"
    assert row.points[1].actions[0].source_warehouse_ref1c == "store-3"


def test_curve_does_not_let_blocked_old_line_hoard_shared_supply():
    rows = allocate_readiness_curves(
        (
            ReadinessCurveLine(1, "001", 1, 100, Decimal("1"), "assembly"),
            ReadinessCurveLine(2, "002", 1, 200, Decimal("1"), "assembly"),
        ),
        (
            FrozenBomEdge(1, 100, 10, Decimal("1")),
            FrozenBomEdge(1, 100, 20, Decimal("1")),
            FrozenBomEdge(1, 200, 10, Decimal("1")),
        ),
        (ReadinessSupply("shared", 10, Decimal("1"), "now", "assembly"),),
        (),
        as_of=date(2026, 9, 3),
    )

    assert rows[0].points[0].cumulative_qty == Decimal("0.000")
    assert rows[1].points[0].cumulative_qty == Decimal("1.000")


def test_curve_explains_kitting_then_recursive_make_launch():
    [row] = allocate_readiness_curves(
        (ReadinessCurveLine(1, "001", 1, 100, Decimal("1"), "assembly"),),
        (
            FrozenBomEdge(1, 100, 20, Decimal("1")),
            FrozenBomEdge(1, 20, 30, Decimal("2")),
            FrozenBomEdge(1, 30, 40, Decimal("3")),
        ),
        (ReadinessSupply("metal", 40, Decimal("6"), "now", "store"),),
        (
            ReplenishmentPolicy(1, 20, "make", 1, "kitting", 3, "store-3"),
            ReplenishmentPolicy(1, 30, "make", 2, "production", 7, "wip"),
        ),
        as_of=date(2026, 9, 3),
    )

    # Kitting alone cannot make the nested DSE; the full launch horizon can.
    assert row.points[2].cumulative_qty == Decimal("0.000")
    assert row.points[4].cumulative_qty == Decimal("1.000")
    kinds = {action.action_kind for action in row.points[4].actions}
    assert kinds == {"transfer", "make", "kitting"}
    assert row.points[4].available_date == date(2026, 9, 6)


def test_curve_keeps_frozen_bom_versions_separate_between_runs():
    rows = allocate_readiness_curves(
        (
            ReadinessCurveLine(1, "001", 11, 100, Decimal("1"), "assembly"),
            ReadinessCurveLine(2, "002", 22, 100, Decimal("1"), "assembly"),
        ),
        (
            FrozenBomEdge(11, 100, 10, Decimal("1")),
            FrozenBomEdge(22, 100, 20, Decimal("1")),
        ),
        (ReadinessSupply("only-new-spec", 20, Decimal("1"), "now", "assembly"),),
        (),
        as_of=date(2026, 9, 3),
    )

    assert rows[0].points[0].cumulative_qty == Decimal("0.000")
    assert rows[1].points[0].cumulative_qty == Decimal("1.000")


def test_curve_keeps_exact_future_supply_eta_and_run_ownership():
    rows = allocate_readiness_curves(
        (
            ReadinessCurveLine(1, "001", 11, 100, Decimal("1"), "assembly"),
            ReadinessCurveLine(2, "002", 22, 100, Decimal("1"), "assembly"),
        ),
        (
            FrozenBomEdge(11, 100, 10, Decimal("1")),
            FrozenBomEdge(22, 100, 10, Decimal("1")),
        ),
        (
            ReadinessSupply(
                "wip-11", 10, Decimal("1"), "committed", "assembly",
                date(2026, 9, 9), "committed", 11,
            ),
        ),
        (),
        as_of=date(2026, 9, 3),
    )

    assert rows[0].points[3].cumulative_qty == Decimal("1.000")
    assert rows[0].points[3].available_date == date(2026, 9, 9)
    assert rows[0].points[3].actions[0].action_kind == "committed_supply"
    assert rows[1].points[3].cumulative_qty == Decimal("0.000")


def test_curve_preserves_leaf_blocker_and_recursive_path_after_launch_horizon():
    [row] = allocate_readiness_curves(
        (ReadinessCurveLine(1, "001", 1, 100, Decimal("2"), "assembly"),),
        (
            FrozenBomEdge(1, 100, 20, Decimal("1")),
            FrozenBomEdge(1, 20, 30, Decimal("2")),
        ),
        (ReadinessSupply("one-leaf", 30, Decimal("1"), "now", "wip"),),
        (ReplenishmentPolicy(1, 20, "make", 1, "production", 7, "wip"),),
        as_of=date(2026, 9, 3),
    )

    assert row.status == "blocked"
    assert row.points[-1].cumulative_qty == Decimal("0")
    assert len(row.blockers) == 1
    blocker = row.blockers[0]
    assert blocker.item_id == 30
    assert blocker.required_qty == Decimal("4.000")
    assert blocker.available_qty == Decimal("1.000")
    assert blocker.shortage_qty == Decimal("3.000")
    assert blocker.reason == "REPLENISHMENT_POLICY_MISSING"
    assert blocker.destination_warehouse_ref1c == "wip"
    assert blocker.path == (100, 20)


def test_curve_keeps_rework_as_an_explicit_launch_action():
    [row] = allocate_readiness_curves(
        (ReadinessCurveLine(1, "001", 1, 100, Decimal("1"), "assembly"),),
        (
            FrozenBomEdge(1, 100, 20, Decimal("1")),
            FrozenBomEdge(1, 20, 30, Decimal("1")),
        ),
        (ReadinessSupply("blank", 30, Decimal("1"), "now", "rework"),),
        (ReplenishmentPolicy(1, 20, "rework", 1, "production", 9, "rework"),),
        as_of=date(2026, 9, 3),
    )

    assert row.points[-1].cumulative_qty == Decimal("1.000")
    assert "rework" in {action.action_kind for action in row.points[-1].actions}


def test_curve_rejects_fractional_finished_assembly_quantity():
    with pytest.raises(ValueError, match="assembly root quantity must be whole"):
        allocate_readiness_curves(
            (ReadinessCurveLine(1, "001", 1, 100, Decimal("1.5"), "assembly"),),
            (FrozenBomEdge(1, 100, 20, Decimal("1")),),
            (),
            (),
            as_of=date(2026, 9, 3),
        )


def test_curve_exposes_ambiguous_frozen_route_instead_of_missing_warehouse():
    [row] = allocate_readiness_curves(
        (ReadinessCurveLine(1, "001", 1, 100, Decimal("1"), "assembly"),),
        (
            FrozenBomEdge(1, 100, 20, Decimal("1")),
            FrozenBomEdge(1, 20, 30, Decimal("1")),
        ),
        (),
        (
            ReplenishmentPolicy(
                1,
                20,
                "make",
                unavailable_reason="FROZEN_SPEC_AMBIGUOUS",
            ),
        ),
        as_of=date(2026, 9, 3),
    )

    assert row.blockers[0].reason == "FROZEN_SPEC_AMBIGUOUS"

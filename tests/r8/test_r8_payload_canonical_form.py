"""R8: a technical republish of the same result must not grow the change audit.

The hourly physical refresh rebuilds every current execution payload from
scratch.  Two things must therefore hold and are proved here:

* the readiness/drum/journal builders must not depend on the order in which
  their input collections arrive (an unordered query, a ``set`` or a dict is
  not allowed to decide the saved answer), and
* the same quantity must always be written in the same canonical text, so a
  wider ``Decimal`` scale never looks like a new business value.

A real change in a quantity, a status or a reason must still be recorded.
"""

from datetime import date, timedelta
from decimal import Decimal
import random

import pytest

from app import models
from app.services.item_ledger.assembly_readiness_core import (
    FrozenBomEdge,
    ReadinessCurveLine,
    ReadinessSupply,
    ReplenishmentPolicy,
    allocate_readiness_curves,
)
from app.services.item_ledger.assembly_readiness_persistence import _qty
from app.services.item_ledger.current_execution import (
    canonical_quantity_text,
    publish_current_execution_scope,
)
from app.services.item_ledger.drum_scheduler import (
    AssemblyRateProfile,
    QueueLine,
    build_drum_plan,
)
from app.services.production_control_material_availability import _aggregate_coverage


def _readiness_row(identity: str, qty: str, *, status: str = "partial"):
    return {
        "entity_kind": "assembly_readiness",
        "business_identity": identity,
        "scope_key": "assembly:all-live-plans",
        "payload": {
            "plan_line_id": 301,
            "status": status,
            "open_qty": qty,
            "readiness_curve": [
                {
                    "horizon": "launch",
                    "cumulative_qty": qty,
                    "required_actions": [{"item_id": 401, "qty": qty}],
                }
            ],
        },
    }


def _publish(db, rows, revision):
    result = publish_current_execution_scope(
        db,
        source_revision=revision,
        scope_key="assembly:all-live-plans",
        rows=rows,
        entity_kinds=("assembly_readiness",),
    )
    db.flush()
    return result


# --------------------------------------------------------------------------
# Canonical quantity text
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("4", "4.000"),
        ("4.000000", "4.000"),
        ("4.000000000000000000000000", "4.000"),
        (Decimal("4.0000000000000000000000"), "4.000"),
        ("0", "0.000"),
        (Decimal("0.000"), "0.000"),
        (Decimal("1E+2"), "100.000"),
        ("10.5", "10.500"),
        ("-3.20", "-3.200"),
        # A value finer than the canonical Decimal(15,3) scale is never
        # rounded away: the representation is canonical, the number is exact.
        ("0.0000001", "0.0000001"),
    ],
)
def test_r8_canonical_quantity_text_fixes_representation_without_changing_value(value, expected):
    assert canonical_quantity_text(value) == expected
    assert Decimal(canonical_quantity_text(value)) == Decimal(str(value))


def test_r8_readiness_payload_quantities_use_the_canonical_scale():
    # ``needed * norm`` inherits the scale of every frozen norm on the BOM
    # path; the saved payload must not.
    needed = Decimal("1") * Decimal("2.000") * Decimal("2.000") * Decimal("1.000")
    assert str(needed) != "4.000"
    assert _qty(needed) == "4.000"


def test_r8_wider_decimal_scale_is_not_a_business_change(db_session):
    _publish(db_session, [_readiness_row("plan-line:301", "4.000")], "physical:g1")
    row = db_session.query(models.CurrentExecutionRow).one()
    row_id, updated_at = int(row.id), row.updated_at
    payload = dict(row.payload)
    changes = db_session.query(models.CurrentExecutionChange).count()

    result = _publish(
        db_session,
        [_readiness_row("plan-line:301", "4.000000000000000000000000")],
        "physical:g2",
    )

    assert result.idempotent is True
    row = db_session.query(models.CurrentExecutionRow).one()
    assert (int(row.id), row.updated_at) == (row_id, updated_at)
    assert dict(row.payload) == payload
    assert db_session.query(models.CurrentExecutionChange).count() == changes


def test_r8_real_quantity_change_still_writes_exactly_one_change_row(db_session):
    _publish(db_session, [_readiness_row("plan-line:301", "4.000")], "physical:g1")
    changes = db_session.query(models.CurrentExecutionChange).count()

    _publish(db_session, [_readiness_row("plan-line:301", "4.001")], "physical:g2")

    assert db_session.query(models.CurrentExecutionChange).count() == changes + 1
    row = db_session.query(models.CurrentExecutionRow).one()
    assert row.payload["open_qty"] == "4.001"


def test_r8_status_change_at_an_unchanged_quantity_still_writes_a_change_row(db_session):
    _publish(db_session, [_readiness_row("plan-line:301", "4.000")], "physical:g1")
    changes = db_session.query(models.CurrentExecutionChange).count()

    _publish(
        db_session,
        [_readiness_row("plan-line:301", "4.000000", status="blocked")],
        "physical:g2",
    )

    assert db_session.query(models.CurrentExecutionChange).count() == changes + 1
    assert db_session.query(models.CurrentExecutionRow).one().payload["status"] == "blocked"


def test_r8_zero_padded_identity_text_is_never_read_as_a_quantity(db_session):
    """Sort keys and codes carry ordering, not arithmetic: they stay verbatim."""

    def row(sort_key: str):
        payload = _readiness_row("plan-line:301", "4.000")
        payload["payload"]["sort_key"] = sort_key
        payload["payload"]["item_code"] = "00-00000212"
        return payload

    _publish(db_session, [row("0000000301")], "physical:g1")
    changes = db_session.query(models.CurrentExecutionChange).count()

    _publish(db_session, [row("301")], "physical:g2")

    assert db_session.query(models.CurrentExecutionChange).count() == changes + 1


# --------------------------------------------------------------------------
# Order invariance of the builders
# --------------------------------------------------------------------------


def _readiness_case():
    lines = (
        ReadinessCurveLine(1, "001", 7, 100, Decimal("2"), "assembly"),
        ReadinessCurveLine(2, "002", 7, 200, Decimal("3"), "assembly"),
    )
    edges = (
        FrozenBomEdge(7, 100, 10, Decimal("1"), root_item_id=100),
        FrozenBomEdge(7, 100, 20, Decimal("2"), root_item_id=100),
        FrozenBomEdge(7, 100, 30, Decimal("1"), root_item_id=100),
        FrozenBomEdge(7, 200, 20, Decimal("1"), root_item_id=200),
        FrozenBomEdge(7, 200, 40, Decimal("1"), root_item_id=200),
        FrozenBomEdge(7, 40, 10, Decimal("2"), root_item_id=200),
    )
    supplies = (
        ReadinessSupply("stock:assembly:10", 10, Decimal("3"), "now", "assembly"),
        ReadinessSupply("stock:store:10", 10, Decimal("5"), "now", "store"),
        ReadinessSupply("stock:assembly:20", 20, Decimal("1"), "now", "assembly"),
        ReadinessSupply(
            "transfer:store:30", 30, Decimal("4"), "transfer", "store",
            transfer_destination_warehouse_ref1c="assembly",
        ),
        ReadinessSupply(
            "future:40", 40, Decimal("2"), "committed", "assembly",
            available_date=date(2026, 9, 12), confidence="committed",
            source_kind="wip_order", source_ref="WIP-1",
        ),
    )
    policies = (
        ReplenishmentPolicy(
            7, 100, "make", lead_days=1, root_item_id=100,
            output_warehouse_ref1c="assembly", material_warehouse_ref1c="assembly",
        ),
        ReplenishmentPolicy(
            7, 200, "make", lead_days=1, root_item_id=200,
            output_warehouse_ref1c="assembly", material_warehouse_ref1c="assembly",
        ),
        ReplenishmentPolicy(
            7, 40, "make", lead_days=2, root_item_id=200,
            output_warehouse_ref1c="assembly", material_warehouse_ref1c="assembly",
        ),
        ReplenishmentPolicy(7, 10, "buy", lead_days=3, root_item_id=100),
        ReplenishmentPolicy(7, 20, "buy", lead_days=4, root_item_id=100),
        ReplenishmentPolicy(7, 30, "buy", lead_days=5, root_item_id=100),
    )
    return lines, edges, supplies, policies


def test_r8_readiness_curve_is_invariant_to_input_collection_order():
    lines, edges, supplies, policies = _readiness_case()
    expected = allocate_readiness_curves(
        lines, edges, supplies, policies, as_of=date(2026, 9, 10)
    )
    # A meaningful case, otherwise the invariance below would be vacuous.
    assert any(point.blockers or point.required_actions
               for row in expected for point in row.points)

    for seed in range(12):
        rnd = random.Random(seed)
        shuffled = []
        for collection in (lines, edges, supplies, policies):
            values = list(collection)
            rnd.shuffle(values)
            shuffled.append(tuple(values))
        assert allocate_readiness_curves(
            *shuffled, as_of=date(2026, 9, 10)
        ) == expected, f"readiness depends on input order (seed {seed})"


def _drum_case():
    queue_lines = (
        QueueLine(
            queue_line_id=-301, plan_id=1, plan_line_id=301, item_id=100,
            sort_key="2026-09-10|1|301", planned_output_qty=Decimal("6"),
            accepted_plan_output_qty=Decimal("0"), original_priority=("2026-09-10", 1, 301),
            assembly_remaining_qty=Decimal("6"), ready_qty=Decimal("6"),
            readiness_status="ready",
            readiness_curve=(("now", Decimal("6"), date(2026, 9, 10)),),
        ),
        QueueLine(
            queue_line_id=-302, plan_id=1, plan_line_id=302, item_id=200,
            sort_key="2026-09-11|1|302", planned_output_qty=Decimal("5"),
            accepted_plan_output_qty=Decimal("0"), original_priority=("2026-09-11", 1, 302),
            assembly_remaining_qty=Decimal("5"), ready_qty=Decimal("0"),
            readiness_status="partial",
            readiness_curve=(("launch", Decimal("5"), date(2026, 9, 14)),),
        ),
    )
    rates = {
        100: (AssemblyRateProfile(resource_id=9, qty_per_capacity=Decimal("2")),),
        200: (AssemblyRateProfile(resource_id=9, qty_per_capacity=Decimal("2")),),
    }
    calendar = {
        date(2026, 9, 10) + timedelta(days=offset): True for offset in range(8)
    }
    return queue_lines, rates, calendar


def test_r8_drum_plan_is_invariant_to_queue_input_order():
    queue_lines, rates, calendar = _drum_case()
    schedule_from, schedule_to = date(2026, 9, 10), date(2026, 9, 17)
    expected = build_drum_plan(
        queue_lines, rates, calendar,
        schedule_from=schedule_from, schedule_to=schedule_to,
        resource_capacity_by_id={9: Decimal("2")},
        resource_horizon_end_by_id={9: schedule_to},
    )
    assert expected.slots

    for seed in range(12):
        rnd = random.Random(seed)
        values = list(queue_lines)
        rnd.shuffle(values)
        plan = build_drum_plan(
            tuple(values), rates, calendar,
            schedule_from=schedule_from, schedule_to=schedule_to,
            resource_capacity_by_id={9: Decimal("2")},
            resource_horizon_end_by_id={9: schedule_to},
        )
        assert plan.slots == expected.slots, f"drum slots depend on input order (seed {seed})"
        assert plan.gaps == expected.gaps, f"drum gaps depend on input order (seed {seed})"
        assert plan.slot_signature == expected.slot_signature
        assert plan.gap_signature == expected.gap_signature


def test_r8_journal_material_coverage_is_invariant_to_component_order():
    """The journal coverage verdict is a set property of its components."""

    labels = ["ok", "partial", "ok", "ok"]
    expected = _aggregate_coverage(labels)
    assert expected == "partial"
    for seed in range(12):
        shuffled = list(labels)
        random.Random(seed).shuffle(shuffled)
        assert _aggregate_coverage(shuffled) == expected

    blocked = ["ok", "partial", "shortage", "ok"]
    assert _aggregate_coverage(blocked) == "shortage"
    for seed in range(12):
        shuffled = list(blocked)
        random.Random(seed).shuffle(shuffled)
        assert _aggregate_coverage(shuffled) == "shortage"

    # A genuine coverage change is still visible.
    assert _aggregate_coverage(["ok", "ok"]) == "ready"

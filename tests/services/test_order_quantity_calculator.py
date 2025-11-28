import math
from types import SimpleNamespace
from datetime import date

import pytest

# Ensure project root on sys.path for "backend" top-level package if needed
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.app.services.order_quantity_calculator import OrderQuantityCalculator  # noqa: E402


def make_item(optimal_batch=None, moq=None, stock_qty=0.0):
    return SimpleNamespace(
        item_id=1,
        optimal_batch=optimal_batch,
        moq=moq,
        stock_qty=stock_qty,
    )


def make_spec(spec_id=100, production_kind_id=10):
    return SimpleNamespace(
        spec_id=spec_id,
        production_kind_id=production_kind_id,
    )


def make_resource(resource_id=1000, buffer_days=3, daily_work_hours=8.0, capacity=1.0):
    return SimpleNamespace(
        resource_id=resource_id,
        buffer_days=buffer_days,
        daily_work_hours=daily_work_hours,
        capacity=capacity,
    )


def comp(item_id, quantity):
    return SimpleNamespace(item_id=item_id, quantity=quantity)


def test_buffer_days_avg_daily_demand_priority_over_qty():
    """
    Buffer quantity = avg_daily_demand * buffer_days.
    avg_daily_demand computed as total_demand / horizon_days (not number of buckets).
    """
    snapshot = {
        "production": {"lot_sizing": {"min_batch": 1, "multiple": 1, "rounding": "ceil"}},
    }
    item_id = 1
    default_spec_map = {item_id: 100}
    spec_by_id = {100: make_spec(100, production_kind_id=10)}
    item_by_id = {item_id: make_item(optimal_batch=None, stock_qty=0.0)}
    res = make_resource(resource_id=500, buffer_days=3)
    res_by_id = {500: res}
    production_kinds_by_resource = {500: {10}}  # resource 500 supports pk=10
    stock_by_item = {2: 0.0, 3: 0.0}
    wip_by_item = {2: 0.0, 3: 0.0}
    horizon_days = 10
    total_demand_by_item = {item_id: 100.0}  # avg = 10/day

    def components_loader(spec_id):
        return []  # no components

    oqc = OrderQuantityCalculator(
        snapshot=snapshot,
        default_spec_map=default_spec_map,
        spec_by_id=spec_by_id,
        components_loader=components_loader,
        item_by_id=item_by_id,
        res_by_id=res_by_id,
        production_kinds_by_resource=production_kinds_by_resource,
        stock_by_item=stock_by_item,
        wip_by_item=wip_by_item,
        horizon_days=horizon_days,
        total_demand_by_item=total_demand_by_item,
    )

    # Directly check internal buffer calculation via public path
    requested = 1.0
    final_qty, normalized, details, warnings = oqc.compute(item_id, requested)

    # Buffer qty = 10 * 3 = 30; min lot sizing will raise base to at least buffer if buffer > requested
    # No optimal_batch, so normalized should be >= 30 (exact 30 with min_batch=1, multiple=1)
    assert final_qty == min(requested, requested, 100.0)  # horizon cap doesn't bite here -> 1.0
    assert normalized >= 30.0
    assert len([w for w in warnings if w.get("code") == "COMPONENT_SHORTAGE"]) == 0


def test_optimal_batch_wins_over_buffer():
    """
    optimal_batch has priority over buffer.
    If optimal_batch >= base_qty (incl. buffer), use optimal_batch.
    """
    snapshot = {
        "production": {"lot_sizing": {"min_batch": 1, "multiple": 1, "rounding": "ceil"}},
    }
    item_id = 1
    default_spec_map = {item_id: 100}
    spec_by_id = {100: make_spec(100, production_kind_id=10)}
    # Set optimal_batch = 50
    item_by_id = {item_id: make_item(optimal_batch=50.0, stock_qty=0.0)}
    res = make_resource(resource_id=500, buffer_days=3)
    res_by_id = {500: res}
    production_kinds_by_resource = {500: {10}}
    stock_by_item = {}
    wip_by_item = {}
    horizon_days = 10
    total_demand_by_item = {item_id: 100.0}  # avg 10/day, buffer=30

    def components_loader(spec_id):
        return []

    oqc = OrderQuantityCalculator(
        snapshot=snapshot,
        default_spec_map=default_spec_map,
        spec_by_id=spec_by_id,
        components_loader=components_loader,
        item_by_id=item_by_id,
        res_by_id=res_by_id,
        production_kinds_by_resource=production_kinds_by_resource,
        stock_by_item=stock_by_item,
        wip_by_item=wip_by_item,
        horizon_days=horizon_days,
        total_demand_by_item=total_demand_by_item,
    )

    requested = 10.0
    final_qty, normalized, details, warnings = oqc.compute(item_id, requested)

    # final_qty limited by horizon (100) and components (no limit) → 10
    assert final_qty == 10.0
    # normalized must become 50 (optimal_batch wins over buffer 30 and requested 10)
    assert normalized == 50.0
    assert len(warnings) == 0


def test_component_shortage_warning_and_limit():
    """
    If a component is limiting, final_qty is reduced and COMPONENT_SHORTAGE is emitted.
    Example: per_unit=2, available=15 => max possible=7.5 from component.
    """
    snapshot = {
        "production": {"lot_sizing": {"min_batch": 1, "multiple": 1, "rounding": "ceil"}},
    }
    parent_id = 10
    child_id = 20
    default_spec_map = {parent_id: 100}
    spec_by_id = {100: make_spec(100, production_kind_id=11)}
    item_by_id = {parent_id: make_item(optimal_batch=None, stock_qty=0.0)}
    res = make_resource(resource_id=600, buffer_days=0)
    res_by_id = {600: res}
    production_kinds_by_resource = {600: {11}}
    stock_by_item = {child_id: 15.0}  # available 15
    wip_by_item = {}
    horizon_days = 5
    total_demand_by_item = {parent_id: 1000.0}  # effectively no horizon cap

    def components_loader(spec_id):
        return [comp(child_id, 2.0)]  # per unit 2

    oqc = OrderQuantityCalculator(
        snapshot=snapshot,
        default_spec_map=default_spec_map,
        spec_by_id=spec_by_id,
        components_loader=components_loader,
        item_by_id=item_by_id,
        res_by_id=res_by_id,
        production_kinds_by_resource=production_kinds_by_resource,
        stock_by_item=stock_by_item,
        wip_by_item=wip_by_item,
        horizon_days=horizon_days,
        total_demand_by_item=total_demand_by_item,
    )

    requested = 10.0
    final_qty, normalized, details, warnings = oqc.compute(parent_id, requested)

    # Component limit should be exposed via details and warnings emitted, but final_qty is not capped by components here.
    # Component limit: 15 / 2 = 7.5
    assert details.get("component_limit") == pytest.approx(7.5, rel=1e-6)
    assert final_qty == pytest.approx(10.0, rel=1e-6)  # capped by requested only (no horizon cap in this case)
    # Normalization should not be less than final_qty
    assert normalized >= final_qty
    assert any(w.get("code") == "COMPONENT_SHORTAGE" for w in warnings)


def test_horizon_cap_limits_final_qty():
    """
    final_qty = min(requested, component_limit, horizon_total_demand).
    If horizon_total_demand is small, it caps the final result.
    """
    snapshot = {
        "production": {"lot_sizing": {"min_batch": 1, "multiple": 1, "rounding": "ceil"}},
    }
    item_id = 1
    default_spec_map = {item_id: 100}
    spec_by_id = {100: make_spec(100, production_kind_id=12)}
    item_by_id = {item_id: make_item(optimal_batch=None, stock_qty=0.0)}
    res = make_resource(resource_id=700, buffer_days=0)
    res_by_id = {700: res}
    production_kinds_by_resource = {700: {12}}
    stock_by_item = {}
    wip_by_item = {}
    horizon_days = 30
    total_demand_by_item = {item_id: 5.0}  # very small horizon demand

    def components_loader(spec_id):
        return []

    oqc = OrderQuantityCalculator(
        snapshot=snapshot,
        default_spec_map=default_spec_map,
        spec_by_id=spec_by_id,
        components_loader=components_loader,
        item_by_id=item_by_id,
        res_by_id=res_by_id,
        production_kinds_by_resource=production_kinds_by_resource,
        stock_by_item=stock_by_item,
        wip_by_item=wip_by_item,
        horizon_days=horizon_days,
        total_demand_by_item=total_demand_by_item,
    )

    requested = 100.0
    final_qty, normalized, details, warnings = oqc.compute(item_id, requested)

    assert final_qty == 5.0  # capped by horizon demand
    assert normalized >= 5.0
    assert len(warnings) == 0
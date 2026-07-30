from datetime import date

import pytest

from app import models
from app.services.bom_specification_resolver import (
    BomSpecificationResolutionError,
    BomSpecificationResolver,
)
from app.services.mrp_freeze import FreezeSharedPools, FreezeTrace
from app.services.one_c_manufacture_export import _component_rows
from app.services.one_c_production_order_export import _materials_for_spec
from app.services.period_plan_service import _explode_bom_net_first


def _item(db, code: str, *, flow: str = "Производство") -> models.Item:
    row = models.Item(
        item_code=code,
        item_name=code,
        item_ref1c=f"item-ref-{code}",
        replenishment_method=flow,
        unit="шт",
        status="active",
    )
    db.add(row)
    db.flush()
    return row


def _spec(db, ref: str) -> models.Specification:
    row = models.Specification(spec_name=ref, spec_ref1c=ref)
    db.add(row)
    db.flush()
    return row


def _pinned_fixture(db):
    root = _item(db, "PIN-ROOT")
    child = _item(db, "PIN-CHILD")
    pinned_leaf = _item(db, "PIN-LEAF-X", flow="Закупка")
    default_leaf = _item(db, "PIN-LEAF-Y", flow="Закупка")
    root_spec = _spec(db, "spec-root")
    pinned_spec = _spec(db, "spec-child-x")
    default_spec = _spec(db, "spec-child-y")
    db.add_all([
        models.DefaultSpecification(item_id=root.item_id, spec_id=root_spec.spec_id),
        models.DefaultSpecification(item_id=child.item_id, spec_id=default_spec.spec_id),
        models.SpecComponent(
            spec_id=root_spec.spec_id,
            item_id=child.item_id,
            quantity=3,
            component_type="Сборка",
            component_spec_ref1c=pinned_spec.spec_ref1c,
        ),
        models.SpecComponent(
            spec_id=pinned_spec.spec_id,
            item_id=pinned_leaf.item_id,
            quantity=5,
            component_type="Материал",
        ),
        models.SpecComponent(
            spec_id=default_spec.spec_id,
            item_id=default_leaf.item_id,
            quantity=7,
            component_type="Материал",
        ),
    ])
    db.flush()
    return root, child, pinned_leaf, default_leaf, root_spec, pinned_spec


def test_pinned_non_default_child_spec_drives_mrp_walkers_and_exports(db_session):
    (
        root,
        child,
        pinned_leaf,
        default_leaf,
        root_spec,
        pinned_spec,
    ) = _pinned_fixture(db_session)
    pools = FreezeSharedPools(
        stock={},
        stock_initial={},
        wip={},
        supplier={},
    )
    trace = FreezeTrace()

    gross, _net, _levels, _warnings = _explode_bom_net_first(
        db_session,
        {root.item_id: {date(2026, 7, 30): 2.0}},
        shared_pools=pools,
        trace=trace,
        need_date_floor=date(2026, 7, 1),
    )

    assert gross[child.item_id][date(2026, 7, 30)] == 6.0
    assert gross[pinned_leaf.item_id][date(2026, 7, 30)] == 30.0
    assert default_leaf.item_id not in gross
    assert (
        child.item_id,
        pinned_leaf.item_id,
        pinned_spec.spec_id,
        5.0,
    ) in trace.component_norms

    descendants = BomSpecificationResolver(db_session).descendant_ids_by_root(
        [root.item_id]
    )[root.item_id]
    assert pinned_leaf.item_id in descendants
    assert default_leaf.item_id not in descendants

    [order_material] = _materials_for_spec(
        db_session,
        spec_id=root_spec.spec_id,
        order_qty=2,
        reserve_structural_unit_ref1c="warehouse",
    )
    assert order_material.spec_ref1c == pinned_spec.spec_ref1c
    [manufacture_material] = _component_rows(
        db_session,
        None,  # the helper only needs the selected specification
        2,
        root_spec.spec_id,
    )
    assert manufacture_material["Спецификация_Key"] == pinned_spec.spec_ref1c


def test_missing_pinned_child_spec_fails_closed_in_mrp_and_exports(db_session):
    root = _item(db_session, "PIN-BAD-ROOT")
    child = _item(db_session, "PIN-BAD-CHILD")
    root_spec = _spec(db_session, "spec-bad-root")
    db_session.add(
        models.DefaultSpecification(item_id=root.item_id, spec_id=root_spec.spec_id)
    )
    db_session.add(models.SpecComponent(
        spec_id=root_spec.spec_id,
        item_id=child.item_id,
        quantity=1,
        component_type="Сборка",
        component_spec_ref1c="missing-child-spec",
    ))
    db_session.flush()
    pools = FreezeSharedPools(stock={}, stock_initial={}, wip={}, supplier={})

    with pytest.raises(BomSpecificationResolutionError, match="missing pinned"):
        _explode_bom_net_first(
            db_session,
            {root.item_id: {date(2026, 7, 30): 1.0}},
            shared_pools=pools,
            trace=FreezeTrace(),
            need_date_floor=date(2026, 7, 1),
        )
    with pytest.raises(BomSpecificationResolutionError, match="missing pinned"):
        _materials_for_spec(
            db_session,
            spec_id=root_spec.spec_id,
            order_qty=1,
            reserve_structural_unit_ref1c=None,
        )
    with pytest.raises(BomSpecificationResolutionError, match="missing pinned"):
        BomSpecificationResolver(db_session).descendant_ids_by_root([root.item_id])


def test_conflicting_pins_for_aggregated_child_fail_closed(db_session):
    root = _item(db_session, "PIN-CONFLICT-ROOT")
    child = _item(db_session, "PIN-CONFLICT-CHILD")
    root_spec = _spec(db_session, "spec-conflict-root")
    child_x = _spec(db_session, "spec-conflict-x")
    child_y = _spec(db_session, "spec-conflict-y")
    db_session.add(
        models.DefaultSpecification(item_id=root.item_id, spec_id=root_spec.spec_id)
    )
    db_session.add_all([
        models.SpecComponent(
            spec_id=root_spec.spec_id,
            item_id=child.item_id,
            quantity=1,
            component_type="Сборка",
            component_spec_ref1c=child_x.spec_ref1c,
        ),
        models.SpecComponent(
            spec_id=root_spec.spec_id,
            item_id=child.item_id,
            quantity=1,
            component_type="Сборка",
            component_spec_ref1c=child_y.spec_ref1c,
        ),
    ])
    db_session.flush()

    with pytest.raises(
        BomSpecificationResolutionError,
        match="conflicting specification selections",
    ):
        _explode_bom_net_first(
            db_session,
            {root.item_id: {date(2026, 7, 30): 1.0}},
            shared_pools=FreezeSharedPools(
                stock={},
                stock_initial={},
                wip={},
                supplier={},
            ),
            trace=FreezeTrace(),
            need_date_floor=date(2026, 7, 1),
        )

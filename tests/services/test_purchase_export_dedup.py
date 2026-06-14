"""M-4 (residual): planned purchases already exported to 1C must be excluded
from regrouping, so re-exporting the full run cannot create a duplicate
supplier order for an already-exported purchase (Codex finding #3).
"""

import datetime

from app.models import Item, PlannedPurchase, PlanningRun, SyncLink
from app.services.one_c_purchase_order_export import (
    _collect_purchase_groups,
    PURCHASE_ORDER_ENTITY,
)


def _mk_run(db):
    run = PlanningRun(
        status="IN_PROGRESS",
        started_by="t",
        horizon_days=10,
        config_version_id=None,
        config_snapshot={},
        warnings=[],
        kpi={},
        started_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(run)
    db.flush()
    return run


def _mk_purchase(db, run, item, supplier):
    d = datetime.date(2025, 1, 1)
    p = PlannedPurchase(
        run_id=run.run_id, item_id=item.item_id,
        requested_qty=5, planned_qty=5, qty=5,
        need_date=d, order_date=d, lead_time_days=1, bucket_date=d,
        supplier_ref1c=supplier,
    )
    db.add(p)
    db.flush()
    return p


def test_already_exported_purchases_excluded_from_grouping(db_session):
    db = db_session
    run = _mk_run(db)
    a = Item(item_code="PA", item_name="PA", item_article="PA", item_ref1c="item-a", status="active")
    b = Item(item_code="PB", item_name="PB", item_article="PB", item_ref1c="item-b", status="active")
    db.add_all([a, b])
    db.flush()

    pa = _mk_purchase(db, run, a, "supplier-A")
    pb = _mk_purchase(db, run, b, "supplier-B")

    # supplier B's purchase is already exported to 1C
    db.add(SyncLink(
        source_system="PRODPLAN",
        source_doctype="planned_purchase",
        source_id=pb.purchase_id,
        target_system="1C",
        target_entity=PURCHASE_ORDER_ENTITY,
        target_ref_key="ref-b",
        status="success",
    ))
    db.flush()

    groups, missing, already = _collect_purchase_groups(db, run.run_id)

    # Only supplier A is regrouped; B is left as its existing 1C order.
    assert {g.supplier_ref1c for g in groups} == {"supplier-A"}
    assert pb.purchase_id in already
    assert pa.purchase_id not in already


def test_errored_export_link_does_not_exclude(db_session):
    db = db_session
    run = _mk_run(db)
    a = Item(item_code="PA2", item_name="PA2", item_article="PA2", item_ref1c="item-a2", status="active")
    db.add(a)
    db.flush()
    pa = _mk_purchase(db, run, a, "supplier-A")

    # a previous failed attempt must NOT block a retry
    db.add(SyncLink(
        source_system="PRODPLAN",
        source_doctype="planned_purchase",
        source_id=pa.purchase_id,
        target_system="1C",
        target_entity=PURCHASE_ORDER_ENTITY,
        target_ref_key=None,
        status="error",
    ))
    db.flush()

    groups, missing, already = _collect_purchase_groups(db, run.run_id)
    assert {g.supplier_ref1c for g in groups} == {"supplier-A"}
    assert already == []

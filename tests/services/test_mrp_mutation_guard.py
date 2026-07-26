from datetime import date

import pytest

from app import models
from app.services import one_c_purchase_order_export as purchase_exporter
from app.services.planning_truth import PlanningTruthUnavailable


def test_dry_run_is_blocked_before_network_when_truth_unavailable(
    db_session, monkeypatch
):
    item = models.Item(
        item_code="guard-purchase-unavailable",
        item_name="Guard purchase",
        item_ref1c="ITEM-REF",
        supplier_ref1c="SUPPLIER-REF",
        status="active",
    )
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        active_freeze_version=1,
    )
    db_session.add_all([item, run])
    db_session.flush()
    purchase = models.PlannedPurchase(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=1,
        planned_qty=1,
        qty=1,
        need_date=date(2026, 7, 30),
        order_date=date(2026, 7, 23),
        lead_time_days=7,
        priority_index=1,
        bucket_date=date(2026, 7, 30),
    )
    db_session.add(purchase)
    db_session.commit()

    network_called = False

    def network_bomb(*_args, **_kwargs):
        nonlocal network_called
        network_called = True
        raise AssertionError("network client must not be constructed")

    monkeypatch.setattr(purchase_exporter, "_create_odata_client", network_bomb)
    with pytest.raises(PlanningTruthUnavailable):
        purchase_exporter.export_planned_purchases_to_1c(
            db_session,
            run.run_id,
            purchase_ids=[purchase.purchase_id],
            dry_run=True,
        )

    assert network_called is False
    assert db_session.query(models.SyncLink).count() == 0
    assert db_session.query(models.PurchaseExportLineAllocation).count() == 0

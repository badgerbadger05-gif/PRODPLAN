import asyncio

import pytest
from fastapi import HTTPException

from app.routers import plan


@pytest.mark.parametrize(
    ("endpoint", "args", "operation"),
    [
        (plan.start_planning_run, (plan.CalcRequest(),), "calc"),
        (plan.calc_preview, (plan.CalcRequest(),), "calc_preview"),
        (plan.calc_gross, (plan.CalcRequest(),), "calc_gross"),
        (
            plan.refreeze_mrp_snapshots,
            (plan.RefreezeRequest(),),
            "mrp_snapshot_refreeze",
        ),
        (plan.reconcile_active_snapshots, (plan.ReconcileRequest(),), "reconcile"),
        (
            plan.reconcile_single_snapshot,
            (123, plan.ReconcileRequest()),
            "run_reconcile",
        ),
    ],
)
def test_public_legacy_live_mrp_endpoints_are_gone(
    endpoint, args, operation
):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(*args, db=object()))

    assert exc.value.status_code == 410
    assert exc.value.detail["code"] == "legacy_live_mrp_retired"
    assert exc.value.detail["operation"] == operation
    assert exc.value.detail["truth_status"] == "unavailable"


def test_retired_worker_never_opens_network():
    import reconcile_worker

    assert "urllib" not in reconcile_worker.__dict__
    assert reconcile_worker.main() == 0

"""Schema-primitives test for the fixed-MRP execution ledger (PHASE 1).

Verifies the new additive columns exist and default correctly. No behavior
is exercised here — only defaults applied by create_all + insert.
"""

import datetime

from app import models


def test_mrp_requirement_ledger_defaults(db_session):
    item = models.Item(item_code="LEDGER-TEST", item_name="Ledger Test")
    db_session.add(item)
    db_session.flush()

    run = models.PlanningRun(config_snapshot={})
    db_session.add(run)
    db_session.flush()

    req = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        period_from=datetime.date(2026, 1, 1),
        period_to=datetime.date(2026, 1, 31),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)

    assert float(req.executed_qty) == 0
    assert float(req.carried_remaining) == 0
    assert req.status == "open"
    assert req.closed_at is None
    assert req.initial_snapshot_stock is None


def test_planning_run_prior_run_defaults(db_session):
    run = models.PlanningRun(config_snapshot={})
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    assert run.prior_run_id is None
    assert run.prior_run is None

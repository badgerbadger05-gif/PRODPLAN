"""Real PostgreSQL regressions: operator commands win over background builds."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.services.accepted_command_mutation import (
    AcceptedPublicationBusy, lock_publication_epoch,
    record_accepted_production_mutation,
)
from app.services.specification_mrp_rebase import rebase_fixed_plan_remaining_roots
from tests.services.test_reservation_current_prepare_install import postgres_reservation_session


def _insert_order(engine, run_id, number):
    with Session(engine) as command:
        command.execute(text("SET LOCAL lock_timeout='1s'"))
        command.query(models.PlanningTruthState).filter_by(id=1).with_for_update().one()
        command.add(models.ProductionOrder(
            order_number=number, source="mrp", source_run_id=run_id,
            order_date=datetime.now(timezone.utc),
        ))
        command.commit()


def test_postgres_order_commits_during_rebase_preparation(postgres_reservation_session, monkeypatch):
    from tests.services.test_specification_mrp_rebase import _world, _stub_publication
    from app.services import specification_mrp_rebase as rebase
    from app.services import obligation_refresh_orchestrator as orchestrator

    db = postgres_reservation_session
    _generation, _plan, run, _line = _world(db, accepted_qty=Decimal("8"))
    run_id = int(run.run_id)
    monkeypatch.setattr(rebase, "live_plan_run_ids", lambda *_: [run_id])
    _stub_publication(monkeypatch, db)
    publish = orchestrator.run_obligation_refresh

    def paused_build(*args, **kwargs):
        db.flush()  # Include the real wrapper's closed-snapshot FK writes.
        _insert_order(db.get_bind(), run_id, "DURING-REBASE")
        return publish(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "run_obligation_refresh", paused_build)
    assert rebase_fixed_plan_remaining_roots(db, run_id)["status"] == "rebased"
    assert db.query(models.ProductionOrder).filter_by(order_number="DURING-REBASE").count() == 1


def test_postgres_reconciliation_does_not_lock_joined_source_run(postgres_reservation_session):
    from tests.services.test_local_mrp_order_reconciliation import _scenario
    from app.services.local_mrp_order_reconciliation import reconcile_local_mrp_orders

    db = postgres_reservation_session
    generation, live, order, product = _scenario(
        db, order_qty=Decimal("10"), target_qty=Decimal("5"), order_ref1c="exported-order",
    )
    source_run_id = int(order.source_run_id)
    db.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    db.commit()
    reconcile_local_mrp_orders(db, ledger_generation_id=generation.id, live_run_ids=[live.run_id])
    _insert_order(db.get_bind(), source_run_id, "DURING-RECONCILIATION")
    db.rollback()


def test_postgres_final_publication_yields_to_command_and_rejects_stale_epoch(postgres_reservation_session):
    from tests.services.test_accepted_command_mutation import _truth

    db = postgres_reservation_session
    parent_id = _truth(db).id
    target = SimpleNamespace(source_watermarks={"accepted_production_mutation_epoch": 0})
    with Session(db.get_bind()) as command:
        command.query(models.PlanningTruthState).filter_by(id=1).with_for_update().one()
        with pytest.raises(AcceptedPublicationBusy):
            lock_publication_epoch(db, generation=target, expected_parent_id=parent_id, nowait=True)
        db.rollback()
        record_accepted_production_mutation(command, mutation_kind="executor", product_ids=[9])
        command.commit()
    with pytest.raises(RuntimeError, match="mutations changed"):
        lock_publication_epoch(db, generation=target, expected_parent_id=parent_id, nowait=True)
    db.rollback()
    target.source_watermarks["accepted_production_mutation_epoch"] = 1
    _pointer, _state, epoch = lock_publication_epoch(
        db, generation=target, expected_parent_id=parent_id, nowait=True,
    )
    assert epoch == 1
    db.rollback()

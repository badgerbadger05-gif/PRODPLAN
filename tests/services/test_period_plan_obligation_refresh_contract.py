"""Period-plan API/service contract for immutable Ledger snapshot publication."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app import models
from app.services import period_plan_service as service


def _truth_and_plan(db):
    cutoff = datetime(2026, 7, 23, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="period-refresh-physical",
        status="completed",
        cutoff=cutoff,
        source_watermarks={},
        completed_at=cutoff,
    )
    generation = models.LedgerGeneration(
        generation_key="period-refresh-parent",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        algorithm_version="test",
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=physical,
    )
    item = models.Item(item_code="PERIOD-REFRESH", item_name="period refresh")
    db.add_all([physical, generation, item])
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    plan = models.ProductionPlanHeader(
        name="August", status="fixed",
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
    )
    db.add(plan)
    db.flush()
    db.add(models.ProductionPlanLine(
        plan_id=plan.id, item_id=item.item_id,
        bucket_date=plan.period_from, qty=10,
    ))
    db.commit()
    return generation, plan


def test_snapshot_requires_explicit_generation_key(db_session):
    _generation, plan = _truth_and_plan(db_session)
    with pytest.raises(ValueError, match="generation_key is required"):
        service.create_mrp_snapshot_from_period_plan(
            db_session, plan.id, generation_key=""
        )


def test_snapshot_uses_current_truth_and_returns_published_plan_candidate(
    db_session, monkeypatch
):
    generation, plan = _truth_and_plan(db_session)
    published = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        ledger_generation_id=generation.id,
        config_snapshot={},
        started_at=datetime.now(timezone.utc),
        pinned=True,
    )
    db_session.add(published)
    db_session.flush()
    observed = {}

    def fake_refresh(db, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(
            target_generation_id=generation.id,
            candidate_run_ids=(published.run_id,),
            published=True,
        )

    monkeypatch.setattr(
        "app.services.obligation_refresh_orchestrator.run_obligation_refresh",
        fake_refresh,
    )
    result = service.create_mrp_snapshot_from_period_plan(
        db_session,
        plan.id,
        generation_key="period-refresh-1",
        started_by="test",
    )

    assert observed == {}
    assert result == {
        "status": "ok",
        "generation_key": "period-refresh-1",
        "ledger_generation_id": generation.id,
        "run_id": published.run_id,
        "published": False,
        "immutable": True,
    }


def test_run_list_and_delete_guard_use_only_exact_current_published_truth(db_session):
    current, plan = _truth_and_plan(db_session)
    old = models.LedgerGeneration(
        generation_key="period-refresh-old",
        status="accepted",
        cutoff=current.cutoff,
        accepted_at=current.accepted_at,
        algorithm_version="test",
        source_watermarks={},
        capabilities={},
        physical_import_batch_id=current.physical_import_batch_id,
    )
    db_session.add(old)
    db_session.flush()
    current_run = models.PlanningRun(
        status="FIXED_SNAPSHOT", source_plan_id=plan.id,
        ledger_generation_id=current.id, config_snapshot={}, pinned=True,
    )
    stale_run = models.PlanningRun(
        status="SUPERSEDED", source_plan_id=plan.id,
        ledger_generation_id=old.id, config_snapshot={}, pinned=True,
    )
    legacy_success = models.PlanningRun(
        status="SUCCESS", source_plan_id=plan.id,
        ledger_generation_id=current.id, config_snapshot={},
    )
    db_session.add_all([current_run, stale_run, legacy_success])
    db_session.commit()

    assert [row["run_id"] for row in service.list_mrp_runs_for_plan(
        db_session, plan.id
    )["rows"]] == [current_run.run_id]
    with pytest.raises(ValueError, match="зафиксированных расчётов"):
        service.delete_period_plan(db_session, plan.id)


def test_delete_guard_also_preserves_historical_snapshot_lineage(db_session):
    current, plan = _truth_and_plan(db_session)
    historical = models.LedgerGeneration(
        generation_key="period-refresh-history-only",
        status="accepted",
        cutoff=current.cutoff,
        accepted_at=current.accepted_at,
        algorithm_version="test",
        source_watermarks={},
        capabilities={},
        physical_import_batch_id=current.physical_import_batch_id,
    )
    db_session.add(historical)
    db_session.flush()
    db_session.add(models.PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        ledger_generation_id=historical.id,
        config_snapshot={},
        pinned=True,
    ))
    db_session.commit()

    with pytest.raises(ValueError, match="зафиксированных расчётов"):
        service.delete_period_plan(db_session, plan.id)

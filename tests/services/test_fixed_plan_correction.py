from datetime import date, datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from app import models
from app.services.fixed_plan_correction import (
    FixedPlanCorrectionError,
    correct_fixed_plan_groups,
)
from app.services.period_plan_service import _saved_plan_output_validation_error
from app.services.item_ledger.current_execution import publish_current_execution_scope


CUTOFF = datetime(2026, 1, 1, tzinfo=timezone.utc)
REVISION = "accepted:g1:period_plan_execution"
SOURCE_HASH = "a" * 64


def _world(db):
    physical = models.PhysicalImportBatch(
        batch_key="plan-correction-physical",
        status="completed",
        cutoff=CUTOFF,
        completed_at=CUTOFF,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="plan-correction-parent",
        status="accepted",
        cutoff=CUTOFF,
        accepted_at=CUTOFF,
        source_watermarks={},
        capabilities={},
        physical_import_batch=physical,
        algorithm_version="test",
    )
    items = [
        models.Item(item_code=f"CORR-{value}", item_name=f"Item {value}", status="active")
        for value in (1, 2, 3)
    ]
    db.add_all([physical, generation, *items])
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=int(generation.id)))

    plans = []
    runs = {}
    lines = {}
    definitions = (
        (1, date(2026, 1, 1), date(2026, 1, 31), items[0], "5", "4"),
        (2, date(2026, 1, 1), date(2026, 1, 31), items[0], "5", "3"),
        (3, date(2026, 3, 1), date(2026, 3, 31), items[1], "4", "2"),
        (4, date(2026, 4, 1), date(2026, 4, 30), items[2], "7", "0"),
    )
    for identity, period_from, period_to, item, qty, accepted in definitions:
        plan = models.ProductionPlanHeader(
            id=identity,
            name=f"Plan {identity}",
            period_from=period_from,
            period_to=period_to,
            status="fixed",
            fixed_at=CUTOFF,
        )
        db.add(plan)
        db.flush()
        run = models.PlanningRun(
            source_plan_id=identity,
            status="FIXED_SNAPSHOT",
            ledger_generation_id=int(generation.id),
            ledger_cutoff=CUTOFF,
            period_from=period_from,
            period_to=period_to,
            fixed_at=CUTOFF,
            config_snapshot={"version": 1},
            horizon_days=30,
            pinned=True,
        )
        db.add(run)
        db.flush()
        db.add(models.PlanningLivePointer(
            plan_id=identity,
            run_id=int(run.run_id),
            status="active",
        ))
        line = models.ProductionPlanLine(
            plan_id=identity,
            item_id=int(item.item_id),
            bucket_date=period_from,
            qty=Decimal(qty),
            accepted_output_qty=Decimal(accepted),
            remaining_output_qty=max(Decimal(qty) - Decimal(accepted), Decimal("0")),
            locked_by_run_id=int(run.run_id),
        )
        db.add(line)
        db.flush()
        db.add(models.MrpRunRoot(
            run_id=int(run.run_id),
            plan_line_id=int(line.id),
            planned_qty=Decimal(qty),
            accepted_qty=Decimal(accepted),
            remaining_qty=max(Decimal(qty) - Decimal(accepted), Decimal("0")),
        ))
        plans.append(plan)
        runs[identity] = run
        lines[identity] = line
    publish_current_execution_scope(
        db,
        source_revision=REVISION,
        source_generation_id=int(generation.id),
        scope_key="period-plan:all-live-plans",
        rows=[],
        entity_kinds=("period_plan_execution",),
        summary={"snapshots": {}},
    )
    db.commit()
    return generation, plans, runs, lines, items


def _stub_refresh(monkeypatch, db, calls):
    from app.services import obligation_refresh_orchestrator, period_plan_service

    monkeypatch.setattr(
        period_plan_service,
        "_read_period_plan_execution_payload_for_run",
        lambda _db, *, plan, run, generation_id: {
            "plan": {"id": int(plan.id)},
            "run_id": int(run.run_id),
            "generation_id": int(generation_id),
            "matrix": [
                {
                    "line_id": int(line.id),
                    "qty": str(line.qty),
                    "accepted": str(line.accepted_output_qty),
                    "remaining": str(line.remaining_output_qty),
                }
                for line in _db.query(models.ProductionPlanLine)
                .filter_by(plan_id=int(plan.id))
                .order_by(models.ProductionPlanLine.id)
            ],
        },
    )

    def refresh(_db, **kwargs):
        calls.append(dict(kwargs))
        parent = _db.get(models.LedgerGeneration, int(kwargs["parent_generation_id"]))
        target = models.LedgerGeneration(
            generation_key=kwargs["generation_key"],
            status="accepted",
            cutoff=parent.cutoff,
            accepted_at=CUTOFF,
            source_watermarks={"parent_generation_id": int(parent.id)},
            capabilities={},
            physical_import_batch_id=int(parent.physical_import_batch_id),
            algorithm_version="test",
        )
        _db.add(target)
        _db.flush()
        for plan_id in kwargs["replace_plan_ids"]:
            old = _db.query(models.PlanningRun).filter_by(
                source_plan_id=int(plan_id), status="FIXED_SNAPSHOT"
            ).one()
            old.status = "CLOSED"
            successor = models.PlanningRun(
                source_plan_id=int(plan_id),
                prior_run_id=int(old.run_id),
                status="FIXED_SNAPSHOT",
                ledger_generation_id=int(target.id),
                ledger_cutoff=target.cutoff,
                period_from=old.period_from,
                period_to=old.period_to,
                fixed_at=CUTOFF,
                config_snapshot=dict(kwargs["config_snapshot"]),
                horizon_days=kwargs["horizon_days"],
                pinned=True,
            )
            _db.add(successor)
            _db.flush()
            for line in _db.query(models.ProductionPlanLine).filter_by(
                plan_id=int(plan_id)
            ):
                line.locked_by_run_id = int(successor.run_id)
                _db.add(models.MrpRunRoot(
                    run_id=int(successor.run_id),
                    plan_line_id=int(line.id),
                    planned_qty=Decimal(line.remaining_output_qty),
                    accepted_qty=Decimal("0"),
                    remaining_qty=Decimal(line.remaining_output_qty),
                ))
        _db.get(models.PlanningTruthState, 1).current_generation_id = int(target.id)
        _db.add(models.LedgerBuildBatch(
            ledger_generation_id=int(target.id),
            stage="snapshot_build",
            batch_key=f"test-correction-snapshot-{int(target.id)}",
            status="completed",
            algorithm_version="test",
            metrics={
                "local_order_reconciliation": {
                    "ledger_generation_id": int(target.id),
                    "resized": 0,
                    "cancelled": 0,
                    "locked": 0,
                    "entries": [],
                }
            },
            completed_at=CUTOFF,
        ))
        _db.flush()
        return SimpleNamespace(target_generation_id=int(target.id), published=True)

    monkeypatch.setattr(obligation_refresh_orchestrator, "run_obligation_refresh", refresh)


def _groups(items, *, reverse=False):
    groups = [
        {
            "plan_ids": [2, 1],
            "target_entries": [
                {
                    "item_id": int(items[2].item_id),
                    "bucket_date": "2026-01-15",
                    "qty": "5",
                },
                {
                    "item_id": int(items[0].item_id),
                    "bucket_date": "2026-01-01",
                    "qty": "6",
                },
            ],
        },
        {
            "plan_ids": [3],
            "target_entries": [{
                "item_id": int(items[1].item_id),
                "bucket_date": "2026-03-01",
                "qty": "0",
            }],
        },
    ]
    if reverse:
        groups.reverse()
        groups[-1]["target_entries"].reverse()
    return groups


def test_batch_correction_preserves_fact_owner_and_publishes_once(
    db_session, monkeypatch
):
    generation, _plans, runs, lines, items = _world(db_session)
    calls = []
    _stub_refresh(monkeypatch, db_session, calls)

    result = correct_fixed_plan_groups(
        db_session,
        groups=_groups(items),
        source_id="google-plan-correction-target",
        source_hash=SOURCE_HASH,
        expected_generation_id=int(generation.id),
        expected_source_revision=REVISION,
        corrected_by="test",
    )

    assert result["status"] == "applied"
    assert len(calls) == 1
    assert calls[0]["replace_plan_ids"] == (1, 2, 3)
    assert calls[0]["local_order_reconciliation_plan_ids"] == (1, 2, 3)
    assert result["local_order_reconciliation"]["entries"] == []
    db_session.expire_all()
    assert Decimal(db_session.get(models.ProductionPlanLine, lines[1].id).qty) == 4
    secondary = db_session.get(models.ProductionPlanLine, lines[2].id)
    assert Decimal(secondary.qty) == 2
    assert Decimal(secondary.accepted_output_qty) == 3
    assert Decimal(secondary.remaining_output_qty) == 0
    march = db_session.get(models.ProductionPlanLine, lines[3].id)
    assert Decimal(march.qty) == 0
    assert Decimal(march.accepted_output_qty) == 2
    assert Decimal(march.remaining_output_qty) == 0
    untouched = db_session.get(models.ProductionPlanLine, lines[4].id)
    assert Decimal(untouched.qty) == 7
    assert int(untouched.locked_by_run_id) == int(runs[4].run_id)
    created = db_session.query(models.ProductionPlanLine).filter_by(
        plan_id=1,
        item_id=int(items[2].item_id),
        bucket_date=date(2026, 1, 15),
    ).one()
    assert Decimal(created.qty) == 5

    audit = db_session.get(models.ProductionPlanCorrection, result["correction_id"])
    assert audit is not None
    assert audit.before_payload["run_roots"]
    assert audit.after_payload["run_roots"]
    assert db_session.query(models.ClosedPlanSnapshot).count() == 3
    line_audit = db_session.query(models.ProductionPlanCorrectionLine).filter_by(
        plan_line_id=int(secondary.id)
    ).one()
    assert Decimal(line_audit.accepted_qty) == 3
    assert Decimal(line_audit.after_overproduction_qty) == 1


def test_reordered_retry_is_idempotent_before_stale_generation_guard(
    db_session, monkeypatch
):
    generation, _plans, _runs, _lines, items = _world(db_session)
    calls = []
    _stub_refresh(monkeypatch, db_session, calls)
    kwargs = {
        "source_id": "google-plan-correction-target",
        "source_hash": SOURCE_HASH,
        "expected_generation_id": int(generation.id),
        "expected_source_revision": REVISION,
        "corrected_by": "test",
    }
    first = correct_fixed_plan_groups(db_session, groups=_groups(items), **kwargs)
    retry = correct_fixed_plan_groups(
        db_session,
        groups=_groups(items, reverse=True),
        **kwargs,
    )

    assert retry["status"] == "already_applied"
    assert retry["correction_id"] == first["correction_id"]
    assert len(calls) == 1

    changed = _groups(items)
    changed[0]["target_entries"][0]["qty"] = "6"
    with pytest.raises(FixedPlanCorrectionError, match="different target payload"):
        correct_fixed_plan_groups(db_session, groups=changed, **kwargs)


def test_dry_run_executes_publication_path_and_rolls_back(db_session, monkeypatch):
    generation, _plans, runs, lines, items = _world(db_session)
    calls = []
    _stub_refresh(monkeypatch, db_session, calls)

    result = correct_fixed_plan_groups(
        db_session,
        groups=_groups(items),
        source_id="google-plan-correction-dry-run",
        source_hash="b" * 64,
        expected_generation_id=int(generation.id),
        expected_source_revision=REVISION,
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["correction_id"] is None
    assert len(calls) == 1
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == generation.id
    assert db_session.query(models.ProductionPlanCorrection).count() == 0
    assert Decimal(db_session.get(models.ProductionPlanLine, lines[1].id).qty) == 5
    assert int(db_session.get(models.ProductionPlanLine, lines[1].id).locked_by_run_id) == int(runs[1].run_id)


def test_correction_rejects_cross_month_group_before_mutation(db_session, monkeypatch):
    generation, _plans, _runs, lines, items = _world(db_session)
    calls = []
    _stub_refresh(monkeypatch, db_session, calls)

    with pytest.raises(FixedPlanCorrectionError, match="same calendar month"):
        correct_fixed_plan_groups(
            db_session,
            groups=[{
                "plan_ids": [1, 3],
                "target_entries": [],
            }],
            source_id="cross-month",
            source_hash="c" * 64,
            expected_generation_id=int(generation.id),
            expected_source_revision=REVISION,
        )

    assert calls == []
    assert Decimal(db_session.get(models.ProductionPlanLine, lines[1].id).qty) == 5


def test_mixed_overproduction_summary_keeps_cellwise_remaining():
    payload = {
        "plan_output_rows": [
            {
                "plan_line_id": 1,
                "planned_output_qty": 0,
                "accepted_plan_output_qty": 3,
                "assembly_remaining_qty": 0,
                "overproduction_qty": 3,
            },
            {
                "plan_line_id": 2,
                "planned_output_qty": 10,
                "accepted_plan_output_qty": 0,
                "assembly_remaining_qty": 10,
                "overproduction_qty": 0,
            },
        ],
        "summary": {
            "planned_output_qty": 10,
            "accepted_plan_output_qty": 3,
            "assembly_remaining_qty": 10,
            "overproduction_qty": 3,
        },
    }

    assert _saved_plan_output_validation_error(payload) is None


def test_scenario_builder_binds_target_to_current_guards(db_session, tmp_path):
    generation, _plans, _runs, _lines, items = _world(db_session)
    target_path = tmp_path / "correction-target.json"
    target_path.write_text(
        json.dumps({
            "months": [
                {
                    "month": "2026-01",
                    "total_qty": 6,
                    "cells": [{
                        "item_id": int(items[0].item_id),
                        "item_code": str(items[0].item_code),
                        "bucket_date": "2026-01-01",
                        "qty": 6,
                        "source_row": 10,
                        "week": 1,
                    }],
                },
                {
                    "month": "2026-03",
                    "total_qty": 0,
                    "cells": [],
                },
            ]
        }),
        encoding="utf-8",
    )
    from tools.fixed_plan_correction_scenario import build_request

    request, evidence = build_request(
        db_session,
        target_path=target_path,
        groups=[[1, 2], [3]],
        untouched_plan_ids=[4],
        source_id="google-plan-correction-target",
        corrected_by="test",
        dry_run=True,
    )

    assert request["expected_generation_id"] == int(generation.id)
    assert request["expected_source_revision"] == REVISION
    assert request["groups"][0]["plan_ids"] == [1, 2]
    assert request["groups"][1]["target_entries"] == []
    assert evidence["current_run_ids"].keys() == {"1", "2", "3"}
    assert evidence["untouched_plan_ids"] == [4]

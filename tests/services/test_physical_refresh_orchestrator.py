"""Targeted contract tests for the physical refresh orchestrator."""

from datetime import datetime, timedelta, timezone

import pytest

from app import models
from app.services.item_ledger import historical_bootstrap_phase0 as bootstrap
from app.services.item_ledger import historical_import_orchestration as importer
from app.services.item_ledger import physical_refresh_generation
from app.services.item_ledger import physical_refresh_orchestrator as workflow
from app.services.item_ledger.physical import (
    guard_physical_batch_writer,
    physical_sequence_lock_context,
)
from app.services.obligation_refresh_orchestrator import ObligationRefreshOrchestrationResult


def test_physical_lifecycle_lock_uses_dedicated_connection_across_commits():
    events = []

    class Connection:
        def execute(self, *_args, **_kwargs):
            events.append("execute")
            return type("Result", (), {"fetchone": lambda self: (True,)})()

        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

        def close(self):
            events.append("close")

    connection = Connection()

    class Bind:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def connect(self):
            events.append("connect")
            return connection

    class DB:
        def get_bind(self):
            return Bind()

        def commit(self):
            events.append("worker-commit")

    lock = workflow._acquire_lifecycle_lock(DB())
    assert lock is connection
    DB().commit()
    workflow._release_lifecycle_lock(lock)
    assert events == ["connect", "execute", "commit", "worker-commit", "execute", "commit", "close"]


def test_lifecycle_guard_skips_nested_sequence_advisory_lock_only_in_context():
    events = []

    class DB:
        def get_bind(self):
            return type(
                "Bind",
                (),
                {"dialect": type("Dialect", (), {"name": "postgresql"})()},
            )()

        def execute(self, *_args, **_kwargs):
            events.append("execute")

    db = DB()
    guard_physical_batch_writer(db)
    assert events == ["execute"]
    with physical_sequence_lock_context():
        guard_physical_batch_writer(db)
    assert events == ["execute"]


def _accepted_parent(db_session, *, generation_key="accepted-parent"):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    parent_batch = models.PhysicalImportBatch(
        batch_key=f"accepted-physical:{generation_key}",
        status="completed",
        cutoff=cutoff,
        source_watermarks={"rows_read": 123},
        completed_at=cutoff,
    )
    parent = models.LedgerGeneration(
        generation_key=generation_key,
        status="accepted",
        cutoff=cutoff,
        source_watermarks={"replay_from": "2026-07-01T00:00:00+00:00"},
        capabilities={"physical_ledger": True},
        physical_import_batch=parent_batch,
        algorithm_version="accepted/1",
        accepted_at=cutoff,
    )
    db_session.add_all([parent_batch, parent])
    db_session.flush()
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=parent.id))
    db_session.commit()
    return parent, parent_batch


def test_run_physical_refresh_no_work_on_lock_contention(db_session, monkeypatch):
    parent, _ = _accepted_parent(db_session)
    blocked = []

    monkeypatch.setattr(workflow, "_acquire_lifecycle_lock", lambda db: False)
    monkeypatch.setattr(
        workflow,
        "fork_physical_refresh_generation",
        lambda *args, **kwargs: blocked.append("fork") or pytest.fail("should not fork on lock contention"),
    )
    monkeypatch.setattr(
        workflow,
        "run_physical_recorder_audit",
        lambda *args, **kwargs: blocked.append("audit") or pytest.fail("should not audit on lock contention"),
    )
    monkeypatch.setattr(
        workflow,
        "run_historical_physical_import",
        lambda *args, **kwargs: blocked.append("import") or pytest.fail("should not import on lock contention"),
    )
    monkeypatch.setattr(
        workflow,
        "evaluate_physical_refresh_balance_convergence",
        lambda *args, **kwargs: blocked.append("balance") or pytest.fail(
            "should not validate balance on lock contention",
        ),
    )
    with pytest.raises(
        workflow.PhysicalRefreshOrchestratorError,
        match="another physical refresh is running",
    ):
        workflow.run_physical_refresh(
            db_session,
            generation_key="lock-contention",
            target_cutoff=parent.cutoff + timedelta(days=1),
            client=object(),
            balance_snapshot={},
        )

    assert blocked == []
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id


def test_run_physical_refresh_processes_fork_import_balance_accept_obligation_in_order(db_session, monkeypatch):
    parent, parent_batch = _accepted_parent(db_session)
    forked_batch = models.PhysicalImportBatch(
        batch_key="forked-physical",
        status="completed",
        cutoff=parent.cutoff + timedelta(days=1),
        source_watermarks={},
        completed_at=parent.cutoff + timedelta(days=1),
    )
    physical = models.LedgerGeneration(
        generation_key="physical-fork",
        status="building",
        cutoff=parent.cutoff + timedelta(days=1),
        source_watermarks={"replay_from": "2026-07-01T00:00:00+00:00"},
        physical_import_batch=forked_batch,
        algorithm_version="ledger-physical-refresh-generation/1",
        replay_version="ledger-physical-refresh-replay/1",
    )
    published = models.LedgerGeneration(
        generation_key="published-physical",
        status="accepted",
        cutoff=parent.cutoff + timedelta(days=1),
        source_watermarks={"reason": "test"},
        physical_import_batch=parent_batch,
        algorithm_version="ledger-physical-refresh-generation/1",
        accepted_at=parent.cutoff + timedelta(days=1),
        capabilities={"physical_ledger": True},
    )
    db_session.add_all([forked_batch, physical, published])
    db_session.flush()

    target_cutoff = parent.cutoff + timedelta(days=1)
    calls = []
    commit_calls = []
    original_commit = db_session.commit

    def _commit():
        commit_calls.append("commit")
        return original_commit()

    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=physical.id,
        generation_key="physical-fork",
        physical_import_batch_id=forked_batch.id,
        cutoff=target_cutoff,
        from_cutoff=parent.cutoff,
        created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=physical.id,
        from_exclusive=parent.cutoff,
        cutoff=target_cutoff,
        completed_through=target_cutoff,
        windows_completed=1,
        windows_resumed=0,
        recorders_pulled=0,
        movements_inserted=0,
        complete=True,
        physical_import_batch_id=forked_batch.id,
    )
    balance_result = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=physical.id,
        cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(),
        valid=True,
        content_hash="hash",
        compared=0,
        mismatched=0,
        matched=0,
        terminal_batch_id=forked_batch.id,
        deltas=(),
    )

    monkeypatch.setattr(workflow, "_acquire_lifecycle_lock", lambda db: True)
    monkeypatch.setattr(
        workflow,
        "fork_physical_refresh_generation",
        lambda *args, **kwargs: calls.append("fork") or fork_result,
    )
    monkeypatch.setattr(
        workflow,
        "run_physical_recorder_audit",
        lambda *args, **kwargs: calls.append("audit") or object(),
    )
    monkeypatch.setattr(
        workflow,
        "run_historical_physical_import",
        lambda *args, **kwargs: calls.append("import") or import_result,
    )
    monkeypatch.setattr(
        workflow,
        "evaluate_physical_refresh_balance_convergence",
        lambda *args, **kwargs: calls.append("balance") or balance_result,
    )
    monkeypatch.setattr(
        workflow,
        "accept_generation_build",
        lambda *args, **kwargs: calls.append("accept") or {},
    )
    monkeypatch.setattr(
        workflow,
        "run_obligation_refresh",
        lambda *args, **kwargs: calls.append("obligation")
        or _publish_obligation_refresh(db_session, parent.id, published.id),
    )
    monkeypatch.setattr(db_session, "commit", _commit)

    result = workflow.run_physical_refresh(
        db_session,
        generation_key="happy-path",
        target_cutoff=target_cutoff,
        client=object(),
        balance_snapshot={},
        started_by="pytest",
    )

    assert calls == ["fork", "audit", "import", "balance", "accept", "obligation"]
    assert commit_calls == ["commit", "commit"]
    assert result.parent_generation_id == parent.id
    assert result.physical_generation_id == physical.id
    assert result.published_generation_id == published.id
    assert result.cutoff == target_cutoff.replace(tzinfo=timezone.utc)
    assert result.published is True
    assert result.candidate_run_ids == (11, 12, 13)
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == published.id


def _publish_obligation_refresh(db_session, parent_id, published_id):
    state = db_session.get(models.PlanningTruthState, 1)
    if state is None:
        state = models.PlanningTruthState(id=1)
        db_session.add(state)
    state.current_generation_id = published_id
    db_session.flush()
    return ObligationRefreshOrchestrationResult(
        parent_generation_id=parent_id,
        target_generation_id=published_id,
        candidate_run_ids=(11, 12, 13),
        published=True,
    )


def test_balance_mismatch_stops_before_accept_or_obligation(db_session, monkeypatch):
    parent, _ = _accepted_parent(db_session)
    target_cutoff = parent.cutoff + timedelta(days=1)
    calls = []

    forked_batch = models.PhysicalImportBatch(
        batch_key="forked-physical",
        status="completed",
        cutoff=target_cutoff,
        source_watermarks={},
        completed_at=target_cutoff,
    )
    physical = models.LedgerGeneration(
        generation_key="physical-fork",
        status="building",
        cutoff=target_cutoff,
        source_watermarks={"replay_from": "2026-07-01T00:00:00+00:00"},
        physical_import_batch=forked_batch,
        algorithm_version="ledger-physical-refresh-generation/1",
        replay_version="ledger-physical-refresh-replay/1",
    )
    db_session.add_all([forked_batch, physical])
    db_session.flush()

    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=physical.id,
        generation_key="physical-fork",
        physical_import_batch_id=forked_batch.id,
        cutoff=target_cutoff,
        from_cutoff=parent.cutoff,
        created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=physical.id,
        from_exclusive=parent.cutoff,
        cutoff=target_cutoff,
        completed_through=target_cutoff,
        windows_completed=1,
        windows_resumed=0,
        recorders_pulled=0,
        movements_inserted=0,
        complete=True,
        physical_import_batch_id=forked_batch.id,
    )
    mismatch = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=physical.id,
        cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(),
        valid=False,
        content_hash="mismatch",
        compared=1,
        mismatched=2,
        matched=0,
        terminal_batch_id=forked_batch.id,
        deltas=(),
    )

    monkeypatch.setattr(workflow, "fork_physical_refresh_generation", lambda *args, **kwargs: calls.append("fork") or fork_result)
    monkeypatch.setattr(workflow, "run_physical_recorder_audit", lambda *args, **kwargs: calls.append("audit") or object())
    monkeypatch.setattr(workflow, "run_historical_physical_import", lambda *args, **kwargs: calls.append("import") or import_result)
    monkeypatch.setattr(workflow, "evaluate_physical_refresh_balance_convergence", lambda *args, **kwargs: calls.append("balance") or mismatch)
    monkeypatch.setattr(
        workflow,
        "accept_generation_build",
        lambda *args, **kwargs: calls.append("accept") or pytest.fail("accept must not run on mismatch"),
    )
    monkeypatch.setattr(
        workflow,
        "run_obligation_refresh",
        lambda *args, **kwargs: calls.append("obligation") or pytest.fail("obligation must not run on mismatch"),
    )

    with pytest.raises(
        workflow.PhysicalRefreshOrchestratorError,
        match="Balance convergence failed: 2 mismatches",
    ):
        workflow.run_physical_refresh(
            db_session,
            generation_key="balance-mismatch",
            target_cutoff=target_cutoff,
            client=object(),
            balance_snapshot={},
        )

    assert calls == ["fork", "audit", "import", "balance"]
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id


def test_final_obligation_exception_rolls_back_intermediate_pointer(db_session, monkeypatch):
    parent, _ = _accepted_parent(db_session)
    target_cutoff = parent.cutoff + timedelta(days=1)
    calls = []
    commit_calls = []
    original_commit = db_session.commit

    def _commit():
        commit_calls.append("commit")
        return original_commit()

    forked_batch = models.PhysicalImportBatch(
        batch_key="forked-physical",
        status="completed",
        cutoff=target_cutoff,
        source_watermarks={},
        completed_at=target_cutoff,
    )
    physical = models.LedgerGeneration(
        generation_key="physical-fork",
        status="building",
        cutoff=target_cutoff,
        source_watermarks={"replay_from": "2026-07-01T00:00:00+00:00"},
        physical_import_batch=forked_batch,
        algorithm_version="ledger-physical-refresh-generation/1",
        replay_version="ledger-physical-refresh-replay/1",
    )
    db_session.add_all([forked_batch, physical])
    db_session.flush()

    fork_result = physical_refresh_generation.PhysicalRefreshGenerationResult(
        ledger_generation_id=physical.id,
        generation_key="physical-fork",
        physical_import_batch_id=forked_batch.id,
        cutoff=target_cutoff,
        from_cutoff=parent.cutoff,
        created=True,
    )
    import_result = importer.HistoricalImportResult(
        ledger_generation_id=physical.id,
        from_exclusive=parent.cutoff,
        cutoff=target_cutoff,
        completed_through=target_cutoff,
        windows_completed=1,
        windows_resumed=0,
        recorders_pulled=0,
        movements_inserted=0,
        complete=True,
        physical_import_batch_id=forked_batch.id,
    )
    balance_result = bootstrap.BalanceConvergenceResult(
        ledger_generation_id=physical.id,
        cutoff=target_cutoff.isoformat(),
        checked_at=target_cutoff.isoformat(),
        valid=True,
        content_hash="hash",
        compared=0,
        mismatched=0,
        matched=0,
        terminal_batch_id=forked_batch.id,
        deltas=(),
    )

    def _accept(*_args, **_kwargs):
        calls.append("accept")
        state = db_session.get(models.PlanningTruthState, 1)
        state.current_generation_id = physical.id
        db_session.flush()

    def _obligation(*_args, **_kwargs):
        calls.append("obligation")
        raise RuntimeError("obligation publish failed")

    monkeypatch.setattr(workflow, "fork_physical_refresh_generation", lambda *args, **kwargs: calls.append("fork") or fork_result)
    monkeypatch.setattr(workflow, "run_physical_recorder_audit", lambda *args, **kwargs: calls.append("audit") or object())
    monkeypatch.setattr(workflow, "run_historical_physical_import", lambda *args, **kwargs: calls.append("import") or import_result)
    monkeypatch.setattr(workflow, "evaluate_physical_refresh_balance_convergence", lambda *args, **kwargs: calls.append("balance") or balance_result)
    monkeypatch.setattr(workflow, "accept_generation_build", _accept)
    monkeypatch.setattr(workflow, "run_obligation_refresh", _obligation)
    monkeypatch.setattr(db_session, "commit", _commit)

    with pytest.raises(RuntimeError, match="obligation publish failed"):
        workflow.run_physical_refresh(
            db_session,
            generation_key="rollback-obligation",
            target_cutoff=target_cutoff,
            client=object(),
            balance_snapshot={},
        )

    assert calls == ["fork", "audit", "import", "balance", "accept", "obligation"]
    assert commit_calls == ["commit"]
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == parent.id

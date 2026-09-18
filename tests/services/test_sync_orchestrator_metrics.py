from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services import sync_orchestrator


class _FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _FakeDb:
    def __init__(self, value=245799, error=None):
        self.value = value
        self.error = error
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        if self.error:
            raise self.error
        return _FakeResult(self.value)


def test_automatic_refresh_preflight_measures_ledger_without_manual_override():
    db = _FakeDb()
    count, elapsed_ms = sync_orchestrator._measure_database_ledger_rows(db)

    assert count == 245799
    assert elapsed_ms >= 0
    assert "stock_ledger_entry" in str(db.statement)


def test_automatic_refresh_preflight_does_not_turn_database_error_into_zero():
    error = RuntimeError("ledger count unavailable")
    with pytest.raises(RuntimeError, match="ledger count unavailable"):
        sync_orchestrator._measure_database_ledger_rows(_FakeDb(error=error))


class _Query:
    def filter(self, *args, **kwargs):
        return self

    def one_or_none(self):
        return None


def test_automatic_refresh_passes_measured_count_to_existing_argument(monkeypatch):
    captured = {}
    parent = SimpleNamespace(id=1391, cutoff=datetime(2026, 9, 17, tzinfo=timezone.utc))
    db = SimpleNamespace(
        query=lambda *_args, **_kwargs: _Query(),
        execute=lambda statement: _FakeResult(245799),
        commit=lambda: None,
        rollback=lambda: None,
    )
    monkeypatch.setattr(sync_orchestrator, "_current_accepted_parent", lambda _db: parent)
    monkeypatch.setattr(sync_orchestrator, "_run_nomenclature", lambda *_args: None)
    monkeypatch.setattr(sync_orchestrator, "load_odata_config", lambda: {})
    monkeypatch.setattr(sync_orchestrator, "_build_client", lambda: SimpleNamespace(
        base_url="http://example", username="u", password="p", token=None,
    ))
    monkeypatch.setattr(sync_orchestrator, "get_stock_from_1c_odata", lambda **_kwargs: [])
    monkeypatch.setattr(sync_orchestrator, "build_balance_snapshot", lambda *_args, **_kwargs: {})

    def fake_refresh(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            parent_generation_id=1391,
            physical_generation_id=1392,
            published_generation_id=1392,
            cutoff=datetime(2026, 9, 18, tzinfo=timezone.utc),
            published=True,
            candidate_run_ids=(1,),
            opening_reconcile=None,
            input_delta_rows=1,
            replayed_rows=1,
            affected_scopes=(),
            duration_ms=10,
            database_ledger_rows=kwargs["database_ledger_rows"],
            phase_timings=(),
        )

    monkeypatch.setattr(sync_orchestrator, "run_physical_refresh", fake_refresh)
    result = sync_orchestrator._run_physical_refresh_job(
        db, datetime(2026, 9, 18, tzinfo=timezone.utc), "physical-refresh:test"
    )

    assert captured["database_ledger_rows"] == 245799
    assert result["result"]["database_ledger_rows"] == 245799
    assert "database_ledger_rows_count_ms" in result["result"]

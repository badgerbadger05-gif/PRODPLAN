"""Tests for the auto-sync orchestrator: due selection, ordering, state, throttle."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import sync_orchestrator as orch
from app.services.sync_orchestrator import SyncJob


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "STATE_PATH", tmp_path / "sync_schedule.json")
    return tmp_path


@pytest.fixture
def stub_jobs(monkeypatch):
    """Replace the real registry with two recording stub jobs (no OData)."""
    calls = []

    def make(idx):
        def runner(db, config):
            calls.append(idx)
            return {"ran": idx}
        return runner

    jobs = [
        SyncJob("alpha", "Alpha", 1000, make("alpha")),   # parent / low index
        SyncJob("beta", "Beta", 100, make("beta")),       # child / high index
    ]
    monkeypatch.setattr(orch, "SYNC_JOBS", jobs)
    monkeypatch.setattr(orch, "_JOB_BY_ID", {j.id: j for j in jobs})
    monkeypatch.setattr(orch, "_ORDER_INDEX", {j.id: i for i, j in enumerate(jobs)})
    monkeypatch.setattr(orch, "load_odata_config", lambda: {"base_url": "http://x/unf_demo/odata"})
    return calls


def test_tick_skipped_when_not_configured(tmp_state, monkeypatch):
    monkeypatch.setattr(orch, "load_odata_config", lambda: {"base_url": ""})
    res = orch.tick(db=None)
    assert res["status"] == "skipped"


def test_tick_runs_lowest_order_due_job_first(tmp_state, stub_jobs):
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    res = orch.tick(db=None, now=now)
    assert res["status"] == "ok"
    assert res["job"] == "alpha"          # lowest dependency index wins
    assert res["due_count"] == 2
    assert stub_jobs == ["alpha"]


def test_second_tick_runs_next_due_job(tmp_state, stub_jobs):
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    orch.tick(db=None, now=now)            # runs alpha, stamps its last_run
    res = orch.tick(db=None, now=now)      # alpha not due now → beta
    assert res["job"] == "beta"
    assert stub_jobs == ["alpha", "beta"]


def test_interval_throttles_rerun(tmp_state, stub_jobs):
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    orch.tick(db=None, now=now)            # alpha
    orch.tick(db=None, now=now)            # beta
    # Both just ran; nothing is due a second later.
    res = orch.tick(db=None, now=now + timedelta(seconds=1))
    assert res["status"] == "idle"
    # beta (interval 100s) becomes due again before alpha (1000s).
    res = orch.tick(db=None, now=now + timedelta(seconds=150))
    assert res["job"] == "beta"


def test_failing_job_is_stamped_and_not_hammered(tmp_state, monkeypatch):
    def boom(db, config):
        raise RuntimeError("odata down")

    jobs = [SyncJob("solo", "Solo", 300, boom)]
    monkeypatch.setattr(orch, "SYNC_JOBS", jobs)
    monkeypatch.setattr(orch, "_JOB_BY_ID", {j.id: j for j in jobs})
    monkeypatch.setattr(orch, "_ORDER_INDEX", {"solo": 0})
    monkeypatch.setattr(orch, "load_odata_config", lambda: {"base_url": "http://x/unf_demo/odata"})

    class _DB:
        def rollback(self):
            pass

    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    res = orch.tick(db=_DB(), now=now)
    assert res["status"] == "error"
    # last_run_at stamped despite the failure → no retry until the interval elapses.
    res2 = orch.tick(db=_DB(), now=now + timedelta(seconds=10))
    assert res2["status"] == "idle"


def test_real_registry_orders_nomenclature_before_stock():
    # Sanity: the real registry keeps dependency order (nomenclature precedes stock).
    idx = orch._ORDER_INDEX
    assert idx["nomenclature"] < idx["specifications"]
    assert idx["nomenclature"] < idx["stock"]
    assert idx["warehouses"] < idx["stock"]


def test_registry_covers_employees_warehouses_and_groups():
    ids = {j.id for j in orch.SYNC_JOBS}
    # The three the operator asked about are all scheduled.
    assert {"employees", "warehouses", "nomenclatureGroups"} <= ids
    # Group list refresh comes right after nomenclature (shares that reference data).
    assert orch._ORDER_INDEX["nomenclatureGroups"] == orch._ORDER_INDEX["nomenclature"] + 1


def test_status_reports_all_jobs(tmp_state, monkeypatch):
    monkeypatch.setattr(orch, "load_odata_config", lambda: {"base_url": "http://x/unf_demo/odata"})
    snap = orch.status()
    assert snap["configured"] is True
    assert len(snap["jobs"]) == len(orch.SYNC_JOBS)
    assert all("next_due_at" in j and "interval_seconds" in j for j in snap["jobs"])


def test_source_sync_marks_dbr_dirty_and_next_tick_runs_maintenance(tmp_state, monkeypatch):
    calls = []

    def stock(db, config):
        calls.append("stock")
        return {"stock": "ok"}

    job = SyncJob("stock", "Stock", 1800, stock)
    monkeypatch.setattr(orch, "SYNC_JOBS", [job])
    monkeypatch.setattr(orch, "_JOB_BY_ID", {"stock": job})
    monkeypatch.setattr(orch, "_ORDER_INDEX", {"stock": 0})
    monkeypatch.setattr(orch, "load_odata_config", lambda: {"base_url": "http://x/unf_demo/odata"})
    monkeypatch.setattr(orch, "pull_queue_health", lambda db: {"pending": 0, "error_retryable": 0, "error_exhausted": 0, "ready": 0})
    monkeypatch.setattr(orch, "_run_dbr_maintenance", lambda db, full: calls.append("dbr") or {"full": full})

    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    first = orch.tick(db=None, now=now)
    assert first["job"] == "stock"
    assert calls == ["stock"]  # never recalculate DBR in the source-sync tick
    assert orch.status()["dbr_maintenance"]["dirty"] is True

    second = orch.tick(db=None, now=now + timedelta(seconds=1))
    assert second["job"] == "dbrMaintenance"
    assert second["mode"] == "incremental"
    assert calls == ["stock", "dbr"]
    assert orch.status()["dbr_maintenance"]["dirty"] is False


def test_dbr_maintenance_failure_keeps_dirty_marker_with_backoff(tmp_state, monkeypatch):
    monkeypatch.setattr(orch, "load_odata_config", lambda: {"base_url": "http://x/unf_demo/odata"})
    monkeypatch.setattr(orch, "pull_queue_health", lambda db: {"pending": 0, "error_retryable": 0, "error_exhausted": 0, "ready": 0})
    monkeypatch.setattr(orch, "_run_dbr_maintenance", lambda db, full: (_ for _ in ()).throw(RuntimeError("broken")))
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    orch._save_state({"dbr_maintenance": {"dirty": True}})

    class _DB:
        def rollback(self):
            pass

    result = orch.tick(db=_DB(), now=now)
    assert result["status"] == "error"
    maintenance = orch.status()["dbr_maintenance"]
    assert maintenance["dirty"] is True
    assert maintenance["next_retry_at"] is not None

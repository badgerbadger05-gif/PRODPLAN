"""Tests for the auto-sync orchestrator: due selection, ordering, state, throttle."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import sync_orchestrator as orch
from app.services.sync_orchestrator import SyncJob
from app import models


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


def test_corrupt_schedule_state_fails_visible(tmp_state):
    orch.STATE_PATH.write_text("{partial", encoding="utf-8")
    with pytest.raises(RuntimeError, match="state is unreadable"):
        orch.status()


def test_scheduler_advisory_lock_uses_dedicated_connection_across_commits():
    """Pool check: worker-session commits cannot release the scheduler lock."""
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

    lock = orch._acquire_cluster_lock(DB())
    assert lock is connection
    DB().commit()
    orch._release_cluster_lock(lock)
    assert events == ["connect", "execute", "commit", "worker-commit", "execute", "commit", "close"]


def _accepted_parent_fixture(
    db_session, *, cutoff: datetime | None = None
) -> models.LedgerGeneration:
    now = cutoff or datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="physical-sync-orchestrator",
        status="completed",
        cutoff=now,
    )
    generation = models.LedgerGeneration(
        generation_key="accepted-truth",
        status="accepted",
        cutoff=now,
        accepted_at=now,
        physical_import_batch=physical,
        source_watermarks={"replay_from": now.isoformat()},
        capabilities={"physical_ledger": True},
        algorithm_version="test",
    )
    db_session.add_all([physical, generation])
    db_session.flush()
    db_session.add(
        models.PlanningTruthState(id=1, current_generation_id=generation.id)
    )
    db_session.commit()
    return generation


def test_pending_ledger_queue_triggers_physical_refresh(
    tmp_state, db_session, monkeypatch
):
    parent = _accepted_parent_fixture(db_session)
    calls: list[str] = []

    monkeypatch.setattr(orch, "load_odata_config", lambda: {"base_url": "http://x/unf_demo/odata"})
    monkeypatch.setattr(orch, "pull_queue_health", lambda db: {"pending": 1, "error_retryable": 0, "error_exhausted": 0, "ready": 1})
    monkeypatch.setattr(
        orch,
        "_run_physical_refresh_job",
        lambda db, cutoff, key: calls.append("physical") or {
            "parent_generation_id": parent.id,
            "physical_generation_id": parent.id + 1,
            "published_generation_id": parent.id + 2,
            "target_cutoff": cutoff.isoformat(),
            "published": True,
            "result": {"queue_integrated": True},
        },
    )

    res = orch.tick(db=db_session, now=datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc))
    assert res["job"] == "physicalRefresh"
    assert calls == ["physical"]


def test_physical_refresh_runs_with_strict_snapshot_and_stores_state(tmp_state, db_session, monkeypatch):
    parent = _accepted_parent_fixture(db_session)
    got_filter: list[str] = []
    got_strict = {"value": False}

    class DummyClient:
        base_url = "https://example.local/odata"
        username = "u"
        password = "p"
        token = "t"

    class Result:
        def __init__(self):
            self.parent_generation_id = parent.id
            self.physical_generation_id = 11
            self.published_generation_id = 12
            self.cutoff = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
            self.candidate_run_ids = (100, 101)
            self.published = True

    def _mock_balance(*args, **kwargs):
        got_filter.append(str(kwargs["filter_query"]))
        return []

    def _mock_snapshot(*args, **kwargs):
        got_strict["value"] = bool(kwargs["strict"])
        return {}

    def _mock_run(*args, **kwargs):
        assert kwargs["generation_key"].startswith(f"physical-refresh:{parent.id}:")
        assert kwargs["target_cutoff"].tzinfo is not None
        return Result()

    monkeypatch.setattr(orch, "load_odata_config", lambda: {"base_url": "http://x/unf_demo/odata"})
    monkeypatch.setattr(orch, "pull_queue_health", lambda db: {"pending": 0, "error_retryable": 0, "error_exhausted": 0, "ready": 0})
    monkeypatch.setattr(orch, "_build_client", lambda: DummyClient())
    monkeypatch.setattr(orch, "get_stock_from_1c_odata", _mock_balance)
    monkeypatch.setattr(orch, "build_balance_snapshot", _mock_snapshot)
    monkeypatch.setattr(orch, "run_physical_refresh", _mock_run)

    now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
    result = orch.tick(db=db_session, now=now)
    assert result["job"] == "physicalRefresh"
    expected_cutoff = now.astimezone(ZoneInfo("Europe/Moscow")).replace(
        tzinfo=None, microsecond=0
    ).isoformat()
    assert got_filter == [f"Period le datetime'{expected_cutoff}'"]
    assert got_strict["value"] is True
    state = orch.status()["physical_refresh"]
    assert state["last_status"] == "ok"
    assert state["last_cutoff"] == now.isoformat()
    assert state["last_result"]["published"] is True


def test_physical_refresh_failure_uses_exponential_backoff(tmp_state, db_session, monkeypatch):
    _accepted_parent_fixture(db_session)
    monkeypatch.setattr(orch, "load_odata_config", lambda: {"base_url": "http://x/unf_demo/odata"})
    monkeypatch.setattr(orch, "pull_queue_health", lambda db: {"pending": 0, "error_retryable": 0, "error_exhausted": 0, "ready": 0})
    class DummyClient:
        base_url = "https://example.local/odata"
        username = None
        password = None
        token = None
    monkeypatch.setattr(orch, "_build_client", lambda: DummyClient())
    monkeypatch.setattr(orch, "get_stock_from_1c_odata", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("one-c oops")))

    now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
    failed = orch.tick(db=db_session, now=now)
    assert failed["status"] == "error"
    assert failed["job"] == "physicalRefresh"
    status = orch.status()["physical_refresh"]
    assert status["failure_count"] == 1
    assert status["next_retry_at"] is not None
    assert status["active_cutoff"] == now.isoformat()
    active_key = status["active_generation_key"]
    assert active_key
    next_retry = datetime.fromisoformat(status["next_retry_at"])
    assert next_retry > now

    monkeypatch.setattr(orch, "_due_jobs", lambda state, current: [])
    again = orch.tick(db=db_session, now=now + timedelta(seconds=10))
    assert again["status"] == "idle"

    monkeypatch.setattr(orch, "_build_client", lambda: type("Client", (), {"base_url": "https://example.local/odata", "username": None, "password": None, "token": None}))
    def _resume(db, cutoff, key):
        assert cutoff == now
        assert key == active_key
        return {"parent_generation_id": 1, "physical_generation_id": 1, "published_generation_id": 1, "target_cutoff": cutoff.isoformat(), "published": True, "result": {"ok": True}}

    monkeypatch.setattr(orch, "_run_physical_refresh_job", _resume)
    done = orch.tick(db=db_session, now=next_retry + timedelta(seconds=1))
    assert done["status"] == "ok"
    assert done["job"] == "physicalRefresh"
    assert orch.status()["physical_refresh"]["active_generation_key"] is None


def test_physical_refresh_recovers_building_generation_when_state_is_lost(
    tmp_state, db_session, monkeypatch
):
    parent = _accepted_parent_fixture(db_session)
    cutoff = parent.cutoff + timedelta(hours=2)
    candidate = models.LedgerGeneration(
        generation_key="physical-refresh:recover-from-db",
        status="building",
        cutoff=cutoff,
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": parent.id,
            "parent_physical_import_batch_id": parent.physical_import_batch_id,
            "from_cutoff": parent.cutoff.replace(
                tzinfo=timezone.utc
            ).isoformat(),
            "replay_from": parent.source_watermarks["replay_from"],
        },
        capabilities={},
        physical_import_batch_id=parent.physical_import_batch_id,
        algorithm_version="ledger-physical-refresh-generation/1",
        replay_version="ledger-physical-refresh-replay/1",
    )
    db_session.add(candidate)
    db_session.commit()
    seen = {}

    def _resume(db, target_cutoff, generation_key):
        seen.update(cutoff=target_cutoff, key=generation_key)
        return {
            "parent_generation_id": parent.id,
            "physical_generation_id": candidate.id,
            "published_generation_id": candidate.id,
            "target_cutoff": target_cutoff.isoformat(),
            "published": True,
            "result": {"recovered": True},
        }

    monkeypatch.setattr(
        orch, "load_odata_config", lambda: {"base_url": "http://configured"}
    )
    monkeypatch.setattr(
        orch,
        "pull_queue_health",
        lambda db: {
            "pending": 0,
            "error_retryable": 0,
            "error_exhausted": 0,
            "ready": 0,
        },
    )
    monkeypatch.setattr(orch, "_run_physical_refresh_job", _resume)

    result = orch.tick(
        db=db_session,
        now=cutoff + timedelta(hours=1),
    )

    assert result["job"] == "physicalRefresh"
    assert seen["key"] == candidate.generation_key
    assert seen["cutoff"] == cutoff.replace(tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "marks,key,algorithm",
    [
        (
            {"generation_kind": "physical_refresh", "parent_generation_id": 999},
            "physical-refresh:old-parent",
            "ledger-physical-refresh-generation/1",
        ),
        (
            {"parent_generation_id": 1},
            "physical-refresh:malformed",
            "ledger-physical-refresh-generation/1",
        ),
    ],
)
def test_tick_fails_closed_on_unexpected_building_physical_refresh(
    tmp_state, db_session, monkeypatch, marks, key, algorithm
):
    parent = _accepted_parent_fixture(db_session)
    db_session.add(
        models.LedgerGeneration(
            generation_key=key,
            status="building",
            cutoff=parent.cutoff + timedelta(hours=1),
            source_watermarks=marks,
            capabilities={},
            physical_import_batch_id=parent.physical_import_batch_id,
            algorithm_version=algorithm,
        )
    )
    db_session.commit()
    monkeypatch.setattr(orch, "load_odata_config", lambda: {"base_url": "http://configured"})
    with pytest.raises(RuntimeError, match="unexpected BUILDING physical refresh"):
        orch.tick(db_session, now=datetime(2026, 7, 24, 12, tzinfo=timezone.utc))
    inventory = orch.status(db_session)["physical_refresh"]
    assert inventory["building_inventory_total"] == 1
    assert inventory["unexpected_building_count"] == 1


def test_orphan_non_physical_terminal_is_visible_and_blocks_remote_refresh(
    tmp_state, db_session, monkeypatch
):
    parent = _accepted_parent_fixture(db_session)
    db_session.add(
        models.PhysicalImportBatch(
            batch_key="bootstrap-orphan-terminal",
            status="completed",
            cutoff=parent.cutoff + timedelta(hours=1),
            source_watermarks={"source": "historical-bootstrap-boundary"},
        )
    )
    db_session.commit()
    monkeypatch.setattr(orch, "load_odata_config", lambda: {"base_url": "http://configured"})
    monkeypatch.setattr(
        orch,
        "_run_physical_refresh_job",
        lambda *args, **kwargs: pytest.fail("remote refresh must be blocked by terminal conflict"),
    )
    snapshot = orch.status(db_session)
    physical = snapshot["physical_refresh"]
    assert physical["accepted_physical_terminal_id"] == int(parent.physical_import_batch_id)
    assert physical["global_physical_terminal_id"] != physical["accepted_physical_terminal_id"]
    assert physical["terminal_conflict"] is True
    with pytest.raises(RuntimeError, match="terminal conflicts"):
        orch.tick(db_session, now=datetime(2026, 7, 24, 12, tzinfo=timezone.utc))

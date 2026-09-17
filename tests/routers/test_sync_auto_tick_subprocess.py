"""The auto-sync tick must not run inside the uvicorn HTTP worker.

The bounded physical refresh is CPU-bound for tens of seconds and holds the GIL,
so the worker missed the multiprocess supervisor's liveness ping and was killed
mid-transaction on every tick. The endpoint now delegates to a child process and
passes its JSON through untouched.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers import sync as routes
from app.services import sync_tick_runner
from app.services.sync_tick_runner import SyncTickSubprocessError


def _call(db=None):
    return asyncio.run(routes.sync_auto_tick(db=db))


@pytest.fixture(autouse=True)
def _no_inprocess_flag(monkeypatch):
    monkeypatch.delenv(sync_tick_runner.ENV_INPROCESS, raising=False)
    monkeypatch.delenv(sync_tick_runner.ENV_TIMEOUT, raising=False)


# --- endpoint ---------------------------------------------------------------

def test_endpoint_delegates_to_subprocess_and_passes_json_through(monkeypatch):
    payload = {
        "status": "ok",
        "job": "physicalRefresh",
        "summary": {"target_cutoff": "2026-09-17T09:00:00+00:00", "rows": 4211},
        "duration_ms": 151234,
    }
    calls = []

    def fake_subprocess():
        calls.append("subprocess")
        return payload

    def boom(*args, **kwargs):  # the HTTP worker must never run the tick itself
        raise AssertionError("tick() ran in-process")

    monkeypatch.setattr(routes.sync_tick_runner, "run_tick_subprocess", fake_subprocess)
    monkeypatch.setattr(routes.sync_orchestrator, "tick", boom)

    result = _call()

    assert calls == ["subprocess"]
    assert result == payload


def test_endpoint_passes_busy_and_idle_shapes_through_unchanged(monkeypatch):
    monkeypatch.setattr(
        routes.sync_tick_runner,
        "run_tick_subprocess",
        lambda: {"status": "busy", "reason": "another sync is running (cluster lock)", "lock": "busy"},
    )
    assert _call() == {
        "status": "busy",
        "reason": "another sync is running (cluster lock)",
        "lock": "busy",
    }


def test_child_failure_maps_to_the_existing_error_shape(monkeypatch):
    def fail():
        raise SyncTickSubprocessError("RuntimeError: 1C refused the balance read")

    monkeypatch.setattr(routes.sync_tick_runner, "run_tick_subprocess", fail)

    with pytest.raises(HTTPException) as excinfo:
        _call()

    assert excinfo.value.status_code == 500
    assert excinfo.value.detail == (
        "Sync tick error: RuntimeError: 1C refused the balance read"
    )


def test_inprocess_flag_keeps_the_legacy_path(monkeypatch):
    monkeypatch.setenv(sync_tick_runner.ENV_INPROCESS, "1")
    seen = {}

    def fake_tick(db):
        seen["db"] = db
        return {"status": "idle", "due": 0}

    monkeypatch.setattr(routes.sync_orchestrator, "tick", fake_tick)
    monkeypatch.setattr(
        routes.sync_tick_runner,
        "run_tick_subprocess",
        lambda: pytest.fail("subprocess used while SYNC_TICK_INPROCESS=1"),
    )

    sentinel = object()
    assert _call(db=sentinel) == {"status": "idle", "due": 0}
    assert seen["db"] is sentinel


# --- child command / environment --------------------------------------------

def test_child_runs_the_same_interpreter_and_module():
    command = sync_tick_runner.build_command("/tmp/result.json")
    assert command[0] == sys.executable
    assert command[1:4] == ["-m", sync_tick_runner.MODULE_NAME, sync_tick_runner.RESULT_ARG]
    assert command[4] == "/tmp/result.json"


def test_child_env_inherits_parent_and_exposes_the_package_root(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/prodplan")
    env = sync_tick_runner.build_env()
    assert env["DATABASE_URL"] == "postgresql://u:p@db/prodplan"
    root = Path(env["PYTHONPATH"].split(os.pathsep)[0])
    assert (root / "app" / "services" / "sync_tick_runner.py").exists()


def test_timeout_reads_the_env_flag_with_a_generous_default(monkeypatch):
    assert sync_tick_runner.subprocess_timeout_seconds() == 3600.0
    monkeypatch.setenv(sync_tick_runner.ENV_TIMEOUT, "900")
    assert sync_tick_runner.subprocess_timeout_seconds() == 900.0
    monkeypatch.setenv(sync_tick_runner.ENV_TIMEOUT, "not-a-number")
    assert sync_tick_runner.subprocess_timeout_seconds() == 3600.0


# --- parent side of the spawn -----------------------------------------------

class _FakeCompleted:
    def __init__(self, returncode=0):
        self.returncode = returncode


def _stub_run(monkeypatch, writer, *, returncode=0, record=None):
    def fake_run(command, **kwargs):
        if record is not None:
            record.append((command, kwargs))
        result_path = command[command.index(sync_tick_runner.RESULT_ARG) + 1]
        writer(result_path)
        return _FakeCompleted(returncode)

    monkeypatch.setattr(sync_tick_runner.subprocess, "run", fake_run)


def test_run_tick_subprocess_returns_the_child_result(monkeypatch):
    record = []
    _stub_run(
        monkeypatch,
        lambda path: Path(path).write_text(
            json.dumps({"ok": True, "result": {"status": "ok", "job": "stock"}}),
            encoding="utf-8",
        ),
        record=record,
    )

    assert sync_tick_runner.run_tick_subprocess() == {"status": "ok", "job": "stock"}

    _command, kwargs = record[0]
    # config/odata_config.json and config/sync_schedule.json are cwd-relative.
    assert kwargs["cwd"] == os.getcwd()
    assert kwargs["timeout"] == 3600.0
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_run_tick_subprocess_cleans_up_its_result_file(monkeypatch):
    seen = {}

    def writer(path):
        seen["path"] = path
        Path(path).write_text(json.dumps({"ok": True, "result": {"status": "idle"}}), encoding="utf-8")

    _stub_run(monkeypatch, writer)
    sync_tick_runner.run_tick_subprocess()
    assert not Path(seen["path"]).exists()


def test_child_exception_surfaces_as_a_subprocess_error(monkeypatch):
    _stub_run(
        monkeypatch,
        lambda path: Path(path).write_text(
            json.dumps(
                {
                    "ok": False,
                    "error": "ValueError: balance did not converge",
                    "traceback": "Traceback ...",
                }
            ),
            encoding="utf-8",
        ),
        returncode=1,
    )

    with pytest.raises(SyncTickSubprocessError) as excinfo:
        sync_tick_runner.run_tick_subprocess()
    assert "balance did not converge" in str(excinfo.value)


def test_killed_child_without_output_is_still_reported(monkeypatch):
    _stub_run(monkeypatch, lambda path: None, returncode=-9)

    with pytest.raises(SyncTickSubprocessError) as excinfo:
        sync_tick_runner.run_tick_subprocess()
    assert "-9" in str(excinfo.value)


def test_timed_out_child_is_reported(monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])

    monkeypatch.setattr(sync_tick_runner.subprocess, "run", fake_run)

    with pytest.raises(SyncTickSubprocessError) as excinfo:
        sync_tick_runner.run_tick_subprocess(timeout=5)
    assert "timed out" in str(excinfo.value)


def test_second_concurrent_spawn_reports_busy(monkeypatch):
    def writer(path):
        # Re-entering while the first spawn still holds the guard must not fork
        # a second interpreter — it reports the orchestrator's own busy shape.
        assert sync_tick_runner.run_tick_subprocess() == {
            "status": "busy",
            "reason": "another sync is running",
        }
        Path(path).write_text(json.dumps({"ok": True, "result": {"status": "idle"}}), encoding="utf-8")

    _stub_run(monkeypatch, writer)
    assert sync_tick_runner.run_tick_subprocess() == {"status": "idle"}


# --- child side --------------------------------------------------------------

def test_child_main_writes_the_encoded_tick_result(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from app.services import sync_orchestrator as orch

    closed = []

    class _FakeSession:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(sync_tick_runner, "_open_session", lambda: _FakeSession())
    monkeypatch.setattr(
        orch,
        "tick",
        lambda db: {
            "status": "ok",
            "job": "physicalRefresh",
            # datetimes must come out ISO-encoded, exactly as FastAPI would.
            "summary": {"cutoff": datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)},
        },
    )

    out = tmp_path / "result.json"
    code = sync_tick_runner.main([sync_tick_runner.RESULT_ARG, str(out)])

    assert code == 0
    assert closed == [True]
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["result"]["job"] == "physicalRefresh"
    cutoff = payload["result"]["summary"]["cutoff"]
    assert isinstance(cutoff, str) and cutoff.startswith("2026-09-17T09:00:00")


def test_child_main_writes_the_failure_payload_and_exits_nonzero(monkeypatch, tmp_path):
    def blow_up():
        raise RuntimeError("no OData connection")

    monkeypatch.setattr(sync_tick_runner, "run_tick_inprocess", blow_up)

    out = tmp_path / "result.json"
    code = sync_tick_runner.main([sync_tick_runner.RESULT_ARG, str(out)])

    assert code == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["error"] == "RuntimeError: no OData connection"
    assert "RuntimeError" in payload["traceback"]

"""Run one auto-sync tick in a dedicated child process.

Why a child process at all
--------------------------
``POST /api/v1/sync/auto/tick`` used to call :func:`sync_orchestrator.tick`
inside the uvicorn HTTP worker.  The bounded physical refresh is CPU-bound for
tens of seconds (JSON serialisation, flushing thousands of audit rows) and holds
the GIL the whole time, so the worker's event loop could not answer the uvicorn
multiprocess supervisor's liveness ping (``timeout_worker_healthcheck``).  The
supervisor logged "Child process [pid] died", killed the worker mid-transaction
and restarted it — every tick, forever, so the candidate generation was
discarded/resumed on every attempt and physical truth never advanced.

Running the very same ``tick()`` in a separate interpreter moves that GIL out of
the HTTP worker: the worker only waits on ``subprocess``/``os.waitpid``, which
releases the GIL, so the event loop keeps answering pings.

What this module does NOT change
--------------------------------
Sync semantics are untouched.  The child calls the one canonical
:func:`sync_orchestrator.tick`, so "one due job per tick", the per-job
intervals, the physical-refresh fairness rule and the Postgres advisory lock all
behave exactly as before.  The advisory lock is what actually serialises ticks
across processes (it always did — the in-process ``threading.Lock`` never
covered uvicorn's 4 workers), so spawning a child does not weaken it.

The JSON handed back to the caller is produced with FastAPI's own
``jsonable_encoder`` inside the child, so ``sync_worker.py`` and the frontend
sync page see byte-identical payloads to the in-process path.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ``python -m`` target for the child.
MODULE_NAME = "app.services.sync_tick_runner"
RESULT_ARG = "--result-path"

ENV_TIMEOUT = "SYNC_TICK_SUBPROCESS_TIMEOUT_SECONDS"
ENV_INPROCESS = "SYNC_TICK_INPROCESS"

# Generous on purpose: a full bounded refresh on a production-size database runs
# ~2.5 min, and a cold one after a long outage much longer.  The timeout is a
# last-resort guard against a wedged child, not a work budget.
DEFAULT_TIMEOUT_SECONDS = 3600.0

# One child per HTTP worker.  Mirrors the orchestrator's own in-process guard so
# a burst of tick requests hitting the same worker reports the familiar "busy"
# shape instead of forking a second interpreter that would only bounce off the
# advisory lock.
_spawn_lock = threading.Lock()

_BUSY_RESULT: Dict[str, Any] = {"status": "busy", "reason": "another sync is running"}


class SyncTickSubprocessError(RuntimeError):
    """The child could not produce a tick result (crash, timeout, bad output).

    The router turns this into the same ``HTTP 500 {"detail": "Sync tick error:
    ..."}`` body it has always returned when ``tick()`` raised in-process.
    """


def _flag_enabled(raw: Optional[str]) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def inprocess_enabled() -> bool:
    """``SYNC_TICK_INPROCESS=1`` keeps the legacy in-process path (tests/debug)."""
    return _flag_enabled(os.getenv(ENV_INPROCESS))


def subprocess_timeout_seconds() -> float:
    raw = os.getenv(ENV_TIMEOUT)
    if raw is None or not str(raw).strip():
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a number; using %s", ENV_TIMEOUT, raw, DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return value


def _package_root() -> str:
    """Directory that contains the importable ``app`` package (``/app`` in the image)."""
    return str(Path(__file__).resolve().parents[2])


def build_command(result_path: str) -> List[str]:
    """Same interpreter, same module — no shell, no PATH lookup."""
    return [sys.executable, "-m", MODULE_NAME, RESULT_ARG, result_path]


def build_env() -> Dict[str, str]:
    """Inherit the parent environment (DATABASE_URL, TZ, OData secrets, ...).

    Only ``PYTHONPATH`` is extended, so ``-m app.services.sync_tick_runner``
    resolves even when the process was started from another directory.
    """
    env = dict(os.environ)
    root = _package_root()
    existing = env.get("PYTHONPATH") or ""
    parts = [p for p in existing.split(os.pathsep) if p]
    if root not in parts:
        parts.insert(0, root)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _read_result_file(path: str) -> Optional[Dict[str, Any]]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def run_tick_subprocess(*, timeout: Optional[float] = None) -> Dict[str, Any]:
    """Spawn the child, wait for it, return its JSON result unchanged.

    Blocking on purpose: the caller runs it off the event loop.
    """
    if not _spawn_lock.acquire(blocking=False):
        return dict(_BUSY_RESULT)
    fd, result_path = tempfile.mkstemp(prefix="sync-tick-", suffix=".json")
    os.close(fd)
    try:
        command = build_command(result_path)
        # cwd is inherited deliberately: config/odata_config.json and
        # config/sync_schedule.json are resolved relative to it, so the child
        # must read and write exactly the files the API process uses.
        # stdout/stderr are inherited too, so the tick's log lines keep landing
        # in the backend container log as they did in-process.
        try:
            completed = subprocess.run(
                command,
                cwd=os.getcwd(),
                env=build_env(),
                stdin=subprocess.DEVNULL,
                timeout=timeout if timeout is not None else subprocess_timeout_seconds(),
            )
        except subprocess.TimeoutExpired as exc:
            raise SyncTickSubprocessError(
                f"sync tick subprocess timed out after {exc.timeout}s"
            ) from exc
        except OSError as exc:
            raise SyncTickSubprocessError(
                f"sync tick subprocess could not be started: {exc}"
            ) from exc

        payload = _read_result_file(result_path)
        if payload is None:
            raise SyncTickSubprocessError(
                "sync tick subprocess produced no result "
                f"(exit code {completed.returncode})"
            )
        if payload.get("ok"):
            result = payload.get("result")
            if not isinstance(result, dict):
                raise SyncTickSubprocessError(
                    "sync tick subprocess returned a non-object result"
                )
            return result
        detail = str(payload.get("error") or "unknown error")
        child_traceback = payload.get("traceback")
        if child_traceback:
            logger.error("sync tick subprocess failed:\n%s", child_traceback)
        raise SyncTickSubprocessError(detail)
    finally:
        try:
            os.unlink(result_path)
        except OSError:
            pass
        _spawn_lock.release()


# --- child side --------------------------------------------------------------

def _open_session():
    from ..database import SessionLocal

    return SessionLocal()


def run_tick_inprocess() -> Dict[str, Any]:
    """The child's actual work: one canonical tick on its own DB session."""
    from . import sync_orchestrator

    db = _open_session()
    try:
        return sync_orchestrator.tick(db)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001 - never mask the tick result
            logger.exception("sync tick subprocess could not close its session")


def _encode(result: Dict[str, Any]) -> Any:
    """Encode exactly the way the FastAPI handler would, so the wire shape is identical."""
    try:
        from fastapi.encoders import jsonable_encoder
    except Exception:  # noqa: BLE001 - fastapi is a hard dependency; stay defensive
        return result
    return jsonable_encoder(result)


def _parse_result_path(argv: List[str]) -> Optional[str]:
    if RESULT_ARG in argv:
        index = argv.index(RESULT_ARG)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    result_path = _parse_result_path(args)
    try:
        payload: Dict[str, Any] = {"ok": True, "result": _encode(run_tick_inprocess())}
        code = 0
    except BaseException as exc:  # noqa: BLE001 - the parent needs the reason, always
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
        code = 1
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if result_path:
        Path(result_path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    return code


if __name__ == "__main__":  # pragma: no cover - exercised through the subprocess
    sys.exit(main())

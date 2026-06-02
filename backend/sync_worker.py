"""Standalone auto-sync worker.

Runs in its own container (docker-compose `sync-worker`). On a short cadence it
POSTs to the backend's `/api/v1/sync/auto/tick`, which runs at most one due 1C
sync job. The tick interval is the stagger spacing: with one job per tick there
is never more than one OData pull in flight, so the 1C server is not hammered.

Dependency-free (stdlib only) so it runs on the plain backend image. The tick is
idempotent and self-throttling (per-job intervals), so an occasional double-fire
or a missed tick is harmless.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[sync-worker {ts}] {msg}", flush=True)


def _post(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def main() -> int:
    base_url = os.getenv("SYNC_BASE_URL", "http://backend:8000").rstrip("/")
    url = f"{base_url}/api/v1/sync/auto/tick"
    # Stagger spacing between jobs. 120s → at most one OData pull every 2 min.
    interval = int(os.getenv("SYNC_TICK_INTERVAL_SECONDS", "120"))
    timeout = float(os.getenv("SYNC_TICK_TIMEOUT_SECONDS", "600"))
    startup_delay = int(os.getenv("SYNC_STARTUP_DELAY_SECONDS", "30"))

    _log(f"start: url={url} interval={interval}s startup_delay={startup_delay}s")
    if startup_delay > 0:
        time.sleep(startup_delay)

    while True:
        started = time.time()
        try:
            result = _post(url, timeout)
            status = result.get("status")
            if status in ("ok", "error"):
                _log(
                    "tick: status={s} job={j} duration_ms={d} error={e}".format(
                        s=status, j=result.get("job"),
                        d=result.get("duration_ms"), e=result.get("error"),
                    )
                )
            else:
                # idle / busy / skipped — quiet, log only occasionally would spam; keep terse.
                _log(f"tick: {status} {result.get('reason') or ''}".strip())
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8")
            except Exception:
                pass
            _log(f"HTTP {exc.code} error: {body[:500]}")
        except Exception as exc:  # noqa: BLE001 — keep the loop alive on any blip
            _log(f"error: {exc}")

        elapsed = time.time() - started
        time.sleep(max(1.0, interval - elapsed))


if __name__ == "__main__":
    sys.exit(main())

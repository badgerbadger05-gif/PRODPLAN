"""Standalone MRP-reconciliation worker.

Runs in its own container (see docker-compose `reconcile-worker`). Every
RECONCILE_INTERVAL_SECONDS it POSTs to the backend's `/api/v1/plan/reconcile`
endpoint, which recomputes residual demand on active snapshots and tops up the
gap (production journal lines + purchase rows). Nothing is written to 1C — that
stays a user action.

Kept dependency-free (stdlib only) so it can run on the plain backend image.
The endpoint itself is idempotent, so an occasional double-fire is harmless.
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
    print(f"[reconcile-worker {ts}] {msg}", flush=True)


def _post(url: str, timeout: float) -> dict:
    data = json.dumps({"dry_run": False}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def main() -> int:
    base_url = os.getenv("RECONCILE_BASE_URL", "http://backend:8000").rstrip("/")
    url = f"{base_url}/api/v1/plan/reconcile"
    interval = int(os.getenv("RECONCILE_INTERVAL_SECONDS", "10800"))  # 3h
    timeout = float(os.getenv("RECONCILE_TIMEOUT_SECONDS", "300"))
    startup_delay = int(os.getenv("RECONCILE_STARTUP_DELAY_SECONDS", "60"))

    _log(f"start: url={url} interval={interval}s startup_delay={startup_delay}s")
    if startup_delay > 0:
        time.sleep(startup_delay)

    while True:
        started = time.time()
        try:
            result = _post(url, timeout)
            _log(
                "reconciled: runs_checked={runs} production_added={prod} "
                "purchase_added={purch}".format(
                    runs=result.get("runs_checked"),
                    prod=result.get("production_lines_added"),
                    purch=result.get("purchase_lines_added"),
                )
            )
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

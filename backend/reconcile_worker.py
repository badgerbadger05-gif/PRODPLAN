"""Retired legacy MRP-reconciliation worker.

Accepted Item Ledger generations and their persisted read snapshots are the
only planning truth. The old timer bypassed the atomic candidate/publish flow.
This tombstone remains for old scripts, but performs no HTTP request.
"""
from __future__ import annotations

import sys
import time


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[reconcile-worker {ts}] {msg}", flush=True)


def main() -> int:
    _log(
        "retired: legacy live MRP reconcile is disabled; "
        "use the Ledger obligation-refresh orchestrator"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

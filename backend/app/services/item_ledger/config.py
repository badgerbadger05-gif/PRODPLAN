"""Item-ledger runtime configuration — the Inc5 reader feature flag.

Inc5 flips the *stock on-hand source* from the legacy warehouse tables
(``ItemWarehouseStock`` / ``Item.stock_qty``) to the physical ledger's
``stock_bin`` pool, gated by a single env var so the DEFAULT is byte-identical
to today.

``STOCK_SOURCE`` ∈ {``legacy`` (default), ``bin``}. The flag is read once per
call via :func:`stock_source` — no module-level caching, so tests (and ops) can
flip it with ``monkeypatch.setenv`` / ``STOCK_SOURCE=bin`` without a reimport.

With ``STOCK_SOURCE`` unset or anything other than ``bin`` the whole increment
is inert: every reader keeps its legacy path and the full suite stays green
unchanged.
"""

from __future__ import annotations

import os

STOCK_SOURCE_ENV = "STOCK_SOURCE"
STOCK_SOURCE_LEGACY = "legacy"
STOCK_SOURCE_BIN = "bin"


def stock_source() -> str:
    """Return the active stock source, normalised to ``legacy`` | ``bin``.

    Read once from the environment on every call (no caching). Any value other
    than an exact case-insensitive ``bin`` resolves to ``legacy`` — an unknown
    flag never silently switches the source.
    """
    raw = os.environ.get(STOCK_SOURCE_ENV)
    if raw is None:
        return STOCK_SOURCE_LEGACY
    return STOCK_SOURCE_BIN if str(raw).strip().lower() == STOCK_SOURCE_BIN else STOCK_SOURCE_LEGACY


def use_bin_stock() -> bool:
    """True when ``STOCK_SOURCE=bin`` — the Inc5 ledger-sourced reader path."""
    return stock_source() == STOCK_SOURCE_BIN

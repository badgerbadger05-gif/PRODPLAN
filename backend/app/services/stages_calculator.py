from __future__ import annotations

from dataclasses import dataclass, asdict  # kept for compatibility
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session


def _read_last_stock_sync_at() -> Optional[str]:
    p = Path("config") / "last_sync_time.json"
    if not p.exists():
        return None
    try:
        import json
        data = json.loads(p.read_text("utf-8") or "{}")
        val = str(data.get("last_sync") or "").strip()
        return val or None
    except Exception:
        return None


def calculate_stages(db: Session) -> Dict[str, Any]:
    # Compatibility shim: stage-based calculation logic has been moved
    # to resource_calculator. We return an empty structure here to keep
    # API import working and allow the backend to start.
    return {
        "asOf": _read_last_stock_sync_at(),
        "stages": []
    }
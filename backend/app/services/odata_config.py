from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


CONFIG_PATH = Path("config") / "odata_config.json"


def sanitize_base_url(value: str) -> str:
    base_url = (value or "").strip().rstrip("/")
    if base_url.lower().endswith("$metadata"):
        base_url = base_url[: -len("$metadata")].rstrip("/")
    return base_url


def load_odata_config() -> Dict[str, Any]:
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text("utf-8") or "{}")
    except Exception:
        pass
    return {}


def save_odata_config(data: Dict[str, Any]) -> Dict[str, Any]:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    clean = dict(data or {})
    clean["base_url"] = sanitize_base_url(str(clean.get("base_url") or ""))
    CONFIG_PATH.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean

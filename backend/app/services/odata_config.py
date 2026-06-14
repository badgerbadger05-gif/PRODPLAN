from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


CONFIG_PATH = Path("config") / "odata_config.json"

# Placeholder the API returns instead of real secrets. The SPA loads the config
# into an editable form and posts it back, so an inbound secret equal to this
# sentinel means "keep the stored value" (never overwrite a real password/token
# with the mask).
MASKED_SECRET = "***"


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


def mask_odata_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """Возвращает копию конфига с замаскированными секретами для отдачи в HTTP-ответ.

    Не мутирует исходный словарь. password/token: "" если пусто, иначе "***".
    Реальные значения остаются только во внутреннем использовании и при записи на диск.
    """
    masked = dict(data or {})
    for key in ("password", "token"):
        if key in masked:
            masked[key] = MASKED_SECRET if masked.get(key) else ""
    return masked


def resolve_config_secrets(data: Dict[str, Any]) -> Dict[str, Any]:
    """Replace masked secrets in an inbound config with the stored real values.

    The API never returns real password/token (see mask_odata_config); when a
    caller posts back the "***" placeholder it means "unchanged", so we restore
    the stored secret instead of treating "***" as the new password.
    """
    resolved = dict(data or {})
    stored = load_odata_config()
    for key in ("password", "token"):
        if resolved.get(key) == MASKED_SECRET:
            resolved[key] = stored.get(key, "")
    return resolved


def save_odata_config(data: Dict[str, Any]) -> Dict[str, Any]:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    clean = resolve_config_secrets(data)
    clean["base_url"] = sanitize_base_url(str(clean.get("base_url") or ""))
    CONFIG_PATH.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean

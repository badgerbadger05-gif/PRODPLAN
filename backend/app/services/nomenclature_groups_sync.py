"""Refresh the nomenclature *group* (folder) list from 1C.

These are `Catalog_Номенклатура` rows with `IsFolder eq true` — the folder tree
used by the group-selection UI. They are NOT items and are not touched by the
regular nomenclature item sync. The available-group list is cached in
`output/odata_groups_nomenclature.json`; the user's selection
(`config/odata_groups_selected.json`) is a separate, manual choice and is never
modified here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .odata_config import sanitize_base_url
from .odata_client import OData1CClient


OUTPUT_DIR = Path("output")
GROUPS_JSON = OUTPUT_DIR / "odata_groups_nomenclature.json"


def refresh_nomenclature_groups(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pull nomenclature folders (`IsFolder eq true`) and cache them. Read-only on
    the 1C side. Returns a small summary dict.
    """
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("base_url is required")

    client = OData1CClient(
        base_url=sanitize_base_url(base_url),
        username=config.get("username") or None,
        password=config.get("password") or None,
        token=config.get("token") or None,
    )
    rows = client.get_all(
        "Catalog_Номенклатура",
        filter_query="IsFolder eq true",
        select_fields=["Ref_Key", "Code", "Description", "IsFolder"],
        top=1000,
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    GROUPS_JSON.write_text(
        json.dumps({"value": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"status": "ok", "total": len(rows), "file": str(GROUPS_JSON)}

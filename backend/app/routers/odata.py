from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.odata_config import (
    load_odata_config,
    mask_odata_config,
    sanitize_base_url,
    save_odata_config,
)
from ..services.odata_client import OData1CClient
from ..services.nomenclature_groups_sync import GROUPS_JSON, OUTPUT_DIR, refresh_nomenclature_groups

router = APIRouter(prefix="/v1/odata", tags=["odata"])

GROUPS_SELECTED = Path("config") / "odata_groups_selected.json"


class ODataConfig(BaseModel):
    base_url: str
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None


class GroupsSelection(BaseModel):
    ids: List[str]


def _load_config() -> Dict[str, Any]:
    return load_odata_config()


def _save_config(data: Dict[str, Any]) -> Dict[str, Any]:
    return save_odata_config(data)


@router.get("/config")
def get_config():
    """Возвращает сохранённую конфигурацию OData (секреты замаскированы)."""
    return mask_odata_config(_load_config())


@router.post("/config")
def save_config(cfg: ODataConfig):
    """Сохраняет конфигурацию OData."""
    saved = _save_config(cfg.model_dump())
    return {"status": "ok", "config": mask_odata_config(saved)}


@router.post("/test")
def test_connection(cfg: Optional[ODataConfig] = None):
    """Проверка подключения к OData ($metadata)."""
    data = cfg.model_dump() if cfg is not None else _load_config()
    base_url = data.get("base_url")
    if not base_url:
        raise HTTPException(status_code=400, detail="base_url is required")
    client = OData1CClient(
        base_url=sanitize_base_url(base_url),
        username=data.get("username") or None,
        password=data.get("password") or None,
        token=data.get("token") or None,
    )
    try:
        resp = client._make_request("$metadata")
        if isinstance(resp, dict) and "_raw" in resp:
            raw = str(resp.get("_raw") or "")
            size = len(raw.encode("utf-8", "ignore"))
            return {"status": "ok", "bytes": size, "type": "xml"}
        size = len(json.dumps(resp, ensure_ascii=False))
        return {"status": "ok", "bytes": size, "type": "json"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OData test failed: {e}")


@router.post("/metadata")
def fetch_metadata(cfg: Optional[ODataConfig] = None):
    """Выгружает $metadata в output/odata_metadata.xml и краткое summary в output/odata_metadata_summary.json."""
    data = cfg.model_dump() if cfg is not None else _load_config()
    base_url = data.get("base_url")
    if not base_url:
        raise HTTPException(status_code=400, detail="base_url is required")
    client = OData1CClient(
        base_url=sanitize_base_url(base_url),
        username=data.get("username") or None,
        password=data.get("password") or None,
        token=data.get("token") or None,
    )
    try:
        resp = client._make_request("$metadata")
        if isinstance(resp, dict) and "_raw" in resp:
            xml_text = str(resp.get("_raw") or "")
        else:
            xml_text = f"<!-- non-XML response -->\n{json.dumps(resp, ensure_ascii=False, indent=2)}"
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_xml = OUTPUT_DIR / "odata_metadata.xml"
        out_sum = OUTPUT_DIR / "odata_metadata_summary.json"
        out_xml.write_text(xml_text, encoding="utf-8")

        # Простое извлечение EntitySets и EntityType имён
        summary = {"entities": [], "entity_sets": []}  # type: Dict[str, List[str]]
        try:
            for line in xml_text.splitlines():
                s = line.strip()
                if "EntitySet Name=" in s and "EntityType=" in s:
                    i = s.find('Name="') + 6
                    j = s.find('"', i)
                    if i > 5 and j > i:
                        summary["entity_sets"].append(s[i:j])
                elif "<EntityType Name=" in s:
                    i = s.find('Name="') + 6
                    j = s.find('"', i)
                    if i > 5 and j > i:
                        summary["entities"].append(s[i:j])
        except Exception:
            pass
        out_sum.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "ok", "xml": str(out_xml), "entity_sets": len(summary.get("entity_sets", []))}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Metadata fetch failed: {e}")


@router.post("/categories/export_groups")
def export_groups(cfg: Optional[ODataConfig] = None):
    """
    Выгружает группы номенклатуры (IsFolder eq true) в output/odata_groups_nomenclature.json.
    """
    data = cfg.model_dump() if cfg is not None else _load_config()
    if not data.get("base_url"):
        raise HTTPException(status_code=400, detail="base_url is required")
    try:
        return refresh_nomenclature_groups(data)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Export groups failed: {e}")


@router.get("/groups")
def get_saved_groups():
    """Возвращает сохранённые группы номенклатуры из файла."""
    if not GROUPS_JSON.exists():
        return {"value": []}
    try:
        data = json.loads(GROUPS_JSON.read_text("utf-8") or "{}")
        if isinstance(data, dict) and "value" in data:
            return {"value": data.get("value") or []}
        return {"value": data or []}
    except Exception:
        return {"value": []}


@router.get("/groups/selection")
def get_groups_selection():
    """Возвращает выбранные Ref_Key групп (для индексации)."""
    if GROUPS_SELECTED.exists():
        try:
            ids = json.loads(GROUPS_SELECTED.read_text("utf-8") or "[]")
            if isinstance(ids, list):
                return {"ids": [str(x) for x in ids]}
        except Exception:
            pass
    return {"ids": []}


@router.post("/groups/selection")
def save_groups_selection(payload: GroupsSelection):
    """Сохраняет выбранные Ref_Key групп в config/odata_groups_selected.json."""
    GROUPS_SELECTED.parent.mkdir(parents=True, exist_ok=True)
    GROUPS_SELECTED.write_text(json.dumps(payload.ids, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "ok", "saved": len(payload.ids)}

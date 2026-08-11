from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from ..services.odata_config import (
    load_odata_config,
    mask_odata_config,
    resolve_config_secrets,
    sanitize_base_url,
    save_odata_config,
)
from ..services.odata_client import OData1CClient
from ..services.nomenclature_groups_sync import GROUPS_JSON, OUTPUT_DIR, refresh_nomenclature_groups
from ..services.operation_norm_exports import (
    OPERATION_RATE_FIELDS,
    RELEASE_FACT_FIELDS,
    default_date_from,
    export_operation_rates,
    export_release_facts,
    rows_to_csv,
)

router = APIRouter(prefix="/v1/odata", tags=["odata"])

GROUPS_SELECTED = Path("config") / "odata_groups_selected.json"


class ODataConfig(BaseModel):
    base_url: str
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None


class GroupsSelection(BaseModel):
    ids: List[str]


class NomenclatureGroupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    code: str
    name: str


class NomenclatureGroupsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[NomenclatureGroupResponse]
    selected_ids: list[str]


def _load_config() -> Dict[str, Any]:
    return load_odata_config()


def _save_config(data: Dict[str, Any]) -> Dict[str, Any]:
    return save_odata_config(data)


def _config_from_request(cfg: Optional["ODataConfig"]) -> Dict[str, Any]:
    """Use the posted config (with masked secrets restored from storage) or the
    stored one when the request body is empty."""
    if cfg is None:
        return _load_config()
    return resolve_config_secrets(cfg.model_dump())


def _client_from_config(data: Dict[str, Any]) -> OData1CClient:
    base_url = data.get("base_url")
    if not base_url:
        raise HTTPException(status_code=400, detail="base_url is required")
    return OData1CClient(
        base_url=sanitize_base_url(base_url),
        username=data.get("username") or None,
        password=data.get("password") or None,
        token=data.get("token") or None,
    )


def _csv_response(csv_text: str, filename: str) -> StreamingResponse:
    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
    data = _config_from_request(cfg)
    client = _client_from_config(data)
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
    data = _config_from_request(cfg)
    client = _client_from_config(data)
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
    data = _config_from_request(cfg)
    if not data.get("base_url"):
        raise HTTPException(status_code=400, detail="base_url is required")
    try:
        return refresh_nomenclature_groups(data)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Export groups failed: {e}")


@router.post("/exports/operation-rates.csv")
def export_operation_rates_csv(
    cfg: Optional[ODataConfig] = None,
    max_rows: Optional[int] = Query(None, ge=1),
    page_size: int = Query(1000, ge=1, le=5000),
):
    """
    Read-only export of 1C specification operation rows with operation rates.

    Uses the saved OData config when request body is empty/null. Secrets are
    never returned in the response.
    """
    data = _config_from_request(cfg)
    client = _client_from_config(data)
    try:
        rows = export_operation_rates(client, max_rows=max_rows, page_size=page_size)
        return _csv_response(rows_to_csv(rows, OPERATION_RATE_FIELDS), "operation_rates_1c.csv")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Operation rates export failed: {e}")


@router.post("/exports/release-facts.csv")
def export_release_facts_csv(
    cfg: Optional[ODataConfig] = None,
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    max_rows: Optional[int] = Query(None, ge=1),
    page_size: int = Query(1000, ge=1, le=5000),
):
    """
    Read-only export of factual production release from
    AccumulationRegister_ВыпускПродукции_RecordType, aggregated by
    item/characteristic/specification for the selected period.
    """
    effective_to = date_to or date.today()
    effective_from = date_from or default_date_from(effective_to)
    if effective_from > effective_to:
        raise HTTPException(status_code=400, detail="date_from must be before or equal to date_to")

    data = _config_from_request(cfg)
    client = _client_from_config(data)
    try:
        rows = export_release_facts(
            client,
            date_from=effective_from,
            date_to=effective_to,
            max_rows=max_rows,
            page_size=page_size,
        )
        filename = f"release_facts_1c_{effective_from.isoformat()}_{effective_to.isoformat()}.csv"
        return _csv_response(rows_to_csv(rows, RELEASE_FACT_FIELDS), filename)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Release facts export failed: {e}")


def _load_selected_group_ids() -> list[str]:
    if GROUPS_SELECTED.exists():
        try:
            ids = json.loads(GROUPS_SELECTED.read_text("utf-8") or "[]")
            if isinstance(ids, list):
                return [str(value) for value in ids]
        except Exception:
            pass
    return []


@router.get("/groups", response_model=NomenclatureGroupsResponse)
def get_saved_groups() -> NomenclatureGroupsResponse:
    """Canonical UI view over the independent 1C cache and manual selection."""
    raw_rows: list[Any] = []
    if not GROUPS_JSON.exists():
        data: Any = []
    else:
        try:
            data = json.loads(GROUPS_JSON.read_text("utf-8") or "{}")
        except Exception:
            data = []
    if isinstance(data, dict):
        candidate = data.get("value")
        raw_rows = candidate if isinstance(candidate, list) else []
    elif isinstance(data, list):
        raw_rows = data

    items = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        group_id = str(row.get("Ref_Key") or "").strip()
        if not group_id:
            continue
        items.append(
            {
                "id": group_id,
                "code": str(row.get("Code") or ""),
                "name": str(row.get("Description") or ""),
            }
        )
    return NomenclatureGroupsResponse.model_validate(
        {"items": items, "selected_ids": _load_selected_group_ids()}
    )


@router.get("/groups/selection")
def get_groups_selection():
    """Возвращает выбранные Ref_Key групп (для индексации)."""
    return {"ids": _load_selected_group_ids()}


@router.post("/groups/selection")
def save_groups_selection(payload: GroupsSelection):
    """Сохраняет выбранные Ref_Key групп в config/odata_groups_selected.json."""
    GROUPS_SELECTED.parent.mkdir(parents=True, exist_ok=True)
    GROUPS_SELECTED.write_text(json.dumps(payload.ids, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "ok", "saved": len(payload.ids)}

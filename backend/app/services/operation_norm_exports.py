from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .odata_client import OData1CClient
from .one_c_export_common import clean_ref1c
from .operations_sync import _extract_operation_price, _s, _to_float


SPEC_OPERATIONS_ENTITY = "Catalog_Спецификации_Операции"
SPEC_ENTITY = "Catalog_Спецификации"
PRODUCTION_KIND_ENTITY = "Catalog_ВидыПроизводства"
PRICE_KIND_ENTITY = "Catalog_ВидыЦен"
NOMENCLATURE_PRICE_REGISTER = "InformationRegister_ЦеныНоменклатуры"
RELEASE_REGISTER_ENTITY = "AccumulationRegister_ВыпускПродукции_RecordType"
DEFAULT_ACCOUNTING_PRICE_TYPE_REF = "81c4a02c-991b-11eb-e39a-fa163e61326a"


OPERATION_RATE_FIELDS = [
    "spec_ref1c",
    "spec_code_1c",
    "spec_name_1c",
    "item_ref1c",
    "production_kind_ref1c",
    "production_kind_name_1c",
    "sequence_id",
    "operation_ref1c",
    "operation_name_1c",
    "stage_ref1c",
    "operation_erpnext",
    "operation_rate",
    "operation_rate_source",
    "source_time_norm",
    "source_time_unit",
]

RELEASE_FACT_FIELDS = [
    "item_ref1c",
    "characteristic_ref1c",
    "spec_ref1c",
    "fact_release_qty",
    "records_count",
    "date_from",
    "date_to",
]


def default_date_from(today: Optional[date] = None) -> date:
    base = today or date.today()
    return base - timedelta(days=365)


def _date_literal(value: date) -> str:
    return datetime.combine(value, datetime.min.time()).replace(microsecond=0).isoformat()


def _next_date(value: date) -> date:
    return value + timedelta(days=1)


def _row_value(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row:
            value = row.get(key)
            if value is not None and str(value).strip() != "":
                return value
    return ""


def _line_number(row: Dict[str, Any]) -> str:
    return _s(_row_value(row, "LineNumber", "НомерСтроки", "line_number", "sequence_id"))


def _load_specs(client: OData1CClient) -> Dict[str, Dict[str, str]]:
    specs: Dict[str, Dict[str, str]] = {}
    try:
        rows = client.get_all(
            SPEC_ENTITY,
            select_fields=["Ref_Key", "Code", "Description", "Owner_Key", "ВидПроизводства_Key"],
            top=1000,
            max_pages=10000,
            order_by="Ref_Key",
        )
    except Exception:
        rows = client.get_all(
            SPEC_ENTITY,
            select_fields=["Ref_Key", "Code", "Description", "ВидПроизводства_Key"],
            top=1000,
            max_pages=10000,
            order_by="Ref_Key",
        )
    for row in rows:
        ref = clean_ref1c(row.get("Ref_Key"))
        if not ref:
            continue
        specs[ref] = {
            "spec_code_1c": _s(row.get("Code")),
            "spec_name_1c": _s(row.get("Description")),
            "item_ref1c": clean_ref1c(row.get("Owner_Key")),
            "production_kind_ref1c": clean_ref1c(row.get("ВидПроизводства_Key")),
        }
    return specs


def _load_production_kinds(client: OData1CClient) -> Dict[str, str]:
    kinds: Dict[str, str] = {}
    try:
        rows = client.get_all(
            PRODUCTION_KIND_ENTITY,
            select_fields=["Ref_Key", "Description"],
            top=1000,
            max_pages=1000,
            order_by="Ref_Key",
        )
    except Exception:
        return kinds
    for row in rows:
        ref = clean_ref1c(row.get("Ref_Key"))
        if ref:
            kinds[ref] = _s(row.get("Description"))
    return kinds


def _load_accounting_price_type_ref(client: OData1CClient) -> str:
    try:
        rows = client.get_all(
            PRICE_KIND_ENTITY,
            select_fields=["Ref_Key", "Description"],
            top=1000,
            max_pages=10,
            order_by="Description",
        )
    except Exception:
        return DEFAULT_ACCOUNTING_PRICE_TYPE_REF

    for row in rows:
        name = _s(row.get("Description")).casefold().replace("ё", "е")
        if name == "учетная цена":
            return clean_ref1c(row.get("Ref_Key")) or DEFAULT_ACCOUNTING_PRICE_TYPE_REF
    for row in rows:
        name = _s(row.get("Description")).casefold().replace("ё", "е")
        if "учет" in name:
            return clean_ref1c(row.get("Ref_Key")) or DEFAULT_ACCOUNTING_PRICE_TYPE_REF
    return DEFAULT_ACCOUNTING_PRICE_TYPE_REF


def _load_accounting_prices(client: OData1CClient) -> Dict[str, float]:
    price_type_ref = _load_accounting_price_type_ref(client)
    entity = f"{NOMENCLATURE_PRICE_REGISTER}/SliceLast(Period=datetime'{_date_literal(date.today())}')"
    try:
        rows = client.get_all(
            entity,
            filter_query=f"ВидЦен_Key eq guid'{price_type_ref}'",
            select_fields=["Period", "ВидЦен_Key", "Номенклатура_Key", "Цена"],
            top=5000,
            max_pages=1000,
            order_by=None,
        )
    except Exception:
        return {}

    prices: Dict[str, float] = {}
    for row in rows:
        item_ref = clean_ref1c(row.get("Номенклатура_Key"))
        price = _to_float(row.get("Цена"), 0.0)
        if item_ref and price > 0:
            prices[item_ref] = price
    return prices


def _fetch_navigation_record(
    client: OData1CClient,
    nav_url: str,
    select_fields: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not nav_url:
        return None
    try:
        params = {"$select": select_fields} if select_fields else None
        resp = client._make_request(nav_url, params)
    except Exception:
        try:
            resp = client._make_request(nav_url, None)
        except Exception:
            return None
    return resp if isinstance(resp, dict) else None


def _operation_info(
    client: OData1CClient,
    row: Dict[str, Any],
    cache: Dict[str, Dict[str, Any]],
    accounting_prices: Dict[str, float],
) -> Dict[str, Any]:
    op_ref = clean_ref1c(row.get("Операция_Key"))
    if not op_ref:
        return {}
    if op_ref in cache:
        return cache[op_ref]

    info: Dict[str, Any] = {
        "operation_name_1c": "",
        "source_time_norm": _to_float(row.get("НормаВремени"), 0.0),
        "operation_rate": _extract_operation_price(row),
        "operation_rate_source": "row" if _extract_operation_price(row) > 0 else "",
    }

    rec = _fetch_navigation_record(
        client,
        _s(row.get("Операция@navigationLinkUrl")),
        "Ref_Key,Code,Description,НормаВремени,Цена,Расценка,УчетнаяЦена,Цены",
    )
    if rec:
        info["operation_name_1c"] = _s(rec.get("Description") or rec.get("Code"))
        nav_time = _to_float(rec.get("НормаВремени"), 0.0)
        if nav_time > 0:
            info["source_time_norm"] = nav_time
        if not info["operation_rate"]:
            nav_price = _extract_operation_price(rec)
            if nav_price > 0:
                info["operation_rate"] = nav_price
                info["operation_rate_source"] = "operation_card"

    if not info["operation_rate"]:
        price = accounting_prices.get(op_ref, 0.0)
        if price > 0:
            info["operation_rate"] = price
            info["operation_rate_source"] = "price_register_accounting"

    cache[op_ref] = info
    return info


def _stage_name(client: OData1CClient, row: Dict[str, Any], cache: Dict[str, str]) -> str:
    stage_ref = clean_ref1c(row.get("Этап_Key"))
    if not stage_ref:
        return ""
    if stage_ref in cache:
        return cache[stage_ref]
    rec = _fetch_navigation_record(client, _s(row.get("Этап@navigationLinkUrl")), "Ref_Key,Code,Description")
    name = ""
    if rec:
        name = _s(rec.get("Description") or rec.get("Code"))
    cache[stage_ref] = name
    return name


def export_operation_rates(
    client: OData1CClient,
    *,
    max_rows: Optional[int] = None,
    page_size: int = 1000,
) -> List[Dict[str, Any]]:
    specs = _load_specs(client)
    production_kinds = _load_production_kinds(client)
    accounting_prices = _load_accounting_prices(client)
    op_cache: Dict[str, Dict[str, Any]] = {}
    stage_cache: Dict[str, str] = {}
    rows_out: List[Dict[str, Any]] = []

    processed = 0
    for page in client.iter_pages(
        SPEC_OPERATIONS_ENTITY,
        select_fields=None,
        top=max(1, int(page_size or 1000)),
        max_pages=10000,
        order_by="Ref_Key",
    ):
        for row in page:
            if max_rows is not None and processed >= int(max_rows):
                return rows_out
            processed += 1

            spec_ref = clean_ref1c(row.get("Ref_Key"))
            spec = specs.get(spec_ref, {})
            production_kind_ref = spec.get("production_kind_ref1c", "")
            op_info = _operation_info(client, row, op_cache, accounting_prices)
            erp_operation = _stage_name(client, row, stage_cache)

            rows_out.append(
                {
                    "spec_ref1c": spec_ref,
                    "spec_code_1c": spec.get("spec_code_1c", ""),
                    "spec_name_1c": spec.get("spec_name_1c", ""),
                    "item_ref1c": spec.get("item_ref1c", ""),
                    "production_kind_ref1c": production_kind_ref,
                    "production_kind_name_1c": production_kinds.get(production_kind_ref, ""),
                    "sequence_id": _line_number(row),
                    "operation_ref1c": clean_ref1c(row.get("Операция_Key")),
                    "operation_name_1c": op_info.get("operation_name_1c", ""),
                    "stage_ref1c": clean_ref1c(row.get("Этап_Key")),
                    "operation_erpnext": erp_operation,
                    "operation_rate": op_info.get("operation_rate", 0.0),
                    "operation_rate_source": op_info.get("operation_rate_source", ""),
                    "source_time_norm": op_info.get("source_time_norm", _to_float(row.get("НормаВремени"), 0.0)),
                    "source_time_unit": "hours",
                }
            )

    return rows_out


def export_release_facts(
    client: OData1CClient,
    *,
    date_from: date,
    date_to: date,
    max_rows: Optional[int] = None,
    page_size: int = 1000,
) -> List[Dict[str, Any]]:
    filter_query = (
        f"Period ge datetime'{_date_literal(date_from)}' and "
        f"Period lt datetime'{_date_literal(_next_date(date_to))}' and "
        "Active eq true"
    )
    select_fields = [
        "Period",
        "Active",
        "Номенклатура_Key",
        "Характеристика_Key",
        "Спецификация_Key",
        "Количество",
    ]
    totals: Dict[Tuple[str, str, str], Dict[str, Any]] = defaultdict(
        lambda: {"fact_release_qty": 0.0, "records_count": 0}
    )

    processed = 0
    for page in client.iter_pages(
        RELEASE_REGISTER_ENTITY,
        filter_query=filter_query,
        select_fields=select_fields,
        top=max(1, int(page_size or 1000)),
        max_pages=10000,
        order_by="Period",
    ):
        for row in page:
            if max_rows is not None and processed >= int(max_rows):
                break
            processed += 1

            item_ref = clean_ref1c(row.get("Номенклатура_Key"))
            spec_ref = clean_ref1c(row.get("Спецификация_Key"))
            characteristic_ref = clean_ref1c(row.get("Характеристика_Key"))
            qty = _to_float(row.get("Количество"), 0.0)
            if not item_ref or qty == 0:
                continue

            bucket = totals[(item_ref, characteristic_ref, spec_ref)]
            bucket["fact_release_qty"] += qty
            bucket["records_count"] += 1
        if max_rows is not None and processed >= int(max_rows):
            break

    rows_out: List[Dict[str, Any]] = []
    for (item_ref, characteristic_ref, spec_ref), total in sorted(totals.items()):
        rows_out.append(
            {
                "item_ref1c": item_ref,
                "characteristic_ref1c": characteristic_ref,
                "spec_ref1c": spec_ref,
                "fact_release_qty": total["fact_release_qty"],
                "records_count": total["records_count"],
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
            }
        )
    return rows_out


def rows_to_csv(rows: Iterable[Dict[str, Any]], fieldnames: List[str]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()

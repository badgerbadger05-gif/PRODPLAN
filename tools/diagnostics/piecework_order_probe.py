from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Any, Dict, Optional


ENTITY = "Document_СдельныйНаряд"


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name) or default).strip()


def _base_url() -> str:
    return _env("ODATA_BASE_URL", "http://mtzw7/unf_demo/odata/standard.odata").rstrip("/")


def _headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/json;odata.metadata=minimal",
        "Content-Type": "application/json",
    }
    token = _env("ODATA_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        return headers

    username = _env("ODATA_USERNAME")
    password = _env("ODATA_PASSWORD")
    if username and password:
        raw = f"{username}:{password}".encode("utf-8")
        headers["Authorization"] = f"Basic {base64.b64encode(raw).decode('ascii')}"
    return headers


def _request(method: str, endpoint: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    endpoint_quoted = urllib.parse.quote(endpoint.lstrip("/"), safe="$()_-,.=/'")
    url = f"{_base_url()}/{endpoint_quoted}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in _headers().items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            text = response.read().decode("utf-8", errors="replace").strip()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {details}") from exc


def _dt(value: Optional[str]) -> str:
    if value:
        return datetime.fromisoformat(value).replace(microsecond=0).isoformat()
    return datetime.combine(date.today(), datetime.min.time()).isoformat()


def _non_empty(value: Optional[str]) -> Optional[str]:
    clean = str(value or "").strip()
    return clean or None


def build_payload(args: argparse.Namespace) -> Dict[str, Any]:
    when = _dt(args.date)
    order_ref = _non_empty(args.order_ref)
    unit_ref = _non_empty(args.structural_unit_ref)
    link_key = int(args.link_key)
    qty = float(args.qty)
    norm = float(args.time_norm)
    price = float(args.price)
    hours = qty * norm
    cost = qty * price

    operation_row: Dict[str, Any] = {
        "LineNumber": 1,
        "Период": when,
        "Номенклатура_Key": args.item_ref,
        "Операция_Key": args.operation_ref,
        "КоличествоПлан": qty,
        "КоличествоФакт": qty,
        "НормаВремени": norm,
        "Расценка": price,
        "Нормочасы": hours,
        "Стоимость": cost,
        "КлючСвязи": link_key,
    }
    if order_ref:
        operation_row["ЗаказНаПроизводство_Key"] = order_ref
    if unit_ref:
        operation_row["СтруктурнаяЕдиница_Key"] = unit_ref
    if _non_empty(args.spec_ref):
        operation_row["Спецификация_Key"] = args.spec_ref
    if _non_empty(args.stage_ref):
        operation_row["Этап_Key"] = args.stage_ref
    if _non_empty(args.unit):
        operation_row["ЕдиницаИзмерения"] = args.unit

    executor_ref = _non_empty(args.executor_ref) or _non_empty(args.employee_ref)
    if executor_ref:
        operation_row["Исполнитель"] = executor_ref
        operation_row["Исполнитель_Type"] = args.executor_type

    payload: Dict[str, Any] = {
        "Number": args.number,
        "Date": when,
        "Posted": False,
        "Закрыт": False,
        "Комментарий": args.comment,
        "Операции": [operation_row],
    }
    if order_ref:
        payload["ЗаказНаПроизводство_Key"] = order_ref
    if _non_empty(args.organization_ref):
        payload["Организация_Key"] = args.organization_ref
    if unit_ref:
        payload["СтруктурнаяЕдиница_Key"] = unit_ref
    if _non_empty(args.business_operation_ref):
        payload["ХозяйственнаяОперация_Key"] = args.business_operation_ref
    if _non_empty(args.basis_ref):
        payload["ДокументОснование"] = args.basis_ref
        payload["ДокументОснование_Type"] = args.basis_type
    if executor_ref:
        payload["Исполнитель"] = executor_ref
        payload["Исполнитель_Type"] = args.executor_type

    if args.team and _non_empty(args.employee_ref):
        team_row: Dict[str, Any] = {
            "LineNumber": 1,
            "Сотрудник_Key": args.employee_ref,
            "КТУ": float(args.ktu),
            "КлючСвязи": link_key,
        }
        if unit_ref:
            team_row["СтруктурнаяЕдиница_Key"] = unit_ref
        payload["СоставБригады"] = [team_row]

    return payload


def read_recent() -> None:
    select = ",".join(
        [
            "Ref_Key",
            "Number",
            "Date",
            "Posted",
            "Организация_Key",
            "СтруктурнаяЕдиница_Key",
            "ЗаказНаПроизводство_Key",
            "Операции",
            "СоставБригады",
        ]
    )
    endpoint = f"{ENTITY}?$top=5&$orderby=Date desc&$select={select}"
    print(json.dumps(_request("GET", endpoint), ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe 1C OData Document_СдельныйНаряд.")
    parser.add_argument("--read", action="store_true", help="Read recent piecework orders.")
    parser.add_argument("--apply", action="store_true", help="POST the generated payload.")
    parser.add_argument("--number", default=f"PW{datetime.now():%H%M%S}")
    parser.add_argument("--date")
    parser.add_argument("--comment", default="PRODPLAN source=piecework_probe/1")
    parser.add_argument("--order-ref")
    parser.add_argument("--item-ref")
    parser.add_argument("--operation-ref")
    parser.add_argument("--employee-ref")
    parser.add_argument("--executor-ref")
    parser.add_argument("--executor-type", default="StandardODATA.Catalog_Сотрудники")
    parser.add_argument("--team", action="store_true", help="Also fill СоставБригады from employee-ref.")
    parser.add_argument("--organization-ref")
    parser.add_argument("--structural-unit-ref")
    parser.add_argument("--business-operation-ref")
    parser.add_argument("--basis-ref")
    parser.add_argument("--basis-type", default="StandardODATA.Document_СборкаЗапасов")
    parser.add_argument("--spec-ref")
    parser.add_argument("--stage-ref")
    parser.add_argument("--unit")
    parser.add_argument("--qty", default=1)
    parser.add_argument("--time-norm", default=0)
    parser.add_argument("--price", default=0)
    parser.add_argument("--ktu", default=1)
    parser.add_argument("--link-key", default=1)
    args = parser.parse_args()

    if args.read:
        read_recent()
        return 0

    missing = [name for name in ("item_ref", "operation_ref") if not _non_empty(getattr(args, name))]
    if missing:
        parser.error(f"missing required args for payload: {', '.join('--' + m.replace('_', '-') for m in missing)}")

    payload = build_payload(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if not args.apply:
        print("\nDry run only. Add --apply to POST into 1C.")
        return 0
    if "unf_demo" not in _base_url().lower():
        raise SystemExit(f"Refusing to write to non-demo OData base: {_base_url()}")

    created = _request("POST", ENTITY, payload)
    print("\nCreated:")
    print(json.dumps(created, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

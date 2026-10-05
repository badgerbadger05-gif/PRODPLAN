# -*- coding: utf-8 -*-
"""Repair September 2026 piecework orders whose operation rows carry warehouse
refs (Склад №3 / Участок порошковой окраски) as the department.

Payroll attributes piecework accruals by the row department, so these must be
the production department ("Производственное (ЗСМ)"), as in manual ЗСНФ docs.

Default is --dry-run: prints the planned change per document, no writes.
--apply --confirm-production: Unpost -> PATCH -> Post each document.

Credentials: ODATA_BASE_URL/ODATA_USERNAME/ODATA_PASSWORD env, or
config/odata_config.json (same layout as other tools).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

ENTITY = "Document_СдельныйНаряд"
PRODUCTION_UNIT = "c74ea54c-d1b2-11ef-9e01-9ee51454587f"  # Производственное (ЗСМ)
WAREHOUSE_UNITS = {
    "ab951594-75e8-11f1-8002-9ee51454587f": "Склад №3 - склад покрашенных деталей",
    "c8cf29e0-bae2-11f0-885d-9ee51454587f": "Участок порошковой окраски",
}
EXPECTED_NUMBERS = {
    "PW000001196", "PW000001198", "PW000001200", "PW000001205", "PW000001211",
    "PW000001217", "PW000001225", "PW000001227", "PW000001236", "PW000001239",
    "PW000001242", "PW000001247", "PW000001249", "PW000001251", "PW000001271",
    "PW000001275", "PW000001278", "PW000001280", "PW000001285", "PW000001286",
    "PW000001287", "PW000001288", "PW000001289", "PW000001292", "PW000001293",
    "PW000001295", "PW000001298", "PW000001300", "PW000001306", "PW000001310",
    "PW000001311", "PW000001314", "PW000001317", "PW000001318",
}


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name) or default).strip()


def _config() -> Dict[str, str]:
    cfg: Dict[str, Any] = {}
    path = Path("config") / "odata_config.json"
    if path.exists():
        try:
            cfg = json.loads(path.read_text("utf-8") or "{}")
        except Exception:
            cfg = {}
    return {
        "base_url": _env("ODATA_BASE_URL", str(cfg.get("base_url") or "")).rstrip("/"),
        "username": _env("ODATA_USERNAME", str(cfg.get("username") or "")),
        "password": _env("ODATA_PASSWORD", str(cfg.get("password") or "")),
        "token": _env("ODATA_TOKEN", str(cfg.get("token") or "")),
    }


class Client:
    def __init__(self, cfg: Dict[str, str]) -> None:
        if not cfg["base_url"]:
            raise SystemExit("ODATA_BASE_URL не задан (ни env, ни config/odata_config.json)")
        self.base_url = cfg["base_url"]
        self.headers = {
            "Accept": "application/json;odata.metadata=minimal",
            "Content-Type": "application/json",
        }
        if cfg["token"]:
            self.headers["Authorization"] = f"Bearer {cfg['token']}"
        elif cfg["username"] and cfg["password"]:
            raw = f"{cfg['username']}:{cfg['password']}".encode("utf-8")
            self.headers["Authorization"] = f"Basic {base64.b64encode(raw).decode('ascii')}"

    def request(self, method: str, endpoint: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        quoted = urllib.parse.quote(endpoint.lstrip("/"), safe="$()_-,.=/'")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(f"{self.base_url}/{quoted}", data=data, method=method)
        for key, value in self.headers.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                text = response.read().decode("utf-8", errors="replace").strip()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {details}") from exc

    def get(self, endpoint: str) -> Dict[str, Any]:
        return self.request("GET", endpoint)

    def patch(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("PATCH", endpoint, payload)

    def post_operation(self, endpoint: str) -> Dict[str, Any]:
        return self.request("POST", endpoint, {})


def find_target_docs(client: Client) -> List[Dict[str, Any]]:
    doc_filter = (
        "Date ge datetime'2026-09-01T00:00:00' and Date lt datetime'2026-10-01T00:00:00' "
        "and Posted eq true and DeletionMark eq false"
    )
    select = "Ref_Key,Number,Date,Posted,Закрыт,СтруктурнаяЕдиница_Key,Операции"
    docs = client.get(
        f"{ENTITY}?$filter={urllib.parse.quote(doc_filter)}&$select={urllib.parse.quote(select, safe=',')}"
    ).get("value", [])
    targets = []
    for doc in docs:
        rows = doc.get("Операции") or []
        if any(str(r.get("СтруктурнаяЕдиница_Key") or "") in WAREHOUSE_UNITS for r in rows):
            targets.append(doc)
    return targets


def build_repair(doc: Dict[str, Any]) -> Dict[str, Any] | None:
    """Return the PATCH body, or None when the doc is already clean."""
    rows = doc.get("Операции") or []
    needs = str(doc.get("СтруктурнаяЕдиница_Key") or "") != PRODUCTION_UNIT
    new_rows: List[Dict[str, Any]] = []
    for row in rows:
        clean = {k: v for k, v in row.items() if k != "Ref_Key"}
        for field in ("СтруктурнаяЕдиница_Key", "ПодразделениеЗавершающегоЭтапа_Key"):
            if str(clean.get(field) or "") != PRODUCTION_UNIT:
                needs = True
                clean[field] = PRODUCTION_UNIT
        new_rows.append(clean)
    if not needs:
        return None
    return {
        "СтруктурнаяЕдиница_Key": PRODUCTION_UNIT,
        "Операции": new_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Только показать план (по умолчанию).")
    parser.add_argument("--apply", action="store_true", help="Выполнить запись в 1С.")
    parser.add_argument("--confirm-production", action="store_true", help="Подтверждение записи в боевую базу.")
    args = parser.parse_args()

    client = Client(_config())
    targets = find_target_docs(client)
    numbers = {str(d.get("Number") or "") for d in targets}
    if numbers != EXPECTED_NUMBERS:
        missing = EXPECTED_NUMBERS - numbers
        extra = numbers - EXPECTED_NUMBERS
        raise SystemExit(
            "Набор документов не совпал с ожидаемым (fail-closed).\n"
            f"  нет в выборке: {sorted(missing)}\n  лишние: {sorted(extra)}"
        )
    print(f"Целевых документов: {len(targets)} (совпало с ожидаемым набором)")

    plan = []
    for doc in sorted(targets, key=lambda d: str(d.get("Date") or "")):
        repair = build_repair(doc)
        rows = doc.get("Операции") or []
        current_units = sorted({WAREHOUSE_UNITS.get(str(r.get("СтруктурнаяЕдиница_Key") or ""), str(r.get("СтруктурнаяЕдиница_Key") or "")) for r in rows})
        header_unit = WAREHOUSE_UNITS.get(str(doc.get("СтруктурнаяЕдиница_Key") or ""), str(doc.get("СтруктурнаяЕдиница_Key") or ""))
        print(f"{doc['Number']} от {str(doc.get('Date'))[:10]}: шапка [{header_unit}], строки {current_units} -> Производственное (ЗСМ)")
        if repair is not None:
            plan.append((doc, repair))

    print(f"\nТребуют исправления: {len(plan)} из {len(targets)}")
    if not args.apply:
        print("Dry-run. Для записи: --apply --confirm-production")
        return 0
    if not args.confirm_production:
        raise SystemExit("Отказ: запись без --confirm-production не выполняется")
    if "unf_demo" in client.base_url.lower():
        raise SystemExit("Отказ: цель — демо-база, скрипт предназначен для боевой")

    for doc, repair in plan:
        ref = doc["Ref_Key"]
        number = doc["Number"]
        client.post_operation(f"{ENTITY}(guid'{ref}')/Unpost")
        client.patch(f"{ENTITY}(guid'{ref}')", repair)
        client.post_operation(f"{ENTITY}(guid'{ref}')/Post")
        check = client.get(f"{ENTITY}(guid'{ref}')?$select=Number,Posted,СтруктурнаяЕдиница_Key,Операции")
        bad = [
            r for r in (check.get("Операции") or [])
            if str(r.get("СтруктурнаяЕдиница_Key") or "") != PRODUCTION_UNIT
        ]
        status = "OK" if check.get("Posted") and not bad and str(check.get("СтруктурнаяЕдиница_Key") or "") == PRODUCTION_UNIT else "ПРОВЕРИТЬ"
        print(f"{number}: исправлен и проведён — {status}")
    print("\nГотово. Пересчёт сдельных начислений за сентябрь — в 1С (зарплата).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

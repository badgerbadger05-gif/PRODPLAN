"""Validate and atomically apply the dated workshop feeder decision over local API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen


def _json(url: str, *, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    assignments = list(manifest.get("assignments") or [])
    if not assignments:
        raise ValueError("assignment manifest is empty")
    settings_url = args.base_url.rstrip("/") + "/api/v1/production-control/settings"
    settings = _json(settings_url)
    rows = {int(row.get("workshop_id") or row.get("resource_id")): dict(row)
            for row in settings.get("workshop_warehouses") or []}
    warehouses = {str(row.get("warehouse_ref1c") or ""): str(row.get("warehouse_name") or "")
                  for row in settings.get("warehouses") or []}
    for assignment in assignments:
        workshop_id = int(assignment["workshop_id"])
        row = rows.get(workshop_id)
        if row is None or str(row.get("workshop_name") or "") != str(assignment["workshop_name"]):
            raise ValueError(f"workshop mismatch for workshop_id={workshop_id}")
        ref = str(assignment["feeder_warehouse_ref1c"])
        if warehouses.get(ref) != str(assignment["feeder_warehouse_name"]):
            raise ValueError(f"warehouse mismatch for workshop_id={workshop_id}")
        row["feeder_warehouse_ref1c"] = ref
    payload = {
        "workshop_warehouses": list(rows.values()),
        "ignored_warehouses": list(settings.get("ignored_warehouses") or []),
    }
    result = {
        "status": "validated",
        "decision_date": manifest.get("decision_date"),
        "assignments": len(assignments),
        "applied": False,
    }
    if args.apply:
        saved = _json(settings_url, payload=payload)
        result.update(status="applied", applied=True, saved_bindings=len(saved.get("workshop_warehouses") or []))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

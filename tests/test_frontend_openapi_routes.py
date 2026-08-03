"""Static guard: frontend service paths must exist in the committed OpenAPI."""

from __future__ import annotations

import json
import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SERVICES = REPO / "frontend-erp-shell/src/services"
OPENAPI = REPO / "docs/api/openapi.json"


def _normalise(path: str) -> str:
    path = path.split("?", 1)[0]
    path = re.sub(r"\$\{[^}]+\}", "{param}", path)
    path = re.sub(r"\{[^}]+\}", "{param}", path)
    return path.removesuffix("{param}").rstrip("/")


def test_frontend_service_paths_exist_in_openapi():
    document = json.loads(OPENAPI.read_text(encoding="utf-8"))
    api_paths = {
        _normalise(path.removeprefix("/api"))
        for path in document["paths"]
    }
    missing: list[str] = []

    for source_path in sorted(SERVICES.glob("*.ts")):
        if source_path.name.endswith(".test.ts"):
            continue
        source = source_path.read_text(encoding="utf-8")
        for raw_path in re.findall(r"[\"'`](/v1/[^\"'`]+)[\"'`]", source):
            candidate = _normalise(raw_path)
            # A service may define a base constant and append the operation
            # path later (for example specification-repair and item-ledger).
            if not any(
                candidate == api_path or api_path.startswith(candidate + "/")
                for api_path in api_paths
            ):
                missing.append(f"{source_path.relative_to(REPO)}: {raw_path}")

    assert not missing, "Frontend routes absent from OpenAPI:\n" + "\n".join(missing)

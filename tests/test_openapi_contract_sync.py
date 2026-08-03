"""The committed API contract must be generated from the current FastAPI app."""

import json
import os
from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]


def test_committed_openapi_matches_application_schema(tmp_path: Path) -> None:
    committed = json.loads(
        (REPO / "docs/api/openapi.json").read_text(encoding="utf-8")
    )
    database_path = tmp_path / "openapi-contract.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{database_path}",
        "PYTHONPATH": str(REPO / "backend"),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from app.main import app; "
                "print(json.dumps(app.openapi(), ensure_ascii=False))"
            ),
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    generated = json.loads(result.stdout)

    assert committed == generated, (
        "docs/api/openapi.json is stale; regenerate it from app.main:app "
        "and then run frontend-erp-shell npm run api:types"
    )

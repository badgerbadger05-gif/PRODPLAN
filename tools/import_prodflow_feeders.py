"""One-time atomic import of captured ProdFlow feeder reference settings."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from app.services.prodflow_feeder_config import publish_prodflow_feeder_assignments


def _active(value: object) -> bool:
    if value in (True, 1, "1"):
        return True
    if value in (False, 0, "0"):
        return False
    raise ValueError(f"invalid is_active value: {value!r}")


def _validated_database_target(*, expected_host: str, expected_database: str) -> tuple[str, str]:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise ValueError("DATABASE_URL must be explicitly set")
    parsed = urlsplit(raw)
    host = str(parsed.hostname or "")
    database = str(parsed.path or "").lstrip("/")
    if not host or not database:
        raise ValueError("DATABASE_URL must contain a host and database")
    if host.casefold() != expected_host.strip().casefold() or database != expected_database.strip():
        raise ValueError(
            f"database target mismatch: got {host}/{database}, "
            f"expected {expected_host}/{expected_database}"
        )
    return host, database


def _message_rows(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    rows = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{path.name}: expected a message array")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--positions", type=Path, required=True)
    parser.add_argument("--warehouses", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--expected-db-host", required=True)
    parser.add_argument("--expected-db-name", required=True)
    parser.add_argument("--observed-at", required=True, help="ISO timestamp of source capture")
    parser.add_argument("--apply", action="store_true", help="commit; default is rollback dry-run")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    positions = _message_rows(args.positions)
    warehouses = _message_rows(args.warehouses)
    if int(args.expected_count) <= 0:
        raise ValueError("expected-count must be greater than zero")
    active = [row for row in positions if _active(row.get("is_active"))]
    if len(active) != int(args.expected_count):
        raise ValueError(
            f"active position count mismatch: expected {args.expected_count}, got {len(active)}"
        )
    observed_at = datetime.fromisoformat(str(args.observed_at))
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)

    target_host, target_database = _validated_database_target(
        expected_host=args.expected_db_host,
        expected_database=args.expected_db_name,
    )
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result = publish_prodflow_feeder_assignments(
            db,
            positions=positions,
            warehouses=warehouses,
            observed_at=observed_at,
        )
        result = {
            **result,
            "dry_run": not bool(args.apply),
            "positions_file": args.positions.name,
            "warehouses_file": args.warehouses.name,
            "database_target": f"{target_host}/{target_database}",
        }
        if args.apply:
            db.commit()
        else:
            db.rollback()
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + "\n", encoding="utf-8")
        print(text)
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

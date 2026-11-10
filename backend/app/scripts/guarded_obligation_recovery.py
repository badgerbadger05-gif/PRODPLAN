
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import text

from app import models
from app.database import SessionLocal
from app.services.item_ledger.live_plan_scope import live_plan_run_ids, sealed_generation_lineage_ids
from app.services.item_ledger.obligation_generation import _current_reservation_owner_fingerprint
from app.services.obligation_refresh_orchestrator import run_guarded_obligation_recovery


class RecoveryDriverError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def read_json(path: str | os.PathLike[str]) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryDriverError(f"invalid JSON input: {path}") from exc


def write_json(path: str | os.PathLike[str], value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    temporary.replace(target)


def normalize_source_map(value: Any) -> dict[int, int]:
    if not isinstance(value, dict) or not value:
        raise RecoveryDriverError("source map must be a non-empty JSON object")
    try:
        result = {int(key): int(raw) for key, raw in value.items()}
    except (TypeError, ValueError) as exc:
        raise RecoveryDriverError("source map keys and values must be integers") from exc
    if any(source < -1 for source in result.values()):
        raise RecoveryDriverError("source generation must be -1 or a non-negative generation")
    return dict(sorted(result.items()))


def normalize_fingerprints(value: Any) -> dict[int, list[str]]:
    if not isinstance(value, dict) or not value:
        raise RecoveryDriverError("fingerprints must be a non-empty JSON object")
    result: dict[int, list[str]] = {}
    for key, raw in value.items():
        try:
            run_id = int(key)
        except (TypeError, ValueError) as exc:
            raise RecoveryDriverError("fingerprint run IDs must be integers") from exc
        if not isinstance(raw, list) or not raw or any(not isinstance(item, str) or not item for item in raw):
            raise RecoveryDriverError(f"fingerprints for run {run_id} must be a non-empty string list")
        result[run_id] = sorted(set(raw))
    return dict(sorted(result.items()))


def validate_source_inputs(retain_run_ids: Sequence[int], source_map: Mapping[int, int], fingerprints: Mapping[int, Sequence[str]]) -> None:
    expected = {int(run_id) for run_id in retain_run_ids}
    if set(source_map) != expected:
        raise RecoveryDriverError(f"source map run set mismatch: expected {sorted(expected)}, got {sorted(source_map)}")
    if set(fingerprints) != expected:
        raise RecoveryDriverError(f"fingerprint run set mismatch: expected {sorted(expected)}, got {sorted(fingerprints)}")


def _manifest_summary(generation: models.LedgerGeneration) -> list[dict[str, Any]]:
    marks = dict(generation.source_watermarks or {})
    manifest = marks.get("obligation_refresh_manifest")
    entries = manifest.get("entries") if isinstance(manifest, dict) else []
    if not isinstance(entries, list):
        raise RecoveryDriverError("parent has no valid sealed obligation manifest")
    safe: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RecoveryDriverError("sealed manifest entry is malformed")
        safe.append({
            key: entry[key] for key in ("action", "plan_id", "parent_run_id", "candidate_run_id", "reason")
            if key in entry and isinstance(entry[key], (str, int, float, bool, type(None)))
        })
    return safe


def _source_rows(db: Any, run_id: int, source_generation: int) -> list[models.ReservationEntry]:
    query = db.query(models.ReservationEntry).filter(models.ReservationEntry.run_id == int(run_id))
    if source_generation == -1:
        query = query.filter(models.ReservationEntry.is_current.is_(True))
    else:
        query = query.filter(models.ReservationEntry.ledger_generation_id == int(source_generation))
    return list(query.order_by(models.ReservationEntry.id.asc()).all())


def _source_proof(db: Any, generation: models.LedgerGeneration, run_id: int, source_generation: int) -> dict[str, Any]:
    rows = _source_rows(db, run_id, source_generation)
    lineage = set(sealed_generation_lineage_ids(db, generation))
    for row in rows:
        row_generation = int(row.ledger_generation_id)
        if row_generation not in lineage:
            raise RecoveryDriverError(f"run {run_id} source generation {row_generation} is outside parent lineage")
        if source_generation != -1 and row_generation != source_generation:
            raise RecoveryDriverError(f"run {run_id} source generation drift")
    fingerprints = sorted(_current_reservation_owner_fingerprint(row) for row in rows)
    modes: dict[str, int] = {}
    for row in rows:
        mode = str(row.realization_mode or "")
        modes[mode] = modes.get(mode, 0) + 1
    return {"row_count": len(rows), "fingerprints": fingerprints, "mode_counts": dict(sorted(modes.items()))}


def database_name(db: Any) -> str:
    """Return only the server database name; never expose the connection URL."""
    try:
        value = db.execute(text("SELECT current_database()")).scalar_one()
    except Exception as exc:
        raise RecoveryDriverError("could not verify target database identity") from exc
    name = str(value or "").strip()
    if not name or any(char in name for char in "\\/@:"):
        raise RecoveryDriverError("database identity is malformed")
    return name


def build_receipt(db: Any, parent_generation_id: int, source_map: Mapping[int, int], fingerprints: Mapping[int, Sequence[str]], *, expected_database: str | None = None) -> dict[str, Any]:
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or pointer.current_generation_id is None:
        raise RecoveryDriverError("planning truth pointer is unavailable")
    if int(pointer.current_generation_id) != int(parent_generation_id):
        raise RecoveryDriverError(f"pointer drift: expected {parent_generation_id}, got {pointer.current_generation_id}")
    generation = db.get(models.LedgerGeneration, int(parent_generation_id))
    if generation is None or str(generation.status) != "accepted":
        raise RecoveryDriverError("parent generation is not accepted")
    target_database = database_name(db)
    if expected_database and target_database != expected_database:
        raise RecoveryDriverError(f"database mismatch: expected {expected_database}, got {target_database}")
    retain = tuple(int(v) for v in live_plan_run_ids(db, generation))
    validate_source_inputs(retain, source_map, fingerprints)
    source_proofs: dict[str, Any] = {}
    for run_id in retain:
        proof = _source_proof(db, generation, run_id, int(source_map[run_id]))
        if sorted(fingerprints[run_id]) != proof["fingerprints"]:
            raise RecoveryDriverError(f"owner fingerprint drift for run {run_id}")
        source_proofs[str(run_id)] = {
            "source_generation": int(source_map[run_id]),
            "row_count": proof["row_count"],
            "mode_counts": proof["mode_counts"],
            "fingerprint_sha256": sha256_json(proof["fingerprints"]),
        }
    marks = dict(generation.source_watermarks or {})
    before = {
        "pointer_generation_id": int(pointer.current_generation_id),
        "status": str(generation.status),
        "generation_key": str(generation.generation_key),
        "cutoff": generation.cutoff.isoformat() if generation.cutoff else None,
        "lineage_generation_ids": list(sealed_generation_lineage_ids(db, generation)),
        "retain_run_ids": list(retain),
        "manifest": _manifest_summary(generation),
        "source_proofs": source_proofs,
        "source_watermark_keys": sorted(str(key) for key in marks),
    }
    owner_hash = sha256_json({str(k): sorted(v) for k, v in sorted(fingerprints.items())})
    recovery_key = f"guarded-recovery:{int(parent_generation_id)}:{owner_hash[:24]}"
    return {
        "schema": "guarded_obligation_recovery_receipt/v1",
        "created_at": utc_now(),
        "parent_generation_id": int(parent_generation_id),
        "parent_generation_key": str(generation.generation_key),
        "recovery_generation_key": recovery_key,
        "database_name": target_database,
        "expected_pointer_generation_id": int(parent_generation_id),
        "source_generation_by_run": {str(k): int(v) for k, v in sorted(source_map.items())},
        "owner_fingerprints": {str(k): sorted(v) for k, v in sorted(fingerprints.items())},
        "owner_fingerprint_sha256": owner_hash,
        "before": before,
        "status": "prepared",
    }


def verify_receipt(db: Any, receipt: Mapping[str, Any]) -> tuple[dict[int, int], dict[int, list[str]]]:
    if database_name(db) != str(receipt.get("database_name") or ""):
        raise RecoveryDriverError("saved receipt is bound to another database")
    source_map = normalize_source_map(receipt.get("source_generation_by_run"))
    fingerprints = normalize_fingerprints(receipt.get("owner_fingerprints"))
    current = build_receipt(db, int(receipt["parent_generation_id"]), source_map, fingerprints, expected_database=str(receipt["database_name"]))
    if current["owner_fingerprint_sha256"] != receipt.get("owner_fingerprint_sha256"):
        raise RecoveryDriverError("saved owner fingerprint hash drift")
    return source_map, fingerprints


def execute(db: Any, receipt: Mapping[str, Any], *, commit: bool, started_by: str) -> dict[str, Any]:
    source_map, fingerprints = verify_receipt(db, receipt)
    db.rollback()
    db.begin()
    try:
        result = run_guarded_obligation_recovery(
            db,
            parent_generation_id=int(receipt["parent_generation_id"]),
            generation_key=str(receipt["recovery_generation_key"]),
            source_generation_by_run=source_map,
            owner_fingerprints=fingerprints,
            started_by=started_by,
        )
        outcome = {
            "target_generation_id": int(result.target_generation_id),
            "published": bool(result.published),
            "candidate_run_ids": [int(v) for v in result.candidate_run_ids],
        }
        if commit:
            db.commit()
            outcome["status"] = "applied"
        else:
            db.rollback()
            outcome["status"] = "simulated_rolled_back"
        return outcome
    except Exception:
        db.rollback()
        raise


def command_prepare(args: argparse.Namespace) -> dict[str, Any]:
    source_map = normalize_source_map(read_json(args.source_map))
    db = SessionLocal()
    try:
        parent = db.get(models.LedgerGeneration, int(args.parent_generation))
        if parent is None:
            raise RecoveryDriverError("parent generation is unavailable")
        retain = tuple(int(v) for v in live_plan_run_ids(db, parent))
        if set(source_map) != set(retain):
            raise RecoveryDriverError(f"source map run set mismatch: expected {sorted(retain)}, got {sorted(source_map)}")
        if args.fingerprints:
            fingerprints = normalize_fingerprints(read_json(args.fingerprints))
        elif args.collect_fingerprints:
            fingerprints = {
                run_id: _source_proof(db, parent, run_id, source_map[run_id])["fingerprints"]
                for run_id in retain
            }
        else:
            raise RecoveryDriverError("--fingerprints or explicit --collect-fingerprints is required")
        receipt = build_receipt(db, int(args.parent_generation), source_map, fingerprints, expected_database=args.expected_database)
    finally:
        db.rollback()
        db.close()
    write_json(args.receipt, receipt)
    return {"status": "prepared", "receipt": str(Path(args.receipt)), "owner_fingerprint_sha256": receipt["owner_fingerprint_sha256"]}


def command_execute(args: argparse.Namespace, *, commit: bool) -> dict[str, Any]:
    receipt = read_json(args.receipt)
    if not isinstance(receipt, dict) or receipt.get("schema") != "guarded_obligation_recovery_receipt/v1":
        raise RecoveryDriverError("invalid recovery receipt")
    db = SessionLocal()
    try:
        result = execute(db, receipt, commit=commit, started_by=args.started_by)
    finally:
        db.close()
    if args.result:
        write_json(args.result, {"receipt": str(Path(args.receipt)), "created_at": utc_now(), **result})
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded, receipt-bound obligation recovery driver")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--parent-generation", required=True, type=int)
    prepare.add_argument("--source-map", required=True)
    prepare.add_argument("--fingerprints")
    prepare.add_argument("--collect-fingerprints", action="store_true")
    prepare.add_argument("--expected-database", required=True)
    prepare.add_argument("--receipt", required=True)
    simulate = sub.add_parser("simulate")
    simulate.add_argument("--receipt", required=True)
    simulate.add_argument("--result")
    simulate.add_argument("--started-by", default="guarded-recovery-simulate")
    apply = sub.add_parser("apply")
    apply.add_argument("--receipt", required=True)
    apply.add_argument("--result")
    apply.add_argument("--started-by", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = command_prepare(args)
        elif args.command == "simulate":
            result = command_execute(args, commit=False)
        else:
            result = command_execute(args, commit=True)
        print(json.dumps(result, sort_keys=True))
        return 0
    except RecoveryDriverError as exc:
        print(f"guarded recovery refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"guarded recovery failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

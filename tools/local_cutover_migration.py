"""Rehearse the canonical cutover pipeline on an explicitly restored local DB.

This runner cannot target the server or a production database. An unfenced
source dump is useful for rehearsal only and never authorizes a live cutover.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.current_execution_migration import (
    _accepted_truth_generation, apply_current_obligation_migration,
    build_manifest, postflight_manifest,
)
from tools.operational_preservation import (
    SOURCE_REVISION, TARGET_REVISION, capture_before, verify_preservation,
)

PGOPTIONS = (
    "-c prodplan.execution_projection_backup_ready=on "
    "-c maintenance_work_mem=2GB -c timezone=Europe/Moscow"
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DATABASE_PREFIXES = ("prodplan_weekend_", "prodplan_cutover_rehearsal_")


class RehearsalBlocked(RuntimeError):
    """A safe diagnostic code, never a raw SQL/DSN error message."""


def validate_target(database_url, expected_database, restore_receipt, *, writers_stopped):
    """Validate all identity/backup acknowledgements before opening a DB."""
    if not writers_stopped:
        raise RehearsalBlocked("writers_stopped_required")
    url = make_url(database_url)
    if url.drivername not in {"postgresql", "postgresql+psycopg2"}:
        raise RehearsalBlocked("psycopg2_postgresql_required")
    if url.host not in LOOPBACK_HOSTS or url.query:
        raise RehearsalBlocked("literal_loopback_without_url_query_required")
    if url.port is None or url.port == 5432 or not 1024 <= url.port <= 65535:
        raise RehearsalBlocked("explicit_isolated_local_port_required")
    if not expected_database.startswith(DATABASE_PREFIXES) or url.database != expected_database:
        raise RehearsalBlocked("rehearsal_database_identity_required")
    if not url.username or url.username in {"postgres", "prodplan"}:
        raise RehearsalBlocked("isolated_rehearsal_role_required")
    if restore_receipt.get("receipt_version") != 1 or restore_receipt.get("status") != "restored":
        raise RehearsalBlocked("verified_restore_receipt_required")
    if restore_receipt.get("verified_backup") is not True or restore_receipt.get("restore_verified") is not True:
        raise RehearsalBlocked("backup_and_restore_verification_required")
    if restore_receipt.get("source_revision") != SOURCE_REVISION:
        raise RehearsalBlocked("restore_source_revision_mismatch")
    checksum = restore_receipt.get("backup_sha256", "")
    if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
        raise RehearsalBlocked("verified_backup_hash_required")
    size = restore_receipt.get("source_dump_bytes")
    if type(size) is not int or size <= 0:
        raise RehearsalBlocked("positive_source_dump_size_required")
    target = restore_receipt.get("target")
    expected_target = {"host": url.host, "port": url.port, "database": expected_database, "user": url.username}
    if target != expected_target:
        raise RehearsalBlocked("restore_target_dsn_mismatch")
    return url


class _AlembicConfig(Config):
    def set_main_option(self, name, value):
        # env.py also sets DATABASE_URL; protect percent-encoded credentials
        # from ConfigParser interpolation without changing the actual DSN.
        super().set_main_option(name, value.replace("%", "%%") if name == "sqlalchemy.url" else value)


def _config(database_url):
    config = _AlembicConfig()
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@contextmanager
def _local_process_environment(database_url):
    saved = {key: os.environ.get(key) for key in ("DATABASE_URL", "PGOPTIONS")}
    os.environ["DATABASE_URL"] = database_url
    os.environ["PGOPTIONS"] = PGOPTIONS
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _check_connection_identity(engine, url):
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        row = connection.execute(text(
            "SELECT current_database() AS database, current_user AS role, "
            "current_setting('maintenance_work_mem') AS maintenance_work_mem, "
            "current_setting('TimeZone') AS timezone, "
            "current_setting('prodplan.execution_projection_backup_ready', true) AS backup_guard"
        )).mappings().one()
    if row["database"] != url.database or row["role"] != url.username:
        raise RehearsalBlocked("connected_database_or_role_mismatch")
    if row["backup_guard"] != "on" or row["maintenance_work_mem"] != "2GB" or row["timezone"] != "Europe/Moscow":
        raise RehearsalBlocked("local_session_guard_settings_mismatch")
    return dict(row)


def _checkpoint(handle, payload):
    handle.seek(0)
    json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


def run_rehearsal(database_url, *, expected_database, restore_receipt,
                  writers_stopped, output, dry_run=False):
    """Guarded orchestration only; canonical migration functions own writes."""
    url = validate_target(database_url, expected_database, restore_receipt, writers_stopped=writers_stopped)
    output = Path(output)
    baseline_path = output.with_name(output.stem + "-before.json")
    if output.exists() or baseline_path.exists():
        raise RehearsalBlocked("new_receipt_and_baseline_paths_required")
    config = _config(database_url)
    if ScriptDirectory.from_config(config).get_heads() != [TARGET_REVISION]:
        raise RehearsalBlocked("repository_alembic_head_mismatch")
    payload = {
        "receipt_version": 1, "status": "running", "phase": "source_guard",
        "rehearsal_only": True, "dry_run": dry_run,
        "target": restore_receipt["target"],
        "backup_sha256": restore_receipt["backup_sha256"],
        "source_dump_bytes": restore_receipt["source_dump_bytes"],
        "source_revision": SOURCE_REVISION, "target_revision": TARGET_REVISION,
        "before_receipt": str(baseline_path),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve a new receipt before any connection/mutation; no prior evidence
    # may be overwritten. The owning file handle records crash/failure phases.
    with output.open("x", encoding="utf-8") as handle:
        _checkpoint(handle, payload)
        engine = None
        try:
            with _local_process_environment(database_url):
                engine = create_engine(url, connect_args={"options": PGOPTIONS})
                payload["session"] = _check_connection_identity(engine, url)
                before = capture_before(engine)
                with baseline_path.open("x", encoding="utf-8") as baseline:
                    json.dump(before, baseline, ensure_ascii=False, indent=2)
                    baseline.flush()
                    os.fsync(baseline.fileno())
                payload["phase"] = "source_captured"
                _checkpoint(handle, payload)
                if dry_run:
                    payload.update(status="dry_run_passed", phase="complete")
                else:
                    payload["phase"] = "alembic_to_r9"
                    _checkpoint(handle, payload)
                    command.upgrade(config, "20260911_01")
                    payload["phase"] = "current_owner_preflight"
                    _checkpoint(handle, payload)
                    payload["preflight"] = build_manifest(engine)
                    if payload["preflight"]["status"] != "ready":
                        raise RehearsalBlocked("current_owner_preflight_blocked")
                    payload["phase"] = "current_owner_apply"
                    _checkpoint(handle, payload)
                    payload["apply"] = apply_current_obligation_migration(engine, writers_stopped=True)
                    if payload["apply"]["status"] != "ready":
                        raise RehearsalBlocked("current_owner_apply_blocked")
                    payload["phase"] = "current_owner_postflight"
                    _checkpoint(handle, payload)
                    payload["postflight"] = postflight_manifest(engine, generation_id=_accepted_truth_generation(engine))
                    if payload["postflight"]["status"] != "ready":
                        raise RehearsalBlocked("current_owner_postflight_blocked")
                    payload["phase"] = "alembic_to_head"
                    _checkpoint(handle, payload)
                    command.upgrade(config, "head")
                    payload["phase"] = "operational_preservation"
                    _checkpoint(handle, payload)
                    payload["operational_preservation"] = verify_preservation(engine, before)
                    if payload["operational_preservation"]["status"] != "passed":
                        raise RehearsalBlocked("operational_preservation_blocked")
                    payload.update(status="passed", phase="complete")
        except Exception as exc:
            payload.update(status="failed", error_type=type(exc).__name__)
            if isinstance(exc, RehearsalBlocked):
                payload["error_code"] = str(exc)
            # Driver errors may embed SQL, parameters and passwords. Preserve
            # the phase/type and safe manifest results, never raw exception text.
        finally:
            if engine is not None:
                engine.dispose()
            _checkpoint(handle, payload)
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="Prefer DATABASE_URL environment variable")
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--restore-receipt", type=Path, required=True)
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("set DATABASE_URL or --database-url")
    try:
        restore_receipt = json.loads(args.restore_receipt.read_text(encoding="utf-8"))
        payload = run_rehearsal(database_url, expected_database=args.expected_database,
                               restore_receipt=restore_receipt, writers_stopped=args.writers_stopped,
                               output=args.output, dry_run=args.dry_run)
        print(json.dumps({"status": payload["status"], "phase": payload["phase"], "receipt": str(args.output)}))
        return 0 if payload["status"] in {"passed", "dry_run_passed"} else 1
    except Exception as exc:
        result = {"status": "blocked", "error_type": type(exc).__name__}
        if isinstance(exc, RehearsalBlocked):
            result["error_code"] = str(exc)
        print(json.dumps(result), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

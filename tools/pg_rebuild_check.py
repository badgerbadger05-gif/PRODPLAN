"""Rehearse the Ledger rebuild migrations and SQL against a real PostgreSQL.

The repository already contains textual and SQLite-based guards for
``tools/sql/clear_rebuildable_ledger_projections.sql`` and
``tools/sql/verify_ledger_rebuild.sql``.  They cannot prove that either file is
executable: SQLite never sees ``ALTER TABLE ... ADD CONSTRAINT`` (the migration
harness stubs it out), so the majority of the KEEP -> CLEAR foreign keys the
clear script detaches are invisible to those tests.  This command closes that
gap by running the real statements on a real PostgreSQL 15 server.

Stages (each reported separately as PASS/FAIL):

1. ``migrate``     -- ``alembic upgrade head``.
2. ``round-trip``  -- ``head -> downgrade 20260726_14 -> head``.  This is the
   deepest safe floor: ``20260726_04`` is an intentional destructive canon
   cleanup whose ``downgrade()`` raises ``RuntimeError``, so a full downgrade to
   base is impossible by design.  The floor is exactly the parent of the two new
   revisions ``20260730_01`` / ``20260730_02``, which is what the round trip is
   meant to exercise.
3. ``clear``       -- executes the destructive clear script through ``COMMIT``.
   On a freshly migrated database the script's guard would refuse to start, so a
   minimal accepted-generation fixture is seeded first (only when the database is
   provably empty of rebuildable state).  Reaching ``CLEAR PASS`` proves that
   every declared FK exists, that the TRUNCATE set is closed over the migrated
   PostgreSQL schema and that all constraints can be restored.
4. ``verify``      -- executes the read-only verifier.

   * ``--smoke`` (default): the verifier is expected to fail on emptiness.  Only
     failures from the documented known-empty allow-list are tolerated; any other
     message, and any SQL/parse/relation error, fails the stage.  The verifier's
     final summary projection is additionally executed on its own, because the
     aborted ``DO`` block would otherwise never reach it.
   * ``--strict``: intended for a restored production dump after the replay.
     Any failure is a failure.

By default the command starts a disposable ``postgres:15`` container on a free
port and removes it afterwards.  Point it at an existing server (a restored
production dump copy) with ``--host/--port/--user/--password/--database`` or
``--dsn`` / ``PRODPLAN_PG_CHECK_DSN``.

Examples::

    python tools/pg_rebuild_check.py
    python tools/pg_rebuild_check.py --dsn postgresql://user:pw@host:5432/prodplan_copy \\
        --stages clear --allow-destructive-clear \\
        --expected-generation-key prod-rebuild-20260729-obligation-plan11
"""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import Sequence
from urllib.parse import quote, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
CLEAR_SQL = REPO_ROOT / "tools" / "sql" / "clear_rebuildable_ledger_projections.sql"
VERIFY_SQL = REPO_ROOT / "tools" / "sql" / "verify_ledger_rebuild.sql"

# Parent of 20260730_01; see the module docstring for why a full downgrade to
# base is impossible.
ROUND_TRIP_FLOOR = "20260726_14"

SEED_GENERATION_KEY = "pg-rebuild-check"
SEED_CUTOFF = "2026-07-28 14:30:24+03"
DEFAULT_OPENING_BASELINE_AT = "2026-05-31 23:59:59.999999"
DEFAULT_FIXED_RUN_COUNT = 10

STAGE_NAMES = ("migrate", "round-trip", "clear", "verify")

# Verifier failures that a legitimately empty database must produce.  Every
# other message means the SQL itself, the schema or the data is broken.
KNOWN_EMPTY_VERIFY_FAILURES = (
    r"planning truth singleton count is \d+, expected 1",
    r"current truth is absent, unaccepted, or differs from expected key/cutoff",
    r"current generation has no completed physical import boundary",
    r"fixed run count is \d+, expected \d+",
    r"fixed runs have no MRP requirements",
    r"fixed run for source_plan_id=1 is absent or has no active freeze",
    r"source_plan_id=1 has no active baseline at expected timestamp",
    r"current generation has no reservation entries",
    r"current generation has no realization events",
    r"\d+ required planning read snapshot consumers are missing",
    r"\d+ fixed runs have no accepted MRP result snapshot",
    r"\d+ fixed runs have no accepted period-plan execution snapshot",
    r"current accepted assembly queue snapshot count is \d+, expected 1",
    r"current accepted production journal snapshot count is \d+, expected 1",
    r"current production journal is empty or its row count is inconsistent",
    r"current accepted purchase journal snapshot count is \d+, expected 1",
    r"current purchase journal has no BUY rows to verify",
    r"current assembly queue is empty or has no open quantity",
    r"completed current drum schedule count is \d+, expected 1",
    r"current drum has no slots",
)

SEED_FIXTURE_SQL = f"""
BEGIN;
INSERT INTO physical_import_batch
    (batch_key, status, cutoff, source_watermarks, completed_at, created_at)
VALUES
    ('{SEED_GENERATION_KEY}', 'completed', '{SEED_CUTOFF}', '{{}}', now(), now());
INSERT INTO ledger_generation
    (generation_key, status, cutoff, source_watermarks, capabilities,
     physical_import_batch_id, algorithm_version, accepted_at,
     created_at, updated_at)
VALUES
    ('{SEED_GENERATION_KEY}', 'accepted', '{SEED_CUTOFF}', '{{}}', '{{}}',
     (SELECT id FROM physical_import_batch
       WHERE batch_key = '{SEED_GENERATION_KEY}'),
     '{SEED_GENERATION_KEY}', now(), now(), now());
INSERT INTO planning_truth_state (id, current_generation_id, updated_at)
VALUES
    (1,
     (SELECT id FROM ledger_generation
       WHERE generation_key = '{SEED_GENERATION_KEY}'),
     now())
ON CONFLICT (id) DO UPDATE
    SET current_generation_id = EXCLUDED.current_generation_id,
        updated_at = now();
COMMIT;
"""

# A freshly migrated database is not literally empty: 20260723_06 seeds one
# *rejected* legacy generation.  Only accepted truth and real facts count.
EMPTINESS_PROBE_SQL = """
SELECT
    (SELECT count(*) FROM ledger_generation WHERE status = 'accepted')
  + (SELECT count(*) FROM stock_ledger_entry)
  + (SELECT count(*) FROM planning_run)
  + (SELECT count(*) FROM reservation_entry);
"""


class CheckError(RuntimeError):
    """The rehearsal cannot continue."""


@dataclass
class StageResult:
    name: str
    status: str
    detail: str = ""
    seconds: float = 0.0


@dataclass
class Target:
    """Connection coordinates of the database under rehearsal."""

    host: str
    port: int
    user: str
    password: str
    database: str
    container: str | None = None
    owned: bool = False

    def url(self) -> str:
        return (
            "postgresql://"
            f"{quote(self.user, safe='')}:{quote(self.password, safe='')}"
            f"@{self.host}:{self.port}/{self.database}"
        )


def _run(
    command: Sequence[str],
    *,
    stdin_text: str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=None if cwd is None else str(cwd),
        timeout=timeout,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=60).returncode == 0


# --------------------------------------------------------------------------- #
# psql execution
# --------------------------------------------------------------------------- #


class Psql:
    """Runs psql scripts against the target without requiring a host client.

    Three transports, in order of preference: a container the caller named (or
    the disposable one this command started), a local ``psql`` binary, and a
    throwaway client container.
    """

    def __init__(self, target: Target, *, image: str, binary: str | None, container: str | None):
        self.target = target
        self.image = image
        self.binary = binary
        self.container = container or target.container
        if self.container is None and self.binary is None:
            self.binary = shutil.which("psql")
        if self.container is None and self.binary is None and not _docker_available():
            raise CheckError("no psql transport: neither a container nor a psql binary is available")

    def _command(self, args: Sequence[str], database: str) -> list[str]:
        env = ["-e", f"PGPASSWORD={self.target.password}", "-e", "PGCLIENTENCODING=UTF8"]
        psql = [
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            self.target.user,
            "-d",
            database,
            *args,
        ]
        if self.container is not None:
            return ["docker", "exec", "-i", *env, self.container, *psql]
        if self.binary is not None:
            return [self.binary, "-h", self.target.host, "-p", str(self.target.port), *psql]
        host = self.target.host
        if host in {"localhost", "127.0.0.1", "::1"}:
            host = "host.docker.internal"
        return [
            "docker",
            "run",
            "--rm",
            "-i",
            "--add-host=host.docker.internal:host-gateway",
            *env,
            self.image,
            *psql,
            "-h",
            host,
            "-p",
            str(self.target.port),
        ]

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PGPASSWORD"] = self.target.password
        env["PGCLIENTENCODING"] = "UTF8"
        return env

    def script(
        self,
        sql: str,
        *,
        variables: dict[str, str] | None = None,
        database: str | None = None,
        timeout: int = 1800,
    ) -> subprocess.CompletedProcess[str]:
        args: list[str] = []
        for name, value in (variables or {}).items():
            args += ["-v", f"{name}={value}"]
        command = self._command(args, database or self.target.database)
        return _run(command, stdin_text=sql, env=self._env(), timeout=timeout)

    def scalar(self, sql: str, *, database: str | None = None) -> str:
        result = self.script(f"\\pset tuples_only on\n\\pset format unaligned\n{sql}", database=database)
        if result.returncode != 0:
            raise CheckError(f"probe query failed: {(result.stderr or result.stdout).strip()}")
        return result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""


# --------------------------------------------------------------------------- #
# disposable server
# --------------------------------------------------------------------------- #


def start_disposable_server(args: argparse.Namespace) -> Target:
    if not _docker_available():
        raise CheckError("Docker is unavailable; pass --host/--dsn to use an existing server")
    port = args.port or _free_port()
    name = args.container_name or f"prodplan-pg-rebuild-check-{os.getpid()}"
    _run(["docker", "rm", "-f", name], timeout=120)
    started = _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-e",
            f"POSTGRES_DB={args.database}",
            "-e",
            f"POSTGRES_USER={args.user}",
            "-e",
            f"POSTGRES_PASSWORD={args.password}",
            "-p",
            f"127.0.0.1:{port}:5432",
            args.image,
            "postgres",
            "-c",
            "timezone=Europe/Moscow",
        ],
        timeout=600,
    )
    if started.returncode != 0:
        raise CheckError(f"cannot start {args.image}: {started.stderr.strip()}")
    target = Target(
        host="127.0.0.1",
        port=port,
        user=args.user,
        password=args.password,
        database=args.database,
        container=name,
        owned=True,
    )
    _wait_ready(target, timeout=args.startup_timeout)
    return target


def _wait_ready(target: Target, *, timeout: int) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if target.container is not None:
            probe = _run(
                ["docker", "exec", target.container, "pg_isready", "-U", target.user, "-d", target.database],
                timeout=60,
            )
            last = (probe.stdout or probe.stderr).strip()
            if probe.returncode == 0:
                with contextlib.suppress(OSError):
                    with socket.create_connection((target.host, target.port), timeout=3):
                        return
        else:
            with contextlib.suppress(OSError):
                with socket.create_connection((target.host, target.port), timeout=3):
                    return
        time.sleep(1.0)
    raise CheckError(f"database did not become ready within {timeout}s: {last}")


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #


def _alembic(target: Target, *command: str, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["DATABASE_URL"] = target.url()
    env["PYTHONIOENCODING"] = "utf-8"
    return _run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *command],
        env=env,
        cwd=BACKEND_DIR,
        timeout=timeout,
    )


def stage_migrate(target: Target) -> StageResult:
    started = time.time()
    result = _alembic(target, "upgrade", "head")
    if result.returncode != 0:
        return StageResult(
            "migrate", "FAIL", _tail(result.stderr or result.stdout), time.time() - started
        )
    current = _alembic(target, "current")
    return StageResult(
        "migrate", "PASS", _tail(current.stdout, lines=1), time.time() - started
    )


def stage_round_trip(target: Target) -> StageResult:
    started = time.time()
    for command in (("downgrade", ROUND_TRIP_FLOOR), ("upgrade", "head")):
        result = _alembic(target, *command)
        if result.returncode != 0:
            return StageResult(
                "round-trip",
                "FAIL",
                f"alembic {' '.join(command)}: {_tail(result.stderr or result.stdout)}",
                time.time() - started,
            )
    return StageResult(
        "round-trip",
        "PASS",
        f"head -> {ROUND_TRIP_FLOOR} -> head",
        time.time() - started,
    )


def _seed_guard_fixture(psql: Psql) -> str:
    """Give the clear script an accepted generation to retire.

    Only a database with no rebuildable state at all is seeded; a restored dump
    keeps its own generation and must be addressed with the real key.
    """
    rebuildable = psql.scalar(EMPTINESS_PROBE_SQL)
    if rebuildable != "0":
        return "existing generation reused (database is not empty)"
    result = psql.script(SEED_FIXTURE_SQL)
    if result.returncode != 0:
        raise CheckError(f"cannot seed the guard fixture: {_tail(result.stderr or result.stdout)}")
    return f"seeded accepted generation '{SEED_GENERATION_KEY}'"


def stage_clear(psql: Psql, target: Target, args: argparse.Namespace) -> StageResult:
    started = time.time()
    if not target.owned and not args.allow_destructive_clear:
        return StageResult(
            "clear",
            "SKIP",
            "refusing to COMMIT the destructive clear on a database this run did not create; "
            "pass --allow-destructive-clear when the target is a restored copy",
        )
    try:
        seeded = "skipped" if args.no_seed else _seed_guard_fixture(psql)
    except CheckError as exc:
        return StageResult("clear", "FAIL", str(exc), time.time() - started)
    expected_key = args.expected_generation_key or (
        SEED_GENERATION_KEY if seeded.startswith("seeded") else None
    )
    if expected_key is None:
        return StageResult(
            "clear",
            "FAIL",
            "--expected-generation-key is required for a database with existing state",
            time.time() - started,
        )
    result = psql.script(
        CLEAR_SQL.read_text(encoding="utf-8"),
        variables={
            "expected_database": args.expected_database or target.database,
            "expected_generation_key": expected_key,
            "confirm": "CLEAR_REBUILDABLE_LEDGER_PROJECTIONS",
        },
    )
    elapsed = time.time() - started
    if result.returncode != 0 or "CLEAR PASS" not in result.stdout:
        return StageResult("clear", "FAIL", _tail(result.stderr or result.stdout), elapsed)
    return StageResult("clear", "PASS", f"CLEAR PASS ({seeded})", elapsed)


def verify_summary_query(sql_text: str) -> str:
    """Extract the verifier's trailing summary projection.

    The ``DO`` block aborts the whole script on the first failed invariant, so on
    an empty database the projection is never reached.  Running it separately is
    what actually proves that every relation and column it names exists.
    """
    marker = "$verify$;"
    end = sql_text.index(marker) + len(marker)
    tail = sql_text[end:]
    echo = tail.index("\\echo")
    query = tail[:echo].strip()
    if not query.upper().startswith("WITH "):
        raise CheckError("cannot locate the verifier summary projection")
    return query


def classify_verify_failure(message: str) -> str | None:
    """Return the matched known-empty pattern, or ``None`` when unexpected."""
    for pattern in KNOWN_EMPTY_VERIFY_FAILURES:
        if re.search(pattern, message):
            return pattern
    return None


def stage_verify(psql: Psql, target: Target, args: argparse.Namespace) -> StageResult:
    started = time.time()
    verify_text = VERIFY_SQL.read_text(encoding="utf-8")
    result = psql.script(
        verify_text,
        variables={
            "expected_database": args.expected_database or target.database,
            "expected_generation_key": args.expected_generation_key or SEED_GENERATION_KEY,
            "expected_cutoff": args.expected_cutoff,
            "expected_fixed_run_count": str(args.expected_fixed_run_count),
            "expected_opening_baseline_at": args.expected_opening_baseline_at,
        },
    )
    elapsed = time.time() - started
    passed = result.returncode == 0 and "PASS: Ledger rebuild invariants verified" in result.stdout
    if passed:
        return StageResult("verify", "PASS", "invariants verified", elapsed)
    output = result.stderr or result.stdout
    if args.strict:
        return StageResult("verify", "FAIL", _tail(output), elapsed)
    matched = classify_verify_failure(output)
    if matched is None:
        return StageResult(
            "verify",
            "FAIL",
            f"failure is not a known-empty one: {_tail(output)}",
            elapsed,
        )
    projection = psql.script(
        "BEGIN TRANSACTION READ ONLY;\n" + verify_summary_query(verify_text) + "\nROLLBACK;\n"
    )
    if projection.returncode != 0:
        return StageResult(
            "verify",
            "FAIL",
            f"summary projection is not executable: {_tail(projection.stderr or projection.stdout)}",
            time.time() - started,
        )
    return StageResult(
        "verify",
        "PASS",
        f"smoke: executable; known-empty failure [{matched}]; summary projection executable",
        time.time() - started,
    )


def _tail(text: str, *, lines: int = 6) -> str:
    stripped = [line.rstrip() for line in (text or "").splitlines() if line.strip()]
    return " | ".join(stripped[-lines:])


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dsn", default=os.environ.get("PRODPLAN_PG_CHECK_DSN"))
    parser.add_argument("--host", default=None, help="existing server; omit to start a disposable one")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--user", default=os.environ.get("PRODPLAN_PG_CHECK_USER", "prodplan"))
    parser.add_argument(
        "--password", default=os.environ.get("PRODPLAN_PG_CHECK_PASSWORD", "prodplan_check")
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("PRODPLAN_PG_CHECK_DB", "prodplan_pgcheck"),
        help="database to work in; on an existing server it is used as-is (restored dump)",
    )
    parser.add_argument("--image", default="postgres:15")
    parser.add_argument("--container-name", default=None)
    parser.add_argument("--psql-container", default=None, help="run psql inside this running container")
    parser.add_argument("--psql-binary", default=None)
    parser.add_argument("--startup-timeout", type=int, default=120)
    parser.add_argument("--keep", action="store_true", help="keep the disposable container for debugging")
    parser.add_argument(
        "--stages",
        default="all",
        help=f"comma-separated subset of {','.join(STAGE_NAMES)} (default: all)",
    )
    parser.add_argument("--strict", action="store_true", help="any verifier failure is a failure")
    parser.add_argument("--no-seed", action="store_true", help="never seed the clear-script guard fixture")
    parser.add_argument("--allow-destructive-clear", action="store_true")
    parser.add_argument("--expected-database", default=None)
    parser.add_argument("--expected-generation-key", default=None)
    parser.add_argument("--expected-cutoff", default=SEED_CUTOFF)
    parser.add_argument("--expected-fixed-run-count", type=int, default=DEFAULT_FIXED_RUN_COUNT)
    parser.add_argument("--expected-opening-baseline-at", default=DEFAULT_OPENING_BASELINE_AT)
    return parser


def _apply_dsn(args: argparse.Namespace) -> None:
    if not args.dsn:
        return
    parsed = urlparse(args.dsn)
    if parsed.scheme.split("+")[0] not in {"postgres", "postgresql"}:
        raise CheckError(f"unsupported DSN scheme: {parsed.scheme}")
    args.host = parsed.hostname or args.host or "127.0.0.1"
    args.port = parsed.port or args.port or 5432
    args.user = parsed.username or args.user
    args.password = parsed.password or args.password
    if parsed.path.strip("/"):
        args.database = parsed.path.strip("/")


def _selected_stages(raw: str) -> tuple[str, ...]:
    if raw.strip() == "all":
        return STAGE_NAMES
    selected = tuple(name.strip() for name in raw.split(",") if name.strip())
    unknown = [name for name in selected if name not in STAGE_NAMES]
    if unknown:
        raise CheckError(f"unknown stage(s): {', '.join(unknown)}")
    return selected


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _apply_dsn(args)
    stages = _selected_stages(args.stages)

    owned_target: Target | None = None
    results: list[StageResult] = []
    try:
        if args.host:
            target = Target(
                host=args.host,
                port=args.port or 5432,
                user=args.user,
                password=args.password,
                database=args.database,
            )
            print(f"target: existing server {target.host}:{target.port}/{target.database}")
        else:
            target = start_disposable_server(args)
            owned_target = target
            print(
                f"target: disposable {args.image} container {target.container} "
                f"on 127.0.0.1:{target.port}/{target.database}"
            )
        psql = Psql(
            target,
            image=args.image,
            binary=args.psql_binary,
            container=args.psql_container,
        )

        for index, name in enumerate(stages, start=1):
            print(f"[{index}/{len(stages)}] {name} ...", flush=True)
            if name == "migrate":
                result = stage_migrate(target)
            elif name == "round-trip":
                result = stage_round_trip(target)
            elif name == "clear":
                result = stage_clear(psql, target, args)
            else:
                result = stage_verify(psql, target, args)
            results.append(result)
            print(f"    {result.status} ({result.seconds:.1f}s) {result.detail}", flush=True)
            if result.status == "FAIL":
                break
    except CheckError as exc:
        results.append(StageResult("setup", "FAIL", str(exc)))
        print(f"    FAIL {exc}", file=sys.stderr)
    finally:
        if owned_target is not None and owned_target.container and not args.keep:
            _run(["docker", "rm", "-f", owned_target.container], timeout=300)

    print("\nsummary")
    for result in results:
        print(f"  {result.status:<4} {result.name:<11} {result.detail}")
    failed = [result for result in results if result.status == "FAIL"]
    verdict = "FAIL" if failed else "PASS"
    print(f"  ---- overall: {verdict} ({'smoke' if not args.strict else 'strict'} mode)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

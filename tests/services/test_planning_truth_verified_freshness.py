"""Decision §57: a converged 1C check without a delta extends freshness.

Freshness of the accepted generation is the age of the last successful
reconciliation with 1C, not of the last publication.  A weekend without
postings produces no successor generation at all, and before §57 every HTTP
reader answered 503 ``planning_truth_unavailable`` until the first Monday
document arrived.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app import models
from app.services import planning_truth


REPO_ROOT = Path(__file__).resolve().parents[2]


ANCHOR = datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc)
MAX_AGE_SECONDS = 24 * 3600


def _accepted(db, key: str, *, accepted_at: datetime) -> models.LedgerGeneration:
    generation = models.LedgerGeneration(
        generation_key=key,
        status="accepted",
        cutoff=accepted_at,
        accepted_at=accepted_at,
        source_watermarks={"1c": accepted_at.isoformat()},
        capabilities={planning_truth.CAPABILITY_PHYSICAL_LEDGER: True},
        algorithm_version="verified-freshness-test/1",
        replay_version="verified-freshness-test/1",
        physical_import_batch=models.PhysicalImportBatch(
            batch_key=f"physical-{key}",
            status="completed",
            cutoff=accepted_at,
            source_watermarks={},
        ),
    )
    planning_truth.publish_generation(db, generation)
    db.commit()
    return generation


@pytest.fixture
def threshold(monkeypatch):
    monkeypatch.setenv(
        planning_truth.TRUTH_MAX_AGE_SECONDS_ENV, str(MAX_AGE_SECONDS)
    )
    return MAX_AGE_SECONDS


def test_pointer_is_stale_without_a_verification(db_session, threshold):
    _accepted(db_session, "quiet-weekend", accepted_at=ANCHOR)

    state = planning_truth.get_readiness(
        db_session, now=ANCHOR + timedelta(hours=40)
    )

    assert state.truth_status == "stale"
    assert state.ready is False
    assert state.verified_cutoff is None


def test_converged_check_without_delta_keeps_the_reader_ready(db_session, threshold):
    generation = _accepted(db_session, "verified-pointer", accepted_at=ANCHOR)
    checked_at = ANCHOR + timedelta(hours=38)

    stored = planning_truth.record_pointer_verification(
        db_session,
        verified_generation_id=int(generation.id),
        verified_cutoff=checked_at,
        balance_convergence_valid=True,
        verified_at=checked_at,
    )
    db_session.commit()

    assert stored == checked_at
    state = planning_truth.get_readiness(
        db_session, now=ANCHOR + timedelta(hours=40)
    )
    assert state.truth_status == "accepted"
    assert state.ready is True
    # Operators read "сверено до ..." straight off the readiness payload.
    assert state.verified_cutoff == checked_at
    assert state.verified_at == checked_at
    assert state.as_dict()["verified_cutoff"] == checked_at
    # The accepted generation row is untouched: lineage stays immutable.
    assert planning_truth._as_utc(generation.cutoff) == ANCHOR
    assert planning_truth._as_utc(generation.accepted_at) == ANCHOR
    # ... and the verification ages like any other reference.
    later = planning_truth.get_readiness(
        db_session, now=checked_at + timedelta(seconds=MAX_AGE_SECONDS + 1)
    )
    assert later.truth_status == "stale"
    assert "max_age_seconds=86400" in later.reason


def test_failed_convergence_never_extends_freshness(db_session, threshold):
    generation = _accepted(db_session, "diverged-check", accepted_at=ANCHOR)

    stored = planning_truth.record_pointer_verification(
        db_session,
        verified_generation_id=int(generation.id),
        verified_cutoff=ANCHOR + timedelta(hours=38),
        balance_convergence_valid=False,
        verified_at=ANCHOR + timedelta(hours=38),
    )
    db_session.commit()

    assert stored is None
    pointer = db_session.get(models.PlanningTruthState, 1)
    assert pointer.verified_generation_id is None
    assert pointer.verified_cutoff is None
    assert planning_truth.get_readiness(
        db_session, now=ANCHOR + timedelta(hours=40)
    ).truth_status == "stale"


def test_check_computed_from_another_generation_is_refused(db_session, threshold):
    _accepted(db_session, "pointer-generation", accepted_at=ANCHOR)
    foreign = models.LedgerGeneration(
        generation_key="foreign-parent",
        status="accepted",
        cutoff=ANCHOR - timedelta(hours=1),
        accepted_at=ANCHOR - timedelta(hours=1),
        source_watermarks={},
        capabilities={},
        algorithm_version="verified-freshness-test/1",
        physical_import_batch=models.PhysicalImportBatch(
            batch_key="physical-foreign-parent",
            status="completed",
            cutoff=ANCHOR - timedelta(hours=1),
            source_watermarks={},
        ),
    )
    db_session.add(foreign)
    db_session.flush()

    stored = planning_truth.record_pointer_verification(
        db_session,
        verified_generation_id=int(foreign.id),
        verified_cutoff=ANCHOR + timedelta(hours=38),
        balance_convergence_valid=True,
        verified_at=ANCHOR + timedelta(hours=38),
    )
    db_session.commit()

    assert stored is None
    pointer = db_session.get(models.PlanningTruthState, 1)
    assert pointer.verified_generation_id is None
    assert planning_truth.get_readiness(
        db_session, now=ANCHOR + timedelta(hours=40)
    ).truth_status == "stale"


def test_verification_moves_forward_only(db_session, threshold):
    generation = _accepted(db_session, "forward-only", accepted_at=ANCHOR)
    far = ANCHOR + timedelta(hours=38)
    planning_truth.record_pointer_verification(
        db_session,
        verified_generation_id=int(generation.id),
        verified_cutoff=far,
        balance_convergence_valid=True,
        verified_at=far,
    )
    db_session.commit()

    backwards = planning_truth.record_pointer_verification(
        db_session,
        verified_generation_id=int(generation.id),
        verified_cutoff=ANCHOR + timedelta(hours=10),
        balance_convergence_valid=True,
        verified_at=ANCHOR + timedelta(hours=39),
    )
    db_session.commit()

    assert backwards is None
    pointer = db_session.get(models.PlanningTruthState, 1)
    assert planning_truth._as_utc(pointer.verified_cutoff) == far
    assert planning_truth.get_readiness(
        db_session, now=ANCHOR + timedelta(hours=40)
    ).ready is True


def test_check_dated_past_its_own_moment_buys_no_freshness(db_session, threshold):
    """A cutoff can only vouch for the span the check actually read."""
    generation = _accepted(db_session, "future-cutoff", accepted_at=ANCHOR)

    stored = planning_truth.record_pointer_verification(
        db_session,
        verified_generation_id=int(generation.id),
        verified_cutoff=ANCHOR + timedelta(days=30),
        balance_convergence_valid=True,
        verified_at=ANCHOR + timedelta(minutes=5),
    )
    db_session.commit()

    assert stored is not None
    assert planning_truth.get_readiness(
        db_session, now=ANCHOR + timedelta(hours=40)
    ).truth_status == "stale"


def test_new_accepted_generation_supersedes_the_verification(db_session, threshold):
    generation = _accepted(db_session, "verified-before", accepted_at=ANCHOR)
    checked_at = ANCHOR + timedelta(hours=38)
    planning_truth.record_pointer_verification(
        db_session,
        verified_generation_id=int(generation.id),
        verified_cutoff=checked_at,
        balance_convergence_valid=True,
        verified_at=checked_at,
    )
    db_session.commit()
    assert planning_truth.get_readiness(
        db_session, now=ANCHOR + timedelta(hours=40)
    ).ready is True

    # An obligation refresh inherits the parent cutoff: the successor carries
    # no reconciliation of its own, so the old proof stops counting at once.
    successor = _accepted(db_session, "obligation-successor", accepted_at=ANCHOR)

    state = planning_truth.get_readiness(
        db_session, now=ANCHOR + timedelta(hours=40)
    )
    assert state.ledger_generation == successor.id
    assert state.truth_status == "stale"
    assert state.verified_cutoff is None
    pointer = db_session.get(models.PlanningTruthState, 1)
    assert int(pointer.verified_generation_id) == int(generation.id)


def test_verified_freshness_migration_is_linear_head_and_reversible(tmp_path):
    """The columns arrive at head and the revision steps back cleanly."""
    path = (
        REPO_ROOT
        / "backend/alembic/versions/20260925_01_planning_truth_verified_freshness.py"
    )
    spec = importlib.util.spec_from_file_location("planning_truth_verified", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.revision == "20260925_01"
    assert module.down_revision == "20260924_03"

    config = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert list(script.get_heads()) == ["20260925_01"]

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tests" / "alembic_sqlite_upgrade.py"),
            str(tmp_path / "verified-freshness.db"),
            "--step-down",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    payload = json.loads(result.stdout.split("---JSON---", 1)[1])
    added = {"verified_generation_id", "verified_cutoff", "verified_at"}
    assert added <= set(payload["tables"]["planning_truth_state"])
    assert not added & set(payload["tables_after_step_down"]["planning_truth_state"])
    assert "current_generation_id" in payload["tables_after_step_down"]["planning_truth_state"]

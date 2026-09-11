"""Schema contract for canonical material coverage ownership."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory

from app import models


REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS_DIR = REPO_ROOT / "backend" / "alembic" / "versions"


def _load_migration(filename: str, module_name: str):
    path = VERSIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_line_state_no_longer_owns_material_coverage():
    columns = set(models.ProductionOrderLineState.__table__.c.keys())
    assert not {
        "material_coverage_status",
        "material_coverage_label",
        "material_coverage_calculated_at",
        "material_coverage_ledger_generation_id",
        "material_coverage_snapshot",
    } & columns


def test_material_cache_drop_migration_is_linear_head():
    module = _load_migration(
        "20260731_03_drop_material_coverage_cache.py",
        "drop_material_coverage_cache",
    )
    assert module.revision == "20260731_03"
    assert module.down_revision == "20260731_02"

    # Граф миграций остаётся линейным: ровно одна голова, кто бы её ни двигал.
    config = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1


def test_ownership_migrations_upgrade_on_disposable_sqlite(tmp_path):
    """The disposable schema reaches head; the current cutover is irreversible."""
    db_path = tmp_path / "ownership-upgrade.db"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tests" / "alembic_sqlite_upgrade.py"),
            str(db_path),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "---JSON---" in result.stdout
    schema = json.loads(result.stdout.split("---JSON---", 1)[1])['tables']
    purchase_columns = set(schema["purchase_export_batch"])
    assert "planning_read_snapshot_id" not in purchase_columns
    assert {
        "current_execution_scope_id",
        "current_execution_source_revision",
    } <= purchase_columns

    cutover_tests = (
        REPO_ROOT / "tests" / "tools" / "test_r10_purchase_anchor_cutover.py"
    ).read_text(encoding="utf-8")
    assert (
        "test_cutover_removes_legacy_anchor_after_verified_current_mapping_and_downgrade_fails_closed"
        in cutover_tests
    )

"""Schema contract for generation-pinned material coverage snapshots."""

import importlib.util
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


def test_material_coverage_snapshot_has_nullable_generation_lineage():
    table = models.ProductionOrderLineState.__table__
    column = table.c.material_coverage_ledger_generation_id
    assert column.nullable
    assert {
        (foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in column.foreign_keys
    } == {("ledger_generation.id", "RESTRICT")}
    assert (
        "ix_prod_line_state_coverage_generation"
        in {index.name for index in table.indexes}
    )


def test_material_coverage_generation_migration_follows_head():
    module = _load_migration(
        "20260730_01_material_coverage_generation.py",
        "material_coverage_generation_migration",
    )
    assert module.revision == "20260730_01"
    assert module.down_revision == "20260726_14"


def test_ownership_migration_names_fit_postgresql_limit():
    material = _load_migration(
        "20260730_01_material_coverage_generation.py",
        "material_coverage_generation_names",
    )
    spec_index = _load_migration(
        "20260730_02_spec_components_spec_index.py",
        "spec_components_index_names",
    )

    names = [
        material.INDEX_NAME,
        material.FK_NAME,
        material.LEGACY_INDEX_NAME,
        material.LEGACY_FK_NAME,
        spec_index.INDEX_NAME,
    ]
    assert all(len(name.encode("utf-8")) <= 63 for name in names)


def test_alembic_has_one_head():
    config = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "alembic"),
    )
    assert ScriptDirectory.from_config(config).get_heads() == ["20260730_02"]


def test_ownership_migrations_round_trip_on_disposable_sqlite(tmp_path):
    db_path = tmp_path / "ownership-roundtrip.db"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tests" / "alembic_sqlite_upgrade.py"),
            str(db_path),
            "--round-trip",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "---JSON---" in result.stdout


class _Inspector:
    def __init__(self, *, legacy: bool):
        self.legacy = legacy

    def get_table_names(self):
        return ["production_order_line_states"]

    def get_columns(self, _table):
        return [{"name": "material_coverage_ledger_generation_id"}]

    def get_indexes(self, _table):
        if not self.legacy:
            return []
        return [{
            "name": "ix_production_order_line_states_material_coverage_ledger_genera",
            "column_names": ["material_coverage_ledger_generation_id"],
        }]

    def get_foreign_keys(self, _table):
        if not self.legacy:
            return []
        return [{
            "name": "fk_production_order_line_states_material_coverage_ledger_genera",
            "constrained_columns": ["material_coverage_ledger_generation_id"],
            "referred_table": "ledger_generation",
            "referred_columns": ["id"],
        }]


class _Operations:
    def __init__(self):
        self.calls = []

    def get_bind(self):
        return object()

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record


def test_upgrade_accepts_postgresql_truncated_legacy_objects(monkeypatch):
    module = _load_migration(
        "20260730_01_material_coverage_generation.py",
        "material_coverage_generation_legacy_upgrade",
    )
    operations = _Operations()
    monkeypatch.setattr(module, "op", operations)
    monkeypatch.setattr(module.sa, "inspect", lambda _bind: _Inspector(legacy=True))

    module.upgrade()

    assert operations.calls == []


def test_downgrade_drops_postgresql_truncated_legacy_objects(monkeypatch):
    module = _load_migration(
        "20260730_01_material_coverage_generation.py",
        "material_coverage_generation_legacy_downgrade",
    )
    operations = _Operations()
    monkeypatch.setattr(module, "op", operations)
    monkeypatch.setattr(module.sa, "inspect", lambda _bind: _Inspector(legacy=True))

    module.downgrade()

    assert [call[0] for call in operations.calls] == [
        "drop_constraint",
        "drop_index",
        "drop_column",
    ]
    assert operations.calls[0][1][0] == module.LEGACY_FK_NAME
    assert operations.calls[1][1][0] == module.LEGACY_INDEX_NAME

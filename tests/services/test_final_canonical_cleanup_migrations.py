from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


VERSIONS = Path(__file__).parents[2] / "backend/alembic/versions"


def _load(filename: str):
    path = VERSIONS / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_requirement_cache_cleanup_drops_only_mutable_caches(monkeypatch) -> None:
    migration = _load("20260726_14_drop_mrp_requirement_execution_caches.py")
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "mrp_requirement",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("net_required_qty", sa.Numeric, nullable=False),
        *[sa.Column(name, sa.Numeric) for name in migration.CACHE_COLUMNS],
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(connection))
        )
        migration.upgrade()
        columns = {
            row["name"]
            for row in sa.inspect(connection).get_columns("mrp_requirement")
        }
    assert {"id", "net_required_qty"} <= columns
    assert not (set(migration.CACHE_COLUMNS) & columns)


@pytest.mark.parametrize(
    "filename",
    [
        "20260726_14_drop_mrp_requirement_execution_caches.py",
    ],
)
def test_destructive_cleanup_requires_backup_for_downgrade(filename: str) -> None:
    with pytest.raises(RuntimeError, match="database backup"):
        _load(filename).downgrade()

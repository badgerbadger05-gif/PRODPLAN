"""Schema contract for generation-pinned material coverage snapshots."""

import importlib.util
from pathlib import Path

from app import models


def test_material_coverage_snapshot_has_nullable_generation_lineage():
    table = models.ProductionOrderLineState.__table__
    column = table.c.material_coverage_ledger_generation_id
    assert column.nullable
    assert {
        (foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in column.foreign_keys
    } == {("ledger_generation.id", "RESTRICT")}
    assert (
        "ix_production_order_line_states_material_coverage_ledger_generation_id"
        in {index.name for index in table.indexes}
    )


def test_material_coverage_generation_migration_follows_head():
    path = (
        Path(__file__).resolve().parents[2]
        / "backend/alembic/versions/20260730_01_material_coverage_generation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "material_coverage_generation_migration",
        path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert module.revision == "20260730_01"
    assert module.down_revision == "20260726_14"

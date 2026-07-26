import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION = Path(
    "backend/alembic/versions/20260726_06_drop_legacy_dbr.py"
)


def test_legacy_dbr_cleanup_is_forward_only_and_preserves_takt_master():
    source = MIGRATION.read_text()

    assert 'down_revision = "20260726_05"' in source
    assert '"dbr_assembly_rate"' not in source.split(
        "LEGACY_TABLES_IN_DROP_ORDER =", 1
    )[1].split(")", 1)[0]
    for table_name in (
        "dbr_feeder_signal",
        "dbr_supermarket_position",
        "dbr_drum_schedule",
        "dbr_production_program",
        "dbr_settings",
    ):
        assert f'"{table_name}"' in source
    assert 'batch_op.drop_column("source_dbr_signal_id")' in source
    assert "raise RuntimeError" in source


def _load_migration():
    spec = importlib.util.spec_from_file_location("legacy_dbr_cleanup", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_drops_legacy_tables_and_source_link_but_keeps_rates(
    monkeypatch,
):
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    feeder = sa.Table(
        "dbr_feeder_signal",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    products = sa.Table(
        "production_products",
        metadata,
        sa.Column("product_id", sa.Integer, primary_key=True),
        sa.Column(
            "source_dbr_signal_id",
            sa.Integer,
            sa.ForeignKey(
                feeder.c.id,
                name="fk_production_products_source_dbr_signal_id_dbr_feeder_signal",
            ),
        ),
    )
    sa.Index(
        "ix_production_products_source_dbr_signal_id",
        products.c.source_dbr_signal_id,
    )
    sa.Index(
        "ux_production_products_source_dbr_signal",
        products.c.source_dbr_signal_id,
        unique=True,
    )
    sa.Table(
        "dbr_assembly_rate",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    for table_name in migration.LEGACY_TABLES_IN_DROP_ORDER:
        if table_name == "dbr_feeder_signal":
            continue
        sa.Table(
            table_name,
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
        )
    metadata.create_all(engine)

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()
        inspector = sa.inspect(connection)
        remaining = set(inspector.get_table_names())
        product_columns = {
            row["name"] for row in inspector.get_columns("production_products")
        }

    assert "dbr_assembly_rate" in remaining
    assert "production_products" in remaining
    assert not (set(migration.LEGACY_TABLES_IN_DROP_ORDER) & remaining)
    assert "source_dbr_signal_id" not in product_columns


def test_downgrade_requires_backup():
    with pytest.raises(RuntimeError, match="cannot be downgraded"):
        _load_migration().downgrade()

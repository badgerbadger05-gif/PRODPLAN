"""Схема должна воспроизводиться из Alembic, а не из ``Base.metadata.create_all``.

Исторически десяток таблиц (units, default_specifications, production_stages,
production_components, production_operations, production_plan_entries,
resource_stages, root_products, spec_operations, item_embeddings) плюс базовые
справочники (items, item_categories, specifications, spec_components,
operations, production_orders, production_products, production_resources)
не создавались ни одной миграцией и жили только за счёт ``create_all`` в
``app/main.py``. На чистой БД цепочка миграций даже не доходила до конца.

Тест прогоняет ``alembic upgrade head`` на пустом SQLite и сверяет полученную
схему с ``Base.metadata``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.database import Base
from app import models  # noqa: F401  — регистрирует таблицы в Base.metadata

REPO_ROOT = Path(__file__).resolve().parents[1]
UPGRADE_SCRIPT = Path(__file__).with_name("alembic_sqlite_upgrade.py")

# Таблицы, которые миграции создают сознательно, но в ORM их нет.
EXPECTED_EXTRA_TABLES = {
    # служебная таблица Alembic
    "alembic_version",
    # архивы legacy-бакетов из 20251205_08: держатся ради истории, из ORM убраны
    "mrp_bucket_type_legacy",
    "planning_run_bucket_modes",
}

# Таблицы из корневой ревизии 20250924_00: для них сверяем ещё и состав колонок,
# потому что их «домиграционная» форма восстановлена вручную.
BASELINE_TABLES = (
    "item_categories",
    "items",
    "units",
    "production_stages",
    "specifications",
    "spec_components",
    "operations",
    "spec_operations",
    "production_orders",
    "production_products",
    "production_components",
    "production_operations",
    "default_specifications",
    "root_products",
    "production_resources",
    "resource_stages",
    "production_plan_entries",
    "item_embeddings",
)


@pytest.fixture(scope="module")
def migrated_schema(tmp_path_factory) -> dict:
    """Схема чистой SQLite-БД после ``alembic upgrade head``.

    Подпроцесс нужен из-за глобальных SQLite-заглушек (см. docstring
    ``alembic_sqlite_upgrade``): в общем процессе они утекли бы в другие тесты.
    """
    db_path = tmp_path_factory.mktemp("alembic") / "schema.db"
    result = subprocess.run(
        [sys.executable, str(UPGRADE_SCRIPT), str(db_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        pytest.fail(
            "alembic upgrade head упал на чистом SQLite:\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )
    marker = "---JSON---"
    assert marker in result.stdout, result.stdout
    payload = result.stdout.split(marker, 1)[1].strip()
    return json.loads(payload)["tables"]


def test_migrations_create_every_orm_table(migrated_schema):
    missing = sorted(set(Base.metadata.tables) - set(migrated_schema))
    assert not missing, (
        "Эти таблицы есть в models.py, но их не создаёт ни одна миграция "
        f"(схема держится только на create_all): {missing}"
    )


def test_migrations_do_not_create_unexpected_tables(migrated_schema):
    extra = sorted(set(migrated_schema) - set(Base.metadata.tables) - EXPECTED_EXTRA_TABLES)
    assert not extra, (
        "Миграции создают таблицы, которых нет ни в models.py, ни в списке "
        f"известных исключений: {extra}"
    )


@pytest.mark.parametrize("table_name", BASELINE_TABLES)
def test_baseline_tables_match_orm_columns(migrated_schema, table_name):
    """Корневая ревизия + последующие ALTER должны давать ровно форму models.py."""
    assert table_name in migrated_schema
    from_db = set(migrated_schema[table_name])
    from_orm = {column.name for column in Base.metadata.tables[table_name].columns}
    assert from_db == from_orm, (
        f"{table_name}: нет в БД {sorted(from_orm - from_db)}, "
        f"лишние в БД {sorted(from_db - from_orm)}"
    )

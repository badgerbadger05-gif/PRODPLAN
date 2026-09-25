"""Прогон ``alembic upgrade head`` на чистом SQLite (запускается подпроцессом).

Модуль намеренно НЕ называется ``test_*``: pytest его не собирает.
Тест ``test_alembic_schema_reproducibility`` запускает его отдельным процессом,
потому что для SQLite приходится ставить совместимостные заплатки на классы
SQLAlchemy/Alembic, а они глобальные и не должны протекать в остальные тесты.

Заплатки закрывают только то, чего SQLite не умеет в принципе и что не влияет
на СОСТАВ схемы:

* ``JSONB`` / ``UUID`` из диалекта PostgreSQL — рендерим как ``JSON`` / ``VARCHAR``;
* ``BIGINT`` первичный ключ — SQLite автоинкрементит только ``INTEGER PRIMARY KEY``
  (в ORM это уже решено через ``BigInteger().with_variant(Integer, "sqlite")``,
  в миграциях типы записаны как ``sa.BigInteger()``);
* ``ALTER TABLE ... ADD/DROP CONSTRAINT`` и ``ALTER COLUMN`` — SQLite их не
  поддерживает вне batch-режима;
* в batch-режиме — ``drop_constraint`` для CHECK, который из-за предыдущего
  пункта до базы не доехал.

Результат печатается в stdout как JSON: ``{"tables": {name: [columns...]}}``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"


def _install_sqlite_compat() -> None:
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
    from sqlalchemy.ext.compiler import compiles

    @compiles(postgresql.JSONB, "sqlite")
    def _jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN202
        return "JSON"

    @compiles(postgresql.UUID, "sqlite")
    def _uuid_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN202
        return "VARCHAR(36)"

    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"

    from alembic.ddl.sqlite import SQLiteImpl

    SQLiteImpl.add_constraint = lambda self, const: None
    SQLiteImpl.drop_constraint = lambda self, const: None
    SQLiteImpl.alter_column = lambda self, table_name, column_name, **kw: None

    from alembic.operations.batch import ApplyBatchImpl

    original_drop = ApplyBatchImpl.drop_constraint

    def _tolerant_drop(self, const):  # noqa: ANN001, ANN202
        try:
            original_drop(self, const)
        except ValueError:
            # Ограничение не доехало до SQLite (см. заглушку add_constraint).
            pass

    ApplyBatchImpl.drop_constraint = _tolerant_drop


def _tables() -> dict[str, list[str]]:
    from sqlalchemy import create_engine, inspect

    engine = create_engine(os.environ["DATABASE_URL"])
    inspector = inspect(engine)
    tables = {
        name: sorted(col["name"] for col in inspector.get_columns(name))
        for name in inspector.get_table_names()
    }
    engine.dispose()
    return tables


def main(db_path: str, *, round_trip: bool = False, step_down: bool = False) -> int:
    sys.path.insert(0, str(BACKEND))
    os.chdir(BACKEND)
    os.environ["DATABASE_URL"] = "sqlite:///" + db_path.replace("\\", "/")

    _install_sqlite_compat()

    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    command.upgrade(config, "head")
    if round_trip:
        command.downgrade(config, "20260726_14")
        command.upgrade(config, "head")

    # Полный round trip невозможен (есть необратимые cutover-ревизии), поэтому
    # обратимость новой головы проверяется на один шаг: вниз и снова вверх.
    stepped: dict[str, list[str]] | None = None
    if step_down:
        command.downgrade(config, "-1")
        stepped = _tables()
        command.upgrade(config, "head")

    payload: dict[str, object] = {"tables": _tables()}
    if stepped is not None:
        payload["tables_after_step_down"] = stepped

    print("---JSON---")
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(
        main(
            sys.argv[1],
            round_trip="--round-trip" in sys.argv[2:],
            step_down="--step-down" in sys.argv[2:],
        )
    )

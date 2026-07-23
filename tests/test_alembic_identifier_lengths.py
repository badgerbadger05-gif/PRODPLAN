"""PostgreSQL identifier guard for explicitly named Alembic objects."""

from __future__ import annotations

import ast
from pathlib import Path


POSTGRES_IDENTIFIER_LIMIT = 63
VERSIONS_DIR = Path(__file__).resolve().parents[1] / "backend" / "alembic" / "versions"

_OP_NAME_METHODS = {
    "create_index",
    "drop_index",
    "create_foreign_key",
    "drop_constraint",
    "create_unique_constraint",
    "create_primary_key",
    "create_check_constraint",
}
_SA_NAMED_CONSTRAINTS = {
    "CheckConstraint",
    "ForeignKeyConstraint",
    "UniqueConstraint",
    "PrimaryKeyConstraint",
}


def _literal_migration_names(path: Path) -> list[tuple[str, str, int]]:
    """Return explicit literal operation/constraint names with source lines.

    Dynamic f-strings are intentionally excluded: this guard covers only names
    that are spelled out in migrations, where PostgreSQL's 63-byte limit can be
    checked without executing schema changes.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Name):
            continue
        if owner.id == "op" and node.func.attr in _OP_NAME_METHODS and node.args:
            name = node.args[0]
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                names.append((name.value, node.func.attr, node.lineno))
        if owner.id == "sa" and node.func.attr in _SA_NAMED_CONSTRAINTS:
            for keyword in node.keywords:
                if (
                    keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    names.append((keyword.value.value, node.func.attr, node.lineno))
    return names


def test_explicit_alembic_names_fit_postgresql_identifier_limit():
    too_long: list[str] = []
    for migration in sorted(VERSIONS_DIR.glob("*.py")):
        for name, operation, line in _literal_migration_names(migration):
            if len(name.encode("utf-8")) > POSTGRES_IDENTIFIER_LIMIT:
                too_long.append(
                    f"{migration.name}:{line} {operation} {name!r} "
                    f"({len(name.encode('utf-8'))} bytes)"
                )
    assert not too_long, "PostgreSQL identifiers must be at most 63 bytes:\n" + "\n".join(too_long)

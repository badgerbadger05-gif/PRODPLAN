from types import SimpleNamespace
import inspect

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import models
from app.database import (
    PRODUCTION_READ_SNAPSHOT_INFO_KEY,
    get_db,
    production_read_snapshot,
)
from app.routers.production_control import (
    get_order_line_materials,
    get_orders_journal,
    get_production_read_db,
    get_work_item_materials,
    post_material_issues,
    print_route_sheets,
)
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    _require_current_execution_manifest,
)


class _ContextDB:
    def __init__(self, dialect: str):
        self.info = {}
        self.statements = []
        self._dialect = dialect

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self._dialect))

    def execute(self, statement):
        self.statements.append(str(statement))


def test_production_read_snapshot_is_transaction_local_and_restores_flag():
    db = _ContextDB("postgresql")

    with production_read_snapshot(db) as same:
        assert same is db
        assert db.info[PRODUCTION_READ_SNAPSHOT_INFO_KEY] is True
        assert db.statements == [
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        ]

    assert PRODUCTION_READ_SNAPSHOT_INFO_KEY not in db.info


def test_sqlite_read_snapshot_sets_flag_without_postgresql_statement():
    db = _ContextDB("sqlite")

    with production_read_snapshot(db):
        assert db.info[PRODUCTION_READ_SNAPSHOT_INFO_KEY] is True

    assert db.statements == []
    assert PRODUCTION_READ_SNAPSHOT_INFO_KEY not in db.info


class _ManifestQuery:
    def __init__(self, db, entity):
        self.db = db
        self.entity = entity

    def filter(self, *args, **kwargs):
        return self

    def populate_existing(self):
        return self

    def with_for_update(self, **kwargs):
        self.db.lock_calls.append((self.entity, kwargs))
        return self

    def one_or_none(self):
        if self.entity is models.CurrentExecutionScope:
            return self.db.manifest
        if self.entity is models.PlanningTruthState:
            return self.db.pointer
        if self.entity is models.LedgerGeneration:
            return self.db.generation
        raise AssertionError(self.entity)


class _ManifestDB:
    def __init__(self, *, read_snapshot: bool, manifest_generation: int = 7):
        self.info = (
            {PRODUCTION_READ_SNAPSHOT_INFO_KEY: True}
            if read_snapshot else {}
        )
        self.new = ()
        self.dirty = ()
        self.lock_calls = []
        self.manifest = models.CurrentExecutionScope(
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
            source_revision="rev-7",
            source_generation_id=manifest_generation,
            result_ready=True,
            content_hash="hash",
            summary={},
        )
        self.pointer = models.PlanningTruthState(id=1, current_generation_id=7)
        self.generation = models.LedgerGeneration(id=7, status="accepted")

    def query(self, entity):
        return _ManifestQuery(self, entity)


def test_read_snapshot_omits_manifest_lock_but_mutation_path_keeps_it():
    read_db = _ManifestDB(read_snapshot=True)
    manifest = _require_current_execution_manifest(
        read_db,
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    )
    assert manifest.source_revision == "rev-7"
    assert read_db.lock_calls == []

    mutation_db = _ManifestDB(read_snapshot=False)
    _require_current_execution_manifest(
        mutation_db,
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    )
    assert mutation_db.lock_calls == [
        (models.CurrentExecutionScope, {
            "read": True,
            "of": models.CurrentExecutionScope,
        })
    ]


@pytest.mark.parametrize("failure", ["missing", "stale"])
def test_read_snapshot_still_fails_closed_for_missing_or_stale_scope(failure):
    db = _ManifestDB(read_snapshot=True, manifest_generation=6 if failure == "stale" else 7)
    if failure == "missing":
        db.manifest = None

    with pytest.raises(CurrentExecutionUnavailable):
        _require_current_execution_manifest(
            db,
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
        )


def test_nested_dependency_reuses_overridden_get_db_session(db_session):
    app = FastAPI()

    @app.get("/read")
    def read(db=Depends(get_production_read_db)):
        return {
            "same": db is db_session,
            "snapshot": bool(db.info.get(PRODUCTION_READ_SNAPSHOT_INFO_KEY)),
        }

    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        response = client.get("/read")

    assert response.status_code == 200
    assert response.json() == {"same": True, "snapshot": True}
    assert PRODUCTION_READ_SNAPSHOT_INFO_KEY not in db_session.info


def test_read_snapshot_dependency_is_limited_to_three_pure_gets():
    for endpoint in (
        get_orders_journal,
        get_order_line_materials,
        get_work_item_materials,
    ):
        assert inspect.signature(endpoint).parameters["db"].default.dependency is get_production_read_db

    assert inspect.signature(print_route_sheets).parameters["db"].default.dependency is get_db
    assert inspect.signature(post_material_issues).parameters["db"].default.dependency is get_db

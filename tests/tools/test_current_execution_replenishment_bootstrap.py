"""Contracts of the one-off ``--phase replenishment-bootstrap`` migration step.

A freshly migrated database has no R4 current replenishment state at all: the
bounded physical path only replays the BUY scopes a delta touched.  This phase
calls the one canonical accepted-generation writer; the tests here pin the
guards around that call, so the real writer is patched on purpose.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from tools.current_execution_migration import (
    PostflightBlocked,
    PreflightBlocked,
    apply_current_replenishment_bootstrap,
)


def _engine():
    return create_engine("sqlite:///:memory:")


def _schema(engine):
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE planning_truth_state ("
            "id INTEGER PRIMARY KEY, current_generation_id INTEGER)"
        ))
        connection.execute(text(
            "CREATE TABLE ledger_generation (id INTEGER PRIMARY KEY, status TEXT NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE stock_ledger_entry ("
            "id INTEGER PRIMARY KEY, qty NUMERIC NOT NULL, active BOOLEAN NOT NULL DEFAULT 1)"
        ))
        connection.execute(text(
            "CREATE TABLE stock_ledger_supplier_receipt_provenance ("
            "id INTEGER PRIMARY KEY, ledger_generation_id INTEGER NOT NULL, "
            "stock_ledger_entry_id INTEGER NOT NULL, match_status TEXT NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE current_replenishment_state ("
            "id INTEGER PRIMARY KEY, scope_key TEXT NOT NULL, status TEXT NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE reservation_consumption_allocation ("
            "id INTEGER PRIMARY KEY, allocation_role TEXT NOT NULL, is_current BOOLEAN NOT NULL)"
        ))


def _seed(engine, *, pointer=7, provenance_generation=7, exact_rows=2):
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO ledger_generation (id, status) VALUES (7, 'accepted'), (6, 'accepted')"
        ))
        connection.execute(
            text("INSERT INTO planning_truth_state (id, current_generation_id) VALUES (1, :pointer)"),
            {"pointer": int(pointer)},
        )
        for index in range(1, int(exact_rows) + 1):
            connection.execute(
                text("INSERT INTO stock_ledger_entry (id, qty, active) VALUES (:id, 5, 1)"),
                {"id": index},
            )
            connection.execute(text(
                "INSERT INTO stock_ledger_supplier_receipt_provenance "
                "(id, ledger_generation_id, stock_ledger_entry_id, match_status) "
                "VALUES (:id, :generation, :id, 'exact')"
            ), {"id": index, "generation": int(provenance_generation)})


def _writer(*, allocations=3, changed_pairs=4, states=2):
    calls: list[int] = []

    def _apply(session, *, generation_id):
        calls.append(int(generation_id))
        for index in range(1, int(states) + 1):
            session.execute(text(
                "INSERT INTO current_replenishment_state (id, scope_key, status) "
                "VALUES (:id, :scope, 'completed')"
            ), {"id": index, "scope": f"scope-{index}"})
        for index in range(1, int(allocations) + 1):
            session.execute(text(
                "INSERT INTO reservation_consumption_allocation "
                "(id, allocation_role, is_current) VALUES (:id, :role, 1)"
            ), {"id": index, "role": "supplier_receipt" if index % 2 else "consumption"})
        return tuple(
            SimpleNamespace(
                inserted=1, updated=0, deleted=0,
                changed_pairs=int(changed_pairs), audit_events=1, idempotent=False,
            )
            for _ in range(1)
        )

    _apply.calls = calls
    return _apply


def test_bootstrap_requires_explicit_writers_stopped(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed(engine)
    writer = _writer()
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        writer,
    )

    with pytest.raises(PreflightBlocked, match="writers-stopped"):
        apply_current_replenishment_bootstrap(engine, writers_stopped=False)
    assert writer.calls == []


def test_bootstrap_blocks_when_another_generation_owns_the_provenance(monkeypatch):
    """Provenance stays at the generation that computed it; the pointer moved on."""

    engine = _engine()
    _schema(engine)
    _seed(engine, pointer=7, provenance_generation=6)
    writer = _writer()
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        writer,
    )

    with pytest.raises(PreflightBlocked, match=r"owned by g6\(2 rows\)"):
        apply_current_replenishment_bootstrap(engine, writers_stopped=True)
    assert writer.calls == []
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM current_replenishment_state"
        )).scalar_one() == 0


def test_bootstrap_reports_state_pairs_and_allocations_by_role(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed(engine)
    writer = _writer(allocations=3, changed_pairs=4, states=2)
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        writer,
    )

    report = apply_current_replenishment_bootstrap(engine, writers_stopped=True)

    assert writer.calls == [7]
    assert report["status"] == "ready"
    assert report["generation_id"] == 7
    assert report["current_replenishment_state"] == 2
    assert report["changed_pairs"] == 4
    assert report["idempotent"] is False
    assert report["exact_provenance_rows"] == 2
    assert report["exact_provenance_rows_with_qty"] == 2
    assert report["reservation_consumption_allocation_current"] == {
        "total": 3,
        "by_allocation_role": {"consumption": 1, "supplier_receipt": 2},
    }


def test_second_bootstrap_run_reports_no_changes(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed(engine)
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        _writer(allocations=3, changed_pairs=4, states=2),
    )
    first = apply_current_replenishment_bootstrap(engine, writers_stopped=True)

    def _idempotent(session, *, generation_id):
        return (
            SimpleNamespace(
                inserted=0, updated=0, deleted=0,
                changed_pairs=0, audit_events=0, idempotent=True,
            ),
        )

    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        _idempotent,
    )
    second = apply_current_replenishment_bootstrap(engine, writers_stopped=True)

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["changed_pairs"] == 0
    assert second["current_replenishment_state"] == first["current_replenishment_state"]
    assert (
        second["reservation_consumption_allocation_current"]
        == first["reservation_consumption_allocation_current"]
    )


def test_zero_allocations_with_exact_provenance_fails_closed(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed(engine)
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        _writer(allocations=0, changed_pairs=9, states=554),
    )

    with pytest.raises(PostflightBlocked, match="0 current allocations"):
        apply_current_replenishment_bootstrap(engine, writers_stopped=True)

    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT count(*) FROM current_replenishment_state"
        )).scalar_one() == 0


def test_no_supplier_provenance_anywhere_is_a_legitimate_empty_bootstrap(monkeypatch):
    engine = _engine()
    _schema(engine)
    _seed(engine, exact_rows=0)
    monkeypatch.setattr(
        "tools.current_execution_migration.apply_current_replenishment_for_accepted_generation",
        _writer(allocations=0, changed_pairs=0, states=0),
    )

    report = apply_current_replenishment_bootstrap(engine, writers_stopped=True)
    assert report["status"] == "ready"
    assert report["exact_provenance_rows"] == 0
    assert report["reservation_consumption_allocation_current"]["total"] == 0
